from pathlib import Path

import pytest

from openrig_matrix_bridge.config import ConfigError, load_config


def write_config(path: Path, extra: str = "") -> Path:
    path.write_text(
        """
matrix:
  homeserver: https://matrix.example.org
  user_id: "@bot:matrix.example.org"
  device_id: BOT
  allowed_users: ["@owner:matrix.example.org"]
  state_dir: /tmp/state
openrig:
  command: /opt/rig
  codex_home: /tmp/codex
  sender_session: matrix-bridge@kernel
  human_destinations: ["owner@external"]
  mirror_commentary: true
agents:
  advisor:
    display_name: Advisor
    session: advisor-lead@kernel
""".strip()
        + extra,
        encoding="utf-8",
    )
    return path


def test_loads_minimal_config(tmp_path: Path):
    config = load_config(write_config(tmp_path / "config.yaml"))
    assert config.homeserver == "https://matrix.example.org"
    assert config.agents["advisor"].session == "advisor-lead@kernel"
    assert config.status_refresh_seconds == 30
    assert config.poll_interval_seconds == 0.2
    assert config.human_queue_poll_seconds == 2.0
    assert config.rig_command == "/opt/rig"
    assert config.codex_home == Path("/tmp/codex")
    assert config.sender_session == "matrix-bridge@kernel"
    assert config.human_destinations == ("owner@external",)
    assert config.mirror_commentary is True
    assert config.media.enabled is True
    assert config.media.max_image_bytes == 25 * 1024 * 1024


def test_rejects_duplicate_sessions(tmp_path: Path):
    extra = """
  duplicate:
    session: advisor-lead@kernel
"""
    with pytest.raises(ConfigError, match="duplicate agent session"):
        load_config(write_config(tmp_path / "config.yaml", extra))

