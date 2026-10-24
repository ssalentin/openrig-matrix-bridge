import json
from pathlib import Path

from openrig_matrix_bridge.transcript import (
    commentary_messages,
    final_answer,
    read_complete_records,
)


def test_extracts_codex_final_answer():
    record = {
        "type": "event_msg",
        "payload": {
            "type": "item_completed",
            "item": {
                "type": "AgentMessage",
                "phase": "final_answer",
                "content": [{"type": "Text", "text": "Fertig."}],
            },
        },
    }
    assert final_answer(record) == "Fertig."


def test_ignores_partial_line_and_reads_next_complete_line(tmp_path: Path):
    path = tmp_path / "session.jsonl"
    first = json.dumps(
        {
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "item": {
                    "type": "AgentMessage",
                    "phase": "final_answer",
                    "content": [{"type": "Text", "text": "hello"}],
                },
            },
        }
    )
    path.write_bytes((first + "\n" + '{"type":').encode())
    records, offset = read_complete_records(path, 0)
    assert len(records) == 1
    assert offset == len(first) + 1


def test_extracts_visible_commentary_messages_in_order():
    def message(item_id: str, phase: str, text: str):
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

    records = [
        message("one", "commentary", "Ich prüfe das."),
        {"type": "response_item", "payload": {"type": "reasoning"}},
        message("two", "commentary", "Jetzt setze ich es um."),
        message("final", "final_answer", "Fertig."),
    ]

    assert commentary_messages(records) == [
        ("one", "Ich prüfe das."),
        ("two", "Jetzt setze ich es um."),
    ]

