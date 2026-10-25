from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import threading
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


def read_complete_transcript_records(path: Path) -> list[dict[str, object]]:
    """Parse only newline-terminated lines; a writer may be mid-append."""
    if not path.exists():
        return []
    *complete, _unterminated = path.read_text(encoding="utf-8").split("\n")
    return [json.loads(line) for line in complete if line]


def wait_for_final_answers(path: Path, expected: list[str], timeout: float = 30.0):
    """Poll until every expected final answer is in the transcript, or fail."""
    deadline = time.monotonic() + timeout
    records: list[dict[str, object]] = []
    while time.monotonic() < deadline:
        records = read_complete_transcript_records(path)
        answers = [
            str(record)
            for record in records
            if record.get("payload", {}).get("item", {}).get("phase")
            == "final_answer"
        ]
        if all(any(text in answer for answer in answers) for text in expected):
            return records
        time.sleep(0.05)
    raise AssertionError(f"transcript never contained final answers {expected}")


def test_fake_transport_can_materialize_steering_after_completion(
    tmp_path: Path,
):
    # The first turn must outlast the time the steering `send` needs to start its
    # Python process, which can be hundreds of milliseconds on a loaded runner.
    first = fake(
        tmp_path,
        "send",
        "integration-agent@local",
        "active prompt",
        "--raw",
        "--json",
        delay="3.0",
    )
    assert first.returncode == 0, first.stderr

    steer = fake(
        tmp_path,
        "send",
        "integration-agent@local",
        "late follow-up",
        "--raw",
        "--json",
        allow_steering=True,
        extra_env={"OPENRIG_FAKE_LATE_STEER_SECONDS": "0.4"},
    )
    assert steer.returncode == 0, steer.stderr
    assert json.loads(steer.stdout)["materialization"] == "late"

    records = wait_for_final_answers(
        tmp_path / "transcript.jsonl",
        ["Fake integration answer: active prompt", "Late steering answer:"],
    )

    user_records = [
        record
        for record in records
        if record.get("type") == "response_item"
        and record.get("payload", {}).get("role") == "user"
    ]
    assert [record["payload"]["content"][0]["text"] for record in user_records] == [
        "active prompt",
        "late follow-up",
    ]
    assert len(
        {
            record["payload"]["internal_chat_message_metadata_passthrough"][
                "turn_id"
            ]
            for record in user_records
        }
    ) == 2


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
    assert result.returncode == 0, result.stderr

    records = wait_for_final_answers(
        tmp_path / "transcript.jsonl",
        [
            "Unrelated human answer that must not reach Matrix.",
            "Fake integration answer: owner prompt",
        ],
    )

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
            if (
                sum(
                    isinstance(record.get("payload"), dict)
                    and isinstance(record["payload"].get("item"), dict)
                    for record in records
                )
                >= 2
            ):
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


def write_fake_state(root: Path, **overrides: object) -> None:
    state: dict[str, object] = {
        "queue": {},
        "prompts": [],
        "steers": [],
        "calls": [],
        "human_registered": False,
        "busy_until": 0.0,
        **overrides,
    }
    (root / "fake-openrig.json").write_text(json.dumps(state), encoding="utf-8")


def replace_fake_state(root: Path, state: dict) -> None:
    """Replace the fake's state atomically, as its own writer does.

    A detached fake worker may be polling the file; a truncate-then-write
    could hand it a torn read and kill it silently.
    """
    temporary = root / "fake-openrig.json.tmp"
    temporary.write_text(json.dumps(state), encoding="utf-8")
    os.replace(temporary, root / "fake-openrig.json")


def read_fake_state(root: Path) -> dict:
    return json.loads((root / "fake-openrig.json").read_text(encoding="utf-8"))


