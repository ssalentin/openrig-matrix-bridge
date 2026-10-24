from __future__ import annotations

import json
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

