from pathlib import Path

import pytest

from openrig_matrix_bridge.config import ConfigError, load_config, safe_agent_directory


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


def test_acceptance_reaction_can_be_enabled(tmp_path: Path):
    path = write_config(tmp_path / "config.yaml")
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            '  human_destinations: ["owner@external"]',
            '  human_destinations: ["owner@external"]\n'
            "  acceptance_reaction_enabled: true",
        ),
        encoding="utf-8",
    )

    assert load_config(path).acceptance_reaction_enabled is True


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
    assert config.queue_inbound_messages is True
    assert config.inbound_message_priority == "urgent"
    assert config.inbound_queue_source == "owner@external"
    assert config.steering_mode == "auto"
    assert config.steering_max_chars == 2000
    assert config.steering_batch_window_seconds == 0.5
    assert config.steering_admission_timeout_seconds == 2.0
    assert config.acceptance_reaction_enabled is False
    assert config.reaction_feedback_enabled is True
    assert config.mirror_commentary is True
    assert config.avatars.enabled is True
    assert config.avatars.replace_existing is False
    assert config.avatars.set_profile is True
    assert config.media.enabled is True
    assert config.media.max_image_bytes == 25 * 1024 * 1024
    assert config.media.max_file_bytes == 25 * 1024 * 1024


def test_rejects_duplicate_sessions(tmp_path: Path):
    extra = """
  duplicate:
    session: advisor-lead@kernel
"""
    with pytest.raises(ConfigError, match="duplicate agent session"):
        load_config(write_config(tmp_path / "config.yaml", extra))


def test_rejects_invalid_inbound_message_priority(tmp_path: Path):
    path = write_config(tmp_path / "config.yaml")
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            '  human_destinations: ["owner@external"]',
            '  human_destinations: ["owner@external"]\n'
            "  inbound_message_priority: immediate",
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="inbound_message_priority"):
        load_config(path)


def test_loads_explicit_steering_configuration(tmp_path: Path):
    path = write_config(tmp_path / "config.yaml")
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            '  human_destinations: ["owner@external"]',
            '  human_destinations: ["owner@external"]\n'
            "  steering_mode: steer\n"
            "  steering_max_chars: 640\n"
            "  steering_batch_window_seconds: 1.25\n"
            "  steering_admission_timeout_seconds: 3.5",
        ),
        encoding="utf-8",
    )

    config = load_config(path)

    assert config.steering_mode == "steer"
    assert config.steering_max_chars == 640
    assert config.steering_batch_window_seconds == 1.25
    assert config.steering_admission_timeout_seconds == 3.5


@pytest.mark.parametrize(
    ("setting", "value", "message"),
    [
        ("steering_mode", "sometimes", "steering_mode"),
        ("steering_max_chars", "0", "steering_max_chars"),
        ("steering_batch_window_seconds", "0", "steering_batch_window_seconds"),
        (
            "steering_admission_timeout_seconds",
            "0",
            "steering_admission_timeout_seconds",
        ),
    ],
)
def test_rejects_invalid_steering_configuration(
    tmp_path: Path,
    setting: str,
    value: str,
    message: str,
):
    path = write_config(tmp_path / "config.yaml")
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            '  human_destinations: ["owner@external"]',
            f'  human_destinations: ["owner@external"]\n  {setting}: {value}',
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match=message):
        load_config(path)


@pytest.mark.parametrize(
    "homeserver",
    [
        "http://matrix.example.org",
        "http://localhost.example.org",
        "http://192.0.2.1",
        "ftp://matrix.example.org",
    ],
)
def test_rejects_insecure_or_invalid_homeserver(tmp_path: Path, homeserver: str):
    path = write_config(tmp_path / "config.yaml")
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "https://matrix.example.org",
            homeserver,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="homeserver"):
        load_config(path)


@pytest.mark.parametrize(
    "homeserver",
    [
        "http://localhost:8008",
        "http://127.0.0.1:8008",
        "http://[::1]:8008",
        "https://matrix.example.org",
    ],
)
def test_allows_secure_or_loopback_homeserver(tmp_path: Path, homeserver: str):
    path = write_config(tmp_path / "config.yaml")
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "https://matrix.example.org",
            homeserver,
        ),
        encoding="utf-8",
    )

    assert load_config(path).homeserver == homeserver


@pytest.mark.parametrize(
    "agent_key",
    ["../escape", "nested/agent", "..", ".hidden", "bad key", ""],
)
def test_rejects_unsafe_agent_keys(tmp_path: Path, agent_key: str):
    path = write_config(tmp_path / "config.yaml")
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "  advisor:",
            f"  {agent_key!r}:",
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="agent keys"):
        load_config(path)


def test_safe_agent_directory_rejects_symlink_escape(tmp_path: Path):
    root = tmp_path / "inbox"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "advisor").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ConfigError, match="agent key"):
        safe_agent_directory(root, "advisor")


def test_safe_agent_directory_returns_key_below_root(tmp_path: Path):
    assert safe_agent_directory(tmp_path, "advisor") == (tmp_path / "advisor").resolve()
