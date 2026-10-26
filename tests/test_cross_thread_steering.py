"""Issue #87: steer mode steers short owner messages from any thread."""

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_steering import (
    SteeringHarness,
    append_records,
    transcript_agent_message,
    transcript_user_message,
)


def top_level_burst(harness: SteeringHarness) -> list[dict]:
    harness.enqueue("$root", "root request", None)
    harness.busy = True
    harness.enqueue("$top-one", "first unrelated task", None)
    harness.enqueue("$top-two", "second unrelated task", None)
    return harness.bridge.state.data["pending"]["advisor"]


def final_answer(harness: SteeringHarness, turn_id: str, text: str) -> None:
    append_records(
        harness.transcript,
        [transcript_agent_message(turn_id, f"final-{turn_id}", "final_answer", text)],
    )


def test_steer_mode_batches_unrelated_top_level_messages_into_the_active_turn(
    tmp_path: Path,
):
    harness = SteeringHarness(tmp_path, "steer")
    pending = top_level_burst(harness)

    assert len(pending) == 2
    group = pending[1]
    assert group["deferred_steering"] is True
    assert group["source_event_ids"] == ["$top-one", "$top-two"]
    assert group["qitem_ids"] == ["$qitem-2", "$qitem-3"]
    # The batch answers in the active request's thread; every source keeps
    # its own root.
    assert group["thread_root_event_id"] == "$root"
    assert group["reply_to_event_id"] == "$root"
    assert [entry["thread_root_event_id"] for entry in group["steering_entries"]] == [
        "$top-one",
        "$top-two",
    ]

    group["injection_ready_at"] = 0
    asyncio.run(harness.bridge._poll_agent(harness.agent))

    assert len(harness.steers) == 1
    steer = harness.steers[0]
    assert steer.index("first unrelated task") < steer.index("second unrelated task")
    assert "while another request was active" in steer
    assert "[$top-one] (new top-level message) first unrelated task" in steer

    final_answer(harness, "turn-1", "One answer for all three messages.")
    asyncio.run(harness.bridge._poll_agent(harness.agent))

    assert harness.sent_answers == [("One answer for all three messages.", "$root")]
    assert harness.sent_source_ids == [["$root", "$top-one", "$top-two"]]
    assert sorted(harness.finished_qitems) == ["$qitem-1", "$qitem-2", "$qitem-3"]
    assert harness.bridge.state.data["pending"]["advisor"] == []


@pytest.mark.parametrize("mode", ["auto", "queue"])
def test_other_modes_keep_unrelated_top_level_messages_fifo(
    tmp_path: Path,
    mode: str,
):
    pending = top_level_burst(SteeringHarness(tmp_path, mode))

    assert [request["source_event_ids"] for request in pending] == [
        ["$root"],
        ["$top-one"],
        ["$top-two"],
    ]
    assert not any(request.get("deferred_steering") for request in pending)


def test_steer_mode_injects_a_reply_from_another_thread_keeping_its_root(
    tmp_path: Path,
):
    harness = SteeringHarness(tmp_path, "steer")
    harness.enqueue("$root", "root request", None)
    harness.busy = True
    harness.enqueue("$other", "reply in another thread", "$other-root")
    pending = harness.bridge.state.data["pending"]["advisor"]
    group = pending[1]

    entry = group["steering_entries"][0]
    assert entry["event_id"] == "$other"
    assert entry["thread_root_event_id"] == "$other-root"
    assert entry["qitem_id"] == "$qitem-2"
    assert group["thread_root_event_id"] == "$root"

    group["injection_ready_at"] = 0
    asyncio.run(harness.bridge._poll_agent(harness.agent))
    assert "[$other] (sent in thread $other-root) reply in another thread" in (
        harness.steers[0]
    )
    final_answer(harness, "turn-1", "Answer.")
    asyncio.run(harness.bridge._poll_agent(harness.agent))

    assert harness.sent_answers == [("Answer.", "$root")]
    assert harness.sent_source_ids == [["$root", "$other"]]
    assert sorted(harness.finished_qitems) == ["$qitem-1", "$qitem-2"]


