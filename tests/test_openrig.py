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


def node_payload(transcript: Path, activity: str = "idle"):
    return [
        {
            "canonicalSessionName": "advisor-lead@kernel",
            "sessionStatus": "running",
            "runtime": "codex",
            "resumeToken": "01a1654e-3934-7162-b01e-81e809069786",
            "agentActivity": {"state": activity},
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
