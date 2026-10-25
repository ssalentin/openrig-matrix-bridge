"""Group related owner messages into one conversation burst.

A burst is the set of Matrix events that belong to one exchange: a gallery and
the text written about it, a voice note followed by a photo, or every part sent
into one thread. Each event becomes a part of exactly one burst when the bridge
first sees it, in timeline order, before any download or transcription runs.

Correlation prefers explicit relations. A thread reply joins the burst its
thread root belongs to, and a reply joins the burst of the event it answers.
Unthreaded events join the sender's latest unthreaded burst in the same room
only while the gap to the previous part stays within the inactivity boundary,
the burst stays within its maximum span, and the new part or the previous part
carries an attachment. Two plain texts therefore stay separate.
"""

from __future__ import annotations

import uuid
from typing import Any

ATTACHMENT_KINDS = frozenset({"voice", "image", "file", "gallery"})
KIND_LABELS = {
    "text": "Text",
    "voice": "Sprachnachricht",
    "image": "Bild",
    "file": "Datei",
    "gallery": "Galerie",
}
RELATION_LABELS = {
    "thread": "Matrix-Thread",
    "reply": "Matrix-Antwortbezug",
    "time": "zeitliche Nähe",
    "other": "einzelnes Ereignis",
}
MAX_BURSTS = 500
SETTLED_PHASES = frozenset({"delivered", "failed", "reported"})


def relation_reference(relation: dict[str, Any] | None) -> tuple[str, str | None]:
    """Classify an m.relates_to block as thread, reply, other, or none."""
    if not isinstance(relation, dict):
        return "none", None
    target = relation.get("event_id")
    if relation.get("rel_type") == "m.thread" and isinstance(target, str):
        return "thread", target.strip() or None
    if relation.get("rel_type") is None:
        reply = relation.get("m.in_reply_to")
        if isinstance(reply, dict) and isinstance(reply.get("event_id"), str):
            return "reply", reply["event_id"]
    return "other", None


def burst_of_event(
    bursts: dict[str, dict[str, Any]],
    room_id: str,
    event_id: str | None,
) -> dict[str, Any] | None:
    if not event_id:
        return None
    for burst in bursts.values():
        if burst.get("room_id") == room_id and any(
            part.get("event_id") == event_id for part in burst.get("parts", [])
        ):
            return burst
    return None


