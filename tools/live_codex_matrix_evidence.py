#!/usr/bin/env python3
"""Collect disposable Matrix-to-live-Codex steering evidence.

The runner starts an ephemeral Synapse, configures the bridge to exactly one
externally owned Codex seat, and sends one long root request plus two rapid
same-thread follow-ups. It retains counts and timings, not Matrix credentials,
room IDs, raw tool output, or full prompt text.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import statistics
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from openrig_matrix_bridge import __version__
from openrig_matrix_bridge.transcript import (
    read_complete_records,
    record_turn_id,
    user_messages,
)
from tools.matrix_integration import (
    BRIDGE_PASSWORD,
    BRIDGE_USER,
    OWNER_PASSWORD,
    OWNER_USER,
    CheckFailure,
    IntegrationRun,
    MatrixOwner,
    run,
    validate_qitem_history,
)


class LiveEvidenceRun(IntegrationRun):
    def __init__(self, args: argparse.Namespace):
        super().__init__(args)
        self.live_session = str(args.live_session)
        self.real_rig = str(args.rig_command or shutil.which("rig") or "")
        self.rounds = max(1, int(args.rounds))
        self.agent_key = ""
        self.agent_session = ""
        self.round_results: list[dict[str, Any]] = []
        self.room_id = ""
        self.side_effect_path = args.effect_path
        self.rig_wrapper = self.work / "rig-live-wrapper"

    def write_rig_wrapper(self) -> None:
        if not self.real_rig:
            raise CheckFailure("live evidence requires a rig command")
        self.rig_wrapper.write_text(
            """#!/usr/bin/env python3
import json
import os
import subprocess
import sys

args = sys.argv[1:]
real = os.environ["OPENRIG_LIVE_REAL_RIG"]
target = os.environ["OPENRIG_LIVE_SESSION"]
if args[:1] == ["ps"] and "--nodes" in args:
    result = subprocess.run([real, *args], capture_output=True, text=True)
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        raise SystemExit(result.returncode)
    nodes = json.loads(result.stdout)
    selected = [
        node for node in nodes
        if node.get("canonicalSessionName") == target
    ]
    if len(selected) != 1:
        raise SystemExit(f"expected one live node for {target}, got {len(selected)}")
    print(json.dumps(selected, sort_keys=True))
    raise SystemExit(0)
