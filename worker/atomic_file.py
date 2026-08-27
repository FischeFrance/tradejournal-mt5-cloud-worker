"""Cross-platform durable publication primitives.

The caller must fsync the temporary file before invoking ``durable_replace``. On POSIX the
replacement is followed by a directory fsync. On Windows ``MoveFileExW`` with
``MOVEFILE_WRITE_THROUGH`` provides the corresponding write-through replacement without trying
to open a directory through ``os.open`` (which is not supported by Windows Python).
"""

from __future__ import annotations

import os
import time


_MOVEFILE_REPLACE_EXISTING = 0x00000001
_MOVEFILE_WRITE_THROUGH = 0x00000008
_WINDOWS_TRANSIENT_REPLACE_ERRORS = frozenset((5, 32, 33))
_WINDOWS_REPLACE_ATTEMPTS = 7
_WINDOWS_REPLACE_BASE_DELAY_SECONDS = 0.05


def durable_replace(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
    source_path = os.path.abspath(os.fspath(source))
    destination_path = os.path.abspath(os.fspath(destination))
    if os.name == "nt":
        _windows_durable_replace_with_retry(source_path, destination_path)
        return

    os.replace(source_path, destination_path)
    fsync_directory(os.path.dirname(destination_path) or ".")


def _windows_durable_replace_with_retry(source: str, destination: str) -> None:
    """Retry only transient Windows file-lock failures for a bounded period.

    Defender, indexing, and backup filters may briefly reopen a freshly
    published JSON/SQLite sidecar without delete sharing.  A following atomic
    replacement then reports access denied/sharing violation even though the
    service owns the file and directory.  Preserve fail-closed behavior for
    every other error and for a lock that outlives the bounded retry window.
    Directory moves retain their existing single-attempt semantics; pool code
    owns its separate directory-level retry policy.
    """

    if os.path.isdir(source):
        _windows_replace_write_through(source, destination)
        return
    for attempt in range(_WINDOWS_REPLACE_ATTEMPTS):
        try:
            _windows_replace_write_through(source, destination)
            return
        except OSError as exc:
            winerror = getattr(exc, "winerror", None)
            if (
                winerror not in _WINDOWS_TRANSIENT_REPLACE_ERRORS
                or attempt + 1 >= _WINDOWS_REPLACE_ATTEMPTS
                or not os.path.exists(source)
            ):
                raise
            time.sleep(_WINDOWS_REPLACE_BASE_DELAY_SECONDS * (2**attempt))


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
    # MoveFileExW rejects a non-empty directory with MOVEFILE_WRITE_THROUGH on
    # supported Windows Server builds (ERROR_ACCESS_DENIED), even on one volume
    # and with SYSTEM full control. os.rename uses the atomic directory rename
    # path and preserves the required fail-if-destination-exists semantics.
    if os.path.isdir(source):
        os.rename(source, destination)
        return
    import ctypes
    from ctypes import wintypes

    move_file_ex = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
    move_file_ex.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD)
    move_file_ex.restype = wintypes.BOOL
    if not move_file_ex(
        source,
        destination,
        _windows_move_flags(source),
    ):
        raise ctypes.WinError(ctypes.get_last_error())


def _windows_move_flags(source: str) -> int:
    """Return valid ``MoveFileExW`` flags for files and directories.

    Windows rejects ``MOVEFILE_REPLACE_EXISTING`` when either path names a
    directory. Pool slots are directories whose destination must not already
    exist, so keep the durable write-through flag but only request replacement
    for regular files.
    """

    flags = _MOVEFILE_WRITE_THROUGH
    if not os.path.isdir(source):
        flags |= _MOVEFILE_REPLACE_EXISTING
    return flags
