import asyncio
import signal
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_acceptance_reaction import ROOM_ID, AckHarness, ReactionClient
from test_steering import (
    append_records,
    transcript_agent_message,
    transcript_user_message,
)

from openrig_matrix_bridge.bridge import AGENT_STATUS_EVENT
from openrig_matrix_bridge.openrig import GatewayError


class FeedbackClient(ReactionClient):
    """Matrix client double that records typing and agent-status state."""

    def __init__(self):
        super().__init__()
        self.typing_calls: list[bool] = []
        self.statuses: list[str] = []

    async def room_typing(self, room_id, typing_state=True, timeout=30000):
        self.typing_calls.append(typing_state)
        return SimpleNamespace()

    async def room_put_state(self, room_id, event_type, content, state_key=""):
        if event_type == AGENT_STATUS_EVENT:
            self.statuses.append(content["state"])
        return SimpleNamespace()


class FeedbackHarness(AckHarness):
    """Drive the real typing and status code in one persistent event loop.

    Typing loops are real asyncio tasks, so a leaked or duplicated loop
    stays observable across polls instead of vanishing with asyncio.run.
    """

    def __init__(self, tmp_path: Path, mode: str = "auto"):
        super().__init__(tmp_path, mode)
        self.client = FeedbackClient()
        self.bridge.client = self.client
        # Status and typing are the acknowledgement; prove them with the
        # optional 👍 receipt off.
        self.bridge.config.acceptance_reaction_enabled = False
        del self.bridge._set_typing
        del self.bridge._update_agent_status
        self.bridge.typing_tasks = {}
        self.bridge.agent_statuses = {}
        self.bridge.config.status_refresh_seconds = 30
        self.loop = asyncio.new_event_loop()

    def resolve_many(self, sessions):
        return {session: self.resolve(session) for session in sessions}

    def run(self, coroutine):
        result = self.loop.run_until_complete(coroutine)
        self.loop.run_until_complete(asyncio.sleep(0))
        return result

    def enqueue(self, event_id: str, text: str, root: str | None):
        relates_to = (
            {
                "rel_type": "m.thread",
                "event_id": root,
                "m.in_reply_to": {"event_id": root},
            }
            if root
            else None
        )
        self.run(
            self.bridge._enqueue_prompt(
                self.agent,
                SimpleNamespace(room_id=ROOM_ID),
                event_id,
                1_700_000_000_000,
                text,
                relates_to=relates_to,
            )
        )

    def poll(self):
        self.run(self.bridge._poll_answers_once())

    def answer(self, turn_id: str, text: str):
        append_records(
            self.transcript,
            [transcript_agent_message(turn_id, f"final-{turn_id}", "final_answer", text)],
        )

    def typing_tasks(self) -> list[asyncio.Task]:
        return [task for task in self.bridge.typing_tasks.values() if not task.done()]

    def typing_active(self) -> bool:
        return bool(self.typing_tasks())

    def status(self) -> str | None:
        return self.client.statuses[-1] if self.client.statuses else None

    def close(self):
        for task in list(self.bridge.typing_tasks.values()):
            task.cancel()
        self.loop.run_until_complete(asyncio.sleep(0))
        self.loop.close()


@pytest.fixture
def harness(tmp_path: Path):
    harness = FeedbackHarness(tmp_path)
    yield harness
    harness.close()


def complete_root_thread(harness: FeedbackHarness) -> None:
    harness.enqueue("$root", "root request", None)
    harness.answer("turn-1", "Root answer.")
    harness.poll()
    harness.client.statuses.clear()
    harness.client.typing_calls.clear()


def test_top_level_prompt_shows_working_then_ready(harness: FeedbackHarness):
    harness.enqueue("$root", "root request", None)

    assert harness.typing_active()
    assert harness.client.typing_calls == [True]
    assert harness.status() == "busy"

    harness.answer("turn-1", "Root answer.")
    harness.poll()

    assert not harness.typing_active()
    assert harness.client.typing_calls[-1] is False
    assert harness.client.statuses == ["busy", "ready"]


