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
moshi:
  gateway_url: http://127.0.0.1:24543
  codex_sessions_dir: /tmp/sessions
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


def test_rejects_duplicate_sessions(tmp_path: Path):
    extra = """
  duplicate:
    session: advisor-lead@kernel
"""
    with pytest.raises(ConfigError, match="duplicate agent session"):
        load_config(write_config(tmp_path / "config.yaml", extra))

