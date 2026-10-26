"""Issue #91: a qitem the seat closed itself is adopted, never reported."""

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_qitem_lifecycle import bridge_fixture
from test_steering import (
    SteeringHarness,
    append_records,
    transcript_agent_message,
    transcript_user_message,
)

from openrig_matrix_bridge.bridge import MatrixBridge
from openrig_matrix_bridge.openrig import GatewayError

ROOM = "!advisor:example.org"
LIFECYCLE_WORDS = ("Queue-Item", "qitem-", "terminal", "beansprucht")


def queued(request_id, text, qitem_id, phase="pending"):
    return {
        "id": request_id,
        "event_id": f"${request_id}",
        "reply_to_event_id": f"${request_id}",
        "thread_root_event_id": f"${request_id}",
        "source_event_ids": [f"${request_id}"],
        "qitem_id": qitem_id,
        "qitem_ids": [qitem_id],
        "qitem_phase": phase,
        "qitem_phases": {qitem_id: phase},
        "prompt_phase": "pending",
        "answer_phase": None,
        "started_at": None,
        "text": text,
    }


class SeatGateway:
    """A seat that may already have closed some qitems itself."""

    def __init__(self, states, *, show_error=False):
        self.states = dict(states)
        self.show_error = show_error
        self.prompts = []
        self.claims = []
        self.failed = []

    async def queue_show(self, qitem_id):
        if self.show_error:
            raise GatewayError("queue show unavailable")
        return {"state": self.states.get(qitem_id, "pending")}

    async def queue_claim(self, qitem_id, session):
        self.claims.append(qitem_id)
        if self.states.get(qitem_id, "pending") != "pending":
            raise GatewayError("OpenRig command failed: qitem_not_claimable")
        self.states[qitem_id] = "in-progress"

    async def queue_fail(self, qitem_id, session, resolution):
        self.failed.append(qitem_id)
        self.states[qitem_id] = "failed"

    async def send_prompt(self, ref, text):
        self.prompts.append(text)


def started_bridge(requests, gateway):
    bridge, agent = bridge_fixture()
    bridge.state.data["pending"]["advisor"] = list(requests)
    transcript = SimpleNamespace(stat=lambda: SimpleNamespace(st_size=0))

    class Resolver:
        def resolve(self, session):
            return SimpleNamespace(
                session_id="session-one",
                transcript_path=transcript,
                busy=False,
            )

    async def noop(*args, **kwargs):
        return None

    bridge.resolver = Resolver()
    bridge.gateway = gateway
    bridge.typing_tasks = {}
    bridge._set_typing = noop
    bridge._update_agent_status = noop
    bridge._update_queue_notice = noop
    return bridge, agent


def visible_texts(bridge):
    return [entry[1] for entry in bridge.sent]


def test_seat_completed_qitem_before_claim_is_adopted_silently():
    first = queued("one", "first work", "qitem-one")
    second = queued("two", "second work", "qitem-two")
    gateway = SeatGateway({"qitem-one": "done"})
    bridge, agent = started_bridge([first, second], gateway)

    asyncio.run(MatrixBridge._start_request(bridge, agent, first, ROOM))

    assert gateway.prompts == ["second work"]
    assert visible_texts(bridge) == []
    assert gateway.failed == []
    assert bridge.state.data["pending"]["advisor"] == [second]
    assert first["qitem_phases"]["qitem-one"] == "done"


def test_done_found_by_the_pre_claim_read_after_restart_is_silent():
    first = queued("one", "first work", "qitem-one", phase="claiming")
    gateway = SeatGateway({"qitem-one": "done"})
    bridge, _agent = started_bridge([first], gateway)

    asyncio.run(bridge._reconcile_pending_operations())

    assert gateway.prompts == []
    assert gateway.claims == []
    assert visible_texts(bridge) == []
    assert bridge.state.data["pending"]["advisor"] == []


