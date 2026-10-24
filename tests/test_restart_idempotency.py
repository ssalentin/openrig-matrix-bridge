import asyncio
import time
from types import SimpleNamespace

import pytest
from nio import RoomSendResponse

from openrig_matrix_bridge.bridge import MatrixBridge
from openrig_matrix_bridge.config import AgentConfig


class SimulatedCrash(BaseException):
    pass


def operation_bridge(tmp_path, request):
    agent = AgentConfig(
        key="advisor",
        display_name="Advisor",
        session="advisor-lead@kernel",
    )
    bridge = object.__new__(MatrixBridge)
    bridge.config = SimpleNamespace(
        agents={"advisor": agent},
        status_refresh_seconds=30,
        mirror_commentary=False,
        response_timeout_minutes=360,
    )
    bridge.room_agents = {"!advisor:example.org": "advisor"}
    bridge.locks = {"advisor": asyncio.Lock()}
    bridge.state = SimpleNamespace(
        save=lambda: None,
        data={
            "rooms": {
                "advisor": {
                    "room_id": "!advisor:example.org",
                }
            },
            "pending": {"advisor": [request]},
            "processed_events": [],
        },
    )
    bridge.sent = []

    async def send(room_id, text, reply_to=None, **kwargs):
        bridge.sent.append((room_id, text, reply_to, kwargs))
        return RoomSendResponse("$notice", room_id)

    async def noop(*args, **kwargs):
        return None

    bridge._send = send
    bridge._set_typing = noop
    bridge._update_agent_status = noop
    bridge._update_queue_notice = noop
    bridge._start_request = noop
    return bridge, agent


def request(**values):
    base = {
        "id": "request-one",
        "event_id": "$prompt",
        "reply_to_event_id": "$prompt",
        "thread_root_event_id": "$prompt",
        "qitem_id": "qitem-one",
        "queue_tracked": True,
        "suppress_reply": False,
        "text": "do the work",
        "answer_text": None,
        "prompt_phase": "sent",
        "qitem_phase": "claimed",
    }
    base.update(values)
    return base


@pytest.mark.parametrize("real_queue_shape", [False, True])
def test_claim_reconciliation_accepts_already_claimed_qitem(
    tmp_path,
    real_queue_shape,
):
    item = request(qitem_phase="claiming")
    bridge, agent = operation_bridge(tmp_path, item)
    calls = []

    class Gateway:
        async def queue_show(self, qitem_id):
            if real_queue_shape:
                return {
                    "state": "in-progress",
                    "waiting": {"owner": agent.session},
                }
            return {"state": "in-progress", "claimed_by": agent.session}

        async def queue_claim(self, qitem_id, session):
            calls.append(("claim", qitem_id, session))

    bridge.gateway = Gateway()

    assert asyncio.run(bridge._claim_inbound_qitem(agent, item)) is True
    assert item["qitem_phase"] == "claimed"
    assert calls == []


def test_recovered_claim_resumes_prompt_that_never_started_sending(tmp_path):
    item = request(
        prompt_phase="pending",
        qitem_phase="claiming",
        started_at=time.time(),
    )
    bridge, agent = operation_bridge(tmp_path, item)
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")
    prompts = []

    class Resolver:
        def resolve(self, session):
            return SimpleNamespace(
                session_id="session-one",
                transcript_path=transcript,
                busy=False,
            )

    class Gateway:
        async def queue_show(self, qitem_id):
            return {
                "state": "in-progress",
                "waiting": {"owner": agent.session},
            }

        async def queue_claim(self, qitem_id, session):
            raise AssertionError("already-claimed qitem was claimed again")

        async def send_prompt(self, ref, text):
            assert item["prompt_phase"] == "sending"
            prompts.append(text)

    bridge.resolver = Resolver()
    bridge.gateway = Gateway()
    del bridge._start_request

    asyncio.run(bridge._reconcile_pending_operations())

    assert prompts == ["do the work"]
    assert item["qitem_phase"] == "claimed"
    assert item["prompt_phase"] == "sent"


