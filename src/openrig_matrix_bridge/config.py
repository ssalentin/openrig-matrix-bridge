from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from ipaddress import ip_address
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

SAFE_AGENT_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")


class ConfigError(ValueError):
    pass


def safe_agent_directory(root: Path, key: str) -> Path:
    if SAFE_AGENT_KEY.fullmatch(key) is None:
        raise ConfigError(
            "agent keys must start with an alphanumeric character and contain "
            "only letters, digits, underscores, or hyphens (maximum 64 characters)"
        )
    resolved_root = root.resolve()
    directory = (resolved_root / key).resolve()
    if directory.parent != resolved_root:
        raise ConfigError(f"agent key resolves outside the media inbox: {key}")
    return directory


def validate_homeserver(value: str) -> str:
    homeserver = value.rstrip("/")
    parts = urlsplit(homeserver)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ConfigError("matrix.homeserver must be a valid HTTP(S) URL")
    if parts.username is not None or parts.password is not None:
        raise ConfigError("matrix.homeserver must not contain credentials")
    if parts.query or parts.fragment:
        raise ConfigError("matrix.homeserver must not contain a query or fragment")
    if parts.scheme == "http":
        try:
            loopback = ip_address(parts.hostname).is_loopback
        except ValueError:
            loopback = parts.hostname == "localhost"
        if not loopback:
            raise ConfigError(
                "matrix.homeserver must use HTTPS except for loopback test servers"
            )
    return homeserver


@dataclass(frozen=True)
class AgentConfig:
    key: str
    display_name: str
    session: str


@dataclass(frozen=True)
class TranscriptionConfig:
    enabled: bool
    download_root: Path
    max_audio_bytes: int
    num_threads: int


@dataclass(frozen=True)
class MediaConfig:
    enabled: bool
    inbox_dir: Path
    max_image_bytes: int
    max_file_bytes: int
    transfer_notices_enabled: bool = True


@dataclass(frozen=True)
class AvatarConfig:
    enabled: bool
    replace_existing: bool
    set_profile: bool


@dataclass(frozen=True)
class BridgeConfig:
    homeserver: str
    user_id: str
    device_id: str
    allowed_users: tuple[str, ...]
    state_dir: Path
    rig_command: str
    codex_home: Path
    sender_session: str
    human_destinations: tuple[str, ...]
    queue_inbound_messages: bool
    inbound_message_priority: str
    inbound_queue_source: str
    steering_mode: str
    steering_max_chars: int
    steering_batch_window_seconds: float
    steering_admission_timeout_seconds: float
    burst_inactivity_seconds: float
    burst_max_span_seconds: float
    acceptance_reaction_enabled: bool
    reaction_feedback_enabled: bool
    mirror_commentary: bool
    commentary_redaction_enabled: bool
    inventory_cache_seconds: float
    inventory_max_age_seconds: float
    poll_interval_seconds: float
    human_queue_poll_seconds: float
    max_message_age_minutes: int
    response_timeout_minutes: int
    status_refresh_seconds: float
    transcription: TranscriptionConfig
    media: MediaConfig
    avatars: AvatarConfig
    agents: dict[str, AgentConfig]


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{name} must be a mapping")
    return value


