#!/usr/bin/env python3
"""Deterministic OpenRig CLI transport used by the Matrix integration harness.

This is deliberately a separate process boundary so the bridge exercises the
same subprocess commands it uses with the real `rig` executable. It is not
evidence for live OpenRig/Codex behavior.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

SESSION = "integration-agent@local"
TRANSCRIPT_RECORD = {
    "type": "event_msg",
    "payload": {"type": "item_completed"},
}


class TransportBusy(RuntimeError):
    pass


def state_root() -> Path:
    raw = os.environ.get("OPENRIG_FAKE_STATE")
    if not raw:
        raise SystemExit("OPENRIG_FAKE_STATE is required")
    path = Path(raw)
    path.mkdir(parents=True, exist_ok=True)
    (path / "transcript.jsonl").touch(exist_ok=True)
    return path


def _default_state() -> dict[str, Any]:
    return {
        "queue": {},
        "prompts": [],
        "calls": [],
        "human_registered": False,
        "busy_until": 0.0,
    }


def read_state(root: Path) -> dict[str, Any]:
    path = root / "fake-openrig.json"
    with (root / ".lock").open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not path.exists():
            return _default_state()
        state = json.loads(path.read_text(encoding="utf-8"))
        fcntl.flock(lock, fcntl.LOCK_UN)
        return state


def update_state(root: Path, callback) -> dict[str, Any]:
    path = root / "fake-openrig.json"
    with (root / ".lock").open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = (
            json.loads(path.read_text(encoding="utf-8"))
            if path.exists()
            else _default_state()
        )
        callback(state)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(state, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
        fcntl.flock(lock, fcntl.LOCK_UN)
        return state


def append_transcript(root: Path, phase: str, text: str) -> None:
    record = json.loads(json.dumps(TRANSCRIPT_RECORD))
    item = {
        "type": "AgentMessage",
        "id": f"fake-{uuid.uuid4().hex}",
        "phase": phase,
        "content": [{"type": "Text", "text": text}],
    }
    record["payload"]["item"] = item
    with (root / "transcript.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, separators=(",", ":")) + "\n")


def node_payload(root: Path) -> dict[str, Any]:
    state = read_state(root)
    busy = float(state.get("busy_until", 0)) > time.time()
    return {
        "logicalId": "integration.agent",
        "canonicalSessionName": SESSION,
        "runtime": "codex",
        "sessionStatus": "running",
        "resumeToken": "integration-session",
        "contextUsage": {
            "transcriptPath": str(root / "transcript.jsonl"),
        },
        "agentActivity": {"state": "running" if busy else "idle"},
    }


def print_json(value: Any) -> None:
    print(json.dumps(value, sort_keys=True))


def option(args: list[str], name: str) -> str | None:
    try:
        return args[args.index(name) + 1]
    except (ValueError, IndexError):
        return None


def complete_prompt(root: Path, prompt: str, prompt_id: str) -> None:
    delay = (
        3.2
        if "hold first" in prompt.lower()
        else float(os.environ.get("OPENRIG_FAKE_RESPONSE_DELAY", "1.2"))
    )
    time.sleep(delay)
    append_transcript(root, "commentary", f"Fake transport received: {prompt[:80]}")
    time.sleep(0.15)

    def ready(state: dict[str, Any]) -> None:
        state["busy_until"] = 0.0
        for record in reversed(state["prompts"]):
            if record["id"] == prompt_id:
                record["completed_at"] = time.time()
                break

    update_state(root, ready)
    answer = f"Fake integration answer: {prompt[:120]}"
    append_transcript(root, "final_answer", answer)


def handle_send(root: Path, args: list[str]) -> None:
    if len(args) < 2:
        raise SystemExit("send requires a destination and prompt")
    destination, prompt = args[0], args[1]
    if destination != SESSION:
        raise SystemExit(f"unknown session: {destination}")
    delay = (
        3.2
        if "hold first" in prompt.lower()
        else float(os.environ.get("OPENRIG_FAKE_RESPONSE_DELAY", "1.2"))
    )
    prompt_id = uuid.uuid4().hex

    def record(state: dict[str, Any]) -> None:
        if float(state.get("busy_until", 0)) > time.time():
            raise TransportBusy(
                f"agent is busy; refusing concurrent send to {destination}"
            )
        state["prompts"].append(
            {
                "id": prompt_id,
                "destination": destination,
                "prompt": prompt,
                "sent_at": time.time(),
                "completed_at": None,
            }
        )
        state["busy_until"] = time.time() + delay

    try:
        update_state(root, record)
    except TransportBusy as exc:
        raise SystemExit(str(exc)) from exc
    process = subprocess.Popen(
        [sys.executable, __file__, "_complete", prompt, prompt_id],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env=os.environ.copy(),
    )
    print_json({"delivered": True, "session": destination, "pid": process.pid})


def handle_queue(root: Path, args: list[str]) -> None:
    if not args:
        raise SystemExit("queue requires a command")
    command = args[0]
    if command == "create":
        qitem_id = f"qitem-fake-{uuid.uuid4().hex[:12]}"
        item = {
            "qitemId": qitem_id,
            "sourceSession": option(args, "--source"),
            "destinationSession": option(args, "--destination"),
            "summary": option(args, "--summary"),
            "body": option(args, "--body"),
            "priority": option(args, "--priority"),
            "tags": (option(args, "--tags") or "").split(","),
            "state": "pending",
            "history": [{"state": "pending", "at": time.time()}],
        }

        def create(state: dict[str, Any]) -> None:
            state["queue"][qitem_id] = item

        update_state(root, create)
        print_json({"qitemId": qitem_id})
        return
    if command == "list":
        print_json(list(read_state(root)["queue"].values()))
        return
    if command == "show":
        qitem_id = args[1]
        print_json(read_state(root)["queue"][qitem_id])
        return
    if command in {"claim", "update"}:
        qitem_id = args[1]

        def mutate(state: dict[str, Any]) -> None:
            item = state["queue"][qitem_id]
            if command == "claim":
                item["state"] = "in-progress"
                item["claimed_by"] = os.environ.get("OPENRIG_SESSION_NAME")
            else:
                item["state"] = option(args, "--state") or item["state"]
                item["closure_reason"] = option(args, "--closure-reason")
                item["resolution"] = option(args, "--resolution")
            item.setdefault("history", []).append(
                {"state": item["state"], "at": time.time()}
            )

        update_state(root, mutate)
        print_json({"qitemId": qitem_id, "ok": True})
        return
    raise SystemExit(f"unsupported fake queue command: {command}")


def handle_human(root: Path, args: list[str]) -> None:
    if args[:2] != ["gateway", "human"]:
        raise SystemExit("unsupported gateway command")
    command = args[2]
    if command == "show":
        if read_state(root).get("human_registered"):
            print("registered")
            return
        raise SystemExit("not found")
    if command == "add":
        update_state(root, lambda state: state.update(human_registered=True))
        print_json({"ok": True})
        return
    raise SystemExit(f"unsupported human command: {command}")


def main() -> None:
    root = state_root()
    args = sys.argv[1:]
    if not args:
        raise SystemExit("missing command")
    command = args[0]
    update_state(root, lambda state: state["calls"].append({"argv": args, "at": time.time()}))
    if command == "ps":
        print_json([node_payload(root)])
    elif command == "send":
        handle_send(root, args[1:])
    elif command == "queue":
        handle_queue(root, args[1:])
    elif command == "gateway":
        handle_human(root, args)
    elif command == "_complete":
        complete_prompt(root, args[1], args[2])
    else:
        raise SystemExit(f"unsupported fake command: {command}")


if __name__ == "__main__":
    main()
