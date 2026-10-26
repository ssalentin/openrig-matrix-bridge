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

TERMINAL_STATES = {"done", "failed", "canceled", "denied"}
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
        "steers": [],
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


def append_user_message(root: Path, turn_id: str, text: str) -> None:
    record = {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": text}],
            "internal_chat_message_metadata_passthrough": {
                "turn_id": turn_id,
            },
        },
    }
    with (root / "transcript.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, separators=(",", ":")) + "\n")


def append_agent_message(
    root: Path,
    turn_id: str,
    phase: str,
    text: str,
) -> None:
    record = json.loads(json.dumps(TRANSCRIPT_RECORD))
    record["payload"]["turn_id"] = turn_id
    item = {
        "type": "AgentMessage",
        "id": f"fake-{uuid.uuid4().hex}",
        "phase": phase,
        "content": [{"type": "Text", "text": text}],
    }
    record["payload"]["item"] = item
    with (root / "transcript.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, separators=(",", ":")) + "\n")


def hold_seconds() -> float:
    """How long a "hold first" prompt keeps its turn running."""
    return float(os.environ.get("OPENRIG_FAKE_HOLD_SECONDS", "3.2") or "3.2")


def active_prompt(state: dict[str, Any]) -> dict[str, Any] | None:
    return next(
        (item for item in reversed(state["prompts"]) if item.get("completed_at") is None),
        None,
    )


def turn_active(state: dict[str, Any]) -> bool:
    """A turn is running until it completes, not merely until its hold ends."""
    return (
        float(state.get("busy_until", 0)) > time.time()
        or bool(state.get("late_steer_pending"))
        or active_prompt(state) is not None
    )


def node_payload(root: Path) -> dict[str, Any]:
    state = read_state(root)
    busy = turn_active(state)
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


def response_copy(variable: str, default: str, prompt: str) -> str:
    template = os.environ.get(variable, default)
    return template.replace("{prompt}", prompt)


def option(args: list[str], name: str) -> str | None:
    try:
        return args[args.index(name) + 1]
    except (ValueError, IndexError):
        return None


def complete_prompt(root: Path, prompt: str, prompt_id: str) -> None:
    delay = (
        hold_seconds()
        if "hold first" in prompt.lower()
        else float(os.environ.get("OPENRIG_FAKE_RESPONSE_DELAY", "1.2"))
    )
    time.sleep(delay)
    state = read_state(root)
    prompt_record = next(item for item in state["prompts"] if item["id"] == prompt_id)
    turn_id = str(prompt_record["turn_id"])
    append_agent_message(
        root,
        turn_id,
        "commentary",
        response_copy(
            "OPENRIG_FAKE_COMMENTARY",
            "Fake transport received: {prompt}",
            prompt[:80],
        ),
    )
    time.sleep(0.15)

    def ready(state: dict[str, Any]) -> None:
        if not state.get("late_steer_pending"):
            state["busy_until"] = 0.0
        for record in reversed(state["prompts"]):
            if record["id"] == prompt_id:
                record["completed_at"] = time.time()
                break

    update_state(root, ready)
    if os.environ.get("OPENRIG_FAKE_UNRELATED_TURN") == "1":
        append_user_message(root, "unrelated-human-turn", "local human task")
        append_agent_message(
            root,
            "unrelated-human-turn",
            "final_answer",
            "Unrelated human answer that must not reach Matrix.",
        )
    answer = response_copy(
        "OPENRIG_FAKE_ANSWER",
        "Fake integration answer: {prompt}",
        prompt[:120],
    )
    append_agent_message(root, turn_id, "final_answer", answer)


def materialize_late_steer(
    root: Path,
    prompt: str,
    turn_id: str,
    delay: float,
    source_turn_id: str,
) -> None:
    """Admit busy-pane input as the next turn once the source turn is done."""
    time.sleep(delay)
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        source = next(
            (
                item
                for item in read_state(root)["prompts"]
                if str(item.get("turn_id")) == source_turn_id
            ),
            None,
        )
        if source is None or source.get("completed_at") is not None:
            break
        time.sleep(0.05)
    append_user_message(root, turn_id, prompt)
    time.sleep(0.15)
    answer = response_copy(
        "OPENRIG_FAKE_LATE_ANSWER",
        "Late steering answer: {prompt}",
        prompt[:120],
    )
    append_agent_message(root, turn_id, "final_answer", answer)

    def ready(state: dict[str, Any]) -> None:
        state["busy_until"] = 0.0
        state["late_steer_pending"] = False
        state["late_steer_completed_at"] = time.time()

    update_state(root, ready)


def handle_send(root: Path, args: list[str]) -> None:
    if len(args) < 2:
        raise SystemExit("send requires a destination and prompt")
    destination, prompt = args[0], args[1]
    if destination != SESSION:
        raise SystemExit(f"unknown session: {destination}")
    delay = (
        hold_seconds()
        if "hold first" in prompt.lower()
        else float(os.environ.get("OPENRIG_FAKE_RESPONSE_DELAY", "1.2"))
    )
    prompt_id = uuid.uuid4().hex
    turn_id = str(uuid.uuid4())
    steered_turn_id: str | None = None
    late_turn_id: str | None = None
    source_turn_id: str | None = None
    late_delay = float(os.environ.get("OPENRIG_FAKE_LATE_STEER_SECONDS", "0") or "0")

    def record(state: dict[str, Any]) -> None:
        nonlocal late_turn_id, source_turn_id, steered_turn_id
        if turn_active(state):
            if os.environ.get("OPENRIG_FAKE_ALLOW_STEERING") != "1":
                raise TransportBusy(
                    f"agent is busy; refusing concurrent send to {destination}"
                )
            active = active_prompt(state)
            if active is None:
                raise TransportBusy("agent is busy without an active turn")
            steered_turn_id = (
                uuid.uuid4().hex if late_delay > 0 else str(active["turn_id"])
            )
            source_turn_id = str(active["turn_id"])
            if late_delay > 0:
                late_turn_id = steered_turn_id
                state["late_steer_pending"] = True
                state["busy_until"] = max(
                    float(state.get("busy_until", 0)),
                    time.time() + late_delay + 0.4,
                )
            state.setdefault("steers", []).append(
                {
                    "destination": destination,
                    "prompt": prompt,
                    "turn_id": steered_turn_id,
                    "source_turn_id": active["turn_id"],
                    "late_materialization": late_delay > 0,
                    "sent_at": time.time(),
                }
            )
            return
        state["prompts"].append(
            {
                "id": prompt_id,
                "turn_id": turn_id,
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
    if steered_turn_id is not None:
        if late_turn_id is None:
            append_user_message(root, steered_turn_id, prompt)
        else:
            subprocess.Popen(
                [
                    sys.executable,
                    __file__,
                    "_late_steer",
                    prompt,
                    late_turn_id,
                    str(late_delay),
                    str(source_turn_id),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                env=os.environ.copy(),
            )
        print_json(
            {
                "delivered": True,
                "steered": True,
                "session": destination,
                "turnId": steered_turn_id,
                "materialization": "late" if late_turn_id else "immediate",
            }
        )
        return
    append_user_message(root, turn_id, prompt)
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
    injected_failure = os.environ.get("OPENRIG_FAKE_FAIL")
    if command == "create":
        if injected_failure == "create":
            raise SystemExit("injected queue create failure")
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

        marker = os.environ.get("OPENRIG_FAKE_SELF_COMPLETE_MARKER")
        if marker and marker in str(item["body"] or ""):
            # The seat polls, claims and completes this row itself before the
            # bridge gets to claim it (#91).
            now = time.time()
            item["state"] = "done"
            item["claimed_by"] = item["destinationSession"]
            item["closure_reason"] = "no-follow-on"
            item["resolution"] = "handled by the seat"
            item["history"] += [
                {"state": "in-progress", "at": now},
                {"state": "done", "at": now},
            ]

        def create(state: dict[str, Any]) -> None:
            state["queue"][qitem_id] = item
            if item["state"] == "done":
                state.setdefault("self_completed", []).append(qitem_id)

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
        if command == "claim" and injected_failure == "claim":
            raise SystemExit("injected queue claim failure")
        if command == "update":
            target_state = option(args, "--state")
            if target_state == "done" and injected_failure == "finish":
                raise SystemExit("injected queue finish failure")
            if target_state == "failed" and injected_failure == "fail":
                raise SystemExit("injected queue fail failure")

        def mutate(state: dict[str, Any]) -> None:
            item = state["queue"][qitem_id]
            if command == "claim":
                # Like `rig queue claim`: only a pending row can be claimed.
                if item["state"] != "pending":
                    raise SystemExit("qitem_not_claimable")
                item["state"] = "in-progress"
                item["claimed_by"] = os.environ.get("OPENRIG_SESSION_NAME")
            else:
                # Same admission rule as the real `rig queue update`: on a row that
                # is already closed, an update is a state-preserving note append and
                # accepts only --note (and an optional same --state).
                if item["state"] in TERMINAL_STATES and any(
                    option(args, flag)
                    for flag in ("--closure-reason", "--closure-target", "--resolution")
                ):
                    raise SystemExit(
                        "note_append_fields_not_admitted: a state-preserving note "
                        "append accepts only --note (and an optional same --state); "
                        "state-write fields were supplied"
                    )
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
    update_state(
        root, lambda state: state["calls"].append({"argv": args, "at": time.time()})
    )
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
    elif command == "_late_steer":
        materialize_late_steer(root, args[1], args[2], float(args[3]), args[4])
    else:
        raise SystemExit(f"unsupported fake command: {command}")


if __name__ == "__main__":
    main()
