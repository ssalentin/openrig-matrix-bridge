from __future__ import annotations

import asyncio
import json
import os
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class AgentSessionRef:
    session_id: str
    transcript_path: Path
    tmux_session: str
    busy: bool
    activity_state: str = "unknown"
    needs_input_count: int = 0
    # Wall-clock start of the inventory read this state comes from (#96).
    observed_at: float = 0.0


class AgentSessionNotFound(RuntimeError):
    pass


class GatewayError(RuntimeError):
    pass


class _InventoryRead:
    """One `rig ps` read that concurrent callers wait for together."""

    def __init__(self) -> None:
        self.done = threading.Event()
        self.observed_at = 0.0
        self.entries: list[dict[str, Any]] | None = None
        self.error: AgentSessionNotFound | None = None


class OpenRigSessionResolver:
    """Resolve live Codex sessions from the OpenRig node inventory.

    `rig ps --nodes -A --json --full` takes seconds on a large fleet (#96), so
    one parsed inventory is reused for `cache_seconds` and concurrent readers
    share a single in-flight read. An older inventory, up to `stale_seconds`,
    is still returned at once while one background read refreshes it; past
    that bound readers wait for the read. A failed read is never cached. With
    both bounds at 0 every read asks OpenRig, as before.
    """

    def __init__(
        self,
        command: str,
        codex_home: Path,
        timeout: int = 30,
        *,
        cache_seconds: float = 0.0,
        stale_seconds: float = 0.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.command = command
        self.codex_home = codex_home
        self.timeout = timeout
        self.cache_seconds = cache_seconds
        self.stale_seconds = max(stale_seconds, cache_seconds)
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: list[dict[str, Any]] | None = None
        self._read_at = 0.0
        self._observed_at = 0.0
        self._in_flight: _InventoryRead | None = None

    def _inventory(self) -> tuple[list[dict[str, Any]], float]:
        """The inventory entries and the wall-clock start of their read."""
        with self._lock:
            # Freshness counts from the start of the read the entries describe.
            age = (
                self._clock() - self._read_at
                if self._entries is not None
                else float("inf")
            )
            if age < self.cache_seconds:
                return self._entries, self._observed_at  # type: ignore[return-value]
            read = self._in_flight
            owner = read is None
            if owner:
                read = self._in_flight = _InventoryRead()
            if age < self.stale_seconds:
                if owner:
                    threading.Thread(
                        target=self._complete_read,
                        args=(read,),
                        name="openrig-inventory",
                        daemon=True,
                    ).start()
                return self._entries, self._observed_at  # type: ignore[return-value]
        assert read is not None
        if owner:
            self._complete_read(read)
        elif not read.done.wait(self.timeout + 5):
            raise AgentSessionNotFound("OpenRig node inventory read timed out")
        if read.error is not None:
            raise read.error
        assert read.entries is not None
        return read.entries, read.observed_at

    def _complete_read(self, read: _InventoryRead) -> None:
        """Run one read; whatever happens, release its waiters."""
        started = self._clock()
        read.observed_at = time.time()
        try:
            read.entries = self._read_inventory()
        except AgentSessionNotFound as exc:
            read.error = exc
        except Exception as exc:  # noqa: BLE001 - a read must never wedge readers
            read.error = AgentSessionNotFound(f"OpenRig node inventory failed: {exc}")
        finally:
            with self._lock:
                self._in_flight = None
                if read.entries is not None:
                    self._entries = read.entries
                    self._read_at = started
                    self._observed_at = read.observed_at
            read.done.set()

    def _read_inventory(self) -> list[dict[str, Any]]:
        command = [self.command, "ps", "--nodes", "-A", "--json", "--full"]
        try:
            result = subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except (
            OSError,
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
        ) as exc:
            raise AgentSessionNotFound(f"OpenRig node inventory failed: {exc}") from exc
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise AgentSessionNotFound(
                "OpenRig node inventory returned invalid JSON"
            ) from exc
        entries = payload.get("entries", []) if isinstance(payload, dict) else payload
        if not isinstance(entries, list):
            raise AgentSessionNotFound("OpenRig node inventory has an invalid shape")
        return [entry for entry in entries if isinstance(entry, dict)]

    def resolve_many(
        self, sessions: set[str] | None = None
    ) -> dict[str, AgentSessionRef]:
        resolved: dict[str, AgentSessionRef] = {}
        entries, observed_at = self._inventory()
        for entry in entries:
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
            activity_state, needs_input_count = self._safe_activity(entry)
            resolved[session_name] = AgentSessionRef(
                session_id=session_id,
                transcript_path=transcript,
                tmux_session=session_name,
                busy=activity_state == "working",
                activity_state=activity_state,
                needs_input_count=needs_input_count,
                observed_at=observed_at,
            )
        return resolved

    @staticmethod
    def _safe_activity(entry: dict[str, Any]) -> tuple[str, int]:
        activity = entry.get("activityState")
        if isinstance(activity, dict):
            state = activity.get("activity")
            needs_input = activity.get("needsInput")
            count = needs_input.get("count") if isinstance(needs_input, dict) else 0
            if isinstance(state, str) and state in {
                "working",
                "idle-at-prompt",
                "unknown",
            }:
                safe_count = (
                    count
                    if isinstance(count, int) and not isinstance(count, bool)
                    and count > 0
                    else 0
                )
                return state, safe_count

        legacy = entry.get("agentActivity")
        state = legacy.get("state") if isinstance(legacy, dict) else None
        if state == "running":
            return "working", 0
        if state == "idle":
            return "idle-at-prompt", 0
        if state == "needs_input":
            return "idle-at-prompt", 1
        return "unknown", 0

    def resolve(self, session_name: str) -> AgentSessionRef:
        resolved = self.resolve_many({session_name})
        reference = resolved.get(session_name)
        if reference is None:
            raise AgentSessionNotFound(
                f"no live Codex session found for {session_name}"
            )
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

    async def queue_transitions(self, qitem_id: str) -> list[dict[str, Any]]:
        payload = await self._run_json(
            "queue",
            "transitions",
            qitem_id,
            "--full",
            "--json",
        )
        entries = payload.get("transitions", []) if isinstance(payload, dict) else payload
        if not isinstance(entries, list):
            raise GatewayError("OpenRig queue transitions returned an invalid shape")
        return [entry for entry in entries if isinstance(entry, dict)]

    async def record_human_delivery(
        self,
        qitem_id: str,
        message_ts: str,
        *,
        close_update: bool,
    ) -> None:
        """Record the owner-delivery receipt the OpenRig Slack subsystem owns.

        Matrix is the active human transport.  The OpenRig gateway schema still
        calls that connector kind "slack", so the bridge must write the same
        episode-keyed receipt after a successful Matrix post.  Otherwise the row
        is reported as never-posted even though the user received it.
        """
        transitions = await self.queue_transitions(qitem_id)
        owner = next(
            (
                transition
                for transition in reversed(transitions)
                if transition.get("state") == "pending"
                and transition.get("ownerNotificationKind")
                in {"human-required", "human-update"}
            ),
            None,
        )
        if owner is None:
            raise GatewayError(
                f"no current human notification transition for {qitem_id}"
            )
        transition_id = owner.get("transitionId")
        if not isinstance(transition_id, int):
            raise GatewayError(
                f"human notification transition for {qitem_id} has no integer id"
            )
        notification_key = f"{qitem_id}:{transition_id}"
        note_prefix = "slack-owner-notification-posted"
        already_posted = any(
            str(transition.get("transitionNote") or "").startswith(f"{note_prefix} ")
            and f"notification_key={notification_key}"
            in str(transition.get("transitionNote") or "").split()
            for transition in transitions
        )
        if already_posted:
            return

        args = ["queue", "update", qitem_id]
        if close_update:
            args.extend(
                ["--state", "done", "--closure-reason", "no-follow-on"]
            )
        args.extend(
            [
                "--note",
                " ".join(
                    [
                        note_prefix,
                        f"notification_key={notification_key}",
                        f"level={owner.get('ownerNotificationLevel') or 'RECORD'}",
                        f"kind={owner.get('ownerNotificationKind')}",
                        f"message_ts={message_ts}",
                        f"thread_ts={message_ts}",
                    ]
                ),
                "--json",
            ]
        )
        environment = os.environ.copy()
        environment["OPENRIG_SESSION_NAME"] = self.sender_session
        payload = await self._run_json(*args, environment=environment)
        if not isinstance(payload, dict):
            raise GatewayError("OpenRig queue update returned an invalid shape")

    async def queue_create(
        self,
        *,
        source_session: str,
        destination_session: str,
        summary: str,
        body: str,
        priority: str,
    ) -> str:
        environment = os.environ.copy()
        environment.pop("OPENRIG_SESSION_NAME", None)
        payload = await self._run_json(
            "queue",
            "create",
            "--source",
            source_session,
            "--destination",
            destination_session,
            "--summary",
            summary,
            "--body",
            body,
            "--priority",
            priority,
            "--tags",
            "matrix,inbound",
            "--no-nudge",
            "--json",
            environment=environment,
        )
        if not isinstance(payload, dict):
            raise GatewayError("OpenRig queue create returned an invalid shape")
        qitem_id = payload.get("qitemId")
        if not isinstance(qitem_id, str) or not qitem_id:
            raise GatewayError("OpenRig queue create did not return a qitem id")
        return qitem_id

    async def queue_claim(self, qitem_id: str, destination_session: str) -> None:
        environment = os.environ.copy()
        environment["OPENRIG_SESSION_NAME"] = destination_session
        payload = await self._run_json(
            "queue",
            "claim",
            qitem_id,
            "--json",
            environment=environment,
        )
        if not isinstance(payload, dict):
            raise GatewayError("OpenRig queue claim returned an invalid shape")

    async def queue_finish(
        self,
        qitem_id: str,
        destination_session: str,
        resolution: str,
    ) -> None:
        environment = os.environ.copy()
        environment["OPENRIG_SESSION_NAME"] = destination_session
        payload = await self._run_json(
            "queue",
            "update",
            qitem_id,
            "--state",
            "done",
            "--closure-reason",
            "no-follow-on",
            "--resolution",
            resolution,
            "--note",
            "Matrix answer delivered by openrig-matrix-bridge",
            "--json",
            environment=environment,
        )
        if not isinstance(payload, dict):
            raise GatewayError("OpenRig queue update returned an invalid shape")

    async def queue_fail(
        self,
        qitem_id: str,
        destination_session: str,
        resolution: str,
    ) -> None:
        environment = os.environ.copy()
        environment["OPENRIG_SESSION_NAME"] = destination_session
        payload = await self._run_json(
            "queue",
            "update",
            qitem_id,
            "--state",
            "failed",
            "--resolution",
            resolution,
            "--note",
            "Matrix request failed in openrig-matrix-bridge",
            "--json",
            environment=environment,
        )
        if not isinstance(payload, dict):
            raise GatewayError("OpenRig queue update returned an invalid shape")

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

    async def steer_prompt(self, ref: AgentSessionRef, text: str) -> None:
        """Best-effort pane injection into an active seat.

        OpenRig currently exposes no turn-identity or safe-boundary response for
        this operation. Callers must confirm admission from the transcript and
        retain a durable fallback until they do.
        """
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
