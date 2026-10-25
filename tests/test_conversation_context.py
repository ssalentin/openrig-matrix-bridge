"""Related owner messages reach the agent as one complete, ordered context."""

import asyncio
import copy
import hashlib
import json
import re
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from nio import RoomMessageAudio, RoomSendResponse
from nio.crypto import encrypt_attachment
from nio.events.room_events import RoomMessage

from openrig_matrix_bridge.bridge import MatrixBridge
from openrig_matrix_bridge.config import AgentConfig

ROOM_ID = "!ebay:matrix.example.org"
OWNER = "@owner:matrix.example.org"
T0 = 1_700_000_000_000


def user_record(turn_id: str, text: str) -> dict:
    return {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": text}],
            "internal_chat_message_metadata_passthrough": {"turn_id": turn_id},
        },
    }


def answer_record(turn_id: str, text: str) -> dict:
    return {
        "type": "event_msg",
        "payload": {
            "type": "item_completed",
            "turn_id": turn_id,
            "item": {
                "type": "AgentMessage",
                "id": f"answer-{turn_id}",
                "phase": "final_answer",
                "content": [{"type": "Text", "text": text}],
            },
        },
    }


class ContextHarness:
    """A bridge with real handlers, a fake OpenRig seat and fake Matrix media."""

    def __init__(self, tmp_path: Path, *, data: dict | None = None):
        self.tmp_path = tmp_path
        self.transcript = tmp_path / "transcript.jsonl"
        self.transcript.touch()
        self.agent = AgentConfig(
            key="ebay",
            display_name="eBay",
            session="ebay-lead@kernel",
        )
        self.busy = False
        self.prompts: list[str] = []
        self.steers: list[str] = []
        self.qitems: dict[str, str] = {}
        self.qitem_states: dict[str, str] = {}
        self.sent: list[dict] = []
        self.blobs: dict[str, bytes] = {}
        self.downloads: list[str] = []
        self.crash_on: str | None = None
        self.failing: set[str] = set()
        self.transcribe_gate = threading.Event()
        self.transcribe_gate.set()
        self.transcribing = threading.Event()
        bridge = object.__new__(MatrixBridge)
        bridge.config = SimpleNamespace(
            user_id="@openrig:matrix.example.org",
            allowed_users=(OWNER,),
            max_message_age_minutes=-1,
            agents={"ebay": self.agent},
            media=SimpleNamespace(
                enabled=True,
                inbox_dir=tmp_path / "inbox",
                max_image_bytes=1024 * 1024,
                max_file_bytes=1024 * 1024,
            ),
            transcription=SimpleNamespace(enabled=True, max_audio_bytes=1024 * 1024),
            mirror_commentary=False,
            response_timeout_minutes=30,
            steering_mode="auto",
            steering_max_chars=120,
            steering_batch_window_seconds=0.001,
            steering_admission_timeout_seconds=2.0,
        )
        bridge.room_agents = {ROOM_ID: "ebay"}
        bridge.locks = {"ebay": asyncio.Lock()}
        self.data = data if data is not None else {
            "pending": {},
            "processed_events": [],
            "rooms": {"ebay": {"room_id": ROOM_ID}},
        }
        bridge.processed_events = set(self.data["processed_events"])
        bridge.state = SimpleNamespace(save=lambda: None, data=self.data)
        bridge.resolver = self
        bridge.gateway = self
        bridge.client = self
        bridge.transcriber = self

        async def create_qitem(agent, *, event_id, text, source_label):
            qitem_id = f"q-{len(self.qitems) + 1}"
            self.qitems[qitem_id] = event_id
            return qitem_id

        async def nothing(*args, **kwargs):
            return None

        async def send(room_id, text, reply_to=None, **kwargs):
            self.sent.append({"text": text, "reply_to": reply_to, **kwargs})
            return RoomSendResponse(f"$sent-{len(self.sent)}", room_id)

        bridge._create_inbound_qitem = create_qitem
        bridge._update_queue_notice = nothing
        bridge._set_typing = nothing
        bridge._update_agent_status = nothing
        bridge._send = send
        bridge._edit_message = send
        self.bridge = bridge

    # Fake OpenRig seat -------------------------------------------------
    def resolve(self, session):
        return SimpleNamespace(
            session_id="session-one",
            transcript_path=self.transcript,
            busy=self.busy,
        )

    async def queue_claim(self, qitem_id, session):
        self.qitem_states[qitem_id] = "in-progress"

    async def queue_finish(self, qitem_id, session, resolution):
        self.qitem_states[qitem_id] = "done"

    async def queue_fail(self, qitem_id, session, resolution):
        self.qitem_states[qitem_id] = "failed"

    async def queue_show(self, qitem_id):
        return {"state": self.qitem_states.get(qitem_id, "pending")}

    def append(self, record: dict) -> None:
        with self.transcript.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")

    async def send_prompt(self, ref, text):
        self.prompts.append(text)
        self.append(user_record(f"turn-{len(self.prompts)}", text))
        self.busy = True

    async def steer_prompt(self, ref, text):
        self.steers.append(text)
        self.append(user_record(f"turn-{len(self.prompts)}", text))

    def finish_turn(self) -> None:
        self.append(answer_record(f"turn-{len(self.prompts)}", "Antwort"))
        self.busy = False
        self.poll()

    def poll(self) -> None:
        asyncio.run(self.bridge._poll_agent(self.agent))

    # Fake Matrix media ---------------------------------------------------
    async def download(self, mxc):
        self.downloads.append(mxc)
        if mxc == self.crash_on:
            raise SystemExit(86)
        if mxc in self.failing:
            return SimpleNamespace(body=None, message="M_NOT_FOUND")
        return SimpleNamespace(body=self.blobs[mxc])

    def transcribe(self, audio, filename):
        self.transcribing.set()
        self.transcribe_gate.wait(5)
        return "Bitte diese Fotos einstellen"

    def encrypted(self, mxc: str, data: bytes) -> dict:
        ciphertext, keys = encrypt_attachment(data)
        self.blobs[mxc] = ciphertext
        return {"url": mxc, **keys}

    # Owner events --------------------------------------------------------
    def text_event(self, event_id, body, seconds, relation=None):
        content = {"msgtype": "m.text", "body": body}
        if relation:
            content["m.relates_to"] = relation
        return SimpleNamespace(
            event_id=event_id,
            sender=OWNER,
            server_timestamp=T0 + int(seconds * 1000),
            body=body,
            source={"content": content},
        )

    def gallery_event(self, event_id, seconds, count=4, relation=None):
        items = []
        for index in range(count):
            data = b"\x00\x00\x00\x18ftypheic" + f"{event_id}:{index}".encode() * 8
            items.append(
                {
                    "itemtype": "m.image",
                    "body": f"{event_id[1:]}-{index}.heic",
                    "filename": f"{event_id[1:]}-{index}.heic",
                    "file": self.encrypted(f"mxc://example.org/{event_id[1:]}-{index}", data),
                    "info": {"mimetype": "image/heic", "size": len(data)},
                }
            )
        content = {"msgtype": "dm.filament.gallery", "body": "", "itemtypes": items}
        if relation:
            content["m.relates_to"] = relation
        return RoomMessage.parse_decrypted_event(
            {
                "type": "m.room.message",
                "event_id": event_id,
                "sender": OWNER,
                "origin_server_ts": T0 + int(seconds * 1000),
                "room_id": ROOM_ID,
                "content": content,
            }
        )

    def voice_event(self, event_id, seconds):
        mxc = f"mxc://example.org/{event_id[1:]}"
        self.blobs[mxc] = b"opus audio " + event_id.encode()
        event = RoomMessageAudio(
            {
                "event_id": event_id,
                "sender": OWNER,
                "origin_server_ts": T0 + int(seconds * 1000),
                "type": "m.room.message",
                "content": {"msgtype": "m.audio", "url": mxc, "body": "voice.ogg"},
            },
            mxc,
            "voice.ogg",
        )
        event.source["content"]["org.matrix.msc3245.voice"] = {}
        return event

    def image_event(self, event_id, seconds, data=b"\x89PNG\r\n\x1a\nphoto"):
        mxc = f"mxc://example.org/{event_id[1:]}"
        self.blobs[mxc] = data
        return SimpleNamespace(
            event_id=event_id,
            sender=OWNER,
            server_timestamp=T0 + int(seconds * 1000),
            url=mxc,
            body="photo.png",
            source={"content": {"msgtype": "m.image", "url": mxc, "body": "photo.png"}},
        )

    def handle(self, event) -> None:
        asyncio.run(self.dispatch(event))

    async def dispatch(self, event) -> None:
        room = SimpleNamespace(room_id=ROOM_ID)
        if isinstance(event, RoomMessageAudio):
            await self.bridge._handle_audio(room, event)
        elif getattr(event, "msgtype", None) == "dm.filament.gallery":
            await self.bridge._handle_gallery(room, event)
        elif getattr(event, "url", None):
            await self.bridge._handle_image(room, event)
        else:
            await self.bridge._handle_room_message(room, event)

    @property
    def pending(self) -> list[dict]:
        return self.data["pending"].get("ebay", [])

    def answers(self) -> list[dict]:
        return [item for item in self.sent if item["text"] == "Antwort"]


