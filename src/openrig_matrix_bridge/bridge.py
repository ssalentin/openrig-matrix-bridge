from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import stat
import tempfile
import time
import uuid
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any

from nio import (
    AsyncClient,
    AsyncClientConfig,
    InviteMemberEvent,
    KeyVerificationCancel,
    KeyVerificationEvent,
    KeyVerificationKey,
    KeyVerificationMac,
    KeyVerificationStart,
    MatrixRoom,
    ProfileSetAvatarResponse,
    ReactionEvent,
    RoomCreateResponse,
    RoomEncryptedAudio,
    RoomEncryptedFile,
    RoomEncryptedImage,
    RoomGetEventResponse,
    RoomGetStateEventResponse,
    RoomMemberEvent,
    RoomMessageAudio,
    RoomMessageFile,
    RoomMessageImage,
    RoomMessageText,
    RoomSendResponse,
    SyncResponse,
    ToDeviceError,
    UnknownEvent,
    UploadResponse,
)
from nio.api import RoomPreset, RoomVisibility
from nio.crypto import decrypt_attachment

from .avatar_pool import (
    assign_unique_avatar_ids,
    avatar_png,
    avatar_pool_fingerprint,
    profile_png,
)
from .config import AgentConfig, BridgeConfig, safe_agent_directory
from .decisions import (
    POLL_RESPONSE_TYPE,
    POLL_START_TYPE,
    STABLE_POLL_RESPONSE_TYPE,
    DecisionError,
    normalize_questions,
    poll_response,
    poll_start_content,
)
from .formatting import markdown_to_matrix_html
from .media import (
    MediaError,
    detect_image_file,
    detect_image_type,
    encrypted_file_content,
    encrypted_image_content,
    is_voice_message_content,
    normalize_mime_type,
)
from .openrig import (
    AgentSessionNotFound,
    AgentSessionRef,
    GatewayError,
    OpenRigGateway,
    OpenRigSessionResolver,
)
from .transcript import commentary_messages, final_answer, read_complete_records
from .transcription import Transcriber

LOGGER = logging.getLogger("openrig-matrix-bridge")
AGENT_STATUS_EVENT = "dev.openrig.agent_status"
_DEFAULT_THREAD_ROOT = object()
MAX_DELIVERY_ATTEMPTS = 3
STATUS_LABELS = {
    "ready": "🟢",
    "busy": "🟡",
    "offline": "⚫",
    "error": "🟠",
}


def _has_complete_encryption_metadata(encryption: object) -> bool:
    if not isinstance(encryption, dict):
        return False
    key = encryption.get("key")
    hashes = encryption.get("hashes")
    return (
        isinstance(key, dict)
        and isinstance(key.get("k"), str)
        and bool(key["k"])
        and isinstance(hashes, dict)
        and isinstance(hashes.get("sha256"), str)
        and bool(hashes["sha256"])
        and isinstance(encryption.get("iv"), str)
        and bool(encryption["iv"])
    )


def _open_verified_snapshot(
    path: Path,
    *,
    expected_size: int,
    expected_sha256: str,
    max_file_bytes: int,
) -> Any:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise MediaError("file snapshot is not a regular file")
        if metadata.st_size != expected_size:
            raise MediaError(
                "file snapshot size changed: "
                f"{metadata.st_size} instead of {expected_size} bytes"
            )
        if metadata.st_size <= 0 or metadata.st_size > max_file_bytes:
            raise MediaError(
                "file size must be between 1 and "
                f"{max_file_bytes} bytes"
            )
        handle = os.fdopen(descriptor, "rb")
        descriptor = -1
    finally:
        if descriptor >= 0:
            os.close(descriptor)

    try:
        digest = hashlib.sha256()
        size = 0
        while chunk := handle.read(1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
        if size != expected_size or digest.hexdigest() != expected_sha256:
            raise MediaError("file snapshot digest does not match admission metadata")
        handle.seek(0)
        return handle
    except Exception:
        handle.close()
        raise


def room_name(agent: AgentConfig) -> str:
    return agent.display_name


@dataclass(frozen=True)
class ResponseTarget:
    thread_root_event_id: str | None
    reply_to_event_id: str


def resolve_response_target(
    event_id: str,
    relates_to: dict[str, Any] | None,
) -> ResponseTarget:
    if isinstance(relates_to, dict):
        thread_root = relates_to.get("event_id")
        if relates_to.get("rel_type") == "m.thread" and isinstance(thread_root, str):
            thread_root = thread_root.strip()
            if thread_root:
                return ResponseTarget(thread_root, thread_root)
        return ResponseTarget(None, event_id)
    return ResponseTarget(event_id, event_id)


def message_relation(event: Any) -> dict[str, Any] | None:
    source = getattr(event, "source", {})
    if not isinstance(source, dict):
        return None
    content = source.get("content", {})
    if not isinstance(content, dict):
        return None
    relation = content.get("m.relates_to")
    return relation if isinstance(relation, dict) else None


def formatted_message_content(text: str) -> dict[str, Any]:
    content: dict[str, Any] = {"msgtype": "m.text", "body": text[:60000]}
    formatted_body = markdown_to_matrix_html(text[:60000])
    if formatted_body:
        content["format"] = "org.matrix.custom.html"
        content["formatted_body"] = formatted_body
    return content


def verification_decision(text: str) -> bool | None:
    normalized = text.strip().lower().rstrip(".!")
    if normalized in {"ja", "ja bitte", "bestätigen", "bestaetigen", "stimmt"}:
        return True
    if normalized in {"nein", "ablehnen", "stimmt nicht"}:
        return False
    return None


class StateStore:
    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, Any] = {
            "version": 1,
            "rooms": {},
            "transcripts": {},
            "pending": {},
            "processed_events": [],
            "welcomed": {},
            "decisions": {},
            "media": {},
            "media_failures": {},
            "messages": {},
            "queue_notices": {},
            "queue_mirror": {"initialized": False, "seen": {}},
            "rejected_rooms": {},
            "avatar_pool": {
                "asset_fingerprint": None,
                "assignments": {},
                "uploads": {},
                "profile_mxc": None,
            },
        }
        if path.exists():
            self.data.update(json.loads(path.read_text(encoding="utf-8")))

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self.data, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass


