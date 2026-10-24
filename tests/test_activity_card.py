import asyncio
import json
import time
from types import SimpleNamespace

from nio import RoomSendResponse

from openrig_matrix_bridge.bridge import MatrixBridge
from openrig_matrix_bridge.config import AgentConfig


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


def activity_bridge(tmp_path, records, *, activity_state="working"):
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )
    agent = AgentConfig(
        key="advisor",
        display_name="Advisor",
        session="advisor-lead@kernel",
    )
    request = {
        "id": "request-1",
        "event_id": "$prompt",
        "thread_root_event_id": "$prompt",
        "reply_to_event_id": "$prompt",
        "answer_text": None,
        "started_at": time.time(),
        "session_id": "session",
        "transcript_path": str(transcript),
        "offset": 0,
        "activity_state": activity_state,
        "needs_input_count": 0,
    }
    bridge = object.__new__(MatrixBridge)
    bridge.config = SimpleNamespace(
        mirror_commentary=True,
        response_timeout_minutes=30,
        status_refresh_seconds=30,
        agents={"advisor": agent},
    )
    bridge.locks = {"advisor": asyncio.Lock()}
    bridge.state = SimpleNamespace(
        save=lambda: None,
        data={
            "pending": {"advisor": [request]},
            "rooms": {"advisor": {"room_id": "!advisor:matrix.example.org"}},
        },
    )
    bridge.agent_statuses = {}
    sent = []
    edited = []

    async def send(room_id, text, reply_to=None, **kwargs):
        sent.append((text, reply_to, kwargs))
        event_id = "$status" if len(sent) == 1 else "$answer"
        return RoomSendResponse(event_id, room_id)

    async def edit(room_id, event_id, text, **kwargs):
        edited.append((event_id, text, kwargs))
        return RoomSendResponse("$edit", room_id)

    async def noop(*args, **kwargs):
        return None

    bridge._send = send
    bridge._edit_message = edit
    bridge._set_typing = noop
    bridge._update_agent_status = noop
    bridge._finish_inbound_qitem = noop
    bridge._update_queue_notice = noop
    return bridge, agent, request, sent, edited


def test_activity_card_combines_known_activity_and_safe_commentary(tmp_path):
    bridge, agent, _request, sent, edited = activity_bridge(
        tmp_path,
        [
            message_record("comment-1", "commentary", "Ich prüfe die Tests."),
            message_record("final-1", "final_answer", "Fertig."),
        ],
    )

    asyncio.run(bridge._poll_agent(agent))

    assert sent == [
        (
            "**Aktivität:** Arbeitet\n\nIch prüfe die Tests.",
            "$prompt",
            {"thread_root": "$prompt"},
        ),
        (
            "Fertig.",
            "$prompt",
            {"thread_root": "$prompt", "tx_id": "answer-request-1"},
        ),
    ]
    assert edited == [
        (
            "$status",
            "**Aktivität:** Abgeschlossen\n\nIch prüfe die Tests.",
            {"tx_id": "activity-terminal-request-1"},
        )
    ]
    assert bridge.state.data["pending"]["advisor"] == []


def test_activity_card_uses_generic_label_for_unknown_runtime_state(tmp_path):
    bridge, agent, _request, sent, _edited = activity_bridge(
        tmp_path,
        [message_record("comment-1", "commentary", "Ich arbeite daran.")],
        activity_state="unknown",
    )

    asyncio.run(bridge._poll_agent(agent))

    assert sent[0][0] == "**Aktivität:** Arbeitet\n\nIch arbeite daran."


def test_activity_card_is_created_before_visible_commentary(tmp_path):
    bridge, agent, _request, sent, _edited = activity_bridge(tmp_path, [])

    asyncio.run(bridge._poll_agent(agent))

    assert sent[0][0] == "**Aktivität:** Arbeitet"


