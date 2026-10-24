from __future__ import annotations

from typing import Any

TERMINAL_REPLY_GRACE_SECONDS = 600
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
        "superseded",
    }
)
INTERRUPTED_NOTIFICATION_TEXT = (
    "Die Geräteverifikation wurde durch einen Neustart unterbrochen. "
    "Bitte starte sie im Matrix-Client erneut."
)
TIMED_OUT_NOTIFICATION_TEXT = (
    "Zeitüberschreitung bei der Geräteverifikation. "
    "Bitte starte sie erneut."
)


def _queue_terminal_notification(
    record: dict[str, Any],
    transaction_id: str,
    kind: str,
    *,
    now: float,
) -> None:
    existing = record.get("terminal_notification")
    if isinstance(existing, dict):
        return
    text = (
        INTERRUPTED_NOTIFICATION_TEXT
        if kind == "interrupted"
        else TIMED_OUT_NOTIFICATION_TEXT
    )
    record["terminal_notification"] = {
        "kind": kind,
        "room_id": str(record.get("room_id") or ""),
        "text": text,
        "tx_id": f"verification-{transaction_id}-{kind}",
        "state": "pending",
        "queued_at": now,
        "event_id": None,
        "delivered_at": None,
    }


def begin_verification(
    records: dict[str, dict[str, Any]],
    transaction_id: str,
    room_id: str,
    *,
    now: float,
) -> None:
    for existing_id, existing in records.items():
        if (
            existing_id != transaction_id
            and existing.get("room_id") == room_id
            and existing.get("phase") == "interrupted"
        ):
            existing["phase"] = "superseded"
            existing["updated_at"] = now
            existing["terminal_at"] = now
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
            matches.append((transaction_id, record))
            continue
        if phase in TERMINAL_PHASES:
            terminal_at = float(record.get("terminal_at") or record.get("updated_at") or 0)
            if now - terminal_at <= TERMINAL_REPLY_GRACE_SECONDS:
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
        phase = record.get("phase")
        if phase == "interrupted":
            if not isinstance(record.get("terminal_notification"), dict):
                _queue_terminal_notification(
                    record,
                    transaction_id,
                    "interrupted",
                    now=now,
                )
                interrupted.append((transaction_id, str(record.get("room_id") or "")))
            continue
        if phase == "timed_out":
            if not isinstance(record.get("terminal_notification"), dict):
                _queue_terminal_notification(
                    record,
                    transaction_id,
                    "timed_out",
                    now=now,
                )
                interrupted.append((transaction_id, str(record.get("room_id") or "")))
            continue
        if phase not in ACTIVE_PHASES:
            continue
        room_id = str(record.get("room_id") or "")
        record.update(
            {
                "phase": "interrupted",
                "interrupted_at": now,
                "updated_at": now,
            }
        )
        _queue_terminal_notification(
            record,
            transaction_id,
            "interrupted",
            now=now,
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
        _queue_terminal_notification(
            record,
            transaction_id,
            "timed_out",
            now=now,
        )
        expired.append((transaction_id, room_id))
    return expired


def prune_terminal_verifications(
    records: dict[str, dict[str, Any]],
    *,
    now: float,
    retention: float,
) -> list[str]:
    pruned: list[str] = []
    for transaction_id, record in list(records.items()):
        phase = record.get("phase")
        if phase not in TERMINAL_PHASES:
            continue
        notification = record.get("terminal_notification")
        if isinstance(notification, dict) and notification.get("state") == "pending":
            continue
        terminal_at = float(record.get("terminal_at") or record.get("updated_at") or 0)
        if now - terminal_at <= retention:
            continue
        records.pop(transaction_id, None)
        pruned.append(transaction_id)
    return pruned