def _latest(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    return max(candidates, key=lambda burst: int(burst["last_ts"]), default=None)


def assign_part(
    bursts: dict[str, dict[str, Any]],
    *,
    agent_key: str,
    room_id: str,
    sender: str,
    event_id: str,
    kind: str,
    server_ts: int,
    relation: dict[str, Any] | None,
    thread_root_event_id: str | None,
    reply_to_event_id: str,
    inactivity_seconds: float,
    max_span_seconds: float,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Add one event to the burst it belongs to and return (burst, part).

    `thread_root_event_id` and `reply_to_event_id` are the response target of
    this event; a new burst keeps them so every answer lands where the first
    part's answer would.
    """
    inactivity_ms = max(0.0, inactivity_seconds) * 1000
    max_span_ms = max(0.0, max_span_seconds) * 1000
    reference_kind, reference = relation_reference(relation)
    candidate: dict[str, Any] | None = None
    if reference_kind in {"thread", "reply"}:
        candidate = burst_of_event(bursts, room_id, reference)
        if candidate is None and reference_kind == "thread":
            candidate = _latest(
                [
                    burst
                    for burst in bursts.values()
                    if burst.get("room_id") == room_id
                    and burst.get("relation") == "thread"
                    and burst.get("anchor_event_id") == reference
                ]
            )
        if candidate is not None and (
            candidate.get("agent_key") != agent_key
            or server_ts - int(candidate["started_ts"]) > max_span_ms
        ):
            candidate = None
    elif reference_kind == "none" and inactivity_ms > 0:
        candidate = _latest(
            [
                burst
                for burst in bursts.values()
                if burst.get("relation") == "time"
                and burst.get("agent_key") == agent_key
                and burst.get("room_id") == room_id
                and burst.get("sender") == sender
            ]
        )
        if candidate is not None:
            previous = candidate["parts"][-1]
            if not (
                abs(server_ts - int(previous["server_ts"])) <= inactivity_ms
                and server_ts - int(candidate["started_ts"]) <= max_span_ms
                and (
                    kind in ATTACHMENT_KINDS
                    or previous.get("kind") in ATTACHMENT_KINDS
                )
            ):
                candidate = None

    part = {
        "event_id": event_id,
        "kind": kind,
        "server_ts": int(server_ts),
        "phase": "processing",
    }
    if candidate is None:
        # Only "time" bursts accept unthreaded parts by timing.
        relation_kind = {"none": "time"}.get(reference_kind, reference_kind)
        candidate = {
            "id": uuid.uuid4().hex,
            "agent_key": agent_key,
            "room_id": room_id,
            "sender": sender,
            "relation": relation_kind,
            "anchor_event_id": (
                reference if reference_kind == "thread" and reference else event_id
            ),
            "thread_root_event_id": thread_root_event_id,
            "reply_to_event_id": reply_to_event_id,
            "started_ts": int(server_ts),
            "last_ts": int(server_ts),
            "parts": [],
        }
        bursts[candidate["id"]] = candidate
    candidate["parts"].append(part)
    candidate["last_ts"] = max(int(candidate["last_ts"]), int(server_ts))
    prune_bursts(bursts, keep=candidate["id"])
    return candidate, part


def prune_bursts(bursts: dict[str, dict[str, Any]], *, keep: str | None = None) -> None:
    """Forget the oldest fully settled bursts beyond MAX_BURSTS."""
    if len(bursts) <= MAX_BURSTS:
        return
    settled = sorted(
        (
            burst
            for burst in bursts.values()
            if burst["id"] != keep
            and all(part.get("phase") in SETTLED_PHASES for part in burst["parts"])
        ),
        key=lambda burst: int(burst["last_ts"]),
    )
    for burst in settled[: len(bursts) - MAX_BURSTS]:
        del bursts[burst["id"]]


def is_linked(burst: dict[str, Any]) -> bool:
    """A burst with an attachment-bearing part is delivered as one linked context.

    Text-only bursts (plain chat and plain thread replies) keep the existing
    per-message delivery.
    """
    return any(part.get("kind") in ATTACHMENT_KINDS for part in burst["parts"])


def deliverable_parts(burst: dict[str, Any]) -> list[dict[str, Any]]:
    """Settled-but-undelivered parts, in order, up to the first part in progress.

    A later part never overtakes an earlier part of the same burst that is
    still downloading or being transcribed.
    """
    parts: list[dict[str, Any]] = []
    for part in burst["parts"]:
        phase = part.get("phase")
        if phase == "processing":
            break
        if phase in {"ready", "failed"}:
            parts.append(part)
    return parts


def render_burst(
    burst: dict[str, Any],
    new_event_ids: list[str],
    *,
    inactivity_seconds: float,
) -> str:
    """One prompt listing every part up to the newest new part, in source order.

    Parts outside `new_event_ids` were delivered before and are restated so the
    agent always sees the complete related context.
    """
    new = set(new_event_ids)
    last = max(
        index
        for index, part in enumerate(burst["parts"])
        if part["event_id"] in new
    )
    parts = [
        part
        for part in burst["parts"][: last + 1]
        if part.get("phase") != "processing"
    ]
    numbers = [
        index
        for index, part in enumerate(parts, start=1)
        if part["event_id"] in new
    ]
    relation = RELATION_LABELS.get(str(burst.get("relation")), "Bezug")
    if burst.get("relation") == "time":
        relation += f" (höchstens {inactivity_seconds:g} s Pause)"
    earlier = len(parts) - len(numbers)
    lines = [
        (
            f"Zusammenhängende Matrix-Nachrichten von {burst['sender']}: "
            f"{len(parts)} Teile in Originalreihenfolge, neu: "
            + ", ".join(str(number) for number in numbers)
            + (
                f"; {earlier} bereits zugestellte Teile sind zur Vollständigkeit "
                "wiederholt."
                if earlier
                else "."
            )
        ),
        f"Unterhaltung ab Ereignis {burst['anchor_event_id']}; Zuordnung: {relation}.",
        "Quellereignisse: " + ", ".join(part["event_id"] for part in parts),
    ]
    blocks = ["\n".join(lines)]
    for index, part in enumerate(parts, start=1):
        title = (
            f"Teil {index} · {KIND_LABELS.get(part['kind'], part['kind'])} · "
            f"{part['event_id']}"
        )
        if part["event_id"] not in new:
            title += " · bereits zugestellt"
        if part.get("phase") == "failed" or (
            part.get("phase") == "reported" and part.get("error")
        ):
            blocks.append(f"{title}\nNicht übernommen: {part.get('error')}")
        else:
            blocks.append(f"{title}\n{part.get('text') or ''}")
    return "\n\n".join(blocks)