class MatrixBridge:
    def __init__(self, config: BridgeConfig, access_token: str):
        if not access_token:
            raise ValueError("MATRIX_ACCESS_TOKEN is required")
        self.config = config
        self.config.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        (self.config.state_dir / "matrix-store").mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state = StateStore(config.state_dir / "state.json")
        self.resolver = OpenRigSessionResolver(config.rig_command, config.codex_home)
        self.gateway = OpenRigGateway(config.rig_command, config.sender_session)
        self.transcriber = Transcriber(config.transcription)
        self.locks = {key: asyncio.Lock() for key in config.agents}
        self.room_agents: dict[str, str] = {}
        self.processed_events: set[str] = set(self.state.data.get("processed_events", []))
        self.verification_rooms: dict[str, str] = {}
        self.typing_tasks: dict[str, asyncio.Task[None]] = {}
        self.pin_locks = {
            str(room["room_id"]): asyncio.Lock()
            for room in self.state.data["rooms"].values()
            if room.get("room_id")
        }
        self.agent_statuses: dict[str, str] = {}
        self.decision_inbox = config.state_dir / "decision-inbox"
        self.decision_sent = self.decision_inbox / "sent"
        self.decision_failed = self.decision_inbox / "failed"
        self.decision_inbox.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.decision_sent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.decision_failed.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.media_outbox = config.state_dir / "media-outbox"
        self.media_sent = self.media_outbox / "sent"
        self.media_failed = self.media_outbox / "failed"
        self.media_outbox.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.media_sent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.media_failed.mkdir(parents=True, exist_ok=True, mode=0o700)
        config.media.inbox_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.message_outbox = config.state_dir / "message-outbox"
        self.message_sent = self.message_outbox / "sent"
        self.message_failed = self.message_outbox / "failed"
        self.message_outbox.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.message_sent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.message_failed.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.client = AsyncClient(
            config.homeserver,
            config.user_id,
            device_id=config.device_id,
            store_path=str(config.state_dir / "matrix-store"),
            config=AsyncClientConfig(
                encryption_enabled=True,
                store_sync_tokens=True,
                store_name="matrix-store",
            ),
        )
        self.client.restore_login(
            user_id=config.user_id,
            device_id=config.device_id,
            access_token=access_token,
        )
        self.client.add_event_callback(self._on_room_message, RoomMessageText)
        self.client.add_event_callback(self._on_reaction, ReactionEvent)
        self.client.add_event_callback(self._on_audio, (RoomMessageAudio, RoomEncryptedAudio))
        self.client.add_event_callback(self._on_image, (RoomMessageImage, RoomEncryptedImage))
        self.client.add_event_callback(self._on_file, (RoomMessageFile, RoomEncryptedFile))
        self.client.add_event_callback(self._on_invite, InviteMemberEvent)
        self.client.add_event_callback(self._on_member, RoomMemberEvent)
        self.client.add_event_callback(self._on_unknown_event, UnknownEvent)
        self.client.add_to_device_callback(self._on_verification, (KeyVerificationEvent,))

    async def run(self) -> None:
        await self._initial_sync()
        self._load_room_index()
        await self._reconcile_room_memberships()
        if any(key not in self.state.data["rooms"] for key in self.config.agents):
            await self._create_rooms()
        await self._restore_room_names()
        await self._ensure_room_avatars()
        self._initialize_transcript_offsets()
        self._repair_decision_reply_targets()
        await self._refresh_agent_statuses(force=True)
        poller = asyncio.create_task(self._poll_answers(), name="openrig-matrix-poller")
        status_poller = asyncio.create_task(
            self._poll_agent_statuses(),
            name="openrig-matrix-status",
        )
        decision_poller = asyncio.create_task(
            self._poll_decisions(),
            name="openrig-matrix-decisions",
        )
        media_poller = asyncio.create_task(
            self._poll_media_outbox(),
            name="openrig-matrix-media",
        )
        message_poller = asyncio.create_task(
            self._poll_message_outbox(),
            name="openrig-matrix-messages",
        )
        human_queue_poller = asyncio.create_task(
            self._poll_human_queue(),
            name="openrig-matrix-human-queue",
        )
        try:
            await self.client.sync_forever(
                timeout=30000,
                full_state=True,
                set_presence="online",
            )
        finally:
            poller.cancel()
            status_poller.cancel()
            decision_poller.cancel()
            media_poller.cancel()
            message_poller.cancel()
            human_queue_poller.cancel()
            for room_id in list(self.typing_tasks):
                await self._set_typing(room_id, False)
            await self.client.close()

    async def _initial_sync(self) -> None:
        response = await self.client.sync(timeout=0, full_state=True)
        if not isinstance(response, SyncResponse):
            raise ConnectionError(f"initial Matrix sync failed: {response}")
        if self.client.should_upload_keys:
            await self.client.keys_upload()

    def _load_room_index(self) -> None:
        agents = self.config.agents
        rooms = self.state.data.setdefault("rooms", {})
        for key, value in rooms.items():
            room_id = value.get("room_id")
            if key in agents and room_id:
                self.room_agents[room_id] = key

    def _repair_decision_reply_targets(self) -> None:
        changed = False
        for agent_key, pending in self.state.data["pending"].items():
            for request in pending:
                if request.get("reply_to_event_id"):
                    continue
                if request.get("source_label") != "decision" and not str(
                    request.get("text", "")
                ).startswith("Matrix-Entscheidung beantwortet."):
                    continue
                text = str(request.get("text", ""))
                candidates = [
                    decision
                    for decision in self.state.data["decisions"].values()
                    if decision.get("agent") == agent_key
                    and str(decision.get("title", "")) in text
                    and decision.get("event_ids")
                ]
                if not candidates:
                    continue
                decision = max(candidates, key=lambda item: item.get("created_at", 0))
                request["reply_to_event_id"] = str(decision["event_ids"][0])
                request["source_label"] = "decision"
                changed = True
        if changed:
            self.state.save()

    async def _poll_decisions(self) -> None:
        while True:
            try:
                await self._process_decision_inbox()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("decision processing failed")
            await asyncio.sleep(self.config.poll_interval_seconds)

    async def _process_decision_inbox(self) -> None:
        for path in sorted(self.decision_inbox.glob("*.json")):
            try:
                request = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(request, dict):
                    raise DecisionError("decision request must be an object")
                await self._publish_decision(request)
            except (DecisionError, json.JSONDecodeError, OSError):
                LOGGER.warning("invalid decision request: %s", path, exc_info=True)
                os.replace(path, self.decision_failed / path.name)
                continue
            except Exception:
                LOGGER.exception("failed to publish decision request: %s", path)
                os.replace(path, self.decision_failed / path.name)
                continue
            os.replace(path, self.decision_sent / path.name)

    async def _publish_decision(self, request: dict[str, Any]) -> None:
        request_id = str(request.get("id") or "").strip()
        agent_key = str(request.get("agent") or "").strip()
        title = str(request.get("title") or "").strip()
        body = str(request.get("body") or "").strip()
        qitem_id = str(request.get("qitem_id") or "").strip() or None
        questions = normalize_questions(request.get("questions"))
        agent = self.config.agents.get(agent_key)
        if not request_id or not title:
            raise DecisionError("decision request needs an id and title")
        if agent is None:
            raise DecisionError(f"unknown agent: {agent_key}")

        room = self.state.data["rooms"].get(agent.key)
        if not room:
            raise DecisionError(f"no Matrix room configured for: {agent_key}")
        room_id = str(room["room_id"])

        lines = [f"**Entscheidung: {title}**"]
        if body:
            lines.extend(["", body])
        if qitem_id:
            lines.extend(["", f"Queue-Item: `{qitem_id}`"])
        response = await self._send(room_id, "\n".join(lines))
        if not isinstance(response, RoomSendResponse):
            raise ConnectionError(f"decision message failed: {response}")

        event_ids = [response.event_id]
        polls: dict[str, dict[str, Any]] = {}
        for question in questions:
            poll_response_event = await self.client.room_send(
                room_id,
                POLL_START_TYPE,
                poll_start_content(question),
                ignore_unverified_devices=True,
            )
            if not isinstance(poll_response_event, RoomSendResponse):
                raise ConnectionError(f"decision poll failed: {poll_response_event}")
            poll_id = poll_response_event.event_id
            event_ids.append(poll_id)
            polls[poll_id] = question

        await self._add_pinned(room_id, event_ids)
        self.state.data["decisions"][request_id] = {
            "agent": agent.key,
            "qitem_id": qitem_id,
            "title": title,
            "room_id": room_id,
            "event_ids": event_ids,
            "polls": polls,
            "answers": {},
            "created_at": int(time.time() * 1000),
            "closed_at": None,
        }
        self.state.save()
        LOGGER.info("published decision %s in %s", request_id, agent.session)

    async def _poll_message_outbox(self) -> None:
        while True:
            try:
                await self._process_message_outbox()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("message processing failed")
            await asyncio.sleep(self.config.poll_interval_seconds)

    async def _poll_human_queue(self) -> None:
        while True:
            try:
                await self._mirror_human_queue()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("human queue mirror failed")
            await asyncio.sleep(self.config.human_queue_poll_seconds)

    async def _mirror_human_queue(self) -> None:
        if not self.config.human_destinations:
            return
        agents_by_session = {
            agent.session: agent for agent in self.config.agents.values()
        }
        items = await self.gateway.queue_list()
        mirror = self.state.data.setdefault(
            "queue_mirror",
            {"initialized": False, "seen": {}},
        )
        seen = mirror.setdefault("seen", {})
        if not mirror.get("initialized"):
            for item in items:
                qitem_id = str(item.get("qitemId") or "")
                if qitem_id:
                    seen[qitem_id] = int(time.time() * 1000)
            mirror["initialized"] = True
            self.state.save()
            LOGGER.info("initialized Matrix human-queue mirror")
            return

        human_destinations = set(self.config.human_destinations)
        for item in items:
            qitem_id = str(item.get("qitemId") or "").strip()
            if not qitem_id or qitem_id in seen:
                continue
            if item.get("destinationSession") not in human_destinations:
                seen[qitem_id] = int(time.time() * 1000)
                continue
            agent = agents_by_session.get(str(item.get("sourceSession") or ""))
            if agent is None:
                seen[qitem_id] = int(time.time() * 1000)
                continue
            if any(
                decision.get("qitem_id") == qitem_id
                for decision in self.state.data["decisions"].values()
            ):
                seen[qitem_id] = int(time.time() * 1000)
                continue

            detail = await self.gateway.queue_show(qitem_id)
            title = str(
                detail.get("summary") or item.get("summary") or "OpenRig update"
            ).strip()
            body = str(detail.get("body") or "").strip()
            if detail.get("humanIntent") == "decision":
                request_id = f"queue-{qitem_id}"
                await self._publish_decision(
                    {
                        "id": request_id,
                        "agent": agent.key,
                        "title": title,
                        "body": body,
                        "qitem_id": qitem_id,
                        "questions": normalize_questions(
                            detail.get("humanQuestions")
                        ),
                    }
                )
            else:
                text = f"**{title}**"
                if body:
                    text += f"\n\n{body}"
                await self._publish_message(
                    {
                        "id": f"queue-{qitem_id}",
                        "agent": agent.key,
                        "text": text,
                    }
                )
            seen[qitem_id] = int(time.time() * 1000)
            self.state.save()
        if len(seen) > 2000:
            newest = sorted(seen.items(), key=lambda row: row[1])[-2000:]
            mirror["seen"] = dict(newest)
            self.state.save()

    async def _process_message_outbox(self) -> None:
        for path in sorted(self.message_outbox.glob("*.json")):
            try:
                request = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(request, dict):
                    raise TypeError("message request must be an object")
                await self._publish_message(request)
            except (TypeError, ValueError, json.JSONDecodeError, OSError):
                LOGGER.warning("invalid message request: %s", path, exc_info=True)
                os.replace(path, self.message_failed / path.name)
                continue
            except Exception:
                LOGGER.exception("failed to publish message request: %s", path)
                os.replace(path, self.message_failed / path.name)
                continue
            os.replace(path, self.message_sent / path.name)

    async def _publish_message(self, request: dict[str, Any]) -> None:
        request_id = str(request.get("id") or "").strip()
        agent_key = str(request.get("agent") or "").strip()
        text = str(request.get("text") or "").strip()
        agent = self.config.agents.get(agent_key)
        if not request_id or not text:
            raise ValueError("message request needs an id and text")
        if agent is None:
            raise ValueError(f"unknown agent: {agent_key}")
        room = self.state.data["rooms"].get(agent.key)
        if not room:
            raise ValueError(f"no Matrix room configured for: {agent.key}")

        response = await self._send(str(room["room_id"]), text)
        if not isinstance(response, RoomSendResponse):
            raise ConnectionError(f"message delivery failed: {response}")
        self.state.data["messages"][request_id] = {
            "agent": agent.key,
            "event_id": response.event_id,
            "sent_at": int(time.time() * 1000),
        }
        self.state.save()
        LOGGER.info("published message %s in %s", request_id, agent.session)

    async def _poll_media_outbox(self) -> None:
        while True:
            try:
                await self._process_media_outbox()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("media processing failed")
            await asyncio.sleep(self.config.poll_interval_seconds)

    async def _process_media_outbox(self) -> None:
        for path in sorted(self.media_outbox.glob("*.json")):
            request_id = path.stem
            if request_id in self.state.data.get("media", {}):
                os.replace(path, self.media_sent / path.name)
                continue
            if request_id in self.state.data.get("media_failures", {}):
                os.replace(path, self.media_failed / path.name)
                continue
            try:
                request = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(request, dict):
                    raise MediaError("media request must be an object")
                request_id = str(request.get("id") or path.stem)
                if request.get("kind") == "file":
                    await self._publish_file(request)
                else:
                    await self._publish_image(request)
            except MediaError as exc:
                LOGGER.warning("rejected media request: %s", path, exc_info=True)
                self._record_media_failure(request_id, path, str(exc))
                os.replace(path, self.media_failed / path.name)
                continue
            except (json.JSONDecodeError, OSError) as exc:
                LOGGER.warning("invalid media request: %s", path, exc_info=True)
                self._record_media_failure(
                    request_id,
                    path,
                    f"invalid media request: {exc}",
                )
                os.replace(path, self.media_failed / path.name)
                continue
            except Exception as exc:
                LOGGER.exception("failed to publish media request: %s", path)
                self._record_media_failure(request_id, path, str(exc))
                os.replace(path, self.media_failed / path.name)
                continue
            os.replace(path, self.media_sent / path.name)

    def _record_media_failure(
        self,
        request_id: str,
        path: Path,
        error: str,
    ) -> None:
        failures = self.state.data.setdefault("media_failures", {})
        failures[request_id] = {
            "error": error[:2000],
            "request": path.name,
            "failed_at": int(time.time() * 1000),
        }
        self.state.save()

    async def _publish_image(self, request: dict[str, Any]) -> None:
        if not self.config.media.enabled:
            raise MediaError("image exchange is disabled")
        request_id = str(request.get("id") or "").strip()
        agent_key = str(request.get("agent") or "").strip()
        agent = self.config.agents.get(agent_key)
        if not request_id or agent is None:
            raise MediaError("image request needs an id and configured agent")
        room = self.state.data["rooms"].get(agent.key)
        if not room:
            raise MediaError(f"no Matrix room configured for: {agent.key}")

        image_path = Path(str(request.get("path") or ""))
        if not image_path.is_absolute() or not image_path.is_file():
            raise MediaError(f"image file not found: {image_path}")
        size = image_path.stat().st_size
        if size <= 0 or size > self.config.media.max_image_bytes:
            raise MediaError(f"image size is outside the allowed range: {size} bytes")
        image_type = await asyncio.to_thread(detect_image_file, image_path)
        if image_type is None:
            raise MediaError("unsupported image type; use PNG, JPEG, GIF, or WebP")

        upload, encryption = await self.client.upload(
            lambda _retries, _timeouts: image_path,
            content_type=image_type.mime_type,
            filename=image_path.name,
            encrypt=True,
            filesize=size,
        )
        if not isinstance(upload, UploadResponse) or not _has_complete_encryption_metadata(
            encryption
        ):
            raise ConnectionError(f"encrypted image upload failed: {upload}")

        caption = str(request.get("caption") or "").strip()[:10_000]
        response = await self.client.room_send(
            str(room["room_id"]),
            "m.room.message",
            encrypted_image_content(
                filename=image_path.name,
                caption=caption,
                mime_type=image_type.mime_type,
                size=size,
                upload_url=upload.content_uri,
                encryption=encryption,
                formatted_caption=markdown_to_matrix_html(caption),
            ),
            tx_id=request_id,
            ignore_unverified_devices=True,
        )
        if not isinstance(response, RoomSendResponse):
            raise ConnectionError(f"encrypted image message failed: {response}")
        self.state.data["media"][request_id] = {
            "agent": agent.key,
            "event_id": response.event_id,
            "path": str(image_path),
            "mime_type": image_type.mime_type,
            "size": size,
            "sent_at": int(time.time() * 1000),
        }
        self.state.save()
        LOGGER.info("published encrypted image %s in %s", request_id, agent.session)

    async def _publish_file(self, request: dict[str, Any]) -> None:
        if not self.config.media.enabled:
            raise MediaError("file exchange is disabled")
        request_id = str(request.get("id") or "").strip()
        agent_key = str(request.get("agent") or "").strip()
        agent = self.config.agents.get(agent_key)
        if not request_id or agent is None:
            raise MediaError("file request needs an id and configured agent")
        room = self.state.data["rooms"].get(agent.key)
        if not room:
            raise MediaError(f"no Matrix room configured for: {agent.key}")

        file_path = Path(str(request.get("path") or ""))
        snapshot_value = str(request.get("snapshot_path") or "")
        expected_size = request.get("size")
        expected_sha256 = request.get("sha256")
        if (
            not snapshot_value
            or isinstance(expected_size, bool)
            or not isinstance(expected_size, int)
            or not isinstance(expected_sha256, str)
            or len(expected_sha256) != 64
        ):
            raise MediaError("file request lacks snapshot size or digest metadata")
        snapshot_path = Path(snapshot_value)
        snapshot_root = self.config.state_dir / "media-outbox" / "files"
        if not snapshot_path.is_absolute() or snapshot_path.parent != snapshot_root:
            raise MediaError("file snapshot is outside the private outbox")
        filename = str(request.get("filename") or file_path.name)
        mime_type = normalize_mime_type(
            request.get("mime_type") or "application/octet-stream"
        )
        try:
            snapshot_handle = await asyncio.to_thread(
                _open_verified_snapshot,
                snapshot_path,
                expected_size=expected_size,
                expected_sha256=expected_sha256,
                max_file_bytes=self.config.media.max_file_bytes,
            )
        except OSError as exc:
            raise MediaError(f"file snapshot could not be opened: {exc}") from exc
        size = expected_size
        try:
            upload, encryption = await self.client.upload(
                snapshot_handle,
                content_type=mime_type,
                filename=filename,
                encrypt=True,
                filesize=size,
            )
        finally:
            snapshot_handle.close()
        if not isinstance(upload, UploadResponse) or not _has_complete_encryption_metadata(
            encryption
        ):
            raise MediaError(
                "encrypted file upload returned incomplete encryption metadata"
            )

        caption = str(request.get("caption") or "").strip()[:10_000]
        response = await self.client.room_send(
            str(room["room_id"]),
            "m.room.message",
            encrypted_file_content(
                filename=filename,
                caption=caption,
                mime_type=mime_type,
                size=size,
                upload_url=upload.content_uri,
                encryption=encryption,
                formatted_caption=markdown_to_matrix_html(caption),
            ),
            tx_id=request_id,
            ignore_unverified_devices=True,
        )
        if not isinstance(response, RoomSendResponse):
            raise ConnectionError(f"encrypted file message failed: {response}")
        self.state.data["media"][request_id] = {
            "agent": agent.key,
            "event_id": response.event_id,
            "path": str(file_path),
            "snapshot_path": str(snapshot_path),
            "filename": filename,
            "mime_type": mime_type,
            "size": size,
            "sent_at": int(time.time() * 1000),
        }
        self.state.save()
        LOGGER.info("published encrypted file %s in %s", request_id, agent.session)

    async def _add_pinned(self, room_id: str, event_ids: list[str]) -> None:
        lock = self.pin_locks.setdefault(room_id, asyncio.Lock())
        async with lock:
            pinned = await self._pinned_event_ids(room_id)
            merged = list(dict.fromkeys([*pinned, *event_ids]))
            if merged != pinned:
                await self.client.room_put_state(
                    room_id,
                    "m.room.pinned_events",
                    {"pinned": merged},
                )

    async def _update_queue_notice(
        self,
        agent: AgentConfig,
        room_id: str,
        waiting: int,
    ) -> None:
        notices = self.state.data.setdefault("queue_notices", {})
        notice = notices.setdefault(agent.key, {})
        event_id = notice.get("event_id")
        if waiting <= 0:
            if event_id and notice.get("pinned", True):
                await self._remove_pinned(room_id, [str(event_id)])
                notice["pinned"] = False
                self.state.save()
            return

        noun = "Nachricht wartet" if waiting == 1 else "Nachrichten warten"
        priority = str(
            getattr(self.config, "inbound_message_priority", "urgent")
        )
        priority_label = {
            "routine": "normal",
            "urgent": "hoch",
            "critical": "kritisch",
        }.get(priority, priority)
        text = (
            f"⏳ **Warteschlange:** {waiting} {noun} · "
            f"Priorität {priority_label}.\n\n"
            "Diese Hinweiskarte wird aktualisiert und nach dem Abarbeiten entpinnt."
        )
        target_event_id = str(event_id) if event_id else None
        response = None
        if target_event_id:
            response = await self._edit_message(room_id, target_event_id, text)
        if not isinstance(response, RoomSendResponse):
            response = await self._send(room_id, text)
            target_event_id = None
        if not isinstance(response, RoomSendResponse):
            LOGGER.warning("queue notice delivery failed for %s: %s", agent.session, response)
            return
        if target_event_id is None:
            notice["event_id"] = response.event_id
        notice["pinned"] = True
        self.state.save()
        await self._add_pinned(room_id, [str(notice["event_id"])])

    async def _remove_pinned(self, room_id: str, event_ids: list[str]) -> None:
        lock = self.pin_locks.setdefault(room_id, asyncio.Lock())
        async with lock:
            pinned = await self._pinned_event_ids(room_id)
            remove = set(event_ids)
            remaining = [event_id for event_id in pinned if event_id not in remove]
            if remaining != pinned:
                await self.client.room_put_state(
                    room_id,
                    "m.room.pinned_events",
                    {"pinned": remaining},
                )

    async def _pinned_event_ids(self, room_id: str) -> list[str]:
        response = await self.client.room_get_state_event(
            room_id,
            "m.room.pinned_events",
        )
        if not isinstance(response, RoomGetStateEventResponse):
            return []
        pinned = response.content.get("pinned")
        if not isinstance(pinned, list):
            return []
        return [item for item in pinned if isinstance(item, str)]

    async def _restore_room_names(self) -> None:
        for agent in self.config.agents.values():
            room = self.state.data["rooms"].get(agent.key)
            if not room:
                continue
            stable_name = room_name(agent)
            try:
                await self.client.room_put_state(
                    str(room["room_id"]),
                    "m.room.name",
                    {"name": stable_name},
                )
            except Exception:
                LOGGER.warning("failed to restore room name for %s", agent.session, exc_info=True)
                continue
            room["name"] = stable_name
        self.state.save()

    async def _ensure_room_avatars(self) -> None:
        if not self.config.avatars.enabled:
            return
        avatar_state = self.state.data.setdefault(
            "avatar_pool",
            {
                "asset_fingerprint": None,
                "assignments": {},
                "uploads": {},
                "profile_mxc": None,
            },
        )
        fingerprint = avatar_pool_fingerprint()
        if avatar_state.get("asset_fingerprint") != fingerprint:
            avatar_state["asset_fingerprint"] = fingerprint
            avatar_state["uploads"] = {}
            avatar_state["profile_mxc"] = None
        assignments = avatar_state.setdefault("assignments", {})
        uploads = avatar_state.setdefault("uploads", {})
        candidates: list[str] = []
        existing: dict[str, str] = {}
        for key in self.config.agents:
            room = self.state.data["rooms"].get(key)
            if not room:
                continue
            current = assignments.get(key, {})
            current_id = current.get("id") if isinstance(current, dict) else None
            room_state = self.client.rooms.get(str(room["room_id"]))
            current_url = getattr(room_state, "avatar_url", None)
            managed_url = current.get("mxc") if isinstance(current, dict) else None
            if (
                not self.config.avatars.replace_existing
                and current_url
                and current_url != managed_url
            ):
                continue
            candidates.append(key)
            if isinstance(current_id, str):
                existing[key] = current_id

        selected = assign_unique_avatar_ids(candidates, existing)
        assignments.clear()
        assignments.update(
            {
                key: {"id": avatar_id}
                for key, avatar_id in selected.items()
            }
        )
        for key, avatar_id in selected.items():
            room = self.state.data["rooms"][key]
            room_id = str(room["room_id"])
            avatar_url = uploads.get(avatar_id)
            if not isinstance(avatar_url, str):
                image_data = avatar_png(avatar_id)
                upload, _encryption = await self.client.upload(
                    BytesIO(image_data),
                    content_type="image/png",
                    filename=f"{avatar_id}.png",
                    filesize=len(image_data),
                )
                if not isinstance(upload, UploadResponse):
                    LOGGER.warning("avatar upload failed for %s: %s", avatar_id, upload)
                    continue
                avatar_url = upload.content_uri
                uploads[avatar_id] = avatar_url
            room_state = self.client.rooms.get(room_id)
            if (
                self.config.avatars.replace_existing
                or getattr(room_state, "avatar_url", None) != avatar_url
            ):
                response = await self.client.room_put_state(
                    room_id,
                    "m.room.avatar",
                    {"url": avatar_url},
                )
                if hasattr(response, "status_code") and response.status_code >= 400:
                    LOGGER.warning("room avatar update failed for %s: %s", key, response)
                    continue
            assignments[key] = {"id": avatar_id, "mxc": avatar_url}
            room["avatar"] = avatar_id

        missing = [key for key in candidates if key not in selected]
        if missing:
            LOGGER.warning(
                "avatar pool exhausted; no avatar assigned for: %s",
                ", ".join(missing),
            )

        if self.config.avatars.set_profile:
            profile_url = avatar_state.get("profile_mxc")
            if not isinstance(profile_url, str):
                image_data = profile_png()
                upload, _encryption = await self.client.upload(
                    BytesIO(image_data),
                    content_type="image/png",
                    filename="openrig-profile.png",
                    filesize=len(image_data),
                )
                if isinstance(upload, UploadResponse):
                    profile_url = upload.content_uri
                    avatar_state["profile_mxc"] = profile_url
                else:
                    LOGGER.warning("OpenRig profile avatar upload failed: %s", upload)
            if isinstance(profile_url, str):
                response = await self.client.set_avatar(profile_url)
                if not isinstance(response, ProfileSetAvatarResponse):
                    LOGGER.warning("OpenRig profile avatar update failed: %s", response)
        self.state.save()

    async def _create_rooms(self) -> None:
        owner = self.config.allowed_users[0]
        rejected = self.state.data.setdefault("rejected_rooms", {})
        encryption = {
            "type": "m.room.encryption",
            "state_key": "",
            "content": {
                "algorithm": "m.megolm.v1.aes-sha2",
                "rotation_period_ms": 604800000,
                "rotation_period_msgs": 100,
            },
        }
        for key, agent in self.config.agents.items():
            if key in self.state.data["rooms"]:
                continue
            if key in rejected:
                LOGGER.info(
                    "skipping rejected Matrix room for %s",
                    agent.session,
                )
                continue
            name = room_name(agent)
            LOGGER.info("creating Matrix room %s for %s", name, agent.session)
            response = await self.client.room_create(
                visibility=RoomVisibility.private,
                name=name,
                topic=f"OpenRig seat {agent.session}",
                invite=[owner],
                is_direct=True,
                preset=RoomPreset.private_chat,
                initial_state=[encryption],
            )
            if isinstance(response, RoomCreateResponse):
                self.state.data["rooms"][key] = {
                    "room_id": response.room_id,
                    "name": name,
                }
                self.room_agents[response.room_id] = key
                self.state.save()
                continue
            raise RuntimeError(f"room creation failed for {name}: {response}")

    async def _reconcile_room_memberships(self) -> None:
        owner = self.config.allowed_users[0]
        for key, room in list(self.state.data["rooms"].items()):
            agent = self.config.agents.get(key)
            if agent is None:
                continue
            room_id = str(room.get("room_id") or "")
            if not room_id:
                continue
            response = await self.client.room_get_state_event(
                room_id,
                "m.room.member",
                owner,
            )
            if not isinstance(response, RoomGetStateEventResponse):
                LOGGER.warning(
                    "could not read owner membership for %s: %s",
                    room_id,
                    response,
                )
                continue
            membership = str(response.content.get("membership") or "")
            if membership in {"leave", "ban"}:
                await self._reject_agent_room(
                    agent,
                    room_id,
                    reason=f"owner_{membership}",
                )

    async def _reject_agent_room(
        self,
        agent: AgentConfig,
        room_id: str,
        *,
        reason: str,
    ) -> None:
        rejected = self.state.data.setdefault("rejected_rooms", {})
        existing = rejected.get(agent.key)
        if isinstance(existing, dict) and existing.get("room_id") == room_id:
            return
        rejected[agent.key] = {
            "room_id": room_id,
            "reason": reason,
            "rejected_at": int(time.time() * 1000),
        }
        self.state.data["rooms"].pop(agent.key, None)
        self.state.data["pending"].pop(agent.key, None)
        self.state.data["welcomed"].pop(agent.key, None)
        self.state.data["queue_notices"].pop(agent.key, None)
        avatar_pool = self.state.data.get("avatar_pool", {})
        assignments = avatar_pool.get("assignments")
        if isinstance(assignments, dict):
            assignments.pop(agent.key, None)
        self.room_agents.pop(room_id, None)
        self.agent_statuses.pop(agent.key, None)
        self.state.save()
        LOGGER.info(
            "rejected Matrix room %s for %s (%s)",
            room_id,
            agent.session,
            reason,
        )
        response = await self.client.room_leave(room_id)
        if hasattr(response, "status_code") and response.status_code >= 400:
            LOGGER.warning(
                "could not leave rejected Matrix room %s: %s",
                room_id,
                response,
            )

    def _initialize_transcript_offsets(self) -> None:
        offsets = self.state.data.setdefault("transcripts", {})
        try:
            resolved = self.resolver.resolve_many(
                {agent.session for agent in self.config.agents.values()}
            )
        except AgentSessionNotFound:
            resolved = {}
        for key, agent in self.config.agents.items():
            ref = resolved.get(agent.session)
            if ref is None:
                continue
            offsets[key] = {
                "path": str(ref.transcript_path),
                "offset": ref.transcript_path.stat().st_size,
            }
        self.state.save()

    async def _on_invite(self, room: MatrixRoom, event: InviteMemberEvent) -> None:
        if event.sender in self.config.allowed_users:
            await self.client.join(room.room_id)

    def _on_member(self, room: MatrixRoom, event: RoomMemberEvent) -> None:
        asyncio.create_task(self._handle_member(room, event))

    async def _handle_member(self, room: MatrixRoom, event: RoomMemberEvent) -> None:
        if event.state_key not in self.config.allowed_users:
            return
        agent = self._agent_for_room(room.room_id)
        if not agent:
            return
        if event.membership in {"leave", "ban"}:
            await self._reject_agent_room(
                agent,
                room.room_id,
                reason=f"owner_{event.membership}",
            )
            return
        if event.membership != "join" or self.state.data["welcomed"].get(agent.key):
            return
        response = await self._send(
            room.room_id,
            f"Mit **{agent.display_name}** verbunden. Nachrichten in diesem Raum gehen an "
            f"`{agent.session}`.",
        )
        if isinstance(response, RoomSendResponse):
            self.state.data["welcomed"][agent.key] = True
            self.state.save()

    def _on_room_message(self, room: MatrixRoom, event: RoomMessageText) -> None:
        asyncio.create_task(self._handle_room_message(room, event))

    def _on_audio(self, room: MatrixRoom, event: RoomMessageAudio) -> None:
        asyncio.create_task(self._handle_audio(room, event))

    def _on_image(
        self,
        room: MatrixRoom,
        event: RoomMessageImage | RoomEncryptedImage,
    ) -> None:
        asyncio.create_task(self._handle_image(room, event))

    def _on_file(
        self,
        room: MatrixRoom,
        event: RoomMessageFile | RoomEncryptedFile,
    ) -> None:
        asyncio.create_task(self._handle_file(room, event))

    def _on_unknown_event(self, room: MatrixRoom, event: UnknownEvent) -> None:
        if event.type in {POLL_RESPONSE_TYPE, STABLE_POLL_RESPONSE_TYPE}:
            asyncio.create_task(self._handle_poll_response(room, event))

    async def _handle_poll_response(
        self,
        room: MatrixRoom,
        event: UnknownEvent,
    ) -> None:
        if event.sender not in self.config.allowed_users:
            return
        agent = self._agent_for_room(room.room_id)
        if agent is None:
            return
        response = poll_response(event.source.get("content", {}))
        if response is None:
            return
        poll_id, selected = response
        if not selected:
            return

        for decision in self.state.data["decisions"].values():
            question = decision.get("polls", {}).get(poll_id)
            if not isinstance(question, dict):
                continue
            question_id = str(question["id"])
            if question_id in decision.get("answers", {}):
                return
            options = {
                str(option["id"]): str(option["label"])
                for option in question["options"]
            }
            selected_labels = [
                options.get(option_id, option_id)
                for option_id in selected
                if option_id in options
            ]
            if not selected_labels:
                return
            decision.setdefault("answers", {})[question_id] = {
                "option_ids": selected,
                "labels": selected_labels,
                "answered_at": int(time.time() * 1000),
            }

            all_answered = all(
                poll_question["id"] in decision["answers"]
                for poll_question in decision.get("polls", {}).values()
            )
            unpin = (
                decision.get("event_ids", [])
                if all_answered
                else [poll_id]
            )
            await self._remove_pinned(room.room_id, unpin)
            if all_answered:
                decision["closed_at"] = int(time.time() * 1000)
            self.state.save()

            answer_text = ", ".join(selected_labels)
            queue_line = (
                f"Queue-Item: {decision['qitem_id']}\n"
                if decision.get("qitem_id")
                else ""
            )
            await self._enqueue_prompt(
                agent,
                room,
                event.event_id,
                int(event.server_timestamp),
                "Matrix-Entscheidung beantwortet.\n\n"
                f"{queue_line}"
                f"Entscheidung: {decision['title']}\n"
                f"Frage: {question['question']}\n"
                f"Auswahl: {answer_text} ({', '.join(selected)})\n\n"
                "Behandle diese Antwort als Entscheidung und aktualisiere das "
                "zugehörige Queue-Item.",
                "decision",
                str(decision["event_ids"][0]),
            )
            thread_root = str(decision["event_ids"][0])
            await self._send(
                room.room_id,
                f"Antwort an **{agent.display_name}** übergeben: {answer_text}",
                thread_root,
            )
            return

    def _agent_for_room(self, room_id: str) -> AgentConfig | None:
        agent_key = self.room_agents.get(room_id)
        return self.config.agents.get(agent_key) if agent_key else None

    async def _on_reaction(
        self,
        room: MatrixRoom,
        event: ReactionEvent,
    ) -> None:
        try:
            await self._handle_reaction(room, event)
        except Exception:
            LOGGER.exception("Matrix reaction processing failed")

    async def _handle_reaction(
        self,
        room: MatrixRoom,
        event: ReactionEvent,
    ) -> None:
        if not getattr(self.config, "reaction_feedback_enabled", True):
            return
        if event.sender == self.config.user_id:
            return
        agent = self._agent_for_room(room.room_id)
        if agent is None:
            return
        if event.sender not in self.config.allowed_users:
            return
        if event.event_id in self.processed_events:
            return
        if self.config.max_message_age_minutes >= 0:
            age_ms = int(time.time() * 1000) - int(event.server_timestamp)
            if age_ms > self.config.max_message_age_minutes * 60_000:
                return

        self.processed_events.add(event.event_id)
        self.state.data["processed_events"] = list(self.processed_events)[-2000:]
        self.state.save()

        target_text = ""
        try:
            response = await self.client.room_get_event(
                room.room_id,
                event.reacts_to,
            )
        except Exception:
            LOGGER.warning(
                "could not load Matrix reaction target %s",
                event.reacts_to,
                exc_info=True,
            )
            response = None
        if isinstance(response, RoomGetEventResponse):
            target = response.event
            if getattr(target, "sender", None) != self.config.user_id:
                return
            target_text = str(getattr(target, "body", "") or "").strip()

        preview = " ".join(target_text.split())
        if len(preview) > 600:
            preview = preview[:597] + "..."
        feedback = (
            f"Matrix-Reaktion von {event.sender}: {event.key}\n"
            f"Reagiertes Event: {event.reacts_to}\n"
            f"Bezugstext: {preview or '(nicht verfügbar)'}\n\n"
            "Nimm diese Reaktion als stilles Feedback wahr. Wenn sie für "
            "Erinnerungen, Vorlieben oder eine spätere Entscheidung relevant "
            "ist, berücksichtige sie. Erzeuge dafür keine sichtbare "
            "Matrix-Antwort."
        )
        await self._enqueue_prompt(
            agent,
            room,
            event.event_id,
            event.server_timestamp,
            feedback,
            "reaction",
            event.reacts_to,
            suppress_reply=True,
            queue_tracked=False,
        )

    async def _handle_room_message(self, room: MatrixRoom, event: RoomMessageText) -> None:
        if event.sender == self.config.user_id:
            return
        agent = self._agent_for_room(room.room_id)
        if not agent:
            return
        if event.sender not in self.config.allowed_users:
            LOGGER.warning("ignoring Matrix sender %s in %s", event.sender, room.room_id)
            return
        if event.event_id in self.processed_events:
            return
        if self.config.max_message_age_minutes >= 0:
            age_ms = int(time.time() * 1000) - int(event.server_timestamp)
            if age_ms > self.config.max_message_age_minutes * 60_000:
                await self._send(room.room_id, "Nachricht ist zu alt und wurde nicht an den Agenten gesendet.", event.event_id)
                return
        text = event.body.strip()
        if not text:
            return
        if await self._handle_verification_reply(room, event, text):
            return
        relates_to = message_relation(event)
        await self._enqueue_prompt(
            agent,
            room,
            event.event_id,
            event.server_timestamp,
            text,
            relates_to=relates_to,
        )

    async def _handle_verification_reply(
        self,
        room: MatrixRoom,
        event: RoomMessageText,
        text: str,
    ) -> bool:
        decision = verification_decision(text)
        if decision is None:
            return False
        transaction_id = next(
            (
                tx_id
                for tx_id, room_id in self.verification_rooms.items()
                if room_id == room.room_id
            ),
            None,
        )
        if transaction_id is None:
            return False
        self.processed_events.add(event.event_id)
        if decision:
            result = await self.client.confirm_short_auth_string(transaction_id)
            if isinstance(result, ToDeviceError):
                await self._send(room.room_id, f"Bestätigung fehlgeschlagen: {result}", event.event_id)
            else:
                await self._send(
                    room.room_id,
                    "SAS bestätigt. Bestätige den Vorgang jetzt auch im Element-Dialog.",
                    event.event_id,
                )
        else:
            result = await self.client.cancel_key_verification(transaction_id, reject=True)
            if isinstance(result, ToDeviceError):
                await self._send(room.room_id, f"Abbruch fehlgeschlagen: {result}", event.event_id)
            else:
                self.verification_rooms.pop(transaction_id, None)
                await self._send(room.room_id, "Geräteverifikation abgelehnt.", event.event_id)
        return True

    def _verification_room(self) -> str:
        advisor_room = self.state.data["rooms"].get("advisor", {}).get("room_id")
        if advisor_room:
            return str(advisor_room)
        return str(next(iter(self.state.data["rooms"].values()))["room_id"])

    async def _on_verification(self, event: KeyVerificationEvent) -> None:
        if getattr(event, "sender", None) not in self.config.allowed_users:
            return
        room_id = self._verification_room()
        if isinstance(event, KeyVerificationStart):
            if "emoji" not in event.short_authentication_string:
                return
            response = await self.client.accept_key_verification(event.transaction_id)
            if isinstance(response, ToDeviceError):
                await self._send(room_id, f"Matrix-Verifikation konnte nicht gestartet werden: {response}")
                return
            sas = self.client.key_verifications.get(event.transaction_id)
            if sas is not None:
                response = await self.client.to_device(sas.share_key())
                if isinstance(response, ToDeviceError):
                    await self._send(room_id, f"Schlüsselaustausch fehlgeschlagen: {response}")
                    return
            self.verification_rooms[event.transaction_id] = room_id
            await self._send(
                room_id,
                "Matrix-Verifikationsanfrage empfangen. Die Emoji-Prüfung folgt gleich.",
            )
            return

        if isinstance(event, KeyVerificationKey):
            sas = self.client.key_verifications.get(event.transaction_id)
            if sas is None:
                return
            emojis = sas.get_emoji()
            rendered = " ".join(emoji for emoji, _name in emojis)
            named = ", ".join(f"{emoji} {name}" for emoji, name in emojis)
            self.verification_rooms[event.transaction_id] = room_id
            await self._send(
                room_id,
                "Vergleiche diese sieben Emojis mit dem Element-Dialog:\n\n"
                f"{rendered}\n\n{named}\n\n"
                "Wenn sie übereinstimmen, schreibe hier `ja`. Sonst `nein`.",
            )
            return

        if isinstance(event, KeyVerificationMac):
            sas = self.client.key_verifications.get(event.transaction_id)
            if sas is not None and sas.verified:
                self.verification_rooms.pop(event.transaction_id, None)
                await self._send(
                    room_id,
                    f"Gerät `{sas.other_olm_device.id}` wurde erfolgreich verifiziert.",
                )
            return

        if isinstance(event, KeyVerificationCancel):
            self.verification_rooms.pop(event.transaction_id, None)
            await self._send(
                room_id,
                f"Geräteverifikation abgebrochen: {event.reason or event.code}",
            )

    async def _enqueue_prompt(
        self,
        agent: AgentConfig,
        room: MatrixRoom,
        event_id: str,
        event_timestamp: int,
        text: str,
        source_label: str | None = None,
        reply_to_event_id: str | None = None,
        *,
        relates_to: dict[str, Any] | None = None,
        suppress_reply: bool = False,
        queue_tracked: bool = True,
    ) -> None:
        async with self.locks[agent.key]:
            pending = self.state.data["pending"].setdefault(agent.key, [])
            self.processed_events.add(event_id)
            self.state.data["processed_events"] = list(self.processed_events)[-2000:]
            target = (
                ResponseTarget(reply_to_event_id, reply_to_event_id)
                if reply_to_event_id
                else resolve_response_target(event_id, relates_to)
            )
            qitem_id = None
            if queue_tracked:
                qitem_id = await self._create_inbound_qitem(
                    agent,
                    event_id=event_id,
                    text=text,
                    source_label=source_label or "message",
                )
            request = {
                "id": uuid.uuid4().hex,
                "event_id": event_id,
                "reply_to_event_id": target.reply_to_event_id,
                "thread_root_event_id": target.thread_root_event_id,
                "source_event_ids": [event_id],
                "source_relation": dict(relates_to) if relates_to else None,
                "source_label": source_label or "message",
                "qitem_id": qitem_id,
                "queue_tracked": queue_tracked,
                "suppress_reply": suppress_reply,
                "priority": getattr(
                    self.config,
                    "inbound_message_priority",
                    "urgent",
                ),
                "text": text,
                "enqueued_at": time.time(),
                "started_at": None,
                "session_id": None,
                "transcript_path": None,
                "offset": None,
                "answer_text": None,
            }
            pending.append(request)
            self.state.save()
            if len(pending) == 1:
                await self._start_request(agent, pending[0], room.room_id)
            else:
                await self._update_queue_notice(
                    agent,
                    room.room_id,
                    len(pending) - 1,
                )

    async def _create_inbound_qitem(
        self,
        agent: AgentConfig,
        *,
        event_id: str,
        text: str,
        source_label: str,
    ) -> str | None:
        if not getattr(self.config, "queue_inbound_messages", False):
            return None
        source_session = str(
            getattr(self.config, "inbound_queue_source", "")
        ).strip()
        if not source_session:
            LOGGER.warning("inbound queue source is not configured")
            return None
        first_line = next(
            (line.strip() for line in text.splitlines() if line.strip()),
            f"Matrix {source_label}",
        )
        priority = str(
            getattr(self.config, "inbound_message_priority", "urgent")
        )
        try:
            qitem_id = await self.gateway.queue_create(
                source_session=source_session,
                destination_session=agent.session,
                summary=f"Matrix ({source_label}): {first_line[:180]}",
                body=(
                    f"Matrix message for {agent.session}\n"
                    f"Event: {event_id}\n"
                    f"Priority: {priority}\n\n"
                    f"{text}"
                ),
                priority=priority,
            )
        except (GatewayError, OSError):
            LOGGER.warning(
                "could not create priority qitem for Matrix event %s",
                event_id,
                exc_info=True,
            )
            return None
        LOGGER.info(
            "created %s qitem %s for Matrix event %s",
            priority,
            qitem_id,
            event_id,
        )
        return qitem_id

    async def _claim_inbound_qitem(
        self,
        agent: AgentConfig,
        request: dict[str, Any],
    ) -> None:
        qitem_id = str(request.get("qitem_id") or "").strip()
        if not qitem_id:
            return
        try:
            await self.gateway.queue_claim(qitem_id, agent.session)
        except (GatewayError, OSError):
            LOGGER.warning(
                "could not claim Matrix qitem %s",
                qitem_id,
                exc_info=True,
            )

    async def _finish_inbound_qitem(
        self,
        agent: AgentConfig,
        request: dict[str, Any],
    ) -> None:
        qitem_id = str(request.get("qitem_id") or "").strip()
        if not qitem_id:
            return
        try:
            await self.gateway.queue_finish(
                qitem_id,
                agent.session,
                "Matrix answer delivered",
            )
        except (GatewayError, OSError):
            LOGGER.warning(
                "could not finish Matrix qitem %s",
                qitem_id,
                exc_info=True,
            )

    async def _fail_inbound_qitem(
        self,
        agent: AgentConfig,
        request: dict[str, Any],
        resolution: str,
    ) -> None:
        qitem_id = str(request.get("qitem_id") or "").strip()
        if not qitem_id:
            return
        try:
            await self.gateway.queue_fail(
                qitem_id,
                agent.session,
                resolution[:1000],
            )
        except (GatewayError, OSError):
            LOGGER.warning(
                "could not fail Matrix qitem %s",
                qitem_id,
                exc_info=True,
            )

    async def _handle_audio(
        self,
        room: MatrixRoom,
        event: RoomMessageAudio | RoomEncryptedAudio,
    ) -> None:
        if not is_voice_message_content(event.source.get("content", {})):
            await self._handle_file(room, event)
            return
        if event.sender == self.config.user_id:
            return
        agent = self._agent_for_room(room.room_id)
        if not agent:
            return
        if event.sender not in self.config.allowed_users:
            LOGGER.warning("ignoring Matrix sender %s in %s", event.sender, room.room_id)
            return
        if event.event_id in self.processed_events:
            return
        if self.config.max_message_age_minutes >= 0:
            age_ms = int(time.time() * 1000) - int(event.server_timestamp)
            if age_ms > self.config.max_message_age_minutes * 60_000:
                await self._send_to_event(
                    room.room_id,
                    "Sprachnachricht ist zu alt und wurde nicht verarbeitet.",
                    event,
                )
                return
        if not self.config.transcription.enabled:
            await self._send_to_event(
                room.room_id,
                "Sprachnachrichten sind auf dieser Bridge deaktiviert.",
                event,
            )
            return

        self.processed_events.add(event.event_id)
        self.state.data["processed_events"] = list(self.processed_events)[-2000:]
        self.state.save()
        await self._send_to_event(
            room.room_id,
            "Sprachnachricht empfangen, transkribiere …",
            event,
        )
        try:
            download = await self.client.download(mxc=event.url)
            body = getattr(download, "body", None)
            if not isinstance(body, bytes):
                raise ConnectionError(str(download))
            if len(body) > self.config.transcription.max_audio_bytes:
                raise RuntimeError("Sprachnachricht überschreitet das Größenlimit")
            audio = body
            if isinstance(event, RoomEncryptedAudio):
                audio = decrypt_attachment(
                    body,
                    event.key["k"],
                    event.hashes["sha256"],
                    event.iv,
                )
            text = await asyncio.to_thread(self.transcriber.transcribe, audio, event.body)
        except Exception as exc:
            LOGGER.exception("audio transcription failed")
            await self._send_to_event(
                room.room_id,
                f"Sprachnachricht konnte nicht transkribiert werden: {exc}",
                event,
            )
            return
        if not text:
            await self._send_to_event(
                room.room_id,
                "In der Sprachnachricht wurde kein Text erkannt.",
                event,
            )
            return
        await self._send_to_event(room.room_id, f"Transkript:\n{text}", event)
        await self._enqueue_prompt(
            agent,
            room,
            event.event_id,
            event.server_timestamp,
            f"Sprachnachricht von {event.sender} via Matrix (lokal transkribiert):\n\n{text}",
            "audio",
            relates_to=message_relation(event),
        )

    async def _handle_image(
        self,
        room: MatrixRoom,
        event: RoomMessageImage | RoomEncryptedImage,
    ) -> None:
        if not self.config.media.enabled:
            return
        if event.sender == self.config.user_id:
            return
        agent = self._agent_for_room(room.room_id)
        if agent is None or event.sender not in self.config.allowed_users:
            return
        if event.event_id in self.processed_events:
            return
        if self.config.max_message_age_minutes >= 0:
            age_ms = int(time.time() * 1000) - int(event.server_timestamp)
            if age_ms > self.config.max_message_age_minutes * 60_000:
                await self._send_to_event(
                    room.room_id,
                    "Bild ist zu alt und wurde nicht verarbeitet.",
                    event,
                )
                return

        self.processed_events.add(event.event_id)
        self.state.data["processed_events"] = list(self.processed_events)[-2000:]
        self.state.save()
        await self._send_to_event(
            room.room_id,
            "Bild empfangen, lade und entschlüssele …",
            event,
        )
        try:
            download = await self.client.download(mxc=event.url)
            body = getattr(download, "body", None)
            if not isinstance(body, bytes):
                raise ConnectionError(str(download))
            if len(body) > self.config.media.max_image_bytes:
                raise MediaError("encrypted image exceeds the configured size limit")
            image = body
            if isinstance(event, RoomEncryptedImage):
                image = decrypt_attachment(
                    body,
                    event.key["k"],
                    event.hashes["sha256"],
                    event.iv,
                )
            image_type = detect_image_type(image)
            if image_type is None:
                raise MediaError("unsupported image type; use PNG, JPEG, GIF, or WebP")
            if len(image) > self.config.media.max_image_bytes:
                raise MediaError("image exceeds the configured size limit")
            agent_inbox = safe_agent_directory(self.config.media.inbox_dir, agent.key)
            agent_inbox.mkdir(parents=True, exist_ok=True, mode=0o700)
            stored = agent_inbox / f"{uuid.uuid4().hex}{image_type.suffix}"

            def write_image() -> None:
                with stored.open("xb") as handle:
                    handle.write(image)
                stored.chmod(0o600)

            await asyncio.to_thread(write_image)
        except Exception as exc:
            LOGGER.exception("image download failed")
            await self._send_to_event(
                room.room_id,
                f"Bild konnte nicht übernommen werden: {exc}",
                event,
            )
            return

        caption = event.body.strip()
        caption_line = f"\nBildunterschrift: {caption}" if caption else ""
        await self._send_to_event(
            room.room_id,
            f"Bild gespeichert als `{stored.name}`.",
            event,
        )
        await self._enqueue_prompt(
            agent,
            room,
            event.event_id,
            event.server_timestamp,
            f"Bild von {event.sender} via Matrix empfangen.{caption_line}\n\n"
            f"Lokale Datei: {stored}\n"
            f"Format: {image_type.mime_type}, {len(image)} Bytes",
            "image",
            relates_to=message_relation(event),
        )

    async def _handle_file(
        self,
        room: MatrixRoom,
        event: (
            RoomMessageFile
            | RoomEncryptedFile
            | RoomMessageAudio
            | RoomEncryptedAudio
        ),
    ) -> None:
        if not self.config.media.enabled:
            return
        if event.sender == self.config.user_id:
            return
        agent = self._agent_for_room(room.room_id)
        if agent is None or event.sender not in self.config.allowed_users:
            return
        if event.event_id in self.processed_events:
            return
        if self.config.max_message_age_minutes >= 0:
            age_ms = int(time.time() * 1000) - int(event.server_timestamp)
            if age_ms > self.config.max_message_age_minutes * 60_000:
                await self._send(
                    room.room_id,
                    "Datei ist zu alt und wurde nicht verarbeitet.",
                    event.event_id,
                )
                return

        self.processed_events.add(event.event_id)
        self.state.data["processed_events"] = list(self.processed_events)[-2000:]
        self.state.save()
        await self._send(
            room.room_id,
            "Datei empfangen, lade und entschlüssele …",
            event.event_id,
        )
        try:
            content = event.source.get("content", {})
            filename = str(content.get("filename") or event.body or "attachment")
            info = content.get("info", {})
            mime_type = normalize_mime_type(
                info.get("mimetype")
                or getattr(event, "mimetype", None)
                or "application/octet-stream"
            )
            declared_size = info.get("size")
            if declared_size is not None:
                if (
                    isinstance(declared_size, bool)
                    or not isinstance(declared_size, int)
                    or declared_size < 0
                ):
                    raise MediaError("Angekündigte Dateigröße ist ungültig")
                if declared_size > self.config.media.max_file_bytes:
                    raise MediaError(
                        "Datei überschreitet das konfigurierte Limit von "
                        f"{self.config.media.max_file_bytes} Bytes"
                    )
            download = await self.client.download(mxc=event.url)
            body = getattr(download, "body", None)
            if not isinstance(body, bytes):
                raise ConnectionError(str(download))
            if len(body) > self.config.media.max_file_bytes + 4096:
                raise MediaError(
                    "Datei überschreitet das konfigurierte Limit von "
                    f"{self.config.media.max_file_bytes} Bytes"
                )
            attachment = body
            if isinstance(event, (RoomEncryptedAudio, RoomEncryptedFile)):
                attachment = decrypt_attachment(
                    body,
                    event.key["k"],
                    event.hashes["sha256"],
                    event.iv,
                )
            if not attachment:
                raise MediaError("Datei ist leer")
            if len(attachment) > self.config.media.max_file_bytes:
                raise MediaError(
                    "Datei überschreitet das konfigurierte Limit von "
                    f"{self.config.media.max_file_bytes} Bytes"
                )
            if declared_size is not None and declared_size != len(attachment):
                raise MediaError(
                    "Angekündigte Dateigröße stimmt nicht mit den empfangenen "
                    f"Bytes überein ({declared_size} statt {len(attachment)})"
                )

            suffix = Path(filename).suffix
            if not re.fullmatch(r"\.[A-Za-z0-9]{1,10}", suffix):
                suffix = ".bin"
            agent_inbox = safe_agent_directory(self.config.media.inbox_dir, agent.key)
            agent_inbox.mkdir(parents=True, exist_ok=True, mode=0o700)
            stored = agent_inbox / f"{uuid.uuid4().hex}{suffix.lower()}"
            temporary = agent_inbox / f".{stored.name}.part"

            def write_file() -> None:
                try:
                    with temporary.open("xb") as handle:
                        handle.write(attachment)
                        handle.flush()
                        os.fsync(handle.fileno())
                    temporary.chmod(0o600)
                    os.replace(temporary, stored)
                finally:
                    try:
                        temporary.unlink()
                    except FileNotFoundError:
                        pass

            await asyncio.to_thread(write_file)
        except Exception as exc:
            LOGGER.exception("file download failed")
            await self._send(
                room.room_id,
                f"Datei konnte nicht übernommen werden: {exc}",
                event.event_id,
            )
            return

        await self._send(
            room.room_id,
            f"Datei gespeichert als `{stored.name}`.",
            event.event_id,
        )
        await self._enqueue_prompt(
            agent,
            room,
            event.event_id,
            event.server_timestamp,
            f"Datei von {event.sender} via Matrix empfangen.\n\n"
            f"Lokale Datei: {stored}\n"
            f"Originaldateiname: {json.dumps(filename, ensure_ascii=False)}\n"
            f"Medientyp: {mime_type}\n"
            f"Größe: {len(attachment)} Bytes",
            "attachment",
        )

    async def _start_request(
        self,
        agent: AgentConfig,
        request: dict[str, Any],
        room_id: str,
        ref: AgentSessionRef | None = None,
    ) -> None:
        try:
            ref = ref or await asyncio.to_thread(
                self.resolver.resolve,
                agent.session,
            )
            if ref.busy:
                return
            request["session_id"] = ref.session_id
            request["transcript_path"] = str(ref.transcript_path)
            request["offset"] = ref.transcript_path.stat().st_size
            request["started_at"] = time.time()
            await self._claim_inbound_qitem(agent, request)
            await self.gateway.send_prompt(ref, request["text"])
            if not request.get("suppress_reply"):
                await self._set_typing(room_id, True)
                await self._update_agent_status(agent, "busy", "arbeitet")
        except (AgentSessionNotFound, GatewayError, OSError) as exc:
            LOGGER.exception("prompt failed for %s", agent.session)
            await self._fail_inbound_qitem(
                agent,
                request,
                f"Prompt failed: {exc}",
            )
            await self._set_typing(room_id, False)
            await self._update_agent_status(agent, "error", "Prompt fehlgeschlagen")
            await self._send(
                room_id,
                f"Prompt fehlgeschlagen: {exc}",
                self._reply_target(request),
                **self._delivery_kwargs(request),
            )
            pending = self.state.data["pending"][agent.key]
            pending.pop(0)
            self.state.save()
            await self._update_queue_notice(
                agent,
                room_id,
                max(0, len(pending) - 1),
            )
            if pending:
                await self._start_request(agent, pending[0], room_id)
            return
        self.state.save()

    async def _poll_answers(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.config.poll_interval_seconds)
                for agent in self.config.agents.values():
                    await self._poll_agent(agent)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("answer polling failed")

    async def _poll_agent(self, agent: AgentConfig) -> None:
        async with self.locks[agent.key]:
            pending = self.state.data["pending"].get(agent.key, [])
            if not pending:
                notice = self.state.data.get("queue_notices", {}).get(agent.key, {})
                if notice.get("pinned"):
                    await self._update_queue_notice(
                        agent,
                        self._room_for_agent(agent.key),
                        0,
                    )
                try:
                    ref = await asyncio.to_thread(
                        self.resolver.resolve,
                        agent.session,
                    )
                except AgentSessionNotFound:
                    return
                self.state.data["transcripts"][agent.key] = {
                    "path": str(ref.transcript_path),
                    "offset": ref.transcript_path.stat().st_size,
                }
                return

            request = pending[0]
            if request.get("started_at") is None:
                try:
                    ref = self.resolver.resolve(agent.session)
                except AgentSessionNotFound:
                    return
                if ref.busy:
                    return
                await self._start_request(
                    agent,
                    request,
                    self._room_for_agent(agent.key),
                    ref,
                )
                return
            if request.get("answer_text") is None:
                if request.get("started_at") and time.time() - request["started_at"] > self.config.response_timeout_minutes * 60:
                    await self._fail_inbound_qitem(
                        agent,
                        request,
                        "Timed out waiting for a Matrix answer",
                    )
                    await self._send(
                        self._room_for_agent(agent.key),
                        "Der Agent hat für diese Anfrage nicht rechtzeitig geantwortet.",
                        self._reply_target(request),
                        **self._delivery_kwargs(request),
                    )
                    await self._set_typing(self._room_for_agent(agent.key), False)
                    await self._update_agent_status(agent, "error", "Zeitüberschreitung")
                    pending.pop(0)
                    self.state.save()
                    await self._update_queue_notice(
                        agent,
                        self._room_for_agent(agent.key),
                        max(0, len(pending) - 1),
                    )
                    return
                path = Path(request["transcript_path"])
                try:
                    records, offset = read_complete_records(path, int(request["offset"]))
                except OSError:
                    return
                sent_commentary = request.setdefault("sent_commentary", [])
                sent_ids = set(sent_commentary)
                if (
                    self.config.mirror_commentary
                    and not request.get("suppress_reply")
                    and not request.get("commentary_skipped")
                ):
                    pending_commentary = [
                        message
                        for message in commentary_messages(records)
                        if message[0] not in sent_ids
                    ]
                    if pending_commentary:
                        room_id = self._room_for_agent(agent.key)
                        commentary_event_id = request.get("commentary_event_id")
                        if commentary_event_id:
                            try:
                                response = await self._edit_message(
                                    room_id,
                                    str(commentary_event_id),
                                    pending_commentary[-1][1],
                                )
                            except Exception:
                                response = None
                                LOGGER.warning(
                                    "commentary edit raised for %s",
                                    agent.key,
                                    exc_info=True,
                                )
                        else:
                            response = await self._deliver_to_request(
                                room_id,
                                pending_commentary[-1][1],
                                request,
                            )
                        if not isinstance(response, RoomSendResponse):
                            attempts = request.get("commentary_failure_count", 0) + 1
                            request["commentary_failure_count"] = attempts
                            LOGGER.error(
                                "commentary delivery failed for %s (attempt %s/%s): %s",
                                agent.key,
                                attempts,
                                MAX_DELIVERY_ATTEMPTS,
                                response,
                            )
                            if attempts < MAX_DELIVERY_ATTEMPTS:
                                self.state.save()
                                return
                            request["commentary_skipped"] = True
                            LOGGER.error(
                                "skipping commentary for %s after %s attempts",
                                agent.key,
                                attempts,
                            )
                            self.state.save()
                        if not commentary_event_id and isinstance(
                            response,
                            RoomSendResponse,
                        ):
                            request["commentary_event_id"] = response.event_id
                        if isinstance(response, RoomSendResponse):
                            request["commentary_failure_count"] = 0
                            sent_commentary.extend(
                                message_id for message_id, _text in pending_commentary
                            )
                        self.state.save()
                request["offset"] = offset
                answers = [text for record in records if (text := final_answer(record))]
                if answers:
                    request["answer_text"] = answers[-1]
                self.state.save()

            if request.get("answer_text") is None:
                return

            room_id = self._room_for_agent(agent.key)
            if request.get("suppress_reply"):
                await self._finish_inbound_qitem(agent, request)
                pending.pop(0)
                self.state.save()
                await self._update_queue_notice(
                    agent,
                    room_id,
                    max(0, len(pending) - 1),
                )
                if pending:
                    await self._start_request(agent, pending[0], room_id)
                return
            response = await self._deliver_to_request(
                room_id,
                request["answer_text"],
                request,
            )
            if not isinstance(response, RoomSendResponse):
                attempts = request.get("delivery_failure_count", 0) + 1
                request["delivery_failure_count"] = attempts
                self.state.save()
                LOGGER.error(
                    "answer delivery failed for %s (attempt %s/%s): %s",
                    agent.key,
                    attempts,
                    MAX_DELIVERY_ATTEMPTS,
                    response,
                )
                if attempts < MAX_DELIVERY_ATTEMPTS:
                    return
                await self._fail_inbound_qitem(
                    agent,
                    request,
                    f"Matrix answer delivery failed after {attempts} attempts",
                )
                await self._set_typing(room_id, False)
                await self._update_agent_status(agent, "error", "Zustellung fehlgeschlagen")
                pending.pop(0)
                self.state.save()
                await self._update_queue_notice(
                    agent,
                    room_id,
                    max(0, len(pending) - 1),
                )
                if pending:
                    await self._start_request(agent, pending[0], room_id)
                return
            await self._finish_inbound_qitem(agent, request)
            await self._set_typing(room_id, False)
            await self._update_agent_status(agent, "ready", "bereit")
            pending.pop(0)
            self.state.save()
            await self._update_queue_notice(
                agent,
                room_id,
                max(0, len(pending) - 1),
            )
            if pending:
                await self._start_request(agent, pending[0], room_id)

    def _room_for_agent(self, agent_key: str) -> str:
        return str(self.state.data["rooms"][agent_key]["room_id"])

    @staticmethod
    def _reply_target(request: dict[str, Any]) -> str:
        return str(request.get("reply_to_event_id") or request["event_id"])

    @staticmethod
    def _delivery_kwargs(request: dict[str, Any]) -> dict[str, Any]:
        if "thread_root_event_id" in request:
            return {"thread_root": request["thread_root_event_id"]}
        return {}

    async def _deliver_to_request(
        self,
        room_id: str,
        text: str,
        request: dict[str, Any],
    ):
        try:
            return await self._send(
                room_id,
                text,
                self._reply_target(request),
                **self._delivery_kwargs(request),
            )
        except Exception:
            LOGGER.warning("Matrix delivery raised for %s", request["event_id"], exc_info=True)
            return None

    async def _poll_agent_statuses(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.config.status_refresh_seconds)
                await self._refresh_agent_statuses()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("agent status refresh failed")

    async def _refresh_agent_statuses(self, force: bool = False) -> None:
        try:
            resolved = await asyncio.to_thread(
                self.resolver.resolve_many,
                {agent.session for agent in self.config.agents.values()},
            )
        except AgentSessionNotFound:
            resolved = {}
        for agent in self.config.agents.values():
            if not force and agent.key not in self.agent_statuses:
                continue
            if self.state.data["pending"].get(agent.key):
                status, detail = "busy", "arbeitet"
            else:
                ref = resolved.get(agent.session)
                if ref is None:
                    status, detail = "offline", "offline"
                else:
                    status, detail = ("busy", "arbeitet") if ref.busy else ("ready", "bereit")
            await self._update_agent_status(agent, status, detail)

    async def _update_agent_status(
        self,
        agent: AgentConfig,
        status: str,
        detail: str,
    ) -> None:
        if self.agent_statuses.get(agent.key) == status:
            return
        room = self.state.data["rooms"].get(agent.key)
        if not room:
            return
        room_id = str(room["room_id"])
        status_topic = f"{STATUS_LABELS[status]} {detail} · {agent.session}"
        try:
            await self.client.room_put_state(
                room_id,
                "m.room.topic",
                {"topic": status_topic},
            )
            await self.client.room_put_state(
                room_id,
                AGENT_STATUS_EVENT,
                {
                    "state": status,
                    "detail": detail,
                    "session": agent.session,
                    "updated_at": int(time.time() * 1000),
                },
            )
        except Exception:
            LOGGER.warning("failed to update Matrix status for %s", agent.session, exc_info=True)
            return
        self.agent_statuses[agent.key] = status

    async def _set_typing(self, room_id: str, typing: bool) -> None:
        existing = self.typing_tasks.pop(room_id, None)
        if existing:
            existing.cancel()
            await asyncio.gather(existing, return_exceptions=True)
        if typing:
            self.typing_tasks[room_id] = asyncio.create_task(
                self._typing_loop(room_id),
                name=f"typing:{room_id}",
            )
            return
        try:
            await self.client.room_typing(room_id, False)
        except Exception:
            LOGGER.debug("failed to clear Matrix typing state", exc_info=True)

    async def _typing_loop(self, room_id: str) -> None:
        while True:
            try:
                response = await self.client.room_typing(room_id, True, timeout=30000)
                if hasattr(response, "status_code") and response.status_code >= 400:
                    LOGGER.debug("Matrix typing update failed: %s", response)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.debug("Matrix typing update failed", exc_info=True)
            await asyncio.sleep(20)

    async def _edit_message(self, room_id: str, event_id: str, text: str):
        new_content = formatted_message_content(text)
        content = {
            **new_content,
            "body": f"* {new_content['body']}",
            "m.new_content": new_content,
            "m.relates_to": {
                "rel_type": "m.replace",
                "event_id": event_id,
            },
        }
        if "formatted_body" in new_content:
            content["formatted_body"] = f"* {new_content['formatted_body']}"
        return await self.client.room_send(
            room_id,
            "m.room.message",
            content,
            ignore_unverified_devices=True,
        )

    async def _send_to_event(self, room_id: str, text: str, event: Any):
        target = resolve_response_target(event.event_id, message_relation(event))
        return await self._send(
            room_id,
            text,
            target.reply_to_event_id,
            thread_root=target.thread_root_event_id,
        )

    async def _send(
        self,
        room_id: str,
        text: str,
        reply_to: str | None = None,
        *,
        thread_root: str | None | object = _DEFAULT_THREAD_ROOT,
    ):
        content = formatted_message_content(text)
        effective_root = reply_to if thread_root is _DEFAULT_THREAD_ROOT else thread_root
        if effective_root:
            content["m.relates_to"] = {
                "rel_type": "m.thread",
                "event_id": effective_root,
                "is_falling_back": True,
                "m.in_reply_to": {"event_id": effective_root},
            }
        elif reply_to:
            content["m.relates_to"] = {"m.in_reply_to": {"event_id": reply_to}}
        return await self.client.room_send(
            room_id,
            "m.room.message",
            content,
            ignore_unverified_devices=True,
        )

