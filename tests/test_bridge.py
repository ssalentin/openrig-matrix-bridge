import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from nio import RoomEncryptedImage, RoomSendResponse
from nio.crypto import encrypt_attachment

from openrig_matrix_bridge.bridge import MatrixBridge, verification_decision
from openrig_matrix_bridge.config import AgentConfig
from openrig_matrix_bridge.decisions import POLL_RESPONSE_KEY


class RecordingClient:
    def __init__(self):
        self.calls = []

    async def room_put_state(self, room_id, event_type, content):
        self.calls.append((room_id, event_type, content))
        return SimpleNamespace()


def bridge_with_room():
    bridge = object.__new__(MatrixBridge)
    bridge.client = RecordingClient()
    bridge.agent_statuses = {}
    bridge.state = SimpleNamespace(
        save=lambda: None,
        data={
            "rooms": {
                "advisor": {
                    "room_id": "!advisor:matrix.example.org",
                    "name": "OpenRig · Advisor",
                }
            }
        }
    )
    return bridge


def test_status_uses_topic_and_structured_event_instead_of_room_name():
    bridge = bridge_with_room()
    agent = AgentConfig(
        key="advisor",
        display_name="Advisor",
        session="advisor-lead@kernel",
    )

    asyncio.run(bridge._update_agent_status(agent, "busy", "arbeitet"))

    assert [call[1] for call in bridge.client.calls] == [
        "m.room.topic",
        "dev.openrig.agent_status",
    ]
    assert bridge.client.calls[0][2] == {
        "topic": "🟡 arbeitet · advisor-lead@kernel"
    }
    assert "m.room.name" not in {call[1] for call in bridge.client.calls}


def test_restore_room_names_removes_status_emoji_and_product_prefix():
    bridge = bridge_with_room()
    bridge.config = SimpleNamespace(
        agents={
            "advisor": AgentConfig(
                key="advisor",
                display_name="Advisor",
                session="advisor-lead@kernel",
            )
        }
    )

    asyncio.run(bridge._restore_room_names())

    assert bridge.client.calls == [
        (
            "!advisor:matrix.example.org",
            "m.room.name",
            {"name": "Advisor"},
        )
    ]
    assert bridge.state.data["rooms"]["advisor"]["name"] == "Advisor"


def test_poll_answer_is_forwarded_and_pinned_decision_is_cleared():
    bridge = object.__new__(MatrixBridge)
    agent = AgentConfig(
        key="advisor",
        display_name="Advisor",
        session="advisor-lead@kernel",
    )
    bridge.config = SimpleNamespace(
        allowed_users=("@owner:matrix.example.org",),
        agents={"advisor": agent},
    )
    bridge.room_agents = {"!advisor:matrix.example.org": "advisor"}
    bridge.state = SimpleNamespace(
        save=lambda: None,
        data={
            "decisions": {
                "decision-1": {
                    "agent": "advisor",
                    "qitem_id": "qitem-1",
                    "title": "Welche Variante?",
                    "room_id": "!advisor:matrix.example.org",
                    "event_ids": ["$brief", "$poll"],
                    "polls": {
                        "$poll": {
                            "id": "choice",
                            "question": "Welche Variante?",
                            "options": [
                                {"id": "small", "label": "Klein"},
                                {"id": "full", "label": "Vollständig"},
                            ],
                        }
                    },
                    "answers": {},
                    "closed_at": None,
                }
            }
        },
    )
    forwarded = []
    acknowledgements = []
    unpinned = []

    async def remove_pinned(room_id, event_ids):
        unpinned.append((room_id, event_ids))

    async def enqueue_prompt(*args):
        forwarded.append(args)

    async def send(*args):
        acknowledgements.append(args)

    bridge._remove_pinned = remove_pinned
    bridge._enqueue_prompt = enqueue_prompt
    bridge._send = send
    room = SimpleNamespace(room_id="!advisor:matrix.example.org")
    event = SimpleNamespace(
        sender="@owner:matrix.example.org",
        event_id="$vote",
        server_timestamp=1_700_000_000_000,
        source={
            "content": {
                "m.relates_to": {"rel_type": "m.reference", "event_id": "$poll"},
                POLL_RESPONSE_KEY: {"answers": ["small"]},
            }
        },
    )

    asyncio.run(bridge._handle_poll_response(room, event))

    decision = bridge.state.data["decisions"]["decision-1"]
    assert decision["answers"]["choice"]["option_ids"] == ["small"]
    assert decision["closed_at"] is not None
    assert unpinned == [
        ("!advisor:matrix.example.org", ["$brief", "$poll"])
    ]
    assert "Auswahl: Klein (small)" in forwarded[0][4]
    assert forwarded[0][6] == "$brief"
    assert acknowledgements[0][1] == "Antwort an **Advisor** übergeben: Klein"
    assert acknowledgements[0][2] == "$brief"