def local_files(prompt: str) -> list[Path]:
    return [Path(value) for value in re.findall(r"Lokale Datei: (\S+)", prompt)]


def assert_digests(prompt: str) -> None:
    for block in prompt.split("Lokale Datei: ")[1:]:
        path = Path(block.split()[0])
        digest = re.search(r"SHA-256: ([0-9a-f]{64})", block).group(1)
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest


def test_text_joins_a_gallery_that_is_still_waiting_in_the_queue(tmp_path):
    harness = ContextHarness(tmp_path)
    harness.busy = True  # another turn keeps the seat busy
    harness.handle(harness.gallery_event("$gallery", 0))
    harness.handle(harness.text_event("$text", "Bitte beschreiben", 1.1))

    assert len(harness.pending) == 1
    request = harness.pending[0]
    assert request["source_event_ids"] == ["$gallery", "$text"]
    assert sorted(harness.qitems.values()) == ["$gallery", "$text"]

    harness.busy = False
    harness.poll()
    assert len(harness.prompts) == 1
    prompt = harness.prompts[0]
    assert prompt.index("Teil 1 · Galerie · $gallery") < prompt.index(
        "Teil 2 · Text · $text\nBitte beschreiben"
    )
    assert len(local_files(prompt)) == 4
    assert_digests(prompt)

    harness.finish_turn()
    (answer,) = harness.answers()
    assert answer["reply_to"] == "$gallery"
    assert answer["thread_root"] == "$gallery"
    assert answer["source_event_ids"] == ["$gallery", "$text"]
    assert sorted(harness.qitem_states.values()) == ["done", "done"]


