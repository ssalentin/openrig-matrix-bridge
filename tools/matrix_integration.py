#!/usr/bin/env python3
"""Run executable Matrix/OpenRig integration checks against ephemeral Synapse."""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import re
import shutil
import signal
import socket
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
from nio import (
    AsyncClient,
    AsyncClientConfig,
    InviteMemberEvent,
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
REGISTRATION_SECRET = "openrig-integration-only"
BRIDGE_USER = "@bridge:localhost"
OWNER_USER = "@owner:localhost"
BRIDGE_PASSWORD = "bridge-integration-password"
OWNER_PASSWORD = "owner-integration-password"
SESSION = "integration-agent@local"
AGENT_KEY = "integration-agent"


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


def validate_qitem_history(item: dict[str, Any]) -> None:
    history = item.get("history")
    if not isinstance(history, list):
        raise CheckFailure(
            "qitem history must contain pending -> in-progress -> done"
        )
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


def validate_fifo_records(
    records: list[dict[str, Any]],
    first_prompt: str,
    second_prompt: str,
) -> None:
    first = next((item for item in records if item.get("prompt") == first_prompt), None)
    second = next((item for item in records if item.get("prompt") == second_prompt), None)
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
    raise CheckFailure(f"state condition not met within {timeout}s: {path}")


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

    async def _on_unknown(self, room: Any, event: UnknownEvent) -> None:
        self.unknown.append(event)

    async def _on_invite(self, room: Any, event: InviteMemberEvent) -> None:
        response = await self.client.join(room.room_id)
        if hasattr(response, "status_code") and response.status_code >= 400:
            raise CheckFailure(f"owner could not join invited room: {response}")

    async def sync_once(self, timeout_ms: int = 1000) -> None:
        response = await self.client.sync(timeout=timeout_ms, full_state=True)
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
        while time.monotonic() < deadline:
            await self.sync_once()
            if predicate():
                return
            await asyncio.sleep(0.1)
        raise CheckFailure(f"timed out waiting for {description}")

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
    ) -> str:
        upload, encryption = await self.client.upload(
            io.BytesIO(data),
            content_type=mimetype,
            filename=filename,
            encrypt=True,
            filesize=len(data),
        )
        if not hasattr(upload, "content_uri") or not isinstance(encryption, dict):
            raise CheckFailure(f"owner encrypted upload failed: {upload}")
        content = {
            "msgtype": msgtype,
            "body": body,
            "filename": filename,
            "info": {"mimetype": mimetype, "size": len(data)},
            "file": {"url": upload.content_uri, **encryption},
        }
        response = await self.client.room_send(
            room_id,
            "m.room.message",
            content,
            ignore_unverified_devices=True,
        )
        if not isinstance(response, RoomSendResponse):
            raise CheckFailure(f"owner encrypted file send failed: {response}")
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
        async with aiohttp.ClientSession() as session, session.get(
            f"{self.homeserver}/_matrix/client/v3/rooms/{room}/event/{event}",
            headers={"Authorization": f"Bearer {self.token}"},
        ) as response:
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

    async def http_login(self, user: str, password: str, device: str) -> tuple[str, str]:
        payload = {
            "type": "m.login.password",
            "identifier": {"type": "m.id.user", "user": user},
            "password": password,
            "initial_device_display_name": "OpenRig integration",
            "device_id": device,
        }
        async with aiohttp.ClientSession() as session, session.post(
            f"{self.homeserver}/_matrix/client/v3/login",
            json=payload,
        ) as response:
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
        setup = run(
            [
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
                "--without-voice",
                "--skip-model",
            ],
            env=self.env(),
            timeout=self.args.timeout,
        )
        doctor = self.cli("doctor")
        if doctor.returncode != 0:
            raise CheckFailure(f"bridge doctor failed: {doctor.stdout}{doctor.stderr}")
        self.record(
            "setup-and-doctor",
            "pass",
            (
                f"`setup` wrote one agent room; `doctor` exited 0 "
                f"({setup.stdout.splitlines()[0]})"
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

    def start_bridge(self) -> None:
        if self.bridge is not None and self.bridge.poll() is None:
            raise CheckFailure("bridge is already running")
        log = self.log_path.open("a", encoding="utf-8")
        self.bridge = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "openrig_matrix_bridge",
                "run",
                "--config",
                str(self.config_path),
            ],
            cwd=ROOT,
            env=self.env(),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )

    def stop_bridge(self) -> None:
        process = self.bridge
        if process is None or process.poll() is not None:
            return
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        if process.returncode not in {0, -signal.SIGTERM}:
            raise CheckFailure(f"bridge exited with {process.returncode}")

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
                message["sender"] == BRIDGE_USER
                and "verbunden" in str(message["body"])
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
        if status.content.get("state") != "ready":
            raise CheckFailure(f"initial structured status is not ready: {status.content}")
        if not str(owner.client.rooms[room_id].topic).startswith("🟢 bereit"):
            raise CheckFailure("initial room topic does not show ready status")
        self.record(
            "room-status-avatar",
            "pass",
            (
                "Synapse accepted the owner invite and join; the room uses "
                "`m.megolm.v1.aes-sha2`, has a managed avatar, and publishes "
                "ready topic plus `dev.openrig.agent_status` state."
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
        if not any(
            BRIDGE_USER in item["users"] for item in owner.typing
        ):
            raise CheckFailure("bridge typing indicator was not observed")
        if any(
            "Fake transport received" in str(message["body"])
            for message in owner.messages
        ) is False:
            raise CheckFailure("visible commentary was not mirrored")
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
            or raw_answer.get("content", {}).get("algorithm")
            != "m.megolm.v1.aes-sha2"
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
                "commentary was mirrored, and the final answer returned in-thread. "
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

        def prompt_started(value: dict[str, Any]) -> bool:
            pending = value.get("pending", {}).get(AGENT_KEY, [])
            return bool(
                pending
                and pending[0].get("event_id") == threaded_event_id
                and pending[0].get("started_at")
                and pending[0].get("thread_root_event_id") == root_event_id
            )

        wait_for_json_state(self.state_dir / "state.json", prompt_started, self.args.timeout)
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
        self.stop_bridge()
        self.start_bridge()

        await owner.sync_until(
            lambda: any(
                message["sender"] == BRIDGE_USER
                and "Fake transport received: hold first threaded restart request"
                in str(message["body"])
                for message in owner.messages[before:]
            )
            and any(
                message["sender"] == BRIDGE_USER
                and "Fake integration answer: hold first threaded restart request"
                in str(message["body"])
                for message in owner.messages[before:]
            ),
            self.args.timeout,
            description="threaded commentary and final answer after restart",
        )
        wait_for_json_state(
            self.state_dir / "state.json",
            lambda value: not value.get("pending", {}).get(AGENT_KEY),
            self.args.timeout,
        )
        await asyncio.sleep(0.5)
        await owner.sync_once()

        threaded_messages = [
            message
            for message in owner.messages[before:]
            if message["sender"] == BRIDGE_USER
            and (
                "Fake transport received: hold first threaded restart request"
                in str(message["body"])
                or "Fake integration answer: hold first threaded restart request"
                in str(message["body"])
            )
        ]
        if len(threaded_messages) != 2:
            raise CheckFailure(
                "threaded prompt did not produce exactly one commentary and one final answer"
            )
        for message in threaded_messages:
            validate_answer_relation(
                await owner.raw_event(room_id, message["event_id"]),
                root_event_id,
            )

        fake = json.loads((self.fake_state / "fake-openrig.json").read_text())
        prompt_records = [
            item
            for item in fake["prompts"]
            if item["prompt"].startswith("hold first threaded restart request")
        ]
        if len(prompt_records) != 1:
            raise CheckFailure("threaded prompt was delivered more than once across restart")
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
                "single Matrix qitem completed pending -> in-progress -> done."
            ),
        )

    async def check_fifo(self, room_id: str) -> None:
        assert self.owner is not None
        owner = self.owner
        await owner.send_text(room_id, "hold first request")
        await owner.send_text(room_id, "second queued request")
        await owner.sync_until(
            lambda: any(
                "Warteschlange" in str(message["body"])
                for message in owner.messages
            ),
            self.args.timeout,
            description="pinned queue notice",
        )
        await owner.sync_until(
            lambda: any(
                "Fake integration answer: second queued request"
                in str(message["body"])
                for message in owner.messages
            ),
            self.args.timeout,
            description="second FIFO answer",
        )
        fake = json.loads((self.fake_state / "fake-openrig.json").read_text())
        records = fake["prompts"]
        validate_fifo_records(
            records,
            "hold first request",
            "second queued request",
        )
        first = next(
            item for item in records if item["prompt"] == "hold first request"
        )
        second = next(
            item for item in records if item["prompt"] == "second queued request"
        )
        self.record(
            "fifo-queue-notice",
            "pass",
            (
                "A second owner message arrived during a deliberately slow "
                "first turn; Matrix showed the pinned queue notice and the "
                "fake OpenRig transport received the second prompt only after "
                "recording completion of the first "
                f"({first['completed_at']} < {second['sent_at']})."
            ),
        )

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
        before = sum(message["sender"] == BRIDGE_USER for message in owner.messages)
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
        await asyncio.sleep(1)
        await owner.sync_once()
        after = sum(message["sender"] == BRIDGE_USER for message in owner.messages)
        if after != before:
            raise CheckFailure("reaction feedback created a visible bridge reply")
        self.record(
            "silent-reaction-feedback",
            "pass",
            (
                "Owner reaction reached the fake OpenRig transport as silent "
                "feedback and did not create a visible bot reply."
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
                "Originaldateiname: \"../../Quarterly report.pdf\"" in item["prompt"]
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
                "Originaldateiname: \"../../payload.custom\"" in item["prompt"]
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
            raise CheckFailure(f"send-file PDF failed: {outbound.stdout}{outbound.stderr}")
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
            raise CheckFailure(f"outbound PDF metadata changed: {pdf_content.get('info')}")
        if await owner.download_media(pdf_event) != pdf:
            raise CheckFailure("outbound PDF did not decrypt byte-for-byte")
        raw_pdf = await owner.raw_event(room_id, pdf_event.event_id)
        if (
            raw_pdf.get("type") != "m.room.encrypted"
            or not raw_pdf.get("content", {}).get("ciphertext")
        ):
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
                "both directions through disposable Synapse. Inbound files were "
                "stored under generated agent inbox paths with original metadata; "
                "outbound m.file events preserved filename, MIME type and size, "
                "decrypted byte-for-byte, and were Megolm-encrypted on the wire."
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
        if await owner.download_media(recovery_event) != recovery.read_bytes():
            raise CheckFailure("restart-recovered attachment bytes changed")
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
                "success record names the same Matrix event ID observed by the owner."
            ),
        )

    async def check_audio_gap(self, room_id: str) -> None:
        assert self.owner is not None
        owner = self.owner
        before = len(owner.messages)
        await owner.send_encrypted_file(
            room_id,
            b"OggS-integration-audio-placeholder",
            msgtype="m.audio",
            filename="voice.ogg",
            mimetype="audio/ogg",
            body="voice.ogg",
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
        self.record(
            "voice-disabled-path",
            "pass",
            (
                "An encrypted Matrix audio event reached the bridge and received "
                "the configured disabled-voice response. Real Parakeet model "
                "execution remains an explicit gap because setup used "
                "`--without-voice` and no 465 MB model was downloaded."
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
        if result.returncode == 0 or "Matrix whoami failed" not in result.stdout:
            raise CheckFailure(
                f"doctor did not report an invalid Matrix token: "
                f"{result.stdout}{result.stderr}"
            )
        self.record(
            "doctor-invalid-token",
            "pass",
            "`doctor` exited non-zero and reported the invalid Matrix token.",
        )

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
        await self.check_room(room_id)
        await self.check_chat_and_queue(room_id)
        await self.check_threaded_reply(room_id)
        await self.check_fifo(room_id)
        await self.check_reaction(room_id)
        await self.check_images(room_id)
        await self.check_files(room_id)
        await self.check_audio_gap(room_id)
        await self.check_decision_and_poll(room_id)
        await self.check_proactive_message(room_id)
        await self.check_restart(room_id)
        await self.check_doctor_failure()
        await self.check_room_rejection(room_id)

    def report(self, status: str, error: str | None = None) -> None:
        state: dict[str, Any] = {}
        with suppress(FileNotFoundError, json.JSONDecodeError):
            state = self.bridge_state()
        report: dict[str, Any] = {
            "schema_version": REPORT_SCHEMA_VERSION,
            "generated_at": datetime.now(UTC).isoformat(),
            "duration_seconds": round(time.monotonic() - self.started_at, 1),
            "status": "invalid" if self.source_provenance_error else status,
            "evidence_valid": self.source_provenance_error is None,
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
            "bridge_state_summary": {
                "rooms": len(state.get("rooms", {})),
                "processed_matrix_events": len(state.get("processed_events", [])),
                "rejected_rooms": len(state.get("rejected_rooms", {})),
            },
        }
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
    return parser


def main() -> None:
    if shutil.which("docker") is None:
        raise SystemExit("docker is required for the ephemeral Synapse harness")
    args = build_parser().parse_args()
    raise SystemExit(asyncio.run(async_main(args)))


if __name__ == "__main__":
    main()
