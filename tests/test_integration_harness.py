from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FAKE = ROOT / "tools" / "fake_openrig.py"
HARNESS = ROOT / "tools" / "matrix_integration.py"
SPEC = importlib.util.spec_from_file_location("matrix_integration_harness", HARNESS)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"could not load integration harness from {HARNESS}")
HARNESS_MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = HARNESS_MODULE
SPEC.loader.exec_module(HARNESS_MODULE)

CheckFailure = HARNESS_MODULE.CheckFailure
validate_answer_relation = HARNESS_MODULE.validate_answer_relation
validate_fifo_records = HARNESS_MODULE.validate_fifo_records
validate_qitem_history = HARNESS_MODULE.validate_qitem_history
validate_source_provenance = HARNESS_MODULE.validate_source_provenance


def fake(
    root: Path,
    *args: str,
    fail: str | None = None,
    delay: str = "0.2",
    allow_steering: bool = False,
    unrelated_turn: bool = False,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = {
        **os.environ,
        "OPENRIG_FAKE_RESPONSE_DELAY": delay,
        "OPENRIG_FAKE_STATE": str(root),
        **(extra_env or {}),
    }
    if fail:
        environment["OPENRIG_FAKE_FAIL"] = fail
    if allow_steering:
        environment["OPENRIG_FAKE_ALLOW_STEERING"] = "1"
    if unrelated_turn:
        environment["OPENRIG_FAKE_UNRELATED_TURN"] = "1"
    return subprocess.run(
        [str(FAKE), *args],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=10,
    )


def test_fake_transport_records_qitem_transition_history(tmp_path: Path):
    created = fake(
        tmp_path,
        "queue",
        "create",
        "--source",
        "owner@external",
        "--destination",
        "integration-agent@local",
        "--summary",
        "Matrix message",
        "--body",
        "hello",
        "--priority",
        "urgent",
        "--tags",
        "matrix,inbound",
        "--no-nudge",
        "--json",
    )
    qitem_id = json.loads(created.stdout)["qitemId"]

    assert fake(tmp_path, "queue", "claim", qitem_id, "--json").returncode == 0
    assert (
        fake(
            tmp_path,
            "queue",
            "update",
            qitem_id,
            "--state",
            "done",
            "--closure-reason",
            "no-follow-on",
            "--resolution",
            "delivered",
            "--json",
        ).returncode
        == 0
    )

    state = json.loads((tmp_path / "fake-openrig.json").read_text())
    assert [item["state"] for item in state["queue"][qitem_id]["history"]] == [
        "pending",
        "in-progress",
        "done",
    ]


def test_fake_transport_rejects_duplicate_qitem_claim(tmp_path: Path):
    created = fake(
        tmp_path,
        "queue",
        "create",
        "--source",
        "owner@external",
        "--destination",
        "integration-agent@local",
        "--summary",
        "Matrix message",
        "--body",
        "hello",
        "--priority",
        "urgent",
        "--tags",
        "matrix,inbound",
        "--no-nudge",
        "--json",
    )
    qitem_id = json.loads(created.stdout)["qitemId"]

    assert fake(tmp_path, "queue", "claim", qitem_id, "--json").returncode == 0
    duplicate = fake(tmp_path, "queue", "claim", qitem_id, "--json")

    assert duplicate.returncode != 0
    assert "qitem_not_claimable" in duplicate.stderr


@pytest.mark.parametrize("failure", ["create", "claim", "finish", "fail"])
def test_fake_transport_injects_qitem_lifecycle_failures(
    tmp_path: Path,
    failure: str,
):
    if failure == "create":
        result = fake(
            tmp_path,
            "queue",
            "create",
            "--source",
            "owner@external",
            "--destination",
            "integration-agent@local",
            "--summary",
            "Matrix message",
            "--body",
            "hello",
            "--priority",
            "urgent",
            "--json",
            fail=failure,
        )
        assert result.returncode != 0
        assert f"injected queue {failure} failure" in result.stderr
        return

    created = fake(
        tmp_path,
        "queue",
        "create",
        "--source",
        "owner@external",
        "--destination",
        "integration-agent@local",
        "--summary",
        "Matrix message",
        "--body",
        "hello",
        "--priority",
        "urgent",
        "--json",
    )
    qitem_id = json.loads(created.stdout)["qitemId"]
    if failure == "claim":
        result = fake(
            tmp_path,
            "queue",
            "claim",
            qitem_id,
            "--json",
            fail=failure,
        )
    else:
        result = fake(
            tmp_path,
            "queue",
            "update",
            qitem_id,
            "--state",
            "done" if failure == "finish" else "failed",
            "--resolution",
            "failure probe",
            "--json",
            fail=failure,
        )

    assert result.returncode != 0
    assert f"injected queue {failure} failure" in result.stderr


def test_fake_transport_rejects_send_while_agent_is_busy(tmp_path: Path):
    first = fake(
        tmp_path,
        "send",
        "integration-agent@local",
        "ordinary prompt",
        "--raw",
        "--json",
        delay="5.0",
    )
    assert first.returncode == 0

    second = fake(
        tmp_path,
        "send",
        "integration-agent@local",
        "concurrent prompt",
        "--raw",
        "--json",
        delay="5.0",
    )

    assert second.returncode != 0
    assert "busy" in second.stderr.lower()


def test_fake_transport_records_best_effort_steering_inside_active_turn(
    tmp_path: Path,
):
    first = fake(
        tmp_path,
        "send",
        "integration-agent@local",
        "active prompt",
        "--raw",
        "--json",
        delay="5.0",
    )
    assert first.returncode == 0

    steer = fake(
        tmp_path,
        "send",
        "integration-agent@local",
        "steered follow-up",
        "--raw",
        "--json",
        delay="5.0",
        allow_steering=True,
    )

    assert steer.returncode == 0
    state = json.loads((tmp_path / "fake-openrig.json").read_text())
    assert [item["prompt"] for item in state["prompts"]] == ["active prompt"]
    assert [item["prompt"] for item in state["steers"]] == ["steered follow-up"]
    records = [
        json.loads(line)
        for line in (tmp_path / "transcript.jsonl").read_text().splitlines()
    ]
    user_records = [
        record
        for record in records
        if record.get("type") == "response_item"
        and record.get("payload", {}).get("role") == "user"
    ]
    assert [record["payload"]["content"][0]["text"] for record in user_records] == [
        "active prompt",
        "steered follow-up",
    ]
    assert (
        len(
            {
                record["payload"]["internal_chat_message_metadata_passthrough"][
                    "turn_id"
                ]
                for record in user_records
            }
        )
        == 1
    )


def test_fake_transport_can_emit_unrelated_human_final_before_owner_answer(
    tmp_path: Path,
):
    result = fake(
        tmp_path,
        "send",
        "integration-agent@local",
        "owner prompt",
        "--raw",
        "--json",
        delay="0.1",
        unrelated_turn=True,
    )
    assert result.returncode == 0

    deadline = time.monotonic() + 3
    records = []
    while time.monotonic() < deadline:
        records = [
            json.loads(line)
            for line in (tmp_path / "transcript.jsonl").read_text().splitlines()
        ]
        if any(
            record.get("payload", {}).get("item", {}).get("phase") == "final_answer"
            and "Fake integration answer" in str(record)
            for record in records
        ):
            break
        time.sleep(0.05)

    finals = [
        record["payload"]["item"]["content"][0]["text"]
        for record in records
        if record.get("payload", {}).get("item", {}).get("phase") == "final_answer"
    ]
    assert finals == [
        "Unrelated human answer that must not reach Matrix.",
        "Fake integration answer: owner prompt",
    ]


def test_fake_transport_accepts_natural_demo_copy(tmp_path: Path):
    sent = fake(
        tmp_path,
        "send",
        "integration-agent@local",
        "Summarize the release status",
        "--raw",
        "--json",
        extra_env={
            "OPENRIG_FAKE_COMMENTARY": "Reviewing the release notes.",
            "OPENRIG_FAKE_ANSWER": "Release status: ready for review.",
        },
    )
    assert sent.returncode == 0

    transcript = tmp_path / "transcript.jsonl"
    deadline = time.monotonic() + 5
    records: list[dict[str, object]] = []
    while time.monotonic() < deadline:
        if transcript.exists():
            records = [
                json.loads(line)
                for line in transcript.read_text(encoding="utf-8").splitlines()
            ]
            if len(records) >= 2:
                break
        time.sleep(0.05)

    texts = [
        record["payload"]["item"]["content"][0]["text"]
        for record in records
        if isinstance(record.get("payload"), dict)
        and isinstance(record["payload"].get("item"), dict)
    ]
    assert texts == ["Reviewing the release notes.", "Release status: ready for review."]


def test_thread_relation_requires_reference_to_owner_prompt():
    raw = {
        "type": "m.room.encrypted",
        "content": {
            "m.relates_to": {
                "rel_type": "m.thread",
                "event_id": "$prompt",
                "m.in_reply_to": {"event_id": "$prompt"},
            }
        },
    }

    validate_answer_relation(raw, "$prompt")
    with pytest.raises(CheckFailure, match="thread"):
        validate_answer_relation({"type": "m.room.encrypted", "content": {}}, "$prompt")


def test_qitem_history_must_match_pending_claimed_done():
    item = {
        "history": [
            {"state": "pending", "at": 1},
            {"state": "in-progress", "at": 2},
            {"state": "done", "at": 3},
        ]
    }
    validate_qitem_history(item)

    with pytest.raises(CheckFailure, match="pending"):
        validate_qitem_history({"state": "done"})


def test_fifo_requires_second_send_after_first_completion():
    records = [
        {"prompt": "hold first request", "sent_at": 1.0, "completed_at": 3.0},
        {"prompt": "second queued request", "sent_at": 3.1, "completed_at": 4.0},
    ]
    validate_fifo_records(records, "hold first request", "second queued request")

    with pytest.raises(CheckFailure, match="before"):
        validate_fifo_records(
            [
                {"prompt": "hold first request", "sent_at": 1.0, "completed_at": 4.0},
                {
                    "prompt": "second queued request",
                    "sent_at": 3.0,
                    "completed_at": 5.0,
                },
            ],
            "hold first request",
            "second queued request",
        )


def test_source_provenance_requires_clean_known_commit():
    digest = "a" * 40
    provenance = validate_source_provenance(digest, "", digest)
    assert provenance == {
        "commit": digest,
        "source": "git",
        "worktree_clean": True,
    }

    with pytest.raises(CheckFailure, match="dirty"):
        validate_source_provenance(digest, " M tools/matrix_integration.py", digest)
    with pytest.raises(CheckFailure, match="does not match"):
        validate_source_provenance(digest, "", "b" * 40)
