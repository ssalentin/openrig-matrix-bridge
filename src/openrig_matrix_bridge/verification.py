from __future__ import annotations

from typing import Any

INTERRUPTED_REPLY_GRACE_SECONDS = 600
ACTIVE_PHASES = frozenset(
    {
        "starting",
        "awaiting_key",
        "awaiting_confirmation",
        "confirming",
        "cancelling",
        "awaiting_mac",
    }
)
TERMINAL_PHASES = frozenset(
    {
        "verified",
        "rejected",
        "cancelled",
        "failed",
        "timed_out",
        "stale_reply",
    }
)


def begin_verification(
    records: dict[str, dict[str, Any]],
    transaction_id: str,
    room_id: str,
    *,
    now: float,
) -> None:
    records[transaction_id] = {
        "room_id": room_id,
        "phase": "starting",
        "started_at": now,
        "updated_at": now,
    }


def mark_verification(
    records: dict[str, dict[str, Any]],
    transaction_id: str,
    phase: str,
    *,
    now: float,
) -> None:
    record = records[transaction_id]
    record["phase"] = phase
    record["updated_at"] = now
    if phase in TERMINAL_PHASES:
        record["terminal_at"] = now


def pending_verification(
    records: dict[str, dict[str, Any]],
    room_id: str,
    *,
    now: float,
) -> tuple[str, dict[str, Any]] | None:
    matches: list[tuple[str, dict[str, Any]]] = []
    for transaction_id, record in records.items():
        phase = str(record.get("phase") or "")
        if record.get("room_id") != room_id:
            continue
        if phase in ACTIVE_PHASES:
            matches.append((transaction_id, record))
            continue
        if phase == "interrupted":
            interrupted_at = float(record.get("interrupted_at") or record.get("updated_at") or 0)
            if now - interrupted_at <= INTERRUPTED_REPLY_GRACE_SECONDS:
                matches.append((transaction_id, record))
    if not matches:
        return None
    return max(matches, key=lambda item: float(item[1].get("updated_at") or 0))


def recover_interrupted_verifications(
    records: dict[str, dict[str, Any]],
    *,
    now: float,
) -> list[tuple[str, str]]:
    interrupted: list[tuple[str, str]] = []
    for transaction_id, record in records.items():
        if record.get("phase") not in ACTIVE_PHASES:
            continue
        room_id = str(record.get("room_id") or "")
        record.update(
            {
                "phase": "interrupted",
                "interrupted_at": now,
                "updated_at": now,
            }
        )
        interrupted.append((transaction_id, room_id))
    return interrupted


def expire_active_verifications(
    records: dict[str, dict[str, Any]],
    *,
    now: float,
    timeout: float,
) -> list[tuple[str, str]]:
    expired: list[tuple[str, str]] = []
    for transaction_id, record in records.items():
        if record.get("phase") not in ACTIVE_PHASES:
            continue
        updated_at = float(record.get("updated_at") or record.get("started_at") or 0)
        if now - updated_at <= timeout:
            continue
        room_id = str(record.get("room_id") or "")
        record.update(
            {
                "phase": "timed_out",
                "updated_at": now,
                "terminal_at": now,
            }
        )
        expired.append((transaction_id, room_id))
    return expired
