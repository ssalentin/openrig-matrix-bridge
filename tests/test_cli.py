import hashlib
import json
import os
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
    assert request["size"] == len(b"attachment payload")
    assert request["sha256"] == hashlib.sha256(b"attachment payload").hexdigest()
    snapshot = Path(request["snapshot_path"])
    assert snapshot.parent == state_dir / "media-outbox" / "files"
    assert snapshot.read_bytes() == b"attachment payload"
    assert snapshot.stat().st_mode & 0o777 == 0o600

    source.write_bytes(b"changed after admission")
    assert snapshot.read_bytes() == b"attachment payload"


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


def test_send_file_rejects_symlink_source(tmp_path):
    target = tmp_path / "target.pdf"
    target.write_bytes(b"%PDF-1.7\nsymlink target\n")
    source = tmp_path / "linked.pdf"
    source.symlink_to(target)
    config = SimpleNamespace(
        agents={"advisor": object()},
        state_dir=tmp_path / "state",
        media=SimpleNamespace(max_file_bytes=1024),
    )
    args = SimpleNamespace(
        agent="advisor",
        path=source,
        caption=None,
        mime_type=None,
        wait_seconds=0,
    )

    with pytest.raises(SystemExit, match="regular file"):
        run_send_file(args, config)

    assert not (tmp_path / "state" / "media-outbox").exists()


def test_send_file_rejects_non_regular_source(tmp_path):
    source = tmp_path / "directory.pdf"
    source.mkdir()
    config = SimpleNamespace(
        agents={"advisor": object()},
        state_dir=tmp_path / "state",
        media=SimpleNamespace(max_file_bytes=1024),
    )
    args = SimpleNamespace(
        agent="advisor",
        path=source,
        caption=None,
        mime_type=None,
        wait_seconds=0,
    )

    with pytest.raises(SystemExit, match="regular file"):
        run_send_file(args, config)


def test_send_file_rejects_symlink_swapped_after_precheck(tmp_path, monkeypatch):
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-1.7\noriginal\n")
    target = tmp_path / "target.pdf"
    target.write_bytes(b"%PDF-1.7\nreplacement\n")
    config = SimpleNamespace(
        agents={"advisor": object()},
        state_dir=tmp_path / "state",
        media=SimpleNamespace(max_file_bytes=1024),
    )
    args = SimpleNamespace(
        agent="advisor",
        path=source,
        caption=None,
        mime_type=None,
        wait_seconds=0,
    )
    real_open = os.open
    swapped = False

    def swap_then_open(path, flags, *open_args, **open_kwargs):
        nonlocal swapped
        if not swapped and Path(path) == source:
            swapped = True
            source.unlink()
            source.symlink_to(target)
        return real_open(path, flags, *open_args, **open_kwargs)

    monkeypatch.setattr(os, "open", swap_then_open)

    with pytest.raises(SystemExit, match="regular file"):
        run_send_file(args, config)

    assert not (tmp_path / "state" / "media-outbox").exists()


def _send_file_args(path, **overrides):
    values = {
        "agent": "advisor",
        "path": path,
        "caption": None,
        "mime_type": None,
        "wait_seconds": 0,
        "json": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _send_file_config(tmp_path, max_file_bytes=1024):
    return SimpleNamespace(
        agents={"advisor": object()},
        state_dir=tmp_path / "state",
        media=SimpleNamespace(max_file_bytes=max_file_bytes),
    )


def _assert_nothing_queued(tmp_path):
    outbox = tmp_path / "state" / "media-outbox"
    assert not outbox.exists() or not any(outbox.rglob("*.json"))
    assert not (outbox / "files").exists() or not any((outbox / "files").iterdir())


def test_send_file_rejects_empty_file_before_queueing(tmp_path):
    source = tmp_path / "label.pdf"
    source.write_bytes(b"")

    with pytest.raises(SystemExit, match="between 1 and 1024 bytes"):
        run_send_file(_send_file_args(source), _send_file_config(tmp_path))

    _assert_nothing_queued(tmp_path)


def test_send_file_rejects_missing_file_before_queueing(tmp_path):
    with pytest.raises(SystemExit, match="file not found"):
        run_send_file(
            _send_file_args(tmp_path / "missing.pdf"),
            _send_file_config(tmp_path),
        )

    _assert_nothing_queued(tmp_path)


def test_send_file_rejects_unreadable_file_before_queueing(tmp_path):
    source = tmp_path / "private.pdf"
    source.write_bytes(b"%PDF-1.7\nprivate\n")
    source.chmod(0)
    try:
        source.read_bytes()
    except PermissionError:
        pass
    else:
        pytest.skip("this account can read a mode 000 file (root or DAC capability)")

    with pytest.raises(SystemExit, match="could not be opened"):
        run_send_file(_send_file_args(source), _send_file_config(tmp_path))

    _assert_nothing_queued(tmp_path)


def _confirm_delivery_from_a_fake_bridge(state_dir, event_id, room_id):
    import threading
    import time

    def bridge():
        outbox = state_dir / "media-outbox"
        for _ in range(200):
            requests = list(outbox.glob("file-*.json")) if outbox.exists() else []
            if requests:
                request = json.loads(requests[0].read_text(encoding="utf-8"))
                (state_dir / "state.json").write_text(
                    json.dumps(
                        {
                            "media": {
                                request["id"]: {
                                    "event_id": event_id,
                                    "room_id": room_id,
                                }
                            }
                        }
                    ),
                    encoding="utf-8",
                )
                return
            time.sleep(0.05)

    thread = threading.Thread(target=bridge)
    thread.start()
    return thread


def test_send_file_json_reports_the_event_for_the_agent_to_quote(tmp_path, capsys):
    payload = b"%PDF-1.7\nshipping label\n"
    source = tmp_path / "Shipping label.pdf"
    source.write_bytes(payload)
    config = _send_file_config(tmp_path)
    thread = _confirm_delivery_from_a_fake_bridge(
        config.state_dir,
        "$event-label",
        "!advisor:matrix.example.org",
    )

    result = run_send_file(
        _send_file_args(source, json=True, wait_seconds=10),
        config,
    )
    thread.join()

    assert result == 0
    report = json.loads(capsys.readouterr().out)
    assert report == {
        "ok": True,
        "request_id": report["request_id"],
        "event_id": "$event-label",
        "room_id": "!advisor:matrix.example.org",
        "agent": "advisor",
        "filename": "Shipping label.pdf",
        "mimetype": "application/pdf",
        "size": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }
    assert report["request_id"].startswith("file-")


def test_send_file_json_reports_an_unconfirmed_delivery_as_failure(tmp_path, capsys):
    source = tmp_path / "label.pdf"
    source.write_bytes(b"%PDF-1.7\nlabel\n")

    result = run_send_file(
        _send_file_args(source, json=True),
        _send_file_config(tmp_path),
    )

    assert result == 1
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is False
    assert report["request_id"].startswith("file-")
    assert "event_id" not in report
    assert "did not confirm delivery" in report["error"]


def test_send_file_plain_output_is_unchanged_without_json(tmp_path, capsys):
    source = tmp_path / "label.pdf"
    source.write_bytes(b"%PDF-1.7\nlabel\n")

    result = run_send_file(_send_file_args(source), _send_file_config(tmp_path))

    assert result == 1
    output = capsys.readouterr().out.strip()
    assert output.startswith("queued file-")
    assert output.endswith("the bridge did not confirm delivery within 0s")
