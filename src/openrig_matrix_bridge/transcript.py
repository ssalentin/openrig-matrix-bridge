from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Any


def record_turn_id(record: dict[str, Any]) -> str | None:
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return None
    turn_id = payload.get("turn_id")
    if isinstance(turn_id, str) and turn_id:
        return turn_id
    metadata = payload.get("internal_chat_message_metadata_passthrough")
    if isinstance(metadata, dict):
        turn_id = metadata.get("turn_id")
        if isinstance(turn_id, str) and turn_id:
            return turn_id
    return None


def user_messages(records: list[dict[str, Any]]) -> list[tuple[str, str]]:
    messages: list[tuple[str, str]] = []
    for record in records:
        payload = record.get("payload")
        if (
            record.get("type") != "response_item"
            or not isinstance(payload, dict)
            or payload.get("type") != "message"
            or payload.get("role") != "user"
        ):
            continue
        turn_id = record_turn_id(record)
        if turn_id is None:
            continue
        text = "".join(
            str(part.get("text", ""))
            for part in payload.get("content") or []
            if isinstance(part, dict) and part.get("type") == "input_text"
        ).strip()
        if text:
            messages.append((turn_id, text))
    return messages


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


def _agent_message(record: dict[str, Any]) -> tuple[str, str] | None:
    if record.get("type") != "event_msg":
        return None
    payload = record.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "item_completed":
        return None
    item = payload.get("item")
    if not isinstance(item, dict):
        return None
    if item.get("type") != "AgentMessage":
        return None
    parts = [
        str(part.get("text", ""))
        for part in item.get("content") or []
        if isinstance(part, dict) and part.get("type") in (None, "Text")
    ]
    text = "".join(parts).strip()
    if not text:
        return None
    message_id = str(item.get("id") or sha256(text.encode()).hexdigest())
    return message_id, text


def final_answer(record: dict[str, Any]) -> str | None:
    payload = record.get("payload")
    item = payload.get("item") if isinstance(payload, dict) else None
    if not isinstance(item, dict) or item.get("phase") != "final_answer":
        return None
    message = _agent_message(record)
    return message[1] if message else None


def commentary_messages(
    records: list[dict[str, Any]],
    turn_id: str | None = None,
) -> list[tuple[str, str]]:
    messages: list[tuple[str, str]] = []
    for record in records:
        if turn_id is not None and record_turn_id(record) != turn_id:
            continue
        payload = record.get("payload")
        item = payload.get("item") if isinstance(payload, dict) else None
        if not isinstance(item, dict) or item.get("phase") != "commentary":
            continue
        message = _agent_message(record)
        if message is not None:
            messages.append(message)
    return messages
