import json
import os
from pathlib import Path

from openrig_matrix_bridge.gateway import AgentSessionNotFound, AgentSessionResolver


def test_resolves_latest_live_state_for_tmux_session(tmp_path: Path):
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    state_dir = tmp_path / "sessions"
    state_dir.mkdir()
    payload = {
        "tmuxSession": "advisor-lead@kernel",
        "tmuxPane": "%1",
        "agentPid": os.getpid(),
        "transcriptPath": str(transcript),
        "lastStopTime": 42,
        "promptSequence": 3,
        "completedPromptSequence": 2,
    }
    (state_dir / "01abc.json").write_text(json.dumps(payload), encoding="utf-8")

    ref = AgentSessionResolver(state_dir).resolve("advisor-lead@kernel")

    assert ref.session_id == "01abc"
    assert ref.busy is True


def test_skips_dead_agent_pid(tmp_path: Path):
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    state_dir = tmp_path / "sessions"
    state_dir.mkdir()
    payload = {
        "tmuxSession": "advisor-lead@kernel",
        "transcriptPath": str(transcript),
        "agentPid": 2_000_000_000,
    }
    (state_dir / "01abc.json").write_text(json.dumps(payload), encoding="utf-8")

    try:
        AgentSessionResolver(state_dir).resolve("advisor-lead@kernel")
    except AgentSessionNotFound as exc:
        assert "no live Codex session" in str(exc)
    else:
        raise AssertionError("expected missing live session")