os.execv(real, [real, *args])
""",
            encoding="utf-8",
        )
        self.rig_wrapper.chmod(0o700)

    async def setup_live_bridge(self) -> None:
        self.write_rig_wrapper()
        self.bridge_token, bridge_device = await self.http_login(
            "bridge",
            BRIDGE_PASSWORD,
            "OPENRIG_BRIDGE",
        )
        owner_token, owner_device = await self.http_login(
            "owner",
            OWNER_PASSWORD,
            "OPENRIG_OWNER",
        )
        environment = self.env()
        environment["OPENRIG_LIVE_REAL_RIG"] = self.real_rig
        environment["OPENRIG_LIVE_SESSION"] = self.live_session
        setup = run(
            [
                sys.executable,
                "-m",
                "openrig_matrix_bridge",
                "setup",
                "--config",
                str(self.config_path),
                "--homeserver",
                self.homeserver,
                "--user-id",
                BRIDGE_USER,
                "--device-id",
                bridge_device,
                "--allowed-user",
                OWNER_USER,
                "--state-dir",
                str(self.state_dir),
                "--download-root",
                str(self.work / "parakeet"),
                "--rig-command",
                str(self.rig_wrapper),
                "--codex-home",
                str(Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))),
                "--sender-session",
                "matrix-bridge@local",
                "--no-human-registration",
                "--no-commentary",
                "--no-avatars",
                "--skip-model",
            ],
            env=environment,
            timeout=self.args.timeout,
        )
        raw = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        agents = raw.get("agents") if isinstance(raw, dict) else None
        if not isinstance(agents, dict) or len(agents) != 1:
            raise CheckFailure(
                f"live setup did not configure exactly one agent: {setup.stdout}"
            )
        self.agent_key, agent_config = next(iter(agents.items()))
        if not isinstance(agent_config, dict):
            raise CheckFailure("live agent configuration is not a mapping")
        self.agent_session = str(agent_config.get("session") or "")
        if self.agent_session != self.live_session:
            raise CheckFailure(
                f"setup selected {self.agent_session}, expected {self.live_session}"
            )
        doctor = self.cli("doctor")
        if doctor.returncode != 0:
            raise CheckFailure(f"live bridge doctor failed: {doctor.stdout}")
        self.record(
            "live-setup-and-doctor",
            "pass",
            (
                "Public setup discovered exactly the disposable live Codex seat; "
                "doctor validated Matrix token and live session resolution."
            ),
        )
        self.owner = MatrixOwner(
            self.homeserver,
            owner_token,
            owner_device,
            self.work / "owner-store",
        )
        await self.owner.start()

    def state(self) -> dict[str, Any]:
        return json.loads((self.state_dir / "state.json").read_text(encoding="utf-8"))

    async def wait_for_state(
        self,
        predicate,
        description: str,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + (timeout or self.args.timeout)
        last: dict[str, Any] = {}
        while time.monotonic() < deadline:
            try:
                last = self.state()
            except (FileNotFoundError, json.JSONDecodeError):
                await asyncio.sleep(0.1)
                continue
            if predicate(last):
                return last
            await asyncio.sleep(0.1)
        raise CheckFailure(f"timed out waiting for {description}: {last}")

    def transcript_records(self, path: Path) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return records
        for line in lines:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                records.append(value)
        return records

    def records_after(self, path: Path, offset: int) -> list[dict[str, Any]]:
        try:
            records, _new_offset = read_complete_records(path, offset)
        except FileNotFoundError:
            return []
        return records

    @staticmethod
    def shell_tool_seen(records: list[dict[str, Any]], marker: str) -> bool:
        for record in records:
            payload = record.get("payload")
            if not isinstance(payload, dict):
                continue
            payload_type = str(payload.get("type") or "")
            if (
                ("call" in payload_type or "tool" in payload_type)
                and marker in json.dumps(payload, ensure_ascii=False)
            ):
                return True
        return False

    async def wait_for_batch_injection(
        self,
        follow_ids: list[str],
    ) -> tuple[dict[str, Any], dict[str, Any], float]:
        deadline = time.monotonic() + self.args.timeout
        while time.monotonic() < deadline:
            state = self.state()
            pending = state.get("pending", {}).get(self.agent_key, [])
            group = next(
                (
                    request
                    for request in pending
                    if request.get("deferred_steering")
                    and all(
                        event_id in request.get("source_event_ids", [])
                        for event_id in follow_ids
                    )
                ),
                None,
            )
            if isinstance(group, dict) and isinstance(
                group.get("injection_attempted_at"),
                (int, float),
            ):
                return state, group, time.time()
            await asyncio.sleep(0.05)
        raise CheckFailure("live bridge did not record pane injection for the batch")

    async def wait_for_admission(
        self,
        group_id: str,
        *,
        timeout: float,
    ) -> tuple[dict[str, Any], dict[str, Any], float]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self.state()
            pending = state.get("pending", {}).get(self.agent_key, [])
            group = next(
                (
                    request
                    for request in pending
                    if request.get("id") == group_id
                ),
                None,
            )
            if isinstance(group, dict) and group.get("externally_admitted"):
                return state, group, time.time()
            if isinstance(group, dict) and group.get("injection_phase") == "fallback":
                raise CheckFailure(
                    "live steering fell back before transcript admission: "
                    f"{group.get('last_injection_error')}"
                )
            await asyncio.sleep(0.1)
        raise CheckFailure("live steering batch was not admitted from the transcript")

    async def wait_for_effects_and_answer(
        self,
        markers: list[str],
        *,
        timeout: float,
    ) -> list[dict[str, Any]]:
        assert self.owner is not None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            lines = (
                self.side_effect_path.read_text(encoding="utf-8").splitlines()
                if self.side_effect_path.exists()
                else []
            )
            effects_ok = all(lines.count(marker) == 1 for marker in markers)
            await self.owner.sync_once(timeout_ms=1000)
            answers = [
                message
                for message in self.owner.messages
                if message["sender"] == BRIDGE_USER
                and all(marker in str(message["body"]) for marker in markers)
            ]
            if effects_ok and answers:
                return answers
            await asyncio.sleep(0.2)
        lines = (
            self.side_effect_path.read_text(encoding="utf-8").splitlines()
            if self.side_effect_path.exists()
            else []
        )
        raise CheckFailure(
            "live follow-up effects/answer timed out: "
            f"effect_counts={[lines.count(marker) for marker in markers]}"
        )

    async def join_live_room(self) -> str:
        assert self.owner is not None
        deadline = time.monotonic() + self.args.timeout
        while time.monotonic() < deadline:
            await self.owner.sync_once(timeout_ms=1000)
            try:
                self.room_id = str(self.state()["rooms"][self.agent_key]["room_id"])
            except (KeyError, FileNotFoundError, json.JSONDecodeError):
                await asyncio.sleep(0.1)
                continue
            if self.room_id in self.owner.client.rooms:
                return self.room_id
            await asyncio.sleep(0.1)
        raise CheckFailure("owner did not join the live agent room")

    def queue_show(self, qitem_id: str) -> dict[str, Any]:
        result = run(
            [self.real_rig, "queue", "show", qitem_id, "--json"],
            timeout=self.args.timeout,
        )
        value = json.loads(result.stdout)
        if not isinstance(value, dict):
            raise CheckFailure(f"rig queue show returned a non-object for {qitem_id}")
        return value

    async def run_round(self, index: int) -> dict[str, Any]:
        assert self.owner is not None
        token = f"012-{index}-{int(time.time())}"
        root_marker = f"ROOT-{token}"
        follow_one = f"FOLLOW-ONE-{token}"
        follow_two = f"FOLLOW-TWO-{token}"
        root_prompt = (
            "Disposable OpenRig Matrix Bridge release evidence. "
            "Use a shell tool to run `/bin/sleep 12`, wait for it to finish, "
            "and do not edit files during this root turn. Then reply with "
            f"exactly `{root_marker}`."
        )
        root_event = await self.owner.send_text(self.room_id, root_prompt)
        state = await self.wait_for_state(
            lambda value: bool(
                value.get("pending", {}).get(self.agent_key)
                and value["pending"][self.agent_key][0].get("event_id") == root_event
                and value["pending"][self.agent_key][0].get("prompt_phase") == "sent"
                and value["pending"][self.agent_key][0].get("transcript_path")
            ),
            f"root prompt {index} to reach the live seat",
        )
        root_request = state["pending"][self.agent_key][0]
        root_offset = int(root_request["offset"])
        transcript_path = Path(str(root_request["transcript_path"]))
        root_qitem = str(root_request.get("qitem_id") or "")
        tool_deadline = time.monotonic() + min(30, self.args.timeout)
        while time.monotonic() < tool_deadline:
            records = self.records_after(transcript_path, root_offset)
            if self.shell_tool_seen(records, "/bin/sleep 12"):
                break
            await asyncio.sleep(0.1)
        else:
            raise CheckFailure("live Codex did not materialize the long shell call")
        root_messages = [
            (turn_id, text)
            for turn_id, text in user_messages(records)
            if text == root_prompt
        ]
        if len(root_messages) != 1:
            raise CheckFailure(
                f"root prompt materialized {len(root_messages)} times in the transcript"
            )
        root_turn_id = root_messages[0][0]
        follow_relates_to = {
            "rel_type": "m.thread",
            "event_id": root_event,
            "m.in_reply_to": {"event_id": root_event},
        }
        follow_prompts = [
            (
                "Follow-up one for disposable live evidence. Use a shell tool to "
                f"append exactly one line containing `{follow_one}` to "
                f"`{self.side_effect_path}`. Include `{follow_one}` in the answer. "
                "Execute this instruction once."
            ),
            (
                "Follow-up two for disposable live evidence. Use a shell tool to "
                f"append exactly one line containing `{follow_two}` to "
                f"`{self.side_effect_path}`. Include `{follow_two}` in the answer. "
                "Execute this instruction once."
            ),
        ]
        follow_ids = [
            await self.owner.send_text(
                self.room_id,
                prompt,
                relates_to=follow_relates_to,
            )
            for prompt in follow_prompts
        ]
        _state, injected_group, _injection_seen_at = (
            await self.wait_for_batch_injection(follow_ids)
        )
        root_answer_before_injection = any(
            record_turn_id(record) == root_turn_id
            and record.get("payload", {}).get("item", {}).get("phase")
            == "final_answer"
            for record in self.records_after(transcript_path, root_offset)
        )
        if root_answer_before_injection:
            raise CheckFailure(
                "live steering was attempted only after the root final answer"
            )
        group_id = str(injected_group["id"])
        _state, group, admission_seen_at = await self.wait_for_admission(
            group_id,
            timeout=self.args.timeout,
        )
        group_text = str(group.get("injection_text") or group.get("text") or "")
        injection_offset = int(group["injection_offset"])
        records = self.records_after(transcript_path, injection_offset)
        admitted_users = [
            (turn_id, text)
            for turn_id, text in user_messages(records)
            if text == group_text
        ]
        if len(admitted_users) != 1:
            raise CheckFailure(
                "live batch transcript materialization count was "
                f"{len(admitted_users)}, expected 1"
            )
        follow_turn_id = admitted_users[0][0]
        answers = await self.wait_for_effects_and_answer(
            [follow_one, follow_two],
            timeout=self.args.timeout,
        )
        if len(answers) != 1:
            raise CheckFailure(
                f"live batch produced {len(answers)} Matrix answers, expected 1"
            )
        raw_answer = await self.owner.raw_event(self.room_id, answers[0]["event_id"])
        relation = raw_answer.get("content", {}).get("m.relates_to")
        if not isinstance(relation, dict) or relation.get("event_id") != root_event:
            raise CheckFailure("live batch answer did not use the canonical thread root")
        source_ids = raw_answer.get("content", {}).get("dev.openrig.source_event_ids")
        if not isinstance(source_ids, list) or not all(
            event_id in source_ids for event_id in follow_ids
        ):
            raise CheckFailure(
                "live batch answer did not preserve both follow-up source IDs: "
                f"{source_ids}"
            )
        all_records = self.transcript_records(transcript_path)
        follow_finals = [
            record
            for record in all_records
            if record_turn_id(record) == follow_turn_id
            and record.get("payload", {}).get("item", {}).get("phase")
            == "final_answer"
        ]
        if len(follow_finals) != 1:
            raise CheckFailure(
                f"live follow-up turn produced {len(follow_finals)} final answers"
            )
        qitem_ids = [
            str(value) for value in group.get("qitem_ids", []) if str(value)
        ]
        if len(qitem_ids) != 2:
            raise CheckFailure(
                f"live steering batch retained {len(qitem_ids)} qitems, expected 2"
            )
        await self.wait_for_state(
            lambda value: not value.get("pending", {}).get(self.agent_key),
            f"round {index} pending queue to drain",
        )
        for qitem_id in [*qitem_ids, root_qitem]:
            if not qitem_id:
                raise CheckFailure("live request did not expose a real qitem ID")
            item = self.queue_show(qitem_id)
            if item.get("state") != "done":
                raise CheckFailure(
                    f"live qitem {qitem_id} did not close: {item.get('state')}"
                )
            validate_qitem_history(item)
        lines = self.side_effect_path.read_text(encoding="utf-8").splitlines()
        effect_counts = {
            "follow_one": lines.count(follow_one),
            "follow_two": lines.count(follow_two),
        }
        if effect_counts != {"follow_one": 1, "follow_two": 1}:
            raise CheckFailure(
                f"live side effects were not exactly once: {effect_counts}"
            )
        entries = group.get("steering_entries", [])
        injected_at = float(group["injection_attempted_at"])
        intake_to_pane_ms = [
            round((injected_at - float(entry["enqueued_at"])) * 1000, 1)
            for entry in entries
            if isinstance(entry, dict)
            and isinstance(entry.get("enqueued_at"), (int, float))
        ]
        if len(intake_to_pane_ms) != 2:
            raise CheckFailure("live batch did not expose both intake timestamps")
        return {
            "round": index,
            "root_turn": root_turn_id,
            "follow_turn": follow_turn_id,
            "different_turn": follow_turn_id != root_turn_id,
            "injection_before_root_completion": True,
            "transcript_batch_materializations": len(admitted_users),
            "transcript_final_answers": len(follow_finals),
            "matrix_answers": len(answers),
            "matrix_source_event_ids": len(source_ids),
            "qitems_done": len(qitem_ids) + 1,
            "side_effect_counts": effect_counts,
            "intake_to_pane_ms": intake_to_pane_ms,
            "intake_to_admission_ms": round(
                (
                    admission_seen_at
                    - min(float(entry["enqueued_at"]) for entry in entries)
                )
                * 1000,
                1,
            ),
            "pane_to_admission_ms": round(
                (admission_seen_at - injected_at) * 1000,
                1,
            ),
        }

    async def execute_live(self) -> None:
        self.args.report.parent.mkdir(parents=True, exist_ok=True)
        self.side_effect_path.parent.mkdir(parents=True, exist_ok=True)
        self.side_effect_path.unlink(missing_ok=True)
        await self.start_synapse()
        self.record(
            "ephemeral-matrix",
            "pass",
            "Started disposable Synapse on loopback for live-Codex steering evidence.",
        )
        await self.setup_live_bridge()
        self.start_bridge()
        await self.join_live_room()
        for index in range(1, self.rounds + 1):
            self.round_results.append(await self.run_round(index))
            await asyncio.sleep(0.5)
        self.record(
            "live-codex-steering",
            "pass",
            (
                f"{self.rounds} long-tool rounds delivered two rapid same-thread "
                "Matrix follow-ups, admitted matching transcript input, produced "
                "one answer per batch, closed every qitem, and executed each side "
                "effect exactly once."
            ),
        )

    def write_live_report(self) -> None:
        pane_samples = [
            value
            for item in self.round_results
            for value in item["intake_to_pane_ms"]
        ]
        admission_samples = [
            item["intake_to_admission_ms"] for item in self.round_results
        ]
        pane_to_admission = [
            item["pane_to_admission_ms"] for item in self.round_results
        ]

        def summary(samples: list[float]) -> dict[str, Any]:
            return {
                "samples": len(samples),
                "median_ms": round(statistics.median(samples), 1),
                "max_ms": round(max(samples), 1),
                "values_ms": samples,
            }

        report = {
            "schema_version": 1,
            "generated_at": datetime.now(UTC).isoformat(),
            "status": "pass",
            "evidence_valid": self.source_provenance_error is None,
            "target": {
                "package": "openrig-matrix-bridge",
                "version": __version__,
                "source": self.source_provenance,
            },
            "environment": {
                "matrix": "ephemeral Synapse on loopback",
                "openrig_transport": (
                    "real rig CLI to a dedicated disposable Codex seat"
                ),
                "live_seat": self.live_session,
                "seat_count": 1,
                "rounds": self.rounds,
            },
            "method": {
                "prompt": (
                    "One 12-second shell call followed by two rapid "
                    "same-thread messages."
                ),
                "latency": (
                    "Persisted bridge enqueue/injection times plus the first "
                    "poll after external transcript admission."
                ),
            },
            "latency": {
                "intake_to_pane_injection": summary(pane_samples),
                "intake_to_transcript_admission": summary(admission_samples),
                "pane_injection_to_admission": summary(pane_to_admission),
            },
            "rounds": self.round_results,
            "limits": [
                "The OpenRig pane API exposes no acknowledged safe-boundary token.",
                "Transcript admission polling adds up to one bridge poll interval.",
                "The live seat, Matrix homeserver, and effects file are disposable.",
            ],
            "redaction": {
                "credentials": "not written",
                "room_and_event_ids": "counts only",
                "prompt_text": "omitted",
                "tool_output": "omitted",
                "side_effect_content": "counts only",
            },
        }
        if self.source_provenance_error:
            report["evidence_valid"] = False
            report["provenance_error"] = self.source_provenance_error
        self.report_path.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-session", required=True)
    parser.add_argument("--rig-command", default=shutil.which("rig"))
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument(
        "--report",
        type=Path,
        default=ROOT / "docs" / "artifacts" / "live-codex-steering-evidence.json",
    )
    parser.add_argument("--source-commit")
    parser.add_argument("--keep-workdir", action="store_true")
    return parser


if __name__ == "__main__":
    raise SystemExit("live evidence runner is not enabled until invoked directly")