def test_activity_card_sanitizes_tokens_paths_and_raw_output(tmp_path):
    bridge, agent, _request, sent, _edited = activity_bridge(
        tmp_path,
        [
            message_record(
                "comment-1",
                "commentary",
                (
                    "Prüfe /home/alice/private/project/secret.txt mit "
                    "Authorization: Bearer very-secret-token\n"
                    "API_KEY=super-secret-value\n"
                    "CLIENT_SECRET=top-secret\n"
                    "token ghp_1234567890abcdef\n"
                    "Reading /srv/private/customer/credentials.txt\n"
                    "Reading /mnt/private/customer/credentials.txt\n"
                    "Reading ../customer/credentials.txt\n"
                    "```\nraw private command output\n```\n"
                    "~~~\nraw tilde command output\n~~~\n"
                    + "detail " * 150
                ),
            )
        ],
    )

    asyncio.run(bridge._poll_agent(agent))

    card = sent[0][0]
    assert "very-secret-token" not in card
    assert "super-secret-value" not in card
    assert "top-secret" not in card
    assert "ghp_1234567890abcdef" not in card
    assert "/home/alice/private" not in card
    assert "/srv/private" not in card
    assert "/mnt/private" not in card
    assert "../customer" not in card
    assert "raw private command output" not in card
    assert "raw tilde command output" not in card
    assert "[redacted" in card


def test_activity_card_truncates_long_safe_commentary(tmp_path):
    bridge, agent, _request, sent, _edited = activity_bridge(
        tmp_path,
        [message_record("comment-1", "commentary", "safe update " * 150)],
    )

    asyncio.run(bridge._poll_agent(agent))

    card = sent[0][0]
    assert card.split("\n\n", 1)[1].endswith("…")
    assert len(card.split("\n\n", 1)[1]) <= 600


def test_terminal_activity_edit_failure_posts_explicit_terminal_fallback(tmp_path):
    bridge, agent, request, _sent, edited = activity_bridge(
        tmp_path,
        [message_record("final-1", "final_answer", "Fertig.")],
    )
    request.update(
        {
            "commentary_event_id": "$status",
            "commentary_summary": "Working on it.",
            "activity_card_text": "**Aktivität:** Arbeitet\n\nWorking on it.",
        }
    )
    sent = []

    async def send(room_id, text, reply_to=None, **kwargs):
        sent.append((text, reply_to, kwargs))
        return RoomSendResponse("$answer", room_id)

    async def reject_edit(room_id, event_id, text, **kwargs):
        edited.append((event_id, text, kwargs))
        return SimpleNamespace(status_code="M_UNKNOWN")

    bridge._send = send
    bridge._edit_message = reject_edit

    asyncio.run(bridge._poll_agent(agent))

    assert len(edited) == 3
    assert all(text == "**Aktivität:** Abgeschlossen\n\nWorking on it." for _, text, _ in edited)
    assert {kwargs.get("tx_id") for _, _, kwargs in edited} == {
        "activity-terminal-" + request["id"]
    }
    assert sent == [
        (
            "Fertig.",
            "$prompt",
            {"thread_root": "$prompt", "tx_id": "answer-request-1"},
        ),
        (
            "**Aktivität:** Abgeschlossen\n\nWorking on it.",
            "$prompt",
            {
                "thread_root": "$prompt",
                "tx_id": "activity-terminal-" + request["id"],
            },
        ),
    ]
    assert bridge.state.data["pending"]["advisor"] == []


def test_activity_refresh_edits_the_same_card_for_needs_input(tmp_path):
    bridge, agent, request, _sent, edited = activity_bridge(
        tmp_path,
        [],
    )
    request.update(
        {
            "commentary_event_id": "$status",
            "commentary_summary": "Ich prüfe die Tests.",
            "activity_state": "working",
            "needs_input_count": 0,
        }
    )
    bridge.resolver = SimpleNamespace(
        resolve_many=lambda sessions: {
            agent.session: SimpleNamespace(
                transcript_path=tmp_path / "rollout.jsonl",
                busy=True,
                activity_state="working",
                needs_input_count=1,
            )
        }
    )

    asyncio.run(bridge._refresh_agent_statuses(force=True))

    assert edited == [
        (
            "$status",
            "**Aktivität:** Wartet auf Eingabe\n\nIch prüfe die Tests.",
            {},
        )
    ]


def test_activity_card_is_not_created_when_commentary_is_disabled(tmp_path):
    bridge, agent, _request, sent, edited = activity_bridge(
        tmp_path,
        [message_record("final-1", "final_answer", "Fertig.")],
    )
    bridge.config.mirror_commentary = False

    asyncio.run(bridge._poll_agent(agent))

    assert sent == [
        (
            "Fertig.",
            "$prompt",
            {"thread_root": "$prompt", "tx_id": "answer-request-1"},
        )
    ]
    assert edited == []