def test_gallery_steers_into_the_active_turn_of_the_text_before_it(tmp_path):
    harness = ContextHarness(tmp_path)
    harness.handle(harness.text_event("$text", "Ich schicke gleich Fotos", 0))
    assert harness.prompts == ["Ich schicke gleich Fotos"]

    harness.handle(harness.gallery_event("$gallery", 5, count=7))
    time.sleep(0.01)  # past the steering batch window
    harness.poll()

    # The 7-item gallery is far beyond steering_max_chars=120, yet linked
    # context is steered whole, never truncated or split into a later turn,
    # and restates the text it belongs to.
    (steer,) = harness.steers
    assert "Canonical thread root: $text" in steer
    assert "Teil 1 · Text · $text · bereits zugestellt" in steer
    assert all(f"gallery-{index}.heic" in steer for index in range(7))
    assert_digests(steer)

    harness.finish_turn()
    (answer,) = harness.answers()
    assert answer["thread_root"] == "$text"
    assert answer["source_event_ids"] == ["$text", "$gallery"]
    assert len(harness.prompts) == 1


def test_text_after_the_gallery_turn_ended_is_a_continuation_with_every_item(tmp_path):
    harness = ContextHarness(tmp_path)
    harness.handle(harness.gallery_event("$gallery", 0))
    harness.finish_turn()
    assert len(harness.answers()) == 1

    harness.handle(harness.text_event("$text", "Bitte beschreiben", 29.5))

    assert len(harness.prompts) == 2
    continuation = harness.prompts[1]
    assert "Teil 1 · Galerie · $gallery · bereits zugestellt" in continuation
    assert "Teil 2 · Text · $text\nBitte beschreiben" in continuation
    assert len(local_files(continuation)) == 4
    assert_digests(continuation)
    harness.finish_turn()
    answer = harness.answers()[-1]
    assert answer["thread_root"] == "$gallery"
    assert answer["source_event_ids"] == ["$gallery", "$text"]


