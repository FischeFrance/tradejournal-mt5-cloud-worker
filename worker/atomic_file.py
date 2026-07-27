"""Cross-platform durable publication primitives.

The caller must fsync the temporary file before invoking ``durable_replace``. On POSIX the
replacement is followed by a directory fsync. On Windows ``MoveFileExW`` with
``MOVEFILE_WRITE_THROUGH`` provides the corresponding write-through replacement without trying
to open a directory through ``os.open`` (which is not supported by Windows Python).
"""

from __future__ import annotations

import os
from pathlib import Path


def durable_replace(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
    source_path = os.path.abspath(os.fspath(source))
    destination_path = os.path.abspath(os.fspath(destination))
    if os.name == "nt":
        _windows_replace_write_through(source_path, destination_path)
        return

    os.replace(source_path, destination_path)
    fsync_directory(os.path.dirname(destination_path) or ".")


def fsync_directory(directory: str | os.PathLike[str]) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(os.fspath(directory), flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _windows_replace_write_through(source: str, destination: str) -> None:
    import ctypes
    from ctypes import wintypes

    move_file_ex = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
    move_file_ex.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD)
    move_file_ex.restype = wintypes.BOOL
    movefile_replace_existing = 0x00000001
    movefile_write_through = 0x00000008
    if not move_file_ex(
        source,
        destination,
        movefile_replace_existing | movefile_write_through,
    ):
        raise ctypes.WinError(ctypes.get_last_error())