def test_abandoning_an_all_done_request_posts_nothing_and_starts_the_next():
    first = queued("one", "first work", "qitem-one", phase="done")
    second = queued("two", "second work", "qitem-two")
    gateway = SeatGateway({"qitem-one": "done"})
    bridge, agent = started_bridge([first, second], gateway)

    asyncio.run(bridge._abandon_terminal_qitem(agent, first, ROOM))

    assert visible_texts(bridge) == []
    assert gateway.failed == []
    assert gateway.prompts == ["second work"]
    assert bridge.state.data["pending"]["advisor"] == [second]


@pytest.mark.parametrize("state", ["canceled", "denied", "failed"])
def test_other_terminal_states_fail_closed_without_lifecycle_details(state):
    first = queued("one", "first work", "qitem-one")
    gateway = SeatGateway({"qitem-one": state})
    bridge, agent = started_bridge([first], gateway)

    asyncio.run(MatrixBridge._start_request(bridge, agent, first, ROOM))

    assert gateway.prompts == []
    assert bridge.state.data["pending"]["advisor"] == []
    texts = visible_texts(bridge)
    assert len(texts) == 1
    assert "qitem-one" not in texts[0]
    assert state not in texts[0]
    assert "Queue-Item" not in texts[0]


def test_inspection_failure_after_a_rejected_claim_keeps_the_request():
    first = queued("one", "first work", "qitem-one")
    gateway = SeatGateway({"qitem-one": "done"}, show_error=True)
    bridge, agent = started_bridge([first], gateway)

    asyncio.run(MatrixBridge._start_request(bridge, agent, first, ROOM))

    assert gateway.prompts == []
    assert bridge.state.data["pending"]["advisor"] == [first]
    assert first["qitem_phases"]["qitem-one"] == "claiming"
    assert first["started_at"] is None
    assert not any("qitem-one" in text for text in visible_texts(bridge))

    gateway.show_error = False
    asyncio.run(bridge._reconcile_pending_operations())

    assert gateway.prompts == []
    assert visible_texts(bridge) == [] or all(
        "qitem-one" not in text for text in visible_texts(bridge)
    )
    assert bridge.state.data["pending"]["advisor"] == []


def test_admitted_steering_batch_closed_by_the_seat_keeps_its_answer(
    tmp_path: Path,
):
    from openrig_matrix_bridge.openrig import GatewayError as Error

    harness = SteeringHarness(tmp_path, "steer")
    harness.enqueue("$root", "root request", None)
    harness.busy = True
    harness.enqueue("$follow", "thread follow-up", "$root")
    pending = harness.bridge.state.data["pending"]["advisor"]
    group = pending[1]
    group["injection_phase"] = "injected_unconfirmed"
    group["injection_offset"] = harness.transcript.stat().st_size
    group["injection_text"] = group["text"]
    group["injection_attempted_at"] = time.time()
    for qitem_id in group["qitem_ids"]:
        harness.qitem_states[qitem_id] = "done"

    original_claim = harness.queue_claim

    async def claim(qitem_id, session):
        if harness.qitem_states.get(qitem_id) == "done":
            raise Error("OpenRig command failed: qitem_not_claimable")
        await original_claim(qitem_id, session)

    harness.queue_claim = claim
    append_records(
        harness.transcript,
        [
            transcript_user_message("turn-follow", group["text"]),
            transcript_agent_message(
                "turn-follow", "final-follow", "final_answer", "Follow-up answer."
            ),
        ],
    )
    pending.pop(0)

    asyncio.run(harness.bridge._poll_agent(harness.agent))
    asyncio.run(harness.bridge._poll_agent(harness.agent))

    texts = [text for text, _reply_to in harness.sent_answers]
    assert texts == ["Follow-up answer."]
    assert not any(word in text for text in texts for word in LIFECYCLE_WORDS)
    assert [prompt[1] for prompt in harness.prompts] == ["root request"]
    assert harness.bridge.state.data["pending"]["advisor"] == []