def test_text_beyond_the_boundary_is_an_unrelated_request(tmp_path):
    harness = ContextHarness(tmp_path)
    harness.handle(harness.gallery_event("$gallery", 0))
    harness.finish_turn()
    harness.handle(harness.text_event("$text", "Neues Thema", 46))

    assert harness.prompts[1] == "Neues Thema"
    harness.finish_turn()
    assert harness.answers()[-1]["thread_root"] == "$text"


def test_plain_texts_keep_per_message_delivery(tmp_path):
    harness = ContextHarness(tmp_path)
    harness.busy = True
    harness.handle(harness.text_event("$one", "erste Frage", 0))
    harness.handle(harness.text_event("$two", "zweite Frage", 2))

    assert [request["text"] for request in harness.pending] == [
        "erste Frage",
        "zweite Frage",
    ]
    assert [request["thread_root_event_id"] for request in harness.pending] == [
        "$one",
        "$two",
    ]


def test_text_waits_for_the_voice_note_sent_before_it(tmp_path):
    harness = ContextHarness(tmp_path)
    harness.transcribe_gate.clear()

    async def scenario():
        voice = asyncio.create_task(harness.dispatch(harness.voice_event("$voice", 0)))
        await asyncio.to_thread(harness.transcribing.wait, 5)
        await harness.dispatch(harness.text_event("$text", "und das hier", 1))
        # The text is ready, but it must not overtake the transcript.
        assert harness.prompts == []
        assert harness.pending == []
        harness.transcribe_gate.set()
        await voice

    asyncio.run(scenario())

    (prompt,) = harness.prompts
    assert prompt.index("Teil 1 · Sprachnachricht · $voice") < prompt.index(
        "Bitte diese Fotos einstellen"
    ) < prompt.index("Teil 2 · Text · $text\nund das hier")
    assert "Audio-SHA-256: " in prompt
    assert sorted(harness.qitems.values()) == ["$text", "$voice"]


def test_restart_during_an_image_download_resumes_it_exactly_once(tmp_path):
    harness = ContextHarness(tmp_path)
    image = harness.image_event("$image", 0)
    harness.crash_on = image.url
    try:
        harness.handle(image)
    except SystemExit:
        pass
    assert harness.prompts == []

    restarted = ContextHarness(tmp_path, data=copy.deepcopy(harness.data))
    restarted.blobs = harness.blobs
    asyncio.run(restarted.bridge._resume_galleries())
    asyncio.run(restarted.bridge._resume_bursts())

    (prompt,) = restarted.prompts
    (stored,) = local_files(prompt)
    assert stored.read_bytes() == b"\x89PNG\r\n\x1a\nphoto"
    assert_digests(prompt)
    assert list(stored.parent.glob("*.png")) == [stored]
    # A second restart neither downloads nor delivers again.
    again = ContextHarness(tmp_path, data=copy.deepcopy(restarted.data))
    asyncio.run(again.bridge._resume_bursts())
    assert again.prompts == [] and again.downloads == []


