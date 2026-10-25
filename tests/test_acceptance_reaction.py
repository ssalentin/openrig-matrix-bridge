import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from nio import RoomSendError, RoomSendResponse
from test_steering import (
    SteeringHarness,
    append_records,
    transcript_agent_message,
    transcript_user_message,
)

ROOM_ID = "!advisor:matrix.example.org"
OWNER = "@owner:matrix.example.org"


class ReactionClient:
    """Matrix client double with homeserver-style reaction idempotency.

    Synapse returns the original event for a repeated transaction ID and
    rejects a second identical annotation from the same sender.
    """

    def __init__(self):
        self.sends: list[dict] = []
        self.by_tx: dict[str, str] = {}
        self.annotations: dict[tuple[str, str], str] = {}
        self.fail_next: int = 0

    def reactions(self, event_id: str | None = None) -> list[str]:
        return [
            reaction_id
            for (target, _key), reaction_id in self.annotations.items()
            if event_id is None or target == event_id
        ]

    async def room_send(self, room_id, message_type, content, **kwargs):
        tx_id = kwargs.get("tx_id")
        self.sends.append(
            {
                "room_id": room_id,
                "type": message_type,
                "content": content,
                "tx_id": tx_id,
            }
        )
        if self.fail_next:
            self.fail_next -= 1
            return RoomSendError("homeserver unavailable", "M_UNKNOWN")
        if tx_id in self.by_tx:
            return RoomSendResponse(self.by_tx[tx_id], room_id)
        relation = content.get("m.relates_to", {})
        annotation = (relation.get("event_id"), relation.get("key"))
        if message_type == "m.reaction" and annotation in self.annotations:
            return RoomSendError(
                "Can't send same reaction twice",
                "M_DUPLICATE_ANNOTATION",
            )
        event_id = f"$reaction-{len(self.by_tx) + 1}"
        self.by_tx[tx_id] = event_id
        if message_type == "m.reaction":
            self.annotations[annotation] = event_id
        return RoomSendResponse(event_id, room_id)


class AckHarness(SteeringHarness):
    def __init__(self, tmp_path: Path, mode: str = "auto"):
        super().__init__(tmp_path, mode)
        self.client = ReactionClient()
        self.bridge.client = self.client
        self.bridge.config.acceptance_reaction_enabled = True
        self.bridge.config.user_id = "@openrig:matrix.example.org"
        self.bridge.config.allowed_users = (OWNER,)
        self.bridge.config.max_message_age_minutes = -1
        self.bridge.config.reaction_feedback_enabled = True

    def acks(self) -> dict:
        return self.bridge.state.data.get("acceptance_reactions", {})


def reaction_content(event_id: str) -> dict:
    return {
        "m.relates_to": {
            "rel_type": "m.annotation",
            "event_id": event_id,
            "key": "👍",
        }
    }


def test_accepted_owner_message_gets_exactly_one_thumbs_up(tmp_path: Path):
    harness = AckHarness(tmp_path)

    harness.enqueue("$root", "root request", None)

    reaction_sends = [
        send for send in harness.client.sends if send["type"] == "m.reaction"
    ]
    assert reaction_sends == [
        {
            "room_id": ROOM_ID,
            "type": "m.reaction",
            "content": reaction_content("$root"),
            "tx_id": "ack-$root",
        }
    ]
    record = harness.acks()["$root"]
    assert record["phase"] == "sent"
    assert record["reaction_event_id"] == "$reaction-1"
    assert harness.prompts == [("session-one", "root request")]


def test_acknowledgement_is_persisted_with_the_accepted_request(
    tmp_path: Path,
):
    harness = AckHarness(tmp_path)
    snapshots = []

    def save():
        pending = harness.bridge.state.data["pending"].get("advisor", [])
        snapshots.append(
            (
                [request["event_id"] for request in pending],
                dict(harness.acks().get("$root") or {}),
            )
        )

    harness.bridge.state.save = save
    harness.busy = True

    harness.enqueue("$root", "root request", None)

    first_with_request = next(
        snapshot for snapshot in snapshots if snapshot[0] == ["$root"]
    )
    assert first_with_request[1]["phase"] == "sending"
    assert first_with_request[1]["tx_id"] == "ack-$root"
    assert harness.acks()["$root"]["phase"] == "sent"


