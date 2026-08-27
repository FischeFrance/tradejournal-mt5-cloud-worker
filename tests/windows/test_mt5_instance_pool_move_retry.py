from __future__ import annotations

from pathlib import Path
from unittest.mock import call, patch

from windows_agent.provisioning.mt5_instance_pool import Mt5InstancePool


def _windows_error(code: int) -> PermissionError:
    error = PermissionError("locked")
    error.winerror = code
    return error


def test_ready_slot_move_retries_transient_windows_lock(tmp_path: Path) -> None:
    source = tmp_path / "ready-slot"
    destination = tmp_path / "claimed-slot"
    source.mkdir()
    failures = [_windows_error(5), _windows_error(32), None]

    with (
        patch(
            "windows_agent.provisioning.mt5_instance_pool.durable_replace",
            side_effect=failures,
        ) as replace,
        patch(
            "windows_agent.provisioning.mt5_instance_pool.time.sleep"
        ) as sleep,
    ):
        moved = Mt5InstancePool._move_ready_slot(source, destination)

    assert moved is True
    assert replace.call_count == 3
    assert sleep.call_args_list == [call(0.1), call(0.2)]


def test_ready_slot_move_skips_persistently_locked_slot(tmp_path: Path) -> None:
    source = tmp_path / "ready-slot"
    destination = tmp_path / "claimed-slot"
    source.mkdir()

    with (
        patch(
            "windows_agent.provisioning.mt5_instance_pool.durable_replace",
            side_effect=_windows_error(5),
        ) as replace,
        patch(
            "windows_agent.provisioning.mt5_instance_pool.time.sleep"
        ) as sleep,
    ):
        moved = Mt5InstancePool._move_ready_slot(source, destination)

    assert moved is False
    assert replace.call_count == 5
    assert sleep.call_args_list == [
        call(0.1),
        call(0.2),
        call(0.4),
        call(0.8),
    ]
