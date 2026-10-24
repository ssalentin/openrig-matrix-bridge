import subprocess
from pathlib import Path

from openrig_matrix_bridge.cli import (
    _ensure_human_registration,
    _human_entity_id,
)


def test_human_entity_id_is_derived_from_matrix_user():
    assert _human_entity_id("@owner:matrix.example.org") == "owner"
    assert _human_entity_id("@First.Last:matrix.example.org") == "first-last"


def test_setup_registers_missing_human_with_reusable_binding(monkeypatch):
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        if "show" in args:
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="missing")
        return subprocess.CompletedProcess(args, 0, stdout='{"ok":true}', stderr="")

    monkeypatch.setattr(subprocess, "run", run)

    result = _ensure_human_registration(
        command="/opt/rig",
        entity_id="owner",
        display_name="Owner",
        delivery_class="B",
        state_dir=Path("/var/lib/openrig-matrix-bridge"),
        actor="matrix-bridge@kernel",
        replace=False,
    )

    assert result == "added"
    add_args = calls[1][0]
    assert add_args[:5] == [
        "/opt/rig",
        "gateway",
        "human",
        "add",
        "owner",
    ]
    assert (
        "slack:openrig-matrix:file:/var/lib/openrig-matrix-bridge/"
        "human-delivery.env:primary"
    ) in add_args
    assert add_args[-2:] == ["--actor", "matrix-bridge@kernel"]


def test_setup_retains_existing_human(monkeypatch):
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="owner", stderr="")

    monkeypatch.setattr(subprocess, "run", run)

    result = _ensure_human_registration(
        command="/opt/rig",
        entity_id="owner",
        display_name="Owner",
        delivery_class="B",
        state_dir=Path("/tmp/state"),
        actor="matrix-bridge@kernel",
        replace=False,
    )

    assert result == "retained"
    assert len(calls) == 1


def test_setup_replaces_existing_human_only_when_requested(monkeypatch):
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="ok", stderr="")

    monkeypatch.setattr(subprocess, "run", run)

    result = _ensure_human_registration(
        command="/opt/rig",
        entity_id="owner",
        display_name="Owner",
        delivery_class="B",
        state_dir=Path("/tmp/state"),
        actor="matrix-bridge@kernel",
        replace=True,
    )

    assert result == "replaced"
    assert calls[1][4] == "owner"
    assert "--replace" in calls[1]
