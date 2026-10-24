from __future__ import annotations

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
    model: str
    language: str
    download_root: Path
    max_audio_bytes: int


@dataclass(frozen=True)
class BridgeConfig:
    homeserver: str
    user_id: str
    device_id: str
    allowed_users: tuple[str, ...]
    state_dir: Path
    gateway_url: str
    codex_sessions_dir: Path
    poll_interval_seconds: float
    max_message_age_minutes: int
    response_timeout_minutes: int
    transcription: TranscriptionConfig
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
    moshi = _mapping(root.get("moshi", {}), "moshi")
    agents_raw = _mapping(root.get("agents"), "agents")
    transcription = _mapping(root.get("transcription", {}), "transcription")

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
    sessions_dir = Path(
        str(moshi.get("codex_sessions_dir", "/root/.local/state/moshi/codex-sessions"))
    )
    gateway_url = str(moshi.get("gateway_url", "http://127.0.0.1:24543")).rstrip("/")
    poll_interval = float(moshi.get("poll_interval_seconds", 1.0))
    max_age = int(moshi.get("max_message_age_minutes", 60))
    timeout = int(moshi.get("response_timeout_minutes", 360))
    if poll_interval < 0.2:
        raise ConfigError("moshi.poll_interval_seconds must be at least 0.2")
    if max_age < 0 or timeout <= 0:
        raise ConfigError("message age must be non-negative and timeout positive")
    max_audio_bytes = int(transcription.get("max_audio_bytes", 25 * 1024 * 1024))
    if max_audio_bytes <= 0:
        raise ConfigError("transcription.max_audio_bytes must be positive")

    return BridgeConfig(
        homeserver=homeserver,
        user_id=user_id,
        device_id=device_id,
        allowed_users=tuple(str(item) for item in allowed),
        state_dir=state_dir,
        gateway_url=gateway_url,
        codex_sessions_dir=sessions_dir,
        poll_interval_seconds=poll_interval,
        max_message_age_minutes=max_age,
        response_timeout_minutes=timeout,
        transcription=TranscriptionConfig(
            enabled=bool(transcription.get("enabled", True)),
            model=str(transcription.get("model", "base")),
            language=str(transcription.get("language", "de")),
            download_root=Path(
                str(transcription.get("download_root", "/var/cache/openrig-matrix-bridge/whisper"))
            ),
            max_audio_bytes=max_audio_bytes,
        ),
        agents=agents,
    )

