import asyncio
from types import SimpleNamespace

import pytest

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
    assert acknowledgements[0][1] == "Antwort an **Advisor** übergeben: Klein"


@pytest.mark.parametrize("text", ["ja", "Ja", "JA!", "bestätigen", "stimmt"])
def test_accepts_verification(text):
    assert verification_decision(text) is True


@pytest.mark.parametrize("text", ["nein", "Nein.", "ablehnen", "stimmt nicht"])
def test_rejects_verification(text):
    assert verification_decision(text) is False


def test_ignores_other_messages():
    assert verification_decision("Was ist der Status?") is None
