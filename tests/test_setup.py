import json
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

from openrig_matrix_bridge import cli
from openrig_matrix_bridge.instance import acquire_instance_lock


def _setup_args(tmp_path: Path, *extra: str):
    return cli.build_parser().parse_args(
        [
            "setup",
            "--config",
            str(tmp_path / "config" / "config.yaml"),
            "--state-dir",
            str(tmp_path / "state"),
            "--download-root",
            str(tmp_path / "models"),
            "--homeserver",
            "https://matrix.example.org",
            "--user-id",
            "@bot:matrix.example.org",
            "--device-id",
            "TESTDEVICE",
            "--allowed-user",
            "@owner:matrix.example.org",
            "--rig-command",
            "/opt/rig",
            "--codex-home",
            str(tmp_path / "codex"),
            "--no-human-registration",
            "--skip-model",
            *extra,
        ]
    )


def _mock_existing_install(monkeypatch):
    calls = []

    def discover(command):
        calls.append(("discover", command))
        return {
            "advisor": {
                "display_name": "Advisor",
                "session": "advisor@kernel",
            }
        }

    async def whoami(homeserver, token):
        calls.append(("whoami", homeserver, token))
        return {
            "user_id": "@relay-bot:matrix.example.org",
            "device_id": "RELAY_BRIDGE",
        }

    async def validate_token(homeserver, token):
        calls.append(("validate-token", homeserver, token))
        return {
            "user_id": "@relay-bot:matrix.example.org",
            "device_id": "RELAY_BRIDGE",
        }

    async def display_name(homeserver, user_id, token, name):
        calls.append(("display-name", homeserver, user_id, token, name))

    async def device_name(homeserver, device_id, token, name):
        calls.append(("device-name", homeserver, device_id, token, name))

    monkeypatch.setattr(cli, "discover_openrig_agents", discover)
    monkeypatch.setattr(cli, "_validate_matrix_token", validate_token)
    monkeypatch.setattr(cli, "_matrix_whoami", whoami, raising=False)
    monkeypatch.setattr(cli, "_set_matrix_display_name", display_name)
    monkeypatch.setattr(cli, "_set_matrix_device_name", device_name)
    return calls


def test_clean_existing_install_setup_infers_identity_without_persisting_token(
    tmp_path,
    monkeypatch,
    capsys,
):
    token = "test-token-never-persist"
    monkeypatch.setenv("MATRIX_ACCESS_TOKEN", token)
    calls = _mock_existing_install(monkeypatch)

    # Mock instance_is_running to return False
    monkeypatch.setattr(cli, "instance_is_running", lambda _: False)

    args = _setup_args(tmp_path, "--without-voice")
    cli.run_setup(args)

    config_text = args.config.read_text(encoding="utf-8")
    config = yaml.safe_load(config_text)
    assert config["matrix"]["homeserver"] == "https://matrix.example.org"
    # Setup infers identity from whoami response when token is present
    assert config["matrix"]["user_id"] == "@relay-bot:matrix.example.org"
    assert config["matrix"]["device_id"] == "RELAY_BRIDGE"
    assert config["matrix"]["allowed_users"] == ["@owner:matrix.example.org"]
    assert config["agents"]["advisor"]["session"] == "advisor@kernel"
    assert token not in config_text
    output = capsys.readouterr().out
    assert token not in output
    assert "Next steps:" in output
    assert stat.S_IMODE(args.config.stat().st_mode) == 0o600
    assert stat.S_IMODE(Path(args.state_dir).stat().st_mode) == 0o700
    # Token validation should be called, not the old whoami
    assert ("validate-token", "https://matrix.example.org", token) in calls


def test_setup_refuses_to_overwrite_existing_config(tmp_path, monkeypatch):
    config = tmp_path / "config" / "config.yaml"
    config.parent.mkdir()
    config.write_text("keep: this\n", encoding="utf-8")
    args = _setup_args(tmp_path, "--without-voice")

    with pytest.raises(SystemExit, match="--replace-config"):
        cli.run_setup(args)

    assert config.read_text(encoding="utf-8") == "keep: this\n"


def test_setup_reports_no_running_codex_seat_with_next_step(monkeypatch):
    def run(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 0, stdout="[]", stderr="")

    monkeypatch.setattr(subprocess, "run", run)

    with pytest.raises(RuntimeError, match="Start at least one Codex seat"):
        cli.discover_openrig_agents("/opt/rig")


def test_setup_reports_duplicate_codex_seat(monkeypatch):
    node = {
        "logicalId": "advisor.lead",
        "canonicalSessionName": "advisor@kernel",
        "runtime": "codex",
        "sessionStatus": "running",
    }

    def run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args[0],
            0,
            stdout=f"[{json.dumps(node)}, {json.dumps(node)}]",
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", run)

    with pytest.raises(RuntimeError, match="duplicate Codex seat"):
        cli.discover_openrig_agents("/opt/rig")


def test_setup_reports_human_registry_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("MATRIX_ACCESS_TOKEN", "secret")
    _mock_existing_install(monkeypatch)
    monkeypatch.setattr(cli, "instance_is_running", lambda _: False)

    def fail(*args, **kwargs):
        raise RuntimeError("human registration failed: gateway unavailable")

    monkeypatch.setattr(cli, "_ensure_human_registration", fail)
    args = _setup_args(tmp_path)
    args.no_human_registration = False

    with pytest.raises(SystemExit, match="--no-human-registration"):
        cli.run_setup(args)

    assert not args.config.exists()


def test_setup_reports_model_fallback_after_writing_config(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("MATRIX_ACCESS_TOKEN", "secret")
    _mock_existing_install(monkeypatch)
    monkeypatch.setattr(cli, "instance_is_running", lambda _: False)

    def fail_model(download_root):
        raise OSError("network unavailable")

    monkeypatch.setattr(cli, "ensure_model", fail_model)
    # Create args WITHOUT --skip-model so model download happens
    args = cli.build_parser().parse_args(
        [
            "setup",
            "--config",
            str(tmp_path / "config" / "config.yaml"),
            "--state-dir",
            str(tmp_path / "state"),
            "--download-root",
            str(tmp_path / "models"),
            "--homeserver",
            "https://matrix.example.org",
            "--user-id",
            "@bot:matrix.example.org",
            "--device-id",
            "TESTDEVICE",
            "--allowed-user",
            "@owner:matrix.example.org",
            "--rig-command",
            "/opt/rig",
            "--codex-home",
            str(tmp_path / "codex"),
            "--no-human-registration",
            # NO --skip-model!
        ]
    )

    with pytest.raises(SystemExit, match="--skip-model"):
        cli.run_setup(args)

    assert args.config.is_file()


def test_setup_refuses_when_state_has_running_bridge(tmp_path, monkeypatch):
    monkeypatch.setenv("MATRIX_ACCESS_TOKEN", "secret")
    monkeypatch.setenv("MATRIX_HOMESERVER", "https://matrix.example.org")
    _mock_existing_install(monkeypatch)
    args = _setup_args(tmp_path, "--without-voice")
    lock = acquire_instance_lock(Path(args.state_dir))
    try:
        with pytest.raises(SystemExit, match="already running"):
            cli.run_setup(args)
    finally:
        lock.close()

    assert not args.config.exists()