def wait_for_transcript_text(path: Path, text: str, timeout: float = 30.0) -> None:
    """Poll until ``text`` appears in the transcript, or fail after a bounded wait."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and text in path.read_text(encoding="utf-8"):
            return
        time.sleep(0.05)
    raise AssertionError(f"{text!r} did not appear in the transcript within {timeout}s")


def test_fake_transport_stays_busy_until_its_turn_completes(tmp_path: Path):
    now = time.time()
    root_prompt = {
        "id": "root-prompt",
        "turn_id": "root-turn",
        "destination": "integration-agent@local",
        "prompt": "root",
        "sent_at": now - 5.0,
        "completed_at": None,
    }
    write_fake_state(tmp_path, prompts=[root_prompt], busy_until=now - 1.0)

    nodes = fake(tmp_path, "ps", "--json")
    assert json.loads(nodes.stdout)[0]["agentActivity"]["state"] == "running"
    steer = fake(
        tmp_path,
        "send",
        "integration-agent@local",
        "follow-up after the hold",
        "--raw",
        "--json",
        allow_steering=True,
    )
    assert steer.returncode == 0, steer.stderr
    assert json.loads(steer.stdout)["turnId"] == "root-turn"
    state = read_fake_state(tmp_path)
    assert len(state["prompts"]) == 1
    assert [item["turn_id"] for item in state["steers"]] == ["root-turn"]

    state["prompts"][0]["completed_at"] = time.time()
    (tmp_path / "fake-openrig.json").write_text(json.dumps(state), encoding="utf-8")
    nodes = fake(tmp_path, "ps", "--json")
    assert json.loads(nodes.stdout)[0]["agentActivity"]["state"] == "idle"


def test_fake_transport_hold_for_held_prompts_is_configurable(tmp_path: Path):
    sent = fake(
        tmp_path,
        "send",
        "integration-agent@local",
        "hold first configurable root",
        "--raw",
        "--json",
        extra_env={"OPENRIG_FAKE_HOLD_SECONDS": "0.6"},
    )
    assert sent.returncode == 0, sent.stderr
    state = read_fake_state(tmp_path)
    prompt = state["prompts"][0]
    assert state["busy_until"] - prompt["sent_at"] == pytest.approx(0.6, abs=0.05)


def test_replacing_the_fake_state_is_never_seen_partially(tmp_path: Path):
    spec = importlib.util.spec_from_file_location("fake_openrig_under_test", FAKE)
    assert spec is not None and spec.loader is not None
    fake_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fake_module)
    state = fake_module._default_state()
    state["prompts"] = [{"id": "p", "completed_at": None, "padding": "x" * 200_000}]
    write_fake_state(tmp_path, prompts=state["prompts"])

    torn: list[str] = []
    reads = [0]
    stop = threading.Event()

    def poll_like_a_worker() -> None:
        while not stop.is_set():
            try:
                fake_module.read_state(tmp_path)
                reads[0] += 1
            except json.JSONDecodeError as exc:
                torn.append(str(exc))
            time.sleep(0.001)

    reader = threading.Thread(target=poll_like_a_worker)
    reader.start()
    try:
        for _ in range(150):
            replace_fake_state(tmp_path, state)
            time.sleep(0.002)
    finally:
        stop.set()
        reader.join()

    assert reads[0] > 0
    assert torn == []


def test_fake_late_steer_materializes_only_after_its_root_completes(
    tmp_path: Path,
):
    now = time.time()
    write_fake_state(
        tmp_path,
        prompts=[
            {
                "id": "root-prompt",
                "turn_id": "root-turn",
                "destination": "integration-agent@local",
                "prompt": "root",
                "sent_at": now,
                "completed_at": None,
            }
        ],
        busy_until=now + 30.0,
    )
    (tmp_path / "transcript.jsonl").write_text("", encoding="utf-8")
    steer = fake(
        tmp_path,
        "send",
        "integration-agent@local",
        "late follow-up",
        "--raw",
        "--json",
        allow_steering=True,
        extra_env={"OPENRIG_FAKE_LATE_STEER_SECONDS": "0.1"},
    )
    assert json.loads(steer.stdout)["materialization"] == "late"
    transcript = tmp_path / "transcript.jsonl"
    time.sleep(0.8)
    assert "late follow-up" not in transcript.read_text(encoding="utf-8")

    state = read_fake_state(tmp_path)
    state["prompts"][0]["completed_at"] = time.time()
    replace_fake_state(tmp_path, state)
    wait_for_transcript_text(transcript, "Late steering answer: late follow-up")


def test_shipping_label_fixture_is_a_valid_nonzero_pdf():
    label = HARNESS_MODULE.shipping_label_pdf()

    assert label.startswith(b"%PDF-1.4\n")
    assert label.rstrip().endswith(b"%%EOF")
    assert len(label) > 500
    startxref = int(label.rsplit(b"startxref\n", 1)[1].split()[0])
    assert label[startxref:].startswith(b"xref\n")
    assert label == HARNESS_MODULE.shipping_label_pdf()