def test_reply_inside_existing_thread_shows_working_and_typing(
    harness: FeedbackHarness,
):
    complete_root_thread(harness)

    harness.enqueue("$reply", "thread reply", "$root")

    assert harness.typing_active()
    assert harness.client.typing_calls == [True]
    assert harness.status() == "busy"
    assert harness.client.reactions() == []


def test_steered_thread_follow_up_refreshes_typing_and_keeps_working(
    harness: FeedbackHarness,
):
    harness.enqueue("$root", "root request", None)
    harness.busy = True
    harness.enqueue("$follow", "thread follow-up", "$root")
    group = harness.bridge.state.data["pending"]["advisor"][1]
    group["injection_ready_at"] = 0
    before = len(harness.client.typing_calls)

    harness.poll()

    assert group["injection_phase"] == "admitted"
    assert harness.client.typing_calls[before:] == [True]
    assert harness.typing_active()
    assert len(harness.typing_tasks()) == 1
    assert "ready" not in harness.client.statuses
    assert harness.status() == "busy"


def test_queued_thread_request_does_not_flash_ready_before_queue_drains(
    harness: FeedbackHarness,
):
    harness.enqueue("$root", "root request", None)
    harness.busy = True
    harness.enqueue("$other", "other thread request", "$other-root")
    pending = harness.bridge.state.data["pending"]["advisor"]
    assert [request["event_id"] for request in pending] == ["$root", "$other"]
    assert pending[1]["started_at"] is None

    harness.answer("turn-1", "Root answer.")
    harness.busy = False
    harness.poll()

    assert [prompt[1] for prompt in harness.prompts] == [
        "root request",
        "other thread request",
    ]
    assert harness.typing_active()
    assert harness.client.typing_calls == [True, True]
    assert harness.client.statuses == ["busy"]

    harness.answer("turn-2", "Other answer.")
    harness.poll()

    assert not harness.typing_active()
    assert harness.client.statuses == ["busy", "ready"]


def test_late_admitted_thread_follow_up_keeps_working_feedback(
    harness: FeedbackHarness,
):
    harness.enqueue("$root", "root request", None)
    harness.busy = True
    harness.enqueue("$follow", "thread follow-up", "$root")
    pending = harness.bridge.state.data["pending"]["advisor"]
    group = pending[1]
    group["injection_ready_at"] = 0
    harness.record_steering_admission = False
    harness.poll()
    assert group["injection_phase"] == "injected_unconfirmed"

    harness.answer("turn-1", "Root answer.")
    harness.poll()
    harness.poll()

    assert pending == [group]
    assert group["injection_phase"] == "injected_unconfirmed"
    assert harness.typing_active()
    assert len(harness.typing_tasks()) == 1
    assert "ready" not in harness.client.statuses

    append_records(
        harness.transcript,
        [transcript_user_message("turn-follow", group["text"])],
    )
    harness.answer("turn-follow", "Follow-up answer.")
    harness.poll()
    harness.poll()

    assert harness.sent_answers[-1] == ("Follow-up answer.", "$root")
    assert pending == []
    assert not harness.typing_active()
    assert harness.status() == "ready"


def test_prompt_failure_stops_typing_and_reports_error(harness: FeedbackHarness):
    async def failing_send_prompt(ref, text):
        raise GatewayError("pane unavailable")

    harness.bridge.gateway = SimpleNamespace(
        send_prompt=failing_send_prompt,
        queue_claim=harness.queue_claim,
        queue_fail=harness.queue_fail,
        queue_finish=harness.queue_finish,
        queue_show=harness.queue_show,
    )

    harness.enqueue("$root", "root request", None)

    assert not harness.typing_active()
    assert harness.status() == "error"


def test_timeout_stops_typing_and_reports_error(harness: FeedbackHarness):
    harness.enqueue("$root", "root request", None)
    request = harness.bridge.state.data["pending"]["advisor"][0]
    assert harness.typing_active()

    request["started_at"] = 1.0
    harness.poll()

    assert harness.bridge.state.data["pending"]["advisor"] == []
    assert not harness.typing_active()
    assert harness.status() == "error"