def test_recovered_claim_waits_for_busy_agent_then_sends_once(tmp_path):
    item = request(
        prompt_phase="pending",
        qitem_phase="claiming",
        started_at=123.0,
    )
    bridge, agent = operation_bridge(tmp_path, item)
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")
    prompts = []
    busy = {"value": True}

    class Resolver:
        def resolve(self, session):
            return SimpleNamespace(
                session_id="session-one",
                transcript_path=transcript,
                busy=busy["value"],
            )

    class Gateway:
        async def queue_show(self, qitem_id):
            return {
                "state": "in-progress",
                "waiting": {"owner": agent.session},
            }

        async def queue_claim(self, qitem_id, session):
            raise AssertionError("already-claimed qitem was claimed again")

        async def send_prompt(self, ref, text):
            prompts.append(text)

    bridge.resolver = Resolver()
    bridge.gateway = Gateway()
    del bridge._start_request

    asyncio.run(bridge._reconcile_pending_operations())

    assert prompts == []
    assert item["qitem_phase"] == "claimed"
    assert item["prompt_phase"] == "pending"
    assert item["started_at"] is None

    busy["value"] = False
    asyncio.run(bridge._poll_agent(agent))

    assert prompts == ["do the work"]
    assert item["prompt_phase"] == "sent"


def test_legacy_started_prompt_without_phase_fails_closed(tmp_path):
    item = request(
        prompt_phase=None,
        qitem_phase="not_tracked",
        qitem_id=None,
        started_at=123.0,
        text="legacy prompt",
    )
    bridge, _agent = operation_bridge(tmp_path, item)
    prompts = []

    class Gateway:
        async def send_prompt(self, ref, text):
            prompts.append(text)

    bridge.gateway = Gateway()

    asyncio.run(bridge._reconcile_pending_operations())

    assert prompts == []
    assert bridge.state.data["pending"]["advisor"] == []
    assert item["terminal_error"] == (
        "Prompt delivery outcome was uncertain after bridge restart"
    )
    assert len(bridge.sent) == 1
    assert bridge.sent[0][3]["tx_id"] == "failure-request-one"


def test_finish_reconciliation_accepts_already_done_qitem(tmp_path):
    item = request(
        answer_phase="sent",
        qitem_phase="finishing",
    )
    bridge, agent = operation_bridge(tmp_path, item)
    calls = []

    class Gateway:
        async def queue_show(self, qitem_id):
            return {"state": "done"}

        async def queue_finish(self, qitem_id, session, resolution):
            calls.append(("finish", qitem_id, session, resolution))

    bridge.gateway = Gateway()

    assert asyncio.run(bridge._finish_inbound_qitem(agent, item)) is True
    assert item["qitem_phase"] == "done"
    assert calls == []


def test_failure_reconciliation_accepts_already_failed_qitem(tmp_path):
    item = request(
        prompt_phase="sending",
        qitem_phase="failing",
    )
    bridge, agent = operation_bridge(tmp_path, item)
    calls = []

    class Gateway:
        async def queue_show(self, qitem_id):
            return {"state": "failed"}

        async def queue_fail(self, qitem_id, session, resolution):
            calls.append(("fail", qitem_id, session, resolution))

    bridge.gateway = Gateway()

    assert asyncio.run(
        bridge._fail_inbound_qitem(
            agent,
            item,
            "failed before restart",
        )
    ) is True
    assert item["qitem_phase"] == "failed"
    assert calls == []