def test_qitem_creation_failure_gets_no_acknowledgement(tmp_path: Path):
    from openrig_matrix_bridge.openrig import GatewayError

    harness = AckHarness(tmp_path)

    async def failing_qitem(*args, **kwargs):
        raise GatewayError("rig queue create failed")

    harness.bridge._create_inbound_qitem = failing_qitem

    harness.enqueue("$root", "root request", None)

    assert harness.client.reactions() == []
    assert harness.acks() == {}
    assert harness.bridge.state.data["pending"]["advisor"] == []
    assert any(
        "Queue-Item" in text for text, _reply_to in harness.sent_answers
    )


def message_event(event_id: str, sender: str, body: str, **extra):
    return SimpleNamespace(
        event_id=event_id,
        sender=sender,
        server_timestamp=int(time.time() * 1000),
        body=body,
        source={"content": {"msgtype": "m.text", "body": body}},
        **extra,
    )


def run_room_message(harness: AckHarness, event) -> None:
    async def no_verification(*args):
        return False

    harness.bridge._handle_verification_reply = no_verification
    asyncio.run(
        harness.bridge._handle_room_message(
            SimpleNamespace(room_id=ROOM_ID),
            event,
        )
    )


def test_untrusted_sender_gets_no_acknowledgement(tmp_path: Path):
    harness = AckHarness(tmp_path)

    run_room_message(
        harness,
        message_event("$stranger", "@stranger:matrix.example.org", "hello"),
    )

    assert harness.client.sends == []
    assert harness.acks() == {}
    assert harness.prompts == []


def test_message_rejected_as_too_old_gets_no_acknowledgement(tmp_path: Path):
    harness = AckHarness(tmp_path)
    harness.bridge.config.max_message_age_minutes = 1
    event = message_event("$old", OWNER, "stale request")
    event.server_timestamp = int((time.time() - 3600) * 1000)

    run_room_message(harness, event)

    assert harness.client.reactions() == []
    assert harness.acks() == {}


def test_duplicate_event_delivery_does_not_repeat_acknowledgement(
    tmp_path: Path,
):
    harness = AckHarness(tmp_path)
    event = message_event("$root", OWNER, "root request")

    run_room_message(harness, event)
    run_room_message(harness, event)

    assert harness.client.reactions("$root") == ["$reaction-1"]
    assert [
        send["tx_id"]
        for send in harness.client.sends
        if send["type"] == "m.reaction"
    ] == ["ack-$root"]
    assert harness.prompts == [("session-one", "root request")]


def test_repeated_enqueue_of_acknowledged_event_does_not_resend(
    tmp_path: Path,
):
    harness = AckHarness(tmp_path)
    harness.enqueue("$root", "root request", None)
    sends_before = len(harness.client.sends)

    asyncio.run(
        harness.bridge._acknowledge_acceptance(ROOM_ID, "$root")
    )
    asyncio.run(harness.bridge._recover_acceptance_reactions())

    assert len(harness.client.sends) == sends_before
    assert harness.client.reactions("$root") == ["$reaction-1"]


def test_each_steering_batch_event_gets_one_acknowledgement(tmp_path: Path):
    harness = AckHarness(tmp_path)
    harness.enqueue("$root", "root request", None)
    harness.busy = True
    harness.enqueue("$follow-one", "first follow-up", "$root")
    harness.enqueue("$follow-two", "second follow-up", "$root")
    pending = harness.bridge.state.data["pending"]["advisor"]
    group = pending[1]
    group["injection_ready_at"] = 0

    asyncio.run(harness.bridge._poll_agent(harness.agent))
    assert group["injection_phase"] == "admitted"
    append_records(
        harness.transcript,
        [
            transcript_agent_message(
                "turn-1",
                "final-base",
                "final_answer",
                "Combined answer.",
            )
        ],
    )
    harness.busy = False
    asyncio.run(harness.bridge._poll_agent(harness.agent))
    asyncio.run(harness.bridge._recover_acceptance_reactions())

    reaction_targets = [
        send["content"]["m.relates_to"]["event_id"]
        for send in harness.client.sends
        if send["type"] == "m.reaction"
    ]
    assert reaction_targets == ["$root", "$follow-one", "$follow-two"]
    assert sorted(harness.acks()) == ["$follow-one", "$follow-two", "$root"]
    assert all(record["phase"] == "sent" for record in harness.acks().values())
    assert harness.bridge.state.data["pending"]["advisor"] == []