def test_same_thread_batch_text_is_unchanged(tmp_path: Path):
    harness = SteeringHarness(tmp_path, "steer")
    harness.enqueue("$root", "root request", None)
    harness.busy = True
    harness.enqueue("$follow", "thread follow-up", "$root")

    text = harness.bridge.state.data["pending"]["advisor"][1]["text"]

    assert text.startswith("Matrix follow-up batch for this thread.")
    assert "1. [$follow] thread follow-up" in text


def test_steer_mode_queues_a_long_message_from_another_thread(tmp_path: Path):
    harness = SteeringHarness(tmp_path, "steer")
    harness.enqueue("$root", "root request", None)
    harness.busy = True
    harness.enqueue("$top", "x" * 121, None)

    pending = harness.bridge.state.data["pending"]["advisor"]

    assert len(pending) == 2
    assert not pending[1].get("deferred_steering")
    assert pending[1]["thread_root_event_id"] == "$top"


def test_steer_mode_does_not_pull_media_from_another_thread(tmp_path: Path):
    harness = SteeringHarness(tmp_path, "steer")
    harness.enqueue("$root", "root request", None)
    harness.busy = True
    asyncio.run(
        harness.bridge._enqueue_prompt(
            harness.agent,
            SimpleNamespace(room_id="!advisor:matrix.example.org"),
            "$image",
            1_700_000_000_000,
            "Image caption",
            source_label="image",
        )
    )

    pending = harness.bridge.state.data["pending"]["advisor"]

    assert len(pending) == 2
    assert not pending[1].get("deferred_steering")
    assert pending[1]["source_label"] == "image"


def test_cross_thread_batch_falls_back_once_and_answers_in_the_active_thread(
    tmp_path: Path,
):
    harness = SteeringHarness(tmp_path, "steer")
    pending = top_level_burst(harness)
    group = pending[1]
    group["injection_ready_at"] = 0
    harness.record_steering_admission = False

    asyncio.run(harness.bridge._poll_agent(harness.agent))
    final_answer(harness, "turn-1", "Base answer.")
    harness.busy = False
    asyncio.run(harness.bridge._poll_agent(harness.agent))
    group["fallback_not_before"] = time.time() - 1
    asyncio.run(harness.bridge._poll_agent(harness.agent))

    assert [prompt[1] for prompt in harness.prompts] == ["root request", group["text"]]
    assert group["injection_phase"] == "fallback"
    final_answer(harness, "turn-2", "Fallback answer.")
    asyncio.run(harness.bridge._poll_agent(harness.agent))

    assert harness.sent_answers == [
        ("Base answer.", "$root"),
        ("Fallback answer.", "$root"),
    ]
    assert harness.sent_source_ids[-1] == ["$top-one", "$top-two"]
    assert sorted(harness.finished_qitems) == ["$qitem-1", "$qitem-2", "$qitem-3"]
    assert harness.bridge.state.data["pending"]["advisor"] == []


def test_restart_adopts_a_confirmed_cross_thread_injection_without_resend(
    tmp_path: Path,
):
    harness = SteeringHarness(tmp_path, "steer")
    pending = top_level_burst(harness)
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
                "turn-follow", "final-follow", "final_answer", "Follow-up answer."
            ),
        ],
    )
    pending.pop(0)

    asyncio.run(harness.bridge._poll_agent(harness.agent))
    asyncio.run(harness.bridge._poll_agent(harness.agent))

    assert [prompt[1] for prompt in harness.prompts] == ["root request"]
    assert harness.sent_answers == [("Follow-up answer.", "$root")]
    assert harness.sent_source_ids == [["$top-one", "$top-two"]]
    assert harness.bridge.state.data["pending"]["advisor"] == []


def test_completion_race_adopts_a_cross_thread_follow_turn_without_resend(
    tmp_path: Path,
):
    harness = SteeringHarness(tmp_path, "steer")
    pending = top_level_burst(harness)
    group = pending[1]
    group["injection_phase"] = "injected_unconfirmed"
    group["injection_offset"] = harness.transcript.stat().st_size
    group["injection_text"] = group["text"]
    group["injection_attempted_at"] = time.time()
    append_records(
        harness.transcript,
        [
            transcript_agent_message(
                "turn-1", "final-base", "final_answer", "Base answer."
            ),
            transcript_user_message("turn-follow", group["text"]),
            transcript_agent_message(
                "turn-follow", "final-follow", "final_answer", "Follow-up answer."
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
    assert harness.bridge.state.data["pending"]["advisor"] == []
