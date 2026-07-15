"""Lock advisory per connection/job/agent, destinati al processo host Linux."""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

from .naming import connection_slug
from .validation import validate_uuid

try:
    import fcntl
except ImportError:  # Native Windows compatibility for legacy test/provisioning reads.
    fcntl = None
    import msvcrt


class LockUnavailableError(RuntimeError):
    """Un lock non bloccante e' gia' posseduto da un altro processo."""


class _FileLockManager:
    def __init__(self, root: Path, filename_for: Callable[[str], str]) -> None:
        self.root = Path(root)
        if self.root.exists() and (self.root.is_symlink() or not self.root.is_dir()):
            raise ValueError(f"Directory lock non valida: {self.root}.")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o750)
        self._filename_for = filename_for

    def path_for(self, identifier: str) -> Path:
        return self.root / self._filename_for(identifier)

    @contextmanager
    def acquire(self, identifier: str, *, blocking: bool = True) -> Iterator[None]:
        path = self.path_for(identifier)
        flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags, 0o640)
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, 0o640)
            if fcntl is not None:
                operation = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
                try:
                    fcntl.flock(descriptor, operation)
                except BlockingIOError as exc:
                    raise LockUnavailableError(
                        f"Lock gia' acquisito: {path.name}."
                    ) from exc
            else:
                os.lseek(descriptor, 0, os.SEEK_SET)
                os.write(descriptor, b"\0")
                mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
                try:
                    msvcrt.locking(descriptor, mode, 1)
                except OSError as exc:
                    raise LockUnavailableError(
                        f"Lock gia' acquisito: {path.name}."
                    ) from exc
            yield
        finally:
            try:
                if fcntl is not None:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                else:
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            finally:
                os.close(descriptor)


class ConnectionLockManager:
    def __init__(self, root: Path) -> None:
        self._manager = _FileLockManager(root, self._filename_for)
        self.root = self._manager.root

    @staticmethod
    def _filename_for(connection_id: str) -> str:
        return f"{connection_slug(connection_id)}.lock"

    def path_for(self, connection_id: str) -> Path:
        return self._manager.path_for(connection_id)

    @contextmanager
    def acquire(self, connection_id: str) -> Iterator[None]:
        with self._manager.acquire(connection_id):
            yield


class JobLockManager:
    def __init__(self, root: Path) -> None:
        self._manager = _FileLockManager(root, self._filename_for)
        self.root = self._manager.root

    @staticmethod
    def _filename_for(job_id: str) -> str:
        return f"{validate_uuid(job_id, 'job_id')}.lock"

    def path_for(self, job_id: str) -> Path:
        return self._manager.path_for(job_id)

    @contextmanager
    def acquire(self, job_id: str) -> Iterator[None]:
        with self._manager.acquire(job_id):
            yield


class SingletonLock:
    """Lock non bloccante a nome fisso per impedire due filesystem agent."""

    def __init__(self, root: Path, name: str = "filesystem-agent") -> None:
        if not name or any(char not in "abcdefghijklmnopqrstuvwxyz-" for char in name):
            raise ValueError("Nome singleton lock non valido.")
        self._manager = _FileLockManager(root, lambda _identifier: f".{name}.lock")

    @contextmanager
    def acquire(self) -> Iterator[None]:
        with self._manager.acquire("singleton", blocking=False):
            yield
