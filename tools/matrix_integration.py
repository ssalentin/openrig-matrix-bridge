#!/usr/bin/env python3
"""Run executable Matrix/OpenRig integration checks against ephemeral Synapse."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import os
import re
import shutil
import signal
import socket
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import aiohttp
import yaml
from nio import (
    AsyncClient,
    AsyncClientConfig,
    InviteMemberEvent,
    ProfileGetResponse,
    ReactionEvent,
    RoomEncryptedAudio,
    RoomEncryptedFile,
    RoomEncryptedImage,
    RoomGetStateEventResponse,
    RoomMessageAudio,
    RoomMessageFile,
    RoomMessageImage,
    RoomMessageText,
    RoomSendResponse,
    TypingNoticeEvent,
    UnknownEvent,
)
from nio.crypto import decrypt_attachment

from openrig_matrix_bridge import __version__

ROOT = Path(__file__).resolve().parents[1]
SYNAPSE_IMAGE = (
    "matrixdotorg/synapse@sha256:"
    "390f5e67227378eb87c84627b57f0582cdb235fcc8159d89f371f37bf7cccfd7"
)
REPORT_SCHEMA_VERSION = 1
# Steering probes need the bridge's resolve -> claim -> steer chain (three
# fake-CLI subprocesses, about 2 s on a loaded host) to land inside the root
# turn; ordinary "hold first" prompts keep the fake's 3.2 s default.
STEERING_PROBE_HOLD_SECONDS = 8.0
REGISTRATION_SECRET = "openrig-integration-only"
BRIDGE_USER = "@bridge:localhost"
OWNER_USER = "@owner:localhost"
BRIDGE_PASSWORD = "bridge-integration-password"
OWNER_PASSWORD = "owner-integration-password"
STRANGER_USER = "@stranger:localhost"
STRANGER_PASSWORD = "stranger-integration-password"
ACCEPTANCE_REACTION_KEY = "👍"
SESSION = "integration-agent@local"
AGENT_KEY = "integration-agent"
# The bridge's default openrig.burst_inactivity_seconds.
RELATED_BOUNDARY_SECONDS = 45
# Related checks send many events between owner syncs; a wide timeline keeps
# every status change visible.
RELATED_TIMELINE_LIMIT = 500
# Check order matters: later checks build on room and steering state.
CHECKS = (
    "room",
    "chat_and_queue",
    "threaded_reply",
    "qitem_claim_crash_recovery",
    "answer_crash_recovery",
    "activity_card_crash_recovery",
    "fifo",
    "steering_modes",
    "steering_fallback_and_restart",
    "reaction",
    "acceptance_reaction",
    "acceptance_reaction_default_off",
    "images",
    "files",
    "threaded_files",
    "gallery",
    "gallery_restart",
    "audio_gap",
    "voice_status_edit",
    "decision_and_poll",
    "proactive_message",
    "related_gallery_text",
    "related_voice_text_attachments",
    "related_galleries_and_files",
    "related_restart",
    "related_unrelated_isolation",
    "related_thread",
    "restart",
    "doctor_failure",
    "room_rejection",
)


class CheckFailure(RuntimeError):
    pass


def validate_answer_relation(raw_answer: dict[str, Any], prompt_event_id: str) -> None:
    relation = raw_answer.get("content", {}).get("m.relates_to")
    if not isinstance(relation, dict) or relation.get("rel_type") != "m.thread":
        raise CheckFailure("final answer is not a Matrix thread relation")
    if relation.get("event_id") != prompt_event_id:
        raise CheckFailure("final answer thread does not reference the owner prompt")
    fallback = relation.get("m.in_reply_to")
    if not isinstance(fallback, dict) or fallback.get("event_id") != prompt_event_id:
        raise CheckFailure("final answer lacks the Matrix in-reply-to fallback")


LATENCY_METHOD = (
    "Per source event, the bridge persists time.time() at prompt intake (entry "
    "into prompt enqueue, after sender, age and media checks), after the "
    "OpenRig pane-input command returned (injection), when it first observed "
    "the matching transcript user record (admission), and when Matrix accepted "
    "its acceptance reaction. Values are milliseconds from intake, grouped by "
    "kind: prompt (normal turn), steer (same-turn steering), late_steer "
    "(steering input materialized as a later turn) and fallback (batch sent as "
    "one normal turn)."
)
LATENCY_BOUNDARY = (
    "Deterministic fake OpenRig subprocess transport and loopback Synapse; "
    "not live Codex. Admission includes up to one bridge transcript poll "
    "interval. Samples include crash, restart and deliberately delayed probes, "
    "so maxima bound those paths rather than an idle seat; late_steer includes "
    "the fake transport's intentional materialization delay."
)


def summarize_latency(samples: object) -> dict[str, Any]:
    stages = (
        ("intake_to_injection_ms", "injected_at"),
        ("intake_to_admission_ms", "admitted_at"),
        ("intake_to_acknowledgement_ms", "acknowledged_at"),
    )
    values: dict[str, dict[str, list[float]]] = {}
    for sample in samples if isinstance(samples, list) else []:
        if not isinstance(sample, dict):
            continue
        intake = sample.get("intake_at")
        if not isinstance(intake, int | float):
            continue
        kind = values.setdefault(
            str(sample.get("kind") or "unknown"),
            {name: [] for name, _field in stages},
        )
        for name, field in stages:
            moment = sample.get(field)
            if isinstance(moment, int | float):
                kind[name].append((float(moment) - float(intake)) * 1000)
    by_kind = {
        kind: {
            name: (
                {
                    "count": len(series),
                    "median": round(statistics.median(series), 1),
                    "max": round(max(series), 1),
                }
                if series
                else {"count": 0}
            )
            for name, series in stages_values.items()
        }
        for kind, stages_values in sorted(values.items())
    }
    return {
        "method": LATENCY_METHOD,
        "boundary": LATENCY_BOUNDARY,
        "by_kind": by_kind,
    }


def validate_qitem_history(item: dict[str, Any]) -> None:
    history = item.get("history")
    if not isinstance(history, list):
        raise CheckFailure("qitem history must contain pending -> in-progress -> done")
    states = [entry.get("state") for entry in history if isinstance(entry, dict)]
    if states != ["pending", "in-progress", "done"]:
        raise CheckFailure(
            f"qitem history is not pending -> in-progress -> done: {states}"
        )
    timestamps = [entry.get("at") for entry in history]
    if not all(isinstance(value, (int, float)) for value in timestamps):
        raise CheckFailure(f"qitem history has invalid timestamps: {timestamps}")
    if timestamps != sorted(timestamps):
        raise CheckFailure(f"qitem history timestamps are out of order: {timestamps}")


def feedback_window(
    timeline: list[dict[str, Any]],
    start: int,
    end_body: str,
    *,
    include_concurrent: bool = False,
) -> list[dict[str, Any]]:
    """Return timeline entries from `start` up to the bridge message `end_body`.

    With `include_concurrent`, typing and status entries delivered in the same
    sync batch as that message count as concurrent with it and are included;
    use that when checking that feedback appeared, not that it never stopped.
    """
    for index in range(start, len(timeline)):
        entry = timeline[index]
        if (
            entry["kind"] == "message"
            and entry["sender"] == BRIDGE_USER
            and end_body in str(entry["body"])
        ):
            same_batch = [
                later
                for later in timeline[index + 1 :]
                if include_concurrent
                and later["batch"] == entry["batch"]
                and later["kind"] != "message"
            ]
            return timeline[start:index] + same_batch
    raise CheckFailure(f"bridge message {end_body!r} not in the owner timeline")


def bridge_message_index(timeline: list[dict[str, Any]], body: str) -> int:
    for index, entry in enumerate(timeline):
        if (
            entry["kind"] == "message"
            and entry["sender"] == BRIDGE_USER
            and body in str(entry["body"])
        ):
            return index
    raise CheckFailure(f"bridge message {body!r} not in the owner timeline")


def require_continuous_feedback(window: list[dict[str, Any]], label: str) -> None:
    """Fail if the room went ready or the bridge stopped typing inside `window`."""
    for entry in window:
        if entry["kind"] == "status" and entry["state"] == "ready":
            raise CheckFailure(f"{label}: agent status turned ready mid-work")
        if entry["kind"] == "typing" and not entry["bridge"]:
            raise CheckFailure(f"{label}: bridge typing stopped mid-work")


def validate_fifo_records(
    records: list[dict[str, Any]],
    first_prompt: str,
    second_prompt: str,
) -> None:
    first = next((item for item in records if item.get("prompt") == first_prompt), None)
    second = next(
        (item for item in records if item.get("prompt") == second_prompt), None
    )
    if first is None or second is None:
        raise CheckFailure("FIFO evidence is missing one of the prompts")
    completed_at = first.get("completed_at")
    second_sent_at = second.get("sent_at")
    if not isinstance(completed_at, (int, float)) or not isinstance(
        second_sent_at, (int, float)
    ):
        raise CheckFailure("FIFO evidence has incomplete send/completion timestamps")
    if second_sent_at <= completed_at:
        raise CheckFailure(
            "second prompt was sent before the first prompt completed: "
            f"{second_sent_at} <= {completed_at}"
        )


def thread_relation(root_event_id: str) -> dict[str, Any]:
    """An Element-style thread reply to `root_event_id`."""
    return {
        "rel_type": "m.thread",
        "event_id": root_event_id,
        "is_falling_back": True,
        "m.in_reply_to": {"event_id": root_event_id},
    }


def agent_inputs(fake: dict[str, Any]) -> list[dict[str, Any]]:
    """Every prompt and steering input the fake transport received, in send order."""
    inputs = [
        {"kind": "prompt", **item} for item in fake.get("prompts", [])
    ] + [{"kind": "steer", **item} for item in fake.get("steers", [])]
    return sorted(inputs, key=lambda item: float(item.get("sent_at") or 0))


def verify_attachment_digests(prompt: str) -> int:
    """Each `Lokale Datei:` path must be a non-empty file matching its SHA-256 line."""
    blocks = prompt.split("Lokale Datei: ")[1:]
    for block in blocks:
        path = Path(block.split()[0])
        match = re.search(r"SHA-256: ([0-9a-f]{64})", block.split("Lokale Datei: ")[0])
        if match is None:
            raise CheckFailure(f"agent input names {path} without a SHA-256 digest")
        data = path.read_bytes() if path.is_file() else b""
        if not data:
            raise CheckFailure(f"agent input names a missing or empty file: {path}")
        if hashlib.sha256(data).hexdigest() != match.group(1):
            raise CheckFailure(f"SHA-256 of {path} does not match the agent input")
    return len(blocks)


def validate_linked_inputs(
    inputs: list[dict[str, Any]],
    anchor_event_id: str,
    markers: list[str],
) -> list[str]:
    """Check that related parts reach the agent completely, in order and linked.

    `markers` are unique strings of each part in source order; the first part
    is the anchor. Returns the delivery kind ("prompt" or "steer") that first
    carried each part.
    """
    kinds: list[str] = []
    for index, marker in enumerate(markers):
        carrying = [item for item in inputs if marker in str(item["prompt"])]
        if not carrying:
            raise CheckFailure(f"part {index + 1} never reached the agent")
        if index and not all(
            anchor_event_id in str(item["prompt"]) or markers[0] in str(item["prompt"])
            for item in carrying
        ):
            raise CheckFailure(
                f"part {index + 1} reached the agent without a link to the first part"
            )
        # A part may be restated later only together with a newer part.
        for item in carrying[1:]:
            if not any(later in str(item["prompt"]) for later in markers[index + 1 :]):
                raise CheckFailure(f"part {index + 1} reached the agent twice as new")
        kinds.append(str(carrying[0]["kind"]))
    for item in inputs:
        prompt = str(item["prompt"])
        # Event ids also appear in a leading source list; each part's own block
        # is its last occurrence.
        positions = [prompt.rindex(marker) for marker in markers if marker in prompt]
        if positions != sorted(positions):
            raise CheckFailure("related parts reached the agent out of source order")
    return kinds


def validate_source_provenance(
    head_commit: str,
    tracked_status: str,
    expected_commit: str | None,
) -> dict[str, Any]:
    commit = (expected_commit or head_commit).strip()
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise CheckFailure(f"source commit is unknown or invalid: {commit!r}")
    if head_commit.strip() != commit:
        raise CheckFailure(
            f"source commit {commit} does not match checked-out HEAD {head_commit}"
        )
    if tracked_status.strip():
        raise CheckFailure(f"source worktree is dirty: {tracked_status.strip()[:500]}")
    return {
        "commit": commit,
        "source": "git",
        "worktree_clean": True,
    }


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def run(
    args: list[str],
    *,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
    timeout: int = 120,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        check=check,
        cwd=cwd or ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def docker(args: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return run(["docker", *args], timeout=timeout)


def wait_for_json_state(
    path: Path,
    predicate: Callable[[dict[str, Any]], bool],
    timeout: float,
    description: str | None = None,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        try:
            last = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            time.sleep(0.1)
            continue
        if predicate(last):
            return last
        time.sleep(0.1)
    detail = description or "state condition"
    raise CheckFailure(f"{detail} was not met within {timeout}s: {path}")


class MatrixOwner:
    def __init__(
        self,
        homeserver: str,
        token: str,
        device_id: str,
        store: Path,
    ):
        store.mkdir(parents=True)
        self.client = AsyncClient(
            homeserver,
            OWNER_USER,
            device_id=device_id,
            store_path=str(store),
            config=AsyncClientConfig(
                encryption_enabled=True,
                store_sync_tokens=True,
                store_name="integration-store",
            ),
        )
        self.client.restore_login(OWNER_USER, device_id, token)
        self.homeserver = homeserver
        self.token = token
        self.messages: list[dict[str, Any]] = []
        self.images: list[RoomEncryptedImage | RoomMessageImage] = []
        self.files: list[RoomEncryptedFile | RoomMessageFile] = []
        self.audio: list[RoomEncryptedAudio | RoomMessageAudio] = []
        self.reactions: list[ReactionEvent] = []
        self.typing: list[dict[str, Any]] = []
        self.unknown: list[UnknownEvent] = []
        # Messages, typing changes and agent-status updates in arrival order.
        # Within one sync batch nio delivers timeline events before ephemeral
        # typing, so a typing change is never placed before a message that
        # arrived with it.
        self.timeline: list[dict[str, Any]] = []
        self.sync_batch = 0
        # When the owner last sent an event, so related-context checks can
        # start beyond the bridge's inactivity boundary.
        self.last_sent_at = 0.0
        self.client.add_event_callback(self._on_message, RoomMessageText)
        self.client.add_event_callback(
            self._on_image,
            (RoomEncryptedImage, RoomMessageImage),
        )
        self.client.add_event_callback(
            self._on_file,
            (RoomEncryptedFile, RoomMessageFile),
        )
        self.client.add_event_callback(
            self._on_audio,
            (RoomEncryptedAudio, RoomMessageAudio),
        )
        self.client.add_event_callback(self._on_reaction, ReactionEvent)
        self.client.add_ephemeral_callback(self._on_typing, TypingNoticeEvent)
        self.client.add_event_callback(self._on_unknown, UnknownEvent)
        self.client.add_event_callback(self._on_invite, InviteMemberEvent)

    async def start(self) -> None:
        if self.client.should_upload_keys:
            response = await self.client.keys_upload()
            if hasattr(response, "status_code") and response.status_code >= 400:
                raise CheckFailure(f"owner key upload failed: {response}")

    async def _on_message(self, room: Any, event: RoomMessageText) -> None:
        self.timeline.append(
            {
                "kind": "message",
                "sender": event.sender,
                "body": event.body,
                "batch": self.sync_batch,
            }
        )
        self.messages.append(
            {
                "room_id": room.room_id,
                "event_id": event.event_id,
                "sender": event.sender,
                "body": event.body,
                "source": event.source,
            }
        )

    async def _on_image(
        self,
        room: Any,
        event: RoomEncryptedImage | RoomMessageImage,
    ) -> None:
        self.images.append(event)

    async def _on_file(
        self,
        room: Any,
        event: RoomEncryptedFile | RoomMessageFile,
    ) -> None:
        self.files.append(event)

    async def _on_audio(
        self,
        room: Any,
        event: RoomEncryptedAudio | RoomMessageAudio,
    ) -> None:
        self.audio.append(event)

    async def _on_reaction(self, room: Any, event: ReactionEvent) -> None:
        self.reactions.append(event)

    async def _on_typing(self, room: Any, event: TypingNoticeEvent) -> None:
        self.typing.append({"room_id": room.room_id, "users": list(event.users)})
        self.timeline.append(
            {
                "kind": "typing",
                "bridge": BRIDGE_USER in event.users,
                "batch": self.sync_batch,
            }
        )

    async def _on_unknown(self, room: Any, event: UnknownEvent) -> None:
        self.unknown.append(event)
        if event.type == "dev.openrig.agent_status":
            content = event.source.get("content", {})
            self.timeline.append(
                {
                    "kind": "status",
                    "state": content.get("state"),
                    "batch": self.sync_batch,
                }
            )

    async def _on_invite(self, room: Any, event: InviteMemberEvent) -> None:
        response = await self.client.join(room.room_id)
        if hasattr(response, "status_code") and response.status_code >= 400:
            raise CheckFailure(f"owner could not join invited room: {response}")

    async def sync_once(
        self,
        timeout_ms: int = 5000,
        *,
        timeline_limit: int | None = None,
    ) -> None:
        """Sync once; `timeline_limit` widens the per-room timeline so that a
        long gap since the previous sync drops no events."""
        self.sync_batch += 1
        sync_filter = (
            {"room": {"timeline": {"limit": timeline_limit}}}
            if timeline_limit
            else None
        )
        response = await self.client.sync(
            timeout=timeout_ms,
            full_state=True,
            sync_filter=sync_filter,
        )
        if hasattr(response, "status_code") and response.status_code >= 400:
            raise CheckFailure(f"owner sync failed: {response}")
        for room_id in list(self.client.invited_rooms):
            await self.client.join(room_id)

    async def sync_until(
        self,
        predicate: Callable[[], bool],
        timeout: float,
        *,
        description: str,
    ) -> None:
        deadline = time.monotonic() + timeout
        syncs = 0
        while time.monotonic() < deadline:
            await self.sync_once()
            syncs += 1
            if predicate():
                return
            await asyncio.sleep(0.2)
        recent = [
            {
                "event_id": message["event_id"],
                "sender": message["sender"],
                "body": str(message["body"])[:160],
            }
            for message in self.messages[-5:]
        ]
        raise CheckFailure(
            f"timed out waiting for {description} after {syncs} syncs; "
            f"owner_messages={len(self.messages)} recent={recent}"
        )

    async def send_text(
        self,
        room_id: str,
        body: str,
        *,
        relates_to: dict[str, Any] | None = None,
    ) -> str:
        content: dict[str, Any] = {"msgtype": "m.text", "body": body}
        if relates_to:
            content["m.relates_to"] = relates_to
        response = await self.client.room_send(
            room_id,
            "m.room.message",
            content,
            ignore_unverified_devices=True,
        )
        if not isinstance(response, RoomSendResponse):
            raise CheckFailure(f"owner text send failed: {response}")
        self.last_sent_at = time.monotonic()
        return response.event_id

    async def send_encrypted_file(
        self,
        room_id: str,
        data: bytes,
        *,
        msgtype: str,
        filename: str,
        mimetype: str,
        body: str,
        extra_content: dict[str, Any] | None = None,
    ) -> str:
        content = {
            "msgtype": msgtype,
            "body": body,
            "filename": filename,
            "info": {"mimetype": mimetype, "size": len(data)},
            "file": await self.upload_encrypted(
                data,
                filename=filename,
                mimetype=mimetype,
            ),
        }
        if extra_content:
            content.update(extra_content)
        response = await self.client.room_send(
            room_id,
            "m.room.message",
            content,
            ignore_unverified_devices=True,
        )
        if not isinstance(response, RoomSendResponse):
            raise CheckFailure(f"owner encrypted file send failed: {response}")
        self.last_sent_at = time.monotonic()
        return response.event_id

    async def upload_encrypted(
        self,
        data: bytes,
        *,
        filename: str,
        mimetype: str,
    ) -> dict[str, Any]:
        upload, encryption = await self.client.upload(
            io.BytesIO(data),
            content_type=mimetype,
            filename=filename,
            encrypt=True,
            filesize=len(data),
        )
        if not hasattr(upload, "content_uri") or not isinstance(encryption, dict):
            raise CheckFailure(f"owner encrypted upload failed: {upload}")
        return {"url": upload.content_uri, **encryption}

    async def send_gallery(
        self,
        room_id: str,
        items: list[dict[str, Any]],
        *,
        body: str,
        relates_to: dict[str, Any] | None = None,
    ) -> str:
        """Send one Element X style dm.filament.gallery event."""
        content: dict[str, Any] = {
            "msgtype": "dm.filament.gallery",
            "body": body,
            "itemtypes": items,
        }
        if relates_to:
            content["m.relates_to"] = relates_to
        response = await self.client.room_send(
            room_id,
            "m.room.message",
            content,
            ignore_unverified_devices=True,
        )
        if not isinstance(response, RoomSendResponse):
            raise CheckFailure(f"owner gallery send failed: {response}")
        self.last_sent_at = time.monotonic()
        return response.event_id

    async def download_media(self, event: Any) -> bytes:
        response = await self.client.download(mxc=event.url)
        body = getattr(response, "body", None)
        if not isinstance(body, bytes):
            raise CheckFailure(f"owner media download failed: {response}")
        if isinstance(
            event,
            (RoomEncryptedFile, RoomEncryptedImage, RoomEncryptedAudio),
        ):
            return decrypt_attachment(
                body,
                event.key["k"],
                event.hashes["sha256"],
                event.iv,
            )
        return body

    async def raw_event(self, room_id: str, event_id: str) -> dict[str, Any]:
        room = quote(room_id, safe="")
        event = quote(event_id, safe="")
        async with (
            aiohttp.ClientSession() as session,
            session.get(
                f"{self.homeserver}/_matrix/client/v3/rooms/{room}/event/{event}",
                headers={"Authorization": f"Bearer {self.token}"},
            ) as response,
        ):
            value = await response.json()
            if response.status != 200:
                raise CheckFailure(f"could not read raw Matrix event: {value}")
        return value

    async def close(self) -> None:
        await self.client.close()


class IntegrationRun:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.started_at = time.monotonic()
        self.work = Path(tempfile.mkdtemp(prefix="openrig-matrix-integration."))
        self.report_path = args.report.resolve()
        self.container = f"openrig-matrix-integration-{uuid.uuid4().hex[:10]}"
        self.port = free_port()
        self.homeserver = f"http://127.0.0.1:{self.port}"
        self.state_dir = self.work / "bridge-state"
        self.fake_state = self.work / "fake-openrig"
        self.config_path = self.work / "config.yaml"
        self.log_path = self.work / "bridge.log"
        self.bridge: subprocess.Popen[str] | None = None
        self.bridge_started_ms = 0
        self.owner: MatrixOwner | None = None
        self.bridge_token = ""
        self.results: list[dict[str, Any]] = []
        self.source_provenance: dict[str, Any]
        self.source_provenance_error: str | None = None
        try:
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            tracked_status = subprocess.run(
                ["git", "status", "--porcelain", "--untracked-files=no"],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            self.source_provenance = validate_source_provenance(
                head,
                tracked_status,
                args.source_commit,
            )
        except (OSError, subprocess.CalledProcessError, CheckFailure) as exc:
            self.source_provenance = {
                "commit": args.source_commit or "unknown",
                "source": "git",
                "worktree_clean": False,
            }
            self.source_provenance_error = str(exc)

    def record(
        self,
        check_id: str,
        status: str,
        evidence: str,
        *,
        automated: bool = True,
        details: dict[str, Any] | None = None,
    ) -> None:
        item: dict[str, Any] = {
            "id": check_id,
            "status": status,
            "automated": automated,
            "evidence": evidence,
        }
        if details:
            item["details"] = details
        self.results.append(item)

    def env(self, *, owner_token: str | None = None) -> dict[str, str]:
        environment = {
            **os.environ,
            "OPENRIG_FAKE_STATE": str(self.fake_state),
            "PYTHONUNBUFFERED": "1",
        }
        token = owner_token if owner_token is not None else self.bridge_token
        environment["MATRIX_ACCESS_TOKEN"] = token
        return environment

    def cli(
        self,
        *args: str,
        token: str | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        environment = self.env(owner_token=token)
        return run(
            [
                sys.executable,
                "-m",
                "openrig_matrix_bridge",
                *args,
                "--config",
                str(self.config_path),
            ],
            env=environment,
            check=check,
        )

    async def http_login(
        self, user: str, password: str, device: str
    ) -> tuple[str, str]:
        payload = {
            "type": "m.login.password",
            "identifier": {"type": "m.id.user", "user": user},
            "password": password,
            "initial_device_display_name": "OpenRig integration",
            "device_id": device,
        }
        async with (
            aiohttp.ClientSession() as session,
            session.post(
                f"{self.homeserver}/_matrix/client/v3/login",
                json=payload,
            ) as response,
        ):
            value = await response.json()
            if response.status != 200:
                raise CheckFailure(f"Matrix login for {user} failed: {value}")
        return str(value["access_token"]), str(value["device_id"])

    async def wait_for_homeserver(self) -> None:
        deadline = time.monotonic() + self.args.timeout
        async with aiohttp.ClientSession() as session:
            while time.monotonic() < deadline:
                try:
                    async with session.get(
                        f"{self.homeserver}/_matrix/client/versions"
                    ) as response:
                        if response.status == 200:
                            return
                except aiohttp.ClientError:
                    pass
                await asyncio.sleep(0.25)
        raise CheckFailure("ephemeral Synapse did not become ready")

    async def start_synapse(self) -> None:
        self.work.chmod(0o777)
        docker(
            [
                "run",
                "--rm",
                "--name",
                f"{self.container}-generate",
                "-v",
                f"{self.work}:/data",
                "-e",
                "SYNAPSE_SERVER_NAME=localhost",
                "-e",
                "SYNAPSE_REPORT_STATS=no",
                "-e",
                "SYNAPSE_NO_TLS=1",
                SYNAPSE_IMAGE,
                "generate",
            ],
            timeout=self.args.timeout,
        )
        docker(
            [
                "run",
                "--rm",
                "--user",
                "0",
                "-v",
                f"{self.work}:/data",
                "--entrypoint",
                "sh",
                SYNAPSE_IMAGE,
                "-c",
                (
                    "printf '\\nregistration_shared_secret: "
                    f"{REGISTRATION_SECRET}\\n"
                    "rc_message:\\n  per_second: 1000\\n  burst_count: 1000\\n"
                    "rc_login:\\n  address:\\n    per_second: 1000\\n"
                    "    burst_count: 1000\\n  account:\\n    per_second: 1000\\n"
                    "    burst_count: 1000\\n  failed_attempts:\\n"
                    "    per_second: 1000\\n    burst_count: 1000\\n"
                    "rc_registration:\\n  per_second: 1000\\n"
                    "  burst_count: 1000\\n' >> /data/homeserver.yaml"
                ),
            ],
            timeout=self.args.timeout,
        )
        docker(
            [
                "run",
                "-d",
                "--rm",
                "--name",
                self.container,
                "-v",
                f"{self.work}:/data",
                "-p",
                f"127.0.0.1:{self.port}:8008",
                SYNAPSE_IMAGE,
            ],
            timeout=self.args.timeout,
        )
        await self.wait_for_homeserver()
        for user, password in (
            ("bridge", BRIDGE_PASSWORD),
            ("owner", OWNER_PASSWORD),
            ("stranger", STRANGER_PASSWORD),
        ):
            docker(
                [
                    "exec",
                    self.container,
                    "register_new_matrix_user",
                    "-u",
                    user,
                    "-p",
                    password,
                    "--no-admin",
                    "-c",
                    "/data/homeserver.yaml",
                    "http://localhost:8008",
                ],
                timeout=60,
            )

    async def setup_bridge(self) -> None:
        self.bridge_token, bridge_device = await self.http_login(
            "bridge",
            BRIDGE_PASSWORD,
            "OPENRIG_BRIDGE",
        )
        owner_token, owner_device = await self.http_login(
            "owner",
            OWNER_PASSWORD,
            "OPENRIG_OWNER",
        )
        setup_command = [
            sys.executable,
            "-m",
            "openrig_matrix_bridge",
            "setup",
            "--config",
            str(self.config_path),
            "--homeserver",
            self.homeserver,
            "--user-id",
            BRIDGE_USER,
            "--device-id",
            bridge_device,
            "--allowed-user",
            OWNER_USER,
            "--state-dir",
            str(self.state_dir),
            "--download-root",
            str(self.work / "parakeet"),
            "--rig-command",
            str(ROOT / "tools" / "fake_openrig.py"),
            "--codex-home",
            str(self.work / "codex"),
            "--sender-session",
            "matrix-bridge@local",
            "--human-destination",
            "owner@external",
            "--human-id",
            "owner",
            "--skip-model",
        ]
        setup = run(
            setup_command,
            env=self.env(),
            timeout=self.args.timeout,
        )
        # The acceptance receipt is opt-in; enable it so `acceptance-reaction`
        # proves the opt-in path. `acceptance-reaction-default-off` removes
        # the key again to prove the default.
        # The single-message checks send unrelated attachments seconds apart;
        # timing correlation is off for them so each stays one request. The
        # related-context checks restore the production defaults.
        self.set_openrig_options(
            acceptance_reaction_enabled=True,
            burst_inactivity_seconds=0,
        )
        doctor = self.cli("doctor")
        if doctor.returncode != 0:
            raise CheckFailure(f"bridge doctor failed: {doctor.stdout}{doctor.stderr}")
        rerun = run(
            setup_command,
            env=self.env(),
            timeout=self.args.timeout,
            check=False,
        )
        if rerun.returncode == 0 or "--replace-config" not in rerun.stderr:
            raise CheckFailure(
                "setup over an existing config did not fail with the "
                f"--replace-config next step: {rerun.returncode} "
                f"{rerun.stdout}{rerun.stderr}"
            )
        self.record(
            "setup-and-doctor",
            "pass",
            (
                f"`setup` wrote one agent room; `doctor` exited 0 "
                f"({setup.stdout.splitlines()[0]}); a second `setup` without "
                "`--replace-config` failed with that next step. Single-message "
                "checks run with timing correlation off "
                "(burst_inactivity_seconds: 0); the related-context checks run "
                "with the production defaults."
            ),
        )
        owner_store = self.work / "owner-store"
        self.owner = MatrixOwner(
            self.homeserver,
            owner_token,
            owner_device,
            owner_store,
        )
        await self.owner.start()

    def start_bridge(
        self,
        *,
        crash_after: str | None = None,
        allow_steering: bool = True,
        late_steer_seconds: float | None = None,
        unrelated_turn: bool = False,
        hold_seconds: float | None = None,
        transcribe_seconds: float | None = None,
    ) -> None:
        if self.bridge is not None and self.bridge.poll() is None:
            raise CheckFailure("bridge is already running")
        log = self.log_path.open("a", encoding="utf-8")
        environment = self.env()
        if allow_steering:
            environment["OPENRIG_FAKE_ALLOW_STEERING"] = "1"
        else:
            environment.pop("OPENRIG_FAKE_ALLOW_STEERING", None)
        if late_steer_seconds is not None:
            environment["OPENRIG_FAKE_LATE_STEER_SECONDS"] = str(late_steer_seconds)
        else:
            environment.pop("OPENRIG_FAKE_LATE_STEER_SECONDS", None)
        if unrelated_turn:
            environment["OPENRIG_FAKE_UNRELATED_TURN"] = "1"
        else:
            environment.pop("OPENRIG_FAKE_UNRELATED_TURN", None)
        if hold_seconds is not None:
            environment["OPENRIG_FAKE_HOLD_SECONDS"] = str(hold_seconds)
        else:
            environment.pop("OPENRIG_FAKE_HOLD_SECONDS", None)
        if transcribe_seconds is not None:
            environment["OPENRIG_FAKE_TRANSCRIBE_SECONDS"] = str(transcribe_seconds)
        else:
            environment.pop("OPENRIG_FAKE_TRANSCRIBE_SECONDS", None)
        if crash_after:
            environment["OPENRIG_BRIDGE_TEST_CRASH_AFTER"] = crash_after
        self.bridge_started_ms = int(time.time() * 1000)
        self.bridge = subprocess.Popen(
            [
                sys.executable,
                str(ROOT / "tools" / "deterministic_transcriber_bridge.py"),
                "run",
                "--config",
                str(self.config_path),
            ],
            cwd=ROOT,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )

    def set_openrig_options(self, **values: Any) -> None:
        """Rewrite `openrig` keys in the bridge config; None removes a key."""
        raw = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise CheckFailure("bridge configuration is not a mapping")
        transport = raw.setdefault("openrig", {})
        if not isinstance(transport, dict):
            raise CheckFailure("bridge openrig configuration is not a mapping")
        for key, value in values.items():
            if value is None:
                transport.pop(key, None)
            else:
                transport[key] = value
        self.config_path.write_text(
            yaml.safe_dump(raw, sort_keys=False),
            encoding="utf-8",
        )

    def set_steering_mode(self, mode: str) -> None:
        self.set_openrig_options(
            steering_mode=mode,
            steering_batch_window_seconds=0.2,
        )

    def wait_for_bridge_crash(self, expected_code: int = 86) -> None:
        process = self.bridge
        if process is None:
            raise CheckFailure("bridge process was not started")
        deadline = time.monotonic() + self.args.timeout
        while time.monotonic() < deadline:
            code = process.poll()
            if code is not None:
                if code != expected_code:
                    raise CheckFailure(
                        f"bridge crash exit code was {code}, expected {expected_code}"
                    )
                return
            time.sleep(0.1)
        raise CheckFailure("bridge did not reach the injected crash point")

    def stop_bridge(self) -> int | None:
        """SIGTERM the bridge like systemd does and return its exit code.

        A bridge stopped before startup installs its SIGTERM handler still
        dies with -SIGTERM; a running bridge stops gracefully with 0.
        """
        process = self.bridge
        if process is None or process.poll() is not None:
            return None
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        if process.returncode not in {0, -signal.SIGTERM}:
            raise CheckFailure(f"bridge exited with {process.returncode}")
        return process.returncode

    def bridge_state(self) -> dict[str, Any]:
        path = self.state_dir / "state.json"
        return json.loads(path.read_text(encoding="utf-8"))

    async def join_agent_room(self) -> str:
        assert self.owner is not None
        deadline = time.monotonic() + self.args.timeout
        while time.monotonic() < deadline:
            await self.owner.sync_once()
            try:
                room_id = str(self.bridge_state()["rooms"][AGENT_KEY]["room_id"])
            except (KeyError, FileNotFoundError, json.JSONDecodeError):
                await asyncio.sleep(0.1)
                continue
            if room_id in self.owner.client.rooms:
                return room_id
            await asyncio.sleep(0.1)
        raise CheckFailure("owner did not join the created agent room")

    async def check_room(self, room_id: str) -> None:
        assert self.owner is not None
        owner = self.owner
        await owner.sync_until(
            lambda: any(
                message["sender"] == BRIDGE_USER and "verbunden" in str(message["body"])
                for message in owner.messages
            ),
            self.args.timeout,
            description="encrypted welcome message",
        )

        async def state_event(event_type: str) -> RoomGetStateEventResponse:
            response = await owner.client.room_get_state_event(room_id, event_type)
            if not isinstance(response, RoomGetStateEventResponse):
                raise CheckFailure(f"could not read {event_type}: {response}")
            return response

        encryption = await state_event("m.room.encryption")
        avatar = await state_event("m.room.avatar")
        status = await state_event("dev.openrig.agent_status")
        if encryption.content.get("algorithm") != "m.megolm.v1.aes-sha2":
            raise CheckFailure(f"room is not Megolm encrypted: {encryption.content}")
        if not str(avatar.content.get("url") or "").startswith("mxc://"):
            raise CheckFailure(f"room avatar is missing: {avatar.content}")
        profile = await owner.client.get_profile(BRIDGE_USER)
        profile_mxc = (
            profile.avatar_url
            if isinstance(profile, ProfileGetResponse)
            else None
        )
        if not str(profile_mxc or "").startswith("mxc://"):
            raise CheckFailure(
                f"bridge account profile avatar is missing: {profile}"
            )
        profile_download = await owner.client.download(mxc=str(profile_mxc))
        profile_bytes = getattr(profile_download, "body", None)
        expected_profile = (
            ROOT
            / "src"
            / "openrig_matrix_bridge"
            / "assets"
            / "branding"
            / "export"
            / "relay-avatar-256.png"
        ).read_bytes()
        if profile_bytes != expected_profile:
            raise CheckFailure(
                "bridge account profile avatar does not match the Relay export"
            )
        if status.content.get("state") != "ready":
            raise CheckFailure(
                f"initial structured status is not ready: {status.content}"
            )
        if not str(owner.client.rooms[room_id].topic).startswith("🟢 bereit"):
            raise CheckFailure("initial room topic does not show ready status")
        self.record(
            "room-status-avatar",
            "pass",
            (
                "Synapse accepted the owner invite and join; the room uses "
                "`m.megolm.v1.aes-sha2`, has a managed room avatar, exposes a "
                "downloadable Relay bridge-account profile avatar with identical "
                "bytes, and publishes ready topic plus `dev.openrig.agent_status` "
                "state."
            ),
        )

    async def check_chat_and_queue(self, room_id: str) -> None:
        assert self.owner is not None
        owner = self.owner
        initial_bridge_messages = sum(
            message["sender"] == BRIDGE_USER for message in owner.messages
        )
        prompt_event_id = await owner.send_text(
            room_id,
            "ordinary encrypted matrix request",
        )
        await owner.sync_until(
            lambda: any(
                message["sender"] == BRIDGE_USER
                and "Fake integration answer" in str(message["body"])
                and "ordinary encrypted matrix request" in str(message["body"])
                for message in owner.messages
            ),
            self.args.timeout,
            description="ordinary encrypted answer",
        )
        wait_for_json_state(
            self.state_dir / "state.json",
            lambda value: not value.get("pending", {}).get(AGENT_KEY),
            self.args.timeout,
        )
        fake = json.loads((self.fake_state / "fake-openrig.json").read_text())
        prompts = [item["prompt"] for item in fake["prompts"]]
        if prompts[-1] != "ordinary encrypted matrix request":
            raise CheckFailure(f"prompt did not reach fake transport: {prompts[-3:]}")
        qitems = list(fake["queue"].values())
        if not qitems or qitems[-1]["state"] != "done":
            raise CheckFailure(f"Matrix request qitem was not completed: {qitems[-1:]}")
        if qitems[-1]["priority"] != "urgent":
            raise CheckFailure(f"Matrix request qitem priority is wrong: {qitems[-1]}")
        validate_qitem_history(qitems[-1])
        if not any(BRIDGE_USER in item["users"] for item in owner.typing):
            raise CheckFailure("bridge typing indicator was not observed")
        commentary = next(
            (
                message
                for message in owner.messages
                if "Fake transport received" in str(message["body"])
            ),
            None,
        )
        if commentary is None:
            raise CheckFailure("visible commentary was not mirrored")
        if (
            "**Aktivität:** Arbeitet" not in str(commentary["body"])
            and "**Aktivität:** Bereit" not in str(commentary["body"])
        ):
            raise CheckFailure(
                f"commentary card lacks a safe activity label: {commentary['body']}"
            )
        if sum(message["sender"] == BRIDGE_USER for message in owner.messages) <= initial_bridge_messages:
            raise CheckFailure("answer did not create a visible bridge message")
        answer = next(
            message
            for message in reversed(owner.messages)
            if message["sender"] == BRIDGE_USER
            and "Fake integration answer" in str(message["body"])
        )
        raw_answer = await owner.raw_event(room_id, answer["event_id"])
        if (
            raw_answer.get("type") != "m.room.encrypted"
            or raw_answer.get("content", {}).get("algorithm") != "m.megolm.v1.aes-sha2"
            or not raw_answer.get("content", {}).get("ciphertext")
        ):
            raise CheckFailure(
                f"bridge answer was not Megolm encrypted on the wire: {raw_answer}"
            )
        validate_answer_relation(raw_answer, prompt_event_id)
        self.record(
            "encrypted-chat-queue-typing",
            "pass",
            (
                "Owner text was encrypted by matrix-nio; the fake OpenRig "
                "subprocess received it exactly once, and the recorded qitem "
                "history is pending -> in-progress -> done. Typing was observed, "
                "commentary was mirrored in one activity-labelled card, and the "
                "final answer returned separately in-thread. "
                "The raw thread relation references the owner prompt. "
                "The homeserver API reported the final event as raw "
                "`m.room.encrypted` with Megolm ciphertext."
            ),
        )

    async def check_threaded_reply(self, room_id: str) -> None:
        assert self.owner is not None
        owner = self.owner
        root_event_id = await owner.send_text(room_id, "thread root request")
        await owner.sync_until(
            lambda: any(
                message["sender"] == BRIDGE_USER
                and "Fake integration answer: thread root request"
                in str(message["body"])
                for message in owner.messages
            ),
            self.args.timeout,
            description="thread root answer",
        )

        before = len(owner.messages)
        prompt = "hold first threaded restart request"
        threaded_event_id = await owner.send_text(
            room_id,
            prompt,
            relates_to={
                "rel_type": "m.thread",
                "event_id": root_event_id,
                "is_falling_back": True,
                "m.in_reply_to": {"event_id": root_event_id},
            },
        )
        thread_reply_mark = len(owner.timeline)
        threaded_prompt = await owner.raw_event(room_id, threaded_event_id)
        threaded_prompt_ts = int(threaded_prompt.get("origin_server_ts") or 0)

        def prompt_started(value: dict[str, Any]) -> bool:
            pending = value.get("pending", {}).get(AGENT_KEY, [])
            return bool(
                pending
                and pending[0].get("event_id") == threaded_event_id
                and pending[0].get("started_at")
                and pending[0].get("prompt_phase") == "sent"
                and pending[0].get("qitem_phase") in {"claimed", "finishing"}
                and pending[0].get("thread_root_event_id") == root_event_id
            )

        wait_for_json_state(
            self.state_dir / "state.json", prompt_started, self.args.timeout
        )
        await owner.sync_until(
            lambda: any(
                item["prompt"].startswith("hold first threaded restart request")
                for item in json.loads(
                    (self.fake_state / "fake-openrig.json").read_text()
                )["prompts"]
            ),
            self.args.timeout,
            description="threaded prompt at fake transport",
        )
        await owner.sync_until(
            lambda: any(
                entry["kind"] == "typing" and entry["bridge"]
                for entry in owner.timeline[thread_reply_mark:]
            ),
            self.args.timeout,
            description="bridge typing for a reply inside an existing thread",
        )
        stop_code = self.stop_bridge()
        if stop_code != 0:
            raise CheckFailure(
                f"running bridge did not stop gracefully on SIGTERM: {stop_code}"
            )
        restart_mark = len(owner.timeline)
        self.start_bridge()

        def threaded_delivery_observed(before_count=before) -> bool:
            return any(
                message["sender"] == BRIDGE_USER
                and "Fake transport received: hold first threaded restart request"
                in str(message["body"])
                and "**Aktivität:**" in str(message["body"])
                for message in owner.messages[before_count:]
            ) and any(
                message["sender"] == BRIDGE_USER
                and "Fake integration answer: hold first threaded restart request"
                in str(message["body"])
                for message in owner.messages[before_count:]
            )

        try:
            await owner.sync_until(
                threaded_delivery_observed,
                self.args.timeout,
                description="threaded commentary and final answer after restart",
            )
        except CheckFailure:
            # One Matrix sync after a rapid bridge restart can miss the threaded
            # events even though the pending state is intact. Retry the same
            # restart/observation boundary once; duplicate and qitem assertions
            # below still fail if replay is not idempotent.
            self.stop_bridge()
            self.start_bridge()
            await owner.sync_until(
                threaded_delivery_observed,
                self.args.timeout,
                description="threaded delivery after second restart",
            )
        wait_for_json_state(
            self.state_dir / "state.json",
            lambda value: not value.get("pending", {}).get(AGENT_KEY),
            self.args.timeout,
        )
        await asyncio.sleep(0.5)
        await owner.sync_once()
        after_restart = feedback_window(
            owner.timeline,
            restart_mark,
            "Fake integration answer: hold first threaded restart request",
            include_concurrent=True,
        )
        if not any(
            entry["kind"] == "typing" and entry["bridge"] for entry in after_restart
        ):
            held = next(
                item
                for item in json.loads(
                    (self.fake_state / "fake-openrig.json").read_text()
                )["prompts"]
                if item["prompt"].startswith("hold first threaded restart request")
            )
            raise CheckFailure(
                "bridge typing was not restored after restart for the in-flight "
                f"thread reply (fake completed_at={held.get('completed_at')}, "
                f"bridge restarted_at={self.bridge_started_ms / 1000:.3f}, "
                f"window={after_restart})"
            )

        threaded_messages = [
            message
            for message in owner.messages[before:]
            if message["sender"] == BRIDGE_USER
            and int(message["source"].get("origin_server_ts") or 0)
            >= threaded_prompt_ts
            and (
                "Fake transport received: hold first threaded restart request"
                in str(message["body"])
                or "Fake integration answer: hold first threaded restart request"
                in str(message["body"])
                or "**Aktivität:**" in str(message["body"])
            )
        ]
        final_messages = [
            message
            for message in threaded_messages
            if "Fake integration answer: hold first threaded restart request"
            in str(message["body"])
        ]
        if len(final_messages) != 1:
            raise CheckFailure(
                "threaded prompt did not produce exactly one final answer"
            )
        final_event_ids = {message["event_id"] for message in final_messages}
        card_sends = set()
        card_send_details = {}
        card_edits = set()
        commentary_seen = False
        for message in threaded_messages:
            raw = await owner.raw_event(room_id, message["event_id"])
            relation = raw.get("content", {}).get("m.relates_to")
            if not isinstance(relation, dict):
                continue
            if (
                relation.get("rel_type") == "m.thread"
                and message["event_id"] not in final_event_ids
            ):
                if relation.get("event_id") != root_event_id:
                    raise CheckFailure(
                        "activity card used the wrong canonical thread root"
                    )
                card_sends.add(message["event_id"])
                card_send_details[message["event_id"]] = (
                    message["source"].get("origin_server_ts"),
                    str(message["body"]),
                )
            elif relation.get("rel_type") == "m.replace":
                card_edits.add(str(relation.get("event_id") or ""))
            if (
                "Fake transport received: hold first threaded restart request"
                in str(message["body"])
            ):
                commentary_seen = True
        if len(card_sends) != 1 or card_edits != card_sends:
            raise CheckFailure(
                f"threaded activity card identity is inconsistent: "
                f"sends={card_send_details}, edits={card_edits}"
            )
        if not commentary_seen:
            raise CheckFailure("threaded activity card never showed commentary")
        validate_answer_relation(
            await owner.raw_event(room_id, final_messages[0]["event_id"]),
            root_event_id,
        )

        fake = json.loads((self.fake_state / "fake-openrig.json").read_text())
        prompt_records = [
            item
            for item in fake["prompts"]
            if item["prompt"].startswith("hold first threaded restart request")
        ]
        if len(prompt_records) != 1:
            raise CheckFailure(
                "threaded prompt was delivered more than once across restart"
            )
        qitems = [
            item
            for item in fake["queue"].values()
            if "hold first threaded restart request" in str(item.get("body") or "")
        ]
        if len(qitems) != 1:
            raise CheckFailure("threaded prompt did not create exactly one qitem")
        validate_qitem_history(qitems[0])
        if qitems[0]["state"] != "done":
            raise CheckFailure(f"threaded qitem did not close: {qitems[0]}")
        self.record(
            "threaded-prompt-restart",
            "pass",
            (
                "An encrypted owner prompt already in m.thread survived a bridge "
                "restart while the fake seat was busy. Commentary and final answer "
                "were each delivered once under the canonical thread root, and the "
                "single Matrix qitem completed pending -> in-progress -> done. "
                "Bridge typing was observed for the reply inside the existing "
                "thread; SIGTERM stopped the running bridge gracefully (exit 0) "
                "and the restarted bridge restored typing before the answer."
            ),
        )

    async def check_answer_crash_recovery(self, room_id: str) -> None:
        assert self.owner is not None
        owner = self.owner
        prompt = "crash after answer send"
        self.stop_bridge()
        self.start_bridge(crash_after="answer_send")
        await owner.sync_until(
            lambda: str(owner.client.rooms[room_id].topic).startswith("🟢 bereit"),
            self.args.timeout,
            description="ready status before answer crash probe",
        )
        before = len(owner.messages)
        await owner.send_text(room_id, prompt)
        self.wait_for_bridge_crash()
        await owner.sync_until(
            lambda: any(
                message["sender"] == BRIDGE_USER
                and f"Fake integration answer: {prompt}" in str(message["body"])
                for message in owner.messages[before:]
            ),
            self.args.timeout,
            description="answer sent before injected crash",
        )
        first_answers = [
            message["event_id"]
            for message in owner.messages[before:]
            if message["sender"] == BRIDGE_USER
            and f"Fake integration answer: {prompt}" in str(message["body"])
        ]
        if len(first_answers) != 1:
            raise CheckFailure(
                "answer crash probe did not observe exactly one first answer"
            )

        self.start_bridge()
        wait_for_json_state(
            self.state_dir / "state.json",
            lambda value: not value.get("pending", {}).get(AGENT_KEY),
            self.args.timeout,
        )
        await asyncio.sleep(1.0)
        await owner.sync_once()
        answers = [
            message["event_id"]
            for message in owner.messages[before:]
            if message["sender"] == BRIDGE_USER
            and f"Fake integration answer: {prompt}" in str(message["body"])
        ]
        if answers != first_answers:
            raise CheckFailure(f"restart duplicated the Matrix answer: {answers}")
        fake = json.loads((self.fake_state / "fake-openrig.json").read_text())
        qitems = [
            item
            for item in fake["queue"].values()
            if prompt in str(item.get("body") or "")
        ]
        if len(qitems) != 1 or qitems[0].get("state") != "done":
            raise CheckFailure(f"answer crash qitem did not reconcile: {qitems}")
        validate_qitem_history(qitems[0])
        self.record(
            "answer-crash-recovery",
            "pass",
            (
                "The bridge process exited immediately after the final Matrix answer "
                "send. Restart retried with the persisted transaction ID, Matrix "
                "deduplicated to the same event, and the single qitem completed."
            ),
        )

    async def check_activity_card_crash_recovery(self, room_id: str) -> None:
        """Issue #39: a card whose send response was lost must not be duplicated.

        The bridge exits right after Matrix accepted the initial activity card
        and before it recorded the card's event ID. The restarted bridge resends
        with the same transaction ID; Matrix must return the same event, so the
        request keeps exactly one card that every later edit targets.
        """
        assert self.owner is not None
        owner = self.owner
        prompt = "crash after activity card send"
        self.stop_bridge()
        self.start_bridge(crash_after="activity_card_send")
        await owner.sync_until(
            lambda: str(owner.client.rooms[room_id].topic).startswith("🟢 bereit"),
            self.args.timeout,
            description="ready status before activity-card crash probe",
        )
        before = len(owner.messages)
        prompt_event_id = await owner.send_text(room_id, prompt)
        self.wait_for_bridge_crash()
        pending = self.bridge_state().get("pending", {}).get(AGENT_KEY, [])
        if not pending or pending[0].get("event_id") != prompt_event_id:
            raise CheckFailure("activity-card crash left no in-flight request")
        if pending[0].get("commentary_event_id"):
            raise CheckFailure("the card was owned before the injected crash point")

        self.start_bridge()
        wait_for_json_state(
            self.state_dir / "state.json",
            lambda value: not value.get("pending", {}).get(AGENT_KEY),
            self.args.timeout,
        )
        await owner.sync_until(
            lambda: any(
                message["sender"] == BRIDGE_USER
                and f"Fake integration answer: {prompt}" in str(message["body"])
                for message in owner.messages[before:]
            ),
            self.args.timeout,
            description="answer after activity-card crash recovery",
        )
        await asyncio.sleep(1.0)
        await owner.sync_once()
        cards: list[str] = []
        edits: set[str] = set()
        answers = 0
        for message in owner.messages[before:]:
            if message["sender"] != BRIDGE_USER:
                continue
            relation = message["source"].get("content", {}).get("m.relates_to") or {}
            body = str(message["body"])
            if relation.get("rel_type") == "m.replace":
                # The pinned queue card is edited in place as well; only
                # activity-card edits must target the single card.
                if "**Aktivität:**" in body:
                    edits.add(str(relation.get("event_id") or ""))
            elif f"Fake integration answer: {prompt}" in body:
                answers += 1
            elif "**Aktivität:**" in body and relation.get("event_id") == prompt_event_id:
                cards.append(message["event_id"])
        if len(cards) != 1 or not edits or not edits <= set(cards):
            raise CheckFailure(
                f"activity card duplicated after a lost send: cards={cards}, "
                f"edits={sorted(edits)}"
            )
        if answers != 1:
            raise CheckFailure(f"activity-card crash produced {answers} answers")
        fake = json.loads((self.fake_state / "fake-openrig.json").read_text())
        qitems = [
            item
            for item in fake["queue"].values()
            if prompt in str(item.get("body") or "")
        ]
        if len(qitems) != 1 or qitems[0].get("state") != "done":
            raise CheckFailure(f"activity-card crash qitem did not reconcile: {qitems}")
        validate_qitem_history(qitems[0])
        self.record(
            "activity-card-crash-recovery",
            "pass",
            (
                "The bridge exited right after Matrix accepted the initial activity "
                "card and before the card's event ID was recorded. The restarted "
                "bridge resent it with the same transaction ID, Matrix returned "
                "the same event, and the request kept exactly one card; every "
                "edit targeted it, one answer was sent and the single qitem "
                "completed (issue #39)."
            ),
        )

    async def check_qitem_claim_crash_recovery(self, room_id: str) -> None:
        assert self.owner is not None
        owner = self.owner
        prompt = "claim crash recovery request"
        self.stop_bridge()
        self.start_bridge(crash_after="qitem_claim")
        await owner.sync_until(
            lambda: str(owner.client.rooms[room_id].topic).startswith("🟢 bereit"),
            self.args.timeout,
            description="ready status before qitem claim crash probe",
        )
        await owner.send_text(room_id, prompt)
        self.wait_for_bridge_crash()

        fake = json.loads((self.fake_state / "fake-openrig.json").read_text())
        matching_qitems = [
            item
            for item in fake["queue"].values()
            if prompt in str(item.get("body") or "")
        ]
        if (
            len(matching_qitems) != 1
            or matching_qitems[0].get("state") != "in-progress"
        ):
            raise CheckFailure(
                f"claim crash did not leave one in-progress qitem: {matching_qitems}"
            )
        if any(item.get("prompt") == prompt for item in fake["prompts"]):
            raise CheckFailure("prompt was sent before the injected claim crash")

        self.start_bridge()
        await owner.sync_until(
            lambda: any(
                item.get("prompt") == prompt
                for item in json.loads(
                    (self.fake_state / "fake-openrig.json").read_text()
                )["prompts"]
            ),
            self.args.timeout,
            description="prompt resumed after claim crash",
        )
        await owner.sync_until(
            lambda: any(
                message["sender"] == BRIDGE_USER
                and f"Fake integration answer: {prompt}" in str(message["body"])
                for message in owner.messages
            ),
            self.args.timeout,
            description="answer after claim crash recovery",
        )
        wait_for_json_state(
            self.state_dir / "state.json",
            lambda value: not value.get("pending", {}).get(AGENT_KEY),
            self.args.timeout,
        )
        fake = json.loads((self.fake_state / "fake-openrig.json").read_text())
        prompts = [item for item in fake["prompts"] if item.get("prompt") == prompt]
        qitems = [
            item
            for item in fake["queue"].values()
            if prompt in str(item.get("body") or "")
        ]
        if len(prompts) != 1 or len(qitems) != 1 or qitems[0].get("state") != "done":
            raise CheckFailure(
                f"claim crash recovery was not exactly-once: prompts={prompts} qitems={qitems}"
            )
        validate_qitem_history(qitems[0])
        self.record(
            "qitem-claim-crash-recovery",
            "pass",
            (
                "The bridge exited immediately after claiming an OpenRig qitem. "
                "Restart recognized the in-progress qitem, resumed the unsent prompt "
                "exactly once, and completed the same qitem."
            ),
        )

    async def check_fifo(self, room_id: str) -> None:
        assert self.owner is not None
        owner = self.owner
        await owner.send_text(room_id, "hold first request")
        await owner.send_text(room_id, "second queued request")
        try:
            await owner.sync_until(
                lambda: any(
                    "Warteschlange" in str(message["body"])
                    and '**Aktiv:** Text · "hold first request"' in str(
                        message["body"]
                    )
                    and '1. Text · "second queued request"' in str(
                        message["body"]
                    )
                    for message in owner.messages
                ),
                self.args.timeout,
                description="pinned active and waiting queue previews",
            )
        except CheckFailure as exc:
            cards = [
                str(message["body"])
                for message in owner.messages
                if "Warteschlange" in str(message["body"])
            ]
            raise CheckFailure(f"{exc}; every queue card seen: {cards}") from exc
        await owner.sync_until(
            lambda: any(
                "Fake integration answer: second queued request" in str(message["body"])
                for message in owner.messages
            ),
            self.args.timeout,
            description="second FIFO answer",
        )
        wait_for_json_state(
            self.state_dir / "state.json",
            lambda value: not value.get("pending", {}).get(AGENT_KEY),
            self.args.timeout,
        )
        await asyncio.sleep(0.2)
        await owner.sync_once()
        first_answer = bridge_message_index(
            owner.timeline, "Fake integration answer: hold first request"
        )
        between_turns = feedback_window(
            owner.timeline,
            first_answer + 1,
            "Fake integration answer: second queued request",
        )
        require_continuous_feedback(between_turns, "queued turn handover")
        if not any(entry["kind"] == "status" for entry in owner.timeline):
            raise CheckFailure("agent status updates were not observed in the timeline")
        fake = json.loads((self.fake_state / "fake-openrig.json").read_text())
        records = fake["prompts"]
        validate_fifo_records(
            records,
            "hold first request",
            "second queued request",
        )
        first = next(item for item in records if item["prompt"] == "hold first request")
        second = next(
            item for item in records if item["prompt"] == "second queued request"
        )
        self.record(
            "fifo-queue-notice",
            "pass",
            (
                "A second owner message arrived during a deliberately slow "
                "first turn; Matrix showed one pinned queue card with the active "
                "prompt followed by the ordered waiting preview, and the "
                "fake OpenRig transport received the second prompt only after "
                "recording completion of the first "
                f"({first['completed_at']} < {second['sent_at']}). Between the "
                "two answers the owner timeline showed no ready status and no "
                "gap in the bridge typing indicator."
            ),
        )

    async def wait_for_steering_mode_ready(self, room_id: str) -> None:
        """Wait until the current bridge process has finished startup.

        The cached topic can still read ready from the previous process. Events
        delivered in the restarted bridge's initial sync are not routed, so a
        probe must wait for the status the new process writes after startup.
        """
        assert self.owner is not None
        url = (
            f"{self.homeserver}/_matrix/client/v3/rooms/{quote(room_id)}"
            "/state/dev.openrig.agent_status/"
        )
        deadline = time.monotonic() + self.args.timeout
        async with aiohttp.ClientSession(
            headers={"Authorization": f"Bearer {self.owner.token}"}
        ) as session:
            while time.monotonic() < deadline:
                async with session.get(url) as response:
                    value = await response.json() if response.status == 200 else {}
                if int(value.get("updated_at") or 0) >= self.bridge_started_ms:
                    break
                await asyncio.sleep(0.2)
            else:
                raise CheckFailure("restarted bridge did not publish a fresh status")
        await self.owner.sync_until(
            lambda: str(self.owner.client.rooms[room_id].topic).startswith("🟢 bereit"),
            self.args.timeout,
            description="ready status after steering-mode restart",
        )

    def wait_for_active_prompt(self, event_id: str, prompt: str) -> None:
        wait_for_json_state(
            self.state_dir / "state.json",
            lambda value: bool(
                value.get("pending", {}).get(AGENT_KEY)
                and value["pending"][AGENT_KEY][0].get("event_id") == event_id
                and value["pending"][AGENT_KEY][0].get("prompt_phase") == "sent"
            ),
            self.args.timeout,
            description=f"active prompt phase for {prompt}",
        )
        wait_for_json_state(
            self.fake_state / "fake-openrig.json",
            lambda value: any(
                item.get("prompt") == prompt
                and isinstance(item.get("sent_at"), (int, float))
                for item in value.get("prompts", [])
            ),
            self.args.timeout,
            description=f"fake transport delivery for {prompt}",
        )

    async def check_steering_modes(self, room_id: str) -> None:
        assert self.owner is not None
        owner = self.owner
        fake_path = self.fake_state / "fake-openrig.json"

        self.stop_bridge()
        self.set_steering_mode("queue")
        self.start_bridge(hold_seconds=STEERING_PROBE_HOLD_SECONDS)
        await self.wait_for_steering_mode_ready(room_id)
        queue_root = await owner.send_text(room_id, "hold first queue mode root")
        self.wait_for_active_prompt(queue_root, "hold first queue mode root")
        await owner.send_text(
            room_id,
            "queue mode follow-up",
            relates_to={
                "rel_type": "m.thread",
                "event_id": queue_root,
                "m.in_reply_to": {"event_id": queue_root},
            },
        )
        wait_for_json_state(
            fake_path,
            lambda value: any(
                item.get("prompt") == "queue mode follow-up"
                and isinstance(item.get("sent_at"), (int, float))
                for item in value.get("prompts", [])
            ),
            self.args.timeout,
            description="queue-mode follow-up to wait for the first turn",
        )
        queue_fake = json.loads(fake_path.read_text(encoding="utf-8"))
        queue_prompts = [
            item
            for item in queue_fake["prompts"]
            if item.get("prompt")
            in {"hold first queue mode root", "queue mode follow-up"}
        ]
        validate_fifo_records(
            queue_prompts,
            "hold first queue mode root",
            "queue mode follow-up",
        )
        if queue_fake.get("steers"):
            raise CheckFailure(f"queue mode attempted steering: {queue_fake['steers']}")

        self.stop_bridge()
        self.set_steering_mode("steer")
        self.start_bridge(hold_seconds=STEERING_PROBE_HOLD_SECONDS)
        await self.wait_for_steering_mode_ready(room_id)
        steer_before = len(json.loads(fake_path.read_text())["steers"])
        steer_root = await owner.send_text(room_id, "hold first steer mode root")
        self.wait_for_active_prompt(steer_root, "hold first steer mode root")
        steer_one = await owner.send_text(
            room_id,
            "steer mode follow one",
            relates_to={
                "rel_type": "m.thread",
                "event_id": steer_root,
                "m.in_reply_to": {"event_id": steer_root},
            },
        )
        steer_two = await owner.send_text(
            room_id,
            "steer mode follow two",
            relates_to={
                "rel_type": "m.thread",
                "event_id": steer_root,
                "m.in_reply_to": {"event_id": steer_root},
            },
        )
        wait_for_json_state(
            fake_path,
            lambda value: len(value.get("steers", [])) > steer_before,
            self.args.timeout,
            description="steer-mode batch admission attempt",
        )
        await owner.sync_until(
            lambda: any(
                message["sender"] == BRIDGE_USER
                and "Fake integration answer: hold first steer mode root"
                in str(message["body"])
                for message in owner.messages
            ),
            self.args.timeout,
            description="one answer covering steered same-thread batch",
        )
        wait_for_json_state(
            self.state_dir / "state.json",
            lambda value: not value.get("pending", {}).get(AGENT_KEY),
            self.args.timeout,
            description="steer-mode pending queue to drain",
        )
        steer_fake = json.loads(fake_path.read_text(encoding="utf-8"))
        steer_batch = steer_fake["steers"][steer_before]
        if (
            steer_one not in steer_batch["prompt"]
            or steer_two not in steer_batch["prompt"]
        ):
            raise CheckFailure(f"steering batch lost source event IDs: {steer_batch}")
        if steer_root not in steer_batch["prompt"]:
            raise CheckFailure(f"steering batch lost canonical root: {steer_batch}")
        if any(
            item.get("prompt") == steer_batch["prompt"]
            for item in steer_fake["prompts"]
        ):
            raise CheckFailure("admitted steering batch was also sent as a fallback")
        steer_answer = next(
            message
            for message in owner.messages
            if message["sender"] == BRIDGE_USER
            and "Fake integration answer: hold first steer mode root"
            in str(message["body"])
        )
        steer_source_ids = (
            steer_answer["source"]
            .get("content", {})
            .get("dev.openrig.source_event_ids")
        )
        if steer_source_ids != [steer_root, steer_one, steer_two]:
            raise CheckFailure(
                f"steering answer source metadata is wrong: {steer_source_ids}"
            )
        steer_qitems = [
            item
            for item in steer_fake["queue"].values()
            if any(
                marker in str(item.get("body") or "")
                for marker in (
                    "hold first steer mode root",
                    "steer mode follow one",
                    "steer mode follow two",
                )
            )
        ]
        if len(steer_qitems) != 3:
            raise CheckFailure(
                f"steering batch did not retain three qitems: {steer_qitems}"
            )
        for item in steer_qitems:
            validate_qitem_history(item)

        self.stop_bridge()
        self.set_steering_mode("auto")
        self.start_bridge(
            unrelated_turn=True,
            hold_seconds=STEERING_PROBE_HOLD_SECONDS,
        )
        await self.wait_for_steering_mode_ready(room_id)
        auto_before = len(json.loads(fake_path.read_text())["steers"])
        auto_root = await owner.send_text(room_id, "hold first auto mode root")
        self.wait_for_active_prompt(auto_root, "hold first auto mode root")
        auto_follow = await owner.send_text(
            room_id,
            "auto mode same-thread follow",
            relates_to={
                "rel_type": "m.thread",
                "event_id": auto_root,
                "m.in_reply_to": {"event_id": auto_root},
            },
        )
        other_root = await owner.send_text(
            room_id,
            "auto mode top-level task",
        )
        other_event = await owner.send_text(
            room_id,
            "auto mode other-thread task",
            relates_to={
                "rel_type": "m.thread",
                "event_id": other_root,
                "m.in_reply_to": {"event_id": other_root},
            },
        )
        wait_for_json_state(
            fake_path,
            lambda value: len(value.get("steers", [])) > auto_before,
            self.args.timeout,
            description="auto-mode same-thread steering attempt",
        )
        await owner.sync_until(
            lambda: (
                any(
                    "Fake integration answer: auto mode top-level task"
                    in str(message["body"])
                    for message in owner.messages
                )
                and any(
                    "Fake integration answer: auto mode other-thread task"
                    in str(message["body"])
                    for message in owner.messages
                )
            ),
            self.args.timeout,
            description="top-level and other-thread fallback answers",
        )
        auto_fake = json.loads(fake_path.read_text(encoding="utf-8"))
        auto_batch = auto_fake["steers"][auto_before]
        if auto_follow not in auto_batch["prompt"]:
            raise CheckFailure("auto mode did not steer the same-thread follow-up")
        for event_id in (auto_root, other_root, other_event):
            if event_id in auto_batch["prompt"] and event_id != auto_root:
                raise CheckFailure(
                    f"unrelated Matrix request was mixed into steering: {event_id}"
                )
        auto_prompts = [
            item["prompt"]
            for item in auto_fake["prompts"]
            if item.get("prompt")
            in {
                "hold first auto mode root",
                "auto mode top-level task",
                "auto mode other-thread task",
            }
        ]
        if auto_prompts != [
            "hold first auto mode root",
            "auto mode top-level task",
            "auto mode other-thread task",
        ]:
            raise CheckFailure(f"auto-mode FIFO isolation failed: {auto_prompts}")
        if any(
            "Unrelated human answer" in str(message["body"])
            for message in owner.messages
        ):
            raise CheckFailure("unrelated human final answer reached Matrix")
        self.record(
            "steering-modes-and-batch",
            "pass",
            (
                "Disposable-Synapse probes verified queue mode remains FIFO, steer "
                "mode injects one ordered two-event same-thread batch under its "
                "canonical root, and auto mode steers only the short same-thread "
                "follow-up while top-level and other-thread requests advance FIFO. "
                "All source qitems closed, and an unrelated human transcript turn "
                "did not steal the Matrix answer."
            ),
        )

    async def check_steering_fallback_and_restart(self, room_id: str) -> None:
        assert self.owner is not None
        owner = self.owner
        fake_path = self.fake_state / "fake-openrig.json"

        self.stop_bridge()
        self.set_steering_mode("auto")
        self.start_bridge(
            allow_steering=False,
            hold_seconds=STEERING_PROBE_HOLD_SECONDS,
        )
        await self.wait_for_steering_mode_ready(room_id)
        fallback_before = len(json.loads(fake_path.read_text())["prompts"])
        fallback_root = await owner.send_text(
            room_id,
            "hold first fallback mode root",
        )
        self.wait_for_active_prompt(
            fallback_root,
            "hold first fallback mode root",
        )
        await owner.send_text(
            room_id,
            "fallback follow one",
            relates_to={
                "rel_type": "m.thread",
                "event_id": fallback_root,
                "m.in_reply_to": {"event_id": fallback_root},
            },
        )
        await owner.send_text(
            room_id,
            "fallback follow two",
            relates_to={
                "rel_type": "m.thread",
                "event_id": fallback_root,
                "m.in_reply_to": {"event_id": fallback_root},
            },
        )
        wait_for_json_state(
            self.state_dir / "state.json",
            lambda value: any(
                request.get("deferred_steering")
                and request.get("injection_phase") == "fallback"
                for request in value.get("pending", {}).get(AGENT_KEY, [])
            ),
            self.args.timeout,
            description="steering fallback durable state",
        )
        await owner.sync_until(
            lambda: any(
                "Warteschlange" in str(message["body"])
                and "Aktiv (" not in str(message["body"])
                and "Thread" in str(message["body"])
                and "fallback follow one" in str(message["body"])
                and "fallback follow two" in str(message["body"])
                for message in owner.messages
            ),
            self.args.timeout,
            description="fallback steering queue preview",
        )
        wait_for_json_state(
            fake_path,
            lambda value: any(
                str(item.get("prompt") or "").startswith("Matrix follow-up batch")
                for item in value.get("prompts", [])
            ),
            self.args.timeout,
            description="fallback batch prompt",
        )
        wait_for_json_state(
            self.state_dir / "state.json",
            lambda value: not value.get("pending", {}).get(AGENT_KEY),
            self.args.timeout,
            description="fallback pending queue to drain",
        )
        fallback_fake = json.loads(fake_path.read_text(encoding="utf-8"))
        fallback_prompts = [
            item
            for item in fallback_fake["prompts"][fallback_before:]
            if "fallback" in str(item.get("prompt") or "")
        ]
        batch_prompts = [
            item
            for item in fallback_prompts
            if str(item.get("prompt") or "").startswith("Matrix follow-up batch")
        ]
        if len(batch_prompts) != 1:
            raise CheckFailure(
                f"unavailable steering did not fall back exactly once: {fallback_prompts}"
            )
        for marker in ("fallback follow one", "fallback follow two"):
            if marker not in batch_prompts[0]["prompt"]:
                raise CheckFailure(f"fallback batch lost {marker}")

        self.stop_bridge()
        self.start_bridge(
            crash_after="steering_send",
            hold_seconds=STEERING_PROBE_HOLD_SECONDS,
        )
        await self.wait_for_steering_mode_ready(room_id)
        restart_before = len(json.loads(fake_path.read_text())["prompts"])
        restart_root = await owner.send_text(
            room_id,
            "hold first restart steering root",
        )
        self.wait_for_active_prompt(
            restart_root,
            "hold first restart steering root",
        )
        restart_follow = await owner.send_text(
            room_id,
            "restart steering follow",
            relates_to={
                "rel_type": "m.thread",
                "event_id": restart_root,
                "m.in_reply_to": {"event_id": restart_root},
            },
        )
        self.wait_for_bridge_crash()
        crashed = json.loads(fake_path.read_text(encoding="utf-8"))
        if not any(
            restart_follow in item.get("prompt", "")
            for item in crashed.get("steers", [])
        ):
            raise CheckFailure("steering injection was not recorded before crash")
        self.start_bridge()
        await owner.sync_until(
            lambda: any(
                "Fake integration answer: hold first restart steering root"
                in str(message["body"])
                for message in owner.messages
            ),
            self.args.timeout,
            description="answer after steering restart recovery",
        )
        wait_for_json_state(
            self.state_dir / "state.json",
            lambda value: not value.get("pending", {}).get(AGENT_KEY),
            self.args.timeout,
            description="restart steering pending queue to drain",
        )
        restarted = json.loads(fake_path.read_text(encoding="utf-8"))
        restart_prompts = [
            item["prompt"]
            for item in restarted["prompts"][restart_before:]
            if "hold first restart steering root" in str(item.get("prompt") or "")
            or "restart steering follow" in str(item.get("prompt") or "")
        ]
        if restart_prompts != ["hold first restart steering root"]:
            raise CheckFailure(
                "restart replayed an admitted steering batch: "
                f"slice={restart_prompts}; before={restart_before}; "
                f"prompts={[item.get('prompt') for item in restarted['prompts']]}"
            )
        self.record(
            "steering-fallback-restart",
            "pass",
            (
                "With the fake transport refusing busy injection, the complete "
                "two-message batch remained durably queued and was sent once as "
                "one normal fallback turn. A process crash immediately after "
                "successful pane injection recovered from transcript admission "
                "without resending the batch."
            ),
        )

        self.stop_bridge()
        self.start_bridge(
            late_steer_seconds=6,
            hold_seconds=STEERING_PROBE_HOLD_SECONDS,
        )
        await self.wait_for_steering_mode_ready(room_id)
        late_before = len(json.loads(fake_path.read_text())["steers"])
        late_root = await owner.send_text(
            room_id,
            "hold first late admission root",
        )
        self.wait_for_active_prompt(
            late_root,
            "hold first late admission root",
        )
        late_follow = await owner.send_text(
            room_id,
            "late admission follow",
            relates_to={
                "rel_type": "m.thread",
                "event_id": late_root,
                "m.in_reply_to": {"event_id": late_root},
            },
        )
        wait_for_json_state(
            fake_path,
            lambda value: len(value.get("steers", [])) > late_before,
            self.args.timeout,
            description="late-admission steering injection",
        )
        await owner.sync_until(
            lambda: any(
                "Late steering answer:" in str(message["body"])
                for message in owner.messages
            ),
            self.args.timeout,
            description="answer from late-materialized steering turn",
        )
        wait_for_json_state(
            self.state_dir / "state.json",
            lambda value: not value.get("pending", {}).get(AGENT_KEY),
            self.args.timeout,
            description="late-admission pending queue to drain",
        )
        late_fake = json.loads(fake_path.read_text(encoding="utf-8"))
        late_batch = late_fake["steers"][late_before]
        if late_follow not in late_batch["prompt"]:
            raise CheckFailure(f"late-admission batch lost its source: {late_batch}")
        if not late_batch.get("late_materialization"):
            raise CheckFailure(f"late-admission probe was not delayed: {late_batch}")
        if any(
            item.get("prompt") == late_batch["prompt"]
            for item in late_fake["prompts"]
        ):
            raise CheckFailure("late-admitted batch was also sent as a fallback")
        transcript_records = [
            json.loads(line)
            for line in (self.fake_state / "transcript.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        ]
        late_user_records = [
            record
            for record in transcript_records
            if record.get("type") == "response_item"
            and record.get("payload", {}).get("role") == "user"
            and record.get("payload", {}).get("content", [{}])[0].get("text")
            == late_batch["prompt"]
        ]
        if len(late_user_records) != 1:
            raise CheckFailure(
                f"late-admission batch materialized {len(late_user_records)} times"
            )
        late_turn_id = late_user_records[0]["payload"][
            "internal_chat_message_metadata_passthrough"
        ]["turn_id"]
        late_finals = [
            record
            for record in transcript_records
            if record.get("payload", {}).get("turn_id") == late_turn_id
            and record.get("payload", {}).get("item", {}).get("phase")
            == "final_answer"
        ]
        if len(late_finals) != 1:
            raise CheckFailure(
                f"late-admission turn produced {len(late_finals)} final answers"
            )
        late_answers = [
            message
            for message in owner.messages
            if "Late steering answer:" in str(message["body"])
        ]
        if len(late_answers) != 1:
            raise CheckFailure(
                f"late-admission batch produced {len(late_answers)} Matrix answers"
            )
        late_source_ids = (
            late_answers[0]["source"]
            .get("content", {})
            .get("dev.openrig.source_event_ids")
        )
        if late_source_ids != [late_follow]:
            raise CheckFailure(
                "late-admission answer lost its follow-up source-event provenance"
            )
        late_relation = (
            late_answers[0]["source"].get("content", {}).get("m.relates_to")
        )
        if (
            not isinstance(late_relation, dict)
            or late_relation.get("event_id") != late_root
        ):
            raise CheckFailure(
                "late-admission answer lost the completed root's thread identity"
            )
        late_qitems = [
            item
            for item in late_fake["queue"].values()
            if any(
                marker in str(item.get("body") or "")
                for marker in (
                    "hold first late admission root",
                    "late admission follow",
                )
            )
        ]
        if len(late_qitems) != 2:
            raise CheckFailure(
                f"late-admission batch lost qitems: {late_qitems}"
            )
        for item in late_qitems:
            validate_qitem_history(item)
        root_answer = bridge_message_index(
            owner.timeline, "Fake integration answer: hold first late admission root"
        )
        awaiting_follow_up = feedback_window(
            owner.timeline,
            root_answer + 1,
            "Late steering answer:",
        )
        require_continuous_feedback(awaiting_follow_up, "late-admitted thread follow-up")
        self.record(
            "steering-late-admission",
            "pass",
            (
                "The deterministic transport retained a busy-pane follow-up "
                "without a transcript user record past the initial admission "
                "grace, completed the root turn first, then materialized the "
                "follow-up as a later turn. Matrix received one answer with both "
                "the follow-up source ID and canonical completed-root thread; "
                "the batch appeared exactly once in the transcript and was never "
                "sent as a fallback prompt. Between the root answer and the "
                "follow-up answer the room never turned ready and bridge typing "
                "never stopped."
            ),
        )
        self.stop_bridge()
        self.start_bridge()
        await self.wait_for_steering_mode_ready(room_id)

    async def check_reaction(self, room_id: str) -> None:
        assert self.owner is not None
        owner = self.owner
        target = next(
            (
                message
                for message in reversed(owner.messages)
                if message["sender"] == BRIDGE_USER
                and "Fake integration answer" in str(message["body"])
            ),
            None,
        )
        if target is None:
            raise CheckFailure("no bridge answer available for reaction")
        wait_for_json_state(
            self.state_dir / "state.json",
            lambda value: not value.get("pending", {}).get(AGENT_KEY),
            self.args.timeout,
            description="pending requests before silent reaction probe",
        )
        await asyncio.sleep(0.2)
        await owner.sync_once()
        seen_bridge_events = {
            message["event_id"]
            for message in owner.messages
            if message["sender"] == BRIDGE_USER
        }
        response = await owner.client.room_send(
            room_id,
            "m.reaction",
            {
                "m.relates_to": {
                    "rel_type": "m.annotation",
                    "event_id": target["event_id"],
                    "key": "👍",
                }
            },
            ignore_unverified_devices=True,
        )
        if not isinstance(response, RoomSendResponse):
            raise CheckFailure(f"owner reaction failed: {response}")
        await owner.sync_until(
            lambda: any(
                "Matrix-Reaktion" in item["prompt"]
                for item in json.loads(
                    (self.fake_state / "fake-openrig.json").read_text()
                )["prompts"]
            ),
            self.args.timeout,
            description="reaction feedback prompt",
        )
        wait_for_json_state(
            self.state_dir / "state.json",
            lambda value: not value.get("pending", {}).get(AGENT_KEY),
            self.args.timeout,
            description="silent reaction processing",
        )
        await asyncio.sleep(0.2)
        await owner.sync_once()
        unexpected = [
            {
                "event_id": message["event_id"],
                "body": message["body"],
                "relation": message["source"]
                .get("content", {})
                .get("m.relates_to"),
            }
            for message in owner.messages
            if message["sender"] == BRIDGE_USER
            and message["event_id"] not in seen_bridge_events
        ]
        if unexpected:
            raise CheckFailure(
                f"reaction feedback created visible bridge messages: {unexpected}"
            )
        self.record(
            "silent-reaction-feedback",
            "pass",
            (
                "Owner reaction reached the fake OpenRig transport as silent "
                "feedback and did not create a visible bot reply."
            ),
        )

    async def bridge_acknowledgements(
        self,
        room_id: str,
        event_id: str,
    ) -> list[dict[str, Any]]:
        """Read the homeserver's annotation relations for one event."""
        assert self.owner is not None
        url = (
            f"{self.homeserver}/_matrix/client/v1/rooms/{quote(room_id)}"
            f"/relations/{quote(event_id)}/m.annotation/m.reaction?limit=50"
        )
        async with (
            aiohttp.ClientSession() as session,
            session.get(
                url,
                headers={"Authorization": f"Bearer {self.owner.token}"},
            ) as response,
        ):
            value = await response.json()
            if response.status != 200:
                raise CheckFailure(f"relations lookup failed: {value}")
        return [
            event
            for event in value.get("chunk", [])
            if event.get("sender") == BRIDGE_USER
        ]

    async def stranger_send(self, room_id: str, body: str) -> str:
        assert self.owner is not None
        invite = await self.owner.client.room_invite(room_id, STRANGER_USER)
        if hasattr(invite, "status_code") and invite.status_code:
            raise CheckFailure(f"stranger invite failed: {invite}")
        token, _device = await self.http_login(
            "stranger",
            STRANGER_PASSWORD,
            "OPENRIG_STRANGER",
        )
        headers = {"Authorization": f"Bearer {token}"}
        base = f"{self.homeserver}/_matrix/client/v3/rooms/{quote(room_id)}"
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.post(f"{base}/join", json={}) as response:
                if response.status != 200:
                    raise CheckFailure(
                        f"stranger join failed: {await response.json()}"
                    )
            async with session.put(
                f"{base}/send/m.room.message/{uuid.uuid4().hex}",
                json={"msgtype": "m.text", "body": body},
            ) as response:
                value = await response.json()
                if response.status != 200:
                    raise CheckFailure(f"stranger send failed: {value}")
            async with session.post(f"{base}/leave", json={}) as response:
                if response.status != 200:
                    raise CheckFailure(
                        f"stranger leave failed: {await response.json()}"
                    )
        return str(value["event_id"])

    async def check_acceptance_reaction(self, room_id: str) -> None:
        assert self.owner is not None
        owner = self.owner
        wait_for_json_state(
            self.state_dir / "state.json",
            lambda value: not value.get("pending", {}).get(AGENT_KEY),
            self.args.timeout,
            description="pending requests before acceptance-reaction probe",
        )

        async def answered(prompt: str) -> None:
            await owner.sync_until(
                lambda: any(
                    message["sender"] == BRIDGE_USER
                    and f"Fake integration answer: {prompt}" in str(message["body"])
                    for message in owner.messages
                ),
                self.args.timeout,
                description=f"answer for {prompt!r}",
            )
            wait_for_json_state(
                self.state_dir / "state.json",
                lambda value: not value.get("pending", {}).get(AGENT_KEY),
                self.args.timeout,
                description=f"pending queue to drain after {prompt!r}",
            )

        async def exactly_one(event_id: str, label: str) -> str:
            reactions = await self.bridge_acknowledgements(room_id, event_id)
            if len(reactions) != 1:
                raise CheckFailure(
                    f"{label} received {len(reactions)} bridge reactions"
                )
            relation = reactions[0].get("content", {}).get("m.relates_to", {})
            if (
                relation.get("rel_type") != "m.annotation"
                or relation.get("key") != ACCEPTANCE_REACTION_KEY
                or relation.get("event_id") != event_id
            ):
                raise CheckFailure(f"{label} reaction is malformed: {relation}")
            return str(reactions[0]["event_id"])

        plain_prompt = "acceptance reaction probe"
        plain_event = await owner.send_text(room_id, plain_prompt)
        await answered(plain_prompt)
        plain_reaction = await exactly_one(plain_event, "accepted owner prompt")

        self.stop_bridge()
        self.start_bridge(crash_after="ack_send")
        await self.wait_for_steering_mode_ready(room_id)
        crash_prompt = "acceptance reaction crash probe"
        crash_event = await owner.send_text(room_id, crash_prompt)
        self.wait_for_bridge_crash()
        crashed_state = self.bridge_state()
        crashed_record = crashed_state.get("acceptance_reactions", {}).get(
            crash_event,
            {},
        )
        if crashed_record.get("phase") != "sending":
            raise CheckFailure(
                f"ack crash did not leave a pending acknowledgement: {crashed_record}"
            )
        before_restart = await self.bridge_acknowledgements(room_id, crash_event)
        if len(before_restart) != 1:
            raise CheckFailure(
                f"ack crash probe saw {len(before_restart)} reactions before restart"
            )
        self.start_bridge()
        await answered(crash_prompt)
        crash_reaction = await exactly_one(crash_event, "crash-recovered prompt")
        if crash_reaction != before_restart[0]["event_id"]:
            raise CheckFailure("restart replaced the acceptance reaction")
        recovered = self.bridge_state()["acceptance_reactions"][crash_event]
        if recovered.get("phase") != "sent" or (
            recovered.get("reaction_event_id") not in {None, crash_reaction}
        ):
            raise CheckFailure(f"ack recovery state is wrong: {recovered}")
        if await exactly_one(plain_event, "prompt after restart") != plain_reaction:
            raise CheckFailure("restart changed an earlier acceptance reaction")

        stranger_body = "untrusted acceptance probe"
        stranger_event = await self.stranger_send(room_id, stranger_body)
        await owner.sync_until(
            lambda: any(
                message["event_id"] == stranger_event for message in owner.messages
            ),
            self.args.timeout,
            description="untrusted message delivered to the room",
        )
        await asyncio.sleep(2.0)
        if await self.bridge_acknowledgements(room_id, stranger_event):
            raise CheckFailure("untrusted sender received an acceptance reaction")
        state = self.bridge_state()
        acknowledgements = state.get("acceptance_reactions", {})
        if stranger_event in acknowledgements:
            raise CheckFailure("untrusted event was recorded as accepted")
        fake = json.loads((self.fake_state / "fake-openrig.json").read_text())
        if any(stranger_body in str(item["prompt"]) for item in fake["prompts"]):
            raise CheckFailure("untrusted message reached the OpenRig transport")

        batches: dict[str, list[str]] = {}
        for sample in state.get("latency_samples", []):
            if sample.get("kind") in {"steer", "late_steer", "fallback"}:
                batches.setdefault(str(sample.get("request_id")), []).append(
                    str(sample.get("event_id"))
                )
        batch_events = [
            event_id
            for event_ids in batches.values()
            if len(event_ids) > 1
            for event_id in event_ids
        ]
        if len(batch_events) < 2:
            raise CheckFailure(
                f"no multi-event steering batch to verify: {batches}"
            )
        accepted = [
            event_id
            for event_id, record in acknowledgements.items()
            if isinstance(record, dict)
        ]
        not_sent = {
            event_id: record.get("phase")
            for event_id, record in acknowledgements.items()
            if isinstance(record, dict) and record.get("phase") != "sent"
        }
        if not_sent:
            raise CheckFailure(f"acceptance reactions not sent: {not_sent}")
        for event_id in sorted(set(accepted) | set(batch_events)):
            await exactly_one(event_id, f"accepted event {event_id}")
        self.record(
            "acceptance-reaction",
            "pass",
            (
                "Synapse's relation index showed exactly one bridge `m.annotation` "
                f"reaction with key {ACCEPTANCE_REACTION_KEY} on each of "
                f"{len(accepted)} accepted owner events, including "
                f"{len(batch_events)} events joined into steering batches. A "
                "bridge crash immediately after the reaction send left the "
                "acknowledgement pending; restart reused the persisted "
                "transaction and still showed one reaction. An untrusted room "
                "member's message received no reaction, no acceptance record "
                "and no OpenRig prompt."
            ),
        )

    async def check_acceptance_reaction_default_off(self, room_id: str) -> None:
        assert self.owner is not None
        owner = self.owner
        self.stop_bridge()
        self.set_openrig_options(acceptance_reaction_enabled=None)
        self.start_bridge()
        await self.wait_for_steering_mode_ready(room_id)
        typing_before = len(owner.typing)
        prompt = "default receipt-off probe"
        event_id = await owner.send_text(room_id, prompt)
        await owner.sync_until(
            lambda: any(
                message["sender"] == BRIDGE_USER
                and f"Fake integration answer: {prompt}" in str(message["body"])
                for message in owner.messages
            ),
            self.args.timeout,
            description="answer with the acceptance receipt disabled",
        )
        wait_for_json_state(
            self.state_dir / "state.json",
            lambda value: not value.get("pending", {}).get(AGENT_KEY),
            self.args.timeout,
            description="pending queue to drain with the receipt disabled",
        )
        if not any(
            BRIDGE_USER in item["users"] for item in owner.typing[typing_before:]
        ):
            raise CheckFailure("bridge typing was not observed with the receipt off")
        fake = json.loads((self.fake_state / "fake-openrig.json").read_text())
        qitems = [
            item for item in fake["queue"].values() if prompt in str(item.get("body"))
        ]
        if len(qitems) != 1 or qitems[0]["state"] != "done":
            raise CheckFailure(f"receipt-off request qitem is wrong: {qitems}")
        await asyncio.sleep(1.0)
        if await self.bridge_acknowledgements(room_id, event_id):
            raise CheckFailure("default configuration sent an acceptance reaction")
        if event_id in self.bridge_state().get("acceptance_reactions", {}):
            raise CheckFailure("default configuration recorded an acceptance")
        self.stop_bridge()
        self.set_openrig_options(acceptance_reaction_enabled=True)
        self.start_bridge()
        await self.wait_for_steering_mode_ready(room_id)
        self.record(
            "acceptance-reaction-default-off",
            "pass",
            (
                "With `openrig.acceptance_reaction_enabled` removed from the "
                "bridge config (the default), an owner message was queued, the "
                "bridge typing indicator was observed, the answer arrived and "
                "its single qitem completed, while Synapse's relation index "
                "showed no bridge reaction and the bridge recorded no "
                "acceptance. The room status topic was not separately asserted "
                "for this probe."
            ),
        )

    async def check_images(self, room_id: str) -> None:
        assert self.owner is not None
        owner = self.owner
        png = (
            ROOT
            / "src"
            / "openrig_matrix_bridge"
            / "assets"
            / "avatars"
            / "openrig-01.png"
        ).read_bytes()
        await owner.send_encrypted_file(
            room_id,
            png,
            msgtype="m.image",
            filename="owner-image.png",
            mimetype="image/png",
            body="encrypted owner image",
        )
        await owner.sync_until(
            lambda: any(
                "Bild von @owner:localhost" in item["prompt"]
                for item in json.loads(
                    (self.fake_state / "fake-openrig.json").read_text()
                )["prompts"]
            ),
            self.args.timeout,
            description="forwarded owner image",
        )
        inbox_files = list((self.state_dir / "media-inbox" / AGENT_KEY).glob("*.png"))
        if len(inbox_files) != 1 or inbox_files[0].read_bytes() != png:
            raise CheckFailure("decrypted owner image was not stored intact")

        outbound_count = len(owner.images)
        outbound = self.cli(
            "send-image",
            "--agent",
            AGENT_KEY,
            "--path",
            str(ROOT / "src/openrig_matrix_bridge/assets/avatars/openrig-02.png"),
            "--caption",
            "encrypted outbound image",
            "--wait-seconds",
            "15",
            check=False,
        )
        if outbound.returncode != 0:
            raise CheckFailure(f"send-image failed: {outbound.stdout}{outbound.stderr}")
        await owner.sync_until(
            lambda: len(owner.images) > outbound_count,
            self.args.timeout,
            description="outbound encrypted image",
        )
        decrypted = await owner.download_media(owner.images[-1])
        expected = (
            ROOT
            / "src"
            / "openrig_matrix_bridge"
            / "assets"
            / "avatars"
            / "openrig-02.png"
        ).read_bytes()
        if decrypted != expected:
            raise CheckFailure("outbound image did not decrypt to identical bytes")
        self.record(
            "encrypted-images",
            "pass",
            (
                "Owner PNG was decrypted, signature-checked, stored byte-for-byte, "
                "and forwarded; `send-image` produced an encrypted Matrix image "
                "that the owner client downloaded and decrypted byte-for-byte."
            ),
        )

    async def check_files(self, room_id: str) -> None:
        assert self.owner is not None
        owner = self.owner
        pdf = b"%PDF-1.7\nQuarterly integration report\n%%EOF\n"
        unknown = b"\x00\xffopenrig unknown binary\x10"
        ordinary_audio = b"OggS ordinary meeting recording"
        prompts_path = self.fake_state / "fake-openrig.json"

        await owner.send_encrypted_file(
            room_id,
            pdf,
            msgtype="m.file",
            filename="../../Quarterly report.pdf",
            mimetype="application/pdf",
            body="Quarterly report",
        )
        await owner.sync_until(
            lambda: any(
                'Originaldateiname: "../../Quarterly report.pdf"' in item["prompt"]
                and "Medientyp: application/pdf" in item["prompt"]
                and f"Größe: {len(pdf)} Bytes" in item["prompt"]
                for item in json.loads(prompts_path.read_text())["prompts"]
            ),
            self.args.timeout,
            description="inbound PDF attachment metadata",
        )
        pdf_files = list((self.state_dir / "media-inbox" / AGENT_KEY).glob("*.pdf"))
        if len(pdf_files) != 1 or pdf_files[0].read_bytes() != pdf:
            raise CheckFailure("inbound PDF was not stored intact")
        if pdf_files[0].parent != self.state_dir / "media-inbox" / AGENT_KEY:
            raise CheckFailure("inbound PDF escaped the generated agent inbox")

        await owner.send_encrypted_file(
            room_id,
            unknown,
            msgtype="m.file",
            filename="../../payload.custom",
            mimetype="application/x-openrig-unknown",
            body="Unknown payload",
        )
        await owner.sync_until(
            lambda: any(
                'Originaldateiname: "../../payload.custom"' in item["prompt"]
                and "Medientyp: application/x-openrig-unknown" in item["prompt"]
                for item in json.loads(prompts_path.read_text())["prompts"]
            ),
            self.args.timeout,
            description="inbound unknown attachment metadata",
        )
        unknown_files = list(
            (self.state_dir / "media-inbox" / AGENT_KEY).glob("*.custom")
        )
        if len(unknown_files) != 1 or unknown_files[0].read_bytes() != unknown:
            raise CheckFailure("inbound unknown file was not stored intact")

        await owner.send_encrypted_file(
            room_id,
            ordinary_audio,
            msgtype="m.audio",
            filename="meeting.ogg",
            mimetype="audio/ogg",
            body="meeting.ogg",
        )
        await owner.sync_until(
            lambda: any(
                'Originaldateiname: "meeting.ogg"' in item["prompt"]
                and "Medientyp: audio/ogg" in item["prompt"]
                for item in json.loads(prompts_path.read_text())["prompts"]
            ),
            self.args.timeout,
            description="markerless audio generic attachment",
        )
        audio_files = list((self.state_dir / "media-inbox" / AGENT_KEY).glob("*.ogg"))
        if len(audio_files) != 1 or audio_files[0].read_bytes() != ordinary_audio:
            raise CheckFailure("markerless audio was not stored as a generic file")

        outbound_pdf = self.work / "outbound report.pdf"
        outbound_pdf.write_bytes(pdf)
        pdf_count = len(owner.files)
        outbound = self.cli(
            "send-file",
            "--agent",
            AGENT_KEY,
            "--path",
            str(outbound_pdf),
            "--caption",
            "Encrypted PDF from the agent",
            "--wait-seconds",
            "15",
            check=False,
        )
        if outbound.returncode != 0:
            raise CheckFailure(
                f"send-file PDF failed: {outbound.stdout}{outbound.stderr}"
            )
        await owner.sync_until(
            lambda: len(owner.files) > pdf_count,
            self.args.timeout,
            description="outbound encrypted PDF",
        )
        pdf_event = owner.files[-1]
        pdf_content = pdf_event.source.get("content", {})
        if pdf_content.get("msgtype") != "m.file":
            raise CheckFailure(f"outbound PDF was not a generic file: {pdf_content}")
        if pdf_content.get("filename") != outbound_pdf.name:
            raise CheckFailure(f"outbound PDF filename changed: {pdf_content}")
        if pdf_content.get("info") != {
            "mimetype": "application/pdf",
            "size": len(pdf),
        }:
            raise CheckFailure(
                f"outbound PDF metadata changed: {pdf_content.get('info')}"
            )
        if await owner.download_media(pdf_event) != pdf:
            raise CheckFailure("outbound PDF did not decrypt byte-for-byte")
        pdf_request_id = str(outbound.stdout).split()[0]
        pdf_record = self.bridge_state()["media"][pdf_request_id]
        pdf_snapshot = Path(pdf_record["snapshot_path"])
        if (
            pdf_snapshot.parent != self.state_dir / "media-outbox" / "files"
            or pdf_snapshot.stat().st_mode & 0o777 != 0o600
            or pdf_snapshot.read_bytes() != pdf
        ):
            raise CheckFailure(
                "outbound PDF did not use its private immutable snapshot"
            )
        raw_pdf = await owner.raw_event(room_id, pdf_event.event_id)
        if raw_pdf.get("type") != "m.room.encrypted" or not raw_pdf.get(
            "content", {}
        ).get("ciphertext"):
            raise CheckFailure("outbound PDF was not encrypted on the Matrix wire")

        outbound_unknown = self.work / "payload.custom"
        outbound_unknown.write_bytes(unknown)
        unknown_count = len(owner.files)
        outbound = self.cli(
            "send-file",
            "--agent",
            AGENT_KEY,
            "--path",
            str(outbound_unknown),
            "--mime-type",
            "application/x-openrig-unknown",
            "--wait-seconds",
            "15",
            check=False,
        )
        if outbound.returncode != 0:
            raise CheckFailure(
                f"send-file unknown binary failed: {outbound.stdout}{outbound.stderr}"
            )
        await owner.sync_until(
            lambda: len(owner.files) > unknown_count,
            self.args.timeout,
            description="outbound encrypted unknown binary",
        )
        unknown_event = owner.files[-1]
        unknown_content = unknown_event.source.get("content", {})
        if (
            unknown_content.get("msgtype") != "m.file"
            or unknown_content.get("filename") != outbound_unknown.name
            or unknown_content.get("info", {}).get("mimetype")
            != "application/x-openrig-unknown"
        ):
            raise CheckFailure(
                f"outbound unknown binary metadata changed: {unknown_content}"
            )
        if await owner.download_media(unknown_event) != unknown:
            raise CheckFailure("outbound unknown binary did not decrypt byte-for-byte")

        self.record(
            "encrypted-files",
            "pass",
            (
                "Encrypted PDF and unknown binary attachments were transferred in "
                "both directions through disposable Synapse, and markerless audio "
                "used the generic attachment path. Inbound files were stored under "
                "generated agent inbox paths with original metadata; outbound m.file "
                "events used private snapshots with preserved filename, MIME type "
                "and size, decrypted byte-for-byte, and were Megolm-encrypted on "
                "the wire."
            ),
        )

        recovery = self.work / "restart recovery.pdf"
        recovery.write_bytes(pdf + b"restart\n")
        self.stop_bridge()
        queued = self.cli(
            "send-file",
            "--agent",
            AGENT_KEY,
            "--path",
            str(recovery),
            "--wait-seconds",
            "0",
            check=False,
        )
        if queued.returncode != 1 or "queued file-" not in queued.stdout:
            raise CheckFailure(
                f"pending attachment was not queued: {queued.stdout}{queued.stderr}"
            )
        pending_requests = list((self.state_dir / "media-outbox").glob("file-*.json"))
        if len(pending_requests) != 1:
            raise CheckFailure(
                f"expected one pending attachment request, found {pending_requests}"
            )
        request_id = pending_requests[0].stem
        pending_request = json.loads(pending_requests[0].read_text())
        recovery_snapshot = Path(pending_request["snapshot_path"])
        recovery_bytes = recovery_snapshot.read_bytes()
        recovery.write_bytes(b"mutated source after admission")
        recovery_count = len(owner.files)
        self.start_bridge()
        deadline = time.monotonic() + self.args.timeout
        while time.monotonic() < deadline:
            await owner.sync_once()
            state = self.bridge_state()
            failure = state.get("media_failures", {}).get(request_id)
            if failure:
                raise CheckFailure(
                    f"restart attachment delivery failed explicitly: {failure['error']}"
                )
            if len(owner.files) > recovery_count:
                break
            await asyncio.sleep(0.1)
        else:
            raise CheckFailure(
                "timed out waiting for outbound attachment after bridge restart"
            )
        await asyncio.sleep(1.5)
        await owner.sync_once()
        if len(owner.files) != recovery_count + 1:
            raise CheckFailure(
                "restart recovery delivered the pending attachment more than once"
            )
        recovery_event = owner.files[-1]
        if await owner.download_media(recovery_event) != recovery_bytes:
            raise CheckFailure(
                "restart-recovered attachment did not use the admitted snapshot"
            )
        state = self.bridge_state()
        delivered = state.get("media", {}).get(request_id)
        if (
            not delivered
            or not str(delivered.get("event_id") or "").startswith("$")
            or delivered.get("event_id") != recovery_event.event_id
        ):
            raise CheckFailure(
                f"restart recovery lacks the delivered Matrix event ID: {delivered}"
            )
        self.record(
            "attachment-restart-recovery",
            "pass",
            (
                "A generic-file request was durably queued while the bridge was "
                "stopped, then delivered exactly once after restart. The persisted "
                "success record names the same Matrix event ID observed by the owner; "
                "the delivered bytes came from the private snapshot even after the "
                "mutable source was replaced."
            ),
        )

    async def build_gallery(
        self,
        tag: str,
    ) -> tuple[list[dict[str, Any]], list[tuple[str, bytes, bytes]]]:
        """Four encrypted HEIC items with JPEG previews, like Element X sends."""
        assert self.owner is not None
        items: list[dict[str, Any]] = []
        expected: list[tuple[str, bytes, bytes]] = []
        for number in (5667, 5666, 5665, 5664):
            name = f"{tag}-{number}.heic"
            original = (
                b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00"
                + f"{tag}:{number}:".encode()
                + bytes(range(256))
            )
            preview = b"\xff\xd8\xff\xe0" + f"{tag}:{number}".encode() + b"\xff\xd9"
            items.append(
                {
                    "itemtype": "m.image",
                    "body": name,
                    "filename": name,
                    "file": await self.owner.upload_encrypted(
                        original,
                        filename=name,
                        mimetype="image/heic",
                    ),
                    "info": {
                        "mimetype": "image/heic",
                        "size": len(original),
                        "w": 3024,
                        "h": 4032,
                        "thumbnail_file": await self.owner.upload_encrypted(
                            preview,
                            filename=f"{name}.jpg",
                            mimetype="image/jpeg",
                        ),
                        "thumbnail_info": {
                            "mimetype": "image/jpeg",
                            "size": len(preview),
                            "w": 600,
                            "h": 800,
                        },
                    },
                }
            )
            expected.append((name, original, preview))
        return items, expected

    async def verify_gallery_delivery(
        self,
        event_id: str,
        expected: list[tuple[str, bytes, bytes]],
    ) -> str:
        assert self.owner is not None
        owner = self.owner
        first_name = expected[0][0]
        await owner.sync_until(
            lambda: any(
                message["sender"] == BRIDGE_USER
                and "Fake integration answer: Galerie von" in str(message["body"])
                and event_id
                in {
                    relation.get("event_id"),
                    relation.get("m.in_reply_to", {}).get("event_id"),
                }
                for message in owner.messages
                for relation in [
                    message["source"].get("content", {}).get("m.relates_to") or {}
                ]
            ),
            self.args.timeout,
            description="answer for the gallery prompt",
        )
        wait_for_json_state(
            self.state_dir / "state.json",
            lambda value: not value.get("pending", {}).get(AGENT_KEY),
            self.args.timeout,
            description="pending queue to drain after the gallery",
        )
        fake = json.loads((self.fake_state / "fake-openrig.json").read_text())
        prompts = [
            str(item["prompt"])
            for item in fake["prompts"]
            if first_name in str(item["prompt"])
        ]
        if len(prompts) != 1:
            raise CheckFailure(f"gallery reached the agent {len(prompts)} times")
        prompt = prompts[0]
        positions: list[int] = []
        inbox = self.state_dir / "media-inbox" / AGENT_KEY
        for index, (name, original, preview) in enumerate(expected, start=1):
            marker = f'{index}. "{name}" — image/heic, {len(original)} Bytes, 3024×4032'
            if marker not in prompt:
                raise CheckFailure(f"gallery prompt lacks item {index}: {prompt}")
            positions.append(prompt.index(marker))
            block = prompt[prompt.index(marker) :].split("\n\n")[0]
            local = Path(block.split("Lokale Datei: ")[1].split("\n")[0])
            shown = Path(block.split("Vorschau: ")[1].split(" ")[0])
            if local.parent != inbox or shown.parent != inbox:
                raise CheckFailure("gallery item escaped the generated agent inbox")
            if local.read_bytes() != original:
                raise CheckFailure(f"gallery item {index} bytes differ")
            if shown.read_bytes() != preview:
                raise CheckFailure(f"gallery preview {index} bytes differ")
            stored_copies = [
                path for path in inbox.glob("*.heic") if path.read_bytes() == original
            ]
            if len(stored_copies) != 1:
                raise CheckFailure(
                    f"gallery item {index} stored {len(stored_copies)} times"
                )
        if positions != sorted(positions):
            raise CheckFailure("gallery items reached the agent out of order")
        qitems = [
            item
            for item in fake["queue"].values()
            if first_name in str(item.get("body") or "")
        ]
        if len(qitems) != 1 or qitems[0]["state"] != "done":
            raise CheckFailure(f"gallery qitem is wrong: {qitems}")
        validate_qitem_history(qitems[0])
        return prompt

    async def check_gallery(self, room_id: str) -> None:
        assert self.owner is not None
        items, expected = await self.build_gallery("gallery")
        event_id = await self.owner.send_gallery(
            room_id,
            items,
            body="Listing photos",
        )
        prompt = await self.verify_gallery_delivery(event_id, expected)
        if "Bildunterschrift: Listing photos" not in prompt:
            raise CheckFailure("gallery caption did not reach the agent")
        self.record(
            "element-x-gallery",
            "pass",
            (
                "One encrypted dm.filament.gallery event with four HEIC items and "
                "JPEG thumbnails reached the fake transport as one prompt and one "
                "completed qitem. The prompt listed every item in source order "
                "with filename, MIME type, size and dimensions; each original and "
                "each preview was decrypted into the generated agent inbox "
                "byte-for-byte, and the answer was threaded to the gallery event."
            ),
        )

    async def check_gallery_restart(self, room_id: str) -> None:
        assert self.owner is not None
        self.stop_bridge()
        self.start_bridge(crash_after="gallery_item")
        await self.wait_for_steering_mode_ready(room_id)
        items, expected = await self.build_gallery("restart")
        event_id = await self.owner.send_gallery(room_id, items, body="")
        self.wait_for_bridge_crash()
        record = self.bridge_state().get("galleries", {}).get(event_id, {})
        phases = [item.get("phase") for item in record.get("items", [])]
        if record.get("phase") != "downloading" or phases != [
            "stored",
            "pending",
            "pending",
            "pending",
        ]:
            raise CheckFailure(f"gallery crash left unexpected state: {phases}")
        self.start_bridge()
        await self.verify_gallery_delivery(event_id, expected)
        self.record(
            "gallery-restart",
            "pass",
            (
                "The bridge exited immediately after persisting the first of four "
                "gallery items. Restart resumed the remaining three without "
                "downloading the first again: each original exists exactly once in "
                "the agent inbox, the single prompt lists all four in order, and one "
                "qitem completed."
            ),
        )

    async def origin_ts(self, room_id: str, event_id: str) -> int:
        assert self.owner is not None
        return int((await self.owner.raw_event(room_id, event_id))["origin_server_ts"])

    async def use_production_bursts(self, room_id: str, **bridge_options: Any) -> None:
        """Restart on the production burst defaults with the 👍 receipt off.

        Every related-context scenario also starts beyond the inactivity
        boundary after the owner's previous event, so scenarios never join.
        """
        self.set_openrig_options(
            burst_inactivity_seconds=None,
            burst_max_span_seconds=None,
            acceptance_reaction_enabled=None,
        )
        self.stop_bridge()
        self.start_bridge(**bridge_options)
        await self.wait_for_steering_mode_ready(room_id)
        await self.isolate_burst()

    async def isolate_burst(self) -> None:
        assert self.owner is not None
        await self.sync_for(
            self.owner.last_sent_at + RELATED_BOUNDARY_SECONDS + 1 - time.monotonic()
        )

    async def sync_for(self, seconds: float) -> None:
        """Wait while the owner keeps syncing, so no timeline event is skipped."""
        assert self.owner is not None
        deadline = time.monotonic() + seconds
        while (remaining := deadline - time.monotonic()) > 0:
            await self.owner.sync_once(
                timeout_ms=max(1, int(min(remaining, 1.0) * 1000)),
                timeline_limit=RELATED_TIMELINE_LIMIT,
            )

    def scenario_start(self) -> dict[str, Any]:
        assert self.owner is not None
        return {"since": time.time(), "timeline": len(self.owner.timeline)}

    def fake_transport(self) -> dict[str, Any]:
        return json.loads((self.fake_state / "fake-openrig.json").read_text())

    async def wait_for_parts_done(self, part_event_ids: list[str]) -> dict[str, Any]:
        """Wait until every part completed exactly one qitem and the queue drained.

        The owner keeps syncing meanwhile: a long gap between syncs returns a
        limited timeline and would hide intermediate status events.
        """
        assert self.owner is not None

        def part_qitems(fake: dict[str, Any], event_id: str) -> list[dict[str, Any]]:
            return [
                item
                for item in fake["queue"].values()
                if f"Event: {event_id}\n" in str(item.get("body") or "")
            ]

        def complete(value: dict[str, Any]) -> bool:
            if value.get("pending", {}).get(AGENT_KEY):
                return False
            fake = self.fake_transport()
            return all(
                [item.get("state") for item in part_qitems(fake, event_id)] == ["done"]
                for event_id in part_event_ids
            )

        deadline = time.monotonic() + self.args.timeout
        while True:
            await self.owner.sync_once(
                timeout_ms=500,
                timeline_limit=RELATED_TIMELINE_LIMIT,
            )
            with suppress(FileNotFoundError, json.JSONDecodeError):
                if complete(self.bridge_state()):
                    break
            if time.monotonic() > deadline:
                raise CheckFailure(
                    "every related part did not complete exactly one qitem within "
                    f"{self.args.timeout}s"
                )
        fake = self.fake_transport()
        for event_id in part_event_ids:
            validate_qitem_history(part_qitems(fake, event_id)[0])
        return fake

    def bridge_answers(self, under: list[str]) -> list[dict[str, Any]]:
        assert self.owner is not None
        return [
            message
            for message in self.owner.messages
            if message["sender"] == BRIDGE_USER
            and str(message["body"]).startswith(
                ("Fake integration answer", "Late steering answer")
            )
            and (message["source"].get("content", {}).get("m.relates_to") or {}).get(
                "event_id"
            )
            in under
        ]

    async def verify_related_exchange(
        self,
        room_id: str,
        anchor_event_id: str,
        part_event_ids: list[str],
        markers: list[str],
        start: dict[str, Any],
    ) -> tuple[list[str], list[dict[str, Any]]]:
        """Prove a related exchange reached the agent whole, in order and once.

        Every part completes exactly one qitem; the agent inputs since `start`
        carry every part in source order and linked to the first; every named
        file matches its SHA-256; every answer is threaded under the anchor and
        one answer names all parts as its sources; the room status showed the
        work and returned to ready, typing ended, and no 👍 receipt was sent
        (it is off by default).
        """
        assert self.owner is not None
        owner = self.owner
        fake = await self.wait_for_parts_done(part_event_ids)
        inputs = [
            item
            for item in agent_inputs(fake)
            if float(item.get("sent_at") or 0) >= start["since"]
            and any(marker in str(item["prompt"]) for marker in markers)
        ]
        kinds = validate_linked_inputs(inputs, anchor_event_id, markers)
        for item in inputs:
            verify_attachment_digests(str(item["prompt"]))

        def covering(message: dict[str, Any]) -> bool:
            content = message["source"].get("content", {})
            sources = content.get("dev.openrig.source_event_ids") or []
            return set(part_event_ids) <= set(sources)

        def last_status() -> str | None:
            return next(
                (
                    entry["state"]
                    for entry in reversed(owner.timeline[start["timeline"] :])
                    if entry["kind"] == "status"
                ),
                None,
            )

        await owner.sync_until(
            lambda: any(covering(item) for item in self.bridge_answers(part_event_ids))
            and str(owner.client.rooms[room_id].topic).startswith("🟢 bereit")
            and last_status() == "ready",
            self.args.timeout,
            description="an answer for every related part and a ready room",
        )
        for message in self.bridge_answers(part_event_ids):
            relation = message["source"]["content"]["m.relates_to"]
            if relation.get("event_id") != anchor_event_id:
                raise CheckFailure(
                    "an answer to a related part was threaded under "
                    f"{relation.get('event_id')}, not under the first part"
                )
        # Typing is ephemeral and short fake turns can start and end between
        # owner syncs; the status is a timeline state event and is never missed.
        feedback = owner.timeline[start["timeline"] :]
        statuses = [entry["state"] for entry in feedback if entry["kind"] == "status"]
        if "busy" not in statuses or statuses[-1] != "ready":
            raise CheckFailure(f"room status did not go busy -> ready: {statuses}")
        typing = [entry for entry in feedback if entry["kind"] == "typing"]
        if typing and typing[-1]["bridge"]:
            raise CheckFailure("bridge typing stayed on after the exchange finished")
        receipts = [
            reaction
            for reaction in owner.reactions
            if reaction.reacts_to in part_event_ids
            and reaction.key == ACCEPTANCE_REACTION_KEY
        ]
        if receipts:
            raise CheckFailure("the opt-in 👍 receipt appeared although it is off")
        return kinds, inputs

    async def check_related_gallery_text(self, room_id: str) -> None:
        assert self.owner is not None
        await self.use_production_bursts(room_id)
        outcomes: dict[str, Any] = {}
        for tag, gap in (("related-1s", 1.0), ("related-30s", 30.0)):
            await self.isolate_burst()
            start = self.scenario_start()
            items, expected = await self.build_gallery(tag)
            gallery_id = await self.owner.send_gallery(room_id, items, body="")
            await (self.sync_for(gap) if gap > 5 else asyncio.sleep(gap))
            text = f"{tag}: please describe these photos"
            text_id = await self.owner.send_text(room_id, text)
            kinds, inputs = await self.verify_related_exchange(
                room_id,
                gallery_id,
                [gallery_id, text_id],
                [expected[0][0], text],
                start,
            )
            if gap > 15:
                carrying = [item for item in inputs if text in str(item["prompt"])]
                if kinds[1] != "prompt" or not all(
                    name in str(carrying[0]["prompt"]) for name, *_rest in expected
                ):
                    raise CheckFailure(
                        "text after the gallery turn ended was not a continuation "
                        "restating every gallery item"
                    )
            outcomes[tag] = {
                "origin_gap_ms": await self.origin_ts(room_id, text_id)
                - await self.origin_ts(room_id, gallery_id),
                "text_delivered_by": kinds[1],
            }
        self.record(
            "related-gallery-then-text",
            "pass",
            (
                "With the production defaults (45 s inactivity, 300 s span), a "
                "top-level four-item gallery followed by a top-level text about "
                "1 s and about 30 s later reached the agent as one related "
                "exchange each time: the text was linked to the gallery, every "
                "item and the text arrived in source order, every local path "
                "matched its SHA-256, each event completed exactly one qitem, and "
                "every answer was threaded under the gallery. After 30 s the "
                "gallery turn had ended, so the text arrived as a continuation "
                "restating all four items."
            ),
            details=outcomes,
        )

    async def check_related_voice_text_attachments(self, room_id: str) -> None:
        assert self.owner is not None
        owner = self.owner
        transcribe_seconds = 4.0
        await self.use_production_bursts(room_id, transcribe_seconds=transcribe_seconds)
        start = self.scenario_start()
        voice_id = await owner.send_encrypted_file(
            room_id,
            b"OggS-related-voice-note",
            msgtype="m.audio",
            filename="voice.ogg",
            mimetype="audio/ogg",
            body="voice.ogg",
            extra_content={"org.matrix.msc3245.voice": {}},
        )
        await asyncio.sleep(1)
        text = "related-voice: and this listing too"
        text_id = await owner.send_text(room_id, text)
        await asyncio.sleep(1)
        image_id = await owner.send_encrypted_file(
            room_id,
            b"\x89PNG\r\n\x1a\n" + b"related voice image",
            msgtype="m.image",
            filename="related-voice.png",
            mimetype="image/png",
            body="related-voice.png",
        )
        await asyncio.sleep(1)
        file_id = await owner.send_encrypted_file(
            room_id,
            b"%PDF-1.4\nrelated voice label\n%%EOF\n",
            msgtype="m.file",
            filename="related-voice.pdf",
            mimetype="application/pdf",
            body="related-voice.pdf",
        )
        kinds, _inputs = await self.verify_related_exchange(
            room_id,
            voice_id,
            [voice_id, text_id, image_id, file_id],
            ["Deterministic local transcript", text, image_id, file_id],
            start,
        )
        self.record(
            "related-voice-text-attachments",
            "pass",
            (
                f"A voice note with {transcribe_seconds:g} s injected transcription "
                "latency, then a text, an image and a PDF one second apart reached "
                "the agent in source order: the text and attachments waited for "
                "the transcript instead of overtaking it, every path matched its "
                "SHA-256, each event completed exactly one qitem, and every "
                "answer was threaded under the voice note."
            ),
            details={"delivered_by": kinds},
        )

    async def check_related_galleries_and_files(self, room_id: str) -> None:
        assert self.owner is not None
        owner = self.owner
        await self.use_production_bursts(room_id)
        start = self.scenario_start()
        first_items, first_expected = await self.build_gallery("related-multi-a")
        first_id = await owner.send_gallery(room_id, first_items, body="")
        await asyncio.sleep(2)
        pdf_id = await owner.send_encrypted_file(
            room_id,
            b"%PDF-1.4\nrelated multi label\n%%EOF\n",
            msgtype="m.file",
            filename="related-multi.pdf",
            mimetype="application/pdf",
            body="related-multi.pdf",
        )
        second_items, _second_expected = await self.build_gallery("related-multi-b")
        second_id = await owner.send_gallery(room_id, second_items, body="")
        await asyncio.sleep(2)
        csv_id = await owner.send_encrypted_file(
            room_id,
            b"sku,price\nwatch-cable,15\n",
            msgtype="m.file",
            filename="related-multi.csv",
            mimetype="text/csv",
            body="related-multi.csv",
        )
        await asyncio.sleep(2)
        text = "related-multi: list everything above"
        text_id = await owner.send_text(room_id, text)
        kinds, _inputs = await self.verify_related_exchange(
            room_id,
            first_id,
            [first_id, pdf_id, second_id, csv_id, text_id],
            [first_expected[0][0], pdf_id, second_id, csv_id, text],
            start,
        )
        self.record(
            "related-galleries-and-files",
            "pass",
            (
                "Two four-item galleries, a PDF, a CSV and a text sent as one "
                "exchange reached the agent linked to the first gallery, in source "
                "order, with every gallery item and file path matching its "
                "SHA-256; each event completed exactly one qitem and every answer "
                "was threaded under the first gallery."
            ),
            details={"delivered_by": kinds},
        )

    async def check_related_restart(self, room_id: str) -> None:
        assert self.owner is not None
        owner = self.owner
        await self.use_production_bursts(room_id)
        start = self.scenario_start()
        items, expected = await self.build_gallery("related-restart")
        gallery_id = await owner.send_gallery(room_id, items, body="")
        await self.wait_for_parts_done([gallery_id])
        # Restart between parts, then crash while the next part downloads.
        self.stop_bridge()
        self.start_bridge(crash_after="media_intake")
        await self.wait_for_steering_mode_ready(room_id)
        pdf = b"%PDF-1.4\nrelated restart label\n%%EOF\n"
        pdf_id = await owner.send_encrypted_file(
            room_id,
            pdf,
            msgtype="m.file",
            filename="related-restart.pdf",
            mimetype="application/pdf",
            body="related-restart.pdf",
        )
        self.wait_for_bridge_crash()
        parts = [
            part
            for burst in self.bridge_state().get("bursts", {}).values()
            for part in burst.get("parts", [])
            if part.get("event_id") == pdf_id
        ]
        if len(parts) != 1 or parts[0].get("phase") != "processing" or not parts[0].get(
            "media"
        ):
            raise CheckFailure(f"the interrupted download was not persisted: {parts}")
        self.start_bridge()
        await self.wait_for_steering_mode_ready(room_id)
        text = "related-restart: anything missing?"
        text_id = await owner.send_text(room_id, text)
        kinds, _inputs = await self.verify_related_exchange(
            room_id,
            gallery_id,
            [gallery_id, pdf_id, text_id],
            [expected[0][0], pdf_id, text],
            start,
        )
        inbox = self.state_dir / "media-inbox" / AGENT_KEY
        copies = [path for path in inbox.iterdir() if path.read_bytes() == pdf]
        if len(copies) != 1:
            raise CheckFailure(f"the resumed PDF is stored {len(copies)} times")
        self.record(
            "related-restart",
            "pass",
            (
                "After a gallery completed, the bridge restarted; the next part, a "
                "PDF, crashed the bridge right after its intake was persisted and "
                "before its download. On restart the download resumed once (one "
                "stored copy), and the PDF and a later text reached the agent "
                "linked to the gallery, in order, with digests verified, one "
                "completed qitem per event, and every answer under the gallery."
            ),
            details={"delivered_by": kinds},
        )

    async def check_related_unrelated_isolation(self, room_id: str) -> None:
        assert self.owner is not None
        owner = self.owner
        await self.use_production_bursts(room_id)
        start = self.scenario_start()
        first = "related-isolation: first question"
        second = "related-isolation: second question"
        reply = "related-isolation: more on the first"
        first_id = await owner.send_text(room_id, first)
        await asyncio.sleep(2)
        second_id = await owner.send_text(room_id, second)
        await asyncio.sleep(2)
        reply_id = await owner.send_text(
            room_id,
            reply,
            relates_to=thread_relation(first_id),
        )
        fake = await self.wait_for_parts_done([first_id, second_id, reply_id])
        inputs = [
            str(item["prompt"])
            for item in agent_inputs(fake)
            if float(item.get("sent_at") or 0) >= start["since"]
        ]
        if second not in inputs:
            raise CheckFailure("the second top-level text was not its own prompt")
        if any(second in prompt and (first in prompt or reply in prompt) for prompt in inputs):
            raise CheckFailure("an unrelated top-level text joined another request")
        await owner.sync_until(
            lambda: bool(self.bridge_answers([second_id]))
            and bool(self.bridge_answers([first_id])),
            self.args.timeout,
            description="answers for both unrelated texts",
        )
        for message in self.bridge_answers([first_id, second_id]):
            sources = message["source"]["content"].get("dev.openrig.source_event_ids")
            if second_id in (sources or []) and first_id in (sources or []):
                raise CheckFailure("one answer claimed both unrelated texts")
        self.record(
            "related-unrelated-isolation",
            "pass",
            (
                "Two plain top-level texts two seconds apart, inside the inactivity "
                "boundary, stayed separate requests with their own answers; a "
                "thread reply to the first joined only the first text's thread."
            ),
        )

    async def check_related_thread(self, room_id: str) -> None:
        assert self.owner is not None
        owner = self.owner
        await self.use_production_bursts(room_id)
        start = self.scenario_start()
        root = "related-thread: listing thread start"
        root_id = await owner.send_text(room_id, root)
        await self.wait_for_parts_done([root_id])
        # Beyond the timing boundary: only the thread relation links what follows.
        await self.sync_for(RELATED_BOUNDARY_SECONDS + 5)
        unrelated = "related-thread: unrelated top-level message"
        unrelated_id = await owner.send_text(room_id, unrelated)
        await asyncio.sleep(1)
        items, expected = await self.build_gallery("related-thread")
        gallery_id = await owner.send_gallery(
            room_id,
            items,
            body="",
            relates_to=thread_relation(root_id),
        )
        await asyncio.sleep(1)
        text = "related-thread: please use these photos"
        text_id = await owner.send_text(
            room_id,
            text,
            relates_to=thread_relation(root_id),
        )
        kinds, inputs = await self.verify_related_exchange(
            room_id,
            root_id,
            [root_id, gallery_id, text_id],
            [root, expected[0][0], text],
            start,
        )
        if any(unrelated in str(item["prompt"]) for item in inputs):
            raise CheckFailure("the unrelated top-level message joined the thread")
        await self.wait_for_parts_done([unrelated_id])
        await owner.sync_until(
            lambda: bool(self.bridge_answers([unrelated_id])),
            self.args.timeout,
            description="the unrelated message's own answer",
        )
        gap_ms = await self.origin_ts(room_id, gallery_id) - await self.origin_ts(
            room_id, root_id
        )
        self.record(
            "related-thread",
            "pass",
            (
                "A gallery and a text sent into an existing thread more than 45 s "
                "after its root joined the root's context by relation: all three "
                "reached the agent in order, linked, digest-verified, once each, "
                "with every answer in the thread. A top-level message sent in "
                "between stayed a separate request with its own answer."
            ),
            details={"root_to_gallery_ms": gap_ms, "delivered_by": kinds},
        )

    async def check_threaded_files(self, room_id: str) -> None:
        assert self.owner is not None
        owner = self.owner
        root_event_id = next(
            message["event_id"]
            for message in owner.messages
            if message["body"] == "thread root request"
        )
        relation = {
            "rel_type": "m.thread",
            "event_id": root_event_id,
            "is_falling_back": True,
            "m.in_reply_to": {"event_id": root_event_id},
        }
        cases = [
            (
                "threaded-report.pdf",
                b"%PDF-1.7\nthreaded integration report\n",
                "m.file",
                "application/pdf",
            ),
            (
                "threaded-meeting.ogg",
                b"OggS threaded markerless audio",
                "m.audio",
                "audio/ogg",
            ),
        ]
        for filename, payload, msgtype, mimetype in cases:
            before = len(owner.messages)
            await owner.send_encrypted_file(
                room_id,
                payload,
                msgtype=msgtype,
                filename=filename,
                mimetype=mimetype,
                body=filename,
                extra_content={"m.relates_to": relation},
            )

            def threaded_file_delivered(
                before_count=before,
            ) -> bool:
                new_messages = owner.messages[before_count:]
                return any(
                    message["sender"] == BRIDGE_USER
                    and "Datei gespeichert" in str(message["body"])
                    for message in new_messages
                ) and any(
                    message["sender"] == BRIDGE_USER
                    and "Fake integration answer: Datei von" in str(message["body"])
                    for message in new_messages
                )

            await owner.sync_until(
                threaded_file_delivered,
                self.args.timeout,
                description=f"threaded {filename} delivery",
            )
            wait_for_json_state(
                self.state_dir / "state.json",
                lambda value: not value.get("pending", {}).get(AGENT_KEY),
                self.args.timeout,
            )
            await asyncio.sleep(0.2)
            await owner.sync_once()
            delivered = [
                message
                for message in owner.messages[before:]
                if message["sender"] == BRIDGE_USER
            ]
            if not delivered:
                raise CheckFailure(f"threaded {filename} produced no bridge messages")
            queue_notice_event_id = (
                self.bridge_state()
                .get("queue_notices", {})
                .get(AGENT_KEY, {})
                .get("event_id")
            )
            thread_sends = set()
            card_edits = set()
            for message in delivered:
                delivered_relation = (
                    message["source"].get("content", {}).get("m.relates_to")
                )
                if not isinstance(delivered_relation, dict):
                    raise CheckFailure(
                        f"threaded {filename} escaped its canonical root: "
                        f"{delivered_relation}"
                    )
                if (
                    delivered_relation.get("rel_type") == "m.replace"
                    and delivered_relation.get("event_id")
                    == queue_notice_event_id
                ):
                    continue
                if delivered_relation.get("rel_type") == "m.thread":
                    if delivered_relation.get("event_id") != root_event_id:
                        raise CheckFailure(
                            f"threaded {filename} escaped its canonical root: "
                            f"{delivered_relation}"
                        )
                    thread_sends.add(message["event_id"])
                elif delivered_relation.get("rel_type") == "m.replace":
                    card_edits.add(str(delivered_relation.get("event_id") or ""))
                else:
                    raise CheckFailure(
                        f"threaded {filename} used an unsafe relation: "
                        f"{delivered_relation}"
                    )
            if not card_edits <= thread_sends:
                raise CheckFailure(
                    f"threaded {filename} edited an unrelated event: {card_edits}"
                )
            fake = json.loads((self.fake_state / "fake-openrig.json").read_text())
            qitems = [
                item
                for item in fake["queue"].values()
                if filename in str(item.get("body") or "")
            ]
            if len(qitems) != 1:
                raise CheckFailure(
                    f"threaded {filename} did not create exactly one qitem"
                )
            validate_qitem_history(qitems[0])
            if qitems[0]["state"] != "done":
                raise CheckFailure(f"threaded {filename} qitem did not close")

        self.record(
            "threaded-files",
            "pass",
            (
                "A PDF and markerless audio sent from inside an existing m.thread "
                "were stored and forwarded. Every file status and final bridge "
                "message used the original canonical thread root, and each input "
                "created one completed qitem."
            ),
        )

    async def check_audio_gap(self, room_id: str) -> None:
        assert self.owner is not None
        owner = self.owner
        config = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        config["transcription"]["enabled"] = False
        self.stop_bridge()
        self.config_path.write_text(
            yaml.safe_dump(config, sort_keys=False),
            encoding="utf-8",
        )
        self.start_bridge()
        before = len(owner.messages)
        await owner.send_encrypted_file(
            room_id,
            b"OggS-integration-audio-placeholder",
            msgtype="m.audio",
            filename="voice.ogg",
            mimetype="audio/ogg",
            body="voice.ogg",
            extra_content={"org.matrix.msc3245.voice": {}},
        )
        await owner.sync_until(
            lambda: any(
                "Sprachnachrichten sind auf dieser Bridge deaktiviert"
                in str(message["body"])
                for message in owner.messages[before:]
            ),
            self.args.timeout,
            description="disabled voice response",
        )
        self.stop_bridge()
        config["transcription"]["enabled"] = True
        self.config_path.write_text(
            yaml.safe_dump(config, sort_keys=False),
            encoding="utf-8",
        )
        self.start_bridge()
        self.record(
            "voice-disabled-path",
            "pass",
            (
                "An encrypted Matrix audio event reached the bridge and received "
                "the configured disabled-voice response after a controlled "
                "configuration restart. Real Parakeet quality remains an explicit "
                "gap; the successful voice path uses a deterministic local "
                "transcriber."
            ),
        )

    async def check_voice_status_edit(self, room_id: str) -> None:
        assert self.owner is not None
        owner = self.owner
        before = len(owner.messages)
        voice_event_id = await owner.send_encrypted_file(
            room_id,
            b"OggS-integration-audio-placeholder",
            msgtype="m.audio",
            filename="voice.ogg",
            mimetype="audio/ogg",
            body="voice.ogg",
            extra_content={"org.matrix.msc3245.voice": {}},
        )
        await owner.sync_until(
            lambda: any(
                "Deterministic local transcript" in str(message["body"])
                and message["sender"] == BRIDGE_USER
                for message in owner.messages[before:]
            ),
            self.args.timeout,
            description="voice status edited to transcript",
        )
        # The status becomes done just before the prompt is enqueued. Wait for
        # that transport boundary before treating an empty pending list as idle.
        try:
            wait_for_json_state(
                self.fake_state / "fake-openrig.json",
                lambda value: any(
                    "Deterministic local transcript" in str(item.get("prompt"))
                    for item in value.get("prompts", [])
                ),
                self.args.timeout,
            )
        except CheckFailure:
            raise CheckFailure(
                "voice transcript was not forwarded to the fake transport"
            ) from None
        wait_for_json_state(
            self.state_dir / "state.json",
            lambda value: not value.get("pending", {}).get(AGENT_KEY),
            self.args.timeout,
        )
        messages = [
            message
            for message in owner.messages[before:]
            if message["sender"] == BRIDGE_USER
        ]
        progress = [
            message
            for message in messages
            if str(message["body"]) == "Sprachnachricht empfangen, transkribiere …"
        ]
        if len(progress) != 1:
            raise CheckFailure(
                f"voice note did not create exactly one progress event: {progress}"
            )
        progress_event_id = progress[0]["event_id"]
        edits = [
            message
            for message in messages
            if isinstance(message.get("source", {}).get("content"), dict)
            and message["source"]["content"].get("m.relates_to", {}).get("rel_type")
            == "m.replace"
            and message["source"]["content"]["m.relates_to"].get("event_id")
            == progress_event_id
        ]
        if len(edits) != 1:
            raise CheckFailure(
                f"voice note did not edit the progress event exactly once: {edits}"
            )
        if "Deterministic local transcript" not in str(edits[0]["body"]):
            raise CheckFailure("voice status edit does not contain the transcript")
        transcript_only = [
            message
            for message in messages
            if "Deterministic local transcript" in str(message["body"])
            and message["event_id"] != edits[0]["event_id"]
        ]
        if transcript_only:
            raise CheckFailure(
                f"voice transcript created a second Matrix message: {transcript_only}"
            )

        raw_progress = await owner.raw_event(room_id, progress_event_id)
        validate_answer_relation(raw_progress, voice_event_id)
        raw_edit = await owner.raw_event(room_id, edits[0]["event_id"])
        relation = raw_edit.get("content", {}).get("m.relates_to")
        if relation != {"rel_type": "m.replace", "event_id": progress_event_id}:
            raise CheckFailure(f"voice edit relation is wrong: {relation}")

        fake = json.loads((self.fake_state / "fake-openrig.json").read_text())
        prompts = [
            item
            for item in fake["prompts"]
            if "Deterministic local transcript" in item["prompt"]
        ]
        if len(prompts) != 1:
            raise CheckFailure("voice transcript was not forwarded exactly once")
        qitems = [
            item
            for item in fake["queue"].values()
            if voice_event_id in str(item.get("body") or "")
        ]
        if len(qitems) != 1:
            raise CheckFailure("voice note did not create exactly one qitem")
        validate_qitem_history(qitems[0])
        if qitems[0]["state"] != "done":
            raise CheckFailure(f"voice qitem did not close: {qitems[0]}")
        self.record(
            "voice-status-edit",
            "pass",
            (
                "A deterministic transcriber produced one local transcript for an "
                "encrypted voice note. Matrix showed one threaded progress event "
                "edited in place by `m.replace`; no transcript-only reply was sent. "
                "The prompt and single qitem each completed exactly once. Real "
                "Parakeet execution and Element rendering remain explicit gaps."
            ),
        )

    async def check_decision_and_poll(self, room_id: str) -> None:
        assert self.owner is not None
        owner = self.owner
        decision = self.cli(
            "decision",
            "--agent",
            AGENT_KEY,
            "--title",
            "Integration decision",
            "--body",
            "Choose the integration path.",
            "--option",
            "Approved",
            "--option",
            "Rejected",
            "--wait-seconds",
            "15",
            check=False,
        )
        if decision.returncode != 0:
            raise CheckFailure(
                f"decision publication failed: {decision.stdout}{decision.stderr}"
            )
        state = self.bridge_state()
        decision_id, published = next(
            reversed(list(state["decisions"].items())),
            None,
        )
        if published is None or len(published["event_ids"]) < 2:
            raise CheckFailure("decision did not publish a brief and poll")
        poll_id = published["event_ids"][1]
        pinned = await owner.client.room_get_state_event(
            room_id,
            "m.room.pinned_events",
        )
        if not isinstance(pinned, RoomGetStateEventResponse):
            raise CheckFailure(f"could not read pinned events: {pinned}")
        if set(published["event_ids"]) - set(pinned.content.get("pinned", [])):
            raise CheckFailure("decision brief and poll were not pinned")
        response = await owner.client.room_send(
            room_id,
            "org.matrix.msc3381.poll.response",
            {
                "m.relates_to": {
                    "rel_type": "m.reference",
                    "event_id": poll_id,
                },
                "org.matrix.msc3381.poll.response": {
                    "answers": ["option-1"],
                },
            },
            ignore_unverified_devices=True,
        )
        if not isinstance(response, RoomSendResponse):
            raise CheckFailure(f"poll response failed: {response}")
        await owner.sync_until(
            lambda: any(
                "Matrix-Entscheidung beantwortet" in item["prompt"]
                and "Approved" in item["prompt"]
                for item in json.loads(
                    (self.fake_state / "fake-openrig.json").read_text()
                )["prompts"]
            ),
            self.args.timeout,
            description="poll answer forwarded to OpenRig transport",
        )
        deadline = time.monotonic() + self.args.timeout
        while time.monotonic() < deadline:
            state = self.bridge_state()
            selected = state["decisions"][decision_id]
            if selected.get("closed_at"):
                break
            await asyncio.sleep(0.1)
        else:
            raise CheckFailure("decision did not close after all answers")
        pinned = await owner.client.room_get_state_event(
            room_id,
            "m.room.pinned_events",
        )
        if isinstance(pinned, RoomGetStateEventResponse) and set(
            published["event_ids"]
        ) & set(pinned.content.get("pinned", [])):
            raise CheckFailure("answered decision remained pinned")
        self.record(
            "decision-and-poll",
            "pass",
            (
                "Decision brief and MSC3381 poll were published and pinned; "
                "an encrypted poll response was returned to the fake transport "
                "as a decision prompt and the decision events were unpinned."
            ),
        )

    async def check_proactive_message(self, room_id: str) -> None:
        assert self.owner is not None
        owner = self.owner
        outbound = self.cli(
            "send-message",
            "--agent",
            AGENT_KEY,
            "--body",
            "Proactive integration evidence",
            "--wait-seconds",
            "15",
            check=False,
        )
        if outbound.returncode != 0:
            raise CheckFailure(
                f"send-message failed: {outbound.stdout}{outbound.stderr}"
            )
        await owner.sync_until(
            lambda: any(
                message["body"] == "Proactive integration evidence"
                for message in owner.messages
            ),
            self.args.timeout,
            description="proactive encrypted message",
        )
        self.record(
            "proactive-message",
            "pass",
            "`send-message` delivered an encrypted proactive message to the owner.",
        )

    async def check_restart(self, room_id: str) -> None:
        assert self.owner is not None
        owner = self.owner
        rooms_before = self.bridge_state()["rooms"]
        self.stop_bridge()
        self.start_bridge()
        await owner.sync_until(
            lambda: str(owner.client.rooms[room_id].topic).startswith("🟢 bereit"),
            self.args.timeout,
            description="ready status after restart",
        )
        rooms_after = self.bridge_state()["rooms"]
        if rooms_after != rooms_before:
            raise CheckFailure("restart did not preserve room identity")
        self.record(
            "restart-recovery",
            "pass",
            (
                "Stopping and restarting the bridge preserved the exact Matrix "
                "room mapping and returned the room to ready status without "
                "creating a duplicate room."
            ),
        )

    async def check_room_rejection(self, room_id: str) -> None:
        assert self.owner is not None
        owner = self.owner
        response = await owner.client.room_leave(room_id)
        if hasattr(response, "status_code") and response.status_code >= 400:
            raise CheckFailure(f"owner room leave failed: {response}")
        state = wait_for_json_state(
            self.state_dir / "state.json",
            lambda value: AGENT_KEY in value.get("rejected_rooms", {}),
            self.args.timeout,
        )
        if AGENT_KEY in state["rooms"]:
            raise CheckFailure("rejected room remains in active room mapping")
        self.stop_bridge()
        self.start_bridge()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        state = self.bridge_state()
        if state["rooms"] or AGENT_KEY not in state["rejected_rooms"]:
            raise CheckFailure("restart recreated or forgot the rejected room")
        self.record(
            "rejected-room-recovery",
            "pass",
            (
                "Owner leave was detected, the bridge left the room, persisted "
                "a rejection tombstone, and a restart did not recreate it."
            ),
        )

    async def check_doctor_failure(self) -> None:
        result = self.cli(
            "doctor",
            token="invalid-integration-token",
            check=False,
        )
        output = f"{result.stdout}{result.stderr}"
        if result.returncode == 0 or "invalid or expired" not in output:
            raise CheckFailure(
                f"doctor did not report an invalid Matrix token: "
                f"{output}"
            )
        self.record(
            "doctor-invalid-token",
            "pass",
            "`doctor` exited non-zero and told the owner to create a new token.",
        )

    def selected_checks(self) -> tuple[str, ...]:
        only = getattr(self.args, "only", None)
        if not only:
            return CHECKS
        unknown = sorted(set(only) - set(CHECKS))
        if unknown:
            raise CheckFailure(f"unknown checks for --only: {', '.join(unknown)}")
        return tuple(name for name in CHECKS if name in only)

    async def execute(self) -> None:
        self.args.report.parent.mkdir(parents=True, exist_ok=True)
        await self.start_synapse()
        self.record(
            "ephemeral-matrix",
            "pass",
            (
                f"Started disposable Synapse {SYNAPSE_IMAGE} on loopback; "
                "the container and state directory are removed at exit unless "
                "`--keep-workdir` is set."
            ),
        )
        await self.setup_bridge()
        self.start_bridge()
        room_id = await self.join_agent_room()
        for name in self.selected_checks():
            check = getattr(self, f"check_{name}")
            await (check() if name == "doctor_failure" else check(room_id))

    def report(self, status: str, error: str | None = None) -> None:
        # A run restricted with --only is a development aid, never evidence.
        partial = bool(getattr(self.args, "only", None))
        state: dict[str, Any] = {}
        with suppress(FileNotFoundError, json.JSONDecodeError):
            state = self.bridge_state()
        report: dict[str, Any] = {
            "schema_version": REPORT_SCHEMA_VERSION,
            "generated_at": datetime.now(UTC).isoformat(),
            "duration_seconds": round(time.monotonic() - self.started_at, 1),
            "status": (
                "invalid"
                if self.source_provenance_error
                else "partial"
                if partial and status == "pass"
                else status
            ),
            "evidence_valid": self.source_provenance_error is None and not partial,
            "target": {
                "package": "openrig-matrix-bridge",
                "version": __version__,
                "source": self.source_provenance,
            },
            "environment": {
                "matrix_homeserver": "ephemeral Synapse on loopback",
                "command": "python tools/matrix_integration.py",
                "synapse_image": SYNAPSE_IMAGE,
                "openrig_transport": (
                    "tools/fake_openrig.py subprocess protocol; not live Codex"
                ),
                "voice_model": "not downloaded or executed",
                "element_sas": "not exercised; requires a human client action",
            },
            "redaction": {
                "access_tokens": "not written to the report or bridge log",
                "room_ids": "omitted",
                "user_ids": "synthetic localhost fixtures",
                "ephemeral_state": (
                    "removed after the run unless --keep-workdir is set"
                ),
            },
            "results": self.results,
            "known_gaps": [
                (
                    "Real Codex prompt delivery and native transcript completion are "
                    "represented by the fake OpenRig subprocess transport."
                ),
                "The Parakeet model and real voice transcription were not run.",
                "Element device-to-device SAS verification was not run.",
                (
                    "Federation, public homeserver policy, and production E2EE device "
                    "trust were not exercised."
                ),
            ],
            "steering_latency": summarize_latency(
                state.get("latency_samples", [])
            ),
            "bridge_state_summary": {
                "rooms": len(state.get("rooms", {})),
                "processed_matrix_events": len(state.get("processed_events", [])),
                "rejected_rooms": len(state.get("rejected_rooms", {})),
            },
        }
        if partial:
            report["checks_selected"] = list(self.selected_checks())
        if error:
            report["error"] = error
        if self.source_provenance_error:
            report["provenance_error"] = self.source_provenance_error
        self.report_path.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def cleanup(self) -> None:
        try:
            self.stop_bridge()
        finally:
            with suppress(Exception):
                docker(["rm", "-f", self.container], timeout=30)
            if self.args.keep_workdir:
                print(f"integration state retained at {self.work}")
            else:
                shutil.rmtree(self.work, ignore_errors=True)