def test_restart_between_parts_still_links_the_later_part(tmp_path):
    harness = ContextHarness(tmp_path)
    harness.handle(harness.gallery_event("$gallery", 0))
    harness.finish_turn()

    restarted = ContextHarness(tmp_path, data=copy.deepcopy(harness.data))
    restarted.handle(restarted.text_event("$text", "Bitte beschreiben", 30))

    (continuation,) = restarted.prompts
    assert "Teil 1 · Galerie · $gallery · bereits zugestellt" in continuation
    assert "Teil 2 · Text · $text" in continuation
    restarted.finish_turn()
    assert restarted.answers()[-1]["thread_root"] == "$gallery"


def test_voice_interrupted_by_a_restart_is_named_next_to_the_held_text(tmp_path):
    harness = ContextHarness(tmp_path)
    harness.transcribe_gate.clear()
    snapshot: dict = {}

    async def scenario():
        voice = asyncio.create_task(harness.dispatch(harness.voice_event("$voice", 0)))
        await asyncio.to_thread(harness.transcribing.wait, 5)
        await harness.dispatch(harness.text_event("$text", "und das hier", 1))
        snapshot.update(copy.deepcopy(harness.data))  # the process dies here
        harness.transcribe_gate.set()
        await voice

    asyncio.run(scenario())
    restarted = ContextHarness(tmp_path, data=snapshot)
    asyncio.run(restarted.bridge._recover_voice_statuses())
    asyncio.run(restarted.bridge._resume_bursts())

    (prompt,) = restarted.prompts
    assert "Teil 1 · Sprachnachricht · $voice\nNicht übernommen: " in prompt
    assert "Bridge-Neustart unterbrochen" in prompt
    assert "Teil 2 · Text · $text\nund das hier" in prompt


def test_failed_attachment_is_named_inside_the_related_context(tmp_path):
    harness = ContextHarness(tmp_path)
    harness.busy = True
    harness.handle(harness.gallery_event("$gallery", 0))
    image = harness.image_event("$image", 2)
    harness.failing.add(image.url)
    harness.handle(image)
    harness.handle(harness.text_event("$text", "Bitte alles prüfen", 4))

    harness.busy = False
    harness.poll()
    (prompt,) = harness.prompts
    assert "Teil 2 · Bild · $image\nNicht übernommen: Bild konnte nicht übernommen" in prompt
    assert prompt.index("Teil 2 · Bild") < prompt.index("Teil 3 · Text · $text")
    assert sorted(harness.qitems.values()) == ["$gallery", "$image", "$text"]


def test_threaded_parts_join_by_relation_long_after_the_boundary(tmp_path):
    harness = ContextHarness(tmp_path)
    harness.handle(harness.gallery_event("$gallery", 0))
    harness.finish_turn()
    harness.handle(harness.text_event("$top", "anderes Thema", 100))
    harness.finish_turn()
    relation = {
        "rel_type": "m.thread",
        "event_id": "$gallery",
        "m.in_reply_to": {"event_id": "$gallery"},
    }
    harness.handle(harness.text_event("$reply", "zur Galerie", 200, relation))

    continuation = harness.prompts[-1]
    assert "Teil 1 · Galerie · $gallery · bereits zugestellt" in continuation
    assert "Teil 2 · Text · $reply\nzur Galerie" in continuation
    assert "anderes Thema" not in continuation
    harness.finish_turn()
    assert harness.answers()[-1]["thread_root"] == "$gallery"


