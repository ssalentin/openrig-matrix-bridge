import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from nio import (
    ProfileSetAvatarResponse,
    RoomEncryptedImage,
    RoomGetEventResponse,
    RoomGetStateEventResponse,
    RoomSendResponse,
    UploadResponse,
)
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


def test_assigns_unique_repo_avatars_and_profile():
    class AvatarClient:
        def __init__(self):
            self.rooms = {
                "!advisor:matrix.example.org": SimpleNamespace(avatar_url=None),
                "!operator:matrix.example.org": SimpleNamespace(avatar_url=None),
            }
            self.room_avatars = []
            self.profile_avatar = None

        async def upload(self, data, content_type, filename, filesize):
            assert data.read(8) == b"\x89PNG\r\n\x1a\n"
            assert filesize > 0
            return UploadResponse(f"mxc://example.org/{filename}"), None

        async def room_put_state(self, room_id, event_type, content):
            self.room_avatars.append((room_id, event_type, content))
            return SimpleNamespace()

        async def set_avatar(self, avatar_url):
            self.profile_avatar = avatar_url
            return ProfileSetAvatarResponse()

    bridge = object.__new__(MatrixBridge)
    bridge.config = SimpleNamespace(
        avatars=SimpleNamespace(
            enabled=True,
            replace_existing=True,
            set_profile=True,
        ),
        agents={
            "advisor": AgentConfig(
                key="advisor",
                display_name="Advisor",
                session="advisor-lead@kernel",
            ),
            "operator": AgentConfig(
                key="operator",
                display_name="Operator",
                session="operator-agent@kernel",
            ),
        },
    )
    bridge.client = AvatarClient()
    bridge.state = SimpleNamespace(
        save=lambda: None,
        data={
            "rooms": {
                "advisor": {
                    "room_id": "!advisor:matrix.example.org",
                    "name": "Advisor",
                },
                "operator": {
                    "room_id": "!operator:matrix.example.org",
                    "name": "Operator",
                },
            },
            "avatar_pool": {
                "assignments": {},
                "uploads": {},
                "profile_mxc": None,
            },
        },
    )

    asyncio.run(bridge._ensure_room_avatars())

    assignments = bridge.state.data["avatar_pool"]["assignments"]
    assert len({item["id"] for item in assignments.values()}) == 2
    assert len(bridge.client.room_avatars) == 2
    assert bridge.client.profile_avatar == "mxc://example.org/openrig-profile.png"


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


def test_owner_leaving_room_rejects_agent_and_removes_mapping():
    bridge = object.__new__(MatrixBridge)
    agent = AgentConfig(
        key="operator",
        display_name="Operator",
        session="operator-agent@kernel",
    )
    bridge.config = SimpleNamespace(
        allowed_users=("@owner:matrix.example.org",),
        agents={"operator": agent},
    )
    bridge.room_agents = {"!operator:matrix.example.org": "operator"}
    bridge.agent_statuses = {"operator": "ready"}
    bridge.state = SimpleNamespace(
        save=lambda: None,
        data={
            "rooms": {
                "operator": {
                    "room_id": "!operator:matrix.example.org",
                    "name": "Operator",
                }
            },
            "pending": {"operator": []},
            "welcomed": {"operator": True},
            "queue_notices": {"operator": {"event_id": "$notice"}},
            "rejected_rooms": {},
            "avatar_pool": {
                "assignments": {
                    "operator": {
                        "id": "openrig-02",
                        "mxc": "mxc://example.org/operator",
                    }
                }
            },
        },
    )
    left = []

    class Client:
        async def room_leave(self, room_id):
            left.append(room_id)
            return SimpleNamespace(status_code=200)

    bridge.client = Client()
    room = SimpleNamespace(room_id="!operator:matrix.example.org")
    event = SimpleNamespace(
        state_key="@owner:matrix.example.org",
        membership="leave",
    )

    asyncio.run(bridge._handle_member(room, event))

    assert left == ["!operator:matrix.example.org"]
    assert "operator" not in bridge.state.data["rooms"]
    assert bridge.state.data["rejected_rooms"]["operator"]["reason"] == "owner_leave"
    assert "operator" not in bridge.room_agents
    assert "operator" not in bridge.state.data["avatar_pool"]["assignments"]


