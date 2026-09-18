"""Cross-platform durable publication primitives.

The caller must fsync the temporary file before invoking ``durable_replace``. On POSIX the
replacement is followed by a directory fsync. On Windows ``MoveFileExW`` with
``MOVEFILE_WRITE_THROUGH`` provides the corresponding write-through replacement without trying
to open a directory through ``os.open`` (which is not supported by Windows Python).
"""

from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path


_MOVEFILE_REPLACE_EXISTING = 0x00000001
_MOVEFILE_WRITE_THROUGH = 0x00000008


def durable_copy_contents(
    source: str | os.PathLike[str],
    destination: str | os.PathLike[str],
) -> None:
    """Durably copy bytes into a new writable file without source metadata.

    Content-addressed release assets can be read-only and carry restrictive
    source ACLs.  The destination must inherit its own directory ACL instead of
    copying either property from the release file.
    """

    source_path = Path(source)
    destination_path = Path(destination)
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0)
    )
    descriptor: int | None = None
    created = False
    try:
        descriptor = os.open(os.fspath(destination_path), flags, 0o600)
        created = True
        with source_path.open("rb") as source_handle, os.fdopen(
            descriptor, "wb"
        ) as destination_handle:
            descriptor = None
            shutil.copyfileobj(
                source_handle,
                destination_handle,
                length=1024 * 1024,
            )
            destination_handle.flush()
            os.fsync(destination_handle.fileno())
        make_regular_file_writable(destination_path)
        fsync_directory(destination_path.parent)
    except Exception:
        if descriptor is not None:
            os.close(descriptor)
        if created:
            unlink_readonly_file(destination_path)
        raise


def make_regular_file_writable(path: str | os.PathLike[str]) -> None:
    """Clear Windows' read-only attribute without following reparse points."""

    file_path = Path(path)
    try:
        path_stat = os.lstat(file_path)
    except FileNotFoundError:
        raise ValueError("durable file missing") from None
    attributes = getattr(path_stat, "st_file_attributes", 0)
    if (
        not stat.S_ISREG(path_stat.st_mode)
        or stat.S_ISLNK(path_stat.st_mode)
        or bool(attributes & 0x400)
    ):
        raise ValueError("durable file invalid")
    os.chmod(file_path, path_stat.st_mode | stat.S_IWRITE)


def unlink_readonly_file(path: str | os.PathLike[str]) -> None:
    """Idempotently remove a regular file, including a Windows read-only file."""

    file_path = Path(path)
    try:
        os.lstat(file_path)
    except FileNotFoundError:
        return
    make_regular_file_writable(file_path)
    try:
        file_path.unlink()
    except FileNotFoundError:
        return
    fsync_directory(file_path.parent)


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
