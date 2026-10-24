import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from openrig_matrix_bridge.cli import (
    _agent_key,
    _ensure_human_registration,
    _human_entity_id,
    run_send_file,
)
from openrig_matrix_bridge.config import SAFE_AGENT_KEY


def test_human_entity_id_is_derived_from_matrix_user():
    assert _human_entity_id("@owner:matrix.example.org") == "owner"
    assert _human_entity_id("@First.Last:matrix.example.org") == "first-last"


def test_agent_key_bounds_long_session_and_collision_expansion():
    session = f"{'a' * 100}@rig"

    primary = _agent_key(session)
    collision_expanded = _agent_key(f"second-rig-{session}")

    assert len(primary) == 64
    assert len(collision_expanded) == 64
    assert primary != collision_expanded
    assert SAFE_AGENT_KEY.fullmatch(primary)
    assert SAFE_AGENT_KEY.fullmatch(collision_expanded)
    assert primary == _agent_key(session)


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


@pytest.mark.parametrize(
    ("filename", "expected_mime"),
    [
        ("Quarterly report.pdf", "application/pdf"),
        ("payload.openrig-unknown", "application/octet-stream"),
    ],
)
def test_send_file_queues_original_metadata(
    tmp_path,
    filename,
    expected_mime,
):
    source = tmp_path / filename
    source.write_bytes(b"attachment payload")
    state_dir = tmp_path / "state"
    config = SimpleNamespace(
        agents={"advisor": object()},
        state_dir=state_dir,
        media=SimpleNamespace(max_file_bytes=1024),
    )
    args = SimpleNamespace(
        agent="advisor",
        path=source,
        caption="Returned artifact",
        mime_type=None,
        wait_seconds=0,
    )

    result = run_send_file(args, config)

    assert result == 1
    requests = list((state_dir / "media-outbox").glob("*.json"))
    assert len(requests) == 1
    request = json.loads(requests[0].read_text(encoding="utf-8"))
    assert request["kind"] == "file"
    assert request["agent"] == "advisor"
    assert request["path"] == str(source)
    assert request["filename"] == filename
    assert request["mime_type"] == expected_mime
    assert request["caption"] == "Returned artifact"


def test_send_file_rejects_oversized_file_with_effective_limit(tmp_path):
    source = tmp_path / "too-large.bin"
    source.write_bytes(b"x" * 11)
    config = SimpleNamespace(
        agents={"advisor": object()},
        state_dir=tmp_path / "state",
        media=SimpleNamespace(max_file_bytes=10),
    )
    args = SimpleNamespace(
        agent="advisor",
        path=source,
        caption=None,
        mime_type=None,
        wait_seconds=0,
    )

    with pytest.raises(SystemExit, match="10 bytes"):
        run_send_file(args, config)
