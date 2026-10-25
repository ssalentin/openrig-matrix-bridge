"""Pending requests written by bridge 0.10 are reconciled at startup.

0.10 stored a pending request with a ``qitem_id`` but none of the phase fields
(``prompt_phase``, ``answer_phase``, ``qitem_phase``, ``qitem_phases``) that 0.11
reconciles on. The qitem may have been closed since. Startup must adopt the
remote state before it claims, prompts, notifies or delivers anything.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from nio import RoomSendResponse

from openrig_matrix_bridge.bridge import MatrixBridge
from openrig_matrix_bridge.config import AgentConfig
from openrig_matrix_bridge.openrig import GatewayError

TERMINAL = {"done", "failed", "canceled", "denied"}


class RigDouble:
    """Gateway double with the real CLI's rules for rows that are already closed."""

    def __init__(self, states: dict[str, str]):
        self.states = dict(states)
        self.calls: list[tuple[str, ...]] = []
        self.unreachable = False

    async def queue_show(self, qitem_id):
        self.calls.append(("show", qitem_id))
        if self.unreachable or qitem_id not in self.states:
            raise GatewayError("OpenRig command failed: unreachable")
        return {"state": self.states[qitem_id]}

    async def queue_claim(self, qitem_id, session):
        self.calls.append(("claim", qitem_id))
        if self.states.get(qitem_id) in TERMINAL:
            raise GatewayError("OpenRig command failed: qitem_not_claimable")
        self.states[qitem_id] = "in-progress"

    async def queue_finish(self, qitem_id, session, resolution):
        self.calls.append(("finish", qitem_id))
        if self.states.get(qitem_id) in TERMINAL:
            raise GatewayError("note_append_fields_not_admitted")
        self.states[qitem_id] = "done"

    async def queue_fail(self, qitem_id, session, resolution):
        self.calls.append(("fail", qitem_id))
        if self.states.get(qitem_id) in TERMINAL:
            raise GatewayError("terminal_reopen_requires_ack")
        self.states[qitem_id] = "failed"

    async def send_prompt(self, ref, text):
        self.calls.append(("prompt", ref.session_id))

    def kinds(self) -> list[str]:
        return [call[0] for call in self.calls]


def legacy_request(request_id="req-one", qitem_id="qitem-one", **values):
    """The exact shape bridge 0.10.0 saved (no phase fields, no qitem_ids)."""
    request = {
        "id": request_id,
        "event_id": f"${request_id}",
        "reply_to_event_id": f"${request_id}",
        "thread_root_event_id": f"${request_id}",
        "source_event_ids": [f"${request_id}"],
        "source_relation": None,
        "source_label": "message",
        "qitem_id": qitem_id,
        "queue_tracked": True,
        "suppress_reply": False,
        "priority": "urgent",
        "text": "legacy prompt text",
        "enqueued_at": 1.0,
        "started_at": None,
        "session_id": None,
        "transcript_path": None,
        "offset": None,
        "answer_text": None,
    }
    request.update(values)
    return request


def started(**values):
    return legacy_request(
        started_at=2.0,
        session_id="session-one",
        transcript_path="/nonexistent/transcript.jsonl",
        offset=0,
        **values,
    )


def make_bridge(tmp_path, requests, rig):
    agent = AgentConfig(
        key="advisor",
        display_name="Advisor",
        session="advisor-lead@kernel",
    )
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")
    bridge = object.__new__(MatrixBridge)
    bridge.config = SimpleNamespace(
        agents={"advisor": agent},
        status_refresh_seconds=30,
        mirror_commentary=False,
        response_timeout_minutes=360,
        steering_mode="queue",
    )
    bridge.room_agents = {"!advisor:example.org": "advisor"}
    bridge.locks = {"advisor": asyncio.Lock()}
    bridge.saves = 0

    def save():
        bridge.saves += 1

    bridge.state = SimpleNamespace(
        save=save,
        data={
            "rooms": {"advisor": {"room_id": "!advisor:example.org"}},
            "pending": {"advisor": requests},
            "processed_events": [],
        },
    )
    bridge.gateway = rig
    bridge.resolver = SimpleNamespace(
        resolve=lambda session: SimpleNamespace(
            session_id="session-one",
            transcript_path=transcript,
            busy=False,
            activity_state="idle",
            needs_input_count=0,
        )
    )
    bridge.sent = []

    async def send(room_id, text, reply_to=None, **kwargs):
        bridge.sent.append(text)
        return RoomSendResponse(f"$notice-{len(bridge.sent)}", room_id)

    async def noop(*args, **kwargs):
        return None

    bridge._send = send
    bridge._set_typing = noop
    bridge._sync_typing = noop
    bridge._update_agent_status = noop
    bridge._update_queue_notice = noop
    return bridge, agent


def pending(bridge):
    return bridge.state.data["pending"]["advisor"]


def reconcile(bridge):
    asyncio.run(bridge._reconcile_pending_operations())