def test_qitem_fail_crash_recovers_without_repeating_remote_failure(tmp_path):
    item = request(
        prompt_phase="sent",
        answer_phase="failed",
        qitem_phase="claimed",
    )
    bridge, agent = operation_bridge(tmp_path, item)
    calls = []

    class Gateway:
        async def queue_fail(self, qitem_id, session, resolution):
            calls.append((qitem_id, session, resolution))

    bridge.gateway = Gateway()

    def crash_after_effect(point):
        if point == "qitem_fail":
            raise SimulatedCrash("process died after qitem failure")

    bridge._crash_after_effect = crash_after_effect

    try:
        asyncio.run(
            bridge._fail_inbound_qitem(
                agent,
                item,
                "terminal failure",
            )
        )
    except SimulatedCrash:
        pass

    assert item["qitem_phase"] == "failing"

    class RecoveryGateway:
        async def queue_show(self, qitem_id):
            return {"state": "failed"}

        async def queue_fail(self, qitem_id, session, resolution):
            raise AssertionError("failed qitem was repeated after restart")

    bridge.gateway = RecoveryGateway()
    bridge._crash_after_effect = lambda point: None

    assert asyncio.run(
        bridge._fail_inbound_qitem(
            agent,
            item,
            "terminal failure",
        )
    ) is True
    assert calls == [("qitem-one", agent.session, "terminal failure")]
    assert item["qitem_phase"] == "failed"


def test_ambiguous_prompt_send_is_failed_once_without_resend(tmp_path):
    item = request(
        prompt_phase="sending",
        qitem_phase="claimed",
    )
    bridge, agent = operation_bridge(tmp_path, item)
    failures = []

    class Gateway:
        async def queue_show(self, qitem_id):
            return {"state": "in-progress", "claimed_by": agent.session}

        async def queue_fail(self, qitem_id, session, resolution):
            failures.append((qitem_id, session, resolution))

    bridge.gateway = Gateway()

    asyncio.run(bridge._reconcile_pending_operations())
    asyncio.run(bridge._reconcile_pending_operations())

    assert failures == [
        (
            "qitem-one",
            agent.session,
            "Prompt delivery outcome was uncertain after bridge restart",
        )
    ]
    assert bridge.state.data["pending"]["advisor"] == []
    assert len(bridge.sent) == 1
    assert "nicht eindeutig" in bridge.sent[0][1]
    assert bridge.sent[0][3]["tx_id"] == "failure-request-one"


def test_answer_delivery_uses_stable_transaction_identity():
    bridge = object.__new__(MatrixBridge)
    bridge.sent = []

    async def send(room_id, text, reply_to=None, **kwargs):
        bridge.sent.append(kwargs["tx_id"])
        return RoomSendResponse("$answer", room_id)

    bridge._send = send
    item = request()

    response = asyncio.run(
        bridge._deliver_to_request(
            "!advisor:example.org",
            "answer",
            item,
            tx_id="answer-request-one",
        )
    )

    assert response.event_id == "$answer"
    assert bridge.sent == ["answer-request-one"]


def test_prompt_start_persists_send_phase_before_openrig_call(tmp_path):
    item = request(
        prompt_phase="pending",
        qitem_phase="pending",
    )
    bridge, agent = operation_bridge(tmp_path, item)
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")
    calls = []

    class Resolver:
        def resolve(self, session):
            return SimpleNamespace(
                session_id="session-one",
                transcript_path=transcript,
                busy=False,
            )

    class Gateway:
        async def queue_claim(self, qitem_id, session):
            calls.append(("claim", qitem_id, session))

        async def send_prompt(self, ref, text):
            assert item["prompt_phase"] == "sending"
            calls.append(("send", ref.session_id, text))

    bridge.resolver = Resolver()
    bridge.gateway = Gateway()

    asyncio.run(
        MatrixBridge._start_request(
            bridge,
            agent,
            item,
            "!advisor:example.org",
        )
    )

    assert calls == [
        ("claim", "qitem-one", agent.session),
        ("send", "session-one", "do the work"),
    ]
    assert item["qitem_phase"] == "claimed"
    assert item["prompt_phase"] == "sent"


def test_restart_completes_already_sent_answer_without_resend(tmp_path):
    item = request(
        prompt_phase="sent",
        answer_phase="sent",
        answer_text="answer",
        answer_event_id="$answer",
        qitem_phase="claimed",
    )
    bridge, agent = operation_bridge(tmp_path, item)
    finishes = []

    class Gateway:
        async def queue_finish(self, qitem_id, session, resolution):
            finishes.append((qitem_id, session, resolution))

    bridge.gateway = Gateway()

    asyncio.run(bridge._reconcile_pending_operations())
    asyncio.run(bridge._reconcile_pending_operations())

    assert finishes == [
        ("qitem-one", agent.session, "Matrix answer delivered")
    ]
    assert bridge.state.data["pending"]["advisor"] == []
    assert bridge.sent == []