def test_restart_after_ack_send_reuses_transaction_without_duplicate(
    tmp_path: Path,
):
    harness = AckHarness(tmp_path)
    crashes = []

    def crash_after_effect(point: str) -> None:
        if point == "ack_send" and not crashes:
            crashes.append(point)
            raise SystemExit(86)

    harness.bridge._crash_after_effect = crash_after_effect
    harness.busy = True
    with pytest.raises(SystemExit):
        harness.enqueue("$root", "root request", None)

    record = harness.acks()["$root"]
    assert record["phase"] == "sending"
    assert harness.client.reactions("$root") == ["$reaction-1"]

    harness.bridge._crash_after_effect = lambda point: None
    asyncio.run(harness.bridge._recover_acceptance_reactions())

    assert [
        send["tx_id"]
        for send in harness.client.sends
        if send["type"] == "m.reaction"
    ] == ["ack-$root", "ack-$root"]
    assert harness.client.reactions("$root") == ["$reaction-1"]
    assert harness.acks()["$root"]["phase"] == "sent"
    assert harness.acks()["$root"]["reaction_event_id"] == "$reaction-1"


def test_recovery_treats_duplicate_annotation_as_already_acknowledged(
    tmp_path: Path,
):
    harness = AckHarness(tmp_path)
    harness.enqueue("$root", "root request", None)
    harness.acks()["$root"]["phase"] = "sending"
    harness.client.by_tx.clear()

    asyncio.run(harness.bridge._recover_acceptance_reactions())

    assert harness.client.reactions("$root") == ["$reaction-1"]
    assert harness.acks()["$root"]["phase"] == "sent"


def recover_after_backoff(harness: AckHarness, event_id: str) -> None:
    harness.acks()[event_id]["retry_at"] = 0
    asyncio.run(harness.bridge._recover_acceptance_reactions())


def test_failed_acknowledgement_retries_with_same_transaction_then_stops(
    tmp_path: Path,
):
    from openrig_matrix_bridge.bridge import MAX_ACCEPTANCE_ATTEMPTS

    harness = AckHarness(tmp_path)
    harness.client.fail_next = 1

    harness.enqueue("$root", "root request", None)

    assert harness.acks()["$root"]["phase"] == "sending"
    assert harness.acks()["$root"]["attempts"] == 1
    assert harness.prompts == [("session-one", "root request")]

    recover_after_backoff(harness, "$root")

    assert harness.acks()["$root"]["phase"] == "sent"
    assert harness.client.reactions("$root") == ["$reaction-1"]
    assert {send["tx_id"] for send in harness.client.sends} == {"ack-$root"}

    harness.enqueue("$second", "second request", None)
    harness.client.fail_next = 99
    harness.acks()["$second"].update(phase="sending", attempts=0)
    harness.client.by_tx.pop("ack-$second", None)
    harness.client.annotations.pop(("$second", "👍"), None)
    for _attempt in range(MAX_ACCEPTANCE_ATTEMPTS + 2):
        recover_after_backoff(harness, "$second")

    assert harness.acks()["$second"]["phase"] == "failed"
    assert harness.acks()["$second"]["attempts"] == MAX_ACCEPTANCE_ATTEMPTS
    assert harness.client.reactions("$second") == []


