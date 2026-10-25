from __future__ import annotations

import fcntl
import os
from pathlib import Path
from typing import TextIO

INSTANCE_LOCK_NAME = "bridge.lock"


class InstanceAlreadyRunning(RuntimeError):
    pass


def _lock_path(state_dir: Path) -> Path:
    return state_dir / INSTANCE_LOCK_NAME


def acquire_instance_lock(state_dir: Path) -> TextIO:
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = _lock_path(state_dir)
    handle = path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise InstanceAlreadyRunning(
            f"the Matrix bridge is already running for state directory {state_dir}; "
            "stop the existing bridge before starting another instance"
        ) from exc
    handle.seek(0)
    handle.truncate()
    handle.write(f"{os.getpid()}\n")
    handle.flush()
    return handle


def instance_is_running(state_dir: Path) -> bool:
    path = _lock_path(state_dir)
    if not path.is_file():
        return False
    with path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return False