def load_config(path: str | Path) -> BridgeConfig:
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"config not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc

    root = _mapping(raw, "config")
    matrix = _mapping(root.get("matrix"), "matrix")
    transport = _mapping(root.get("openrig", root.get("moshi", {})), "openrig")
    agents_raw = _mapping(root.get("agents"), "agents")
    transcription = _mapping(root.get("transcription", {}), "transcription")
    media = _mapping(root.get("media", {}), "media")
    avatars = _mapping(root.get("avatars", {}), "avatars")

    homeserver = validate_homeserver(str(matrix.get("homeserver", "")))
    user_id = str(matrix.get("user_id", ""))
    device_id = str(matrix.get("device_id", ""))
    allowed = matrix.get("allowed_users")

    if not user_id.startswith("@") or ":" not in user_id:
        raise ConfigError("matrix.user_id must be a full Matrix user ID")
    if not device_id:
        raise ConfigError("matrix.device_id is required")
    if not isinstance(allowed, list) or not allowed:
        raise ConfigError("matrix.allowed_users must be a non-empty list")
    if not all(isinstance(item, str) and item.startswith("@") for item in allowed):
        raise ConfigError("matrix.allowed_users must contain full Matrix user IDs")

    agents: dict[str, AgentConfig] = {}
    sessions: set[str] = set()
    for key, value in agents_raw.items():
        if not isinstance(key, str) or SAFE_AGENT_KEY.fullmatch(key) is None:
            raise ConfigError(
                "agent keys must start with an alphanumeric character and contain "
                "only letters, digits, underscores, or hyphens (maximum 64 characters)"
            )
        item = _mapping(value, f"agents.{key}")
        session = str(item.get("session", "")).strip()
        if not session or "@" not in session:
            raise ConfigError(f"agents.{key}.session must be a canonical seat session")
        if session in sessions:
            raise ConfigError(f"duplicate agent session: {session}")
        sessions.add(session)
        agents[key] = AgentConfig(
            key=key,
            display_name=str(item.get("display_name") or key.title()),
            session=session,
        )
    if not agents:
        raise ConfigError("at least one agent must be configured")

    state_dir = Path(str(matrix.get("state_dir", "/var/lib/openrig-matrix-bridge")))
    rig_command = str(
        transport.get("command")
        or os.environ.get("OPENRIG_CLI")
        or shutil.which("rig")
        or "rig"
    ).strip()
    if not rig_command:
        raise ConfigError("openrig.command must not be empty")
    codex_home = Path(
        str(
            transport.get("codex_home")
            or os.environ.get("CODEX_HOME")
            or Path.home() / ".codex"
        )
    )
    sender_session = str(
        transport.get("sender_session") or "matrix-bridge@kernel"
    ).strip()
    if "@" not in sender_session:
        raise ConfigError("openrig.sender_session must look like member@rig")
    human_destinations_raw = transport.get("human_destinations", [])
    if not isinstance(human_destinations_raw, list) or not all(
        isinstance(item, str) and item.strip() for item in human_destinations_raw
    ):
        raise ConfigError("openrig.human_destinations must be a list of seat addresses")
    queue_inbound_messages = bool(transport.get("queue_inbound_messages", True))
    inbound_message_priority = str(
        transport.get("inbound_message_priority", "urgent")
    ).strip()
    if inbound_message_priority not in {"routine", "urgent", "critical"}:
        raise ConfigError(
            "openrig.inbound_message_priority must be routine, urgent, or critical"
        )
    inbound_queue_source = str(
        transport.get("inbound_queue_source")
        or (human_destinations_raw[0] if human_destinations_raw else sender_session)
    ).strip()
    if not inbound_queue_source:
        raise ConfigError("openrig.inbound_queue_source must not be empty")
    steering_mode = str(transport.get("steering_mode", "auto")).strip()
    if steering_mode not in {"queue", "steer", "auto"}:
        raise ConfigError("openrig.steering_mode must be queue, steer, or auto")
    steering_max_chars = int(transport.get("steering_max_chars", 2000))
    if steering_max_chars <= 0:
        raise ConfigError("openrig.steering_max_chars must be positive")
    steering_batch_window_seconds = float(
        transport.get("steering_batch_window_seconds", 0.5)
    )
    if steering_batch_window_seconds <= 0:
        raise ConfigError("openrig.steering_batch_window_seconds must be positive")
    steering_admission_timeout_seconds = float(
        transport.get("steering_admission_timeout_seconds", 2.0)
    )
    if steering_admission_timeout_seconds <= 0:
        raise ConfigError(
            "openrig.steering_admission_timeout_seconds must be positive"
        )
    burst_inactivity_seconds = float(transport.get("burst_inactivity_seconds", 45))
    if burst_inactivity_seconds < 0:
        raise ConfigError("openrig.burst_inactivity_seconds must not be negative")
    burst_max_span_seconds = float(transport.get("burst_max_span_seconds", 300))
    if burst_max_span_seconds < burst_inactivity_seconds:
        raise ConfigError(
            "openrig.burst_max_span_seconds must be at least "
            "openrig.burst_inactivity_seconds"
        )
    acceptance_reaction_enabled = bool(
        transport.get("acceptance_reaction_enabled", False)
    )
    reaction_feedback_enabled = bool(transport.get("reaction_feedback_enabled", True))
    mirror_commentary = bool(transport.get("mirror_commentary", True))
    inventory_cache_seconds = float(transport.get("inventory_cache_seconds", 2.0))
    if not 0 <= inventory_cache_seconds <= 10:
        raise ConfigError(
            "openrig.inventory_cache_seconds must be between 0 and 10 seconds"
        )
    inventory_max_age_seconds = float(transport.get("inventory_max_age_seconds", 10.0))
    if not inventory_cache_seconds <= inventory_max_age_seconds <= 60:
        raise ConfigError(
            "openrig.inventory_max_age_seconds must be at least "
            "openrig.inventory_cache_seconds and at most 60 seconds"
        )
    commentary_redaction_enabled = transport.get("commentary_redaction_enabled", True)
    if not isinstance(commentary_redaction_enabled, bool):
        raise ConfigError("openrig.commentary_redaction_enabled must be true or false")
    poll_interval = float(transport.get("poll_interval_seconds", 0.2))
    human_queue_poll = float(transport.get("human_queue_poll_seconds", 2.0))
    max_age = int(transport.get("max_message_age_minutes", 60))
    timeout = int(transport.get("response_timeout_minutes", 360))
    status_refresh = float(transport.get("status_refresh_seconds", 30))
    if poll_interval < 0.2:
        raise ConfigError("openrig.poll_interval_seconds must be at least 0.2")
    if human_queue_poll < 0.5:
        raise ConfigError("openrig.human_queue_poll_seconds must be at least 0.5")
    if status_refresh < 1:
        raise ConfigError("openrig.status_refresh_seconds must be at least 1")
    if max_age < 0 or timeout <= 0:
        raise ConfigError("message age must be non-negative and timeout positive")
    max_audio_bytes = int(transcription.get("max_audio_bytes", 25 * 1024 * 1024))
    if max_audio_bytes <= 0:
        raise ConfigError("transcription.max_audio_bytes must be positive")
    max_image_bytes = int(media.get("max_image_bytes", 25 * 1024 * 1024))
    if max_image_bytes <= 0:
        raise ConfigError("media.max_image_bytes must be positive")
    max_file_bytes = int(media.get("max_file_bytes", 25 * 1024 * 1024))
    if max_file_bytes <= 0:
        raise ConfigError("media.max_file_bytes must be positive")
    media_inbox = Path(str(media.get("inbox_dir", state_dir / "media-inbox")))
    transfer_notices_enabled = media.get("transfer_notices_enabled", True)
    if not isinstance(transfer_notices_enabled, bool):
        raise ConfigError("media.transfer_notices_enabled must be true or false")

    return BridgeConfig(
        homeserver=homeserver,
        user_id=user_id,
        device_id=device_id,
        allowed_users=tuple(str(item) for item in allowed),
        state_dir=state_dir,
        rig_command=rig_command,
        codex_home=codex_home,
        sender_session=sender_session,
        human_destinations=tuple(str(item).strip() for item in human_destinations_raw),
        queue_inbound_messages=queue_inbound_messages,
        inbound_message_priority=inbound_message_priority,
        inbound_queue_source=inbound_queue_source,
        steering_mode=steering_mode,
        steering_max_chars=steering_max_chars,
        steering_batch_window_seconds=steering_batch_window_seconds,
        steering_admission_timeout_seconds=steering_admission_timeout_seconds,
        burst_inactivity_seconds=burst_inactivity_seconds,
        burst_max_span_seconds=burst_max_span_seconds,
        acceptance_reaction_enabled=acceptance_reaction_enabled,
        reaction_feedback_enabled=reaction_feedback_enabled,
        mirror_commentary=mirror_commentary,
        commentary_redaction_enabled=commentary_redaction_enabled,
        inventory_cache_seconds=inventory_cache_seconds,
        inventory_max_age_seconds=inventory_max_age_seconds,
        poll_interval_seconds=poll_interval,
        human_queue_poll_seconds=human_queue_poll,
        max_message_age_minutes=max_age,
        response_timeout_minutes=timeout,
        status_refresh_seconds=status_refresh,
        transcription=TranscriptionConfig(
            enabled=bool(transcription.get("enabled", True)),
            download_root=Path(
                str(
                    transcription.get(
                        "download_root", "/var/cache/openrig-matrix-bridge/parakeet"
                    )
                )
            ),
            max_audio_bytes=max_audio_bytes,
            num_threads=max(1, int(transcription.get("num_threads", 4))),
        ),
        media=MediaConfig(
            enabled=bool(media.get("enabled", True)),
            inbox_dir=media_inbox,
            max_image_bytes=max_image_bytes,
            max_file_bytes=max_file_bytes,
            transfer_notices_enabled=transfer_notices_enabled,
        ),
        avatars=AvatarConfig(
            enabled=bool(avatars.get("enabled", True)),
            replace_existing=bool(avatars.get("replace_existing", False)),
            set_profile=bool(avatars.get("set_profile", True)),
        ),
        agents=agents,
    )