def test_failed_acknowledgement_backs_off_before_retrying(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    import openrig_matrix_bridge.bridge as bridge_module

    now = [1_000.0]
    monkeypatch.setattr(bridge_module.time, "time", lambda: now[0])
    harness = AckHarness(tmp_path)
    harness.client.fail_next = 2

    harness.enqueue("$root", "root request", None)
    first_retry_at = harness.acks()["$root"]["retry_at"]
    assert first_retry_at > now[0]

    for _poll in range(10):
        asyncio.run(harness.bridge._recover_acceptance_reactions())
    assert harness.acks()["$root"]["attempts"] == 1

    now[0] = first_retry_at
    asyncio.run(harness.bridge._recover_acceptance_reactions())
    assert harness.acks()["$root"]["attempts"] == 2
    assert harness.acks()["$root"]["phase"] == "sending"
    second_retry_at = harness.acks()["$root"]["retry_at"]
    assert second_retry_at - now[0] > first_retry_at - 1_000.0

    now[0] = second_retry_at
    asyncio.run(harness.bridge._recover_acceptance_reactions())
    assert harness.acks()["$root"]["phase"] == "sent"
    assert harness.client.reactions("$root") == ["$reaction-1"]


def test_disabled_acceptance_reaction_sends_nothing(tmp_path: Path):
    harness = AckHarness(tmp_path)
    harness.bridge.config.acceptance_reaction_enabled = False

    harness.enqueue("$root", "root request", None)

    assert harness.client.sends == []
    assert harness.acks() == {}
    assert harness.prompts == [("session-one", "root request")]


def test_default_config_accepts_and_starts_work_without_a_reaction(
    tmp_path: Path,
):
    harness = AckHarness(tmp_path)
    del harness.bridge.config.acceptance_reaction_enabled
    feedback: list[tuple[str, object]] = []

    async def set_typing(room_id, typing):
        feedback.append(("typing", typing))

    async def update_status(agent, status, detail):
        feedback.append(("status", status))

    harness.bridge._set_typing = set_typing
    harness.bridge._update_agent_status = update_status

    harness.enqueue("$root", "root request", None)
    asyncio.run(harness.bridge._recover_acceptance_reactions())

    assert harness.client.sends == []
    assert harness.acks() == {}
    assert harness.prompts == [("session-one", "root request")]
    assert feedback == [("typing", True), ("status", "busy")]


def test_disabling_the_receipt_stops_pending_reaction_recovery(tmp_path: Path):
    harness = AckHarness(tmp_path)
    harness.client.fail_next = 1
    harness.enqueue("$root", "root request", None)
    assert harness.acks()["$root"]["phase"] == "sending"
    sends = len(harness.client.sends)

    harness.bridge.config.acceptance_reaction_enabled = False
    recover_after_backoff(harness, "$root")

    assert len(harness.client.sends) == sends
    assert harness.client.reactions("$root") == []


def test_silent_reaction_feedback_gets_no_acknowledgement(tmp_path: Path):
    harness = AckHarness(tmp_path)
    asyncio.run(
        harness.bridge._enqueue_prompt(
            harness.agent,
            SimpleNamespace(room_id=ROOM_ID),
            "$owner-reaction",
            1_700_000_000_000,
            "silent feedback",
            "reaction",
            "$bot-answer",
            suppress_reply=True,
            queue_tracked=False,
        )
    )

    assert harness.client.reactions() == []
    assert harness.acks() == {}


def test_latency_samples_cover_prompt_and_same_turn_steering(tmp_path: Path):
    harness = AckHarness(tmp_path)
    harness.enqueue("$root", "root request", None)
    harness.busy = True
    harness.enqueue("$follow", "thread follow-up", "$root")
    pending = harness.bridge.state.data["pending"]["advisor"]
    group = pending[1]
    group["injection_ready_at"] = 0

    asyncio.run(harness.bridge._poll_agent(harness.agent))

    samples = {
        sample["event_id"]: sample
        for sample in harness.bridge.state.data["latency_samples"]
    }
    assert samples["$root"]["kind"] == "prompt"
    assert samples["$follow"]["kind"] == "steer"
    for sample in samples.values():
        assert sample["intake_at"] <= sample["injected_at"]
        assert sample["injected_at"] <= sample["admitted_at"]
        assert sample["acknowledged_at"] is not None
    assert samples["$follow"]["turn_id"] == "turn-1"

    asyncio.run(harness.bridge._poll_agent(harness.agent))
    assert len(harness.bridge.state.data["latency_samples"]) == 2


def test_late_single_follow_up_keeps_root_thread_and_its_source_id(
    tmp_path: Path,
):
    harness = AckHarness(tmp_path)
    harness.enqueue("$root", "root request", None)
    harness.busy = True
    harness.enqueue("$follow", "thread follow-up", "$root")
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
    assert harness.sent_source_ids == [None, ["$follow"]]
    assert harness.bridge.state.data["pending"]["advisor"] == []


def test_prompt_injection_does_not_wait_for_acknowledgement(tmp_path: Path):
    harness = AckHarness(tmp_path)
    prompts_at_reaction = []
    room_send = harness.client.room_send

    async def observed_room_send(room_id, message_type, content, **kwargs):
        if message_type == "m.reaction":
            prompts_at_reaction.append(len(harness.prompts))
        return await room_send(room_id, message_type, content, **kwargs)

    harness.client.room_send = observed_room_send

    harness.enqueue("$root", "root request", None)

    assert prompts_at_reaction == [1]
    assert harness.acks()["$root"]["phase"] == "sent"
