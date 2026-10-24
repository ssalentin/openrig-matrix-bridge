import json
from pathlib import Path

from openrig_matrix_bridge.transcript import (
    activity_from_record,
    final_answer,
    latest_activity,
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


def test_classifies_reasoning_and_assistant_messages():
    reasoning = {"type": "response_item", "payload": {"type": "reasoning"}}
    writing = {
        "type": "response_item",
        "payload": {"type": "message", "role": "assistant"},
    }

    assert activity_from_record(reasoning) == ("busy", "denkt")
    assert activity_from_record(writing) == ("busy", "schreibt")


def test_classifies_search_network_and_file_edits_without_exposing_content():
    search = {
        "type": "event_msg",
        "payload": {
            "type": "item_completed",
            "item": {
                "type": "CommandExecution",
                "command": ["/bin/bash", "-c", "rg -n secret src"],
                "parsed_cmd": [{"type": "search", "cmd": "rg -n secret src"}],
            },
        },
    }
    network = {
        "type": "event_msg",
        "payload": {
            "type": "item_completed",
            "item": {
                "type": "CommandExecution",
                "command": ["/bin/bash", "-c", "curl https://example.org"],
                "parsed_cmd": [],
            },
        },
    }
    edit = {
        "type": "response_item",
        "payload": {"type": "custom_tool_call", "name": "apply_patch"},
    }

    assert activity_from_record(search) == ("busy", "sucht")
    assert activity_from_record(network) == ("busy", "Netzwerk")
    assert activity_from_record(edit) == ("busy", "bearbeitet Dateien")

    search_call = {
        "type": "response_item",
        "payload": {
            "type": "function_call",
            "name": "exec_command",
            "arguments": json.dumps({"cmd": "rg TODO src"}),
        },
    }
    assert activity_from_record(search_call) == ("busy", "sucht")


def test_latest_activity_uses_the_most_recent_record():
    records = [
        {"type": "response_item", "payload": {"type": "reasoning"}},
        {
            "type": "response_item",
            "payload": {"type": "custom_tool_call", "name": "apply_patch"},
        },
    ]

    assert latest_activity(records) == ("busy", "bearbeitet Dateien")

