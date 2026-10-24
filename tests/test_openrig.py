import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from openrig_matrix_bridge.openrig import (
    AgentSessionNotFound,
    AgentSessionRef,
    GatewayError,
    OpenRigGateway,
    OpenRigSessionResolver,
)


def node_payload(
    transcript: Path,
    activity: str = "idle",
    activity_state: dict | None = None,
):
    return [
        {
            "canonicalSessionName": "advisor-lead@kernel",
            "sessionStatus": "running",
            "runtime": "codex",
            "resumeToken": "01a1654e-3934-7162-b01e-81e809069786",
            "agentActivity": {"state": activity},
            "activityState": activity_state,
            "contextUsage": {"transcriptPath": str(transcript)},
        }
    ]


def test_resolves_live_codex_session_from_rig_inventory(tmp_path, monkeypatch):
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    payload = node_payload(transcript, activity="running")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args,
            0,
            stdout=json.dumps(payload),
            stderr="",
        ),
    )

    ref = OpenRigSessionResolver("/opt/rig", tmp_path).resolve("advisor-lead@kernel")

    assert ref.session_id == "01a1654e-3934-7162-b01e-81e809069786"
    assert ref.transcript_path == transcript
    assert ref.busy is True


def test_resolves_safe_activity_state_and_needs_input_count(tmp_path, monkeypatch):
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    payload = node_payload(
        transcript,
        activity="running",
        activity_state={
            "activity": "working",
            "needsInput": {
                "count": 1,
                "reason": "permission prompt",
            },
        },
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args,
            0,
            stdout=json.dumps(payload),
            stderr="",
        ),
    )

    ref = OpenRigSessionResolver("/opt/rig", tmp_path).resolve("advisor-lead@kernel")

    assert ref.activity_state == "working"
    assert ref.needs_input_count == 1


def test_falls_back_to_legacy_activity_when_taxonomy_is_unavailable(
    tmp_path,
    monkeypatch,
):
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    payload = node_payload(transcript, activity="idle")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args,
            0,
            stdout=json.dumps(payload),
            stderr="",
        ),
    )

    ref = OpenRigSessionResolver("/opt/rig", tmp_path).resolve("advisor-lead@kernel")

    assert ref.activity_state == "idle-at-prompt"
    assert ref.needs_input_count == 0


def test_finds_codex_transcript_by_thread_id_when_usage_has_no_path(
    tmp_path,
    monkeypatch,
):
    transcript = (
        tmp_path
        / "sessions"
        / "2026"
        / "10"
        / "24"
        / "rollout-01a1654e-3934-7162-b01e-81e809069786.jsonl"
    )
    transcript.parent.mkdir(parents=True)
    transcript.write_text("{}\n", encoding="utf-8")
    payload = node_payload(transcript)
    payload[0]["contextUsage"] = {}
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args,
            0,
            stdout=json.dumps(payload),
            stderr="",
        ),
    )

    ref = OpenRigSessionResolver("/opt/rig", tmp_path).resolve("advisor-lead@kernel")

    assert ref.transcript_path == transcript


def test_rejects_missing_live_session(tmp_path, monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args,
            0,
            stdout="[]",
            stderr="",
        ),
    )

    with pytest.raises(AgentSessionNotFound, match="no live Codex session"):
        OpenRigSessionResolver("/opt/rig", tmp_path).resolve("advisor-lead@kernel")


def test_sends_prompt_through_rig_cli_with_bridge_identity(monkeypatch):
    calls = []

    class Process:
        returncode = 0

        async def communicate(self):
            return b'{"ok":true}\n', b""

    async def create_subprocess(*args, **kwargs):
        calls.append((args, kwargs))
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_subprocess)
    gateway = OpenRigGateway("/opt/rig", "matrix-bridge@kernel")
    ref = AgentSessionRef(
        session_id="session",
        transcript_path=Path("/tmp/transcript.jsonl"),
        tmux_session="advisor-lead@kernel",
        busy=False,
    )

    asyncio.run(gateway.send_prompt(ref, "Hallo"))

    args, kwargs = calls[0]
    assert args == (
        "/opt/rig",
        "send",
        "advisor-lead@kernel",
        "Hallo",
        "--raw",
        "--json",
    )
    assert kwargs["env"]["OPENRIG_SESSION_NAME"] == "matrix-bridge@kernel"


def test_reports_rig_cli_failure(monkeypatch):
    class Process:
        returncode = 1

        async def communicate(self):
            return b"", b"target has an interactive prompt"

    async def create_subprocess(*args, **kwargs):
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_subprocess)
    gateway = OpenRigGateway("/opt/rig", "matrix-bridge@kernel")
    ref = AgentSessionRef(
        session_id="session",
        transcript_path=Path("/tmp/transcript.jsonl"),
        tmux_session="advisor-lead@kernel",
        busy=False,
    )

    with pytest.raises(GatewayError, match="interactive prompt"):
        asyncio.run(gateway.send_prompt(ref, "Hallo"))


def test_creates_matrix_qitem_with_priority_and_no_duplicate_nudge(
    monkeypatch,
):
    calls = []

    class Process:
        returncode = 0

        async def communicate(self):
            return (
                b'{"qitemId":"qitem-matrix","priority":"urgent"}\n',
                b"",
            )

    async def create_subprocess(*args, **kwargs):
        calls.append((args, kwargs))
        return Process()

    monkeypatch.setenv("OPENRIG_SESSION_NAME", "advisor-lead@kernel")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_subprocess)
    gateway = OpenRigGateway("/opt/rig", "matrix-bridge@kernel")

    qitem_id = asyncio.run(
        gateway.queue_create(
            source_session="owner@external",
            destination_session="advisor-lead@kernel",
            summary="Matrix: Hallo",
            body="Hallo",
            priority="urgent",
        )
    )

    args, kwargs = calls[0]
    assert qitem_id == "qitem-matrix"
    assert args[:5] == (
        "/opt/rig",
        "queue",
        "create",
        "--source",
        "owner@external",
    )
    assert "--no-nudge" in args
    assert args[args.index("--priority") + 1] == "urgent"
    assert "OPENRIG_SESSION_NAME" not in kwargs["env"]


def test_claims_and_finishes_qitem_as_destination_agent(monkeypatch):
    calls = []

    class Process:
        returncode = 0

        async def communicate(self):
            return b'{"qitemId":"qitem-matrix","state":"ok"}\n', b""

    async def create_subprocess(*args, **kwargs):
        calls.append((args, kwargs))
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_subprocess)
    gateway = OpenRigGateway("/opt/rig", "matrix-bridge@kernel")

    async def run_updates():
        await gateway.queue_claim("qitem-matrix", "advisor-lead@kernel")
        await gateway.queue_finish(
            "qitem-matrix",
            "advisor-lead@kernel",
            "Matrix answer delivered",
        )

    asyncio.run(run_updates())

    claim_args, claim_kwargs = calls[0]
    finish_args, finish_kwargs = calls[1]
    assert claim_args[:4] == ("/opt/rig", "queue", "claim", "qitem-matrix")
    assert finish_args[:4] == ("/opt/rig", "queue", "update", "qitem-matrix")
    assert "--closure-reason" in finish_args
    assert claim_kwargs["env"]["OPENRIG_SESSION_NAME"] == "advisor-lead@kernel"
    assert finish_kwargs["env"]["OPENRIG_SESSION_NAME"] == "advisor-lead@kernel"