def test_reconcile_rejects_room_when_owner_has_left():
    bridge = object.__new__(MatrixBridge)
    agent = AgentConfig(
        key="queue",
        display_name="Queue",
        session="queue-worker@kernel",
    )
    bridge.config = SimpleNamespace(
        allowed_users=("@owner:matrix.example.org",),
        agents={"queue": agent},
    )
    bridge.room_agents = {"!queue:matrix.example.org": "queue"}
    bridge.agent_statuses = {}
    bridge.state = SimpleNamespace(
        save=lambda: None,
        data={
            "rooms": {
                "queue": {
                    "room_id": "!queue:matrix.example.org",
                    "name": "Queue",
                }
            },
            "pending": {},
            "welcomed": {},
            "queue_notices": {},
            "rejected_rooms": {},
            "avatar_pool": {"assignments": {}},
        },
    )
    left = []

    class Client:
        async def room_get_state_event(self, room_id, event_type, state_key):
            assert event_type == "m.room.member"
            assert state_key == "@owner:matrix.example.org"
            return RoomGetStateEventResponse(
                content={"membership": "leave"},
                event_type=event_type,
                state_key=state_key,
                room_id=room_id,
            )

        async def room_leave(self, room_id):
            left.append(room_id)
            return SimpleNamespace(status_code=200)

    bridge.client = Client()

    asyncio.run(bridge._reconcile_room_memberships())

    assert left == ["!queue:matrix.example.org"]
    assert "queue" not in bridge.state.data["rooms"]
    assert bridge.state.data["rejected_rooms"]["queue"]["reason"] == "owner_leave"


def test_rejected_agent_room_is_not_recreated():
    bridge = object.__new__(MatrixBridge)
    advisor = AgentConfig(
        key="advisor",
        display_name="Advisor",
        session="advisor-lead@kernel",
    )
    operator = AgentConfig(
        key="operator",
        display_name="Operator",
        session="operator-agent@kernel",
    )
    bridge.config = SimpleNamespace(
        allowed_users=("@owner:matrix.example.org",),
        agents={"advisor": advisor, "operator": operator},
    )
    bridge.state = SimpleNamespace(
        save=lambda: None,
        data={
            "rooms": {
                "advisor": {
                    "room_id": "!advisor:matrix.example.org",
                    "name": "Advisor",
                }
            },
            "rejected_rooms": {
                "operator": {
                    "room_id": "!old:matrix.example.org",
                    "reason": "owner_leave",
                }
            },
        },
    )
    created = []

    class Client:
        async def room_create(self, **kwargs):
            created.append(kwargs)
            raise AssertionError("rejected room must not be recreated")

    bridge.client = Client()

    asyncio.run(bridge._create_rooms())

    assert created == []


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


def test_reaction_to_bot_post_becomes_silent_feedback():
    bridge = object.__new__(MatrixBridge)
    agent = AgentConfig(
        key="advisor",
        display_name="Advisor",
        session="advisor-lead@kernel",
    )
    bridge.config = SimpleNamespace(
        reaction_feedback_enabled=True,
        user_id="@bot:matrix.example.org",
        allowed_users=("@owner:matrix.example.org",),
        max_message_age_minutes=60,
        agents={"advisor": agent},
    )
    bridge.room_agents = {"!advisor:matrix.example.org": "advisor"}
    bridge.processed_events = set()
    bridge.state = SimpleNamespace(
        save=lambda: None,
        data={"processed_events": []},
    )

    class Client:
        async def room_get_event(self, room_id, event_id):
            response = RoomGetEventResponse()
            response.event = SimpleNamespace(
                sender="@bot:matrix.example.org",
                body="Die 32 Geister sind wieder da.",
            )
            return response

    forwarded = []

    async def enqueue_prompt(*args, **kwargs):
        forwarded.append((args, kwargs))

    bridge.client = Client()
    bridge._enqueue_prompt = enqueue_prompt
    room = SimpleNamespace(room_id="!advisor:matrix.example.org")
    event = SimpleNamespace(
        sender="@owner:matrix.example.org",
        event_id="$reaction",
        reacts_to="$bot-post",
        key="❤️",
        server_timestamp=int(time.time() * 1000),
    )

    asyncio.run(bridge._handle_reaction(room, event))

    args, kwargs = forwarded[0]
    assert args[5] == "reaction"
    assert kwargs == {"suppress_reply": True, "queue_tracked": False}
    assert "❤️" in args[4]
    assert "Die 32 Geister sind wieder da." in args[4]


