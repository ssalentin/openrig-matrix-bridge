from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp


@dataclass(frozen=True)
class AgentSessionRef:
    session_id: str
    state_file: Path
    transcript_path: Path
    tmux_session: str
    tmux_pane: str | None
    agent_pid: int | None
    last_stop_time: float
    prompt_sequence: int
    completed_prompt_sequence: int

    @property
    def busy(self) -> bool:
        return self.prompt_sequence > self.completed_prompt_sequence


class AgentSessionNotFound(RuntimeError):
    pass


class GatewayError(RuntimeError):
    pass


def _pid_alive(pid: int | None) -> bool:
    if pid is None:
        return True
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


class AgentSessionResolver:
    def __init__(self, state_dir: Path):
        self.state_dir = state_dir

    def resolve(self, session_name: str) -> AgentSessionRef:
        candidates: list[AgentSessionRef] = []
        for state_file in self.state_dir.glob("*.json"):
            try:
                data = json.loads(state_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if data.get("tmuxSession") != session_name:
                continue
            transcript = data.get("transcriptPath")
            if not isinstance(transcript, str) or not Path(transcript).is_file():
                continue
            pid = data.get("agentPid")
            pid = int(pid) if isinstance(pid, int) else None
            if not _pid_alive(pid):
                continue
            candidates.append(
                AgentSessionRef(
                    session_id=state_file.stem,
                    state_file=state_file,
                    transcript_path=Path(transcript),
                    tmux_session=session_name,
                    tmux_pane=data.get("tmuxPane"),
                    agent_pid=pid,
                    last_stop_time=float(data.get("lastStopTime") or 0),
                    prompt_sequence=int(data.get("promptSequence") or 0),
                    completed_prompt_sequence=int(data.get("completedPromptSequence") or 0),
                )
            )
        if not candidates:
            raise AgentSessionNotFound(f"no live Codex session found for {session_name}")
        return max(
            candidates,
            key=lambda item: (item.last_stop_time, item.state_file.stat().st_mtime),
        )


class MoshiGateway:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    async def send_prompt(self, ref: AgentSessionRef, text: str) -> None:
        payload: dict[str, Any] = {
            "source": "codex",
            "sessionId": ref.session_id,
            "text": text,
        }
        timeout = aiohttp.ClientTimeout(total=30)
        async with (
            aiohttp.ClientSession(timeout=timeout) as session,
            session.post(f"{self.base_url}/v1/prompt", json=payload) as response,
        ):
            body = await response.text()
            if response.status != 200:
                raise GatewayError(
                    f"Moshi gateway returned HTTP {response.status}: {body[:500]}"
                )
            try:
                parsed = json.loads(body)
            except json.JSONDecodeError as exc:
                raise GatewayError("Moshi gateway returned invalid JSON") from exc
            if parsed.get("ok") is not True:
                raise GatewayError(f"Moshi gateway rejected prompt: {body[:500]}")