def test_steered_part_that_falls_back_still_carries_the_whole_burst(tmp_path):
    harness = ContextHarness(tmp_path)
    harness.handle(harness.gallery_event("$gallery", 0))
    harness.handle(harness.text_event("$text", "Bitte beschreiben", 1))
    time.sleep(0.01)  # past the steering batch window
    harness.busy = False  # the gallery turn ends before the steer is injected
    harness.poll()
    harness.finish_turn()

    assert harness.steers == []
    fallback = harness.prompts[1]
    assert "Canonical thread root: $gallery" in fallback
    assert "Teil 1 · Galerie · $gallery · bereits zugestellt" in fallback
    assert "Teil 2 · Text · $text\nBitte beschreiben" in fallback
    assert_digests(fallback)
    harness.finish_turn()
    answer = harness.answers()[-1]
    assert answer["thread_root"] == "$gallery"
    assert set(answer["source_event_ids"]) == {"$gallery", "$text"}


def test_parts_steered_into_one_batch_share_one_updated_entry(tmp_path):
    harness = ContextHarness(tmp_path)
    harness.handle(harness.gallery_event("$gallery", 0))
    harness.handle(harness.text_event("$one", "erstens", 1))
    harness.handle(harness.image_event("$image", 2))
    group = harness.pending[1]

    (entry,) = group["steering_entries"]
    assert entry["new_event_ids"] == ["$one", "$image"]
    assert group["source_event_ids"] == ["$gallery", "$one", "$image"]
    assert sorted(group["qitem_ids"]) == ["q-2", "q-3"]
    assert group["text"].count("Teil 1 · Galerie · $gallery") == 1
    assert group["text"].index("Teil 2 · Text · $one") < group["text"].index(
        "Teil 3 · Bild · $image"
    )


def test_plain_text_that_failed_to_queue_is_not_sent_later(tmp_path):
    from openrig_matrix_bridge.openrig import GatewayError

    harness = ContextHarness(tmp_path)
    harness.busy = True
    create = harness.bridge._create_inbound_qitem

    refused: list[str] = []

    async def refuse_first(agent, *, event_id, text, source_label):
        if event_id == "$root" and not refused:
            refused.append(event_id)
            raise GatewayError("queue unavailable")
        return await create(agent, event_id=event_id, text=text, source_label=source_label)

    harness.bridge._create_inbound_qitem = refuse_first
    harness.handle(harness.text_event("$root", "erste Frage", 0))
    relation = {
        "rel_type": "m.thread",
        "event_id": "$root",
        "m.in_reply_to": {"event_id": "$root"},
    }
    harness.handle(harness.text_event("$reply", "Nachtrag", 2, relation))

    assert "nicht an den Agenten gesendet" in harness.sent[0]["text"]
    assert [request["text"] for request in harness.pending] == ["Nachtrag"]


def test_queue_card_summarizes_a_related_request_without_paths_or_event_ids(tmp_path):
    from openrig_matrix_bridge.bridge import queue_request_entry

    harness = ContextHarness(tmp_path)
    harness.busy = True
    harness.handle(harness.gallery_event("$gallery", 0, count=2))
    harness.handle(harness.text_event("$text", "Bitte beschreiben", 1))
    (request,) = harness.pending

    entry = queue_request_entry(request)

    assert entry.startswith("Zusammenhang · ")
    assert "2 Teile · Galerie, Text" in entry
    assert "/" not in entry and "$" not in entry
    assert "redacted" not in entry


def test_queue_card_summarizes_a_linked_steering_entry(tmp_path):
    from openrig_matrix_bridge.bridge import queue_request_entry

    harness = ContextHarness(tmp_path)
    harness.handle(harness.text_event("$text", "Ich schicke gleich Fotos", 0))
    harness.handle(harness.gallery_event("$gallery", 5, count=3))
    group = harness.pending[1]

    entry = queue_request_entry(group)

    assert "2 Teile · Text, Galerie" in entry
    assert "/" not in entry and "redacted" not in entry