def test_silent_feedback_does_not_post_agent_answer():
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
                        "event_id": "$reaction",
                        "reply_to_event_id": "$bot-post",
                        "answer_text": "Verstanden.",
                        "started_at": time.time(),
                        "session_id": "session",
                        "transcript_path": "/tmp/transcript.jsonl",
                        "offset": 0,
                        "suppress_reply": True,
                    }
                ]
            },
            "rooms": {
                "advisor": {
                    "room_id": "!advisor:matrix.example.org",
                }
            },
        },
    )
    sent = []
    notices = []

    async def send(room_id, text, reply_to=None):
        sent.append((room_id, text, reply_to))
        return RoomSendResponse("$answer", room_id)

    async def finish_qitem(agent_arg, request):
        return None

    async def update_notice(agent_arg, room_id, waiting):
        notices.append(waiting)

    bridge._send = send
    bridge._finish_inbound_qitem = finish_qitem
    bridge._update_queue_notice = update_notice

    asyncio.run(bridge._poll_agent(agent))

    assert sent == []
    assert notices == [0]
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


@pytest.mark.parametrize(
    ("content", "expected_relation"),
    [
        (
            {"msgtype": "m.text", "body": "top level"},
            None,
        ),
        (
            {
                "msgtype": "m.text",
                "body": "thread reply",
                "m.relates_to": {
                    "rel_type": "m.thread",
                    "event_id": "$root",
                    "m.in_reply_to": {"event_id": "$parent"},
                },
            },
            {
                "rel_type": "m.thread",
                "event_id": "$root",
                "m.in_reply_to": {"event_id": "$parent"},
            },
        ),
        (
            {
                "msgtype": "m.text",
                "body": "rich reply",
                "m.relates_to": {
                    "m.in_reply_to": {"event_id": "$parent"},
                },
            },
            {
                "m.in_reply_to": {"event_id": "$parent"},
            },
        ),
    ],
)
def test_room_message_preserves_source_relation_for_reply_resolution(
    content,
    expected_relation,
):
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
    )
    bridge.room_agents = {"!advisor:matrix.example.org": "advisor"}
    bridge.processed_events = set()
    captured = {}

    async def enqueue(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs

    async def verification(*args):
        return False

    bridge._enqueue_prompt = enqueue
    bridge._handle_verification_reply = verification
    event = SimpleNamespace(
        event_id="$prompt",
        sender="@owner:matrix.example.org",
        server_timestamp=1_700_000_000_000,
        body=content["body"],
        source={"content": content},
    )
    room = SimpleNamespace(room_id="!advisor:matrix.example.org")

    asyncio.run(bridge._handle_room_message(room, event))

    assert captured["kwargs"]["relates_to"] == expected_relation


@pytest.mark.parametrize(
    ("relates_to", "thread_root", "reply_to"),
    [
        (None, "$prompt", "$prompt"),
        (
            {
                "rel_type": "m.thread",
                "event_id": "$root",
                "m.in_reply_to": {"event_id": "$parent"},
            },
            "$root",
            "$root",
        ),
        (
            {"m.in_reply_to": {"event_id": "$parent"}},
            None,
            "$prompt",
        ),
    ],
)
def test_enqueue_prompt_stores_canonical_delivery_target(
    relates_to,
    thread_root,
    reply_to,
):
    agent = AgentConfig(
        key="advisor",
        display_name="Advisor",
        session="advisor-lead@kernel",
    )
    bridge = object.__new__(MatrixBridge)
    bridge.config = SimpleNamespace(inbound_message_priority="urgent")
    bridge.locks = {"advisor": asyncio.Lock()}
    bridge.processed_events = set()
    bridge.state = SimpleNamespace(
        save=lambda: None,
        data={"pending": {}, "processed_events": []},
    )

    async def create_qitem(*args, **kwargs):
        return "$qitem"

    async def start_request(*args, **kwargs):
        return None

    bridge._create_inbound_qitem = create_qitem
    bridge._start_request = start_request
    room = SimpleNamespace(room_id="!advisor:matrix.example.org")

    asyncio.run(
        bridge._enqueue_prompt(
            agent,
            room,
            "$prompt",
            1_700_000_000_000,
            "request",
            relates_to=relates_to,
        )
    )

    request = bridge.state.data["pending"]["advisor"][0]
    assert request["thread_root_event_id"] == thread_root
    assert request["reply_to_event_id"] == reply_to
    assert request["source_event_ids"] == ["$prompt"]
    assert request["source_relation"] == relates_to


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


def test_persistent_commentary_failure_falls_through_to_final_answer(tmp_path):
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_text(
        "".join(
            json.dumps(
                {
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
            )
            + "\n"
            for item_id, phase, text in [
                ("comment-1", "commentary", "Ich prüfe das."),
                ("final-1", "final_answer", "Fertig."),
            ]
        ),
        encoding="utf-8",
    )
    agent = AgentConfig(
        key="advisor",
        display_name="Advisor",
        session="advisor-lead@kernel",
    )
    request = {
        "event_id": "$prompt",
        "thread_root_event_id": "$prompt",
        "reply_to_event_id": "$prompt",
        "answer_text": None,
        "started_at": time.time(),
        "session_id": "session",
        "transcript_path": str(transcript),
        "offset": 0,
    }
    bridge = object.__new__(MatrixBridge)
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
    finished = []

    async def send(room_id, text, reply_to=None, **kwargs):
        if text == "Ich prüfe das.":
            return SimpleNamespace(status_code="M_UNKNOWN")
        sent.append((text, reply_to, kwargs))
        return RoomSendResponse("$answer", room_id)

    async def finish_qitem(agent_arg, request_arg):
        finished.append(request_arg["event_id"])

    async def noop(*args, **kwargs):
        return None

    bridge._send = send
    bridge._finish_inbound_qitem = finish_qitem
    bridge._set_typing = noop
    bridge._update_agent_status = noop
    bridge._update_queue_notice = noop

    for _attempt in range(5):
        if not bridge.state.data["pending"]["advisor"]:
            break
        asyncio.run(bridge._poll_agent(agent))

    assert sent == [("Fertig.", "$prompt", {"thread_root": "$prompt"})]
    assert finished == ["$prompt"]
    assert bridge.state.data["pending"]["advisor"] == []
    assert request["commentary_failure_count"] == 3
    assert request["commentary_skipped"] is True


def test_persistent_final_delivery_failure_fails_qitem_and_advances(tmp_path):
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_text(
        json.dumps(
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
            }
        )
        + "\n",
        encoding="utf-8",
    )
    agent = AgentConfig(
        key="advisor",
        display_name="Advisor",
        session="advisor-lead@kernel",
    )
    first = {
        "event_id": "$first",
        "thread_root_event_id": "$root",
        "reply_to_event_id": "$root",
        "answer_text": None,
        "started_at": time.time(),
        "session_id": "session",
        "transcript_path": str(transcript),
        "offset": 0,
    }
    second = {
        "event_id": "$second",
        "started_at": None,
    }
    bridge = object.__new__(MatrixBridge)
    bridge.config = SimpleNamespace(
        mirror_commentary=False,
        response_timeout_minutes=30,
    )
    bridge.locks = {"advisor": asyncio.Lock()}
    bridge.state = SimpleNamespace(
        save=lambda: None,
        data={
            "pending": {"advisor": [first, second]},
            "rooms": {"advisor": {"room_id": "!advisor:matrix.example.org"}},
        },
    )
    failed = []
    started = []

    async def send(*args, **kwargs):
        return SimpleNamespace(status_code="M_UNKNOWN")

    async def fail_qitem(agent_arg, request_arg, reason):
        failed.append((request_arg["event_id"], reason))

    async def start_request(agent_arg, request_arg, room_id):
        started.append(request_arg["event_id"])

    async def noop(*args, **kwargs):
        return None

    bridge._send = send
    bridge._fail_inbound_qitem = fail_qitem
    bridge._start_request = start_request
    bridge._set_typing = noop
    bridge._update_agent_status = noop
    bridge._update_queue_notice = noop

    for _attempt in range(5):
        if first not in bridge.state.data["pending"]["advisor"]:
            break
        asyncio.run(bridge._poll_agent(agent))

    assert failed == [
        (
            "$first",
            "Matrix answer delivery failed after 3 attempts",
        )
    ]
    assert started == ["$second"]
    assert [
        request["event_id"] for request in bridge.state.data["pending"]["advisor"]
    ] == ["$second"]
    assert first["delivery_failure_count"] == 3


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


@pytest.mark.parametrize(
    ("reply_to", "thread_root", "expected"),
    [
        (
            "$prompt",
            "$prompt",
            {
                "rel_type": "m.thread",
                "event_id": "$prompt",
                "is_falling_back": True,
                "m.in_reply_to": {"event_id": "$prompt"},
            },
        ),
        (
            "$root",
            "$root",
            {
                "rel_type": "m.thread",
                "event_id": "$root",
                "is_falling_back": True,
                "m.in_reply_to": {"event_id": "$root"},
            },
        ),
        (
            "$prompt",
            None,
            {"m.in_reply_to": {"event_id": "$prompt"}},
        ),
    ],
)
def test_send_uses_canonical_thread_target(reply_to, thread_root, expected):
    class ResponseClient:
        async def room_send(self, room_id, message_type, content, **kwargs):
            self.content = content
            return RoomSendResponse("$message", room_id)

    bridge = object.__new__(MatrixBridge)
    bridge.client = ResponseClient()

    asyncio.run(
        bridge._send(
            "!advisor:matrix.example.org",
            "answer",
            reply_to,
            thread_root=thread_root,
        )
    )

    assert bridge.client.content["m.relates_to"] == expected


def test_poll_agent_uses_persisted_canonical_thread_root(tmp_path):
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_text(
        "".join(
            json.dumps(
                {
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
            )
            + "\n"
            for item_id, phase, text in [
                ("comment-1", "commentary", "Ich prüfe das."),
                ("final-1", "final_answer", "Fertig."),
            ]
        ),
        encoding="utf-8",
    )
    agent = AgentConfig(
        key="advisor",
        display_name="Advisor",
        session="advisor-lead@kernel",
    )
    request = {
        "event_id": "$nested-reply",
        "thread_root_event_id": "$root",
        "reply_to_event_id": "$root",
        "source_event_ids": ["$nested-reply"],
        "answer_text": None,
        "started_at": time.time(),
        "session_id": "session",
        "transcript_path": str(transcript),
        "offset": 0,
    }
    bridge = object.__new__(MatrixBridge)
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

    async def send(room_id, text, reply_to=None, **kwargs):
        sent.append((text, reply_to, kwargs))
        return RoomSendResponse("$answer", room_id)

    async def noop(*args):
        return None

    bridge._send = send
    bridge._finish_inbound_qitem = noop
    bridge._set_typing = noop
    bridge._update_agent_status = noop
    bridge._update_queue_notice = noop

    asyncio.run(bridge._poll_agent(agent))

    assert sent == [
        ("Ich prüfe das.", "$root", {"thread_root": "$root"}),
        ("Fertig.", "$root", {"thread_root": "$root"}),
    ]


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
    bridge.config = SimpleNamespace(inbound_message_priority="urgent")
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


def test_matrix_message_creates_priority_qitem():
    bridge = object.__new__(MatrixBridge)
    agent = AgentConfig(
        key="advisor",
        display_name="Advisor",
        session="advisor-lead@kernel",
    )
    created = []
    started = []

    class Gateway:
        async def queue_create(self, **kwargs):
            created.append(kwargs)
            return "qitem-matrix"

    async def start_request(agent_arg, request, room_id):
        started.append((agent_arg.key, request, room_id))

    bridge.config = SimpleNamespace(
        queue_inbound_messages=True,
        inbound_message_priority="urgent",
        inbound_queue_source="owner@external",
    )
    bridge.gateway = Gateway()
    bridge.locks = {"advisor": asyncio.Lock()}
    bridge.processed_events = set()
    bridge.state = SimpleNamespace(
        data={"pending": {}, "processed_events": []},
        save=lambda: None,
    )
    bridge._start_request = start_request
    room = SimpleNamespace(room_id="!advisor:matrix.example.org")

    asyncio.run(
        bridge._enqueue_prompt(
            agent,
            room,
            "$matrix-event",
            1_700_000_000_000,
            "Bitte prüfen.",
        )
    )

    assert created[0]["source_session"] == "owner@external"
    assert created[0]["destination_session"] == "advisor-lead@kernel"
    assert created[0]["priority"] == "urgent"
    assert started[0][1]["qitem_id"] == "qitem-matrix"
    assert started[0][1]["priority"] == "urgent"


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
