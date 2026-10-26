import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from nio import RoomSendResponse

from openrig_matrix_bridge.bridge import MatrixBridge, StateStore
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
            {
                "thread_root": "$prompt",
                "tx_id": "activity-card-request-1",
            },
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
                    "REDIS_URL=redis://default:secretpassword@host\n"
                    "Authorization header: Basic dXNlcjpwYXNzd29yZA==\n"
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
    assert "secretpassword" not in card
    assert "dXNlcjpwYXNzd29yZA==" not in card
    assert "/home/alice/private" not in card
    assert "/srv/private" not in card
    assert "/mnt/private" not in card
    assert "../customer" not in card
    assert "raw private command output" not in card
    assert "raw tilde command output" not in card
    for marker in (
        "[lokaler Pfad]",
        "[Token entfernt]",
        "[Zugangsdaten entfernt]",
        "[Ausgabe entfernt]",
    ):
        assert marker in card
    assert card.startswith("**Aktivität:** Arbeitet\n\nPrüfe [lokaler Pfad] mit")


@pytest.mark.parametrize(
    ("commentary", "secret", "marker"),
    [
        (
            "REDIS_URL=redis://default:secretpassword@host",
            "secretpassword",
            "[Zugangsdaten entfernt]",
        ),
        ("REDIS_URL=redis://:hunter2@host", "hunter2", "[Zugangsdaten entfernt]"),
        (
            "Authorization header: Basic dXNlcjpwYXNzd29yZA==",
            "dXNlcjpwYXNzd29yZA==",
            "[Token entfernt]",
        ),
        (
            "Authorization: Basic dXNlcjpwYXNzd29yZA==",
            "dXNlcjpwYXNzd29yZA==",
            "[Token entfernt]",
        ),
        ("Authorization: Bearer very-secret-token", "very-secret-token", "[Token entfernt]"),
        ("Cookie: session=abcdef1234567890", "abcdef1234567890", "[Cookie entfernt]"),
        ("Cookie session=abcdef1234567890", "abcdef1234567890", "[Cookie entfernt]"),
        ("Set-Cookie: sid=abcdef1234567890", "abcdef1234567890", "[Cookie entfernt]"),
    ],
)
def test_activity_card_redacts_uri_auth_and_cookie_credentials(
    tmp_path,
    commentary,
    secret,
    marker,
):
    bridge, agent, _request, sent, _edited = activity_bridge(
        tmp_path,
        [message_record("comment-1", "commentary", commentary)],
    )

    asyncio.run(bridge._poll_agent(agent))

    card = sent[0][0]
    assert card.startswith("**Aktivität:** Arbeitet\n\n")
    assert secret not in card
    assert marker in card


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


def test_successful_answer_retries_terminal_card_before_advancing(tmp_path):
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
        if text == "Fertig.":
            return RoomSendResponse("$answer", room_id)
        return SimpleNamespace(status_code="M_UNKNOWN")

    async def reject_edit(room_id, event_id, text, **kwargs):
        edited.append((event_id, text, kwargs))
        return SimpleNamespace(status_code="M_UNKNOWN")

    bridge._send = send
    bridge._edit_message = reject_edit

    asyncio.run(bridge._poll_agent(agent))

    assert request in bridge.state.data["pending"]["advisor"]
    assert request["answer_phase"] == "sent"
    assert request["answer_event_id"] == "$answer"
    assert request["activity_terminal_intent"] == {
        "disposition": "completed",
        "label": "Abgeschlossen",
        "status_detail": "bereit",
    }
    assert sum(text == "Fertig." for text, _reply_to, _kwargs in sent) == 1

    store = StateStore(tmp_path / "state.json")
    store.data = bridge.state.data
    store.save()
    bridge.state = StateStore(store.path)

    async def accept_edit(room_id, event_id, text, **kwargs):
        edited.append((event_id, text, kwargs))
        return RoomSendResponse("$edit", room_id)

    bridge._edit_message = accept_edit
    asyncio.run(bridge._poll_agent(agent))

    assert bridge.state.data["pending"]["advisor"] == []
    assert {
        kwargs.get("tx_id")
        for _event_id, _text, kwargs in edited
    } == {"activity-terminal-request-1"}
    assert sum(text == "Fertig." for text, _reply_to, _kwargs in sent) == 1


