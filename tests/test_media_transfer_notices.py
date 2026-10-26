"""Issue #95: successful media transfer notices can be turned off."""

import asyncio
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from nio import RoomEncryptedFile, RoomEncryptedImage, RoomSendResponse
from nio.crypto import encrypt_attachment
from test_bridge import inbound_file_bridge
from test_config import write_config
from test_gallery import GalleryHarness, gallery_event, heic_item

from openrig_matrix_bridge.bridge import MatrixBridge
from openrig_matrix_bridge.config import AgentConfig, ConfigError, load_config

ROOM = SimpleNamespace(room_id="!advisor:matrix.example.org")
PNG = b"\x89PNG\r\n\x1a\n" + b"test image payload"
TRANSFER_WORDS = ("empfangen", "gespeichert", "lade und entschlüssele")


def set_notices(bridge, value):
    if value is not None:
        bridge.config.media.transfer_notices_enabled = value


def image_bridge(tmp_path: Path, notices, *, fail_download=False):
    encrypted, keys = encrypt_attachment(PNG)

    class ImageClient:
        async def download(self, mxc):
            if fail_download:
                raise OSError("download interrupted")
            return SimpleNamespace(body=encrypted)

    agent = AgentConfig(
        key="advisor", display_name="Advisor", session="advisor-lead@kernel"
    )
    bridge = object.__new__(MatrixBridge)
    bridge.config = SimpleNamespace(
        user_id="@openrig:matrix.example.org",
        allowed_users=("@owner:matrix.example.org",),
        max_message_age_minutes=-1,
        agents={"advisor": agent},
        media=SimpleNamespace(
            enabled=True, inbox_dir=tmp_path, max_image_bytes=1024 * 1024
        ),
    )
    set_notices(bridge, notices)
    bridge.room_agents = {ROOM.room_id: "advisor"}
    bridge.processed_events = set()
    bridge.state = SimpleNamespace(save=lambda: None, data={"processed_events": []})
    bridge.client = ImageClient()
    replies, forwarded = [], []

    async def send(room_id, text, reply_to=None, **kwargs):
        replies.append(text)
        return RoomSendResponse("$reply", room_id)

    async def enqueue_prompt(*args, **kwargs):
        forwarded.append(args)

    bridge._send = send
    bridge._enqueue_prompt = enqueue_prompt
    event = RoomEncryptedImage(
        {
            "event_id": "$image",
            "sender": "@owner:matrix.example.org",
            "origin_server_ts": 1_700_000_000_000,
            "type": "m.room.encrypted",
            "content": {},
        },
        "mxc://matrix.example.org/image",
        "Screenshot",
        keys["key"],
        keys["hashes"],
        keys["iv"],
        "image/png",
    )
    return bridge, agent, event, replies, forwarded


@pytest.mark.parametrize("notices", [None, True])
def test_image_success_notices_stay_by_default(tmp_path: Path, notices):
    bridge, _agent, event, replies, forwarded = image_bridge(tmp_path, notices)

    asyncio.run(bridge._handle_image(ROOM, event))

    assert replies[0] == "Bild empfangen, lade und entschlüssele …"
    assert replies[1].startswith("Bild gespeichert als `")
    assert len(forwarded) == 1


def test_image_opt_out_posts_nothing_but_delivers_the_same_prompt(tmp_path: Path):
    bridge, _agent, event, replies, forwarded = image_bridge(tmp_path, False)

    asyncio.run(bridge._handle_image(ROOM, event))

    assert replies == []
    stored = list((tmp_path / "advisor").glob("*.png"))
    assert len(stored) == 1 and stored[0].read_bytes() == PNG
    prompt = forwarded[0][4]
    assert f"Lokale Datei: {stored[0]}" in prompt
    assert f"SHA-256: {hashlib.sha256(PNG).hexdigest()}" in prompt
    assert forwarded[0][5] == "image"


def test_image_resume_path_stays_silent_with_the_opt_out(tmp_path: Path):
    # A restart resumes an interrupted image through _store_image.
    bridge, agent, event, replies, forwarded = image_bridge(tmp_path, False)

    asyncio.run(bridge._store_image(agent, ROOM.room_id, event, encrypted=True))

    assert replies == []
    assert len(forwarded) == 1


def test_image_failure_is_still_reported_with_the_opt_out(tmp_path: Path):
    bridge, _agent, event, replies, forwarded = image_bridge(
        tmp_path, False, fail_download=True
    )

    asyncio.run(bridge._handle_image(ROOM, event))

    assert forwarded == []
    assert len(replies) == 1
    assert "Bild konnte nicht" in replies[0]


def file_event(payload: bytes, keys: dict) -> RoomEncryptedFile:
    return RoomEncryptedFile(
        {
            "event_id": "$file",
            "sender": "@owner:matrix.example.org",
            "origin_server_ts": 1_700_000_000_000,
            "type": "m.room.encrypted",
            "content": {
                "filename": "notes.txt",
                "info": {"mimetype": "text/plain", "size": len(payload)},
            },
        },
        "mxc://matrix.example.org/file",
        "notes.txt",
        keys["key"],
        keys["hashes"],
        keys["iv"],
        "text/plain",
    )


