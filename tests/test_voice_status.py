from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from nio import (
    RoomEncryptedAudio,
    RoomMessageAudio,
    RoomSendError,
    RoomSendResponse,
)
from nio.crypto import encrypt_attachment

from openrig_matrix_bridge.bridge import MatrixBridge, voice_transaction_id
from openrig_matrix_bridge.config import AgentConfig


def _voice_bridge(transcript="Hallo Welt", transcription_error=None):
    agent = AgentConfig(
        key="advisor",
        display_name="Advisor",
        session="advisor-lead@kernel",
    )
    bridge = object.__new__(MatrixBridge)
    bridge.config = SimpleNamespace(
        user_id="@openrig:matrix.example.org",
        allowed_users=("@owner:matrix.example.org",),
        max_message_age_minutes=-1,
        agents={"advisor": agent},
        transcription=SimpleNamespace(
            enabled=True,
            max_audio_bytes=1024 * 1024,
        ),
    )
    bridge.room_agents = {"!advisor:matrix.example.org": "advisor"}
    bridge.processed_events = set()
    bridge.state = SimpleNamespace(
        save=lambda: None,
        data={"processed_events": []},
    )

    class VoiceClient:
        async def download(self, mxc):
            assert mxc == "mxc://matrix.example.org/voice"
            return SimpleNamespace(body=b"audio bytes")

    class VoiceTranscriber:
        def transcribe(self, audio, filename):
            assert audio == b"audio bytes"
            assert filename == "voice.ogg"
            if transcription_error is not None:
                raise transcription_error
            return transcript

    bridge.client = VoiceClient()
    bridge.transcriber = VoiceTranscriber()
    bridge.sent = []
    bridge.edited = []
    bridge.forwarded = []

    async def send(room_id, text, reply_to=None, **kwargs):
        bridge.sent.append((text, reply_to))
        return RoomSendResponse("$voice-status", room_id)

    async def edit(room_id, event_id, text, **kwargs):
        bridge.edited.append((event_id, text))
        return RoomSendResponse("$edit", room_id)

    async def enqueue_prompt(*args, **kwargs):
        bridge.forwarded.append(args)

    bridge._send = send
    bridge._edit_message = edit
    bridge._enqueue_prompt = enqueue_prompt
    room = SimpleNamespace(room_id="!advisor:matrix.example.org")
    event = RoomMessageAudio(
        {
            "event_id": "$voice",
            "sender": "@owner:matrix.example.org",
            "origin_server_ts": 1_700_000_000_000,
            "type": "m.room.message",
            "content": {
                "msgtype": "m.audio",
                "url": "mxc://matrix.example.org/voice",
                "body": "voice.ogg",
            },
        },
        "mxc://matrix.example.org/voice",
        "voice.ogg",
    )
    event.source["content"]["org.matrix.msc3245.voice"] = {}
    return bridge, room, event


def test_voice_transcript_edits_the_single_status_message():
    bridge, room, event = _voice_bridge()

    asyncio.run(bridge._handle_audio(room, event))

    assert bridge.sent == [
        ("Sprachnachricht empfangen, transkribiere …", "$voice")
    ]
    assert bridge.edited == [("$voice-status", "Transkript:\nHallo Welt")]
    status = bridge.state.data["voice_statuses"]["$voice"]
    assert status["status_event_id"] == "$voice-status"
    assert status["state"] == "done"
    assert bridge.forwarded[0][4].endswith("Hallo Welt")


@pytest.mark.parametrize(
    ("transcript", "error", "expected"),
    [
        ("", None, "kein Text erkannt"),
        ("", RuntimeError("decoder failed"), "konnte nicht transkribiert werden"),
    ],
)
def test_voice_terminal_results_edit_the_same_status_message(
    transcript,
    error,
    expected,
):
    bridge, room, event = _voice_bridge(transcript, error)

    asyncio.run(bridge._handle_audio(room, event))

    assert len(bridge.sent) == 1
    assert bridge.sent[0][1] == "$voice"
    assert len(bridge.edited) == 1
    assert bridge.edited[0][0] == "$voice-status"
    assert expected in bridge.edited[0][1]
    assert bridge.forwarded == []
    assert bridge.state.data["voice_statuses"]["$voice"]["state"] in {
        "no-speech",
        "failed",
    }


