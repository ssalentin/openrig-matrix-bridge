from pathlib import Path

import pytest

from openrig_matrix_bridge.instance import (
    InstanceAlreadyRunning,
    acquire_instance_lock,
)


def test_instance_lock_prevents_shared_state_from_two_processes(tmp_path: Path):
    state_dir = tmp_path / "state"
    first = acquire_instance_lock(state_dir)
    try:
        with pytest.raises(InstanceAlreadyRunning, match="already running"):
            acquire_instance_lock(state_dir)
    finally:
        first.close()

    second = acquire_instance_lock(state_dir)
    second.close()
