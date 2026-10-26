import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from openrig_matrix_bridge.bridge import MatrixBridge, queue_notice_text
from openrig_matrix_bridge.config import AgentConfig
from openrig_matrix_bridge.openrig import GatewayError


def transcript_user_message(turn_id: str, text: str) -> dict:
    return {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": text}],
            "internal_chat_message_metadata_passthrough": {
                "turn_id": turn_id,
            },
        },
    }


def transcript_agent_message(
    turn_id: str,
    item_id: str,
    phase: str,
    text: str,
) -> dict:
    return {
        "type": "event_msg",
        "payload": {
            "type": "item_completed",
            "turn_id": turn_id,
            "item": {
                "type": "AgentMessage",
                "id": item_id,
                "phase": phase,
                "content": [{"type": "Text", "text": text}],
            },
        },
    }


def append_records(path: Path, records: list[dict]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


class SteeringHarness:
    def __init__(self, tmp_path: Path, mode: str):
        self.tmp_path = tmp_path
        self.transcript = tmp_path / "transcript.jsonl"
        self.transcript.write_text("", encoding="utf-8")
        self.agent = AgentConfig(
            key="advisor",
            display_name="Advisor",
            session="advisor-lead@kernel",
        )
        self.busy = False
        self.prompts: list[tuple[str, str]] = []
        self.steers: list[str] = []
        self.qitems = 0
        self.queue_updates = []
        self.finished_qitems: list[str] = []
        self.qitem_states: dict[str, str] = {}
        self.sent_answers: list[tuple[str, str]] = []
        self.sent_source_ids: list[list[str] | None] = []
        self.steer_error: Exception | None = None
        self.steer_turn_id = "turn-1"
        self.record_prompt_admission = True
        self.record_steering_admission = True
        self.bridge = object.__new__(MatrixBridge)
        self.bridge.config = SimpleNamespace(
            agents={"advisor": self.agent},
            mirror_commentary=False,
            response_timeout_minutes=30,
            steering_mode=mode,
            steering_max_chars=120,
            steering_batch_window_seconds=0.01,
        )
        self.bridge.locks = {"advisor": asyncio.Lock()}
        self.bridge.processed_events = set()
        self.bridge.state = SimpleNamespace(
            save=lambda: None,
            data={
                "pending": {},
                "processed_events": [],
                "rooms": {
                    "advisor": {
                        "room_id": "!advisor:matrix.example.org",
                    }
                },
            },
        )
        self.bridge.resolver = self
        self.bridge.gateway = self
        self.bridge.room_agents = {
            "!advisor:matrix.example.org": "advisor",
        }

        async def create_qitem(*args, **kwargs):
            self.qitems += 1
            return f"$qitem-{self.qitems}"

        async def update_queue_notice(*args, **kwargs):
            self.queue_updates.append((args, kwargs))

        async def set_typing(*args, **kwargs):
            return None

        async def update_status(*args, **kwargs):
            return None

        self.bridge._create_inbound_qitem = create_qitem
        self.bridge._update_queue_notice = update_queue_notice
        self.bridge._set_typing = set_typing
        self.bridge._update_agent_status = update_status
        self.bridge._send = self.send

    def resolve(self, session: str):
        return SimpleNamespace(
            session_id="session-one",
            transcript_path=self.transcript,
            tmux_session=session,
            busy=self.busy,
        )

    async def queue_claim(self, qitem_id: str, session: str):
        self.qitem_states[qitem_id] = "in-progress"

    async def queue_finish(self, qitem_id: str, session: str, resolution: str):
        self.finished_qitems.append(qitem_id)
        self.qitem_states[qitem_id] = "done"

    async def queue_fail(self, qitem_id: str, session: str, resolution: str):
        self.qitem_states[qitem_id] = "failed"

    async def queue_show(self, qitem_id: str):
        return {"state": self.qitem_states.get(qitem_id, "pending")}

    async def send_prompt(self, ref, text: str):
        self.prompts.append((ref.session_id, text))
        turn_id = f"turn-{len(self.prompts)}"
        if self.record_prompt_admission:
            append_records(
                self.transcript,
                [transcript_user_message(turn_id, text)],
            )

    async def steer_prompt(self, ref, text: str):
        self.steers.append(text)
        if self.steer_error is not None:
            raise self.steer_error
        if self.record_steering_admission:
            append_records(
                self.transcript,
                [transcript_user_message(self.steer_turn_id, text)],
            )

    async def send(self, room_id, text, reply_to=None, **kwargs):
        from nio import RoomSendResponse

        self.sent_answers.append((text, reply_to))
        self.sent_source_ids.append(kwargs.get("source_event_ids"))
        return RoomSendResponse("$answer", room_id)

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
        asyncio.run(
            self.bridge._enqueue_prompt(
                self.agent,
                SimpleNamespace(room_id="!advisor:matrix.example.org"),
                event_id,
                1_700_000_000_000,
                text,
                relates_to=relates_to,
            )
        )


@pytest.mark.parametrize("mode", ["steer", "auto"])
def test_same_thread_followups_form_one_ordered_fallback_batch(
    tmp_path: Path,
    mode: str,
):
    harness = SteeringHarness(tmp_path, mode)
    harness.enqueue("$root", "root request", None)
    harness.busy = True
    harness.enqueue("$follow-one", "first follow-up", "$root")
    harness.enqueue("$follow-two", "second follow-up", "$root")

    pending = harness.bridge.state.data["pending"]["advisor"]

    assert len(pending) == 2
    assert pending[0]["source_event_ids"] == ["$root"]
    assert pending[1]["deferred_steering"] is True
    assert pending[1]["source_event_ids"] == ["$follow-one", "$follow-two"]
    assert pending[1]["qitem_ids"] == ["$qitem-2", "$qitem-3"]
    assert pending[1]["thread_root_event_id"] == "$root"
    assert "$follow-one" in pending[1]["text"]
    assert "$follow-two" in pending[1]["text"]
    assert pending[0]["steering_group_id"] == pending[1]["id"]


def test_queue_card_uses_real_steering_batch_without_waiting_container(
    tmp_path: Path,
):
    harness = SteeringHarness(tmp_path, "auto")
    harness.enqueue("$root", "root request", None)
    harness.busy = True
    harness.enqueue("$follow-one", "first follow-up", "$root")
    harness.enqueue("$follow-two", "second follow-up", "$root")

    card = queue_notice_text(harness.bridge.state.data["pending"]["advisor"])

    # The steered follow-ups belong to the active turn, so nothing waits and
    # there is no queue card (#86).
    assert card is None


def test_queue_card_reveals_fallback_steering_batch_as_waiting(
    tmp_path: Path,
):
    harness = SteeringHarness(tmp_path, "auto")
    harness.enqueue("$root", "root request", None)
    harness.busy = True
    harness.enqueue("$follow-one", "first follow-up", "$root")
    harness.enqueue("$follow-two", "second follow-up", "$root")
    pending = harness.bridge.state.data["pending"]["advisor"]
    pending[1]["injection_phase"] = "fallback"

    card = queue_notice_text(pending)

    assert card is not None
    assert "1 Nachricht wartet" in card
    assert "**Aktiv:** Text" in card
    assert "Aktiv (3 Eingaben)" not in card
    assert "1. Text · Thread" in card
    assert "first follow-up" in card
    assert "second follow-up" in card


def test_refused_steering_refreshes_queue_card_and_keeps_batch_active(tmp_path: Path):
    harness = SteeringHarness(tmp_path, "auto")
    harness.enqueue("$root", "root request", None)
    harness.busy = True
    harness.enqueue("$follow-one", "first follow-up", "$root")
    harness.enqueue("$follow-two", "second follow-up", "$root")
    group = harness.bridge.state.data["pending"]["advisor"][1]
    group["injection_ready_at"] = 0
    harness.steer_error = GatewayError("pane refused")
    harness.queue_updates.clear()

    asyncio.run(harness.bridge._poll_agent(harness.agent))

    # A refused pane injection waits for the admission grace before any fallback,
    # so the follow-ups stay part of the active batch and are not shown as a
    # separate waiting turn. The phase change refreshes the card exactly once.
    assert group["injection_phase"] == "injecting"
    assert group["last_injection_error"] == "pane refused"
    assert len(harness.queue_updates) == 1
    assert queue_notice_text(harness.bridge.state.data["pending"]["advisor"]) is None


def test_queue_card_marks_busy_unstarted_request_as_next(tmp_path: Path):
    harness = SteeringHarness(tmp_path, "auto")
    harness.busy = True
    harness.enqueue("$waiting", "waiting for busy seat", None)

    card = queue_notice_text(harness.bridge.state.data["pending"]["advisor"])

    assert card is not None
    assert "1 Nachricht wartet" in card
    assert "**Aktiv:**" not in card
    assert "**Nächste Anfrage:** Text" in card


def test_queue_mode_keeps_same_thread_followups_fifo(tmp_path: Path):
    harness = SteeringHarness(tmp_path, "queue")
    harness.enqueue("$root", "root request", None)
    harness.busy = True
    harness.enqueue("$follow-one", "first follow-up", "$root")
    harness.enqueue("$follow-two", "second follow-up", "$root")

    pending = harness.bridge.state.data["pending"]["advisor"]

    assert [request["source_event_ids"] for request in pending] == [
        ["$root"],
        ["$follow-one"],
        ["$follow-two"],
    ]
    assert not any(request.get("deferred_steering") for request in pending)


def test_top_level_and_cross_thread_requests_never_join_active_batch(
    tmp_path: Path,
):
    harness = SteeringHarness(tmp_path, "auto")
    harness.enqueue("$root", "root request", None)
    harness.busy = True
    harness.enqueue("$follow", "thread follow-up", "$root")
    harness.enqueue("$top", "new top-level task", None)
    harness.enqueue("$other", "different thread task", "$other-root")

    pending = harness.bridge.state.data["pending"]["advisor"]

    assert len(pending) == 4
    assert pending[1]["source_event_ids"] == ["$follow"]
    assert pending[2]["source_event_ids"] == ["$top"]
    assert pending[3]["source_event_ids"] == ["$other"]
    assert pending[2]["thread_root_event_id"] == "$top"
    assert pending[3]["thread_root_event_id"] == "$other-root"


def test_same_thread_batch_keeps_arrival_order_after_earlier_top_level_request(
    tmp_path: Path,
):
    harness = SteeringHarness(tmp_path, "auto")
    harness.enqueue("$root", "root request", None)
    harness.busy = True
    harness.enqueue("$top", "earlier top-level task", None)
    harness.enqueue("$follow", "later thread follow-up", "$root")

    pending = harness.bridge.state.data["pending"]["advisor"]

    assert [request["source_event_ids"] for request in pending] == [
        ["$root"],
        ["$top"],
        ["$follow"],
    ]


def test_admitted_same_turn_batch_produces_one_answer_and_closes_every_qitem(
    tmp_path: Path,
):
    harness = SteeringHarness(tmp_path, "auto")
    harness.enqueue("$root", "root request", None)
    harness.busy = True
    harness.enqueue("$follow-one", "first follow-up", "$root")
    harness.enqueue("$follow-two", "second follow-up", "$root")
    pending = harness.bridge.state.data["pending"]["advisor"]
    pending[1]["injection_ready_at"] = 0

    asyncio.run(harness.bridge._poll_agent(harness.agent))

    assert len(harness.steers) == 1
    assert "$follow-one" in harness.steers[0]
    assert "$follow-two" in harness.steers[0]
    assert pending[1]["injection_phase"] == "admitted"
    assert pending[1]["injection_turn_id"] == "turn-1"

    append_records(
        harness.transcript,
        [
            transcript_agent_message(
                "turn-1",
                "final-one",
                "final_answer",
                "One answer for the active turn and both follow-ups.",
            )
        ],
    )
    asyncio.run(harness.bridge._poll_agent(harness.agent))

    assert harness.sent_answers == [
        ("One answer for the active turn and both follow-ups.", "$root")
    ]
    assert harness.sent_source_ids == [["$root", "$follow-one", "$follow-two"]]
    assert sorted(harness.finished_qitems) == [
        "$qitem-1",
        "$qitem-2",
        "$qitem-3",
    ]
    assert harness.bridge.state.data["pending"]["advisor"] == []


def test_steering_error_waits_for_grace_then_falls_back_once(
    tmp_path: Path,
):
    from openrig_matrix_bridge.openrig import GatewayError

    harness = SteeringHarness(tmp_path, "steer")
    harness.enqueue("$root", "root request", None)
    harness.busy = True
    harness.enqueue("$follow-one", "first follow-up", "$root")
    harness.enqueue("$follow-two", "second follow-up", "$root")
    pending = harness.bridge.state.data["pending"]["advisor"]
    pending[1]["injection_ready_at"] = 0
    harness.steer_error = GatewayError("pane injection rejected")

    asyncio.run(harness.bridge._poll_agent(harness.agent))

    assert pending[1]["injection_phase"] == "injecting"
    assert pending[1]["last_injection_error"] == "pane injection rejected"

    append_records(
        harness.transcript,
        [
            transcript_agent_message(
                "turn-1",
                "final-base",
                "final_answer",
                "Base answer.",
            )
        ],
    )
    asyncio.run(harness.bridge._poll_agent(harness.agent))
    harness.busy = False
    asyncio.run(harness.bridge._poll_agent(harness.agent))
    assert pending[0]["injection_phase"] == "injecting"
    assert [prompt[1] for prompt in harness.prompts] == ["root request"]

    pending[0]["fallback_not_before"] = time.time() - 1
    asyncio.run(harness.bridge._poll_agent(harness.agent))

    assert [prompt[1] for prompt in harness.prompts] == [
        "root request",
        pending[0]["text"] if pending else harness.prompts[1][1],
    ]
    fallback_prompt = harness.prompts[1][1]
    assert fallback_prompt.count("$follow-one") >= 1
    assert fallback_prompt.count("$follow-two") >= 1
    assert fallback_prompt.index("$follow-one") < fallback_prompt.index("$follow-two")


def test_post_effect_steering_error_waits_for_admission_before_fallback(
    tmp_path: Path,
):
    from openrig_matrix_bridge.openrig import GatewayError

    harness = SteeringHarness(tmp_path, "steer")
    harness.enqueue("$root", "root request", None)
    harness.busy = True
    harness.enqueue("$follow", "thread follow-up", "$root")
    pending = harness.bridge.state.data["pending"]["advisor"]
    group = pending[1]
    group["injection_ready_at"] = 0
    harness.steer_error = GatewayError("pane response was lost after input")
    asyncio.run(harness.bridge._poll_agent(harness.agent))

    assert len(harness.steers) == 1
    assert group["injection_phase"] == "injecting"
    assert group["last_injection_error"] == "pane response was lost after input"

    append_records(
        harness.transcript,
        [transcript_user_message("turn-1", group["text"])],
    )
    asyncio.run(harness.bridge._poll_agent(harness.agent))

    assert group["injection_phase"] == "admitted"
    assert group["injection_turn_id"] == "turn-1"


def test_steering_waits_for_active_prompt_turn_identity(
    tmp_path: Path,
):
    harness = SteeringHarness(tmp_path, "auto")
    harness.record_prompt_admission = False
    harness.enqueue("$root", "root request", None)
    harness.busy = True
    harness.enqueue("$follow", "thread follow-up", "$root")
    pending = harness.bridge.state.data["pending"]["advisor"]
    group = pending[1]
    group["injection_ready_at"] = 0
    harness.steer_turn_id = "turn-human"
    append_records(
        harness.transcript,
        [transcript_user_message("turn-human", "unrelated local task")],
    )

    asyncio.run(harness.bridge._poll_agent(harness.agent))

    assert harness.steers == []
    assert group["injection_phase"] == "pending"
    assert group.get("injection_turn_id") is None
    assert group["last_injection_error"] == (
        "waiting for active Matrix prompt turn identity"
    )


def test_permanently_busy_unadmitted_batch_fails_closed_after_timeout(
    tmp_path: Path,
):
    harness = SteeringHarness(tmp_path, "auto")
    harness.enqueue("$root", "root request", None)
    harness.busy = True
    harness.enqueue("$follow-one", "first follow-up", "$root")
    harness.enqueue("$follow-two", "second follow-up", "$root")
    pending = harness.bridge.state.data["pending"]["advisor"]
    group = pending[1]
    group["injection_phase"] = "injected_unconfirmed"
    group["injection_offset"] = harness.transcript.stat().st_size
    group["injection_text"] = group["text"]
    group["injection_attempted_at"] = (
        time.time()
        - harness.bridge.config.response_timeout_minutes * 60
        - 1
    )
    pending.pop(0)

    asyncio.run(harness.bridge._poll_agent(harness.agent))

    assert [prompt[1] for prompt in harness.prompts] == ["root request"]
    assert harness.bridge.state.data["pending"]["advisor"] == []
    assert harness.qitem_states["$qitem-2"] == "failed"
    assert harness.qitem_states["$qitem-3"] == "failed"
    failure = harness.bridge.state.data["steering_failures"][-1]
    assert failure["source_event_ids"] == ["$follow-one", "$follow-two"]
    assert failure["thread_root_event_id"] == "$root"
    assert failure["qitem_ids"] == ["$qitem-2", "$qitem-3"]
    assert failure["reason"] == (
        "steering admission unresolved while the seat remained busy"
    )
    assert sum(
        "Steering-Eingabe konnte nicht bestätigt werden" in text
        for text, _reply_to in harness.sent_answers
    ) == 1


def test_unrelated_human_turn_does_not_supply_the_active_answer(
    tmp_path: Path,
):
    harness = SteeringHarness(tmp_path, "queue")
    harness.enqueue("$root", "root request", None)
    append_records(
        harness.transcript,
        [
            transcript_agent_message(
                "turn-1",
                "final-base",
                "final_answer",
                "Answer for the Matrix request.",
            ),
            transcript_user_message("turn-human", "unrelated local task"),
            transcript_agent_message(
                "turn-human",
                "final-human",
                "final_answer",
                "Answer for the unrelated human.",
            ),
        ],
    )

    asyncio.run(harness.bridge._poll_agent(harness.agent))

    assert harness.sent_answers == [("Answer for the Matrix request.", "$root")]


def test_restart_adopts_confirmed_injection_without_resending_batch(
    tmp_path: Path,
):
    harness = SteeringHarness(tmp_path, "auto")
    harness.enqueue("$root", "root request", None)
    harness.busy = True
    harness.enqueue("$follow-one", "first follow-up", "$root")
    pending = harness.bridge.state.data["pending"]["advisor"]
    group = pending[1]
    group["injection_phase"] = "injected_unconfirmed"
    group["injection_offset"] = harness.transcript.stat().st_size
    group["injection_text"] = group["text"]
    group["injection_attempted_at"] = time.time()
    append_records(
        harness.transcript,
        [
            transcript_user_message("turn-follow", group["text"]),
            transcript_agent_message(
                "turn-follow",
                "final-follow",
                "final_answer",
                "Follow-up answer.",
            ),
        ],
    )
    pending.pop(0)

    asyncio.run(harness.bridge._poll_agent(harness.agent))
    asyncio.run(harness.bridge._poll_agent(harness.agent))

    assert [prompt[1] for prompt in harness.prompts] == ["root request"]
    assert harness.sent_answers == [("Follow-up answer.", "$root")]
    assert group["prompt_phase"] == "sent"
    assert group["prompt_turn_id"] == "turn-follow"
    assert harness.bridge.state.data["pending"]["advisor"] == []


def test_restart_during_injection_waits_for_late_admission(
    tmp_path: Path,
):
    harness = SteeringHarness(tmp_path, "auto")
    harness.enqueue("$root", "root request", None)
    harness.busy = True
    harness.enqueue("$follow-one", "first follow-up", "$root")
    pending = harness.bridge.state.data["pending"]["advisor"]
    group = pending[1]
    group["injection_phase"] = "injecting"
    group["injection_offset"] = harness.transcript.stat().st_size
    group["injection_text"] = group["text"]
    group["injection_attempted_at"] = time.time() - 10
    pending.pop(0)
    harness.busy = False

    asyncio.run(harness.bridge._poll_agent(harness.agent))

    assert [prompt[1] for prompt in harness.prompts] == ["root request"]
    assert group["injection_phase"] == "injecting"
    assert group["fallback_not_before"] > time.time()

    append_records(
        harness.transcript,
        [
            transcript_user_message("turn-follow", group["text"]),
            transcript_agent_message(
                "turn-follow",
                "final-follow",
                "final_answer",
                "Follow-up answer.",
            ),
        ],
    )
    asyncio.run(harness.bridge._poll_agent(harness.agent))
    asyncio.run(harness.bridge._poll_agent(harness.agent))

    assert [prompt[1] for prompt in harness.prompts] == ["root request"]
    assert harness.sent_answers == [("Follow-up answer.", "$root")]
    assert group["externally_admitted"] is True


def test_completion_race_adopts_admitted_follow_turn_without_resend(
    tmp_path: Path,
):
    harness = SteeringHarness(tmp_path, "auto")
    harness.enqueue("$root", "root request", None)
    harness.busy = True
    harness.enqueue("$follow", "thread follow-up", "$root")
    pending = harness.bridge.state.data["pending"]["advisor"]
    group = pending[1]
    group["injection_phase"] = "injected_unconfirmed"
    group["injection_offset"] = harness.transcript.stat().st_size
    group["injection_text"] = group["text"]
    group["injection_attempted_at"] = time.time()
    append_records(
        harness.transcript,
        [
            transcript_agent_message(
                "turn-1",
                "final-base",
                "final_answer",
                "Base answer.",
            ),
            transcript_user_message("turn-follow", group["text"]),
            transcript_agent_message(
                "turn-follow",
                "final-follow",
                "final_answer",
                "Follow-up answer.",
            ),
        ],
    )

    asyncio.run(harness.bridge._poll_agent(harness.agent))
    asyncio.run(harness.bridge._poll_agent(harness.agent))

    assert [prompt[1] for prompt in harness.prompts] == ["root request"]
    assert harness.sent_answers == [
        ("Base answer.", "$root"),
        ("Follow-up answer.", "$root"),
    ]
    assert group["externally_admitted"] is True
    assert harness.bridge.state.data["pending"]["advisor"] == []


def test_async_transcript_admission_within_grace_prevents_fallback_resend(
    tmp_path: Path,
):
    harness = SteeringHarness(tmp_path, "auto")
    harness.enqueue("$root", "root request", None)
    harness.busy = True
    harness.enqueue("$follow-one", "first follow-up", "$root")
    harness.enqueue("$follow-two", "second follow-up", "$root")
    pending = harness.bridge.state.data["pending"]["advisor"]
    group = pending[1]
    group["injection_ready_at"] = 0
    harness.record_steering_admission = False

    asyncio.run(harness.bridge._poll_agent(harness.agent))
    assert group["injection_phase"] == "injected_unconfirmed"
    append_records(
        harness.transcript,
        [
            transcript_agent_message(
                "turn-1",
                "final-base",
                "final_answer",
                "Base answer.",
            )
        ],
    )
    harness.busy = False
    asyncio.run(harness.bridge._poll_agent(harness.agent))

    assert [prompt[1] for prompt in harness.prompts] == ["root request"]
    assert harness.bridge.state.data["pending"]["advisor"] == [group]

    append_records(
        harness.transcript,
        [
            transcript_user_message("turn-follow", group["text"]),
            transcript_agent_message(
                "turn-follow",
                "final-follow",
                "final_answer",
                "Follow-up answer.",
            ),
        ],
    )
    asyncio.run(harness.bridge._poll_agent(harness.agent))
    asyncio.run(harness.bridge._poll_agent(harness.agent))

    assert [prompt[1] for prompt in harness.prompts] == ["root request"]
    assert harness.sent_answers == [
        ("Base answer.", "$root"),
        ("Follow-up answer.", "$root"),
    ]
    assert group["externally_admitted"] is True
    assert harness.bridge.state.data["pending"]["advisor"] == []


def test_busy_late_next_turn_beyond_grace_is_adopted_without_resend(
    tmp_path: Path,
):
    harness = SteeringHarness(tmp_path, "auto")
    harness.enqueue("$root", "root request", None)
    harness.busy = True
    harness.enqueue("$follow", "thread follow-up", "$root")
    pending = harness.bridge.state.data["pending"]["advisor"]
    group = pending[1]
    group["injection_phase"] = "injected_unconfirmed"
    group["injection_offset"] = harness.transcript.stat().st_size
    group["injection_text"] = group["text"]
    group["injection_attempted_at"] = time.time() - 10
    group["fallback_not_before"] = time.time() - 1
    pending.pop(0)

    asyncio.run(harness.bridge._poll_agent(harness.agent))

    assert group["injection_phase"] == "injected_unconfirmed"
    assert [prompt[1] for prompt in harness.prompts] == ["root request"]

    append_records(
        harness.transcript,
        [
            transcript_user_message("turn-follow", group["text"]),
            transcript_agent_message(
                "turn-follow",
                "final-follow",
                "final_answer",
                "Follow-up answer.",
            ),
        ],
    )
    harness.busy = False
    asyncio.run(harness.bridge._poll_agent(harness.agent))
    asyncio.run(harness.bridge._poll_agent(harness.agent))

    assert [prompt[1] for prompt in harness.prompts] == ["root request"]
    assert harness.sent_answers == [("Follow-up answer.", "$root")]
    assert group["externally_admitted"] is True
    assert harness.bridge.state.data["pending"]["advisor"] == []


def test_unconfirmed_steering_batch_falls_back_after_grace_once(
    tmp_path: Path,
):
    harness = SteeringHarness(tmp_path, "auto")
    harness.enqueue("$root", "root request", None)
    harness.busy = True
    harness.enqueue("$follow-one", "first follow-up", "$root")
    harness.enqueue("$follow-two", "second follow-up", "$root")
    pending = harness.bridge.state.data["pending"]["advisor"]
    group = pending[1]
    group["injection_ready_at"] = 0
    harness.record_steering_admission = False

    asyncio.run(harness.bridge._poll_agent(harness.agent))
    append_records(
        harness.transcript,
        [
            transcript_agent_message(
                "turn-1",
                "final-base",
                "final_answer",
                "Base answer.",
            )
        ],
    )
    harness.busy = False
    asyncio.run(harness.bridge._poll_agent(harness.agent))
    assert [prompt[1] for prompt in harness.prompts] == ["root request"]

    group["fallback_not_before"] = time.time() - 1
    asyncio.run(harness.bridge._poll_agent(harness.agent))

    assert [prompt[1] for prompt in harness.prompts] == [
        "root request",
        group["text"],
    ]
    assert group["injection_phase"] == "fallback"


@pytest.mark.parametrize("disposition", ["timeout", "delivery_failed"])
def test_terminal_recovery_gets_fresh_admission_grace(
    tmp_path: Path,
    disposition: str,
):
    harness = SteeringHarness(tmp_path, "auto")
    harness.enqueue("$root", "root request", None)
    harness.busy = True
    harness.enqueue("$follow-one", "first follow-up", "$root")
    harness.enqueue("$follow-two", "second follow-up", "$root")
    pending = harness.bridge.state.data["pending"]["advisor"]
    base = pending[0]
    group = pending[1]
    group["injection_phase"] = "injected_unconfirmed"
    group["injection_offset"] = harness.transcript.stat().st_size
    group["injection_text"] = group["text"]
    group["injection_attempted_at"] = time.time() - 10
    base["delivery_failure_count"] = 3
    harness.busy = False

    asyncio.run(
        harness.bridge._advance_terminal_request(
            harness.agent,
            base,
            "!advisor:matrix.example.org",
            disposition=disposition,
            status_detail="terminal active turn",
        )
    )

    assert [prompt[1] for prompt in harness.prompts] == ["root request"]
    assert group["fallback_not_before"] > time.time()
    append_records(
        harness.transcript,
        [
            transcript_user_message("turn-follow", group["text"]),
            transcript_agent_message(
                "turn-follow",
                "final-follow",
                "final_answer",
                "Follow-up answer.",
            ),
        ],
    )
    asyncio.run(harness.bridge._poll_agent(harness.agent))
    asyncio.run(harness.bridge._poll_agent(harness.agent))

    assert [prompt[1] for prompt in harness.prompts] == ["root request"]
    assert any(
        text == "Follow-up answer." for text, _reply_to in harness.sent_answers
    )
    assert group["externally_admitted"] is True


def test_terminal_abandon_removes_only_current_legacy_request(
    tmp_path: Path,
):
    harness = SteeringHarness(tmp_path, "queue")
    terminal = {
        "event_id": "$terminal",
        "reply_to_event_id": "$terminal",
        "thread_root_event_id": "$terminal",
        "qitem_id": "$qitem-terminal",
        "qitem_ids": ["$qitem-terminal"],
        "qitem_phase": "done",
        "text": "terminal legacy request",
        "prompt_phase": "sent",
    }
    following = {
        "event_id": "$following",
        "reply_to_event_id": "$following",
        "thread_root_event_id": "$following",
        "qitem_id": "$qitem-following",
        "qitem_ids": ["$qitem-following"],
        "qitem_phase": "pending",
        "text": "following legacy request",
        "prompt_phase": "pending",
    }
    harness.bridge.state.data["pending"]["advisor"] = [terminal, following]
    harness.qitem_states["$qitem-terminal"] = "done"

    asyncio.run(
        harness.bridge._abandon_terminal_qitem(
            harness.agent,
            terminal,
            "!advisor:matrix.example.org",
        )
    )

    assert harness.bridge.state.data["pending"]["advisor"] == [following]
    assert [prompt[1] for prompt in harness.prompts] == [
        "following legacy request"
    ]
    assert harness.qitem_states["$qitem-following"] == "in-progress"
    assert sum(
        "sind terminal" in text for text, _reply_to in harness.sent_answers
    ) == 1


def test_timeout_recovers_two_legacy_requests_without_ids(
    tmp_path: Path,
):
    harness = SteeringHarness(tmp_path, "queue")
    timed_out = {
        "event_id": "$timed-out",
        "reply_to_event_id": "$timed-out",
        "thread_root_event_id": "$timed-out",
        "qitem_id": "$qitem-timed-out",
        "qitem_ids": ["$qitem-timed-out"],
        "qitem_phase": "claimed",
        "text": "timed out legacy request",
        "prompt_phase": "sent",
        "answer_phase": None,
    }
    following = {
        "event_id": "$following",
        "reply_to_event_id": "$following",
        "thread_root_event_id": "$following",
        "qitem_id": "$qitem-following",
        "qitem_ids": ["$qitem-following"],
        "qitem_phase": "pending",
        "text": "following legacy request",
        "prompt_phase": "pending",
    }
    harness.bridge.state.data["pending"]["advisor"] = [timed_out, following]

    asyncio.run(
        harness.bridge._advance_terminal_request(
            harness.agent,
            timed_out,
            "!advisor:matrix.example.org",
            disposition="timeout",
            status_detail="timeout",
        )
    )

    assert harness.bridge.state.data["pending"]["advisor"] == [following]
    assert harness.qitem_states["$qitem-timed-out"] == "failed"
    assert harness.qitem_states["$qitem-following"] == "in-progress"
    assert [prompt[1] for prompt in harness.prompts] == [
        "following legacy request"
    ]
    assert sum(
        "konnte nicht zugestellt" in text
        for text, _reply_to in harness.sent_answers
    ) == 1


@pytest.mark.parametrize("disposition", ["timeout", "delivery_failed"])
def test_terminal_active_turn_preserves_admitted_batch_without_resend(
    tmp_path: Path,
    disposition: str,
):
    harness = SteeringHarness(tmp_path, "auto")
    harness.enqueue("$root", "root request", None)
    harness.busy = True
    harness.enqueue("$follow", "thread follow-up", "$root")
    pending = harness.bridge.state.data["pending"]["advisor"]
    base = pending[0]
    group = pending[1]
    group["injection_phase"] = "admitted"
    group["injection_offset"] = harness.transcript.stat().st_size
    group["injection_text"] = group["text"]
    group["injection_attempted_at"] = time.time()
    group["injection_turn_id"] = "turn-follow"
    append_records(
        harness.transcript,
        [
            transcript_user_message("turn-follow", group["text"]),
            transcript_agent_message(
                "turn-follow",
                "final-follow",
                "final_answer",
                "Follow-up answer.",
            ),
        ],
    )
    base["delivery_failure_count"] = 3

    asyncio.run(
        harness.bridge._advance_terminal_request(
            harness.agent,
            base,
            "!advisor:matrix.example.org",
            disposition=disposition,
            status_detail="terminal active turn",
        )
    )
    asyncio.run(harness.bridge._poll_agent(harness.agent))

    assert [prompt[1] for prompt in harness.prompts] == ["root request"]
    assert any(
        text == "Follow-up answer." for text, _reply_to in harness.sent_answers
    )
    assert group["externally_admitted"] is True
    assert harness.bridge.state.data["pending"]["advisor"] == []


def test_reconcile_fails_remaining_qitems_in_mixed_terminal_batch(
    tmp_path: Path,
):
    harness = SteeringHarness(tmp_path, "auto")
    request = {
        "id": "batch-request",
        "event_id": "$follow-one",
        "reply_to_event_id": "$root",
        "thread_root_event_id": "$root",
        "source_event_ids": ["$follow-one", "$follow-two"],
        "qitem_id": "$qitem-two",
        "qitem_ids": ["$qitem-two", "$qitem-three"],
        "qitem_phase": "done",
        "qitem_phases": {
            "$qitem-two": "done",
            "$qitem-three": "pending",
        },
        "queue_tracked": True,
        "suppress_reply": False,
        "text": "batch",
        "prompt_phase": "sent",
        "answer_phase": None,
    }
    harness.bridge.state.data["pending"]["advisor"] = [request]
    harness.qitem_states["$qitem-two"] = "done"
    harness.qitem_states["$qitem-three"] = "pending"

    asyncio.run(harness.bridge._reconcile_pending_operations())
    asyncio.run(harness.bridge._reconcile_pending_operations())

    assert harness.qitem_states["$qitem-two"] == "done"
    assert harness.qitem_states["$qitem-three"] == "failed"
    assert request["qitem_phases"] == {
        "$qitem-two": "done",
        "$qitem-three": "failed",
    }
    assert harness.bridge.state.data["pending"]["advisor"] == []
    assert sum(
        "sind terminal" in text for text, _reply_to in harness.sent_answers
    ) == 1


def test_live_fallback_claim_preserves_terminal_qitem_and_fails_sibling(
    tmp_path: Path,
):
    harness = SteeringHarness(tmp_path, "auto")
    harness.enqueue("$root", "root request", None)
    harness.busy = True
    harness.enqueue("$follow-one", "first follow-up", "$root")
    harness.enqueue("$follow-two", "second follow-up", "$root")
    pending = harness.bridge.state.data["pending"]["advisor"]
    group = pending[1]
    pending.pop(0)
    group["injection_phase"] = "fallback"
    group["qitem_phase"] = "$qitem-2"
    group["qitem_phases"] = {
        "$qitem-2": "done",
        "$qitem-3": "pending",
    }
    harness.qitem_states["$qitem-2"] = "done"
    harness.qitem_states["$qitem-3"] = "pending"
    harness.busy = False

    asyncio.run(harness.bridge._poll_agent(harness.agent))
    asyncio.run(harness.bridge._reconcile_pending_operations())

    assert [prompt[1] for prompt in harness.prompts] == ["root request"]
    assert harness.qitem_states["$qitem-2"] == "done"
    assert harness.qitem_states["$qitem-3"] == "failed"
    assert group["qitem_phases"] == {
        "$qitem-2": "done",
        "$qitem-3": "failed",
    }
    assert harness.bridge.state.data["pending"]["advisor"] == []
    assert sum(
        "sind terminal" in text for text, _reply_to in harness.sent_answers
    ) == 1


def mixed_terminal_request() -> dict:
    return {
        "id": "batch-request",
        "event_id": "$follow-one",
        "reply_to_event_id": "$root",
        "thread_root_event_id": "$root",
        "source_event_ids": ["$follow-one", "$follow-two"],
        "qitem_id": "$qitem-two",
        "qitem_ids": ["$qitem-two", "$qitem-three"],
        "qitem_phase": "finishing",
        "qitem_phases": {
            "$qitem-two": "finishing",
            "$qitem-three": "claimed",
        },
        "queue_tracked": True,
        "suppress_reply": False,
        "text": "batch",
        "prompt_phase": "sent",
        "answer_phase": None,
    }


def test_finishing_reconciles_remote_failed_then_closes_sibling(
    tmp_path: Path,
):
    harness = SteeringHarness(tmp_path, "auto")
    request = mixed_terminal_request()
    harness.bridge.state.data["pending"]["advisor"] = [request]
    harness.qitem_states["$qitem-two"] = "failed"
    harness.qitem_states["$qitem-three"] = "in-progress"

    assert (
        asyncio.run(
            harness.bridge._advance_terminal_request(
                harness.agent,
                request,
                "!advisor:matrix.example.org",
                disposition="completed",
                status_detail="bereit",
            )
        )
        is False
    )
    assert request["qitem_phases"]["$qitem-two"] == "failed"
    assert "$qitem-two" not in harness.finished_qitems
    assert harness.qitem_states["$qitem-three"] == "failed"
    assert harness.bridge.state.data["pending"]["advisor"] == []
    assert sum(
        "sind terminal" in text for text, _reply_to in harness.sent_answers
    ) == 1


def test_failing_reconciles_remote_canceled_then_closes_sibling(
    tmp_path: Path,
):
    harness = SteeringHarness(tmp_path, "auto")
    request = mixed_terminal_request()
    request["qitem_phase"] = "failing"
    request["qitem_phases"] = {
        "$qitem-two": "failing",
        "$qitem-three": "claimed",
    }
    harness.bridge.state.data["pending"]["advisor"] = [request]
    harness.qitem_states["$qitem-two"] = "canceled"
    harness.qitem_states["$qitem-three"] = "in-progress"

    assert (
        asyncio.run(
            harness.bridge._advance_terminal_request(
                harness.agent,
                request,
                "!advisor:matrix.example.org",
                disposition="delivery_failed",
                status_detail="terminal failure",
            )
        )
        is False
    )
    assert request["qitem_phases"]["$qitem-two"] == "canceled"
    assert harness.qitem_states["$qitem-two"] == "canceled"

    assert harness.qitem_states["$qitem-three"] == "failed"
    assert harness.bridge.state.data["pending"]["advisor"] == []
    assert sum(
        "sind terminal" in text for text, _reply_to in harness.sent_answers
    ) == 1


@pytest.mark.parametrize("mode", ["steer", "auto"])
def test_steering_modes_queue_long_same_thread_message(
    tmp_path: Path,
    mode: str,
):
    harness = SteeringHarness(tmp_path, mode)
    harness.enqueue("$root", "root request", None)
    harness.busy = True
    harness.enqueue("$follow", "x" * 121, "$root")

    pending = harness.bridge.state.data["pending"]["advisor"]

    assert len(pending) == 2
    assert not pending[1].get("deferred_steering")


def test_queue_card_refreshes_when_a_waiting_head_request_starts(tmp_path: Path):
    harness = SteeringHarness(tmp_path, "queue")
    harness.busy = True
    harness.enqueue("$first", "first request", None)
    harness.enqueue("$second", "second request", None)
    pending = harness.bridge.state.data["pending"]["advisor"]
    assert pending[0].get("started_at") is None
    before = queue_notice_text(pending)
    assert before is not None
    assert "**Nächste Anfrage:** Text" in before
    harness.queue_updates.clear()
    harness.busy = False

    asyncio.run(harness.bridge._poll_agent(harness.agent))

    assert pending[0].get("started_at") is not None
    assert len(harness.queue_updates) >= 1
    after = queue_notice_text(pending)
    assert after is not None
    assert '**Aktiv:** Text · "first request"' in after
    assert '1. Text · "second request"' in after


def test_legacy_pending_state_admits_steering_on_the_immediate_scan(tmp_path: Path):
    harness = SteeringHarness(tmp_path, "steer")
    harness.enqueue("$root", "root request", None)
    harness.busy = True
    harness.enqueue("$follow", "thread follow-up", "$root")
    pending = harness.bridge.state.data["pending"]["advisor"]
    active, group = pending[0], pending[1]
    # State saved before turn attribution existed: no prompt turn id, none required.
    active.pop("prompt_turn_id", None)
    active.pop("turn_attribution_required", None)
    group["injection_ready_at"] = 0

    # The immediate scan inside the attempt itself, not a later poll's scan.
    asyncio.run(harness.bridge._attempt_steering(harness.agent, active, group))

    assert len(harness.steers) == 1
    assert group["injection_phase"] == "admitted"
    assert group["injection_turn_id"] == "turn-1"