def test_rejected_voice_status_edit_falls_back_without_logging_transcript(caplog):
    bridge, room, event = _voice_bridge()

    async def reject_edit(room_id, event_id, text, **kwargs):
        bridge.edited.append((event_id, text))
        return RoomSendError("edit rejected")

    bridge._edit_message = reject_edit

    with caplog.at_level("WARNING", logger="openrig-matrix-bridge"):
        asyncio.run(bridge._handle_audio(room, event))

    assert len(bridge.sent) == 2
    assert bridge.sent[1][1] == "$voice"
    assert "Hallo Welt" in bridge.sent[1][0]
    assert "Statusmeldung" in bridge.sent[1][0]
    assert "Hallo Welt" not in caplog.text
    assert bridge.state.data["voice_statuses"]["$voice"]["state"] == "fallback"


@pytest.mark.parametrize("interrupted_state", ["processing", "recovering"])
def test_interrupted_voice_status_recovers_without_duplicate_transcript(
    interrupted_state,
):
    bridge, room, event = _voice_bridge()
    bridge.processed_events.add("$voice")
    bridge.state.data["processed_events"] = ["$voice"]
    bridge.state.data["voice_statuses"] = {
        "$voice": {
            "room_id": room.room_id,
            "status_event_id": "$voice-status",
            "state": interrupted_state,
        }
    }

    asyncio.run(bridge._recover_voice_statuses())

    assert bridge.sent == []
    assert len(bridge.edited) == 1
    assert bridge.edited[0][0] == "$voice-status"
    assert "Neustart" in bridge.edited[0][1]
    assert bridge.forwarded == []
    assert bridge.state.data["voice_statuses"]["$voice"]["state"] == "interrupted"

    bridge.edited.clear()
    asyncio.run(bridge._handle_audio(room, event))
    assert bridge.edited == []
    assert bridge.sent == []


def test_legacy_committing_status_does_not_overwrite_possible_transcript():
    bridge, room, _event = _voice_bridge()
    bridge.state.data["voice_statuses"] = {
        "$voice": {
            "room_id": room.room_id,
            "status_event_id": "$voice-status",
            "thread_root_event_id": "$root",
            "reply_to_event_id": "$root",
            "state": "committing",
        }
    }
    sent = []

    async def send(room_id, text, reply_to=None, **kwargs):
        sent.append((text, reply_to, kwargs))
        return RoomSendResponse("$uncertain", room_id)

    bridge._send = send

    asyncio.run(bridge._recover_voice_statuses())

    assert bridge.edited == []
    assert len(sent) == 1
    assert "nicht sicher bestimmt" in sent[0][0]
    assert sent[0][1:] == (
        "$root",
        {
            "thread_root": "$root",
            "tx_id": voice_transaction_id(
                "$voice",
                "fallback-interrupted",
            ),
        },
    )
    assert bridge.state.data["voice_statuses"]["$voice"]["state"] == "fallback"


def test_recover_committing_voice_status_preserves_terminal_transcript():
    bridge, room, _event = _voice_bridge()
    intent = {
        "text": "Transkript:\nHallo Welt",
        "terminal_state": "done",
        "edit_tx_id": "voice-edit-transaction",
        "fallback_text": "Transkript (Fallback):\nHallo Welt",
        "fallback_tx_id": "voice-fallback-transaction",
    }
    bridge.state.data["voice_statuses"] = {
        "$voice": {
            "room_id": room.room_id,
            "status_event_id": "$voice-status",
            "thread_root_event_id": "$root",
            "reply_to_event_id": "$root",
            "state": "committing",
            "terminal_intent": intent,
        }
    }
    edits = []

    async def edit(room_id, event_id, text, **kwargs):
        edits.append((event_id, text, kwargs))
        return RoomSendResponse("$edit", room_id)

    bridge._edit_message = edit

    asyncio.run(bridge._recover_voice_statuses())

    assert edits == [
        (
            "$voice-status",
            "Transkript:\nHallo Welt",
            {"tx_id": "voice-edit-transaction"},
        )
    ]
    status = bridge.state.data["voice_statuses"]["$voice"]
    assert status["state"] == "done"
    assert "terminal_intent" not in status