def test_restart_with_pending_thread_reply_restores_one_typing_loop(
    harness: FeedbackHarness,
):
    complete_root_thread(harness)
    harness.enqueue("$reply", "thread reply", "$root")
    for task in list(harness.bridge.typing_tasks.values()):
        task.cancel()
    harness.loop.run_until_complete(asyncio.sleep(0))
    harness.bridge.typing_tasks = {}
    harness.bridge.agent_statuses = {}
    harness.client.typing_calls.clear()
    harness.client.statuses.clear()

    harness.run(harness.bridge._restore_activity_feedback())
    first_tasks = harness.typing_tasks()
    for _poll in range(3):
        harness.poll()

    assert harness.status() == "busy"
    assert len(first_tasks) == 1
    assert harness.typing_tasks() == first_tasks
    assert harness.client.typing_calls == [True]

    harness.answer("turn-2", "Reply answer.")
    harness.poll()

    assert harness.sent_answers[-1] == ("Reply answer.", "$root")
    assert not harness.typing_active()
    assert harness.status() == "ready"
    assert harness.client.reactions() == []


def test_silent_reaction_feedback_never_types(harness: FeedbackHarness):
    harness.run(
        harness.bridge._enqueue_prompt(
            harness.agent,
            SimpleNamespace(room_id=ROOM_ID),
            "$feedback",
            1_700_000_000_000,
            "reaction feedback",
            suppress_reply=True,
            queue_tracked=False,
        )
    )
    for _poll in range(2):
        harness.poll()

    assert not harness.typing_active()
    assert harness.client.typing_calls == []


def test_sigterm_clears_typing_and_stops_cleanly(
    harness: FeedbackHarness,
    monkeypatch: pytest.MonkeyPatch,
):
    bridge = harness.bridge

    async def noop(*args, **kwargs):
        return None

    async def idle(*args, **kwargs):
        await asyncio.sleep(3600)

    for name in (
        "_initial_sync",
        "_recover_verifications",
        "_recover_voice_statuses",
        "_reconcile_room_memberships",
        "_create_rooms",
        "_restore_room_names",
        "_ensure_room_avatars",
        "_reconcile_pending_operations",
    ):
        setattr(bridge, name, noop)
    for name in (
        "_load_room_index",
        "_initialize_transcript_offsets",
        "_repair_decision_reply_targets",
    ):
        setattr(bridge, name, lambda: None)
    for name in (
        "_poll_answers",
        "_poll_agent_statuses",
        "_poll_decisions",
        "_poll_media_outbox",
        "_poll_message_outbox",
        "_poll_human_queue",
        "_poll_verification_timeouts",
    ):
        setattr(bridge, name, idle)
    closed: list[bool] = []

    async def close():
        closed.append(True)

    harness.client.sync_forever = idle
    harness.client.close = close
    handlers: dict[int, object] = {}
    monkeypatch.setattr(
        harness.loop,
        "add_signal_handler",
        lambda sig, callback, *args: handlers.__setitem__(sig, callback),
    )
    monkeypatch.setattr(
        harness.loop,
        "remove_signal_handler",
        lambda sig: handlers.pop(sig, None) is not None,
    )
    harness.enqueue("$root", "root request", None)
    assert harness.typing_active()

    async def stop_with_sigterm():
        running = asyncio.ensure_future(bridge.run())
        # Startup includes thread hops (status refresh), so bound by time,
        # not by event-loop yields.
        deadline = harness.loop.time() + 5.0
        while signal.SIGTERM not in handlers and harness.loop.time() < deadline:
            await asyncio.sleep(0.01)
        if signal.SIGTERM not in handlers:
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
            raise AssertionError("bridge did not install a SIGTERM handler")
        handlers[signal.SIGTERM]()
        await running

    harness.run(stop_with_sigterm())

    assert not harness.typing_active()
    assert harness.client.typing_calls[-1] is False
    assert closed == [True]
    assert signal.SIGTERM not in handlers