def test_poll_answer_delivery_uses_decision_brief_as_thread_root():
    bridge = object.__new__(MatrixBridge)
    agent = AgentConfig(
        key="advisor",
        display_name="Advisor",
        session="advisor-lead@kernel",
    )
    bridge.locks = {"advisor": asyncio.Lock()}
    bridge.state = SimpleNamespace(
        save=lambda: None,
        data={
            "pending": {
                "advisor": [
                    {
                        "event_id": "$vote",
                        "reply_to_event_id": "$brief",
                        "answer_text": "Fertig.",
                        "started_at": time.time(),
                        "session_id": "session",
                        "transcript_path": "/tmp/transcript.jsonl",
                        "offset": 0,
                    }
                ]
            },
            "rooms": {
                "advisor": {
                    "room_id": "!advisor:matrix.example.org",
                    "name": "Advisor",
                }
            },
        },
    )
    sent = []

    async def send(room_id, text, reply_to=None):
        sent.append((room_id, text, reply_to))
        return RoomSendResponse("$answer", room_id)

    async def set_typing(room_id, typing):
        return None

    async def update_status(agent, status, detail):
        return None

    bridge._send = send
    bridge._set_typing = set_typing
    bridge._update_agent_status = update_status

    asyncio.run(bridge._poll_agent(agent))

    assert sent == [
        (
            "!advisor:matrix.example.org",
            "Fertig.",
            "$brief",
        )
    ]
    assert bridge.state.data["pending"]["advisor"] == []


def test_poll_delivers_commentary_then_final_answer(tmp_path):
    transcript = tmp_path / "rollout.jsonl"
    records = [
        {
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "item": {
                    "type": "AgentMessage",
                    "id": "comment-1",
                    "phase": "commentary",
                    "content": [{"type": "Text", "text": "Ich prüfe das."}],
                },
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "item": {
                    "type": "AgentMessage",
                    "id": "final-1",
                    "phase": "final_answer",
                    "content": [{"type": "Text", "text": "Fertig."}],
                },
            },
        },
    ]
    transcript.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )
    bridge = object.__new__(MatrixBridge)
    agent = AgentConfig(
        key="advisor",
        display_name="Advisor",
        session="advisor-lead@kernel",
    )
    bridge.config = SimpleNamespace(
        mirror_commentary=True,
        response_timeout_minutes=30,
    )
    bridge.locks = {"advisor": asyncio.Lock()}
    bridge.state = SimpleNamespace(
        save=lambda: None,
        data={
            "pending": {
                "advisor": [
                    {
                        "event_id": "$prompt",
                        "reply_to_event_id": "$prompt",
                        "answer_text": None,
                        "started_at": time.time(),
                        "session_id": "session",
                        "transcript_path": str(transcript),
                        "offset": 0,
                    }
                ]
            },
            "rooms": {
                "advisor": {
                    "room_id": "!advisor:matrix.example.org",
                    "name": "Advisor",
                }
            },
        },
    )
    sent = []

    async def send(room_id, text, reply_to=None):
        sent.append((text, reply_to))
        return RoomSendResponse(f"${len(sent)}", room_id)

    async def set_typing(room_id, typing):
        return None

    async def update_status(agent, status, detail):
        return None

    bridge._send = send
    bridge._set_typing = set_typing
    bridge._update_agent_status = update_status

    asyncio.run(bridge._poll_agent(agent))

    assert sent == [
        ("Ich prüfe das.", "$prompt"),
        ("Fertig.", "$prompt"),
    ]
    assert bridge.state.data["pending"]["advisor"] == []


