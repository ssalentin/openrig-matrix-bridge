from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from openrig_matrix_bridge.bridge import MatrixBridge
from openrig_matrix_bridge.config import AgentConfig
from openrig_matrix_bridge.openrig import GatewayError, OpenRigGateway

ROOT = Path(__file__).resolve().parents[1]
FAKE = ROOT / "tools" / "fake_openrig.py"
SESSION = "integration-agent@local"


def fake(root: Path, *args: str, session: str = SESSION):
    return subprocess.run(
        [str(FAKE), *args],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "OPENRIG_FAKE_STATE": str(root),
            "OPENRIG_SESSION_NAME": session,
        },
        timeout=10,
    )


def create_claimed_qitem(root: Path) -> str:
    created = fake(
        root,
        "queue",
        "create",
        "--source",
        "owner@external",
        "--destination",
        SESSION,
        "--summary",
        "Matrix message",
        "--body",
        "hello",
        "--priority",
        "urgent",
        "--tags",
        "matrix,inbound",
        "--no-nudge",
        "--json",
    )
    qitem_id = json.loads(created.stdout)["qitemId"]
    assert fake(root, "queue", "claim", qitem_id, "--json").returncode == 0
    return qitem_id


def agent_closes(root: Path, qitem_id: str) -> None:
    closed = fake(
        root,
        "queue",
        "update",
        qitem_id,
        "--state",
        "done",
        "--closure-reason",
        "no-follow-on",
        "--resolution",
        "Answered by the agent",
        "--json",
    )
    assert closed.returncode == 0, closed.stderr


def history(root: Path, qitem_id: str) -> list[str]:
    state = json.loads((root / "fake-openrig.json").read_text())
    return [entry["state"] for entry in state["queue"][qitem_id]["history"]]


class StaleShowGateway(OpenRigGateway):
    """Reports the row as still open once, as if it closed after the show."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.stale_shows = 1

    async def queue_show(self, qitem_id: str):
        item = await super().queue_show(qitem_id)
        if self.stale_shows:
            self.stale_shows -= 1
            return {**item, "state": "in-progress"}
        return item


def make_bridge(gateway: OpenRigGateway) -> MatrixBridge:
    bridge = object.__new__(MatrixBridge)
    bridge.gateway = gateway
    bridge.state = SimpleNamespace(save=lambda: None, data={})
    return bridge


def make_agent() -> AgentConfig:
    return AgentConfig(
        key="integration",
        display_name="Integration",
        session=SESSION,
    )


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENRIG_FAKE_STATE", str(tmp_path))
    monkeypatch.delenv("OPENRIG_BRIDGE_TEST_CRASH_AFTER", raising=False)
    return tmp_path


def finish(bridge: MatrixBridge, request: dict) -> bool:
    return asyncio.run(bridge._finish_inbound_qitem(make_agent(), request))


def test_finish_closes_an_open_qitem_once(rig):
    qitem_id = create_claimed_qitem(rig)
    bridge = make_bridge(OpenRigGateway(str(FAKE), SESSION))
    request = {"qitem_id": qitem_id, "qitem_phase": "claimed"}

    assert finish(bridge, request) is True

    assert request["qitem_phase"] == "done"
    assert history(rig, qitem_id) == ["pending", "in-progress", "done"]


def test_agent_closed_qitem_is_success_without_another_update(rig, caplog):
    qitem_id = create_claimed_qitem(rig)
    agent_closes(rig, qitem_id)
    bridge = make_bridge(OpenRigGateway(str(FAKE), SESSION))
    request = {"qitem_id": qitem_id, "qitem_phase": "claimed"}

    assert finish(bridge, request) is True

    assert request["qitem_phase"] == "done"
    assert history(rig, qitem_id) == ["pending", "in-progress", "done"]
    assert "could not finish Matrix qitem" not in caplog.text


def test_state_change_between_show_and_update_is_reconciled(rig, caplog):
    qitem_id = create_claimed_qitem(rig)
    agent_closes(rig, qitem_id)
    bridge = make_bridge(StaleShowGateway(str(FAKE), SESSION))
    request = {"qitem_id": qitem_id, "qitem_phase": "claimed"}

    assert finish(bridge, request) is True

    assert request["qitem_phase"] == "done"
    assert history(rig, qitem_id) == ["pending", "in-progress", "done"]
    assert "could not finish Matrix qitem" not in caplog.text


def test_finish_is_idempotent_across_retries_after_a_restart(rig):
    qitem_id = create_claimed_qitem(rig)
    agent_closes(rig, qitem_id)
    bridge = make_bridge(OpenRigGateway(str(FAKE), SESSION))
    request = {"qitem_id": qitem_id, "qitem_phase": "finishing"}

    assert finish(bridge, request) is True
    assert finish(bridge, request) is True

    assert request["qitem_phase"] == "done"
    assert history(rig, qitem_id) == ["pending", "in-progress", "done"]


def test_a_failed_update_on_an_open_qitem_still_reports_failure(rig):
    qitem_id = create_claimed_qitem(rig)
    bridge = make_bridge(OpenRigGateway(str(FAKE), SESSION))
    request = {"qitem_id": qitem_id, "qitem_phase": "claimed"}

    async def broken_finish(*_args):
        raise GatewayError("OpenRig command failed: injected")

    bridge.gateway.queue_finish = broken_finish

    assert finish(bridge, request) is False

    assert request["qitem_phase"] == "finishing"
    assert history(rig, qitem_id) == ["pending", "in-progress"]


def test_fake_rig_enforces_the_note_append_admission_rule(rig):
    qitem_id = create_claimed_qitem(rig)
    agent_closes(rig, qitem_id)

    again = fake(
        rig,
        "queue",
        "update",
        qitem_id,
        "--state",
        "done",
        "--closure-reason",
        "no-follow-on",
        "--resolution",
        "Matrix answer delivered",
        "--note",
        "Matrix answer delivered by openrig-matrix-bridge",
        "--json",
    )

    assert again.returncode != 0
    assert "note_append_fields_not_admitted" in again.stderr
    assert fake(
        rig, "queue", "update", qitem_id, "--note", "late note", "--json"
    ).returncode == 0
    assert history(rig, qitem_id)[-1] == "done"


@pytest.mark.skipif(shutil.which("rig") is None, reason="OpenRig `rig` is not on PATH")
def test_gateway_finish_syntax_is_admitted_by_the_installed_rig():
    help_text = subprocess.run(
        ["rig", "queue", "update", "--help"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout
    for flag in ("--state", "--closure-reason", "--resolution", "--note", "--json"):
        assert flag in help_text
    assert "no-follow-on" in help_text
    assert "state=done REQUIRES --closure-reason" in help_text