def test_timeout_retries_terminal_card_before_advancing(tmp_path):
    bridge, agent, request, _sent, edited = activity_bridge(tmp_path, [])
    request.update(
        {
            "started_at": time.time() - 60,
            "commentary_event_id": "$status",
            "commentary_summary": "Working on it.",
            "activity_card_text": "**Aktivität:** Arbeitet\n\nWorking on it.",
            "qitem_id": "qitem-request-1",
        }
    )
    bridge.config.response_timeout_minutes = 0
    sent = []
    failed = []

    async def send(room_id, text, reply_to=None, **kwargs):
        sent.append((text, reply_to, kwargs))
        if "Matrix-Antwort konnte nicht zugestellt werden" in text:
            return RoomSendResponse("$timeout", room_id)
        return SimpleNamespace(status_code="M_UNKNOWN")

    async def reject_edit(room_id, event_id, text, **kwargs):
        edited.append((event_id, text, kwargs))
        return SimpleNamespace(status_code="M_UNKNOWN")

    async def fail_qitem(agent_arg, request_arg, reason):
        failed.append((agent_arg.key, request_arg["qitem_id"], reason))

    bridge._send = send
    bridge._edit_message = reject_edit
    bridge._fail_inbound_qitem = fail_qitem

    asyncio.run(bridge._poll_agent(agent))

    assert request in bridge.state.data["pending"]["advisor"]
    assert request["activity_terminal_intent"] == {
        "disposition": "timeout",
        "label": "Zeitüberschreitung",
        "status_detail": "Zeitüberschreitung",
    }
    assert failed == []

    async def accept_edit(room_id, event_id, text, **kwargs):
        edited.append((event_id, text, kwargs))
        return RoomSendResponse("$edit", room_id)

    bridge._edit_message = accept_edit
    asyncio.run(bridge._poll_agent(agent))

    assert bridge.state.data["pending"]["advisor"] == []
    assert sum(
        "Matrix-Antwort konnte nicht zugestellt werden" in text
        for text, _reply_to, _kwargs in sent
    ) == 1
    assert failed == [
        (
            "advisor",
            "qitem-request-1",
            "Timed out waiting for a Matrix answer",
        )
    ]


def test_final_delivery_failure_retries_terminal_card_before_advancing(tmp_path):
    bridge, agent, request, _sent, edited = activity_bridge(
        tmp_path,
        [message_record("final-1", "final_answer", "Fertig.")],
    )
    request.update(
        {
            "commentary_event_id": "$status",
            "commentary_summary": "Working on it.",
            "activity_card_text": "**Aktivität:** Arbeitet\n\nWorking on it.",
            "qitem_id": "qitem-request-1",
        }
    )
    sent = []
    failed = []

    async def send(room_id, text, reply_to=None, **kwargs):
        sent.append((text, reply_to, kwargs))
        if "Matrix-Antwort konnte nicht zugestellt werden" in text:
            return RoomSendResponse("$failure", room_id)
        return SimpleNamespace(status_code="M_UNKNOWN")

    async def reject_edit(room_id, event_id, text, **kwargs):
        edited.append((event_id, text, kwargs))
        return SimpleNamespace(status_code="M_UNKNOWN")

    async def fail_qitem(agent_arg, request_arg, reason):
        failed.append((agent_arg.key, request_arg["qitem_id"], reason))

    bridge._send = send
    bridge._edit_message = reject_edit
    bridge._fail_inbound_qitem = fail_qitem

    for _attempt in range(3):
        asyncio.run(bridge._poll_agent(agent))

    assert request in bridge.state.data["pending"]["advisor"]
    assert request["delivery_failure_count"] == 3
    assert request["activity_terminal_intent"] == {
        "disposition": "delivery_failed",
        "label": "Fehlgeschlagen",
        "status_detail": "Zustellung fehlgeschlagen",
    }
    assert failed == []

    async def accept_edit(room_id, event_id, text, **kwargs):
        edited.append((event_id, text, kwargs))
        return RoomSendResponse("$edit", room_id)

    bridge._edit_message = accept_edit
    asyncio.run(bridge._poll_agent(agent))

    assert bridge.state.data["pending"]["advisor"] == []
    assert failed == [
        (
            "advisor",
            "qitem-request-1",
            "Matrix answer delivery failed after 3 attempts",
        )
    ]
    assert sum(text == "Fertig." for text, _reply_to, _kwargs in sent) == 3
    assert (
        sum(
            "Matrix-Antwort konnte nicht zugestellt werden" in text
            for text, _reply_to, _kwargs in sent
        )
        == 1
    )


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


def test_activity_refresh_does_not_create_a_second_card(tmp_path):
    bridge, agent, _request, sent, edited = activity_bridge(tmp_path, [])
    bridge.resolver = SimpleNamespace(
        resolve_many=lambda sessions: {
            agent.session: SimpleNamespace(
                transcript_path=tmp_path / "rollout.jsonl",
                busy=False,
                activity_state="idle-at-prompt",
                needs_input_count=0,
            )
        }
    )

    asyncio.run(bridge._refresh_agent_statuses(force=True))

    assert sent == []
    assert edited == []