def test_failed_commentary_delivery_does_not_skip_transcript(tmp_path):
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "item": {
                        "type": "AgentMessage",
                        "id": "comment-1",
                        "phase": "commentary",
                        "content": [{"type": "Text", "text": "Ich prüfe das."}],
                    },
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    bridge = object.__new__(MatrixBridge)
    agent = AgentConfig(
        key="advisor",
        display_name="Advisor",
        session="advisor-lead@kernel",
    )
    request = {
        "event_id": "$prompt",
        "reply_to_event_id": "$prompt",
        "answer_text": None,
        "started_at": time.time(),
        "session_id": "session",
        "transcript_path": str(transcript),
        "offset": 0,
    }
    bridge.config = SimpleNamespace(
        mirror_commentary=True,
        response_timeout_minutes=30,
    )
    bridge.locks = {"advisor": asyncio.Lock()}
    bridge.state = SimpleNamespace(
        save=lambda: None,
        data={
            "pending": {"advisor": [request]},
            "rooms": {"advisor": {"room_id": "!advisor:matrix.example.org"}},
        },
    )

    async def failed_send(*args, **kwargs):
        return SimpleNamespace()

    bridge._send = failed_send

    asyncio.run(bridge._poll_agent(agent))

    assert request["offset"] == 0
    assert request.get("sent_commentary") == []


def test_commentary_updates_one_status_message_instead_of_reply_spam(tmp_path):
    transcript = tmp_path / "rollout.jsonl"
    bridge = object.__new__(MatrixBridge)
    agent = AgentConfig(
        key="advisor",
        display_name="Advisor",
        session="advisor-lead@kernel",
    )
    request = {
        "event_id": "$prompt",
        "reply_to_event_id": "$prompt",
        "answer_text": None,
        "started_at": time.time(),
        "session_id": "session",
        "transcript_path": str(transcript),
        "offset": 0,
    }
    bridge.config = SimpleNamespace(
        mirror_commentary=True,
        response_timeout_minutes=30,
    )
    bridge.locks = {"advisor": asyncio.Lock()}
    bridge.state = SimpleNamespace(
        save=lambda: None,
        data={
            "pending": {"advisor": [request]},
            "rooms": {"advisor": {"room_id": "!advisor:matrix.example.org"}},
        },
    )
    sent = []
    edited = []

    def message_record(item_id, phase, text):
        return {
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "item": {
                    "type": "AgentMessage",
                    "id": item_id,
                    "phase": phase,
                    "content": [{"type": "Text", "text": text}],
                },
            },
        }

    async def send(room_id, text, reply_to=None):
        sent.append((text, reply_to))
        return RoomSendResponse("$status", room_id)

    async def edit(room_id, event_id, text):
        edited.append((event_id, text))
        return RoomSendResponse("$edit", room_id)

    async def noop(*args, **kwargs):
        return None

    bridge._send = send
    bridge._edit_message = edit
    bridge._set_typing = noop
    bridge._update_agent_status = noop

    transcript.write_text(
        json.dumps(message_record("comment-1", "commentary", "Schritt eins")) + "\n",
        encoding="utf-8",
    )
    asyncio.run(bridge._poll_agent(agent))

    with transcript.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(message_record("comment-2", "commentary", "Schritt zwei"))
            + "\n"
        )
    asyncio.run(bridge._poll_agent(agent))

    assert sent == [("Schritt eins", "$prompt")]
    assert edited == [("$status", "Schritt zwei")]
    assert request["sent_commentary"] == ["comment-1", "comment-2"]


def test_send_adds_matrix_formatted_body_for_markdown_table():
    class RichTextClient:
        async def room_send(self, room_id, message_type, content, **kwargs):
            self.call = (room_id, message_type, content, kwargs)
            return RoomSendResponse("$message", room_id)

    bridge = object.__new__(MatrixBridge)
    bridge.client = RichTextClient()

    response = asyncio.run(
        bridge._send(
            "!advisor:matrix.example.org",
            "| Agent | Status |\n|---|---|\n| Advisor | bereit |",
            "$prompt",
        )
    )

    assert isinstance(response, RoomSendResponse)
    content = bridge.client.call[2]
    assert content["format"] == "org.matrix.custom.html"
    assert "<table>" in content["formatted_body"]
    assert content["m.relates_to"]["event_id"] == "$prompt"


