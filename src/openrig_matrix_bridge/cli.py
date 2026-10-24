from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import quote

import aiohttp
import yaml

from . import __version__
from .bridge import MatrixBridge
from .config import load_config
from .decisions import normalize_questions
from .gateway import AgentSessionNotFound, AgentSessionResolver
from .media import detect_image_type
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


def _write_private_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False)
            handle.write("\n")
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
        "media": {
            "enabled": True,
            "inbox_dir": str(state_dir / "media-inbox"),
            "max_image_bytes": 25 * 1024 * 1024,
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


def _load_qitem(qitem_id: str) -> dict:
    result = subprocess.run(
        ["rig", "queue", "show", qitem_id, "--full", "--json"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return json.loads(result.stdout)


def run_decision(args: argparse.Namespace, config) -> int:
    if not args.agent:
        raise SystemExit("decision requires --agent")
    if args.agent not in config.agents:
        raise SystemExit(f"unknown agent: {args.agent}")

    qitem = _load_qitem(args.qitem) if args.qitem else {}
    title = (args.title or str(qitem.get("summary") or "")).strip()
    if not title:
        raise SystemExit("decision requires --title or --qitem")

    body = str(qitem.get("body") or "").strip()
    if args.body_file:
        body = Path(args.body_file).read_text(encoding="utf-8").strip()
    elif args.body:
        body = args.body.strip()

    questions = normalize_questions(qitem.get("humanQuestions"))
    if args.option:
        options = [
            {
                "id": f"option-{index}",
                "label": label,
                "recommended": index == 1,
            }
            for index, label in enumerate(args.option, start=1)
        ]
        questions = [{"id": "decision", "question": title, "options": options}]

    request_id = f"decision-{uuid.uuid4().hex}"
    request = {
        "id": request_id,
        "agent": args.agent,
        "title": title,
        "body": body,
        "qitem_id": args.qitem,
        "questions": questions,
    }
    inbox = config.state_dir / "decision-inbox"
    _write_private_json(inbox / f"{request_id}.json", request)

    deadline = time.monotonic() + max(0, args.wait_seconds)
    state_path = config.state_dir / "state.json"
    while time.monotonic() < deadline:
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            time.sleep(0.2)
            continue
        decision = state.get("decisions", {}).get(request_id)
        if decision:
            print(f"{request_id} pinned as {', '.join(decision['event_ids'])}")
            return 0
        time.sleep(0.2)

    print(
        f"queued {request_id}; the bridge did not confirm publication within "
        f"{args.wait_seconds}s"
    )
    return 1


def run_send_image(args: argparse.Namespace, config) -> int:
    if not args.agent:
        raise SystemExit("send-image requires --agent")
    if args.agent not in config.agents:
        raise SystemExit(f"unknown agent: {args.agent}")
    if not args.path:
        raise SystemExit("send-image requires --path")
    image_path = args.path.expanduser().resolve()
    if not image_path.is_file():
        raise SystemExit(f"image not found: {image_path}")
    size = image_path.stat().st_size
    if size <= 0 or size > config.media.max_image_bytes:
        raise SystemExit(
            f"image size must be between 1 and {config.media.max_image_bytes} bytes"
        )
    with image_path.open("rb") as handle:
        image_type = detect_image_type(handle.read(16))
    if image_type is None:
        raise SystemExit("unsupported image type; use PNG, JPEG, GIF, or WebP")

    request_id = f"image-{uuid.uuid4().hex}"
    request = {
        "id": request_id,
        "agent": args.agent,
        "path": str(image_path),
        "caption": (args.caption or "").strip(),
    }
    outbox = config.state_dir / "media-outbox"
    _write_private_json(outbox / f"{request_id}.json", request)

    deadline = time.monotonic() + max(0, args.wait_seconds)
    state_path = config.state_dir / "state.json"
    while time.monotonic() < deadline:
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            time.sleep(0.2)
            continue
        media = state.get("media", {}).get(request_id)
        if media:
            print(f"{request_id} sent as {media['event_id']}")
            return 0
        time.sleep(0.2)

    print(
        f"queued {request_id}; the bridge did not confirm delivery within "
        f"{args.wait_seconds}s"
    )
    return 1


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
    if config.media.enabled:
        try:
            config.media.inbox_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            media_probe = config.media.inbox_dir / ".write-test"
            media_probe.write_text("ok", encoding="utf-8")
            media_probe.unlink()
        except OSError as exc:
            errors.append(f"media inbox is not writable: {exc}")
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
        choices=("run", "setup", "doctor", "download-model", "decision", "send-image"),
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
    parser.add_argument("--agent")
    parser.add_argument("--qitem")
    parser.add_argument("--title")
    parser.add_argument("--body")
    parser.add_argument("--body-file", type=Path)
    parser.add_argument("--option", action="append")
    parser.add_argument("--path", type=Path)
    parser.add_argument("--caption")
    parser.add_argument("--wait-seconds", type=int, default=10)
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
    if args.command == "decision":
        raise SystemExit(run_decision(args, config))
    if args.command == "send-image":
        raise SystemExit(run_send_image(args, config))
    if args.command == "doctor":
        raise SystemExit(asyncio.run(_doctor(args.config)))
    if args.command == "download-model":
        print(ensure_model(config.transcription.download_root))
        return
    token = os.environ.get("MATRIX_ACCESS_TOKEN", "")
    asyncio.run(MatrixBridge(config, token).run())
