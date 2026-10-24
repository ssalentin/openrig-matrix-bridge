from __future__ import annotations

import asyncio
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class AgentSessionRef:
    session_id: str
    transcript_path: Path
    tmux_session: str
    busy: bool
    waiting_for_input: bool = False


class AgentSessionNotFound(RuntimeError):
    pass


class GatewayError(RuntimeError):
    pass


class OpenRigSessionResolver:
    def __init__(self, command: str, codex_home: Path, timeout: int = 30):
        self.command = command
        self.codex_home = codex_home
        self.timeout = timeout

    def resolve_many(self, sessions: set[str] | None = None) -> dict[str, AgentSessionRef]:
        command = [self.command, "ps", "--nodes", "-A", "--json", "--full"]
        try:
            result = subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise AgentSessionNotFound(f"OpenRig node inventory failed: {exc}") from exc
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise AgentSessionNotFound("OpenRig node inventory returned invalid JSON") from exc
        entries = payload.get("entries", []) if isinstance(payload, dict) else payload
        if not isinstance(entries, list):
            raise AgentSessionNotFound("OpenRig node inventory has an invalid shape")

        resolved: dict[str, AgentSessionRef] = {}
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            session_name = entry.get("canonicalSessionName")
            if not isinstance(session_name, str):
                continue
            if sessions is not None and session_name not in sessions:
                continue
            if entry.get("sessionStatus") != "running":
                continue
            if entry.get("runtime") != "codex":
                continue
            session_id = entry.get("resumeToken")
            if not isinstance(session_id, str) or not session_id:
                continue
            transcript = self._transcript_path(entry, session_id)
            if transcript is None:
                continue
            activity = entry.get("agentActivity")
            activity_state = (
                activity.get("state") if isinstance(activity, dict) else None
            )
            runtime_activity = entry.get("activityState")
            needs_input = (
                runtime_activity.get("needsInput")
                if isinstance(runtime_activity, dict)
                else None
            )
            waiting_for_input = bool(
                isinstance(needs_input, dict) and int(needs_input.get("count") or 0) > 0
            )
            resolved[session_name] = AgentSessionRef(
                session_id=session_id,
                transcript_path=transcript,
                tmux_session=session_name,
                busy=activity_state == "running",
                waiting_for_input=waiting_for_input,
            )
        return resolved

    def resolve(self, session_name: str) -> AgentSessionRef:
        resolved = self.resolve_many({session_name})
        reference = resolved.get(session_name)
        if reference is None:
            raise AgentSessionNotFound(f"no live Codex session found for {session_name}")
        return reference

    def _transcript_path(
        self,
        entry: dict[str, Any],
        session_id: str,
    ) -> Path | None:
        usage = entry.get("contextUsage")
        transcript = usage.get("transcriptPath") if isinstance(usage, dict) else None
        if isinstance(transcript, str):
            path = Path(transcript)
            if path.is_file():
                return path
        sessions_root = self.codex_home / "sessions"
        matches = sorted(
            sessions_root.rglob(f"*{session_id}*.jsonl"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        return matches[0] if matches else None


class OpenRigGateway:
    def __init__(
        self,
        command: str,
        sender_session: str,
        timeout: int = 60,
    ):
        self.command = command
        self.sender_session = sender_session
        self.timeout = timeout

    async def _run_json(
        self,
        *args: str,
        environment: dict[str, str] | None = None,
    ) -> Any:
        process = await asyncio.create_subprocess_exec(
            self.command,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=environment,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=self.timeout,
            )
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            raise GatewayError("OpenRig command timed out") from exc
        if process.returncode != 0:
            detail = stderr.decode("utf-8", "replace").strip()
            output = stdout.decode("utf-8", "replace").strip()
            raise GatewayError(
                f"OpenRig command failed: {detail or output or process.returncode}"
            )
        try:
            return json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise GatewayError("OpenRig command returned invalid JSON") from exc

    async def queue_list(self) -> list[dict[str, Any]]:
        payload = await self._run_json("queue", "list", "--json")
        entries = payload.get("items", []) if isinstance(payload, dict) else payload
        if not isinstance(entries, list):
            raise GatewayError("OpenRig queue list returned an invalid shape")
        return [item for item in entries if isinstance(item, dict)]

    async def queue_show(self, qitem_id: str) -> dict[str, Any]:
        payload = await self._run_json(
            "queue",
            "show",
            qitem_id,
            "--full",
            "--json",
        )
        if not isinstance(payload, dict):
            raise GatewayError("OpenRig queue show returned an invalid shape")
        return payload

    async def send_prompt(self, ref: AgentSessionRef, text: str) -> None:
        environment = os.environ.copy()
        environment["OPENRIG_SESSION_NAME"] = self.sender_session
        await self._run_json(
            "send",
            ref.tmux_session,
            text,
            "--raw",
            "--json",
            environment=environment,
        )