def test_message_edit_uses_matrix_replace_relation():
    class ReplaceClient:
        async def room_send(self, room_id, message_type, content, **kwargs):
            self.call = (room_id, message_type, content, kwargs)
            return RoomSendResponse("$edit", room_id)

    bridge = object.__new__(MatrixBridge)
    bridge.client = ReplaceClient()

    response = asyncio.run(
        bridge._edit_message(
            "!advisor:matrix.example.org",
            "$status",
            "**Schritt zwei**",
        )
    )

    assert isinstance(response, RoomSendResponse)
    content = bridge.client.call[2]
    assert content["m.relates_to"] == {
        "rel_type": "m.replace",
        "event_id": "$status",
    }
    assert content["m.new_content"]["formatted_body"] == (
        "<p><strong>Schritt zwei</strong></p>"
    )
    assert content["body"].startswith("* ")


def test_explicit_message_is_published_and_recorded():
    bridge = object.__new__(MatrixBridge)
    agent = AgentConfig(
        key="advisor",
        display_name="Advisor",
        session="advisor-lead@kernel",
    )
    bridge.config = SimpleNamespace(agents={"advisor": agent})
    bridge.state = SimpleNamespace(
        save=lambda: None,
        data={
            "rooms": {
                "advisor": {
                    "room_id": "!advisor:matrix.example.org",
                    "name": "Advisor",
                }
            },
            "messages": {},
        },
    )

    async def send(room_id, text):
        assert room_id == "!advisor:matrix.example.org"
        assert text == "**Hallo** aus Matrix"
        return RoomSendResponse("$message", room_id)

    bridge._send = send

    asyncio.run(
        bridge._publish_message(
            {
                "id": "message-1",
                "agent": "advisor",
                "text": "**Hallo** aus Matrix",
            }
        )
    )

    assert bridge.state.data["messages"]["message-1"]["event_id"] == "$message"


def test_queue_notice_is_one_updated_and_pinned_message():
    bridge = object.__new__(MatrixBridge)
    agent = AgentConfig(
        key="advisor",
        display_name="Advisor",
        session="advisor-lead@kernel",
    )
    bridge.state = SimpleNamespace(
        save=lambda: None,
        data={"queue_notices": {}},
    )
    sent = []
    edited = []
    pinned = []
    unpinned = []

    async def send(room_id, text):
        sent.append(text)
        return RoomSendResponse("$queue", room_id)

    async def edit(room_id, event_id, text):
        edited.append((event_id, text))
        return RoomSendResponse("$edit", room_id)

    async def add_pinned(room_id, event_ids):
        pinned.append(event_ids)

    async def remove_pinned(room_id, event_ids):
        unpinned.append(event_ids)

    bridge._send = send
    bridge._edit_message = edit
    bridge._add_pinned = add_pinned
    bridge._remove_pinned = remove_pinned

    asyncio.run(bridge._update_queue_notice(agent, "!advisor", 2))
    asyncio.run(bridge._update_queue_notice(agent, "!advisor", 3))
    asyncio.run(bridge._update_queue_notice(agent, "!advisor", 0))

    assert len(sent) == 1
    assert "2 Nachrichten warten" in sent[0]
    assert edited[0][0] == "$queue"
    assert "3 Nachrichten warten" in edited[0][1]
    assert pinned == [["$queue"], ["$queue"]]
    assert unpinned == [["$queue"]]
    assert bridge.state.data["queue_notices"]["advisor"]["pinned"] is False