@pytest.mark.parametrize(
    ("notices", "expected"),
    [
        (None, ["Datei empfangen, lade und entschlüssele …", "Datei gespeichert"]),
        (True, ["Datei empfangen, lade und entschlüssele …", "Datei gespeichert"]),
        (False, []),
    ],
)
def test_file_success_notices_follow_the_switch(tmp_path: Path, notices, expected):
    payload = b"short text\n"
    encrypted, keys = encrypt_attachment(payload)
    bridge, forwarded, replies = inbound_file_bridge(tmp_path, encrypted)
    set_notices(bridge, notices)

    asyncio.run(bridge._handle_file(ROOM, file_event(payload, keys)))

    assert [reply[: len(prefix)] for reply, prefix in zip(replies, expected)] == (
        expected
    )
    assert len(replies) == len(expected)
    stored = list((tmp_path / "advisor").glob("*.txt"))
    assert len(stored) == 1 and stored[0].read_bytes() == payload
    assert f"SHA-256: {hashlib.sha256(payload).hexdigest()}" in forwarded[0][4]


def test_file_failure_is_still_reported_with_the_opt_out(tmp_path: Path):
    payload = b"x" * 64
    encrypted, keys = encrypt_attachment(payload)
    bridge, forwarded, replies = inbound_file_bridge(
        tmp_path, encrypted, max_file_bytes=8
    )
    set_notices(bridge, False)

    asyncio.run(bridge._handle_file(ROOM, file_event(payload, keys)))

    assert forwarded == []
    assert len(replies) == 1
    assert not replies[0].startswith(("Datei empfangen", "Datei gespeichert"))


def test_gallery_opt_out_posts_nothing_and_forwards_the_prompt(tmp_path: Path):
    harness = GalleryHarness(tmp_path)
    harness.bridge.config.media.transfer_notices_enabled = False
    items = [heic_item(harness.client, index) for index in range(2)]

    harness.handle(gallery_event(items))

    assert harness.replies == []
    assert len(harness.forwarded) == 1
    assert len(list(harness.inbox().glob("*.heic"))) == 2


def test_gallery_with_failed_items_still_reports_them(tmp_path: Path):
    harness = GalleryHarness(tmp_path)
    harness.bridge.config.media.transfer_notices_enabled = False
    items = [heic_item(harness.client, index) for index in range(3)]
    harness.client.failing.add("mxc://matrix.example.org/item-1")

    harness.handle(gallery_event(items))

    assert len(harness.replies) == 1
    assert "• Element 2" in harness.replies[0][0]
    assert len(harness.forwarded) == 1


def test_empty_gallery_is_still_reported_with_the_opt_out(tmp_path: Path):
    harness = GalleryHarness(tmp_path)
    harness.bridge.config.media.transfer_notices_enabled = False

    harness.handle(gallery_event([]))

    assert [reply[0] for reply in harness.replies] == ["Galerie enthält keine Elemente."]


def test_gallery_resumed_after_a_restart_stays_silent(tmp_path: Path):
    from test_gallery import GalleryClient

    client = GalleryClient()
    first = GalleryHarness(tmp_path, client)
    first.bridge.config.media.transfer_notices_enabled = False
    items = [heic_item(client, index) for index in range(3)]
    client.crash_on = "mxc://matrix.example.org/item-2"
    with pytest.raises(SystemExit):
        first.handle(gallery_event(items))
    client.crash_on = None
    restarted = GalleryHarness(tmp_path, client, state=first.data)
    restarted.bridge.config.media.transfer_notices_enabled = False

    asyncio.run(restarted.bridge._resume_galleries())

    assert first.replies == [] and restarted.replies == []
    assert len(restarted.forwarded) == 1


def media_config(tmp_path: Path, value: str | None) -> Path:
    path = write_config(tmp_path / "config.yaml")
    if value is not None:
        path.write_text(
            path.read_text(encoding="utf-8")
            + f"\nmedia:\n  transfer_notices_enabled: {value}\n",
            encoding="utf-8",
        )
    return path


@pytest.mark.parametrize(
    ("value", "expected"), [(None, True), ("true", True), ("false", False)]
)
def test_config_reads_the_switch(tmp_path: Path, value, expected):
    config = load_config(media_config(tmp_path, value))

    assert config.media.transfer_notices_enabled is expected


@pytest.mark.parametrize("value", ['"false"', "maybe", "0", "null"])
def test_config_rejects_a_non_boolean_switch(tmp_path: Path, value):
    with pytest.raises(ConfigError, match="transfer_notices_enabled"):
        load_config(media_config(tmp_path, value))


def test_no_transfer_words_leak_from_the_opt_out_paths(tmp_path: Path):
    bridge, _agent, event, replies, _forwarded = image_bridge(tmp_path, False)

    asyncio.run(bridge._handle_image(ROOM, event))

    assert not any(word in reply for reply in replies for word in TRANSFER_WORDS)