@pytest.mark.parametrize("answer", [None, "answer captured by 0.10"])
@pytest.mark.parametrize("is_started", [False, True])
def test_done_qitem_removes_the_stale_request_without_replay(
    tmp_path, is_started, answer
):
    build = started if is_started else legacy_request
    rig = RigDouble({"qitem-one": "done"})
    bridge, _agent = make_bridge(tmp_path, [build(answer_text=answer)], rig)

    reconcile(bridge)

    assert pending(bridge) == []
    assert rig.kinds() == ["show"]
    assert bridge.sent == []


@pytest.mark.parametrize("state", ["failed", "canceled", "denied"])
@pytest.mark.parametrize("is_started", [False, True])
def test_other_terminal_qitem_removes_the_request_with_one_notice(
    tmp_path, is_started, state
):
    build = started if is_started else legacy_request
    rig = RigDouble({"qitem-one": state})
    bridge, _agent = make_bridge(tmp_path, [build()], rig)

    reconcile(bridge)

    assert pending(bridge) == []
    assert set(rig.kinds()) == {"show"}
    assert len(bridge.sent) == 1
    assert state in bridge.sent[0]

    reconcile(bridge)

    assert len(bridge.sent) == 1


def test_a_terminal_qitem_is_never_claimed_or_repeatedly_retried(tmp_path):
    rig = RigDouble({"qitem-one": "done"})
    bridge, _agent = make_bridge(tmp_path, [legacy_request()], rig)

    for _ in range(3):
        reconcile(bridge)

    assert "claim" not in rig.kinds()
    assert "prompt" not in rig.kinds()
    assert rig.kinds().count("show") == 1


def test_every_pending_request_is_reconciled_not_only_the_head(tmp_path):
    rig = RigDouble(
        {"qitem-a": "pending", "qitem-b": "done", "qitem-c": "canceled"}
    )
    requests = [
        legacy_request(request_id="req-a", qitem_id="qitem-a"),
        legacy_request(request_id="req-b", qitem_id="qitem-b"),
        legacy_request(request_id="req-c", qitem_id="qitem-c"),
    ]
    bridge, _agent = make_bridge(tmp_path, requests, rig)

    reconcile(bridge)

    assert [item["id"] for item in pending(bridge)] == ["req-a"]
    assert ("claim", "qitem-a") in rig.calls
    assert ("claim", "qitem-b") not in rig.calls
    assert ("claim", "qitem-c") not in rig.calls
    assert rig.kinds().count("prompt") == 1
    assert len(bridge.sent) == 1
    assert "canceled" in bridge.sent[0]


def test_reconciled_phase_is_saved_before_the_request_is_removed(tmp_path):
    rig = RigDouble({"qitem-one": "done"})
    request = legacy_request()
    bridge, _agent = make_bridge(tmp_path, [request], rig)
    seen = []
    bridge.state.save = lambda: seen.append(
        (request.get("qitem_phases"), len(pending(bridge)))
    )

    reconcile(bridge)

    assert seen[0] == ({"qitem-one": "done"}, 1)
    assert pending(bridge) == []


def test_restart_after_the_phase_was_saved_finishes_the_removal(tmp_path):
    rig = RigDouble({"qitem-one": "failed"})
    request = legacy_request(
        qitem_phases={"qitem-one": "failed"},
        qitem_phase="failed",
        failure_notice_event_id="$already-sent",
    )
    bridge, _agent = make_bridge(tmp_path, [request], rig)

    reconcile(bridge)

    assert pending(bridge) == []
    assert bridge.sent == []
    assert "claim" not in rig.kinds()


def test_non_terminal_started_request_stays_fail_closed_and_is_not_resent(tmp_path):
    rig = RigDouble({"qitem-one": "in-progress"})
    bridge, _agent = make_bridge(tmp_path, [started()], rig)

    reconcile(bridge)

    assert "prompt" not in rig.kinds()
    assert "claim" not in rig.kinds()
    assert rig.states["qitem-one"] == "failed"
    assert len(bridge.sent) == 1
    assert pending(bridge) == []


def test_non_terminal_unstarted_request_is_claimed_then_prompted_once(tmp_path):
    rig = RigDouble({"qitem-one": "pending"})
    request = legacy_request()
    bridge, _agent = make_bridge(tmp_path, [request], rig)

    reconcile(bridge)

    assert rig.kinds().count("claim") == 1
    assert rig.kinds().count("prompt") == 1
    assert pending(bridge) == [request]


def test_unreachable_rig_neither_claims_nor_prompts_and_keeps_the_request(tmp_path):
    rig = RigDouble({"qitem-one": "done"})
    rig.unreachable = True
    request = legacy_request()
    bridge, _agent = make_bridge(tmp_path, [request], rig)

    reconcile(bridge)
    reconcile(bridge)

    assert pending(bridge) == [request]
    assert "claim" not in rig.kinds()
    assert "prompt" not in rig.kinds()
    assert "qitem_phases" not in request
    assert request.get("started_at") is None
    assert request.get("prompt_phase") is None
    assert request["answer_text"] is None
    # The existing once-only "could not claim, will retry" notice may be sent;
    # it is idempotent and not repeated by the second pass.
    assert len(bridge.sent) <= 1