@pytest.mark.parametrize("interrupted_state", ["sending-status", "status-send-failed"])
def test_recover_unsaved_voice_status_retries_same_status_transaction(
    interrupted_state,
):
    bridge, room, _event = _voice_bridge()
    bridge.state.data["voice_statuses"] = {
        "$voice": {
            "room_id": room.room_id,
            "status_event_id": None,
            "status_tx_id": "voice-status-transaction",
            "thread_root_event_id": "$root",
            "reply_to_event_id": "$root",
            "state": interrupted_state,
        }
    }
    sent = []
    edited = []

    async def send(room_id, text, reply_to=None, **kwargs):
        sent.append((text, reply_to, kwargs))
        return RoomSendResponse("$voice-status", room_id)

    async def edit(room_id, event_id, text, **kwargs):
        edited.append((event_id, text, kwargs))
        return RoomSendResponse("$edit", room_id)

    bridge._send = send
    bridge._edit_message = edit

    asyncio.run(bridge._recover_voice_statuses())

    assert sent[0][0] == "Sprachnachricht empfangen, transkribiere …"
    assert sent[0][1:] == (
        "$root",
        {"thread_root": "$root", "tx_id": "voice-status-transaction"},
    )
    assert edited[0][0] == "$voice-status"
    assert "Neustart" in edited[0][1]
    assert bridge.state.data["voice_statuses"]["$voice"]["state"] == "interrupted"


def test_recover_failed_status_send_retries_then_posts_one_terminal_fallback():
    bridge, room, _event = _voice_bridge()
    bridge.state.data["voice_statuses"] = {
        "$voice": {
            "room_id": room.room_id,
            "status_event_id": None,
            "status_tx_id": "voice-status-transaction",
            "thread_root_event_id": "$root",
            "reply_to_event_id": "$root",
            "state": "status-send-failed",
        }
    }
    sent = []

    async def send(room_id, text, reply_to=None, **kwargs):
        sent.append((text, reply_to, kwargs))
        if len(sent) == 1:
            return SimpleNamespace(status_code="M_UNKNOWN")
        return RoomSendResponse("$fallback", room_id)

    bridge._send = send

    asyncio.run(bridge._recover_voice_statuses())

    assert [text for text, _reply_to, _kwargs in sent] == [
        "Sprachnachricht empfangen, transkribiere …",
        (
            "Die Sprachnachricht wurde durch einen Bridge-Neustart unterbrochen "
            "und konnte nicht abgeschlossen werden. Bitte erneut senden.\n\n"
            "(Statusmeldung konnte nicht bearbeitet werden.)"
        ),
    ]
    assert sent[0][2]["tx_id"] == "voice-status-transaction"
    assert "tx_id" in sent[1][2]
    assert bridge.state.data["voice_statuses"]["$voice"]["state"] == "fallback"


def test_recover_fallback_intent_does_not_retry_rejected_edit():
    bridge, room, _event = _voice_bridge()
    intent = {
        "text": "Transkript:\nHallo Welt",
        "terminal_state": "done",
        "edit_tx_id": "voice-edit-transaction",
        "fallback_text": "Transkript (Fallback):\nHallo Welt",
        "fallback_tx_id": "voice-fallback-transaction",
    }
    bridge.state.data["voice_statuses"] = {
        "$voice": {
            "room_id": room.room_id,
            "status_event_id": "$voice-status",
            "thread_root_event_id": "$root",
            "reply_to_event_id": "$root",
            "state": "fallbacking",
            "terminal_intent": intent,
        }
    }
    sent = []

    async def send(room_id, text, reply_to=None, **kwargs):
        sent.append((text, reply_to, kwargs))
        return RoomSendResponse("$fallback", room_id)

    bridge._send = send

    asyncio.run(bridge._recover_voice_statuses())

    assert bridge.edited == []
    assert sent == [
        (
            "Transkript (Fallback):\nHallo Welt",
            "$root",
            {
                "thread_root": "$root",
                "tx_id": "voice-fallback-transaction",
            },
        )
    ]
    status = bridge.state.data["voice_statuses"]["$voice"]
    assert status["state"] == "fallback"
    assert "terminal_intent" not in status