def test_restart_reconciles_failed_answer_without_resend(tmp_path):
    item = request(
        prompt_phase="sent",
        answer_phase="failed",
        answer_text="answer",
        qitem_phase="failing",
        terminal_error="Matrix answer delivery failed after 3 attempts",
    )
    bridge, _agent = operation_bridge(tmp_path, item)
    failed = []

    class Gateway:
        async def queue_show(self, qitem_id):
            return {"state": "failed"}

        async def queue_fail(self, qitem_id, session, resolution):
            failed.append((qitem_id, session, resolution))

    bridge.gateway = Gateway()

    asyncio.run(bridge._reconcile_pending_operations())

    assert item["qitem_phase"] == "failed"
    assert failed == []
    assert bridge.state.data["pending"]["advisor"] == []
    assert len(bridge.sent) == 1
    assert bridge.sent[0][3]["tx_id"] == "failure-request-one"


def test_crash_after_prompt_send_fails_closed_without_second_prompt(tmp_path):
    item = request(
        prompt_phase="pending",
        qitem_phase="pending",
    )
    bridge, agent = operation_bridge(tmp_path, item)
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")
    sent_prompts = []

    class Resolver:
        def resolve(self, session):
            return SimpleNamespace(
                session_id="session-one",
                transcript_path=transcript,
                busy=False,
            )

    class CrashGateway:
        async def queue_claim(self, qitem_id, session):
            return None

        async def send_prompt(self, ref, text):
            sent_prompts.append(text)
            raise SimulatedCrash("process died after prompt send")

    bridge.resolver = Resolver()
    bridge.gateway = CrashGateway()

    try:
        asyncio.run(
            MatrixBridge._start_request(
                bridge,
                agent,
                item,
                "!advisor:example.org",
            )
        )
    except SimulatedCrash:
        pass

    assert item["prompt_phase"] == "sending"

    class RecoveryGateway:
        async def queue_fail(self, qitem_id, session, resolution):
            return None

    bridge.gateway = RecoveryGateway()
    bridge._send = _send_success(bridge)

    asyncio.run(bridge._reconcile_pending_operations())

    assert sent_prompts == ["do the work"]
    assert bridge.state.data["pending"]["advisor"] == []
    assert len(bridge.sent) == 1
    assert bridge.sent[0][3]["tx_id"] == "failure-request-one"


def test_crash_after_answer_send_retries_same_transaction(tmp_path):
    item = request(
        answer_text="answer",
        answer_phase="sending",
        qitem_phase="claimed",
        started_at=time.time(),
    )
    bridge, agent = operation_bridge(tmp_path, item)
    attempts = []

    async def crash_after_remote_send(room_id, text, reply_to=None, **kwargs):
        attempts.append(kwargs["tx_id"])
        raise SimulatedCrash("process died after answer send")

    bridge._send = crash_after_remote_send

    try:
        asyncio.run(bridge._poll_agent(agent))
    except SimulatedCrash:
        pass

    assert item["answer_phase"] == "sending"

    finishes = []

    class Gateway:
        async def queue_finish(self, qitem_id, session, resolution):
            finishes.append((qitem_id, session, resolution))

    async def retry_send(room_id, text, reply_to=None, **kwargs):
        attempts.append(kwargs["tx_id"])
        return RoomSendResponse("$answer-once", room_id)

    bridge.gateway = Gateway()
    bridge._send = retry_send

    asyncio.run(bridge._poll_agent(agent))

    assert attempts == ["answer-request-one", "answer-request-one"]
    assert finishes == [
        ("qitem-one", agent.session, "Matrix answer delivered")
    ]
    assert bridge.state.data["pending"]["advisor"] == []


def _send_success(bridge):
    async def send(room_id, text, reply_to=None, **kwargs):
        bridge.sent.append((room_id, text, reply_to, kwargs))
        return RoomSendResponse("$answer", room_id)

    return send