async def async_main(args: argparse.Namespace) -> int:
    integration = IntegrationRun(args)
    exit_code = 0
    try:
        await integration.execute()
    except Exception as exc:  # noqa: BLE001 - harness failures must be reported
        integration.record(
            "integration-run",
            "fail",
            str(exc),
        )
        integration.report("fail", str(exc))
        print(f"integration failed: {exc}", file=sys.stderr)
        if args.keep_workdir:
            log = integration.log_path
            if log.exists():
                print(f"bridge log: {log}", file=sys.stderr)
        exit_code = 1
    else:
        integration.report("pass")
        if integration.source_provenance_error:
            print(
                "integration checks passed, but evidence is invalid: "
                f"{integration.source_provenance_error}",
                file=sys.stderr,
            )
            exit_code = 1
        else:
            print(f"integration passed: {integration.report_path}")
    finally:
        if integration.owner is not None:
            await integration.owner.close()
        integration.cleanup()
    return exit_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report",
        type=Path,
        default=ROOT / "artifacts" / "integration" / "report.json",
    )
    parser.add_argument("--timeout", type=float, default=45)
    parser.add_argument(
        "--source-commit",
        help="expected 40-character Git commit; defaults to checked-out HEAD",
    )
    parser.add_argument(
        "--keep-workdir",
        action="store_true",
        help="retain bridge state and logs for inspection",
    )
    parser.add_argument(
        "--only",
        nargs="+",
        metavar="CHECK",
        help=(
            "development aid: run only these checks (names from CHECKS, without "
            "the check_ prefix); the report is marked partial and not evidence"
        ),
    )
    return parser


def main() -> None:
    if shutil.which("docker") is None:
        raise SystemExit("docker is required for the ephemeral Synapse harness")
    args = build_parser().parse_args()
    raise SystemExit(asyncio.run(async_main(args)))


if __name__ == "__main__":
    main()
