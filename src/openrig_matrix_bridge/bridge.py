from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import signal
import stat
import tempfile
import time
import uuid
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from nio import (
    AsyncClient,
    AsyncClientConfig,
    InviteMemberEvent,
    KeyVerificationCancel,
    KeyVerificationEvent,
    KeyVerificationKey,
    KeyVerificationMac,
    KeyVerificationStart,
    MatrixRoom,
    ProfileSetAvatarResponse,
    ReactionEvent,
    RoomCreateResponse,
    RoomEncryptedAudio,
    RoomEncryptedFile,
    RoomEncryptedImage,
    RoomGetEventResponse,
    RoomGetStateEventResponse,
    RoomMemberEvent,
    RoomMessageAudio,
    RoomMessageFile,
    RoomMessageImage,
    RoomMessageText,
    RoomMessageUnknown,
    RoomSendResponse,
    SyncResponse,
    ToDeviceError,
    UnknownEvent,
    UploadResponse,
)
from nio.api import RoomPreset, RoomVisibility
from nio.crypto import decrypt_attachment

from .avatar_pool import (
    assign_unique_avatar_ids,
    avatar_png,
    avatar_pool_fingerprint,
    profile_png,
)
from .bursts import (
    KIND_LABELS,
    assign_part,
    deliverable_parts,
    is_linked,
    render_burst,
)
from .config import AgentConfig, BridgeConfig, safe_agent_directory
from .decisions import (
    POLL_RESPONSE_TYPE,
    POLL_START_TYPE,
    STABLE_POLL_RESPONSE_TYPE,
    DecisionError,
    normalize_questions,
    poll_response,
    poll_start_content,
)
from .formatting import markdown_to_matrix_html
from .media import (
    MediaError,
    detect_image_file,
    detect_image_type,
    encrypted_file_content,
    encrypted_image_content,
    is_voice_message_content,
    normalize_mime_type,
)
from .openrig import (
    AgentSessionNotFound,
    AgentSessionRef,
    GatewayError,
    OpenRigGateway,
    OpenRigSessionResolver,
)
from .transcript import (
    commentary_messages,
    final_answer,
    read_complete_records,
    record_turn_id,
    user_messages,
)
from .transcription import Transcriber
from .verification import (
    TERMINAL_PHASES,
    VERIFICATION_RECORD_RETENTION_SECONDS,
    begin_verification,
    expire_active_verifications,
    mark_verification,
    pending_verification,
    prune_terminal_verifications,
    recover_interrupted_verifications,
)

LOGGER = logging.getLogger("openrig-matrix-bridge")
AGENT_STATUS_EVENT = "dev.openrig.agent_status"
VERIFICATION_TIMEOUT_SECONDS = 600
_DEFAULT_THREAD_ROOT = object()
MAX_DELIVERY_ATTEMPTS = 3
ACCEPTANCE_REACTION_KEY = "👍"
MAX_ACCEPTANCE_RECORDS = 2000
MAX_ACCEPTANCE_ATTEMPTS = 8
ACCEPTANCE_RETRY_BASE_SECONDS = 5.0
ACCEPTANCE_RETRY_MAX_SECONDS = 300.0
MAX_LATENCY_SAMPLES = 200
TERMINAL_QITEM_PHASES = {"done", "failed", "canceled", "denied"}
VOICE_STATUS_PROGRESS = "Sprachnachricht empfangen, transkribiere …"
VOICE_STATUS_INTERRUPTED = (
    "Die Sprachnachricht wurde durch einen Bridge-Neustart unterbrochen "
    "und konnte nicht abgeschlossen werden. Bitte erneut senden."
)
STATUS_LABELS = {
    "ready": "🟢",
    "busy": "🟡",
    "offline": "⚫",
    "error": "🟠",
}
FENCED_OUTPUT = re.compile(
    r"(?ms)^[ \t]*(?:`{3,}|~{3,}).*?"
    r"(?:^[ \t]*(?:`{3,}|~{3,})[ \t]*$|\Z)"
)
# A header or quoted value is redacted to the end of its line (and any folded
# continuation lines) instead of being parsed, so no quote, escape or separator
# inside it can leave part of the secret behind.
_TO_LINE_END = r"[^\r\n]*(?:\r?\n[ \t]+[^\r\n]*)*"
CREDENTIAL_VALUE = re.compile(
    r"(?i)\b(?:[a-z0-9_]*(?:token|key|secret|password)|authorization)\b"
    r"\s*[:=]\s*(?:[\"']" + _TO_LINE_END + r"|\S+)"
)
URI_USERINFO = re.compile(
    r"(?i)\b[a-z][a-z0-9+.-]*://[^\s/:]*:[^\s/]+@"
)
AUTHORIZATION_SCHEME = re.compile(
    r"(?i)\b(?:proxy-)?authorization\b[^\r\n]{0,40}"
    r"\b(?:basic|bearer|digest|token)\s+" + _TO_LINE_END
)
COOKIE_VALUE = re.compile(
    r"(?i)\b(?:set-)?cookie\b(?:\s*[:=]\s*|\s+)" + _TO_LINE_END
)
BARE_TOKEN = re.compile(
    r"(?i)\b(?:"
    r"sk-[a-z0-9_-]{8,}|"
    r"gh[pousr]_[a-z0-9]{8,}|"
    r"akia[0-9a-z]{16}|"
    r"eyj[a-z0-9_-]+\.[a-z0-9_-]+\.[a-z0-9_-]+"
    r")\b"
)
LABELED_SECRET = re.compile(
    r"(?i)\b(?:token|api[_ -]?key|client[_ -]?secret|password|secret)\b"
    r"\s+(?:is\s+|:\s*)?[a-z0-9_./+=-]{8,}"
)
LONG_SECRET = re.compile(
    r"\b(?=[a-z0-9_-]{32,}\b)(?=[a-z0-9_-]*[a-z])(?=[a-z0-9_-]*\d)"
    r"[a-z0-9_-]+\b",
    re.IGNORECASE,
)
PRIVATE_PATH = re.compile(
    r"(?<![\w])"
    r"(?:/(?:home|Users|root|tmp|var|opt|etc|usr|private|workspace|srv|mnt)/"
    r"[^\s`'\"<>]+|(?:\.\.?|~)/[^\s`'\"<>]+|"
    r"[A-Za-z]:\\[^\s`'\"<>]+|"
    r"(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+)"
)


def activity_card_label(activity_state: str, needs_input_count: int) -> str:
    if needs_input_count > 0:
        return "Wartet auf Eingabe"
    if activity_state == "idle-at-prompt":
        return "Bereit"
    return "Arbeitet"


REDACTION_MARKERS = {
    "output": "[Ausgabe entfernt]",
    "credentials": "[Zugangsdaten entfernt]",
    "token": "[Token entfernt]",
    "cookie": "[Cookie entfernt]",
    "path": "[lokaler Pfad]",
}
# Most specific first: a fenced block or a URI must not be read as a path.
REDACTION_PATTERNS = (
    (FENCED_OUTPUT, "output"),
    (URI_USERINFO, "credentials"),
    (AUTHORIZATION_SCHEME, "token"),
    (COOKIE_VALUE, "cookie"),
    (CREDENTIAL_VALUE, "token"),
    (BARE_TOKEN, "token"),
    (LABELED_SECRET, "token"),
    (LONG_SECRET, "token"),
    (PRIVATE_PATH, "path"),
)
_REDACTION_SLOT = re.compile("\x00(\\w+)\x00")
_PATH_TRAILING_PUNCTUATION = ".,;:!?)"


def _redaction_slot(category: str, match: re.Match[str]) -> str:
    """A placeholder later patterns cannot match; markers are inserted last."""
    slot = f"\x00{category}\x00"
    if category == "credentials":
        # Keep the scheme so the reader still sees what kind of connection it is.
        scheme = match.group(0).split("://", 1)[0]
        return f"{scheme}://{slot}@"
    if category == "path":
        span = match.group(0)
        kept = len(span) - len(span.rstrip(_PATH_TRAILING_PUNCTUATION))
        return slot + (span[len(span) - kept :] if kept else "")
    return slot


def sanitize_activity_summary(text: str) -> str:
    """Replace each sensitive span with a category marker; keep the rest.

    Fenced output, credentials, tokens, cookies and local paths are redacted
    span by span, so safe progress text stays readable and no matched byte
    survives. Whitespace is normalized and the result is bounded.
    """
    # NUL is reserved for the placeholders below.
    summary = text.replace("\x00", " ")
    for pattern, category in REDACTION_PATTERNS:
        summary = pattern.sub(
            lambda match, category=category: _redaction_slot(category, match),
            summary,
        )
    summary = " ".join(summary.split())
    summary = _REDACTION_SLOT.sub(
        lambda match: REDACTION_MARKERS[match.group(1)],
        summary,
    )
    return bounded_summary(summary, 600, keep=597)


def bounded_summary(text: str, limit: int, *, keep: int | None = None) -> str:
    """Cut `text` longer than `limit` to `keep` characters plus an ellipsis.

    The cut never leaves half of a redaction marker behind.
    """
    if len(text) <= limit:
        return text
    cut = text[: limit - 1 if keep is None else keep]
    opening = cut.rfind("[")
    if opening > cut.rfind("]"):
        cut = cut[:opening]
    return cut.rstrip() + "…"


def activity_card_text(request: dict[str, Any]) -> str:
    label = str(
        request.get("activity_label")
        or activity_card_label(
            str(request.get("activity_state") or "unknown"),
            int(request.get("needs_input_count") or 0),
        )
    )
    summary = str(request.get("commentary_summary") or "").strip()
    text = f"**Aktivität:** {label}"
    if summary:
        text += f"\n\n{summary}"
    return text


QUEUE_SOURCE_LABELS = {
    "message": "Text",
    "attachment": "Datei",
    "image": "Bild",
    "audio": "Audio",
    "reaction": "Reaktion",
    "decision": "Entscheidung",
    "gallery": "Galerie",
    "related": "Zusammenhang",
}
QUEUE_FILENAME_LINE = re.compile(r"^Originaldateiname: (\".*\")$", re.MULTILINE)
QUEUE_CAPTION_LINE = re.compile(r"^Bildunterschrift: (.+)$", re.MULTILINE)
QUEUE_GALLERY_ITEM_LINE = re.compile(r"^\d+\. \".*\"", re.MULTILINE)
QUEUE_RELATED_PART_LINE = re.compile(r"^Teil \d+ · ([^·\n]+?) · ", re.MULTILINE)


def queue_preview(text: object, limit: int = 96) -> str:
    return bounded_summary(sanitize_activity_summary(str(text or "")), limit)


def queue_related_preview(text: str) -> str | None:
    """Summarize a related-context prompt by its parts, never its paths or IDs."""
    kinds = QUEUE_RELATED_PART_LINE.findall(text)
    if not kinds:
        return None
    count = f"{len(kinds)} {'Teil' if len(kinds) == 1 else 'Teile'}"
    return queue_preview(f"{count} · " + ", ".join(kind.strip() for kind in kinds))


def queue_request_preview(request: dict[str, Any]) -> str:
    """Return a safe one-line description of what a queued request contains.

    File, image and gallery prompts embed local inbox paths for the agent, so their
    previews are built from the original filename or caption, never the prompt.
    """
    text = str(request.get("text") or "")
    label = str(request.get("source_label") or "message")
    caption_match = QUEUE_CAPTION_LINE.search(text)
    caption = caption_match.group(1) if caption_match else ""
    if label == "attachment":
        match = QUEUE_FILENAME_LINE.search(text)
        if match:
            try:
                return queue_preview(json.loads(match.group(1)))
            except ValueError:
                pass
        return ""
    if label == "image":
        return queue_preview(caption)
    if label == "related":
        return queue_related_preview(text) or ""
    if label == "gallery":
        count = len(QUEUE_GALLERY_ITEM_LINE.findall(text))
        parts = [f"{count} {'Datei' if count == 1 else 'Dateien'}"] if count else []
        if caption:
            parts.append(caption)
        return queue_preview(" · ".join(parts))
    return queue_preview(text)


def queue_request_context(request: dict[str, Any]) -> str:
    source_label = str(request.get("source_label") or "message")
    source = QUEUE_SOURCE_LABELS.get(source_label, source_label)
    event_id = str(request.get("event_id") or "")
    thread_root = str(request.get("thread_root_event_id") or "")
    if thread_root and thread_root != event_id:
        return f"{source} · Thread"
    return source


def queue_request_entry(request: dict[str, Any]) -> str:
    steering_entries = request.get("steering_entries")
    if isinstance(steering_entries, list) and steering_entries:
        texts = [
            str(entry.get("text") or "")
            for entry in steering_entries
            if isinstance(entry, dict)
        ]
        preview = queue_preview(
            "; ".join(queue_related_preview(text) or text for text in texts)
        )
    else:
        preview = queue_request_preview(request)
    context = queue_request_context(request)
    return f'{context} · "{preview}"' if preview else context


def queue_notice_text(
    pending: list[dict[str, Any]],
    *,
    priority_label: str = "hoch",
) -> str | None:
    visible = [
        request
        for request in pending
        if not request.get("suppress_reply")
    ]
    if not visible:
        return None
    first = visible[0]
    active = first if first.get("started_at") is not None else None
    steering_group = None
    if active is not None:
        group_id = active.get("steering_group_id")
        if group_id:
            steering_group = next(
                (
                    request
                    for request in visible[1:]
                    if request.get("id") == group_id
                    and request.get("deferred_steering")
                    and request.get("injection_phase") != "fallback"
                ),
                None,
            )
    waiting = (
        [
            request
            for request in visible[1:]
            if request is not steering_group
        ]
        if active is not None
        else visible
    )
    noun = "Nachricht wartet" if len(waiting) == 1 else "Nachrichten warten"
    source_ids = []
    if active is not None:
        active_sources = active.get("source_event_ids")
        if isinstance(active_sources, list):
            source_ids.extend(active_sources)
    if steering_group is not None:
        steering_sources = steering_group.get("source_event_ids")
        if isinstance(steering_sources, list):
            source_ids.extend(steering_sources)
    source_count = len(set(source_ids)) if isinstance(source_ids, list) else 1
    active_label = (
        f"Aktiv ({source_count} Eingaben)"
        if active is not None and source_count > 1
        else "Aktiv"
    )
    lines = [
        (
            f"⏳ **Warteschlange:** {len(waiting)} {noun} · "
            f"Priorität {priority_label}."
        ),
        "",
    ]
    if active is not None:
        lines.append(f"**{active_label}:** {queue_request_entry(active)}")
    else:
        lines.append(f"**Nächste Anfrage:** {queue_request_entry(first)}")
        waiting = waiting[1:]
    if waiting:
        lines.extend(
            f"{position}. {queue_request_entry(request)}"
            for position, request in enumerate(waiting, start=1)
        )
    elif active is not None:
        lines.append("Keine weiteren Nachrichten warten.")
    return "\n".join(lines)


class QitemLifecycleError(GatewayError):
    pass


def _has_complete_encryption_metadata(encryption: object) -> bool:
    if not isinstance(encryption, dict):
        return False
    key = encryption.get("key")
    hashes = encryption.get("hashes")
    return (
        isinstance(key, dict)
        and isinstance(key.get("k"), str)
        and bool(key["k"])
        and isinstance(hashes, dict)
        and isinstance(hashes.get("sha256"), str)
        and bool(hashes["sha256"])
        and isinstance(encryption.get("iv"), str)
        and bool(encryption["iv"])
    )


def _open_verified_snapshot(
    path: Path,
    *,
    expected_size: int,
    expected_sha256: str,
    max_file_bytes: int,
) -> Any:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise MediaError("file snapshot is not a regular file")
        if metadata.st_size != expected_size:
            raise MediaError(
                "file snapshot size changed: "
                f"{metadata.st_size} instead of {expected_size} bytes"
            )
        if metadata.st_size <= 0 or metadata.st_size > max_file_bytes:
            raise MediaError(f"file size must be between 1 and {max_file_bytes} bytes")
        handle = os.fdopen(descriptor, "rb")
        descriptor = -1
    finally:
        if descriptor >= 0:
            os.close(descriptor)

    try:
        digest = hashlib.sha256()
        size = 0
        while chunk := handle.read(1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
        if size != expected_size or digest.hexdigest() != expected_sha256:
            raise MediaError("file snapshot digest does not match admission metadata")
        handle.seek(0)
        return handle
    except Exception:
        handle.close()
        raise


def room_name(agent: AgentConfig) -> str:
    return agent.display_name


@dataclass(frozen=True)
class ResponseTarget:
    thread_root_event_id: str | None
    reply_to_event_id: str


def resolve_response_target(
    event_id: str,
    relates_to: dict[str, Any] | None,
) -> ResponseTarget:
    if isinstance(relates_to, dict):
        thread_root = relates_to.get("event_id")
        if relates_to.get("rel_type") == "m.thread" and isinstance(thread_root, str):
            thread_root = thread_root.strip()
            if thread_root:
                return ResponseTarget(thread_root, thread_root)
        return ResponseTarget(None, event_id)
    return ResponseTarget(event_id, event_id)


def message_relation(event: Any) -> dict[str, Any] | None:
    source = getattr(event, "source", {})
    if not isinstance(source, dict):
        return None
    content = source.get("content", {})
    if not isinstance(content, dict):
        return None
    relation = content.get("m.relates_to")
    return relation if isinstance(relation, dict) else None


GALLERY_MSGTYPES = {"dm.filament.gallery", "m.gallery"}
GALLERY_FILE_ITEMTYPES = {"m.image", "m.file", "m.video", "m.audio"}
# MSC4274: the 64 KiB event limit caps a gallery at about 60 items.
MAX_GALLERY_ITEMS = 60
_UNSAFE_DISPLAY = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]+")


def display_text(value: object, limit: int = 120) -> str:
    """Render sender-controlled text as one bounded line."""
    text = _UNSAFE_DISPLAY.sub(" ", str(value or "")).strip()
    return f"{text[:limit]}…" if len(text) > limit else text


def elements_dative(count: int) -> str:
    return f"{count} Element" if count == 1 else f"{count} Elementen"


def elements_nominative(count: int) -> str:
    return f"{count} Element" if count == 1 else f"{count} Elemente"


def _media_source(container: dict[str, Any], file_key: str, url_key: str) -> dict:
    encrypted = container.get(file_key)
    if isinstance(encrypted, dict):
        return dict(encrypted)
    url = container.get(url_key)
    return {"url": url} if isinstance(url, str) else {}


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def gallery_items(content: dict[str, Any]) -> list[dict[str, Any]]:
    """Persistable per-item records for a gallery, in source order."""
    raw_items = content.get("itemtypes")
    if not isinstance(raw_items, list):
        return []
    items: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_items, start=1):
        if not isinstance(raw, dict):
            items.append(
                {
                    "index": index,
                    "itemtype": "",
                    "filename": f"Element {index}",
                    "phase": "failed",
                    "error": "ungültiger Galerieeintrag",
                }
            )
            continue
        info = raw.get("info") if isinstance(raw.get("info"), dict) else {}
        item: dict[str, Any] = {
            "index": index,
            "itemtype": display_text(raw.get("itemtype"), 40),
            "filename": display_text(
                raw.get("filename") or raw.get("body") or f"Element {index}"
            ),
            "mimetype": normalize_mime_type(info.get("mimetype")),
            "declared_size": _positive_int(info.get("size")),
            "width": _positive_int(info.get("w")),
            "height": _positive_int(info.get("h")),
            "source": _media_source(raw, "file", "url"),
            "thumbnail_source": _media_source(info, "thumbnail_file", "thumbnail_url"),
            "phase": "pending",
        }
        if index > MAX_GALLERY_ITEMS:
            item.update(
                phase="failed",
                error=f"Galerie überschreitet {MAX_GALLERY_ITEMS} Elemente",
            )
        elif item["itemtype"] not in GALLERY_FILE_ITEMTYPES:
            item.update(
                phase="failed",
                error=f"Elementtyp {item['itemtype'] or '?'} wird nicht unterstützt",
            )
        if item["phase"] == "failed":
            item.pop("source")
            item.pop("thumbnail_source")
        items.append(item)
    return items


def media_stem(event_id: str) -> str:
    """Inbox file stem for one Matrix media event, stable across restarts."""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"matrix-media:{event_id}").hex


def voice_transaction_id(voice_event_id: str, operation: str) -> str:
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"openrig-matrix-bridge:{voice_event_id}:{operation}",
        )
    )


def formatted_message_content(text: str) -> dict[str, Any]:
    content: dict[str, Any] = {"msgtype": "m.text", "body": text[:60000]}
    formatted_body = markdown_to_matrix_html(text[:60000])
    if formatted_body:
        content["format"] = "org.matrix.custom.html"
        content["formatted_body"] = formatted_body
    return content


def steering_batch_text(thread_root: str, entries: list[dict[str, Any]]) -> str:
    lines = [
        (
            "Matrix follow-up batch for this thread. Treat every numbered item below "
            "as ordered input to the same task."
        ),
        f"Canonical thread root: {thread_root}",
        "Source events: " + ", ".join(entry["event_id"] for entry in entries),
        "",
    ]
    for index, entry in enumerate(entries, start=1):
        lines.append(f"{index}. [{entry['event_id']}] {entry['text']}")
    return "\n".join(lines)


def verification_decision(text: str) -> bool | None:
    normalized = text.strip().lower().rstrip(".!")
    if normalized in {"ja", "ja bitte", "bestätigen", "bestaetigen", "stimmt"}:
        return True
    if normalized in {"nein", "ablehnen", "stimmt nicht"}:
        return False
    return None


class StateStore:
    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, Any] = {
            "version": 1,
            "rooms": {},
            "transcripts": {},
            "pending": {},
            "processed_events": [],
            "voice_statuses": {},
            "welcomed": {},
            "decisions": {},
            "media": {},
            "media_failures": {},
            "messages": {},
            "queue_notices": {},
            "queue_mirror": {"initialized": False, "seen": {}},
            "rejected_rooms": {},
            "avatar_pool": {
                "asset_fingerprint": None,
                "assignments": {},
                "uploads": {},
                "profile_mxc": None,
            },
        }
        if path.exists():
            self.data.update(json.loads(path.read_text(encoding="utf-8")))

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", dir=self.path.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self.data, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass


