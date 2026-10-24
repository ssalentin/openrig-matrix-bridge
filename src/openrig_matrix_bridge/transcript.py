from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


def read_complete_records(path: Path, offset: int) -> tuple[list[dict[str, Any]], int]:
    """Read complete JSONL records starting at offset.

    A trailing partial line is deliberately left for the next poll.
    """
    with path.open("rb") as handle:
        handle.seek(offset)
        data = handle.read()
    if not data:
        return [], offset

    last_newline = data.rfind(b"\n")
    if last_newline < 0:
        return [], offset

    complete = data[: last_newline + 1]
    records: list[dict[str, Any]] = []
    for raw_line in complete.splitlines():
        if not raw_line.strip():
            continue
        try:
            value = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            records.append(value)
    return records, offset + len(complete)


def final_answer(record: dict[str, Any]) -> str | None:
    if record.get("type") != "event_msg":
        return None
    payload = record.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "item_completed":
        return None
    item = payload.get("item")
    if not isinstance(item, dict):
        return None
    if item.get("type") != "AgentMessage" or item.get("phase") != "final_answer":
        return None
    parts = [
        str(part.get("text", ""))
        for part in item.get("content") or []
        if isinstance(part, dict) and part.get("type") in (None, "Text")
    ]
    text = "".join(parts).strip()
    return text or None


def _command_activity(item: dict[str, Any]) -> str:
    command = " ".join(str(part) for part in item.get("command") or [])
    arguments = item.get("arguments")
    if isinstance(arguments, str):
        try:
            argument_data = json.loads(arguments)
        except json.JSONDecodeError:
            argument_data = None
        if isinstance(argument_data, dict):
            command += " " + str(
                argument_data.get("cmd") or argument_data.get("command") or ""
            )
    parsed = item.get("parsed_cmd")
    parsed_types = {
        str(entry.get("type"))
        for entry in parsed or []
        if isinstance(entry, dict) and entry.get("type")
    }
    parsed_commands = " ".join(
        str(entry.get("cmd") or "")
        for entry in parsed or []
        if isinstance(entry, dict)
    )
    combined = f"{command}\n{parsed_commands}".lower()
    if re.search(r"(^|[\s;&|])(curl|wget|gh\s+api|ssh|scp)\b", combined):
        return "Netzwerk"
    if "search" in parsed_types or re.search(
        r"(^|[\s;&|])(rg|grep|find|fd|git\s+grep)\b",
        combined,
    ):
        return "sucht"
    if parsed_types & {"read", "list"}:
        return "liest"
    if parsed_types & {"edit", "write"}:
        return "bearbeitet Dateien"
    return "Werkzeug"


def _tool_activity(name: str, item: dict[str, Any]) -> str:
    normalized = name.lower().replace("-", "_")
    if any(token in normalized for token in ("web_search", "search_web", "browser")):
        return "Websuche"
    if any(token in normalized for token in ("apply_patch", "write", "edit")):
        return "bearbeitet Dateien"
    if "view_image" in normalized:
        return "prüft Bild"
    if normalized in {"exec_command", "shell", "bash"}:
        return _command_activity(item)
    return "Werkzeug"


def activity_from_record(record: dict[str, Any]) -> tuple[str, str] | None:
    """Classify one Codex transcript record without exposing reasoning content."""
    if record.get("type") == "event_msg":
        payload = record.get("payload")
        if not isinstance(payload, dict):
            return None
        payload_type = payload.get("type")
        if payload_type == "task_started":
            return "busy", "arbeitet"
        if payload_type == "turn_aborted":
            return "error", "abgebrochen"
        if payload_type != "item_completed":
            return None
        item = payload.get("item")
        if not isinstance(item, dict):
            return None
        item_type = item.get("type")
        if item_type == "Reasoning":
            return "busy", "denkt"
        if item_type == "AgentMessage":
            return "busy", "schreibt"
        if item_type == "CommandExecution":
            return "busy", _command_activity(item)
        if item_type == "FileChange":
            return "busy", "bearbeitet Dateien"
        if item_type == "McpToolCall":
            return "busy", _tool_activity(
                str(item.get("tool") or item.get("name") or ""),
                item,
            )
        return None

    if record.get("type") != "response_item":
        return None
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return None
    payload_type = payload.get("type")
    if payload_type == "reasoning":
        return "busy", "denkt"
    if payload_type == "message" and payload.get("role") == "assistant":
        return "busy", "schreibt"
    if payload_type in {"function_call", "custom_tool_call"}:
        return "busy", _tool_activity(
            str(payload.get("name") or ""),
            payload,
        )
    return None


def latest_activity(records: list[dict[str, Any]]) -> tuple[str, str] | None:
    latest = None
    for record in records:
        activity = activity_from_record(record)
        if activity is not None:
            latest = activity
    return latest

