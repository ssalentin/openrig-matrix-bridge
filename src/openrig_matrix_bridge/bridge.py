from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from nio import (
    AsyncClient,
    AsyncClientConfig,
    InviteMemberEvent,
    MatrixRoom,
    RoomCreateResponse,
    RoomEncryptedAudio,
    RoomMemberEvent,
    RoomMessageAudio,
    RoomMessageText,
    RoomSendResponse,
    SyncResponse,
)
from nio.api import RoomPreset, RoomVisibility
from nio.crypto import decrypt_attachment

from .config import AgentConfig, BridgeConfig
from .gateway import (
    AgentSessionNotFound,
    AgentSessionResolver,
    GatewayError,
    MoshiGateway,
)
from .transcript import final_answer, read_complete_records
from .transcription import Transcriber

LOGGER = logging.getLogger("openrig-matrix-bridge")


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
        self.resolver = AgentSessionResolver(config.codex_sessions_dir)
        self.gateway = MoshiGateway(config.gateway_url)
        self.transcriber = Transcriber(config.transcription)
        self.locks = {key: asyncio.Lock() for key in config.agents}
        self.room_agents: dict[str, str] = {}
        self.processed_events: set[str] = set(self.state.data.get("processed_events", []))
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
        self.client.add_event_callback(self._on_audio, (RoomMessageAudio, RoomEncryptedAudio))
        self.client.add_event_callback(self._on_invite, InviteMemberEvent)
        self.client.add_event_callback(self._on_member, RoomMemberEvent)

    async def run(self) -> None:
        await self._initial_sync()
        self._load_room_index()
        if any(key not in self.state.data["rooms"] for key in self.config.agents):
            await self._create_rooms()
        self._initialize_transcript_offsets()
        poller = asyncio.create_task(self._poll_answers(), name="openrig-matrix-poller")
        try:
            await self.client.sync_forever(
                timeout=30000,
                full_state=True,
                set_presence="online",
            )
        finally:
            poller.cancel()
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

    async def _create_rooms(self) -> None:
        owner = self.config.allowed_users[0]
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
            name = f"OpenRig · {agent.display_name}"
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

    def _initialize_transcript_offsets(self) -> None:
        offsets = self.state.data.setdefault("transcripts", {})
        for key, agent in self.config.agents.items():
            try:
                ref = self.resolver.resolve(agent.session)
            except AgentSessionNotFound:
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
        if event.membership != "join" or event.state_key not in self.config.allowed_users:
            return
        agent = self._agent_for_room(room.room_id)
        if not agent or self.state.data["welcomed"].get(agent.key):
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

    def _agent_for_room(self, room_id: str) -> AgentConfig | None:
        agent_key = self.room_agents.get(room_id)
        return self.config.agents.get(agent_key) if agent_key else None

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
        await self._enqueue_prompt(agent, room, event.event_id, event.server_timestamp, text)

    async def _enqueue_prompt(
        self,
        agent: AgentConfig,
        room: MatrixRoom,
        event_id: str,
        event_timestamp: int,
        text: str,
        source_label: str | None = None,
    ) -> None:
        async with self.locks[agent.key]:
            pending = self.state.data["pending"].setdefault(agent.key, [])
            request = {
                "id": uuid.uuid4().hex,
                "event_id": event_id,
                "text": text,
                "enqueued_at": time.time(),
                "started_at": None,
                "session_id": None,
                "transcript_path": None,
                "offset": None,
                "answer_text": None,
            }
            pending.append(request)
            self.processed_events.add(event_id)
            self.state.data["processed_events"] = list(self.processed_events)[-2000:]
            self.state.save()
            if len(pending) == 1:
                await self._start_request(agent, pending[0], room.room_id)
            else:
                await self._send(
                    room.room_id,
                    f"Warteschlange: {len(pending) - 1} weitere Nachricht(en) vor dir.",
                    event_id,
                )

    async def _handle_audio(
        self,
        room: MatrixRoom,
        event: RoomMessageAudio | RoomEncryptedAudio,
    ) -> None:
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
                await self._send(
                    room.room_id,
                    "Sprachnachricht ist zu alt und wurde nicht verarbeitet.",
                    event.event_id,
                )
                return
        if not self.config.transcription.enabled:
            await self._send(
                room.room_id,
                "Sprachnachrichten sind auf dieser Bridge deaktiviert.",
                event.event_id,
            )
            return

        self.processed_events.add(event.event_id)
        self.state.data["processed_events"] = list(self.processed_events)[-2000:]
        self.state.save()
        await self._send(room.room_id, "Sprachnachricht empfangen, transkribiere …", event.event_id)
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
            await self._send(
                room.room_id,
                f"Sprachnachricht konnte nicht transkribiert werden: {exc}",
                event.event_id,
            )
            return
        if not text:
            await self._send(
                room.room_id,
                "In der Sprachnachricht wurde kein Text erkannt.",
                event.event_id,
            )
            return
        await self._send(room.room_id, f"Transkript:\n{text}", event.event_id)
        await self._enqueue_prompt(
            agent,
            room,
            event.event_id,
            event.server_timestamp,
            f"Sprachnachricht von {event.sender} via Matrix (lokal transkribiert):\n\n{text}",
            "audio",
        )

    async def _start_request(self, agent: AgentConfig, request: dict[str, Any], room_id: str) -> None:
        try:
            ref = self.resolver.resolve(agent.session)
            request["session_id"] = ref.session_id
            request["transcript_path"] = str(ref.transcript_path)
            request["offset"] = ref.transcript_path.stat().st_size
            request["started_at"] = time.time()
            await self.gateway.send_prompt(ref, request["text"])
        except (AgentSessionNotFound, GatewayError, OSError) as exc:
            LOGGER.exception("prompt failed for %s", agent.session)
            await self._send(room_id, f"Prompt fehlgeschlagen: {exc}", request["event_id"])
            pending = self.state.data["pending"][agent.key]
            pending.pop(0)
            self.state.save()
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
                try:
                    ref = self.resolver.resolve(agent.session)
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
                await self._start_request(agent, request, self._room_for_agent(agent.key))
                return
            if request.get("answer_text") is None:
                if request.get("started_at") and time.time() - request["started_at"] > self.config.response_timeout_minutes * 60:
                    await self._send(
                        self._room_for_agent(agent.key),
                        "Der Agent hat für diese Anfrage nicht rechtzeitig geantwortet.",
                        request["event_id"],
                    )
                    pending.pop(0)
                    self.state.save()
                    return
                path = Path(request["transcript_path"])
                try:
                    records, offset = read_complete_records(path, int(request["offset"]))
                except OSError:
                    return
                request["offset"] = offset
                answers = [text for record in records if (text := final_answer(record))]
                if answers:
                    request["answer_text"] = answers[-1]
                    self.state.save()

            if request.get("answer_text") is None:
                return

            room_id = self._room_for_agent(agent.key)
            response = await self._send(room_id, request["answer_text"], request["event_id"])
            if not isinstance(response, RoomSendResponse):
                LOGGER.error("answer delivery failed for %s: %s", agent.key, response)
                return
            pending.pop(0)
            self.state.save()
            if pending:
                await self._start_request(agent, pending[0], room_id)

    def _room_for_agent(self, agent_key: str) -> str:
        return str(self.state.data["rooms"][agent_key]["room_id"])

    async def _send(self, room_id: str, text: str, reply_to: str | None = None):
        content: dict[str, Any] = {"msgtype": "m.text", "body": text[:60000]}
        if reply_to:
            content["m.relates_to"] = {
                "rel_type": "m.thread",
                "event_id": reply_to,
                "is_falling_back": True,
                "m.in_reply_to": {"event_id": reply_to},
            }
        return await self.client.room_send(
            room_id,
            "m.room.message",
            content,
            ignore_unverified_devices=True,
        )