def test_human_queue_items_are_mirrored_to_the_source_agent_room():
    bridge = object.__new__(MatrixBridge)
    agent = AgentConfig(
        key="advisor",
        display_name="Advisor",
        session="advisor-lead@kernel",
    )
    bridge.config = SimpleNamespace(
        agents={"advisor": agent},
        human_destinations=("owner@external",),
    )
    bridge.state = SimpleNamespace(
        save=lambda: None,
        data={
            "decisions": {},
            "queue_mirror": {"initialized": True, "seen": {}},
        },
    )

    class QueueGateway:
        async def queue_list(self):
            return [
                {
                    "qitemId": "qitem-decision",
                    "destinationSession": "owner@external",
                    "sourceSession": "advisor-lead@kernel",
                },
                {
                    "qitemId": "qitem-update",
                    "destinationSession": "owner@external",
                    "sourceSession": "advisor-lead@kernel",
                },
            ]

        async def queue_show(self, qitem_id):
            if qitem_id == "qitem-decision":
                return {
                    "summary": "Welche Variante?",
                    "body": "Bitte entscheiden.",
                    "humanIntent": "decision",
                    "humanQuestions": [
                        {
                            "id": "choice",
                            "question": "Welche Variante?",
                            "options": [
                                {"id": "a", "label": "A"},
                                {"id": "b", "label": "B"},
                            ],
                        }
                    ],
                }
            return {
                "summary": "Arbeit abgeschlossen",
                "body": "Alles erledigt.",
                "humanIntent": "update",
                "humanQuestions": None,
            }

    bridge.gateway = QueueGateway()
    decisions = []
    messages = []

    async def publish_decision(request):
        decisions.append(request)

    async def publish_message(request):
        messages.append(request)

    bridge._publish_decision = publish_decision
    bridge._publish_message = publish_message

    asyncio.run(bridge._mirror_human_queue())

    assert decisions[0]["qitem_id"] == "qitem-decision"
    assert decisions[0]["agent"] == "advisor"
    assert messages[0]["text"] == "**Arbeit abgeschlossen**\n\nAlles erledigt."
    assert set(bridge.state.data["queue_mirror"]["seen"]) == {
        "qitem-decision",
        "qitem-update",
    }


def test_first_human_queue_scan_only_sets_the_watermark():
    bridge = object.__new__(MatrixBridge)
    bridge.config = SimpleNamespace(
        agents={},
        human_destinations=("owner@external",),
    )
    bridge.state = SimpleNamespace(
        save=lambda: None,
        data={
            "decisions": {},
            "queue_mirror": {"initialized": False, "seen": {}},
        },
    )

    class QueueGateway:
        async def queue_list(self):
            return [{"qitemId": "qitem-old"}]

    bridge.gateway = QueueGateway()

    asyncio.run(bridge._mirror_human_queue())

    assert bridge.state.data["queue_mirror"]["initialized"] is True
    assert set(bridge.state.data["queue_mirror"]["seen"]) == {"qitem-old"}


def test_encrypted_image_is_saved_and_forwarded_to_agent(tmp_path):
    png = b"\x89PNG\r\n\x1a\n" + b"test image payload"
    encrypted, keys = encrypt_attachment(png)

    class ImageClient:
        async def download(self, mxc):
            assert mxc == "mxc://matrix.example.org/image"
            return SimpleNamespace(body=encrypted)

    bridge = object.__new__(MatrixBridge)
    agent = AgentConfig(
        key="advisor",
        display_name="Advisor",
        session="advisor-lead@kernel",
    )
    bridge.config = SimpleNamespace(
        user_id="@openrig:matrix.example.org",
        allowed_users=("@owner:matrix.example.org",),
        max_message_age_minutes=-1,
        agents={"advisor": agent},
        media=SimpleNamespace(
            enabled=True,
            inbox_dir=tmp_path,
            max_image_bytes=1024 * 1024,
        ),
    )
    bridge.room_agents = {"!advisor:matrix.example.org": "advisor"}
    bridge.processed_events = set()
    bridge.state = SimpleNamespace(
        save=lambda: None,
        data={"processed_events": []},
    )
    bridge.client = ImageClient()
    bridge._send = lambda *args: _completed(None)
    forwarded = []

    async def enqueue_prompt(*args):
        forwarded.append(args)

    bridge._enqueue_prompt = enqueue_prompt
    room = SimpleNamespace(room_id="!advisor:matrix.example.org")
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

    asyncio.run(bridge._handle_image(room, event))

    stored = list((tmp_path / "advisor").glob("*.png"))
    assert len(stored) == 1
    assert stored[0].read_bytes() == png
    assert forwarded[0][5] == "image"
    assert str(stored[0]) in forwarded[0][4]


async def _completed(value):
    return value


@pytest.mark.parametrize("text", ["ja", "Ja", "JA!", "bestätigen", "stimmt"])
def test_accepts_verification(text):
    assert verification_decision(text) is True


@pytest.mark.parametrize("text", ["nein", "Nein.", "ablehnen", "stimmt nicht"])
def test_rejects_verification(text):
    assert verification_decision(text) is False


def test_ignores_other_messages():
    assert verification_decision("Was ist der Status?") is None
