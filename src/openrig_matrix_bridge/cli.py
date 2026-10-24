from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import quote

import aiohttp
import yaml

from . import __version__
from .bridge import MatrixBridge
from .config import load_config
from .gateway import AgentSessionNotFound, AgentSessionResolver
from .transcription import MODEL_NAME, ensure_model, model_ready


def _default_config() -> Path:
    return Path(
        os.environ.get(
            "OPENRIG_MATRIX_CONFIG",
            Path.home() / ".config" / "openrig-matrix-bridge" / "config.yaml",
        )
    )


def _agent_key(session: str) -> str:
    base = session.split("@", 1)[0]
    return re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-") or "agent"


def _display_name(logical_id: str) -> str:
    return " ".join(part.capitalize() for part in re.split(r"[._-]+", logical_id) if part)


def discover_openrig_agents() -> dict[str, dict[str, str]]:
    result = subprocess.run(
        ["rig", "ps", "--nodes", "-A", "--json"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    nodes = json.loads(result.stdout)
    agents: dict[str, dict[str, str]] = {}
    for node in nodes:
        if node.get("sessionStatus") != "running" or node.get("runtime") != "codex":
            continue
        logical_id = str(node.get("logicalId") or "")
        session = str(node.get("canonicalSessionName") or "")
        if not logical_id or not session or logical_id == "operator.human":
            continue
        key = _agent_key(session)
        if key in agents:
            rig = str(node.get("rigName") or "")
            key = _agent_key(f"{rig}-{session}")
        agents[key] = {
            "display_name": _display_name(logical_id),
            "session": session,
        }
    if not agents:
        raise RuntimeError("no running Codex seats found through `rig ps --nodes -A`")
    return agents


def _write_private_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            yaml.safe_dump(data, handle, sort_keys=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


async def _set_matrix_display_name(
    homeserver: str,
    user_id: str,
    token: str,
    display_name: str,
) -> None:
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session, session.put(
        f"{homeserver}/_matrix/client/v3/profile/{quote(user_id, safe='')}/displayname",
        headers={"Authorization": f"Bearer {token}"},
        json={"displayname": display_name},
    ) as response:
        if response.status != 200:
            body = (await response.text())[:500]
            raise RuntimeError(
                f"Matrix display-name update failed with HTTP {response.status}: {body}"
            )


def run_setup(args: argparse.Namespace) -> None:
    homeserver = (args.homeserver or input("Matrix homeserver URL: ")).strip().rstrip("/")
    user_id = (args.user_id or input("Bot Matrix user ID: ")).strip()
    device_id = (args.device_id or input("Bot Matrix device ID: ")).strip()
    allowed_user = (args.allowed_user or input("Owner Matrix user ID: ")).strip()
    agents = discover_openrig_agents()
    state_dir = Path(args.state_dir or Path.home() / ".local" / "state" / "openrig-matrix-bridge")
    download_root = Path(
        args.download_root or Path.home() / ".cache" / "openrig-matrix-bridge" / "parakeet"
    )
    config = {
        "matrix": {
            "homeserver": homeserver,
            "user_id": user_id,
            "device_id": device_id,
            "allowed_users": [allowed_user],
            "state_dir": str(state_dir),
        },
        "moshi": {
            "gateway_url": args.gateway_url,
            "codex_sessions_dir": str(
                Path.home() / ".local" / "state" / "moshi" / "codex-sessions"
            ),
            "poll_interval_seconds": 1.0,
            "max_message_age_minutes": 60,
            "response_timeout_minutes": 360,
            "status_refresh_seconds": 30,
        },
        "transcription": {
            "enabled": not args.without_voice,
            "download_root": str(download_root),
            "max_audio_bytes": 25 * 1024 * 1024,
            "num_threads": args.parakeet_threads,
        },
        "agents": agents,
    }
    _write_private_yaml(args.config, config)
    print(f"wrote {args.config} with {len(agents)} agent room(s)")
    token = os.environ.get("MATRIX_ACCESS_TOKEN", "")
    if token:
        asyncio.run(_set_matrix_display_name(homeserver, user_id, token, "OpenRig"))
        print("set Matrix bridge display name to OpenRig")
    else:
        print("warning: MATRIX_ACCESS_TOKEN is not set; Matrix display name unchanged")
    if config["transcription"]["enabled"] and not args.skip_model:
        model = ensure_model(download_root)
        print(f"Parakeet ready: {model}")


async def _doctor(config_path: Path) -> int:
    config = load_config(config_path)
    token = os.environ.get("MATRIX_ACCESS_TOKEN", "")
    errors: list[str] = []
    warnings: list[str] = []
    if not token:
        errors.append("MATRIX_ACCESS_TOKEN is not set")
    else:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                f"{config.homeserver}/_matrix/client/v3/account/whoami",
                headers={"Authorization": f"Bearer {token}"},
            ) as response:
                if response.status != 200:
                    errors.append(f"Matrix whoami failed with HTTP {response.status}")
            async with session.get(f"{config.gateway_url}/v1/version") as response:
                if response.status != 200:
                    errors.append(f"Moshi gateway failed with HTTP {response.status}")
    config.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    probe = config.state_dir / ".write-test"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        errors.append(f"state directory is not writable: {exc}")
    resolver = AgentSessionResolver(config.codex_sessions_dir)
    for agent in config.agents.values():
        try:
            resolver.resolve(agent.session)
        except AgentSessionNotFound:
            warnings.append(f"{agent.session}: no live Codex session")
    if config.transcription.enabled:
        if model_ready(config.transcription.download_root):
            print(f"Parakeet: ready ({MODEL_NAME})")
        else:
            warnings.append("Parakeet model is not downloaded")
    for warning in warnings:
        print(f"warning: {warning}")
    for error in errors:
        print(f"error: {error}")
    if errors:
        return 1
    print(f"Matrix bridge configuration ready ({len(config.agents)} agents)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="OpenRig Matrix bridge")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "command",
        nargs="?",
        default="run",
        choices=("run", "setup", "doctor", "download-model"),
    )
    parser.add_argument("--config", type=Path, default=_default_config())
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--homeserver")
    parser.add_argument("--user-id")
    parser.add_argument("--device-id")
    parser.add_argument("--allowed-user")
    parser.add_argument("--state-dir")
    parser.add_argument("--download-root")
    parser.add_argument("--gateway-url", default="http://127.0.0.1:24543")
    parser.add_argument("--parakeet-threads", type=int, default=4)
    parser.add_argument("--skip-model", action="store_true")
    parser.add_argument("--without-voice", action="store_true")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("nio").setLevel(logging.INFO if args.verbose else logging.WARNING)
    if args.command == "setup":
        run_setup(args)
        return
    config = load_config(args.config)
    if args.command == "doctor":
        raise SystemExit(asyncio.run(_doctor(args.config)))
    if args.command == "download-model":
        print(ensure_model(config.transcription.download_root))
        return
    token = os.environ.get("MATRIX_ACCESS_TOKEN", "")
    asyncio.run(MatrixBridge(config, token).run())