def test_initial_voice_status_failure_falls_back_to_visible_terminal_message():
    bridge, room, event = _voice_bridge()
    sent = []

    async def send(room_id, text, reply_to=None, **kwargs):
        sent.append((text, reply_to, kwargs))
        if len(sent) == 1:
            return SimpleNamespace(status_code="M_UNKNOWN")
        return RoomSendResponse("$fallback", room_id)

    bridge._send = send

    asyncio.run(bridge._handle_audio(room, event))

    assert len(sent) == 2
    assert "Status" in sent[1][0]
    assert sent[1][1] == "$voice"
    assert "tx_id" in sent[1][2]
    assert bridge.forwarded == []
    assert bridge.state.data["voice_statuses"]["$voice"]["state"] == "fallback"


def test_encrypted_voice_status_keeps_thread_and_edit_relations():
    encrypted, keys = encrypt_attachment(b"audio bytes")
    bridge, room, _event = _voice_bridge()

    class EncryptedVoiceClient:
        def __init__(self):
            self.calls = []

        async def download(self, mxc):
            assert mxc == "mxc://matrix.example.org/voice"
            return SimpleNamespace(body=encrypted)

        async def room_send(self, room_id, message_type, content, **kwargs):
            self.calls.append((room_id, message_type, content, kwargs))
            event_id = "$voice-status" if len(self.calls) == 1 else "$edit"
            return RoomSendResponse(event_id, room_id)

    bridge.client = EncryptedVoiceClient()
    del bridge._send
    del bridge._edit_message
    bridge._send = MatrixBridge._send.__get__(bridge, MatrixBridge)
    bridge._edit_message = MatrixBridge._edit_message.__get__(bridge, MatrixBridge)
    event = RoomEncryptedAudio(
        {
            "event_id": "$voice",
            "sender": "@owner:matrix.example.org",
            "origin_server_ts": 1_700_000_000_000,
            "type": "m.room.encrypted",
            "content": {},
        },
        "mxc://matrix.example.org/voice",
        "voice.ogg",
        keys["key"],
        keys["hashes"],
        keys["iv"],
        "audio/ogg",
    )
    event.source["content"]["org.matrix.msc3245.voice"] = {}

    asyncio.run(bridge._handle_audio(room, event))

    status_call, edit_call = bridge.client.calls
    assert status_call[2]["m.relates_to"]["event_id"] == "$voice"
    assert edit_call[2]["m.relates_to"] == {
        "rel_type": "m.replace",
        "event_id": "$voice-status",
    }
    assert edit_call[2]["m.new_content"]["body"] == "Transkript:\nHallo Welt"
    assert status_call[3]["ignore_unverified_devices"] is True
    assert edit_call[3]["ignore_unverified_devices"] is True
    assert status_call[3]["tx_id"] == voice_transaction_id("$voice", "status")
    assert edit_call[3]["tx_id"] == voice_transaction_id("$voice", "edit-done")


def test_file_handler_routes_marked_audio_to_voice_handler():
    bridge = object.__new__(MatrixBridge)
    bridge.config = SimpleNamespace(media=SimpleNamespace(enabled=True))
    routed = []

    async def handle_audio(room, event):
        routed.append((room, event))

    bridge._handle_audio = handle_audio
    room = SimpleNamespace(room_id="!advisor:matrix.example.org")
    event = SimpleNamespace(
        source={
            "content": {
                "msgtype": "m.audio",
                "org.matrix.msc3245.voice": {},
            }
        }
    )

    asyncio.run(bridge._handle_file(room, event))

    assert routed == [(room, event)]


