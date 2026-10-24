import asyncio
from types import SimpleNamespace

from nio import RoomSendResponse

from openrig_matrix_bridge.bridge import MatrixBridge
from openrig_matrix_bridge.config import AgentConfig
from openrig_matrix_bridge.openrig import GatewayError


def bridge_fixture():
    agent = AgentConfig(
        key="advisor",
        display_name="Advisor",
        session="advisor-lead@kernel",
    )
    bridge = object.__new__(MatrixBridge)
    bridge.config = SimpleNamespace(
        agents={"advisor": agent},
        queue_inbound_messages=True,
        inbound_queue_source="owner@external",
        inbound_message_priority="urgent",
    )
    bridge.locks = {"advisor": asyncio.Lock()}
    bridge.processed_events = set()
    bridge.state = SimpleNamespace(
        save=lambda: None,
        data={
            "pending": {"advisor": []},
            "processed_events": [],
            "rooms": {"advisor": {"room_id": "!advisor:example.org"}},
        },
    )
    bridge.sent = []

    async def send(room_id, text, reply_to=None, **kwargs):
        bridge.sent.append((room_id, text, reply_to, kwargs))
        return RoomSendResponse("$error", room_id)

    bridge._send = send
    return bridge, agent


def test_qitem_create_failure_sends_no_prompt():
    bridge, agent = bridge_fixture()

    class Gateway:
        async def queue_create(self, **kwargs):
            raise GatewayError("queue unavailable")

    bridge.gateway = Gateway()
    room = SimpleNamespace(room_id="!advisor:example.org")

    asyncio.run(
        bridge._enqueue_prompt(
            agent,
            room,
            "$prompt",
            0,
            "do work",
        )
    )

    assert bridge.state.data["pending"]["advisor"] == []
    assert len(bridge.sent) == 1
    assert "nicht an den Agenten gesendet" in bridge.sent[0][1]


def test_claim_failure_keeps_request_retryable():
    bridge, agent = bridge_fixture()
    request = {
        "id": "request-one",
        "event_id": "$prompt",
        "reply_to_event_id": "$prompt",
        "qitem_id": "qitem-one",
        "qitem_phase": "pending",
        "prompt_phase": "pending",
        "text": "do work",
    }
    bridge.state.data["pending"]["advisor"] = [request]
    transcript = SimpleNamespace(stat=lambda: SimpleNamespace(st_size=0))

    class Resolver:
        def resolve(self, session):
            return SimpleNamespace(
                session_id="session-one",
                transcript_path=transcript,
                busy=False,
            )

    class Gateway:
        async def queue_claim(self, qitem_id, session):
            raise GatewayError("claim unavailable")

    bridge.resolver = Resolver()
    bridge.gateway = Gateway()

    asyncio.run(
        MatrixBridge._start_request(
            bridge,
            agent,
            request,
            "!advisor:example.org",
        )
    )

    assert bridge.state.data["pending"]["advisor"] == [request]
    assert request["qitem_phase"] == "claiming"
    assert request["prompt_phase"] == "pending"
    assert request["started_at"] is None
    assert len(bridge.sent) == 1


def test_finish_failure_remains_pending_for_reconciliation():
    bridge, agent = bridge_fixture()
    request = {
        "id": "request-one",
        "event_id": "$prompt",
        "qitem_id": "qitem-one",
        "qitem_phase": "claimed",
    }

    class Gateway:
        async def queue_finish(self, qitem_id, session, resolution):
            raise GatewayError("finish unavailable")

    bridge.gateway = Gateway()

    assert asyncio.run(bridge._finish_inbound_qitem(agent, request)) is False
    assert request["qitem_phase"] == "finishing"


def test_fail_failure_remains_pending_for_reconciliation():
    bridge, agent = bridge_fixture()
    request = {
        "id": "request-one",
        "event_id": "$prompt",
        "qitem_id": "qitem-one",
        "qitem_phase": "claimed",
    }

    class Gateway:
        async def queue_fail(self, qitem_id, session, resolution):
            raise GatewayError("fail unavailable")

    bridge.gateway = Gateway()

    assert asyncio.run(
        bridge._fail_inbound_qitem(agent, request, "failed")
    ) is False
    assert request["qitem_phase"] == "failing"