class MatrixBridge:
    def __init__(self, config: BridgeConfig, access_token: str):
        if not access_token:
            raise ValueError("MATRIX_ACCESS_TOKEN is required")
        self.config = config
        self.config.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        (self.config.state_dir / "matrix-store").mkdir(
            parents=True, exist_ok=True, mode=0o700
        )
        self.state = StateStore(config.state_dir / "state.json")
        self.resolver = OpenRigSessionResolver(config.rig_command, config.codex_home)
        self.gateway = OpenRigGateway(config.rig_command, config.sender_session)
        self.transcriber = Transcriber(config.transcription)
        self.locks = {key: asyncio.Lock() for key in config.agents}
        self.room_agents: dict[str, str] = {}
        self.processed_events: set[str] = set(
            self.state.data.get("processed_events", [])
        )
        self.verification_records = self.state.data.setdefault("verifications", {})
        self.typing_tasks: dict[str, asyncio.Task[None]] = {}
        self.pin_locks = {
            str(room["room_id"]): asyncio.Lock()
            for room in self.state.data["rooms"].values()
            if room.get("room_id")
        }
        self.agent_statuses: dict[str, str] = {}
        self.decision_inbox = config.state_dir / "decision-inbox"
        self.decision_sent = self.decision_inbox / "sent"
        self.decision_failed = self.decision_inbox / "failed"
        self.decision_inbox.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.decision_sent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.decision_failed.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.media_outbox = config.state_dir / "media-outbox"
        self.media_sent = self.media_outbox / "sent"
        self.media_failed = self.media_outbox / "failed"
        self.media_outbox.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.media_sent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.media_failed.mkdir(parents=True, exist_ok=True, mode=0o700)
        config.media.inbox_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.message_outbox = config.state_dir / "message-outbox"
        self.message_sent = self.message_outbox / "sent"
        self.message_failed = self.message_outbox / "failed"
        self.message_outbox.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.message_sent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.message_failed.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.client = AsyncClient(
            config.homeserver,
            config.user_id,
            device_id=config.device_id,
            store_path=str(config.state_dir / "matrix-store"),
            config=AsyncClientConfig(
                encryption_enabled=True,
                store_sync_tokens=True,
                store_name="matrix-store",
            ),
        )
        self.client.restore_login(
            user_id=config.user_id,
            device_id=config.device_id,
            access_token=access_token,
        )
        self.client.add_event_callback(self._on_room_message, RoomMessageText)
        self.client.add_event_callback(self._on_reaction, ReactionEvent)
        self.client.add_event_callback(
            self._on_audio, (RoomMessageAudio, RoomEncryptedAudio)
        )
        self.client.add_event_callback(
            self._on_image, (RoomMessageImage, RoomEncryptedImage)
        )
        self.client.add_event_callback(
            self._on_file, (RoomMessageFile, RoomEncryptedFile)
        )
        # Encrypted and plain messages with an unknown msgtype both arrive as
        # RoomMessageUnknown; Element X galleries are one of them.
        self.client.add_event_callback(
            self._on_room_message_unknown, RoomMessageUnknown
        )
        self.client.add_event_callback(self._on_invite, InviteMemberEvent)
        self.client.add_event_callback(self._on_member, RoomMemberEvent)
        self.client.add_event_callback(self._on_unknown_event, UnknownEvent)
        self.client.add_to_device_callback(
            self._on_verification, (KeyVerificationEvent,)
        )

    async def run(self) -> None:
        await self._initial_sync()
        await self._recover_verifications()
        self._load_room_index()
        await self._recover_voice_statuses()
        await self._reconcile_room_memberships()
        if any(key not in self.state.data["rooms"] for key in self.config.agents):
            await self._create_rooms()
        await self._restore_room_names()
        await self._ensure_room_avatars()
        self._initialize_transcript_offsets()
        self._repair_decision_reply_targets()
        await self._reconcile_pending_operations()
        await self._resume_galleries()
        await self._resume_bursts()
        await self._recover_acceptance_reactions(startup=True)
        await self._restore_queue_notices()
        await self._restore_activity_feedback()
        poller = asyncio.create_task(self._poll_answers(), name="openrig-matrix-poller")
        status_poller = asyncio.create_task(
            self._poll_agent_statuses(),
            name="openrig-matrix-status",
        )
        decision_poller = asyncio.create_task(
            self._poll_decisions(),
            name="openrig-matrix-decisions",
        )
        media_poller = asyncio.create_task(
            self._poll_media_outbox(),
            name="openrig-matrix-media",
        )
        message_poller = asyncio.create_task(
            self._poll_message_outbox(),
            name="openrig-matrix-messages",
        )
        human_queue_poller = asyncio.create_task(
            self._poll_human_queue(),
            name="openrig-matrix-human-queue",
        )
        verification_poller = asyncio.create_task(
            self._poll_verification_timeouts(),
            name="openrig-matrix-verification-timeouts",
        )
        # systemd stops the service with SIGTERM. Without a handler the
        # process dies before the cleanup below, leaving Matrix typing on
        # until its server-side timeout.
        loop = asyncio.get_running_loop()
        main_task = asyncio.current_task()
        stop_requested = False

        def request_stop() -> None:
            nonlocal stop_requested
            stop_requested = True
            if main_task is not None:
                main_task.cancel()

        try:
            loop.add_signal_handler(signal.SIGTERM, request_stop)
        except (NotImplementedError, RuntimeError):
            LOGGER.debug("SIGTERM handler unavailable", exc_info=True)
        try:
            await self.client.sync_forever(
                timeout=30000,
                full_state=True,
                set_presence="online",
            )
        except asyncio.CancelledError:
            if not stop_requested:
                raise
            if main_task is not None:
                main_task.uncancel()
            LOGGER.info("stopping on SIGTERM")
        finally:
            try:
                loop.remove_signal_handler(signal.SIGTERM)
            except (NotImplementedError, RuntimeError):
                pass
            poller.cancel()
            status_poller.cancel()
            decision_poller.cancel()
            media_poller.cancel()
            message_poller.cancel()
            human_queue_poller.cancel()
            verification_poller.cancel()
            for room_id in list(self.typing_tasks):
                await self._set_typing(room_id, False)
            await self.client.close()

    @staticmethod
    def _crash_after_effect(point: str) -> None:
        if os.environ.get("OPENRIG_BRIDGE_TEST_CRASH_AFTER") == point:
            LOGGER.critical("test fault injection after %s", point)
            os._exit(86)

    async def _initial_sync(self) -> None:
        response = await self.client.sync(timeout=0, full_state=True)
        if not isinstance(response, SyncResponse):
            raise ConnectionError(f"initial Matrix sync failed: {response}")
        if self.client.should_upload_keys:
            await self.client.keys_upload()

    def _load_room_index(self) -> None:
        agents = self.config.agents
        rooms = self.state.data.setdefault("rooms", {})
        for key, value in rooms.items():
            room_id = value.get("room_id")
            if key in agents and room_id:
                self.room_agents[room_id] = key

    def _repair_decision_reply_targets(self) -> None:
        changed = False
        for agent_key, pending in self.state.data["pending"].items():
            for request in pending:
                if request.get("reply_to_event_id"):
                    continue
                if request.get("source_label") != "decision" and not str(
                    request.get("text", "")
                ).startswith("Matrix-Entscheidung beantwortet."):
                    continue
                text = str(request.get("text", ""))
                candidates = [
                    decision
                    for decision in self.state.data["decisions"].values()
                    if decision.get("agent") == agent_key
                    and str(decision.get("title", "")) in text
                    and decision.get("event_ids")
                ]
                if not candidates:
                    continue
                decision = max(candidates, key=lambda item: item.get("created_at", 0))
                request["reply_to_event_id"] = str(decision["event_ids"][0])
                request["source_label"] = "decision"
                changed = True
        if changed:
            self.state.save()

    async def _poll_decisions(self) -> None:
        while True:
            try:
                await self._process_decision_inbox()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("decision processing failed")
            await asyncio.sleep(self.config.poll_interval_seconds)

    async def _process_decision_inbox(self) -> None:
        for path in sorted(self.decision_inbox.glob("*.json")):
            try:
                request = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(request, dict):
                    raise DecisionError("decision request must be an object")
                await self._publish_decision(request)
            except (DecisionError, json.JSONDecodeError, OSError):
                LOGGER.warning("invalid decision request: %s", path, exc_info=True)
                os.replace(path, self.decision_failed / path.name)
                continue
            except Exception:
                LOGGER.exception("failed to publish decision request: %s", path)
                os.replace(path, self.decision_failed / path.name)
                continue
            os.replace(path, self.decision_sent / path.name)

    async def _publish_decision(self, request: dict[str, Any]) -> None:
        request_id = str(request.get("id") or "").strip()
        agent_key = str(request.get("agent") or "").strip()
        title = str(request.get("title") or "").strip()
        body = str(request.get("body") or "").strip()
        qitem_id = str(request.get("qitem_id") or "").strip() or None
        questions = normalize_questions(request.get("questions"))
        agent = self.config.agents.get(agent_key)
        if not request_id or not title:
            raise DecisionError("decision request needs an id and title")
        if agent is None:
            raise DecisionError(f"unknown agent: {agent_key}")

        room = self.state.data["rooms"].get(agent.key)
        if not room:
            raise DecisionError(f"no Matrix room configured for: {agent_key}")
        room_id = str(room["room_id"])

        lines = [f"**Entscheidung: {title}**"]
        if body:
            lines.extend(["", body])
        if qitem_id:
            lines.extend(["", f"Queue-Item: `{qitem_id}`"])
        response = await self._send(room_id, "\n".join(lines))
        if not isinstance(response, RoomSendResponse):
            raise ConnectionError(f"decision message failed: {response}")

        event_ids = [response.event_id]
        polls: dict[str, dict[str, Any]] = {}
        for question in questions:
            poll_response_event = await self.client.room_send(
                room_id,
                POLL_START_TYPE,
                poll_start_content(question),
                ignore_unverified_devices=True,
            )
            if not isinstance(poll_response_event, RoomSendResponse):
                raise ConnectionError(f"decision poll failed: {poll_response_event}")
            poll_id = poll_response_event.event_id
            event_ids.append(poll_id)
            polls[poll_id] = question

        await self._add_pinned(room_id, event_ids)
        self.state.data["decisions"][request_id] = {
            "agent": agent.key,
            "qitem_id": qitem_id,
            "title": title,
            "room_id": room_id,
            "event_ids": event_ids,
            "polls": polls,
            "answers": {},
            "created_at": int(time.time() * 1000),
            "closed_at": None,
        }
        self.state.save()
        LOGGER.info("published decision %s in %s", request_id, agent.session)

    async def _poll_message_outbox(self) -> None:
        while True:
            try:
                await self._process_message_outbox()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("message processing failed")
            await asyncio.sleep(self.config.poll_interval_seconds)

    async def _poll_human_queue(self) -> None:
        while True:
            try:
                await self._mirror_human_queue()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("human queue mirror failed")
            await asyncio.sleep(self.config.human_queue_poll_seconds)

    async def _mirror_human_queue(self) -> None:
        if not self.config.human_destinations:
            return
        agents_by_session = {
            agent.session: agent for agent in self.config.agents.values()
        }
        items = await self.gateway.queue_list()
        mirror = self.state.data.setdefault(
            "queue_mirror",
            {"initialized": False, "seen": {}},
        )
        seen = mirror.setdefault("seen", {})
        if not mirror.get("initialized"):
            for item in items:
                qitem_id = str(item.get("qitemId") or "")
                if qitem_id:
                    seen[qitem_id] = int(time.time() * 1000)
            mirror["initialized"] = True
            self.state.save()
            LOGGER.info("initialized Matrix human-queue mirror")
            return

        human_destinations = set(self.config.human_destinations)
        for item in items:
            qitem_id = str(item.get("qitemId") or "").strip()
            if not qitem_id or qitem_id in seen:
                continue
            if item.get("destinationSession") not in human_destinations:
                seen[qitem_id] = int(time.time() * 1000)
                continue
            agent = agents_by_session.get(str(item.get("sourceSession") or ""))
            if agent is None:
                seen[qitem_id] = int(time.time() * 1000)
                continue
            if any(
                decision.get("qitem_id") == qitem_id
                for decision in self.state.data["decisions"].values()
            ):
                seen[qitem_id] = int(time.time() * 1000)
                continue

            detail = await self.gateway.queue_show(qitem_id)
            title = str(
                detail.get("summary") or item.get("summary") or "OpenRig update"
            ).strip()
            body = str(detail.get("body") or "").strip()
            if detail.get("humanIntent") == "decision":
                request_id = f"queue-{qitem_id}"
                await self._publish_decision(
                    {
                        "id": request_id,
                        "agent": agent.key,
                        "title": title,
                        "body": body,
                        "qitem_id": qitem_id,
                        "questions": normalize_questions(detail.get("humanQuestions")),
                    }
                )
            else:
                text = f"**{title}**"
                if body:
                    text += f"\n\n{body}"
                await self._publish_message(
                    {
                        "id": f"queue-{qitem_id}",
                        "agent": agent.key,
                        "text": text,
                    }
                )
            seen[qitem_id] = int(time.time() * 1000)
            self.state.save()
        if len(seen) > 2000:
            newest = sorted(seen.items(), key=lambda row: row[1])[-2000:]
            mirror["seen"] = dict(newest)
            self.state.save()

    async def _process_message_outbox(self) -> None:
        for path in sorted(self.message_outbox.glob("*.json")):
            try:
                request = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(request, dict):
                    raise TypeError("message request must be an object")
                await self._publish_message(request)
            except (TypeError, ValueError, json.JSONDecodeError, OSError):
                LOGGER.warning("invalid message request: %s", path, exc_info=True)
                os.replace(path, self.message_failed / path.name)
                continue
            except Exception:
                LOGGER.exception("failed to publish message request: %s", path)
                os.replace(path, self.message_failed / path.name)
                continue
            os.replace(path, self.message_sent / path.name)

    async def _publish_message(self, request: dict[str, Any]) -> None:
        request_id = str(request.get("id") or "").strip()
        agent_key = str(request.get("agent") or "").strip()
        text = str(request.get("text") or "").strip()
        agent = self.config.agents.get(agent_key)
        if not request_id or not text:
            raise ValueError("message request needs an id and text")
        if agent is None:
            raise ValueError(f"unknown agent: {agent_key}")
        room = self.state.data["rooms"].get(agent.key)
        if not room:
            raise ValueError(f"no Matrix room configured for: {agent.key}")

        response = await self._send(str(room["room_id"]), text)
        if not isinstance(response, RoomSendResponse):
            raise ConnectionError(f"message delivery failed: {response}")
        self.state.data["messages"][request_id] = {
            "agent": agent.key,
            "event_id": response.event_id,
            "sent_at": int(time.time() * 1000),
        }
        self.state.save()
        LOGGER.info("published message %s in %s", request_id, agent.session)

    async def _poll_media_outbox(self) -> None:
        while True:
            try:
                await self._process_media_outbox()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("media processing failed")
            await asyncio.sleep(self.config.poll_interval_seconds)

    async def _process_media_outbox(self) -> None:
        for path in sorted(self.media_outbox.glob("*.json")):
            request_id = path.stem
            if request_id in self.state.data.get("media", {}):
                os.replace(path, self.media_sent / path.name)
                continue
            if request_id in self.state.data.get("media_failures", {}):
                os.replace(path, self.media_failed / path.name)
                continue
            try:
                request = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(request, dict):
                    raise MediaError("media request must be an object")
                request_id = str(request.get("id") or path.stem)
                if request.get("kind") == "file":
                    await self._publish_file(request)
                else:
                    await self._publish_image(request)
            except MediaError as exc:
                LOGGER.warning("rejected media request: %s", path, exc_info=True)
                self._record_media_failure(request_id, path, str(exc))
                os.replace(path, self.media_failed / path.name)
                continue
            except (json.JSONDecodeError, OSError) as exc:
                LOGGER.warning("invalid media request: %s", path, exc_info=True)
                self._record_media_failure(
                    request_id,
                    path,
                    f"invalid media request: {exc}",
                )
                os.replace(path, self.media_failed / path.name)
                continue
            except Exception as exc:
                LOGGER.exception("failed to publish media request: %s", path)
                self._record_media_failure(request_id, path, str(exc))
                os.replace(path, self.media_failed / path.name)
                continue
            os.replace(path, self.media_sent / path.name)

    def _record_media_failure(
        self,
        request_id: str,
        path: Path,
        error: str,
    ) -> None:
        failures = self.state.data.setdefault("media_failures", {})
        failures[request_id] = {
            "error": error[:2000],
            "request": path.name,
            "failed_at": int(time.time() * 1000),
        }
        self.state.save()

    async def _publish_image(self, request: dict[str, Any]) -> None:
        if not self.config.media.enabled:
            raise MediaError("image exchange is disabled")
        request_id = str(request.get("id") or "").strip()
        agent_key = str(request.get("agent") or "").strip()
        agent = self.config.agents.get(agent_key)
        if not request_id or agent is None:
            raise MediaError("image request needs an id and configured agent")
        room = self.state.data["rooms"].get(agent.key)
        if not room:
            raise MediaError(f"no Matrix room configured for: {agent.key}")

        image_path = Path(str(request.get("path") or ""))
        if not image_path.is_absolute() or not image_path.is_file():
            raise MediaError(f"image file not found: {image_path}")
        size = image_path.stat().st_size
        if size <= 0 or size > self.config.media.max_image_bytes:
            raise MediaError(f"image size is outside the allowed range: {size} bytes")
        image_type = await asyncio.to_thread(detect_image_file, image_path)
        if image_type is None:
            raise MediaError("unsupported image type; use PNG, JPEG, GIF, or WebP")

        upload, encryption = await self.client.upload(
            lambda _retries, _timeouts: image_path,
            content_type=image_type.mime_type,
            filename=image_path.name,
            encrypt=True,
            filesize=size,
        )
        if not isinstance(
            upload, UploadResponse
        ) or not _has_complete_encryption_metadata(encryption):
            raise ConnectionError(f"encrypted image upload failed: {upload}")

        caption = str(request.get("caption") or "").strip()[:10_000]
        response = await self.client.room_send(
            str(room["room_id"]),
            "m.room.message",
            encrypted_image_content(
                filename=image_path.name,
                caption=caption,
                mime_type=image_type.mime_type,
                size=size,
                upload_url=upload.content_uri,
                encryption=encryption,
                formatted_caption=markdown_to_matrix_html(caption),
            ),
            tx_id=request_id,
            ignore_unverified_devices=True,
        )
        if not isinstance(response, RoomSendResponse):
            raise ConnectionError(f"encrypted image message failed: {response}")
        self._crash_after_effect("image_send")
        self.state.data["media"][request_id] = {
            "agent": agent.key,
            "event_id": response.event_id,
            "path": str(image_path),
            "mime_type": image_type.mime_type,
            "size": size,
            "sent_at": int(time.time() * 1000),
        }
        self.state.save()
        LOGGER.info("published encrypted image %s in %s", request_id, agent.session)

    async def _publish_file(self, request: dict[str, Any]) -> None:
        if not self.config.media.enabled:
            raise MediaError("file exchange is disabled")
        request_id = str(request.get("id") or "").strip()
        agent_key = str(request.get("agent") or "").strip()
        agent = self.config.agents.get(agent_key)
        if not request_id or agent is None:
            raise MediaError("file request needs an id and configured agent")
        room = self.state.data["rooms"].get(agent.key)
        if not room:
            raise MediaError(f"no Matrix room configured for: {agent.key}")

        file_path = Path(str(request.get("path") or ""))
        snapshot_value = str(request.get("snapshot_path") or "")
        expected_size = request.get("size")
        expected_sha256 = request.get("sha256")
        if (
            not snapshot_value
            or isinstance(expected_size, bool)
            or not isinstance(expected_size, int)
            or not isinstance(expected_sha256, str)
            or len(expected_sha256) != 64
        ):
            raise MediaError("file request lacks snapshot size or digest metadata")
        snapshot_path = Path(snapshot_value)
        snapshot_root = self.config.state_dir / "media-outbox" / "files"
        if not snapshot_path.is_absolute() or snapshot_path.parent != snapshot_root:
            raise MediaError("file snapshot is outside the private outbox")
        filename = str(request.get("filename") or file_path.name)
        mime_type = normalize_mime_type(
            request.get("mime_type") or "application/octet-stream"
        )
        try:
            snapshot_handle = await asyncio.to_thread(
                _open_verified_snapshot,
                snapshot_path,
                expected_size=expected_size,
                expected_sha256=expected_sha256,
                max_file_bytes=self.config.media.max_file_bytes,
            )
        except OSError as exc:
            raise MediaError(f"file snapshot could not be opened: {exc}") from exc
        size = expected_size
        try:
            upload, encryption = await self.client.upload(
                snapshot_handle,
                content_type=mime_type,
                filename=filename,
                encrypt=True,
                filesize=size,
            )
        finally:
            snapshot_handle.close()
        if not isinstance(
            upload, UploadResponse
        ) or not _has_complete_encryption_metadata(encryption):
            raise MediaError(
                "encrypted file upload returned incomplete encryption metadata"
            )

        caption = str(request.get("caption") or "").strip()[:10_000]
        response = await self.client.room_send(
            str(room["room_id"]),
            "m.room.message",
            encrypted_file_content(
                filename=filename,
                caption=caption,
                mime_type=mime_type,
                size=size,
                upload_url=upload.content_uri,
                encryption=encryption,
                formatted_caption=markdown_to_matrix_html(caption),
            ),
            tx_id=request_id,
            ignore_unverified_devices=True,
        )
        if not isinstance(response, RoomSendResponse):
            raise ConnectionError(f"encrypted file message failed: {response}")
        self._crash_after_effect("attachment_send")
        self.state.data["media"][request_id] = {
            "agent": agent.key,
            "event_id": response.event_id,
            "room_id": str(room["room_id"]),
            "path": str(file_path),
            "snapshot_path": str(snapshot_path),
            "filename": filename,
            "mime_type": mime_type,
            "size": size,
            "sha256": expected_sha256,
            "sent_at": int(time.time() * 1000),
        }
        self.state.save()
        LOGGER.info("published encrypted file %s in %s", request_id, agent.session)

    async def _add_pinned(self, room_id: str, event_ids: list[str]) -> None:
        lock = self.pin_locks.setdefault(room_id, asyncio.Lock())
        async with lock:
            pinned = await self._pinned_event_ids(room_id)
            merged = list(dict.fromkeys([*pinned, *event_ids]))
            if merged != pinned:
                await self.client.room_put_state(
                    room_id,
                    "m.room.pinned_events",
                    {"pinned": merged},
                )

    async def _update_queue_notice(
        self,
        agent: AgentConfig,
        room_id: str,
        waiting: int,
    ) -> None:
        notices = self.state.data.setdefault("queue_notices", {})
        notice = notices.setdefault(agent.key, {})
        event_id = notice.get("event_id")
        pending = self.state.data.get("pending", {}).get(agent.key, [])
        if not pending and waiting <= 0:
            if event_id and notice.get("pinned", True):
                await self._remove_pinned(room_id, [str(event_id)])
                notice["pinned"] = False
                self.state.save()
            return
        priority = str(
            getattr(
                getattr(self, "config", None),
                "inbound_message_priority",
                "urgent",
            )
        )
        priority_label = {
            "routine": "normal",
            "urgent": "hoch",
            "critical": "kritisch",
        }.get(priority, priority)
        text = queue_notice_text(
            pending,
            priority_label=priority_label,
        )
        if text is None and waiting > 0:
            noun = "Nachricht wartet" if waiting == 1 else "Nachrichten warten"
            text = (
                f"⏳ **Warteschlange:** {waiting} {noun} · "
                f"Priorität {priority_label}.\n\n"
                "Diese Hinweiskarte wird aktualisiert und nach dem "
                "Abarbeiten entpinnt."
            )
        if text is None:
            if event_id and notice.get("pinned", True):
                await self._remove_pinned(room_id, [str(event_id)])
                notice["pinned"] = False
                self.state.save()
            return

        target_event_id = str(event_id) if event_id else None
        response = None
        if target_event_id:
            try:
                response = await self._edit_message(
                    room_id,
                    target_event_id,
                    text,
                )
            except Exception:
                response = None
                LOGGER.warning(
                    "queue notice edit raised for %s",
                    agent.session,
                    exc_info=True,
                )
        if not isinstance(response, RoomSendResponse):
            try:
                response = await self._send(room_id, text)
            except Exception:
                response = None
                LOGGER.warning(
                    "queue notice send raised for %s",
                    agent.session,
                    exc_info=True,
                )
            target_event_id = None
        if not isinstance(response, RoomSendResponse):
            LOGGER.warning(
                "queue notice delivery failed for %s: %s", agent.session, response
            )
            return
        if target_event_id is None:
            notice["event_id"] = response.event_id
        notice["pinned"] = True
        self.state.save()
        await self._add_pinned(room_id, [str(notice["event_id"])])

    async def _restore_queue_notices(self) -> None:
        for agent in self.config.agents.values():
            room = self.state.data["rooms"].get(agent.key)
            if not room:
                continue
            pending = self.state.data["pending"].get(agent.key, [])
            await self._update_queue_notice(
                agent,
                str(room["room_id"]),
                max(0, len(pending) - 1),
            )

    async def _remove_pinned(self, room_id: str, event_ids: list[str]) -> None:
        lock = self.pin_locks.setdefault(room_id, asyncio.Lock())
        async with lock:
            pinned = await self._pinned_event_ids(room_id)
            remove = set(event_ids)
            remaining = [event_id for event_id in pinned if event_id not in remove]
            if remaining != pinned:
                await self.client.room_put_state(
                    room_id,
                    "m.room.pinned_events",
                    {"pinned": remaining},
                )

    async def _pinned_event_ids(self, room_id: str) -> list[str]:
        response = await self.client.room_get_state_event(
            room_id,
            "m.room.pinned_events",
        )
        if not isinstance(response, RoomGetStateEventResponse):
            return []
        pinned = response.content.get("pinned")
        if not isinstance(pinned, list):
            return []
        return [item for item in pinned if isinstance(item, str)]

    async def _restore_room_names(self) -> None:
        for agent in self.config.agents.values():
            room = self.state.data["rooms"].get(agent.key)
            if not room:
                continue
            stable_name = room_name(agent)
            try:
                await self.client.room_put_state(
                    str(room["room_id"]),
                    "m.room.name",
                    {"name": stable_name},
                )
            except Exception:
                LOGGER.warning(
                    "failed to restore room name for %s", agent.session, exc_info=True
                )
                continue
            room["name"] = stable_name
        self.state.save()

    async def _ensure_room_avatars(self) -> None:
        if not self.config.avatars.enabled:
            return
        avatar_state = self.state.data.setdefault(
            "avatar_pool",
            {
                "asset_fingerprint": None,
                "assignments": {},
                "uploads": {},
                "profile_mxc": None,
            },
        )
        fingerprint = avatar_pool_fingerprint()
        if avatar_state.get("asset_fingerprint") != fingerprint:
            avatar_state["asset_fingerprint"] = fingerprint
            avatar_state["uploads"] = {}
            avatar_state["profile_mxc"] = None
        assignments = avatar_state.setdefault("assignments", {})
        uploads = avatar_state.setdefault("uploads", {})
        candidates: list[str] = []
        existing: dict[str, str] = {}
        for key in self.config.agents:
            room = self.state.data["rooms"].get(key)
            if not room:
                continue
            current = assignments.get(key, {})
            current_id = current.get("id") if isinstance(current, dict) else None
            room_state = self.client.rooms.get(str(room["room_id"]))
            current_url = getattr(room_state, "avatar_url", None)
            managed_url = current.get("mxc") if isinstance(current, dict) else None
            if (
                not self.config.avatars.replace_existing
                and current_url
                and current_url != managed_url
            ):
                continue
            candidates.append(key)
            if isinstance(current_id, str):
                existing[key] = current_id

        selected = assign_unique_avatar_ids(candidates, existing)
        assignments.clear()
        assignments.update(
            {key: {"id": avatar_id} for key, avatar_id in selected.items()}
        )
        for key, avatar_id in selected.items():
            room = self.state.data["rooms"][key]
            room_id = str(room["room_id"])
            avatar_url = uploads.get(avatar_id)
            if not isinstance(avatar_url, str):
                image_data = avatar_png(avatar_id)
                upload, _encryption = await self.client.upload(
                    BytesIO(image_data),
                    content_type="image/png",
                    filename=f"{avatar_id}.png",
                    filesize=len(image_data),
                )
                if not isinstance(upload, UploadResponse):
                    LOGGER.warning("avatar upload failed for %s: %s", avatar_id, upload)
                    continue
                avatar_url = upload.content_uri
                uploads[avatar_id] = avatar_url
            room_state = self.client.rooms.get(room_id)
            if (
                self.config.avatars.replace_existing
                or getattr(room_state, "avatar_url", None) != avatar_url
            ):
                response = await self.client.room_put_state(
                    room_id,
                    "m.room.avatar",
                    {"url": avatar_url},
                )
                if hasattr(response, "status_code") and response.status_code >= 400:
                    LOGGER.warning(
                        "room avatar update failed for %s: %s", key, response
                    )
                    continue
            assignments[key] = {"id": avatar_id, "mxc": avatar_url}
            room["avatar"] = avatar_id

        missing = [key for key in candidates if key not in selected]
        if missing:
            LOGGER.warning(
                "avatar pool exhausted; no avatar assigned for: %s",
                ", ".join(missing),
            )

        if self.config.avatars.set_profile:
            profile_url = avatar_state.get("profile_mxc")
            if not isinstance(profile_url, str):
                image_data = profile_png()
                upload, _encryption = await self.client.upload(
                    BytesIO(image_data),
                    content_type="image/png",
                    filename="relay-avatar-256.png",
                    filesize=len(image_data),
                )
                if isinstance(upload, UploadResponse):
                    profile_url = upload.content_uri
                    avatar_state["profile_mxc"] = profile_url
                else:
                    LOGGER.warning("OpenRig profile avatar upload failed: %s", upload)
            if isinstance(profile_url, str):
                response = await self.client.set_avatar(profile_url)
                if not isinstance(response, ProfileSetAvatarResponse):
                    LOGGER.warning("OpenRig profile avatar update failed: %s", response)
        self.state.save()

    async def _create_rooms(self) -> None:
        owner = self.config.allowed_users[0]
        rejected = self.state.data.setdefault("rejected_rooms", {})
        encryption = {
            "type": "m.room.encryption",
            "state_key": "",
            "content": {
                "algorithm": "m.megolm.v1.aes-sha2",
                "rotation_period_ms": 604800000,
                "rotation_period_msgs": 100,
            },
        }
        for key, agent in self.config.agents.items():
            if key in self.state.data["rooms"]:
                continue
            if key in rejected:
                LOGGER.info(
                    "skipping rejected Matrix room for %s",
                    agent.session,
                )
                continue
            name = room_name(agent)
            LOGGER.info("creating Matrix room %s for %s", name, agent.session)
            response = await self.client.room_create(
                visibility=RoomVisibility.private,
                name=name,
                topic=f"OpenRig seat {agent.session}",
                invite=[owner],
                is_direct=True,
                preset=RoomPreset.private_chat,
                initial_state=[encryption],
            )
            if isinstance(response, RoomCreateResponse):
                self.state.data["rooms"][key] = {
                    "room_id": response.room_id,
                    "name": name,
                }
                self.room_agents[response.room_id] = key
                self.state.save()
                continue
            raise RuntimeError(f"room creation failed for {name}: {response}")

    async def _reconcile_room_memberships(self) -> None:
        owner = self.config.allowed_users[0]
        for key, room in list(self.state.data["rooms"].items()):
            agent = self.config.agents.get(key)
            if agent is None:
                continue
            room_id = str(room.get("room_id") or "")
            if not room_id:
                continue
            response = await self.client.room_get_state_event(
                room_id,
                "m.room.member",
                owner,
            )
            if not isinstance(response, RoomGetStateEventResponse):
                LOGGER.warning(
                    "could not read owner membership for %s: %s",
                    room_id,
                    response,
                )
                continue
            membership = str(response.content.get("membership") or "")
            if membership in {"leave", "ban"}:
                await self._reject_agent_room(
                    agent,
                    room_id,
                    reason=f"owner_{membership}",
                )

    async def _reject_agent_room(
        self,
        agent: AgentConfig,
        room_id: str,
        *,
        reason: str,
    ) -> None:
        rejected = self.state.data.setdefault("rejected_rooms", {})
        existing = rejected.get(agent.key)
        if isinstance(existing, dict) and existing.get("room_id") == room_id:
            return
        rejected[agent.key] = {
            "room_id": room_id,
            "reason": reason,
            "rejected_at": int(time.time() * 1000),
        }
        self.state.data["rooms"].pop(agent.key, None)
        self.state.data["pending"].pop(agent.key, None)
        self.state.data["welcomed"].pop(agent.key, None)
        self.state.data["queue_notices"].pop(agent.key, None)
        avatar_pool = self.state.data.get("avatar_pool", {})
        assignments = avatar_pool.get("assignments")
        if isinstance(assignments, dict):
            assignments.pop(agent.key, None)
        self.room_agents.pop(room_id, None)
        self.agent_statuses.pop(agent.key, None)
        self.state.save()
        LOGGER.info(
            "rejected Matrix room %s for %s (%s)",
            room_id,
            agent.session,
            reason,
        )
        response = await self.client.room_leave(room_id)
        if hasattr(response, "status_code") and response.status_code >= 400:
            LOGGER.warning(
                "could not leave rejected Matrix room %s: %s",
                room_id,
                response,
            )

    def _initialize_transcript_offsets(self) -> None:
        offsets = self.state.data.setdefault("transcripts", {})
        try:
            resolved = self.resolver.resolve_many(
                {agent.session for agent in self.config.agents.values()}
            )
        except AgentSessionNotFound:
            resolved = {}
        for key, agent in self.config.agents.items():
            ref = resolved.get(agent.session)
            if ref is None:
                continue
            offsets[key] = {
                "path": str(ref.transcript_path),
                "offset": ref.transcript_path.stat().st_size,
            }
        self.state.save()

    async def _on_invite(self, room: MatrixRoom, event: InviteMemberEvent) -> None:
        if event.sender in self.config.allowed_users:
            await self.client.join(room.room_id)

    def _on_member(self, room: MatrixRoom, event: RoomMemberEvent) -> None:
        asyncio.create_task(self._handle_member(room, event))

    async def _handle_member(self, room: MatrixRoom, event: RoomMemberEvent) -> None:
        if event.state_key not in self.config.allowed_users:
            return
        agent = self._agent_for_room(room.room_id)
        if not agent:
            return
        if event.membership in {"leave", "ban"}:
            await self._reject_agent_room(
                agent,
                room.room_id,
                reason=f"owner_{event.membership}",
            )
            return
        if event.membership != "join" or self.state.data["welcomed"].get(agent.key):
            return
        response = await self._send(
            room.room_id,
            f"Mit **{agent.display_name}** verbunden. Nachrichten in diesem Raum gehen an "
            f"`{agent.session}`.",
        )
        if isinstance(response, RoomSendResponse):
            self.state.data["welcomed"][agent.key] = True
            self.state.save()

    def _on_room_message(self, room: MatrixRoom, event: RoomMessageText) -> None:
        asyncio.create_task(self._handle_room_message(room, event))

    def _on_audio(self, room: MatrixRoom, event: RoomMessageAudio) -> None:
        asyncio.create_task(self._handle_audio(room, event))

    def _on_image(
        self,
        room: MatrixRoom,
        event: RoomMessageImage | RoomEncryptedImage,
    ) -> None:
        asyncio.create_task(self._handle_image(room, event))

    def _on_file(
        self,
        room: MatrixRoom,
        event: RoomMessageFile | RoomEncryptedFile,
    ) -> None:
        if isinstance(event, (RoomMessageAudio, RoomEncryptedAudio)):
            return
        asyncio.create_task(self._handle_file(room, event))

    def _on_room_message_unknown(
        self,
        room: MatrixRoom,
        event: RoomMessageUnknown,
    ) -> None:
        if event.msgtype in GALLERY_MSGTYPES:
            asyncio.create_task(self._handle_gallery(room, event))

    def _on_unknown_event(self, room: MatrixRoom, event: UnknownEvent) -> None:
        if event.type in {POLL_RESPONSE_TYPE, STABLE_POLL_RESPONSE_TYPE}:
            asyncio.create_task(self._handle_poll_response(room, event))

    async def _handle_poll_response(
        self,
        room: MatrixRoom,
        event: UnknownEvent,
    ) -> None:
        if event.sender not in self.config.allowed_users:
            return
        agent = self._agent_for_room(room.room_id)
        if agent is None:
            return
        response = poll_response(event.source.get("content", {}))
        if response is None:
            return
        poll_id, selected = response
        if not selected:
            return

        for decision in self.state.data["decisions"].values():
            question = decision.get("polls", {}).get(poll_id)
            if not isinstance(question, dict):
                continue
            question_id = str(question["id"])
            if question_id in decision.get("answers", {}):
                return
            options = {
                str(option["id"]): str(option["label"])
                for option in question["options"]
            }
            selected_labels = [
                options.get(option_id, option_id)
                for option_id in selected
                if option_id in options
            ]
            if not selected_labels:
                return
            decision.setdefault("answers", {})[question_id] = {
                "option_ids": selected,
                "labels": selected_labels,
                "answered_at": int(time.time() * 1000),
            }

            all_answered = all(
                poll_question["id"] in decision["answers"]
                for poll_question in decision.get("polls", {}).values()
            )
            unpin = decision.get("event_ids", []) if all_answered else [poll_id]
            await self._remove_pinned(room.room_id, unpin)
            if all_answered:
                decision["closed_at"] = int(time.time() * 1000)
            self.state.save()

            answer_text = ", ".join(selected_labels)
            queue_line = (
                f"Queue-Item: {decision['qitem_id']}\n"
                if decision.get("qitem_id")
                else ""
            )
            await self._enqueue_prompt(
                agent,
                room,
                event.event_id,
                int(event.server_timestamp),
                "Matrix-Entscheidung beantwortet.\n\n"
                f"{queue_line}"
                f"Entscheidung: {decision['title']}\n"
                f"Frage: {question['question']}\n"
                f"Auswahl: {answer_text} ({', '.join(selected)})\n\n"
                "Behandle diese Antwort als Entscheidung und aktualisiere das "
                "zugehörige Queue-Item.",
                "decision",
                str(decision["event_ids"][0]),
            )
            thread_root = str(decision["event_ids"][0])
            await self._send(
                room.room_id,
                f"Antwort an **{agent.display_name}** übergeben: {answer_text}",
                thread_root,
            )
            return

    def _agent_for_room(self, room_id: str) -> AgentConfig | None:
        agent_key = self.room_agents.get(room_id)
        return self.config.agents.get(agent_key) if agent_key else None

    async def _on_reaction(
        self,
        room: MatrixRoom,
        event: ReactionEvent,
    ) -> None:
        try:
            await self._handle_reaction(room, event)
        except Exception:
            LOGGER.exception("Matrix reaction processing failed")

    async def _handle_reaction(
        self,
        room: MatrixRoom,
        event: ReactionEvent,
    ) -> None:
        if not getattr(self.config, "reaction_feedback_enabled", True):
            return
        if event.sender == self.config.user_id:
            return
        agent = self._agent_for_room(room.room_id)
        if agent is None:
            return
        if event.sender not in self.config.allowed_users:
            return
        if event.event_id in self.processed_events:
            return
        if self.config.max_message_age_minutes >= 0:
            age_ms = int(time.time() * 1000) - int(event.server_timestamp)
            if age_ms > self.config.max_message_age_minutes * 60_000:
                return

        self.processed_events.add(event.event_id)
        self.state.data["processed_events"] = list(self.processed_events)[-2000:]
        self.state.save()

        target_text = ""
        try:
            response = await self.client.room_get_event(
                room.room_id,
                event.reacts_to,
            )
        except Exception:
            LOGGER.warning(
                "could not load Matrix reaction target %s",
                event.reacts_to,
                exc_info=True,
            )
            response = None
        if isinstance(response, RoomGetEventResponse):
            target = response.event
            if getattr(target, "sender", None) != self.config.user_id:
                return
            target_text = str(getattr(target, "body", "") or "").strip()

        preview = " ".join(target_text.split())
        if len(preview) > 600:
            preview = preview[:597] + "..."
        feedback = (
            f"Matrix-Reaktion von {event.sender}: {event.key}\n"
            f"Reagiertes Event: {event.reacts_to}\n"
            f"Bezugstext: {preview or '(nicht verfügbar)'}\n\n"
            "Nimm diese Reaktion als stilles Feedback wahr. Wenn sie für "
            "Erinnerungen, Vorlieben oder eine spätere Entscheidung relevant "
            "ist, berücksichtige sie. Erzeuge dafür keine sichtbare "
            "Matrix-Antwort."
        )
        await self._enqueue_prompt(
            agent,
            room,
            event.event_id,
            event.server_timestamp,
            feedback,
            "reaction",
            event.reacts_to,
            suppress_reply=True,
            queue_tracked=False,
        )

    async def _handle_room_message(
        self, room: MatrixRoom, event: RoomMessageText
    ) -> None:
        if event.sender == self.config.user_id:
            return
        agent = self._agent_for_room(room.room_id)
        if not agent:
            return
        if event.sender not in self.config.allowed_users:
            LOGGER.warning(
                "ignoring Matrix sender %s in %s", event.sender, room.room_id
            )
            return
        if event.event_id in self.processed_events:
            return
        if self.config.max_message_age_minutes >= 0:
            age_ms = int(time.time() * 1000) - int(event.server_timestamp)
            if age_ms > self.config.max_message_age_minutes * 60_000:
                await self._send(
                    room.room_id,
                    "Nachricht ist zu alt und wurde nicht an den Agenten gesendet.",
                    event.event_id,
                )
                return
        text = event.body.strip()
        if not text:
            return
        if await self._handle_verification_reply(room, event, text):
            return
        relates_to = message_relation(event)
        self._intake_part(agent, room.room_id, event, "text")
        await self._enqueue_prompt(
            agent,
            room,
            event.event_id,
            event.server_timestamp,
            text,
            relates_to=relates_to,
        )

    async def _handle_verification_reply(
        self,
        room: MatrixRoom,
        event: RoomMessageText,
        text: str,
    ) -> bool:
        decision = verification_decision(text)
        if decision is None:
            return False
        now = time.time()
        selected = pending_verification(
            self.verification_records,
            room.room_id,
            now=now,
        )
        if selected is None:
            return False
        transaction_id, verification = selected
        self.processed_events.add(event.event_id)
        self.state.data["processed_events"] = list(self.processed_events)[-2000:]
        phase = str(verification.get("phase") or "")
        if phase == "interrupted":
            verification["interrupted_reply_at"] = now
            self.state.save()
            await self._send(
                room.room_id,
                "Für dieses Gerät läuft keine aktive Verifikation mehr. "
                "Bitte starte die Verifikation erneut.",
                event.event_id,
            )
            return True
        if phase != "awaiting_confirmation":
            if phase in TERMINAL_PHASES:
                mark_verification(
                    self.verification_records,
                    transaction_id,
                    "stale_reply",
                    now=now,
                )
                self.state.save()
                await self._send(
                    room.room_id,
                    "Für dieses Gerät läuft keine aktive Verifikation mehr. "
                    "Bitte starte die Verifikation erneut.",
                    event.event_id,
                )
            else:
                await self._send(
                    room.room_id,
                    "Die Geräteverifikation ist noch nicht bereit für `ja` oder `nein`.",
                    event.event_id,
                )
            return True
        if decision:
            mark_verification(
                self.verification_records,
                transaction_id,
                "confirming",
                now=now,
            )
            self.state.save()
            result = await self.client.confirm_short_auth_string(transaction_id)
            if isinstance(result, ToDeviceError):
                mark_verification(
                    self.verification_records,
                    transaction_id,
                    "failed",
                    now=time.time(),
                )
                self.state.save()
                await self._send(
                    room.room_id,
                    f"Bestätigung fehlgeschlagen: {result}",
                    event.event_id,
                )
            else:
                mark_verification(
                    self.verification_records,
                    transaction_id,
                    "awaiting_mac",
                    now=time.time(),
                )
                self.state.save()
                await self._send(
                    room.room_id,
                    "SAS bestätigt. Bestätige den Vorgang jetzt auch im Element-Dialog.",
                    event.event_id,
                )
        else:
            mark_verification(
                self.verification_records,
                transaction_id,
                "cancelling",
                now=now,
            )
            self.state.save()
            result = await self.client.cancel_key_verification(
                transaction_id, reject=True
            )
            if isinstance(result, ToDeviceError):
                mark_verification(
                    self.verification_records,
                    transaction_id,
                    "failed",
                    now=time.time(),
                )
                self.state.save()
                await self._send(
                    room.room_id, f"Abbruch fehlgeschlagen: {result}", event.event_id
                )
            else:
                mark_verification(
                    self.verification_records,
                    transaction_id,
                    "rejected",
                    now=time.time(),
                )
                self.state.save()
                await self._send(
                    room.room_id, "Geräteverifikation abgelehnt.", event.event_id
                )
        return True

    async def _recover_verifications(self) -> None:
        interrupted = recover_interrupted_verifications(
            self.verification_records,
            now=time.time(),
        )
        if interrupted:
            self.state.save()
        await self._deliver_verification_notifications()
        pruned = prune_terminal_verifications(
            self.verification_records,
            now=time.time(),
            retention=VERIFICATION_RECORD_RETENTION_SECONDS,
        )
        if pruned:
            self.state.save()

    async def _expire_verifications(self, *, now: float, timeout: float) -> None:
        expired = expire_active_verifications(
            self.verification_records,
            now=now,
            timeout=timeout,
        )
        if expired:
            self.state.save()
        for transaction_id, room_id in expired:
            sas = self.client.key_verifications.get(transaction_id)
            if sas is not None:
                try:
                    await self.client.cancel_key_verification(transaction_id)
                except Exception:
                    LOGGER.warning(
                        "could not cancel timed-out verification %s",
                        transaction_id,
                        exc_info=True,
                    )
        await self._deliver_verification_notifications()
        pruned = prune_terminal_verifications(
            self.verification_records,
            now=now,
            retention=VERIFICATION_RECORD_RETENTION_SECONDS,
        )
        if pruned:
            self.state.save()

    async def _deliver_verification_notifications(self) -> None:
        for record in self.verification_records.values():
            notification = record.get("terminal_notification")
            if not isinstance(notification, dict):
                continue
            if notification.get("state") == "delivered":
                continue
            room_id = str(notification.get("room_id") or "")
            text = str(notification.get("text") or "")
            tx_id = str(notification.get("tx_id") or "")
            if not room_id or not text or not tx_id:
                LOGGER.warning("invalid verification notification: %s", notification)
                continue
            try:
                response = await self._send(
                    room_id,
                    text,
                    tx_id=tx_id,
                )
            except Exception:
                LOGGER.warning(
                    "could not deliver verification notification %s",
                    tx_id,
                    exc_info=True,
                )
                continue
            event_id = str(getattr(response, "event_id", "") or "")
            if not event_id:
                LOGGER.warning(
                    "verification notification %s failed: %s",
                    tx_id,
                    response,
                )
                continue
            notification.update(
                {
                    "state": "delivered",
                    "event_id": event_id,
                    "delivered_at": time.time(),
                }
            )
            self.state.save()

    async def _poll_verification_timeouts(self) -> None:
        while True:
            try:
                await asyncio.sleep(
                    min(30.0, max(1.0, float(self.config.status_refresh_seconds)))
                )
                await self._expire_verifications(
                    now=time.time(),
                    timeout=VERIFICATION_TIMEOUT_SECONDS,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("verification timeout polling failed")

    def _verification_room(self) -> str:
        advisor_room = self.state.data["rooms"].get("advisor", {}).get("room_id")
        if advisor_room:
            return str(advisor_room)
        return str(next(iter(self.state.data["rooms"].values()))["room_id"])

    async def _on_verification(self, event: KeyVerificationEvent) -> None:
        if getattr(event, "sender", None) not in self.config.allowed_users:
            return
        room_id = self._verification_room()
        if isinstance(event, KeyVerificationStart):
            if "emoji" not in event.short_authentication_string:
                return
            begin_verification(
                self.verification_records,
                event.transaction_id,
                room_id,
                now=time.time(),
            )
            self.state.save()
            response = await self.client.accept_key_verification(event.transaction_id)
            if isinstance(response, ToDeviceError):
                mark_verification(
                    self.verification_records,
                    event.transaction_id,
                    "failed",
                    now=time.time(),
                )
                self.state.save()
                await self._send(
                    room_id,
                    f"Matrix-Verifikation konnte nicht gestartet werden: {response}",
                )
                return
            sas = self.client.key_verifications.get(event.transaction_id)
            if sas is not None:
                response = await self.client.to_device(sas.share_key())
                if isinstance(response, ToDeviceError):
                    mark_verification(
                        self.verification_records,
                        event.transaction_id,
                        "failed",
                        now=time.time(),
                    )
                    self.state.save()
                    await self._send(
                        room_id, f"Schlüsselaustausch fehlgeschlagen: {response}"
                    )
                    return
            mark_verification(
                self.verification_records,
                event.transaction_id,
                "awaiting_key",
                now=time.time(),
            )
            self.state.save()
            await self._send(
                room_id,
                "Matrix-Verifikationsanfrage empfangen. Die Emoji-Prüfung folgt gleich.",
            )
            return

        if isinstance(event, KeyVerificationKey):
            sas = self.client.key_verifications.get(event.transaction_id)
            if sas is None:
                return
            emojis = sas.get_emoji()
            rendered = " ".join(emoji for emoji, _name in emojis)
            named = ", ".join(f"{emoji} {name}" for emoji, name in emojis)
            if event.transaction_id not in self.verification_records:
                begin_verification(
                    self.verification_records,
                    event.transaction_id,
                    room_id,
                    now=time.time(),
                )
            mark_verification(
                self.verification_records,
                event.transaction_id,
                "awaiting_confirmation",
                now=time.time(),
            )
            self.state.save()
            await self._send(
                room_id,
                "Vergleiche diese sieben Emojis mit dem Element-Dialog:\n\n"
                f"{rendered}\n\n{named}\n\n"
                "Wenn sie übereinstimmen, schreibe hier `ja`. Sonst `nein`.",
            )
            return

        if isinstance(event, KeyVerificationMac):
            sas = self.client.key_verifications.get(event.transaction_id)
            if sas is not None and sas.verified:
                if event.transaction_id in self.verification_records:
                    mark_verification(
                        self.verification_records,
                        event.transaction_id,
                        "verified",
                        now=time.time(),
                    )
                    self.state.save()
                await self._send(
                    room_id,
                    f"Gerät `{sas.other_olm_device.id}` wurde erfolgreich verifiziert.",
                )
            return

        if isinstance(event, KeyVerificationCancel):
            if event.transaction_id in self.verification_records:
                mark_verification(
                    self.verification_records,
                    event.transaction_id,
                    "cancelled",
                    now=time.time(),
                )
                self.state.save()
            await self._send(
                room_id,
                f"Geräteverifikation abgebrochen: {event.reason or event.code}",
            )

    async def _enqueue_prompt(
        self,
        agent: AgentConfig,
        room: MatrixRoom,
        event_id: str,
        event_timestamp: int,
        text: str,
        source_label: str | None = None,
        reply_to_event_id: str | None = None,
        *,
        relates_to: dict[str, Any] | None = None,
        suppress_reply: bool = False,
        queue_tracked: bool = True,
    ) -> None:
        found = self._processing_part(event_id)
        if found is not None and queue_tracked and not suppress_reply:
            # An owner event that took its place in a burst at intake: its
            # prompt is ready, and the burst decides how it reaches the agent.
            burst, _part = found
            await self._complete_part(
                burst["id"],
                event_id,
                text,
                source_label or "message",
            )
            return
        intake_at = time.time()
        target = (
            ResponseTarget(reply_to_event_id, reply_to_event_id)
            if reply_to_event_id
            else resolve_response_target(event_id, relates_to)
        )
        async with self.locks[agent.key]:
            await self._enqueue_locked(
                agent,
                room.room_id,
                [(event_id, text)],
                text,
                source_label or "message",
                target,
                relates_to=relates_to,
                suppress_reply=suppress_reply,
                queue_tracked=queue_tracked,
                intake_at=intake_at,
            )

    async def _enqueue_locked(
        self,
        agent: AgentConfig,
        room_id: str,
        parts: list[tuple[str, str]],
        text: str,
        source_label: str,
        target: ResponseTarget,
        *,
        relates_to: dict[str, Any] | None,
        suppress_reply: bool = False,
        queue_tracked: bool = True,
        intake_at: float,
        linked: bool = False,
        burst: dict[str, Any] | None = None,
        burst_parts: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any] | None:
        """Admit one request carrying `parts` (event id, own text) as `text`.

        Each part gets its own qitem. A linked request (a burst with an
        attachment) may steer regardless of steering_max_chars, so related
        context is never truncated. `burst_parts` are marked delivered in the
        same state save that admits the request.
        """
        pending = self.state.data["pending"].setdefault(agent.key, [])
        event_ids = [event_id for event_id, _text in parts]
        for event_id in event_ids:
            self.processed_events.add(event_id)
        self.state.data["processed_events"] = list(self.processed_events)[-2000:]
        qitem_ids: list[str] = []
        if queue_tracked:
            try:
                for event_id, part_text in parts:
                    qitem_id = await self._create_inbound_qitem(
                        agent,
                        event_id=event_id,
                        text=part_text,
                        source_label=source_label,
                    )
                    if qitem_id:
                        qitem_ids.append(qitem_id)
            except (GatewayError, OSError) as exc:
                LOGGER.exception(
                    "could not create durable qitem for Matrix event %s",
                    event_ids[-1],
                )
                for part in burst_parts or []:
                    part["phase"] = "failed"
                    part["error"] = "OpenRig-Queue-Item konnte nicht angelegt werden"
                self.state.save()
                await self._send(
                    room_id,
                    "OpenRig konnte das Queue-Item für diese Anfrage nicht "
                    f"anlegen. Die Anfrage wurde nicht an den Agenten gesendet: {exc}",
                    target.reply_to_event_id,
                    thread_root=target.thread_root_event_id,
                )
                return None
        qitem_id = qitem_ids[0] if qitem_ids else None
        request = {
            "id": uuid.uuid4().hex,
            "event_id": event_ids[-1],
            "reply_to_event_id": target.reply_to_event_id,
            "thread_root_event_id": target.thread_root_event_id,
            "source_event_ids": list(event_ids),
            "source_relation": dict(relates_to) if relates_to else None,
            "source_label": source_label,
            "qitem_id": qitem_id,
            "qitem_ids": list(qitem_ids),
            "queue_tracked": queue_tracked,
            "suppress_reply": suppress_reply,
            "priority": getattr(
                self.config,
                "inbound_message_priority",
                "urgent",
            ),
            "text": text,
            "intake_at": intake_at,
            "enqueued_at": time.time(),
            "prompt_phase": "pending",
            "answer_phase": None,
            "qitem_phase": "pending" if qitem_id else "not_tracked",
            "qitem_phases": {value: "pending" for value in qitem_ids},
            "started_at": None,
            "session_id": None,
            "transcript_path": None,
            "offset": None,
            "answer_text": None,
            "turn_attribution_required": True,
        }
        if burst is not None:
            request["burst_id"] = burst["id"]
            request["burst_part_ids"] = list(event_ids)
            if linked and len(burst["parts"]) > 1:
                # The answer names every related part as its source.
                request["source_event_ids"] = [
                    part["event_id"]
                    for part in burst["parts"]
                    if part.get("phase") in {"delivered", "reported"}
                    or part["event_id"] in event_ids
                ]
        for part in burst_parts or []:
            part["phase"] = "reported" if part.get("phase") == "failed" else "delivered"
            part["request_id"] = request["id"]
        if self._is_steering_candidate(
            pending,
            text,
            target,
            suppress_reply=suppress_reply,
            queue_tracked=queue_tracked,
            linked=linked,
        ):
            if linked and burst is not None:
                self._steer_burst_parts(agent, pending, request, burst)
            for index, (event_id, part_text) in enumerate(
                [] if linked and burst is not None else parts
            ):
                part_qitem = qitem_ids[index] if index < len(qitem_ids) else None
                self._append_steering_followup(
                    agent,
                    pending,
                    {
                        **request,
                        "id": uuid.uuid4().hex,
                        "event_id": event_id,
                        "source_event_ids": [event_id],
                        "qitem_id": part_qitem,
                        "qitem_ids": [part_qitem] if part_qitem else [],
                        "qitem_phase": "pending" if part_qitem else "not_tracked",
                        "qitem_phases": ({part_qitem: "pending"} if part_qitem else {}),
                    },
                    event_id=event_id,
                    text=part_text,
                )
            acknowledge = [
                event_id
                for event_id in event_ids
                if self._record_acceptance(
                    room_id,
                    event_id,
                    queue_tracked=queue_tracked,
                    suppress_reply=suppress_reply,
                )
            ]
            self.state.save()
            for event_id in acknowledge:
                await self._acknowledge_acceptance(room_id, event_id)
            await self._update_queue_notice(
                agent,
                room_id,
                max(0, len(pending) - 1),
            )
            return request
        pending.append(request)
        acknowledge = [
            event_id
            for event_id in event_ids
            if self._record_acceptance(
                room_id,
                event_id,
                queue_tracked=queue_tracked,
                suppress_reply=suppress_reply,
            )
        ]
        self.state.save()
        if len(pending) == 1:
            await self._start_request(agent, pending[0], room_id)
        if not request.get("suppress_reply"):
            await self._update_queue_notice(
                agent,
                room_id,
                max(0, len(pending) - 1),
            )
        for event_id in acknowledge:
            await self._acknowledge_acceptance(room_id, event_id)
        return request

    def _is_steering_candidate(
        self,
        pending: list[dict[str, Any]],
        text: str,
        target: ResponseTarget,
        *,
        suppress_reply: bool,
        queue_tracked: bool,
        linked: bool = False,
    ) -> bool:
        mode = str(getattr(self.config, "steering_mode", "auto"))
        if mode not in {"steer", "auto"}:
            return False
        if suppress_reply or not queue_tracked or not target.thread_root_event_id:
            return False
        max_chars = int(getattr(self.config, "steering_max_chars", 2000))
        if len(text) > max_chars and not linked:
            return False
        if not pending:
            return False
        active = pending[0]
        group_id = active.get("steering_group_id")
        if group_id:
            group = next(
                (request for request in pending[1:] if request.get("id") == group_id),
                None,
            )
            if group is not None and group.get("injection_phase") != "pending":
                return False
        return (
            active.get("prompt_phase") == "sent"
            and active.get("answer_phase") is None
            and active.get("thread_root_event_id") == target.thread_root_event_id
            and not active.get("suppress_reply")
        )

    def _append_steering_followup(
        self,
        agent: AgentConfig,
        pending: list[dict[str, Any]],
        incoming: dict[str, Any],
        *,
        event_id: str,
        text: str,
    ) -> None:
        active = pending[0]
        group_id = active.get("steering_group_id")
        group = next(
            (
                request
                for request in pending[1:]
                if request.get("id") == group_id and request.get("deferred_steering")
            ),
            None,
        )
        entry = {
            "event_id": event_id,
            "text": text,
            "qitem_id": incoming.get("qitem_id"),
            "reply_to_event_id": incoming["reply_to_event_id"],
            "source_relation": incoming.get("source_relation"),
            "intake_at": incoming.get("intake_at"),
            "enqueued_at": incoming["enqueued_at"],
        }
        if group is None:
            group = {
                **incoming,
                "deferred_steering": True,
                "steering_entries": [entry],
                "injection_phase": "pending",
                "injection_ready_at": (
                    time.time()
                    + float(
                        getattr(
                            self.config,
                            "steering_batch_window_seconds",
                            0.5,
                        )
                    )
                ),
                "injection_offset": None,
                "injection_text": None,
                "injection_turn_id": None,
            }
            pending.append(group)
            active["steering_group_id"] = group["id"]
        else:
            group["steering_entries"].append(entry)
            self._extend_steering_group(group, incoming)
        entries = group["steering_entries"]
        group["text"] = steering_batch_text(
            str(active["thread_root_event_id"]),
            entries,
        )
        LOGGER.info(
            "batched Matrix event %s for best-effort steering in %s",
            event_id,
            agent.session,
        )

    def _record_acceptance(
        self,
        room_id: str,
        event_id: str,
        *,
        queue_tracked: bool,
        suppress_reply: bool,
    ) -> bool:
        """Persist the acceptance-reaction intent with the accepted request.

        The caller saves state once, so the request and its pending
        acknowledgement become durable together and a crash before the
        reaction send is repaired by recovery instead of being lost.
        """
        if suppress_reply or not queue_tracked:
            return False
        if not getattr(self.config, "acceptance_reaction_enabled", False):
            return False
        acks = self.state.data.setdefault("acceptance_reactions", {})
        record = acks.get(event_id)
        if isinstance(record, dict):
            return record.get("phase") == "sending"
        now = time.time()
        acks[event_id] = {
            "room_id": room_id,
            "tx_id": f"ack-{event_id}",
            "phase": "sending",
            "attempts": 0,
            "accepted_at": now,
            # The accepting path makes the first attempt itself; recovery only
            # repairs a record that path did not finish.
            "retry_at": now + ACCEPTANCE_RETRY_BASE_SECONDS,
            "acknowledged_at": None,
            "reaction_event_id": None,
            "last_error": None,
        }
        settled = [
            key
            for key, value in acks.items()
            if not (isinstance(value, dict) and value.get("phase") == "sending")
        ]
        for key in settled[: max(0, len(acks) - MAX_ACCEPTANCE_RECORDS)]:
            del acks[key]
        return True

    async def _acknowledge_acceptance(self, room_id: str, event_id: str) -> bool:
        acks = self.state.data.get("acceptance_reactions", {})
        record = acks.get(event_id)
        if not isinstance(record, dict) or record.get("phase") != "sending":
            return False
        record["attempts"] = int(record.get("attempts") or 0) + 1
        error: object = None
        try:
            response = await self.client.room_send(
                room_id,
                "m.reaction",
                {
                    "m.relates_to": {
                        "rel_type": "m.annotation",
                        "event_id": event_id,
                        "key": ACCEPTANCE_REACTION_KEY,
                    }
                },
                tx_id=str(record.get("tx_id") or f"ack-{event_id}"),
                ignore_unverified_devices=True,
            )
        except Exception as exc:
            LOGGER.warning(
                "acceptance reaction for %s raised",
                event_id,
                exc_info=True,
            )
            response = None
            error = exc
        if isinstance(response, RoomSendResponse):
            self._crash_after_effect("ack_send")
            record["phase"] = "sent"
            record["reaction_event_id"] = response.event_id
        elif getattr(response, "status_code", None) == "M_DUPLICATE_ANNOTATION":
            record["phase"] = "sent"
        else:
            record["last_error"] = str(error or response)
            record["retry_at"] = time.time() + min(
                ACCEPTANCE_RETRY_MAX_SECONDS,
                ACCEPTANCE_RETRY_BASE_SECONDS * 2 ** (record["attempts"] - 1),
            )
            if record["attempts"] >= MAX_ACCEPTANCE_ATTEMPTS:
                record["phase"] = "failed"
                LOGGER.error(
                    "giving up acceptance reaction for %s after %s attempts: %s",
                    event_id,
                    record["attempts"],
                    record["last_error"],
                )
            self.state.save()
            return False
        record["acknowledged_at"] = time.time()
        record["last_error"] = None
        record.pop("retry_at", None)
        self.state.save()
        return True

    async def _recover_acceptance_reactions(self, *, startup: bool = False) -> None:
        """Retry pending acceptance reactions.

        At startup no accepting path is still running, so every pending record
        is retried at once; afterwards a record waits for its retry_at.
        """
        acks = self.state.data.get("acceptance_reactions", {})
        if not getattr(self.config, "acceptance_reaction_enabled", False):
            # A receipt still pending while the switch is off would be sent
            # long after its message was accepted if the switch came back on.
            expired = [
                record
                for record in acks.values()
                if isinstance(record, dict) and record.get("phase") == "sending"
            ]
            for record in expired:
                record["phase"] = "expired"
                record["last_error"] = "acceptance receipt disabled"
                record.pop("retry_at", None)
            if expired:
                self.state.save()
            return
        now = time.time()
        for event_id, record in list(acks.items()):
            if (
                isinstance(record, dict)
                and record.get("phase") == "sending"
                and record.get("room_id")
                and (startup or float(record.get("retry_at") or 0) <= now)
            ):
                await self._acknowledge_acceptance(str(record["room_id"]), event_id)

    def _mark_prompt_admitted(
        self,
        request: dict[str, Any],
        turn_id: str,
    ) -> None:
        request["prompt_turn_id"] = turn_id
        request["admitted_at"] = time.time()
        self._record_latency(
            request,
            "fallback" if request.get("deferred_steering") else "prompt",
        )

    def _record_latency(self, request: dict[str, Any], kind: str) -> None:
        """Keep bounded intake, injection and admission timestamps.

        Admission is the bridge's observation of the transcript user record,
        so it includes up to one transcript poll interval.
        """
        if request.get("latency_recorded"):
            return
        request["latency_recorded"] = True
        acks = self.state.data.get("acceptance_reactions", {})
        entries = request.get("steering_entries") or [
            {
                "event_id": request.get("event_id"),
                "intake_at": request.get("intake_at"),
            }
        ]
        samples = self.state.data.setdefault("latency_samples", [])
        for entry in entries:
            event_id = str(entry.get("event_id"))
            ack = acks.get(event_id)
            samples.append(
                {
                    "event_id": event_id,
                    "request_id": request.get("id"),
                    "kind": kind,
                    "turn_id": (
                        request.get("injection_turn_id")
                        or request.get("prompt_turn_id")
                    ),
                    "intake_at": entry.get("intake_at"),
                    "injected_at": request.get("injected_at"),
                    "admitted_at": request.get("admitted_at"),
                    "acknowledged_at": (
                        ack.get("acknowledged_at") if isinstance(ack, dict) else None
                    ),
                }
            )
        self.state.data["latency_samples"] = samples[-MAX_LATENCY_SAMPLES:]

    async def _create_inbound_qitem(
        self,
        agent: AgentConfig,
        *,
        event_id: str,
        text: str,
        source_label: str,
    ) -> str | None:
        if not getattr(self.config, "queue_inbound_messages", False):
            return None
        source_session = str(getattr(self.config, "inbound_queue_source", "")).strip()
        if not source_session:
            raise GatewayError("inbound queue source is not configured")
        first_line = next(
            (line.strip() for line in text.splitlines() if line.strip()),
            f"Matrix {source_label}",
        )
        priority = str(getattr(self.config, "inbound_message_priority", "urgent"))
        qitem_id = await self.gateway.queue_create(
            source_session=source_session,
            destination_session=agent.session,
            summary=f"Matrix ({source_label}): {first_line[:180]}",
            body=(
                f"Matrix message for {agent.session}\n"
                f"Event: {event_id}\n"
                f"Priority: {priority}\n\n"
                f"{text}"
            ),
            priority=priority,
        )
        LOGGER.info(
            "created %s qitem %s for Matrix event %s",
            priority,
            qitem_id,
            event_id,
        )
        return qitem_id

    async def _claim_inbound_qitem(
        self,
        agent: AgentConfig,
        request: dict[str, Any],
    ) -> bool:
        qitem_ids = self._request_qitem_ids(request)
        if not qitem_ids:
            request["qitem_phase"] = "not_tracked"
            return True
        if self._request_terminal_qitem_phase(request):
            return False
        for qitem_id in qitem_ids:
            phase = self._request_qitem_phase(request, qitem_id)
            if phase == "claimed":
                continue
            if phase in TERMINAL_QITEM_PHASES:
                return False
            if phase in {"", "claiming"}:
                # An unknown phase is a request saved before phases existed (0.10);
                # its qitem may be closed, so read it before ever claiming.
                try:
                    item = await self.gateway.queue_show(qitem_id)
                except (GatewayError, OSError):
                    LOGGER.warning(
                        "could not inspect Matrix qitem %s",
                        qitem_id,
                        exc_info=True,
                    )
                    return False
                state = str(item.get("state") or "")
                waiting = item.get("waiting", {})
                owner = waiting.get("owner") if isinstance(waiting, dict) else None
                if state == "in-progress" and agent.session in {
                    item.get("claimed_by"),
                    owner,
                }:
                    self._set_request_qitem_phase(request, qitem_id, "claimed")
                    self.state.save()
                    continue
                if state in TERMINAL_QITEM_PHASES:
                    self._set_request_qitem_phase(request, qitem_id, state)
                    self.state.save()
                    return False
            self._set_request_qitem_phase(request, qitem_id, "claiming")
            self.state.save()
            try:
                await self.gateway.queue_claim(qitem_id, agent.session)
            except (GatewayError, OSError):
                LOGGER.warning(
                    "could not claim Matrix qitem %s",
                    qitem_id,
                    exc_info=True,
                )
                return False
            self._crash_after_effect("qitem_claim")
            self._set_request_qitem_phase(request, qitem_id, "claimed")
            self.state.save()
        return True

    async def _finish_inbound_qitem(
        self,
        agent: AgentConfig,
        request: dict[str, Any],
    ) -> bool:
        qitem_ids = self._request_qitem_ids(request)
        if not qitem_ids:
            request["qitem_phase"] = "not_tracked"
            return True
        for qitem_id in qitem_ids:
            phase = self._request_qitem_phase(request, qitem_id)
            if phase == "done":
                continue
            if phase in TERMINAL_QITEM_PHASES:
                return False
            # Reconcile before every attempt: the agent may have closed the row
            # itself, and a second state-writing update on a closed row is rejected.
            state = await self._inspect_qitem_state(qitem_id)
            if state is None:
                return False
            if state in TERMINAL_QITEM_PHASES:
                self._set_request_qitem_phase(request, qitem_id, state)
                self.state.save()
                if state == "done":
                    continue
                return False
            self._set_request_qitem_phase(request, qitem_id, "finishing")
            self.state.save()
            try:
                await self.gateway.queue_finish(
                    qitem_id,
                    agent.session,
                    "Matrix answer delivered",
                )
            except (GatewayError, OSError):
                # The row can close between the show and the update; re-read it
                # and accept a terminal state instead of reporting a failure.
                state = await self._inspect_qitem_state(qitem_id)
                if state in TERMINAL_QITEM_PHASES:
                    self._set_request_qitem_phase(request, qitem_id, state)
                    self.state.save()
                    if state == "done":
                        continue
                    return False
                LOGGER.warning(
                    "could not finish Matrix qitem %s",
                    qitem_id,
                    exc_info=True,
                )
                return False
            self._crash_after_effect("qitem_finish")
            self._set_request_qitem_phase(request, qitem_id, "done")
            self.state.save()
        return True

    async def _inspect_qitem_state(self, qitem_id: str) -> str | None:
        try:
            item = await self.gateway.queue_show(qitem_id)
        except (GatewayError, OSError):
            LOGGER.warning(
                "could not inspect Matrix qitem %s",
                qitem_id,
                exc_info=True,
            )
            return None
        return str(item.get("state") or "")

    async def _fail_inbound_qitem(
        self,
        agent: AgentConfig,
        request: dict[str, Any],
        resolution: str,
    ) -> bool:
        qitem_ids = self._request_qitem_ids(request)
        if not qitem_ids:
            request["qitem_phase"] = "not_tracked"
            return True
        for qitem_id in qitem_ids:
            phase = self._request_qitem_phase(request, qitem_id)
            if phase in TERMINAL_QITEM_PHASES:
                continue
            if phase == "failing":
                try:
                    item = await self.gateway.queue_show(qitem_id)
                except (GatewayError, OSError):
                    LOGGER.warning(
                        "could not inspect Matrix qitem %s",
                        qitem_id,
                        exc_info=True,
                    )
                    return False
                state = str(item.get("state") or "")
                if state in TERMINAL_QITEM_PHASES:
                    self._set_request_qitem_phase(request, qitem_id, state)
                    self.state.save()
                    if state == "failed":
                        continue
                    return False
            self._set_request_qitem_phase(request, qitem_id, "failing")
            self.state.save()
            try:
                await self.gateway.queue_fail(
                    qitem_id,
                    agent.session,
                    resolution[:1000],
                )
            except (GatewayError, OSError):
                LOGGER.warning(
                    "could not fail Matrix qitem %s",
                    qitem_id,
                    exc_info=True,
                )
                return False
            self._crash_after_effect("qitem_fail")
            self._set_request_qitem_phase(request, qitem_id, "failed")
            self.state.save()
        return True

    @staticmethod
    def _request_qitem_ids(request: dict[str, Any]) -> list[str]:
        values = request.get("qitem_ids")
        if isinstance(values, list):
            ids = [str(value) for value in values if str(value).strip()]
        else:
            primary = str(request.get("qitem_id") or "").strip()
            ids = [primary] if primary else []
        return list(dict.fromkeys(ids))

    @staticmethod
    def _request_qitem_phase(request: dict[str, Any], qitem_id: str) -> str:
        phases = request.get("qitem_phases")
        if isinstance(phases, dict) and qitem_id in phases:
            return str(phases[qitem_id] or "")
        if qitem_id == request.get("qitem_id"):
            return str(request.get("qitem_phase") or "")
        return ""

    @staticmethod
    def _set_request_qitem_phase(
        request: dict[str, Any],
        qitem_id: str,
        phase: str,
    ) -> None:
        phases = request.setdefault("qitem_phases", {})
        phases[qitem_id] = phase
        if qitem_id == request.get("qitem_id"):
            request["qitem_phase"] = phase

    @classmethod
    def _request_terminal_qitem_phase(
        cls,
        request: dict[str, Any],
    ) -> str | None:
        for qitem_id in cls._request_qitem_ids(request):
            phase = cls._request_qitem_phase(request, qitem_id)
            if phase in TERMINAL_QITEM_PHASES:
                return phase
        return None

    @classmethod
    def _is_legacy_pending_request(cls, request: dict[str, Any]) -> bool:
        """True for a request saved by bridge 0.10, before phases were recorded."""
        return bool(
            cls._request_qitem_ids(request)
            and request.get("prompt_phase") is None
            and request.get("answer_phase") is None
            and not request.get("ambiguous_prompt_legacy")
            and not request.get("terminal_error")
        )

    async def _reconcile_legacy_pending(self) -> None:
        """Adopt the remote qitem state of every 0.10 request before any action.

        A request whose qitems are all closed is dropped without replaying its
        prompt or answer; a request that is still open is left to the normal
        reconciliation, which keeps the ambiguous-prompt fail-closed behavior.
        """
        for agent in self.config.agents.values():
            async with self.locks[agent.key]:
                pending = self.state.data["pending"].get(agent.key, [])
                for request in list(pending):
                    if self._is_legacy_pending_request(request):
                        await self._reconcile_legacy_request(
                            agent,
                            pending,
                            request,
                        )

    async def _reconcile_legacy_request(
        self,
        agent: AgentConfig,
        pending: list[dict[str, Any]],
        request: dict[str, Any],
    ) -> None:
        states: dict[str, str] = {}
        for qitem_id in self._request_qitem_ids(request):
            state = await self._inspect_qitem_state(qitem_id)
            if state is None:
                return
            states[qitem_id] = state
        terminal = {
            qitem_id: state
            for qitem_id, state in states.items()
            if state in TERMINAL_QITEM_PHASES
        }
        if not terminal:
            return
        for qitem_id, state in terminal.items():
            self._set_request_qitem_phase(request, qitem_id, state)
        self.state.save()
        room_id = self._room_for_agent(agent.key)
        if set(terminal.values()) == {"done"} and len(terminal) == len(states):
            request["terminal_error"] = "Legacy request already completed"
            pending[:] = [item for item in pending if item is not request]
            self.state.save()
            await self._update_queue_notice(
                agent,
                room_id,
                max(0, len(pending) - 1),
            )
            return
        await self._abandon_terminal_qitem(
            agent,
            request,
            room_id,
            start_next=False,
            best_effort_notice=True,
        )

    async def _reconcile_pending_operations(self) -> None:
        await self._reconcile_legacy_pending()
        for agent in self.config.agents.values():
            async with self.locks[agent.key]:
                pending = self.state.data["pending"].get(agent.key, [])
                if not pending:
                    continue
                request = pending[0]
                terminal_intent = request.get("activity_terminal_intent")
                if isinstance(terminal_intent, dict):
                    acknowledged = await self._finalize_activity_card(
                        agent,
                        request,
                        self._room_for_agent(agent.key),
                        str(
                            terminal_intent.get("label")
                            or "Fehlgeschlagen"
                        ),
                        disposition=str(
                            terminal_intent.get("disposition")
                            or "delivery_failed"
                        ),
                        status_detail=str(
                            terminal_intent.get("status_detail") or ""
                        ),
                    )
                    if acknowledged:
                        await self._advance_terminal_request(
                            agent,
                            request,
                            self._room_for_agent(agent.key),
                            disposition=str(
                                terminal_intent.get("disposition")
                                or "delivery_failed"
                            ),
                            status_detail=str(
                                terminal_intent.get("status_detail") or ""
                            ),
                        )
                    continue
                request_qitems = self._request_qitem_ids(request)
                request_phases = {
                    self._request_qitem_phase(request, qitem_id)
                    for qitem_id in request_qitems
                }
                if (
                    request_qitems
                    and request_phases
                    <= {
                        "pending",
                        "claiming",
                    }
                    and not await self._claim_inbound_qitem(agent, request)
                ):
                    if self._request_terminal_qitem_phase(request):
                        await self._abandon_terminal_qitem(
                            agent,
                            request,
                            self._room_for_agent(agent.key),
                        )
                    continue
                if self._request_terminal_qitem_phase(request):
                    await self._abandon_terminal_qitem(
                        agent,
                        request,
                        self._room_for_agent(agent.key),
                    )
                    continue
                if (
                    request.get("prompt_phase") is None
                    and request.get("started_at") is not None
                ):
                    await self._fail_ambiguous_prompt(agent, request)
                    continue
                if request.get("prompt_phase") in {None, "pending"}:
                    await self._start_request(
                        agent,
                        request,
                        self._room_for_agent(agent.key),
                    )
                    continue
                if request.get("prompt_phase") == "sending":
                    await self._fail_ambiguous_prompt(agent, request)
                    continue
                if request.get("answer_phase") == "failed":
                    if (
                        getattr(self.config, "mirror_commentary", False)
                        and request.get("commentary_event_id")
                        and not request.get("suppress_reply")
                    ):
                        acknowledged = await self._finalize_activity_card(
                            agent,
                            request,
                            self._room_for_agent(agent.key),
                            "Fehlgeschlagen",
                            disposition="delivery_failed",
                            status_detail="Zustellung fehlgeschlagen",
                        )
                        if not acknowledged:
                            continue
                    await self._complete_failed_answer(agent, request, pending)
                    continue
                if (
                    request.get("prompt_phase") == "sent"
                    and request.get("answer_phase") == "sent"
                ):
                    if (
                        getattr(self.config, "mirror_commentary", False)
                        and request.get("commentary_event_id")
                        and not request.get("suppress_reply")
                    ):
                        acknowledged = await self._finalize_activity_card(
                            agent,
                            request,
                            self._room_for_agent(agent.key),
                            "Abgeschlossen",
                            disposition="completed",
                            status_detail="bereit",
                        )
                        if not acknowledged:
                            continue
                    await self._advance_terminal_request(
                        agent,
                        request,
                        self._room_for_agent(agent.key),
                        disposition="completed",
                        status_detail="bereit",
                    )

    @staticmethod
    def _request_matches_identity(
        candidate: dict[str, Any],
        request: dict[str, Any],
    ) -> bool:
        if candidate is request:
            return True
        request_id = request.get("id")
        return request_id is not None and candidate.get("id") == request_id

    @classmethod
    def _is_pending_request_head(
        cls,
        pending: list[dict[str, Any]],
        request: dict[str, Any],
    ) -> bool:
        return bool(
            pending
            and cls._request_matches_identity(pending[0], request)
        )

    async def _fail_ambiguous_prompt(
        self,
        agent: AgentConfig,
        request: dict[str, Any],
    ) -> None:
        request["ambiguous_prompt_legacy"] = True
        resolution = "Prompt delivery outcome was uncertain after bridge restart"
        qitem_id = str(request.get("qitem_id") or "")
        if qitem_id:
            try:
                item = await self.gateway.queue_show(qitem_id)
            except (GatewayError, OSError, AttributeError):
                item = {}
            if item.get("state") == "done":
                request["qitem_phase"] = "done"
                request["terminal_error"] = "Legacy request already completed"
                pending = self.state.data["pending"][agent.key]
                if self._is_pending_request_head(pending, request):
                    pending.pop(0)
                self.state.save()
                return

        closed = await self._fail_inbound_qitem(agent, request, resolution)
        if not request.get("failure_notice_event_id"):
            response = await self._deliver_to_request(
                self._room_for_agent(agent.key),
                "Der Prompt-Versand ist nach dem Neustart nicht eindeutig. "
                "Die Anfrage wird nicht erneut gesendet.",
                request,
                tx_id=f"failure-{request.get('id') or request['event_id']}",
            )
            if isinstance(response, RoomSendResponse):
                request["failure_notice_event_id"] = response.event_id
                self.state.save()
            else:
                self.state.save()
                return
        request["terminal_error"] = resolution
        if not closed:
            self.state.save()
            return
        pending = self.state.data["pending"][agent.key]
        if self._is_pending_request_head(pending, request):
            pending.pop(0)
        self.state.save()

    async def _complete_failed_answer(
        self,
        agent: AgentConfig,
        request: dict[str, Any],
        pending: list[dict[str, Any]],
    ) -> bool:
        resolution = str(
            request.get("terminal_error")
            or "Matrix answer delivery failed before restart"
        )
        if await self._fail_inbound_qitem(agent, request, resolution) is False:
            await self._abandon_if_terminal(
                agent,
                request,
                self._room_for_agent(agent.key),
            )
            return False
        if not request.get("failure_notice_event_id"):
            response = await self._deliver_to_request(
                self._room_for_agent(agent.key),
                f"Die Matrix-Antwort konnte nicht zugestellt werden: {resolution}",
                request,
                tx_id=f"failure-{request.get('id') or request['event_id']}",
            )
            if not isinstance(response, RoomSendResponse):
                return False
            request["failure_notice_event_id"] = response.event_id
        if self._is_pending_request_head(pending, request):
            pending.pop(0)
        self.state.save()
        return True

    async def _abandon_terminal_qitem(
        self,
        agent: AgentConfig,
        request: dict[str, Any],
        room_id: str,
        *,
        start_next: bool = True,
        best_effort_notice: bool = False,
    ) -> None:
        terminal_phases = {
            qitem_id: self._request_qitem_phase(request, qitem_id)
            for qitem_id in self._request_qitem_ids(request)
            if self._request_qitem_phase(request, qitem_id)
            in TERMINAL_QITEM_PHASES
        }
        request["terminal_error"] = (
            "OpenRig batch ended before Matrix delivery: "
            + ", ".join(
                f"{qitem_id}={phase}"
                for qitem_id, phase in terminal_phases.items()
            )
        )
        if not await self._fail_inbound_qitem(
            agent,
            request,
            request["terminal_error"],
        ):
            self.state.save()
            return
        qitem_ids = ", ".join(self._request_qitem_ids(request))
        phase_summary = ", ".join(
            f"{qitem_id}={self._request_qitem_phase(request, qitem_id)}"
            for qitem_id in self._request_qitem_ids(request)
        )
        if not request.get("failure_notice_event_id"):
            response = await self._send(
                room_id,
                f"OpenRig Queue-Item(s) {qitem_ids} sind terminal "
                f"({phase_summary}). Die Anfrage wird nicht gesendet.",
                self._reply_target(request),
                tx_id=f"failure-{request.get('id') or request['event_id']}",
                **self._delivery_kwargs(request),
            )
            if isinstance(response, RoomSendResponse):
                request["failure_notice_event_id"] = response.event_id
            else:
                self.state.save()
                if not best_effort_notice:
                    return
                # The notice is courtesy only; a request that can never be
                # answered must not stay queued and retry forever.
                LOGGER.warning(
                    "could not post the terminal-qitem notice for %s; "
                    "dropping the request anyway",
                    request.get("id"),
                )
        pending = self.state.data["pending"][agent.key]
        was_head = bool(
            pending
            and self._request_matches_identity(pending[0], request)
        )
        if any(
            self._request_matches_identity(item, request)
            for item in pending
        ):
            pending[:] = [
                item
                for item in pending
                if not self._request_matches_identity(item, request)
            ]
            self.state.save()
            await self._update_queue_notice(
                agent,
                room_id,
                max(0, len(pending) - 1),
            )
            if was_head and pending and start_next:
                await self._start_request(agent, pending[0], room_id)

    async def _abandon_if_terminal(
        self,
        agent: AgentConfig,
        request: dict[str, Any],
        room_id: str,
    ) -> bool:
        if self._request_terminal_qitem_phase(request) is None:
            return False
        await self._abandon_terminal_qitem(agent, request, room_id)
        return True

    def _burst_settings(self) -> tuple[float, float]:
        return (
            float(getattr(self.config, "burst_inactivity_seconds", 45)),
            float(getattr(self.config, "burst_max_span_seconds", 300)),
        )

    def _intake_part(
        self,
        agent: AgentConfig,
        room_id: str,
        event: Any,
        kind: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Give an inbound event its place in a burst, in timeline order.

        Handlers call this before their first await, so the order of parts is
        the order nio delivered the events, not the order their downloads or
        transcriptions finish. The caller saves state.
        """
        relation = message_relation(event)
        target = resolve_response_target(event.event_id, relation)
        inactivity, max_span = self._burst_settings()
        burst, part = assign_part(
            self.state.data.setdefault("bursts", {}),
            agent_key=agent.key,
            room_id=room_id,
            sender=str(event.sender),
            event_id=event.event_id,
            kind=kind,
            server_ts=int(event.server_timestamp),
            relation=relation,
            thread_root_event_id=target.thread_root_event_id,
            reply_to_event_id=target.reply_to_event_id,
            inactivity_seconds=inactivity,
            max_span_seconds=max_span,
        )
        part["relates_to"] = relation
        return burst, part

    def _mark_processed(self, event_id: str) -> None:
        self.processed_events.add(event_id)
        self.state.data["processed_events"] = list(self.processed_events)[-2000:]

    def _processing_part(
        self,
        event_id: str,
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        for burst in self.state.data.get("bursts", {}).values():
            for part in burst.get("parts", []):
                if part.get("event_id") == event_id:
                    return (burst, part) if part.get("phase") == "processing" else None
        return None

    async def _fail_event_part(self, event_id: str, error: object) -> None:
        found = self._processing_part(event_id)
        if found is not None:
            await self._fail_part(found[0]["id"], event_id, error)

    def _burst_part(
        self,
        burst_id: str,
        event_id: str,
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        burst = self.state.data.get("bursts", {}).get(burst_id)
        if not isinstance(burst, dict):
            return None
        part = next(
            (item for item in burst["parts"] if item.get("event_id") == event_id),
            None,
        )
        return (burst, part) if part is not None else None

    async def _complete_part(
        self,
        burst_id: str,
        event_id: str,
        text: str,
        source_label: str,
    ) -> None:
        found = self._burst_part(burst_id, event_id)
        if found is None:
            return
        _burst, part = found
        part.update(phase="ready", text=text, label=source_label)
        part.pop("media", None)
        self._mark_processed(event_id)
        self.state.save()
        await self._advance_burst(burst_id)

    async def _fail_part(self, burst_id: str, event_id: str, error: object) -> None:
        found = self._burst_part(burst_id, event_id)
        if found is None:
            return
        _burst, part = found
        part.update(phase="failed", error=display_text(error, 300))
        part.pop("media", None)
        self._mark_processed(event_id)
        self.state.save()
        await self._advance_burst(burst_id)

    async def _advance_burst(self, burst_id: str) -> None:
        burst = self.state.data.get("bursts", {}).get(burst_id)
        if not isinstance(burst, dict):
            return
        agent = self.config.agents.get(str(burst.get("agent_key")))
        if agent is None or not self._burst_has_delivery(burst):
            return
        async with self.locks[agent.key]:
            await self._advance_burst_locked(agent, burst)

    @staticmethod
    def _burst_has_delivery(burst: dict[str, Any]) -> bool:
        """Whether the burst has settled parts that may reach the agent now.

        Failures alone wait: their Matrix notices went out, and the agent hears
        about them with the burst's first usable part.
        """
        new = deliverable_parts(burst)
        return any(part["phase"] == "ready" for part in new) or (
            bool(new)
            and any(
                part.get("phase") in {"delivered", "reported"}
                for part in burst["parts"]
            )
        )

    async def _advance_burst_locked(
        self,
        agent: AgentConfig,
        burst: dict[str, Any],
    ) -> None:
        """Deliver the burst's settled parts in order, as soon as they may go.

        Text-only bursts keep per-message delivery. A burst with an attachment
        is one linked context: new parts join the burst's request that has not
        been sent yet, steer into its active turn, or become a continuation
        that restates every earlier part.
        """
        if not self._burst_has_delivery(burst):
            return
        new = deliverable_parts(burst)
        delivered = [
            part
            for part in burst["parts"]
            if part.get("phase") in {"delivered", "reported"}
        ]
        room_id = str(burst["room_id"])
        intake_at = time.time()
        if not is_linked(burst):
            for part in new:
                if part["phase"] == "failed":
                    # The owner was told it was not sent; plain chat keeps that.
                    part["phase"] = "reported"
                    self.state.save()
                    continue
                await self._enqueue_locked(
                    agent,
                    room_id,
                    [(part["event_id"], str(part.get("text") or ""))],
                    str(part.get("text") or ""),
                    str(part.get("label") or "message"),
                    resolve_response_target(part["event_id"], part.get("relates_to")),
                    relates_to=part.get("relates_to"),
                    intake_at=intake_at,
                    burst=burst,
                    burst_parts=[part],
                )
            return

        inactivity, _max_span = self._burst_settings()
        new_ids = [part["event_id"] for part in new]
        own_texts = [(part["event_id"], self._part_text(part)) for part in new]
        pending = self.state.data["pending"].setdefault(agent.key, [])
        live = next(
            (
                request
                for request in reversed(pending)
                if request.get("burst_id") == burst["id"]
                and not request.get("deferred_steering")
            ),
            None,
        )
        if (
            live is not None
            and live.get("prompt_phase") == "pending"
            and live.get("started_at") is None
        ):
            await self._merge_into_request(
                agent,
                burst,
                live,
                new,
                own_texts,
                inactivity=inactivity,
            )
            return
        if not delivered and len(new) == 1:
            part = new[0]
            text = str(part.get("text") or "")
            label = str(part.get("label") or "message")
        else:
            text = render_burst(burst, new_ids, inactivity_seconds=inactivity)
            label = "related"
        await self._enqueue_locked(
            agent,
            room_id,
            own_texts,
            text,
            label,
            ResponseTarget(
                burst.get("thread_root_event_id"),
                str(burst.get("reply_to_event_id") or burst["anchor_event_id"]),
            ),
            relates_to=new[0].get("relates_to"),
            intake_at=intake_at,
            linked=True,
            burst=burst,
            burst_parts=new,
        )

    @staticmethod
    def _part_text(part: dict[str, Any]) -> str:
        if part.get("phase") == "failed":
            label = KIND_LABELS.get(str(part.get("kind")), "Nachricht")
            return (
                f"{label} {part['event_id']} konnte nicht übernommen werden: "
                f"{part.get('error')}"
            )
        return str(part.get("text") or "")

    async def _merge_into_request(
        self,
        agent: AgentConfig,
        burst: dict[str, Any],
        request: dict[str, Any],
        new: list[dict[str, Any]],
        own_texts: list[tuple[str, str]],
        *,
        inactivity: float,
    ) -> None:
        """Add parts to a request that has not reached the agent yet."""
        room_id = str(burst["room_id"])
        qitem_ids: list[str] = []
        if request.get("queue_tracked", True):
            try:
                for event_id, text in own_texts:
                    qitem_id = await self._create_inbound_qitem(
                        agent,
                        event_id=event_id,
                        text=text,
                        source_label="related",
                    )
                    if qitem_id:
                        qitem_ids.append(qitem_id)
            except (GatewayError, OSError) as exc:
                LOGGER.exception("could not create durable qitem for a related part")
                for part in new:
                    part["phase"] = "failed"
                    part["error"] = "OpenRig-Queue-Item konnte nicht angelegt werden"
                self.state.save()
                await self._send(
                    room_id,
                    "OpenRig konnte das Queue-Item für diese Anfrage nicht "
                    f"anlegen. Die Anfrage wurde nicht an den Agenten gesendet: {exc}",
                    self._reply_target(request),
                    **self._delivery_kwargs(request),
                )
                return
        new_ids = [event_id for event_id, _text in own_texts]
        for event_id in new_ids:
            self.processed_events.add(event_id)
        self.state.data["processed_events"] = list(self.processed_events)[-2000:]
        carried = [*request.get("burst_part_ids", []), *new_ids]
        request["burst_part_ids"] = carried
        request["source_event_ids"] = list(
            dict.fromkeys([*request.get("source_event_ids", []), *new_ids])
        )
        request["event_id"] = new_ids[-1]
        if qitem_ids:
            if not request.get("qitem_id"):
                request["qitem_id"] = qitem_ids[0]
                request["qitem_phase"] = "pending"
            request["qitem_ids"] = [*self._request_qitem_ids(request), *qitem_ids]
            phases = request.setdefault("qitem_phases", {})
            for qitem_id in qitem_ids:
                phases[qitem_id] = "pending"
        for part in new:
            part["phase"] = "reported" if part.get("phase") == "failed" else "delivered"
            part["request_id"] = request["id"]
        request["text"] = render_burst(burst, carried, inactivity_seconds=inactivity)
        request["source_label"] = "related"
        acknowledge = [
            event_id
            for event_id in new_ids
            if self._record_acceptance(
                room_id,
                event_id,
                queue_tracked=bool(request.get("queue_tracked", True)),
                suppress_reply=bool(request.get("suppress_reply")),
            )
        ]
        self.state.save()
        LOGGER.info(
            "merged Matrix events %s into the waiting request for %s",
            ", ".join(new_ids),
            agent.session,
        )
        for event_id in acknowledge:
            await self._acknowledge_acceptance(room_id, event_id)

    def _steer_burst_parts(
        self,
        agent: AgentConfig,
        pending: list[dict[str, Any]],
        request: dict[str, Any],
        burst: dict[str, Any],
    ) -> None:
        """Steer new burst parts as one entry that restates the whole burst.

        Pane input may be admitted into the active turn, materialize as the
        next turn, or fall back to its own turn; in every case the agent sees
        the complete related context and the answer names every source event.
        A burst keeps one entry per batch, updated in place as parts arrive.
        """
        inactivity, _max_span = self._burst_settings()
        group = self._steering_group(pending, pending[0])
        entry = next(
            (
                item
                for item in (group or {}).get("steering_entries", [])
                if item.get("burst_id") == burst["id"]
            ),
            None,
        )
        new_ids = [*(entry or {}).get("new_event_ids", []), *request["burst_part_ids"]]
        text = render_burst(burst, new_ids, inactivity_seconds=inactivity)
        if group is not None and entry is not None:
            entry.update(event_id=new_ids[-1], text=text, new_event_ids=new_ids)
            self._extend_steering_group(group, request)
            group["text"] = steering_batch_text(
                str(pending[0]["thread_root_event_id"]),
                group["steering_entries"],
            )
            return
        self._append_steering_followup(
            agent,
            pending,
            {**request, "id": uuid.uuid4().hex},
            event_id=new_ids[-1],
            text=text,
        )
        group = self._steering_group(pending, pending[0])
        if group is not None:
            group["steering_entries"][-1].update(
                burst_id=burst["id"],
                new_event_ids=new_ids,
            )

    def _extend_steering_group(
        self,
        group: dict[str, Any],
        incoming: dict[str, Any],
    ) -> None:
        group["source_event_ids"] = list(
            dict.fromkeys(
                [
                    *group.get("source_event_ids", []),
                    *incoming.get("source_event_ids", [incoming["event_id"]]),
                ]
            )
        )
        qitem_ids = group.setdefault("qitem_ids", [])
        phases = group.setdefault("qitem_phases", {})
        for qitem_id in self._request_qitem_ids(incoming):
            if qitem_id not in qitem_ids:
                qitem_ids.append(qitem_id)
                phases[qitem_id] = "pending"

    async def _resume_bursts(self) -> None:
        """Finish parts a restart interrupted, then deliver what is settled."""
        bursts = self.state.data.get("bursts", {})
        for burst_id, burst in list(bursts.items()):
            for part in list(burst.get("parts", [])):
                if part.get("phase") != "processing":
                    continue
                if part.get("kind") in {"image", "file"} and isinstance(
                    part.get("media"), dict
                ):
                    await self._process_media_part(burst_id, part["event_id"])
                elif part.get("kind") == "voice":
                    await self._fail_part(
                        burst_id,
                        part["event_id"],
                        VOICE_STATUS_INTERRUPTED,
                    )
                else:
                    await self._fail_part(
                        burst_id,
                        part["event_id"],
                        "Verarbeitung wurde durch einen Bridge-Neustart unterbrochen",
                    )
        for burst_id in list(bursts):
            await self._advance_burst(burst_id)

    async def _handle_audio(
        self,
        room: MatrixRoom,
        event: RoomMessageAudio | RoomEncryptedAudio,
    ) -> None:
        if event.event_id in self.processed_events:
            return
        if not is_voice_message_content(event.source.get("content", {})):
            await self._handle_file(room, event)
            return
        if event.sender == self.config.user_id:
            return
        agent = self._agent_for_room(room.room_id)
        if not agent:
            return
        if event.sender not in self.config.allowed_users:
            LOGGER.warning(
                "ignoring Matrix sender %s in %s", event.sender, room.room_id
            )
            return
        if self.config.max_message_age_minutes >= 0:
            age_ms = int(time.time() * 1000) - int(event.server_timestamp)
            if age_ms > self.config.max_message_age_minutes * 60_000:
                await self._send_to_event(
                    room.room_id,
                    "Sprachnachricht ist zu alt und wurde nicht verarbeitet.",
                    event,
                )
                return
        if not self.config.transcription.enabled:
            await self._send_to_event(
                room.room_id,
                "Sprachnachrichten sind auf dieser Bridge deaktiviert.",
                event,
            )
            return

        target = resolve_response_target(event.event_id, message_relation(event))
        voice_statuses = self.state.data.setdefault("voice_statuses", {})
        voice_statuses[event.event_id] = {
            "room_id": room.room_id,
            "status_event_id": None,
            "status_tx_id": voice_transaction_id(event.event_id, "status"),
            "thread_root_event_id": target.thread_root_event_id,
            "reply_to_event_id": target.reply_to_event_id,
            "state": "sending-status",
            "updated_at": int(time.time() * 1000),
        }
        self._intake_part(agent, room.room_id, event, "voice")
        self.processed_events.add(event.event_id)
        self.state.data["processed_events"] = list(self.processed_events)[-2000:]
        self.state.save()
        if not await self._send_voice_status(event.event_id):
            await self._finish_voice_status(
                event.event_id,
                "Sprachnachricht-Status konnte nicht erstellt werden. "
                "Bitte erneut senden.",
                "failed",
            )
            await self._fail_event_part(
                event.event_id,
                "Sprachnachricht-Status konnte nicht erstellt werden",
            )
            return
        try:
            download = await self.client.download(mxc=event.url)
            body = getattr(download, "body", None)
            if not isinstance(body, bytes):
                raise ConnectionError(str(download))
            if len(body) > self.config.transcription.max_audio_bytes:
                raise RuntimeError("Sprachnachricht überschreitet das Größenlimit")
            audio = body
            if isinstance(event, RoomEncryptedAudio):
                audio = decrypt_attachment(
                    body,
                    event.key["k"],
                    event.hashes["sha256"],
                    event.iv,
                )
            audio_digest = hashlib.sha256(audio).hexdigest()
            text = await asyncio.to_thread(
                self.transcriber.transcribe, audio, event.body
            )
        except Exception as exc:
            LOGGER.exception("audio transcription failed")
            await self._finish_voice_status(
                event.event_id,
                f"Sprachnachricht konnte nicht transkribiert werden: {exc}",
                "failed",
            )
            await self._fail_event_part(
                event.event_id,
                f"Sprachnachricht konnte nicht transkribiert werden: {exc}",
            )
            return
        if not text:
            await self._finish_voice_status(
                event.event_id,
                "In der Sprachnachricht wurde kein Text erkannt.",
                "no-speech",
            )
            await self._fail_event_part(
                event.event_id,
                "In der Sprachnachricht wurde kein Text erkannt",
            )
            return
        await self._finish_voice_status(
            event.event_id,
            f"Transkript:\n{text}",
            "done",
        )
        await self._enqueue_prompt(
            agent,
            room,
            event.event_id,
            event.server_timestamp,
            (
                f"Sprachnachricht von {event.sender} via Matrix (lokal transkribiert, "
                f"Audio-SHA-256: {audio_digest}):\n\n{text}"
            ),
            "audio",
            relates_to=message_relation(event),
        )

    async def _recover_voice_statuses(self) -> None:
        statuses = self.state.data.setdefault("voice_statuses", {})
        for voice_event_id, record in list(statuses.items()):
            if not isinstance(record, dict):
                continue
            if record.get("state") in {
                "starting",
                "sending-status",
                "status-send-failed",
                "processing",
                "recovering",
                "committing",
                "fallbacking",
                "fallback-failed",
            }:
                await self._recover_voice_status(str(voice_event_id))

    async def _recover_voice_status(self, voice_event_id: str) -> None:
        record = self.state.data.setdefault("voice_statuses", {}).get(voice_event_id)
        if not isinstance(record, dict):
            return
        if record.get("state") not in {
            "starting",
            "sending-status",
            "status-send-failed",
            "processing",
            "recovering",
            "committing",
            "fallbacking",
            "fallback-failed",
        }:
            return
        room_id = str(record.get("room_id") or "")
        if not room_id:
            record["state"] = "interrupted"
            record["updated_at"] = int(time.time() * 1000)
            self.state.save()
            return

        intent = record.get("terminal_intent")
        if isinstance(intent, dict):
            await self._commit_voice_terminal(voice_event_id)
            return

        state = str(record.get("state") or "")
        if (
            state in {"starting", "sending-status", "status-send-failed"}
            and not record.get("status_event_id")
            and not await self._send_voice_status(voice_event_id)
        ):
            await self._start_voice_terminal(
                voice_event_id,
                VOICE_STATUS_INTERRUPTED,
                "interrupted",
            )
            return

        if state in {"committing", "fallbacking"}:
            intent = self._voice_terminal_intent(
                voice_event_id,
                VOICE_STATUS_INTERRUPTED
                + " Der vorherige Status konnte nicht sicher bestimmt werden.",
                "interrupted",
                skip_edit=True,
            )
            record["terminal_intent"] = intent
            record["state"] = "committing"
            record["updated_at"] = int(time.time() * 1000)
            self.state.save()
            await self._commit_voice_terminal(voice_event_id)
            return

        await self._start_voice_terminal(
            voice_event_id,
            VOICE_STATUS_INTERRUPTED,
            "interrupted",
        )

    async def _send_voice_status(self, voice_event_id: str) -> bool:
        record = self.state.data.setdefault("voice_statuses", {}).get(voice_event_id)
        if not isinstance(record, dict):
            return False
        room_id = str(record.get("room_id") or "")
        try:
            response = await self._send(
                room_id,
                VOICE_STATUS_PROGRESS,
                str(record.get("reply_to_event_id") or voice_event_id),
                thread_root=record.get("thread_root_event_id"),
                tx_id=record.get("status_tx_id"),
            )
        except Exception:
            response = None
            LOGGER.warning(
                "failed to create voice status for %s",
                voice_event_id,
                exc_info=True,
            )
        if not isinstance(response, RoomSendResponse):
            record["state"] = "status-send-failed"
            record["updated_at"] = int(time.time() * 1000)
            self.state.save()
            LOGGER.warning(
                "failed to create voice status for %s: %s",
                voice_event_id,
                response,
            )
            return False
        record["status_event_id"] = response.event_id
        record["state"] = "processing"
        record["updated_at"] = int(time.time() * 1000)
        self.state.save()
        return True

    @staticmethod
    def _voice_terminal_intent(
        voice_event_id: str,
        text: str,
        terminal_state: str,
        *,
        skip_edit: bool = False,
    ) -> dict[str, Any]:
        if terminal_state == "done":
            fallback_text = (
                "Transkript (Statusmeldung konnte nicht bearbeitet werden):\n"
                f"{text.removeprefix('Transkript:').lstrip()}"
            )
        else:
            fallback_text = f"{text}\n\n(Statusmeldung konnte nicht bearbeitet werden.)"
        return {
            "text": text,
            "terminal_state": terminal_state,
            "edit_tx_id": voice_transaction_id(
                voice_event_id,
                f"edit-{terminal_state}",
            ),
            "fallback_text": fallback_text,
            "fallback_tx_id": voice_transaction_id(
                voice_event_id,
                f"fallback-{terminal_state}",
            ),
            "skip_edit": skip_edit,
        }

    async def _start_voice_terminal(
        self,
        voice_event_id: str,
        text: str,
        terminal_state: str,
        *,
        skip_edit: bool = False,
    ) -> None:
        record = self.state.data.setdefault("voice_statuses", {}).get(voice_event_id)
        if not isinstance(record, dict):
            return
        record["terminal_intent"] = self._voice_terminal_intent(
            voice_event_id,
            text,
            terminal_state,
            skip_edit=skip_edit,
        )
        record["state"] = "committing"
        record["updated_at"] = int(time.time() * 1000)
        self.state.save()
        await self._commit_voice_terminal(voice_event_id)

    async def _commit_voice_terminal(self, voice_event_id: str) -> None:
        record = self.state.data.setdefault("voice_statuses", {}).get(voice_event_id)
        if not isinstance(record, dict):
            return
        intent = record.get("terminal_intent")
        if not isinstance(intent, dict):
            return
        room_id = str(record.get("room_id") or "")
        status_event_id = str(record.get("status_event_id") or "")
        terminal_state = str(intent.get("terminal_state") or "failed")
        response = None
        edit_already_rejected = record.get("state") in {
            "fallbacking",
            "fallback-failed",
        }
        if (
            status_event_id
            and not intent.get("skip_edit")
            and not edit_already_rejected
        ):
            try:
                response = await self._edit_message(
                    room_id,
                    status_event_id,
                    str(intent.get("text") or ""),
                    tx_id=str(intent.get("edit_tx_id") or ""),
                )
            except Exception:
                LOGGER.warning(
                    "failed to edit voice status for %s",
                    voice_event_id,
                    exc_info=True,
                )
        if isinstance(response, RoomSendResponse):
            record["state"] = terminal_state
            record["result"] = terminal_state
            record.pop("terminal_intent", None)
            record["updated_at"] = int(time.time() * 1000)
            self.state.save()
            return

        LOGGER.warning(
            "voice status edit rejected for %s; sending visible fallback",
            voice_event_id,
        )
        record["state"] = "fallbacking"
        record["updated_at"] = int(time.time() * 1000)
        self.state.save()
        try:
            response = await self._send(
                room_id,
                str(intent.get("fallback_text") or ""),
                str(record.get("reply_to_event_id") or voice_event_id),
                thread_root=record.get("thread_root_event_id"),
                tx_id=str(intent.get("fallback_tx_id") or ""),
            )
        except Exception:
            LOGGER.warning(
                "voice status fallback failed for %s",
                voice_event_id,
                exc_info=True,
            )
            response = None
        if isinstance(response, RoomSendResponse):
            record["state"] = "fallback"
            record["result"] = terminal_state
            record.pop("terminal_intent", None)
        else:
            record["state"] = "fallback-failed"
            record["result"] = terminal_state
        record["updated_at"] = int(time.time() * 1000)
        self.state.save()

    async def _finish_voice_status(
        self,
        voice_event_id: str,
        text: str,
        terminal_state: str,
    ) -> None:
        await self._start_voice_terminal(
            voice_event_id,
            text,
            terminal_state,
        )

    async def _handle_image(
        self,
        room: MatrixRoom,
        event: RoomMessageImage | RoomEncryptedImage,
    ) -> None:
        if not self.config.media.enabled:
            return
        if event.sender == self.config.user_id:
            return
        agent = self._agent_for_room(room.room_id)
        if agent is None or event.sender not in self.config.allowed_users:
            return
        if event.event_id in self.processed_events:
            return
        if self.config.max_message_age_minutes >= 0:
            age_ms = int(time.time() * 1000) - int(event.server_timestamp)
            if age_ms > self.config.max_message_age_minutes * 60_000:
                await self._send_to_event(
                    room.room_id,
                    "Bild ist zu alt und wurde nicht verarbeitet.",
                    event,
                )
                return

        self._intake_media(
            agent,
            room.room_id,
            event,
            "image",
            encrypted=isinstance(event, RoomEncryptedImage),
        )
        await self._send_to_event(
            room.room_id,
            "Bild empfangen, lade und entschlüssele …",
            event,
        )
        await self._store_image(
            agent,
            room.room_id,
            event,
            encrypted=isinstance(event, RoomEncryptedImage),
        )

    def _intake_media(
        self,
        agent: AgentConfig,
        room_id: str,
        event: Any,
        kind: str,
        *,
        encrypted: bool,
    ) -> None:
        """Persist what is needed to download the media again after a restart.

        The part and the processed marker are saved together, so a restart
        during the download resumes it instead of losing the event.
        """
        _burst, part = self._intake_part(agent, room_id, event, kind)
        content = event.source.get("content", {})
        part["media"] = {
            "encrypted": encrypted,
            "url": getattr(event, "url", None),
            "key": getattr(event, "key", None),
            "hashes": getattr(event, "hashes", None),
            "iv": getattr(event, "iv", None),
            "body": str(getattr(event, "body", "") or ""),
            "mimetype": getattr(event, "mimetype", None),
            "content": dict(content) if isinstance(content, dict) else {},
            "sender": event.sender,
            "server_timestamp": int(event.server_timestamp),
        }
        self.processed_events.add(event.event_id)
        self.state.data["processed_events"] = list(self.processed_events)[-2000:]
        self.state.save()
        self._crash_after_effect("media_intake")

    async def _process_media_part(self, burst_id: str, event_id: str) -> None:
        """Resume an image or file download that a restart interrupted."""
        found = self._burst_part(burst_id, event_id)
        if found is None:
            return
        burst, part = found
        media = part.get("media")
        agent = self.config.agents.get(str(burst.get("agent_key")))
        if agent is None or not isinstance(media, dict):
            await self._fail_part(burst_id, event_id, "Medienangaben fehlen")
            return
        event = SimpleNamespace(
            event_id=event_id,
            sender=media.get("sender"),
            server_timestamp=media.get("server_timestamp"),
            url=media.get("url"),
            key=media.get("key"),
            hashes=media.get("hashes"),
            iv=media.get("iv"),
            body=media.get("body"),
            mimetype=media.get("mimetype"),
            source={"content": media.get("content") or {}},
        )
        store = self._store_image if part.get("kind") == "image" else self._store_file
        await store(
            agent,
            str(burst["room_id"]),
            event,
            encrypted=bool(media.get("encrypted")),
        )

    async def _store_image(
        self,
        agent: AgentConfig,
        room_id: str,
        event: Any,
        *,
        encrypted: bool,
    ) -> None:
        try:
            download = await self.client.download(mxc=event.url)
            body = getattr(download, "body", None)
            if not isinstance(body, bytes):
                raise ConnectionError(str(download))
            if len(body) > self.config.media.max_image_bytes:
                raise MediaError("encrypted image exceeds the configured size limit")
            image = body
            if encrypted:
                image = decrypt_attachment(
                    body,
                    event.key["k"],
                    event.hashes["sha256"],
                    event.iv,
                )
            image_type = detect_image_type(image)
            if image_type is None:
                raise MediaError("unsupported image type; use PNG, JPEG, GIF, or WebP")
            if len(image) > self.config.media.max_image_bytes:
                raise MediaError("image exceeds the configured size limit")
            stored = await self._write_inbox_file(
                agent,
                image,
                f"image{image_type.suffix}",
                stem=media_stem(event.event_id),
            )
        except Exception as exc:
            LOGGER.exception("image download failed")
            await self._send_to_event(
                room_id,
                f"Bild konnte nicht übernommen werden: {exc}",
                event,
            )
            await self._fail_event_part(
                event.event_id,
                f"Bild konnte nicht übernommen werden: {exc}",
            )
            return

        caption = str(event.body or "").strip()
        caption_line = f"\nBildunterschrift: {caption}" if caption else ""
        await self._send_to_event(
            room_id,
            f"Bild gespeichert als `{stored.name}`.",
            event,
        )
        await self._enqueue_prompt(
            agent,
            SimpleNamespace(room_id=room_id),
            event.event_id,
            event.server_timestamp,
            f"Bild von {event.sender} via Matrix empfangen.{caption_line}\n\n"
            f"Lokale Datei: {stored}\n"
            f"Format: {image_type.mime_type}, {len(image)} Bytes\n"
            f"SHA-256: {hashlib.sha256(image).hexdigest()}",
            "image",
            relates_to=message_relation(event),
        )

    async def _handle_file(
        self,
        room: MatrixRoom,
        event: (
            RoomMessageFile | RoomEncryptedFile | RoomMessageAudio | RoomEncryptedAudio
        ),
    ) -> None:
        content = event.source.get("content", {})
        if (
            isinstance(content, dict)
            and content.get("msgtype") == "m.audio"
            and is_voice_message_content(content)
        ):
            await self._handle_audio(room, event)
            return
        if not self.config.media.enabled:
            return
        if event.sender == self.config.user_id:
            return
        agent = self._agent_for_room(room.room_id)
        if agent is None or event.sender not in self.config.allowed_users:
            return
        if event.event_id in self.processed_events:
            return
        if self.config.max_message_age_minutes >= 0:
            age_ms = int(time.time() * 1000) - int(event.server_timestamp)
            if age_ms > self.config.max_message_age_minutes * 60_000:
                await self._send_to_event(
                    room.room_id,
                    "Datei ist zu alt und wurde nicht verarbeitet.",
                    event,
                )
                return

        encrypted = isinstance(event, (RoomEncryptedAudio, RoomEncryptedFile))
        self._intake_media(agent, room.room_id, event, "file", encrypted=encrypted)
        await self._send_to_event(
            room.room_id,
            "Datei empfangen, lade und entschlüssele …",
            event,
        )
        await self._store_file(agent, room.room_id, event, encrypted=encrypted)

    async def _store_file(
        self,
        agent: AgentConfig,
        room_id: str,
        event: Any,
        *,
        encrypted: bool,
    ) -> None:
        try:
            content = event.source.get("content", {})
            filename = str(content.get("filename") or event.body or "attachment")
            info = content.get("info", {})
            mime_type = normalize_mime_type(
                info.get("mimetype")
                or getattr(event, "mimetype", None)
                or "application/octet-stream"
            )
            declared_size = info.get("size")
            if declared_size is not None:
                if (
                    isinstance(declared_size, bool)
                    or not isinstance(declared_size, int)
                    or declared_size < 0
                ):
                    raise MediaError("Angekündigte Dateigröße ist ungültig")
                if declared_size > self.config.media.max_file_bytes:
                    raise MediaError(
                        "Datei überschreitet das konfigurierte Limit von "
                        f"{self.config.media.max_file_bytes} Bytes"
                    )
            download = await self.client.download(mxc=event.url)
            body = getattr(download, "body", None)
            if not isinstance(body, bytes):
                raise ConnectionError(str(download))
            if len(body) > self.config.media.max_file_bytes + 4096:
                raise MediaError(
                    "Datei überschreitet das konfigurierte Limit von "
                    f"{self.config.media.max_file_bytes} Bytes"
                )
            attachment = body
            if encrypted:
                attachment = decrypt_attachment(
                    body,
                    event.key["k"],
                    event.hashes["sha256"],
                    event.iv,
                )
            if not attachment:
                raise MediaError("Datei ist leer")
            if len(attachment) > self.config.media.max_file_bytes:
                raise MediaError(
                    "Datei überschreitet das konfigurierte Limit von "
                    f"{self.config.media.max_file_bytes} Bytes"
                )
            if declared_size is not None and declared_size != len(attachment):
                raise MediaError(
                    "Angekündigte Dateigröße stimmt nicht mit den empfangenen "
                    f"Bytes überein ({declared_size} statt {len(attachment)})"
                )

            stored = await self._write_inbox_file(
                agent,
                attachment,
                filename,
                stem=media_stem(event.event_id),
            )
        except Exception as exc:
            LOGGER.exception("file download failed")
            await self._send_to_event(
                room_id,
                f"Datei konnte nicht übernommen werden: {exc}",
                event,
            )
            await self._fail_event_part(
                event.event_id,
                f"Datei konnte nicht übernommen werden: {exc}",
            )
            return

        await self._send_to_event(
            room_id,
            f"Datei gespeichert als `{stored.name}`.",
            event,
        )
        await self._enqueue_prompt(
            agent,
            SimpleNamespace(room_id=room_id),
            event.event_id,
            event.server_timestamp,
            f"Datei von {event.sender} via Matrix empfangen.\n\n"
            f"Lokale Datei: {stored}\n"
            f"Originaldateiname: {json.dumps(filename, ensure_ascii=False)}\n"
            f"Medientyp: {mime_type}\n"
            f"Größe: {len(attachment)} Bytes\n"
            f"SHA-256: {hashlib.sha256(attachment).hexdigest()}",
            "attachment",
            relates_to=message_relation(event),
        )

    async def _write_inbox_file(
        self,
        agent: AgentConfig,
        data: bytes,
        filename: str,
        *,
        stem: str | None = None,
    ) -> Path:
        """Store bytes under a generated name in the agent inbox, atomically.

        A `stem` derived from the source event makes the write idempotent, so a
        download resumed after a restart replaces, never duplicates, the file.
        """
        suffix = Path(filename).suffix
        if not re.fullmatch(r"\.[A-Za-z0-9]{1,10}", suffix):
            suffix = ".bin"
        agent_inbox = safe_agent_directory(self.config.media.inbox_dir, agent.key)
        agent_inbox.mkdir(parents=True, exist_ok=True, mode=0o700)
        stored = agent_inbox / f"{stem or uuid.uuid4().hex}{suffix.lower()}"
        temporary = agent_inbox / f".{stored.name}.part"

        def write_file() -> None:
            if stem:
                temporary.unlink(missing_ok=True)
            try:
                with temporary.open("xb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                temporary.chmod(0o600)
                os.replace(temporary, stored)
            finally:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass

        await asyncio.to_thread(write_file)
        return stored

    async def _download_media(self, source: dict[str, Any], limit: int) -> bytes:
        """Download one plain or encrypted media payload within `limit` bytes."""
        mxc = source.get("url")
        if not isinstance(mxc, str) or not mxc.startswith("mxc://"):
            raise MediaError("keine Medienadresse")
        download = await self.client.download(mxc=mxc)
        body = getattr(download, "body", None)
        if not isinstance(body, bytes):
            raise ConnectionError(display_text(download))
        if len(body) > limit + 4096:
            raise MediaError(f"überschreitet das konfigurierte Limit von {limit} Bytes")
        data = body
        if "key" in source:
            key_value = source.get("key")
            key = (
                key_value.get("k")
                if isinstance(key_value, dict)
                else key_value
            )
            hashes = source.get("hashes")
            digest = (
                hashes.get("sha256")
                if isinstance(hashes, dict)
                else None
            )
            iv = source.get("iv")
            if not isinstance(key, str) or not isinstance(digest, str) or not isinstance(
                iv, str
            ):
                raise MediaError("verschlüsselte Mediendaten sind unvollständig")
            data = decrypt_attachment(
                body,
                key,
                digest,
                iv,
            )
        if not data:
            raise MediaError("Datei ist leer")
        if len(data) > limit:
            raise MediaError(f"überschreitet das konfigurierte Limit von {limit} Bytes")
        return data

    async def _handle_gallery(
        self,
        room: MatrixRoom,
        event: RoomMessageUnknown,
    ) -> None:
        if not self.config.media.enabled:
            return
        if event.sender == self.config.user_id:
            return
        agent = self._agent_for_room(room.room_id)
        if agent is None or event.sender not in self.config.allowed_users:
            return
        if event.event_id in self.processed_events:
            return
        if self.config.max_message_age_minutes >= 0:
            age_ms = int(time.time() * 1000) - int(event.server_timestamp)
            if age_ms > self.config.max_message_age_minutes * 60_000:
                await self._send_to_event(
                    room.room_id,
                    "Galerie ist zu alt und wurde nicht verarbeitet.",
                    event,
                )
                return

        items = gallery_items(event.content)
        # The record and the processed marker are saved together, so a
        # restart resumes the gallery instead of skipping or losing it.
        self.processed_events.add(event.event_id)
        self.state.data["processed_events"] = list(self.processed_events)[-2000:]
        if not items:
            self.state.save()
            await self._send_to_event(
                room.room_id,
                "Galerie enthält keine Elemente.",
                event,
            )
            return
        self._intake_part(agent, room.room_id, event, "gallery")
        self.state.data.setdefault("galleries", {})[event.event_id] = {
            "room_id": room.room_id,
            "agent_key": agent.key,
            "sender": event.sender,
            "server_timestamp": int(event.server_timestamp),
            "relates_to": message_relation(event),
            "caption": display_text(event.content.get("body"), 500),
            "items": items,
            "phase": "downloading",
        }
        self.state.save()
        await self._send_to_event(
            room.room_id,
            f"Galerie mit {elements_dative(len(items))} empfangen, "
            "lade und entschlüssele …",
            event,
            tx_id=f"gallery-received-{event.event_id}",
        )
        await self._process_gallery(event.event_id)

    async def _resume_galleries(self) -> None:
        galleries = self.state.data.get("galleries", {})
        for event_id, record in list(galleries.items()):
            if isinstance(record, dict) and record.get("phase") == "downloading":
                await self._process_gallery(event_id)

    async def _process_gallery(self, event_id: str) -> None:
        record = self.state.data["galleries"][event_id]
        agent = self.config.agents.get(str(record.get("agent_key")))
        if agent is None:
            record["phase"] = "failed"
            self.state.save()
            await self._fail_event_part(event_id, "Agent ist nicht mehr konfiguriert")
            return
        for item in record["items"]:
            if item.get("phase") != "pending":
                continue
            try:
                await self._store_gallery_item(agent, item)
                item["phase"] = "stored"
            except Exception as exc:
                LOGGER.warning(
                    "gallery item %s of %s failed",
                    item["index"],
                    event_id,
                    exc_info=True,
                )
                item["phase"] = "failed"
                item["error"] = display_text(exc)
            item.pop("source", None)
            item.pop("thumbnail_source", None)
            self.state.save()
            self._crash_after_effect("gallery_item")

        room_id = str(record["room_id"])
        relation = record.get("relates_to")
        event = SimpleNamespace(
            event_id=event_id,
            source={"content": {"m.relates_to": relation}} if relation else {},
        )
        items = record["items"]
        stored = [item for item in items if item.get("phase") == "stored"]
        failures = [
            f"• Element {item['index']} ({item['filename']}): {item.get('error')}"
            for item in items
            if item.get("phase") == "failed"
        ]
        if not stored:
            record["phase"] = "failed"
            self.state.save()
            await self._send_to_event(
                room_id,
                "\n".join(
                    [
                        (
                            "Galerie konnte nicht übernommen werden: kein Element "
                            "ließ sich speichern."
                        ),
                        *failures,
                    ]
                ),
                event,
                tx_id=f"gallery-failed-{event_id}",
            )
            await self._fail_event_part(
                event_id,
                "Galerie: kein Element ließ sich speichern",
            )
            return
        await self._send_to_event(
            room_id,
            "\n".join(
                [
                    (
                        f"Galerie gespeichert: {len(stored)} von "
                        f"{elements_dative(len(items))}."
                    ),
                    *failures,
                ]
            ),
            event,
            tx_id=f"gallery-stored-{event_id}",
        )
        # Saved together with the enqueued request by _enqueue_prompt.
        record["phase"] = "enqueued"
        await self._enqueue_prompt(
            agent,
            SimpleNamespace(room_id=room_id),
            event_id,
            int(record["server_timestamp"]),
            self._gallery_prompt(record),
            "gallery",
            relates_to=relation,
        )
        self.state.save()

    async def _store_gallery_item(
        self,
        agent: AgentConfig,
        item: dict[str, Any],
    ) -> None:
        limit = self.config.media.max_file_bytes
        declared = item.get("declared_size")
        if declared is not None and declared > limit:
            raise MediaError(f"überschreitet das konfigurierte Limit von {limit} Bytes")
        data = await self._download_media(item.get("source") or {}, limit)
        if declared is not None and declared != len(data):
            # Some Element X galleries declare metadata sizes exactly 90 bytes
            # below the digest-verified plaintext. Preserve the usable payload,
            # but retain the discrepancy in the agent-facing prompt.
            item["declared_size_mismatch"] = declared
        filename = item["filename"]
        if item.get("itemtype") == "m.image":
            detected = detect_image_type(data)
            if detected is not None:
                filename = f"{Path(filename).stem}{detected.suffix}"
                item["filename"] = filename
                item["mimetype"] = detected.mime_type
        stored = await self._write_inbox_file(agent, data, filename)
        item["stored_path"] = str(stored)
        item["size"] = len(data)
        item["sha256"] = hashlib.sha256(data).hexdigest()
        thumbnail = item.get("thumbnail_source") or {}
        if not thumbnail:
            return
        try:
            preview = await self._download_media(
                thumbnail,
                self.config.media.max_image_bytes,
            )
            preview_type = detect_image_type(preview)
            if preview_type is None:
                raise MediaError("Vorschau ist kein unterstütztes Bild")
            path = await self._write_inbox_file(
                agent,
                preview,
                f"preview{preview_type.suffix}",
            )
        except Exception as exc:
            LOGGER.warning("gallery preview download failed", exc_info=True)
            item["preview_error"] = display_text(exc)
            return
        item["preview_path"] = str(path)
        item["preview_mimetype"] = preview_type.mime_type
        item["preview_sha256"] = hashlib.sha256(preview).hexdigest()

    @staticmethod
    def _gallery_prompt(record: dict[str, Any]) -> str:
        items = record["items"]
        lines = [
            (
                f"Galerie von {record['sender']} via Matrix empfangen: "
                f"{elements_nominative(len(items))} in Originalreihenfolge."
            )
        ]
        if record.get("caption"):
            lines.append(f"Bildunterschrift: {record['caption']}")
        blocks = ["\n".join(lines)]
        for item in items:
            filename = json.dumps(item["filename"], ensure_ascii=False)
            title = f"{item['index']}. {filename}"
            if item.get("phase") != "stored":
                blocks.append(f"{title} — nicht übernommen: {item.get('error')}")
                continue
            details = f"{item['mimetype']}, {item['size']} Bytes"
            if "declared_size_mismatch" in item:
                details += f", Metadaten: {item['declared_size_mismatch']} Bytes"
            if item.get("width") and item.get("height"):
                details += f", {item['width']}×{item['height']}"
            if item.get("preview_path"):
                kind = "JPEG" if item.get("preview_mimetype") == "image/jpeg" else "Bild"
                digest = (
                    f", SHA-256 {item['preview_sha256']}"
                    if item.get("preview_sha256")
                    else ""
                )
                preview = (
                    f"{item['preview_path']} ({kind}-Vorschau aus der Galerie{digest})"
                )
            elif item.get("preview_error"):
                preview = f"nicht verfügbar ({item['preview_error']})"
            else:
                preview = "keine in der Galerie enthalten"
            digest = f"   SHA-256: {item['sha256']}\n" if item.get("sha256") else ""
            blocks.append(
                f"{title} — {details}\n"
                f"   Lokale Datei: {item['stored_path']}\n"
                f"{digest}"
                f"   Vorschau: {preview}"
            )
        return "\n\n".join(blocks)

    @staticmethod
    def _steering_group(
        pending: list[dict[str, Any]],
        request: dict[str, Any],
    ) -> dict[str, Any] | None:
        group_id = request.get("steering_group_id")
        if not group_id:
            return None
        return next(
            (
                candidate
                for candidate in pending[1:]
                if candidate.get("id") == group_id
                and candidate.get("deferred_steering")
            ),
            None,
        )

    def _defer_unconfirmed_steering_group(
        self,
        pending: list[dict[str, Any]],
        request: dict[str, Any],
    ) -> None:
        group = self._steering_group(pending, request)
        if group is None or group.get("injection_phase") not in {
            "injecting",
            "injected_unconfirmed",
        }:
            return
        group["fallback_not_before"] = max(
            float(group.get("fallback_not_before") or 0),
            time.time()
            + float(
                getattr(
                    self.config,
                    "steering_admission_timeout_seconds",
                    2.0,
                )
            ),
        )
        group["last_injection_error"] = (
            "waiting for asynchronous transcript admission before fallback"
        )
        self.state.save()

    def _record_steering_failure(
        self,
        request: dict[str, Any],
        reason: str,
    ) -> None:
        failures = self.state.data.setdefault("steering_failures", [])
        failures.append(
            {
                "id": request.get("id"),
                "source_event_ids": list(request.get("source_event_ids", [])),
                "thread_root_event_id": request.get("thread_root_event_id"),
                "reply_to_event_id": request.get("reply_to_event_id"),
                "qitem_ids": self._request_qitem_ids(request),
                "injection_attempted_at": request.get("injection_attempted_at"),
                "injection_turn_id": request.get("injection_turn_id"),
                "reason": reason,
                "failed_at": time.time(),
            }
        )
        self.state.data["steering_failures"] = failures[-200:]
        self.state.save()

    async def _attempt_steering(
        self,
        agent: AgentConfig,
        active: dict[str, Any],
        group: dict[str, Any],
    ) -> None:
        if group.get("injection_phase") != "pending":
            return
        if time.time() < float(group.get("injection_ready_at") or 0):
            return
        try:
            ref = await asyncio.to_thread(self.resolver.resolve, agent.session)
        except AgentSessionNotFound as exc:
            group["injection_phase"] = "fallback"
            group["last_injection_error"] = str(exc)
            self.state.save()
            return
        if not ref.busy:
            group["injection_phase"] = "fallback"
            group["last_injection_error"] = "no active OpenRig turn"
            self.state.save()
            return
        if ref.session_id != active.get("session_id"):
            group["injection_phase"] = "fallback"
            group["last_injection_error"] = "active session identity changed"
            self.state.save()
            return
        prompt_turn_id = active.get("prompt_turn_id")
        if active.get("turn_attribution_required") and not prompt_turn_id:
            try:
                records, _new_offset = await asyncio.to_thread(
                    read_complete_records,
                    ref.transcript_path,
                    int(active["offset"]),
                )
            except (OSError, KeyError, TypeError, ValueError):
                records = []
            prompt_turn_id = next(
                (
                    turn_id
                    for turn_id, message in user_messages(records)
                    if message == active.get("text")
                ),
                None,
            )
            if prompt_turn_id is None:
                group["last_injection_error"] = (
                    "waiting for active Matrix prompt turn identity"
                )
                self.state.save()
                return
            self._mark_prompt_admitted(active, prompt_turn_id)
            self.state.save()
        if not await self._claim_inbound_qitem(agent, group):
            group["injection_phase"] = "fallback"
            group["last_injection_error"] = "could not claim batch qitems"
            self.state.save()
            return

        group["injection_phase"] = "injecting"
        group["injection_offset"] = ref.transcript_path.stat().st_size
        group["injection_text"] = str(group["text"])
        group["injection_attempted_at"] = time.time()
        self.state.save()
        try:
            await self.gateway.steer_prompt(ref, str(group["text"]))
        except (AgentSessionNotFound, GatewayError, OSError) as exc:
            group["last_injection_error"] = str(exc)
            self.state.save()
            return
        self._crash_after_effect("steering_send")
        group["injection_phase"] = "injected_unconfirmed"
        group["injected_at"] = time.time()
        self.state.save()
        if not active.get("suppress_reply"):
            await self._set_typing(self._room_for_agent(agent.key), True)
            await self._update_agent_status(agent, "busy", "arbeitet")
        await self._scan_steering_admission(
            group,
            ref.transcript_path,
            expected_turn_id=str(prompt_turn_id) if prompt_turn_id else None,
        )

    async def _scan_steering_admission(
        self,
        group: dict[str, Any],
        transcript_path: Path,
        *,
        expected_turn_id: str | None = None,
    ) -> bool:
        phase = str(group.get("injection_phase") or "")
        if phase not in {"injecting", "injected_unconfirmed"}:
            return phase == "admitted"
        offset = group.get("injection_offset")
        text = group.get("injection_text")
        if not isinstance(offset, int) or not isinstance(text, str):
            group["injection_phase"] = "fallback"
            group["last_injection_error"] = "steering admission state is incomplete"
            self.state.save()
            return False
        try:
            records, _new_offset = await asyncio.to_thread(
                read_complete_records,
                transcript_path,
                offset,
            )
        except OSError:
            return False
        for turn_id, message in user_messages(records):
            if message == text and (
                expected_turn_id is None or turn_id == expected_turn_id
            ):
                group["injection_phase"] = "admitted"
                group["injection_turn_id"] = turn_id
                group["admitted_at"] = time.time()
                self._record_latency(
                    group,
                    "steer" if expected_turn_id is not None else "late_steer",
                )
                self.state.save()
                return True
        if phase == "injecting":
            if not group.get("fallback_not_before"):
                group["fallback_not_before"] = time.time() + float(
                    getattr(
                        self.config,
                        "steering_admission_timeout_seconds",
                        2.0,
                    )
                )
            if not group.get("last_injection_error"):
                group["last_injection_error"] = (
                    "waiting for transcript admission after restart"
                )
            self.state.save()
        return False

    async def _finish_steering_group(
        self,
        agent: AgentConfig,
        group: dict[str, Any],
    ) -> bool:
        return await self._finish_inbound_qitem(agent, group)

    async def _start_request(
        self,
        agent: AgentConfig,
        request: dict[str, Any],
        room_id: str,
        ref: AgentSessionRef | None = None,
    ) -> None:
        try:
            ref = ref or await asyncio.to_thread(
                self.resolver.resolve,
                agent.session,
            )
            if request.get("deferred_steering"):
                injection_phase = str(request.get("injection_phase") or "")
                if (
                    injection_phase
                    in {"injecting", "injected_unconfirmed", "admitted"}
                    and isinstance(request.get("injection_offset"), int)
                    and isinstance(request.get("injection_text"), str)
                ):
                    admitted = await self._scan_steering_admission(
                        request,
                        ref.transcript_path,
                    )
                    if admitted:
                        request["session_id"] = ref.session_id
                        request["transcript_path"] = str(ref.transcript_path)
                        request["offset"] = int(request["injection_offset"])
                        request["started_at"] = float(
                            request.get("injection_attempted_at") or time.time()
                        )
                        request["prompt_turn_id"] = request["injection_turn_id"]
                        if not await self._claim_inbound_qitem(agent, request):
                            raise QitemLifecycleError(
                                "could not claim admitted steering qitems"
                            )
                        request["prompt_phase"] = "sent"
                        request["externally_admitted"] = True
                        self.state.save()
                        if not request.get("suppress_reply"):
                            await self._set_typing(room_id, True)
                            await self._update_agent_status(
                                agent,
                                "busy",
                                "arbeitet",
                            )
                        return
                    injection_phase = str(
                        request.get("injection_phase") or ""
                    )
                    if injection_phase in {
                        "injecting",
                        "injected_unconfirmed",
                    }:
                        attempted_at = float(
                            request.get("injection_attempted_at") or time.time()
                        )
                        timeout_seconds = max(
                            0.0,
                            float(
                                getattr(
                                    self.config,
                                    "response_timeout_minutes",
                                    360,
                                )
                            )
                            * 60,
                        )
                        if time.time() - attempted_at >= timeout_seconds:
                            reason = (
                                "steering admission unresolved while the "
                                "seat remained busy"
                            )
                            request["terminal_error"] = (
                                "Steering-Eingabe konnte nicht bestätigt werden: "
                                f"{reason}"
                            )
                            self._record_steering_failure(request, reason)
                            await self._advance_terminal_request(
                                agent,
                                request,
                                room_id,
                                disposition="timeout",
                                status_detail="Steering-Zeitüberschreitung",
                            )
                            return
                        deadline = float(
                            request.get("fallback_not_before")
                            or (
                                attempted_at
                                + float(
                                    getattr(
                                        self.config,
                                        "steering_admission_timeout_seconds",
                                        2.0,
                                    )
                                )
                            )
                        )
                        if time.time() < deadline or ref.busy:
                            if ref.busy:
                                request["last_injection_error"] = (
                                    "waiting for transcript admission while "
                                    "the seat remains busy"
                                )
                            request["started_at"] = None
                            self.state.save()
                            return
                        request["injection_phase"] = "fallback"
                        request["last_injection_error"] = (
                            "steering admission did not appear before the "
                            "fallback grace deadline"
                        )
                        self.state.save()
            if ref.busy:
                if request.get("prompt_phase") == "pending":
                    request["started_at"] = None
                    self.state.save()
                return
            request["session_id"] = ref.session_id
            request["transcript_path"] = str(ref.transcript_path)
            request["offset"] = ref.transcript_path.stat().st_size
            request["started_at"] = time.time()
            activity_state = str(
                getattr(ref, "activity_state", "unknown") or "unknown"
            )
            needs_input_count = int(
                getattr(ref, "needs_input_count", 0) or 0
            )
            request["activity_state"] = activity_state
            request["needs_input_count"] = needs_input_count
            request["activity_label"] = activity_card_label(
                activity_state,
                needs_input_count,
            )
            if not await self._claim_inbound_qitem(agent, request):
                if self._request_terminal_qitem_phase(request):
                    await self._abandon_terminal_qitem(agent, request, room_id)
                    return
                raise QitemLifecycleError(
                    f"could not claim Matrix qitem {request.get('qitem_id')}"
                )
            request["prompt_phase"] = "sending"
            self.state.save()
            await self.gateway.send_prompt(ref, request["text"])
            self._crash_after_effect("prompt_send")
            request["prompt_phase"] = "sent"
            request["injected_at"] = time.time()
            self.state.save()
            if not request.get("suppress_reply"):
                await self._set_typing(room_id, True)
                await self._update_agent_status(agent, "busy", "arbeitet")
        except QitemLifecycleError:
            request["started_at"] = None
            self.state.save()
            if not request.get("qitem_error_notified"):
                response = await self._send(
                    room_id,
                    "Das OpenRig Queue-Item konnte nicht beansprucht werden. "
                    "Die Anfrage bleibt vorgemerkt und wird erneut versucht.",
                    self._reply_target(request),
                    tx_id=f"qitem-error-{request.get('id') or request['event_id']}",
                    **self._delivery_kwargs(request),
                )
                if isinstance(response, RoomSendResponse):
                    request["qitem_error_notified"] = response.event_id
                    self.state.save()
            return
        except (AgentSessionNotFound, GatewayError, OSError) as exc:
            LOGGER.exception("prompt failed for %s", agent.session)
            resolution = f"Prompt failed: {exc}"
            request["terminal_error"] = resolution
            await self._set_typing(room_id, False)
            await self._update_agent_status(agent, "error", "Prompt fehlgeschlagen")
            response = await self._send(
                room_id,
                f"Prompt fehlgeschlagen: {resolution}",
                self._reply_target(request),
                tx_id=f"failure-{request.get('id') or request['event_id']}",
                **self._delivery_kwargs(request),
            )
            if isinstance(response, RoomSendResponse):
                request["failure_notice_event_id"] = response.event_id
            if getattr(
                self.config,
                "mirror_commentary",
                False,
            ) and not request.get("suppress_reply"):
                acknowledged = await self._finalize_activity_card(
                    agent,
                    request,
                    room_id,
                    "Fehlgeschlagen",
                    disposition="prompt_failed",
                    status_detail="Prompt fehlgeschlagen",
                )
                if not acknowledged:
                    return
            await self._advance_terminal_request(
                agent,
                request,
                room_id,
                disposition="prompt_failed",
                status_detail="Prompt fehlgeschlagen",
            )
            return
        self.state.save()

    async def _poll_answers(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.config.poll_interval_seconds)
                await self._poll_answers_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("answer polling failed")

    async def _poll_answers_once(self) -> None:
        await self._recover_acceptance_reactions()
        for agent in self.config.agents.values():
            await self._poll_agent(agent)
            async with self.locks[agent.key]:
                await self._sync_typing(agent)

    async def _poll_agent(self, agent: AgentConfig) -> None:
        async with self.locks[agent.key]:
            pending = self.state.data["pending"].get(agent.key, [])
            if not pending:
                notice = self.state.data.get("queue_notices", {}).get(agent.key, {})
                if notice.get("pinned"):
                    await self._update_queue_notice(
                        agent,
                        self._room_for_agent(agent.key),
                        0,
                    )
                try:
                    ref = await asyncio.to_thread(
                        self.resolver.resolve,
                        agent.session,
                    )
                except AgentSessionNotFound:
                    return
                self.state.data["transcripts"][agent.key] = {
                    "path": str(ref.transcript_path),
                    "offset": ref.transcript_path.stat().st_size,
                }
                return

            request = pending[0]
            terminal_intent = request.get("activity_terminal_intent")
            if isinstance(terminal_intent, dict):
                acknowledged = await self._finalize_activity_card(
                    agent,
                    request,
                    self._room_for_agent(agent.key),
                    str(terminal_intent.get("label") or "Fehlgeschlagen"),
                    disposition=str(
                        terminal_intent.get("disposition") or "delivery_failed"
                    ),
                    status_detail=str(
                        terminal_intent.get("status_detail") or ""
                    ),
                )
                if acknowledged:
                    await self._advance_terminal_request(
                        agent,
                        request,
                        self._room_for_agent(agent.key),
                        disposition=str(
                            terminal_intent.get("disposition")
                            or "delivery_failed"
                        ),
                        status_detail=str(
                            terminal_intent.get("status_detail") or ""
                        ),
                    )
                return
            if request.get("ambiguous_prompt_legacy"):
                return
            group = self._steering_group(pending, request)
            if group is not None:
                injection_phase = str(group.get("injection_phase") or "")
                if injection_phase == "pending":
                    await self._attempt_steering(agent, request, group)
                    updated_phase = str(group.get("injection_phase") or "")
                    if updated_phase != injection_phase:
                        await self._update_queue_notice(
                            agent,
                            self._room_for_agent(agent.key),
                            max(0, len(pending) - 1),
                        )
                    injection_phase = updated_phase
                    if injection_phase == "pending":
                        return
                if injection_phase in {
                    "injecting",
                    "injected_unconfirmed",
                } and request.get("transcript_path"):
                    expected_turn_id = request.get("prompt_turn_id")
                    if (
                        not request.get("turn_attribution_required")
                        or expected_turn_id
                    ):
                        phase_before_scan = injection_phase
                        await self._scan_steering_admission(
                            group,
                            Path(str(request["transcript_path"])),
                            expected_turn_id=(
                                str(expected_turn_id)
                                if expected_turn_id
                                else None
                            ),
                        )
                        if group.get("injection_phase") != phase_before_scan:
                            await self._update_queue_notice(
                                agent,
                                self._room_for_agent(agent.key),
                                max(0, len(pending) - 1),
                            )
            if request.get("started_at") is None:
                try:
                    ref = self.resolver.resolve(agent.session)
                except AgentSessionNotFound:
                    return
                deferred_injection = request.get("deferred_steering") and request.get(
                    "injection_phase"
                ) in {"injecting", "injected_unconfirmed", "admitted"}
                if ref.busy and not deferred_injection:
                    return
                await self._start_request(
                    agent,
                    request,
                    self._room_for_agent(agent.key),
                    ref,
                )
                if request.get("started_at") is not None:
                    # The card showed this request as "next" while the seat was
                    # busy; it is active now.
                    await self._update_queue_notice(
                        agent,
                        self._room_for_agent(agent.key),
                        max(0, len(pending) - 1),
                    )
                return
            if request.get("answer_text") is None:
                if (
                    request.get("started_at")
                    and time.time() - request["started_at"]
                    > self.config.response_timeout_minutes * 60
                ):
                    request["terminal_error"] = "Timed out waiting for a Matrix answer"
                    request["answer_phase"] = "failed"
                    if (
                        getattr(self.config, "mirror_commentary", False)
                        and request.get("commentary_event_id")
                        and not request.get("suppress_reply")
                    ):
                        acknowledged = await self._finalize_activity_card(
                            agent,
                            request,
                            self._room_for_agent(agent.key),
                            "Zeitüberschreitung",
                            disposition="timeout",
                            status_detail="Zeitüberschreitung",
                        )
                        if not acknowledged:
                            return
                    await self._advance_terminal_request(
                        agent,
                        request,
                        self._room_for_agent(agent.key),
                        disposition="timeout",
                        status_detail="Zeitüberschreitung",
                    )
                    return
                path = Path(request["transcript_path"])
                try:
                    records, offset = read_complete_records(
                        path, int(request["offset"])
                    )
                except OSError:
                    return
                prompt_turn_id = request.get("prompt_turn_id")
                if request.get("turn_attribution_required") and not prompt_turn_id:
                    prompt_turn_id = next(
                        (
                            turn_id
                            for turn_id, message in user_messages(records)
                            if message == request.get("text")
                        ),
                        None,
                    )
                    if prompt_turn_id is None:
                        return
                    self._mark_prompt_admitted(request, prompt_turn_id)
                    self.state.save()
                sent_commentary = request.setdefault("sent_commentary", [])
                sent_ids = set(sent_commentary)
                if (
                    getattr(self.config, "mirror_commentary", False)
                    and not request.get("suppress_reply")
                    and not request.get("commentary_skipped")
                ):
                    room_id = self._room_for_agent(agent.key)
                    pending_commentary = [
                        message
                        for message in commentary_messages(
                            records,
                            (
                                request.get("prompt_turn_id")
                                if request.get("turn_attribution_required")
                                else None
                            ),
                        )
                        if message[0] not in sent_ids
                    ]
                    if pending_commentary:
                        request["commentary_summary"] = sanitize_activity_summary(
                            pending_commentary[-1][1]
                        )
                    response = await self._deliver_activity_card(room_id, request)
                    if response is not True and not isinstance(
                        response,
                        RoomSendResponse,
                    ):
                        attempts = request.get("commentary_failure_count", 0) + 1
                        request["commentary_failure_count"] = attempts
                        LOGGER.error(
                            "commentary delivery failed for %s (attempt %s/%s): %s",
                            agent.key,
                            attempts,
                            MAX_DELIVERY_ATTEMPTS,
                            response,
                        )
                        if attempts < MAX_DELIVERY_ATTEMPTS:
                            self.state.save()
                            return
                        request["commentary_skipped"] = True
                        LOGGER.error(
                            "skipping commentary for %s after %s attempts",
                            agent.key,
                            attempts,
                        )
                        self.state.save()
                    if response is True or isinstance(response, RoomSendResponse):
                        sent_commentary.extend(
                            message_id for message_id, _text in pending_commentary
                        )
                        self.state.save()
                request["offset"] = offset
                answers = [
                    text
                    for record in records
                    if (
                        not request.get("turn_attribution_required")
                        or record_turn_id(record) == request.get("prompt_turn_id")
                    )
                    and (text := final_answer(record))
                ]
                if answers:
                    request["answer_text"] = answers[-1]
                self.state.save()

            if request.get("answer_text") is None:
                return

            room_id = self._room_for_agent(agent.key)
            if request.get("answer_phase") == "failed":
                timeout = request.get("terminal_error") == (
                    "Timed out waiting for a Matrix answer"
                )
                await self._advance_terminal_request(
                    agent,
                    request,
                    room_id,
                    disposition="timeout" if timeout else "delivery_failed",
                    status_detail=(
                        "Zeitüberschreitung"
                        if timeout
                        else "Zustellung fehlgeschlagen"
                    ),
                )
                return
            if request.get("suppress_reply"):
                request["answer_phase"] = "suppressed"
                self.state.save()
                if await self._finish_inbound_qitem(agent, request) is False:
                    await self._abandon_if_terminal(
                        agent,
                        request,
                        room_id,
                    )
                    return
                pending.pop(0)
                self.state.save()
                await self._update_queue_notice(
                    agent,
                    room_id,
                    max(0, len(pending) - 1),
                )
                if pending:
                    await self._start_request(agent, pending[0], room_id)
                return
            group = self._steering_group(pending, request)
            if (
                group is not None
                and group.get("injection_phase") == "admitted"
                and group.get("injection_turn_id") == request.get("prompt_turn_id")
            ):
                request["answer_source_event_ids"] = list(
                    dict.fromkeys(
                        [
                            *request.get("source_event_ids", []),
                            *group.get("source_event_ids", []),
                        ]
                    )
                )
                self.state.save()
            if request.get("answer_phase") != "sent":
                request["answer_phase"] = "sending"
                request["answer_tx_id"] = (
                    f"answer-{request.get('id') or request['event_id']}"
                )
                self.state.save()
                response = await self._deliver_to_request(
                    room_id,
                    request["answer_text"],
                    request,
                    tx_id=request["answer_tx_id"],
                )
            else:
                response = RoomSendResponse(
                    str(request.get("answer_event_id") or "$already-sent"),
                    room_id,
                )
            if not isinstance(response, RoomSendResponse):
                attempts = request.get("delivery_failure_count", 0) + 1
                request["delivery_failure_count"] = attempts
                self.state.save()
                LOGGER.error(
                    "answer delivery failed for %s (attempt %s/%s): %s",
                    agent.key,
                    attempts,
                    MAX_DELIVERY_ATTEMPTS,
                    response,
                )
                if attempts < MAX_DELIVERY_ATTEMPTS:
                    return
                request["answer_phase"] = "failed"
                request["terminal_error"] = (
                    f"Matrix answer delivery failed after {attempts} attempts"
                )
                if (
                    getattr(self.config, "mirror_commentary", False)
                    and request.get("commentary_event_id")
                    and not request.get("suppress_reply")
                ):
                    acknowledged = await self._finalize_activity_card(
                        agent,
                        request,
                        room_id,
                        "Fehlgeschlagen",
                        disposition="delivery_failed",
                        status_detail="Zustellung fehlgeschlagen",
                    )
                    if not acknowledged:
                        return
                await self._advance_terminal_request(
                    agent,
                    request,
                    room_id,
                    disposition="delivery_failed",
                    status_detail="Zustellung fehlgeschlagen",
                )
                return
            self._crash_after_effect("answer_send")
            request["answer_phase"] = "sent"
            request["answer_event_id"] = response.event_id
            self.state.save()
            if (
                getattr(getattr(self, "config", None), "mirror_commentary", False)
                and request.get("commentary_event_id")
                and not request.get("suppress_reply")
            ):
                acknowledged = await self._finalize_activity_card(
                    agent,
                    request,
                    room_id,
                    "Abgeschlossen",
                    disposition="completed",
                    status_detail="bereit",
                )
                if not acknowledged:
                    return
            await self._advance_terminal_request(
                agent,
                request,
                room_id,
                disposition="completed",
                status_detail="bereit",
            )

    def _room_for_agent(self, agent_key: str) -> str:
        return str(self.state.data["rooms"][agent_key]["room_id"])

    @staticmethod
    def _reply_target(request: dict[str, Any]) -> str:
        return str(request.get("reply_to_event_id") or request["event_id"])

    @staticmethod
    def _delivery_kwargs(request: dict[str, Any]) -> dict[str, Any]:
        if "thread_root_event_id" in request:
            return {"thread_root": request["thread_root_event_id"]}
        return {}

    async def _deliver_to_request(
        self,
        room_id: str,
        text: str,
        request: dict[str, Any],
        *,
        tx_id: str | None = None,
    ):
        try:
            kwargs = self._delivery_kwargs(request)
            if tx_id is not None:
                kwargs["tx_id"] = tx_id
            source_event_ids = request.get(
                "answer_source_event_ids",
                request.get("source_event_ids"),
            )
            if isinstance(source_event_ids, list) and (
                len(source_event_ids) > 1 or request.get("deferred_steering")
            ):
                kwargs["source_event_ids"] = [
                    str(event_id) for event_id in source_event_ids
                ]
            return await self._send(
                room_id,
                text,
                self._reply_target(request),
                **kwargs,
            )
        except Exception:
            LOGGER.warning(
                "Matrix delivery raised for %s", request["event_id"], exc_info=True
            )
            return None

    async def _deliver_activity_card(
        self,
        room_id: str,
        request: dict[str, Any],
        *,
        tx_id: str | None = None,
    ):
        text = activity_card_text(request)
        if text == request.get("activity_card_text"):
            return True
        commentary_event_id = request.get("commentary_event_id")
        if commentary_event_id:
            try:
                edit_kwargs = {"tx_id": tx_id} if tx_id else {}
                response = await self._edit_message(
                    room_id,
                    str(commentary_event_id),
                    text,
                    **edit_kwargs,
                )
            except Exception:
                response = None
                LOGGER.warning(
                    "activity card edit raised for %s",
                    request["event_id"],
                    exc_info=True,
                )
        else:
            card_tx_id = tx_id or (
                "activity-card-"
                + str(request.get("id") or request["event_id"])
            )
            response = await self._deliver_to_request(
                room_id,
                text,
                request,
                tx_id=card_tx_id,
            )
        if isinstance(response, RoomSendResponse):
            if not commentary_event_id:
                # The card exists in Matrix but is not owned yet: a retry must
                # reuse the stable transaction ID so Matrix returns this event.
                self._crash_after_effect("activity_card_send")
                request["commentary_event_id"] = response.event_id
            request["activity_card_text"] = text
            request["commentary_failure_count"] = 0
            self.state.save()
        return response

    async def _finalize_activity_card(
        self,
        agent: AgentConfig,
        request: dict[str, Any],
        room_id: str,
        label: str,
        *,
        disposition: str,
        status_detail: str,
    ) -> bool:
        request["activity_terminal_intent"] = {
            "disposition": disposition,
            "label": label,
            "status_detail": status_detail,
        }
        request["activity_label"] = label
        self.state.save()
        tx_id = (
            "activity-terminal-"
            + str(request.get("id") or request["event_id"])
        )
        for attempt in range(MAX_DELIVERY_ATTEMPTS):
            response = await self._deliver_activity_card(
                room_id,
                request,
                tx_id=tx_id,
            )
            if response is True or isinstance(response, RoomSendResponse):
                return True
            LOGGER.warning(
                "terminal activity-card update failed for %s (%s/%s): %s",
                agent.key,
                attempt + 1,
                MAX_DELIVERY_ATTEMPTS,
                response,
            )

        text = activity_card_text(request)
        try:
            response = await self._send(
                room_id,
                text,
                self._reply_target(request),
                **self._delivery_kwargs(request),
                tx_id=tx_id,
            )
        except Exception:
            LOGGER.warning(
                "terminal activity-card fallback failed for %s",
                agent.key,
                exc_info=True,
            )
            return False
        if not isinstance(response, RoomSendResponse):
            LOGGER.warning(
                "terminal activity-card fallback failed for %s: %s",
                agent.key,
                response,
            )
            return False
        return True

    async def _advance_terminal_request(
        self,
        agent: AgentConfig,
        request: dict[str, Any],
        room_id: str,
        *,
        disposition: str,
        status_detail: str,
    ) -> bool:
        pending = self.state.data["pending"][agent.key]
        if disposition == "completed":
            group = self._steering_group(pending, request)
            if group is not None:
                if group.get("injection_phase") == "admitted":
                    if group.get("injection_turn_id") == request.get(
                        "prompt_turn_id"
                    ):
                        if await self._finish_steering_group(agent, group) is False:
                            await self._abandon_if_terminal(
                                agent,
                                group,
                                room_id,
                            )
                            self.state.save()
                            return False
                        pending.remove(group)
                elif group.get("injection_phase") == "pending":
                    group["injection_phase"] = "fallback"
                    group["last_injection_error"] = (
                        "active turn completed before steering admission"
                    )
            if await self._finish_inbound_qitem(agent, request) is False:
                await self._abandon_if_terminal(
                    agent,
                    request,
                    room_id,
                )
                self.state.save()
                return False
            self._defer_unconfirmed_steering_group(pending, request)
            if self._is_pending_request_head(pending, request):
                pending.pop(0)
            self.state.save()
            if not pending:
                await self._sync_typing(agent)
                await self._update_agent_status(agent, "ready", "bereit")
        else:
            self._defer_unconfirmed_steering_group(pending, request)
            resolutions = {
                "timeout": "Timed out waiting for a Matrix answer",
                "delivery_failed": (
                    "Matrix answer delivery failed after "
                    f"{request.get('delivery_failure_count', 0)} attempts"
                ),
                "prompt_failed": "Prompt failed",
            }
            resolution = resolutions.get(
                disposition,
                status_detail or "Matrix request failed",
            )
            if disposition in {"timeout", "delivery_failed"}:
                if not await self._complete_failed_answer(
                    agent,
                    request,
                    pending,
                ):
                    return False
            else:
                if await self._fail_inbound_qitem(
                    agent,
                    request,
                    resolution,
                ) is False:
                    await self._abandon_if_terminal(
                        agent,
                        request,
                        room_id,
                    )
                    self.state.save()
                    return False
            if self._is_pending_request_head(pending, request):
                pending.pop(0)
            self.state.save()
            if not pending:
                await self._sync_typing(agent)
            await self._update_agent_status(agent, "error", status_detail)

        if pending:
            # Typing continues into the next turn; the start or the next poll
            # cycle's _sync_typing settles it, so it never blinks off between.
            await self._start_request(agent, pending[0], room_id)
        await self._update_queue_notice(
            agent,
            room_id,
            max(0, len(pending) - 1),
        )
        return True

    async def _restore_activity_feedback(self) -> None:
        await self._refresh_agent_statuses(force=True)
        for agent in self.config.agents.values():
            async with self.locks[agent.key]:
                await self._sync_typing(agent)

    async def _poll_agent_statuses(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.config.status_refresh_seconds)
                await self._refresh_agent_statuses()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("agent status refresh failed")

    async def _refresh_agent_statuses(self, force: bool = False) -> None:
        try:
            resolved = await asyncio.to_thread(
                self.resolver.resolve_many,
                {agent.session for agent in self.config.agents.values()},
            )
        except AgentSessionNotFound:
            resolved = {}
        for agent in self.config.agents.values():
            async with self.locks[agent.key]:
                pending = self.state.data["pending"].get(agent.key, [])
                ref = resolved.get(agent.session)
                if (
                    pending
                    and ref is not None
                    and pending[0].get("started_at") is not None
                    and getattr(self.config, "mirror_commentary", False)
                    and not pending[0].get("suppress_reply")
                    and not pending[0].get("activity_terminal_intent")
                    and pending[0].get("commentary_event_id")
                ):
                    request = pending[0]
                    request["activity_state"] = ref.activity_state
                    request["needs_input_count"] = ref.needs_input_count
                    request["activity_label"] = activity_card_label(
                        ref.activity_state,
                        ref.needs_input_count,
                    )
                    self.state.save()
                    response = await self._deliver_activity_card(
                        self._room_for_agent(agent.key),
                        request,
                    )
                    if response is not True and not isinstance(
                        response,
                        RoomSendResponse,
                    ):
                        LOGGER.warning(
                            "failed to refresh activity card for %s: %s",
                            agent.key,
                            response,
                        )
                if not force and agent.key not in self.agent_statuses:
                    continue
                if pending:
                    status, detail = "busy", "arbeitet"
                else:
                    if ref is None:
                        status, detail = "offline", "offline"
                    else:
                        status, detail = (
                            ("busy", "arbeitet")
                            if ref.busy
                            else ("ready", "bereit")
                        )
                await self._update_agent_status(agent, status, detail)

    async def _update_agent_status(
        self,
        agent: AgentConfig,
        status: str,
        detail: str,
    ) -> None:
        if not hasattr(self, "agent_statuses"):
            self.agent_statuses = {}
        if self.agent_statuses.get(agent.key) == status:
            return
        room = self.state.data["rooms"].get(agent.key)
        if not room:
            return
        room_id = str(room["room_id"])
        status_topic = f"{STATUS_LABELS[status]} {detail} · {agent.session}"
        try:
            await self.client.room_put_state(
                room_id,
                "m.room.topic",
                {"topic": status_topic},
            )
            await self.client.room_put_state(
                room_id,
                AGENT_STATUS_EVENT,
                {
                    "state": status,
                    "detail": detail,
                    "session": agent.session,
                    "updated_at": int(time.time() * 1000),
                },
            )
        except Exception:
            LOGGER.warning(
                "failed to update Matrix status for %s", agent.session, exc_info=True
            )
            return
        self.agent_statuses[agent.key] = status

    @staticmethod
    def _request_being_handled(request: dict[str, Any]) -> bool:
        """Whether the agent is working on this queue head for a visible reply.

        A normal request counts once its prompt is being sent; a deferred
        steering batch counts once its input is in the pane, including while
        the bridge still waits to see it admitted as a later turn.
        """
        if request.get("suppress_reply"):
            return False
        if request.get("answer_phase") in {"sent", "suppressed"}:
            return False
        # A failed head, or one the bridge refuses to resend after an
        # ambiguous restart, can wait at the head while its failure is
        # delivered; nobody is working on it.
        if request.get("terminal_error") or request.get("ambiguous_prompt_legacy"):
            return False
        if request.get("deferred_steering") and request.get("injection_phase") in {
            "injecting",
            "injected_unconfirmed",
            "admitted",
        }:
            return True
        return request.get("started_at") is not None and request.get(
            "prompt_phase"
        ) in {"sending", "sent"}

    async def _sync_typing(self, agent: AgentConfig) -> None:
        """Keep one typing loop exactly while the queue head is being handled."""
        room = self.state.data["rooms"].get(agent.key)
        if not room:
            return
        room_id = str(room["room_id"])
        pending = self.state.data["pending"].get(agent.key, [])
        wanted = bool(pending) and self._request_being_handled(pending[0])
        task = getattr(self, "typing_tasks", {}).get(room_id)
        running = task is not None and not task.done()
        if wanted != running:
            await self._set_typing(room_id, wanted)

    async def _set_typing(self, room_id: str, typing: bool) -> None:
        if not hasattr(self, "typing_tasks"):
            self.typing_tasks = {}
        existing = self.typing_tasks.pop(room_id, None)
        if existing:
            existing.cancel()
            await asyncio.gather(existing, return_exceptions=True)
        if typing:
            self.typing_tasks[room_id] = asyncio.create_task(
                self._typing_loop(room_id),
                name=f"typing:{room_id}",
            )
            return
        try:
            await self.client.room_typing(room_id, False)
        except Exception:
            LOGGER.debug("failed to clear Matrix typing state", exc_info=True)

    async def _typing_loop(self, room_id: str) -> None:
        while True:
            try:
                response = await self.client.room_typing(room_id, True, timeout=30000)
                if hasattr(response, "status_code") and response.status_code >= 400:
                    LOGGER.debug("Matrix typing update failed: %s", response)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.debug("Matrix typing update failed", exc_info=True)
            await asyncio.sleep(20)

    async def _edit_message(
        self,
        room_id: str,
        event_id: str,
        text: str,
        *,
        tx_id: str | None = None,
    ):
        new_content = formatted_message_content(text)
        content = {
            **new_content,
            "body": f"* {new_content['body']}",
            "m.new_content": new_content,
            "m.relates_to": {
                "rel_type": "m.replace",
                "event_id": event_id,
            },
        }
        if "formatted_body" in new_content:
            content["formatted_body"] = f"* {new_content['formatted_body']}"
        kwargs = {"ignore_unverified_devices": True}
        if tx_id:
            kwargs["tx_id"] = tx_id
        return await self.client.room_send(room_id, "m.room.message", content, **kwargs)

    async def _send_to_event(
        self,
        room_id: str,
        text: str,
        event: Any,
        *,
        tx_id: str | None = None,
    ):
        target = resolve_response_target(event.event_id, message_relation(event))
        return await self._send(
            room_id,
            text,
            target.reply_to_event_id,
            thread_root=target.thread_root_event_id,
            tx_id=tx_id,
        )

    async def _send(
        self,
        room_id: str,
        text: str,
        reply_to: str | None = None,
        *,
        thread_root: str | None | object = _DEFAULT_THREAD_ROOT,
        tx_id: str | None = None,
        source_event_ids: list[str] | None = None,
    ):
        content = formatted_message_content(text)
        if source_event_ids:
            content["dev.openrig.source_event_ids"] = list(
                dict.fromkeys(source_event_ids)
            )
        effective_root = (
            reply_to if thread_root is _DEFAULT_THREAD_ROOT else thread_root
        )
        if effective_root:
            content["m.relates_to"] = {
                "rel_type": "m.thread",
                "event_id": effective_root,
                "is_falling_back": True,
                "m.in_reply_to": {"event_id": effective_root},
            }
        elif reply_to:
            content["m.relates_to"] = {"m.in_reply_to": {"event_id": reply_to}}
        kwargs = {"ignore_unverified_devices": True}
        if tx_id:
            kwargs["tx_id"] = tx_id
        return await self.client.room_send(room_id, "m.room.message", content, **kwargs)
