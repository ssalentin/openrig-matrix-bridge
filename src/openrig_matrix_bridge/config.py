from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class ConfigError(ValueError):
    pass


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
    mirror_commentary: bool
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

    homeserver = str(matrix.get("homeserver", "")).rstrip("/")
    user_id = str(matrix.get("user_id", ""))
    device_id = str(matrix.get("device_id", ""))
    allowed = matrix.get("allowed_users")

    if not homeserver.startswith(("https://", "http://")):
        raise ConfigError("matrix.homeserver must be an HTTP(S) URL")
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
        if not isinstance(key, str) or not key or any(ch.isspace() for ch in key):
            raise ConfigError("agent keys must be non-empty strings without whitespace")
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
        transport.get("command") or os.environ.get("OPENRIG_CLI") or shutil.which("rig") or "rig"
    ).strip()
    if not rig_command:
        raise ConfigError("openrig.command must not be empty")
    codex_home = Path(
        str(transport.get("codex_home") or os.environ.get("CODEX_HOME") or Path.home() / ".codex")
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
    mirror_commentary = bool(transport.get("mirror_commentary", True))
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
    media_inbox = Path(str(media.get("inbox_dir", state_dir / "media-inbox")))

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
        mirror_commentary=mirror_commentary,
        poll_interval_seconds=poll_interval,
        human_queue_poll_seconds=human_queue_poll,
        max_message_age_minutes=max_age,
        response_timeout_minutes=timeout,
        status_refresh_seconds=status_refresh,
        transcription=TranscriptionConfig(
            enabled=bool(transcription.get("enabled", True)),
            download_root=Path(
                str(transcription.get("download_root", "/var/cache/openrig-matrix-bridge/parakeet"))
            ),
            max_audio_bytes=max_audio_bytes,
            num_threads=max(1, int(transcription.get("num_threads", 4))),
        ),
        media=MediaConfig(
            enabled=bool(media.get("enabled", True)),
            inbox_dir=media_inbox,
            max_image_bytes=max_image_bytes,
        ),
        avatars=AvatarConfig(
            enabled=bool(avatars.get("enabled", True)),
            replace_existing=bool(avatars.get("replace_existing", False)),
            set_profile=bool(avatars.get("set_profile", True)),
        ),
        agents=agents,
    )

