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


def test_setup_reports_when_token_identity_replaces_provided_ids(
    tmp_path,
    monkeypatch,
    capsys,
):
    monkeypatch.setenv("MATRIX_ACCESS_TOKEN", "test-token-never-persist")
    _mock_existing_install(monkeypatch)
    monkeypatch.setattr(cli, "instance_is_running", lambda _: False)

    cli.run_setup(_setup_args(tmp_path, "--without-voice"))

    output = capsys.readouterr().out
    assert "bot user ID @bot:matrix.example.org differs from the access token" in output
    assert "using @relay-bot:matrix.example.org" in output
    assert "device ID TESTDEVICE differs from the access token" in output
    assert "using RELAY_BRIDGE" in output
    assert "test-token-never-persist" not in output


def test_setup_is_quiet_when_provided_ids_match_the_token(
    tmp_path,
    monkeypatch,
    capsys,
):
    monkeypatch.setenv("MATRIX_ACCESS_TOKEN", "test-token-never-persist")
    _mock_existing_install(monkeypatch)
    monkeypatch.setattr(cli, "instance_is_running", lambda _: False)
    args = _setup_args(tmp_path, "--without-voice")
    args.user_id = "@relay-bot:matrix.example.org"
    args.device_id = "RELAY_BRIDGE"

    cli.run_setup(args)

    assert "differs from the access token" not in capsys.readouterr().out


def _interactive_args(tmp_path: Path):
    args = _setup_args(tmp_path, "--without-voice")
    args.user_id = None
    args.device_id = None
    args.allowed_user = None
    return args


def test_setup_with_token_prompts_only_for_the_owner(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MATRIX_ACCESS_TOKEN", "test-token-never-persist")
    _mock_existing_install(monkeypatch)
    monkeypatch.setattr(cli, "instance_is_running", lambda _: False)
    prompts = []

    def fake_input(prompt=""):
        prompts.append(prompt)
        return "@owner:matrix.example.org"

    monkeypatch.setattr("builtins.input", fake_input)
    args = _interactive_args(tmp_path)

    cli.run_setup(args)

    assert prompts == ["Owner Matrix user ID: "]
    config = yaml.safe_load(args.config.read_text(encoding="utf-8"))
    assert config["matrix"]["user_id"] == "@relay-bot:matrix.example.org"
    assert config["matrix"]["device_id"] == "RELAY_BRIDGE"
    assert config["matrix"]["allowed_users"] == ["@owner:matrix.example.org"]
    assert "differs from the access token" not in capsys.readouterr().out


def test_setup_without_token_prompts_for_bot_ids_and_owner(tmp_path, monkeypatch):
    monkeypatch.delenv("MATRIX_ACCESS_TOKEN", raising=False)
    _mock_existing_install(monkeypatch)
    monkeypatch.setattr(cli, "instance_is_running", lambda _: False)
    answers = {
        "Bot Matrix user ID: ": "@typed-bot:matrix.example.org",
        "Bot Matrix device ID: ": "TYPEDDEVICE",
        "Owner Matrix user ID: ": "@owner:matrix.example.org",
    }
    prompts = []

    def fake_input(prompt=""):
        prompts.append(prompt)
        return answers[prompt]

    monkeypatch.setattr("builtins.input", fake_input)
    args = _interactive_args(tmp_path)

    cli.run_setup(args)

    assert prompts == list(answers)
    config = yaml.safe_load(args.config.read_text(encoding="utf-8"))
    assert config["matrix"]["user_id"] == "@typed-bot:matrix.example.org"
    assert config["matrix"]["device_id"] == "TYPEDDEVICE"


@pytest.mark.parametrize(
    "message",
    [
        "OpenRig command not found: /opt/rig. Install OpenRig or pass --rig-command",
        "no running Codex seats found. Start at least one Codex seat",
        "duplicate Codex seat name 'advisor'",
    ],
)
def test_setup_exits_with_the_discovery_next_step_and_no_traceback(
    tmp_path,
    monkeypatch,
    message,
):
    monkeypatch.delenv("MATRIX_ACCESS_TOKEN", raising=False)
    _mock_existing_install(monkeypatch)
    monkeypatch.setattr(cli, "instance_is_running", lambda _: False)

    def fail(_command):
        raise RuntimeError(message)

    monkeypatch.setattr(cli, "discover_openrig_agents", fail)
    args = _setup_args(tmp_path)

    with pytest.raises(SystemExit) as exc_info:
        cli.run_setup(args)

    assert str(exc_info.value) == message
    assert not args.config.exists()