def test_activity_refresh_cannot_overwrite_completed_card(tmp_path):
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

    async def scenario():
        stale_edit_started = asyncio.Event()
        terminal_edit_started = asyncio.Event()
        release_stale_edit = asyncio.Event()
        waiting_edit_count = 0

        async def edit(room_id, event_id, text, **kwargs):
            nonlocal waiting_edit_count
            edited.append((event_id, text, kwargs))
            if text.startswith("**Aktivität:** Wartet auf Eingabe"):
                waiting_edit_count += 1
                if waiting_edit_count == 1:
                    stale_edit_started.set()
                    await release_stale_edit.wait()
            elif text.startswith("**Aktivität:** Abgeschlossen"):
                terminal_edit_started.set()
            return RoomSendResponse("$edit", room_id)

        bridge._edit_message = edit
        refresh = asyncio.create_task(
            bridge._refresh_agent_statuses(force=True)
        )
        await stale_edit_started.wait()
        poll = asyncio.create_task(bridge._poll_agent(agent))
        deadline = asyncio.get_running_loop().time() + 0.2
        while (
            not terminal_edit_started.is_set()
            and asyncio.get_running_loop().time() < deadline
        ):
            await asyncio.sleep(0.01)
        release_stale_edit.set()
        await asyncio.gather(refresh, poll)

    asyncio.run(scenario())

    assert bridge.state.data["pending"]["advisor"] == []
    assert edited[-1][1] == (
        "**Aktivität:** Abgeschlossen\n\nWorking on it."
    )
    assert request["activity_card_text"] == (
        "**Aktivität:** Abgeschlossen\n\nWorking on it."
    )


def test_activity_refresh_does_not_replace_terminal_card_intent(tmp_path):
    bridge, agent, request, _sent, edited = activity_bridge(tmp_path, [])
    terminal_intent = {
        "disposition": "completed",
        "label": "Abgeschlossen",
        "status_detail": "bereit",
    }
    request.update(
        {
            "commentary_event_id": "$status",
            "commentary_summary": "Ich prüfe die Tests.",
            "activity_label": "Abgeschlossen",
            "activity_terminal_intent": terminal_intent,
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

    assert edited == []
    assert request["activity_label"] == "Abgeschlossen"
    assert request["activity_terminal_intent"] == terminal_intent


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


class DedupingRoom:
    """Matrix send semantics the bridge relies on: a repeated transaction ID
    returns the event it already created (Synapse keys on path, user, device).
    A send without one always creates a new event. The first activity-card
    send is persisted but its response is lost.
    """

    def __init__(self):
        self.events: dict[str, dict] = {}
        self.by_txn: dict[str, str] = {}
        self.card_response_lost = False

    async def send(self, room_id, text, reply_to=None, **kwargs):
        tx_id = kwargs.get("tx_id")
        if tx_id and tx_id in self.by_txn:
            return RoomSendResponse(self.by_txn[tx_id], room_id)
        event_id = f"$event-{len(self.events) + 1}"
        self.events[event_id] = {"text": text, "tx_id": tx_id}
        if tx_id:
            self.by_txn[tx_id] = event_id
        if text.startswith("**Aktivität:**") and not self.card_response_lost:
            self.card_response_lost = True
            # Timeout, connection reset, or the bridge stopped mid-request.
            raise ConnectionError("response lost after the event was persisted")
        return RoomSendResponse(event_id, room_id)

    def cards(self) -> list[str]:
        return [
            event_id
            for event_id, event in self.events.items()
            if event["text"].startswith("**Aktivität:**")
        ]


def test_lost_card_response_is_retried_into_the_same_card(tmp_path):
    bridge, agent, request, _sent, edited = activity_bridge(
        tmp_path,
        [message_record("comment-1", "commentary", "Ich prüfe die Tests.")],
    )
    room = DedupingRoom()
    bridge._send = room.send

    asyncio.run(bridge._poll_agent(agent))  # card persisted, response lost
    assert "commentary_event_id" not in request
    asyncio.run(bridge._poll_agent(agent))  # retry with the same transaction

    assert room.cards() == ["$event-1"]
    assert request["commentary_event_id"] == "$event-1"

    with open(request["transcript_path"], "a", encoding="utf-8") as handle:
        handle.write(json.dumps(message_record("final-1", "final_answer", "Fertig.")))
        handle.write("\n")
    asyncio.run(bridge._poll_agent(agent))

    assert {event_id for event_id, _text, _kwargs in edited} == {"$event-1"}
    assert room.cards() == ["$event-1"]
