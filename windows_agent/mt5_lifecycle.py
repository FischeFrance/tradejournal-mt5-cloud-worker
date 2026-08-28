"""Per-connection lifecycle serialization shared by jobs and maintenance."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator

from .security import canonical_uuid


class Mt5LifecycleCoordinator:
    """Serialize filesystem/process ownership for one MT5 connection.

    The Agent intentionally keeps different accounts independent.  A global
    fleet lock would unnecessarily stop live event ingestion for every account
    while only one terminal is being rotated, so locks are allocated lazily per
    canonical connection id.
    """

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[str, threading.RLock] = {}

    def _lock_for(self, connection_id: str) -> threading.RLock:
        connection_id = canonical_uuid(connection_id)
        with self._guard:
            lock = self._locks.get(connection_id)
            if lock is None:
                lock = threading.RLock()
                self._locks[connection_id] = lock
            return lock

    @contextmanager
    def connection(self, connection_id: str) -> Iterator[None]:
        lock = self._lock_for(connection_id)
        with lock:
            yield
