from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from unittest.mock import call, patch

import pytest

from worker import atomic_file


def _windows_error(code: int) -> PermissionError:
    error = PermissionError("temporarily locked")
    error.winerror = code
    return error


@pytest.mark.parametrize("code", (5, 32, 33))
def test_windows_file_replace_retries_transient_lock(
    tmp_path: Path,
    code: int,
) -> None:
    source = tmp_path / "history.tmp"
    source.write_text("checkpoint", encoding="utf-8")
    failures = [_windows_error(code), _windows_error(code), None]

    with (
        patch.object(
            atomic_file,
            "_windows_replace_write_through",
            side_effect=failures,
        ) as replace,
        patch.object(atomic_file.time, "sleep") as sleep,
    ):
        atomic_file._windows_durable_replace_with_retry(
            str(source),
            str(tmp_path / "history.json"),
        )

    assert replace.call_count == 3
    assert sleep.call_args_list == [call(0.05), call(0.1)]


def test_windows_file_replace_fails_after_bounded_retry(tmp_path: Path) -> None:
    source = tmp_path / "history.tmp"
    source.write_text("checkpoint", encoding="utf-8")

    with (
        patch.object(
            atomic_file,
            "_windows_replace_write_through",
            side_effect=_windows_error(5),
        ) as replace,
        patch.object(atomic_file.time, "sleep") as sleep,
        pytest.raises(PermissionError),
    ):
        atomic_file._windows_durable_replace_with_retry(
            str(source),
            str(tmp_path / "history.json"),
        )

    assert replace.call_count == 7
    assert sleep.call_args_list == [
        call(0.05),
        call(0.1),
        call(0.2),
        call(0.4),
        call(0.8),
        call(1.6),
    ]


def test_windows_file_replace_does_not_retry_non_transient_error(
    tmp_path: Path,
) -> None:
    source = tmp_path / "history.tmp"
    source.write_text("checkpoint", encoding="utf-8")

    with (
        patch.object(
            atomic_file,
            "_windows_replace_write_through",
            side_effect=_windows_error(87),
        ) as replace,
        patch.object(atomic_file.time, "sleep") as sleep,
        pytest.raises(PermissionError),
    ):
        atomic_file._windows_durable_replace_with_retry(
            str(source),
            str(tmp_path / "history.json"),
        )

    replace.assert_called_once()
    sleep.assert_not_called()


def test_windows_directory_replace_keeps_single_attempt(tmp_path: Path) -> None:
    source = tmp_path / "ready-slot"
    source.mkdir()

    with (
        patch.object(
            atomic_file,
            "_windows_replace_write_through",
            side_effect=_windows_error(5),
        ) as replace,
        patch.object(atomic_file.time, "sleep") as sleep,
        pytest.raises(PermissionError),
    ):
        atomic_file._windows_durable_replace_with_retry(
            str(source),
            str(tmp_path / "claimed-slot"),
        )

    replace.assert_called_once()
    sleep.assert_not_called()


@pytest.mark.skipif(os.name != "nt", reason="requires a real Windows file lock")
def test_durable_replace_waits_for_real_windows_delete_lock(
    tmp_path: Path,
) -> None:
    import ctypes
    from ctypes import wintypes

    destination = tmp_path / "history.json"
    destination.write_text("old", encoding="utf-8")
    source = tmp_path / "history.tmp"
    source.write_text("new", encoding="utf-8")

    create_file = ctypes.WinDLL("kernel32", use_last_error=True).CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    close_handle = ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    handle = create_file(
        str(destination),
        0x80000000,  # GENERIC_READ
        0x00000001 | 0x00000002,  # share read/write, deliberately not delete
        None,
        3,  # OPEN_EXISTING
        0,
        None,
    )
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())

    def release() -> None:
        time.sleep(0.25)
        close_handle(handle)

    releaser = threading.Thread(target=release)
    releaser.start()
    try:
        atomic_file.durable_replace(source, destination)
    finally:
        releaser.join(timeout=2)
        if releaser.is_alive():
            close_handle(handle)
            releaser.join(timeout=2)

    assert destination.read_text(encoding="utf-8") == "new"
    assert not source.exists()
