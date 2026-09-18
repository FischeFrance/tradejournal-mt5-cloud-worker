"""Continuously consume local MT5 bridge snapshots without recurring API polls."""

from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

from worker.event_outbox import EventOutbox

from .provisioning.secret_store import WindowsSecretStore
from .security import canonical_uuid
from .state_store import atomic_json, read_json
from .worker.dedup import PersistentDedup
from .worker.live_sync import LiveSync
from .worker.mql5_file_adapter import Mql5FileMt5Adapter
from .worker.trading_ingestion_sink import TradingIngestionSink

logger = logging.getLogger(__name__)

_locks_guard = threading.Lock()
_connection_locks: dict[str, threading.RLock] = {}


@contextmanager
def connection_sync_lock(connection_id: str) -> Iterator[None]:
    """Serialize local snapshot/outbox work for one managed connection."""

    key = canonical_uuid(connection_id)
    with _locks_guard:
        lock = _connection_locks.setdefault(key, threading.RLock())
    with lock:
        yield


class _SnapshotState:
    def __init__(self, path: Path) -> None:
        self.path = path

    def get(self) -> dict:
        return read_json(
            self.path,
            {"positions": {}, "orders": {}, "deals": {}},
        )

    def save(self, value: dict) -> None:
        atomic_json(self.path, value)


class Mt5EventSupervisor:
    """Sample every local account while sending only actual trade events.

    The two-second loop reads files on the VPS. It performs no Supabase request when a snapshot
    contains no new event; MAE/MFE samples remain in ``position-excursions.json`` until close.
    """

    def __init__(
        self,
        instances_root: Path,
        secrets_root: Path,
        ingestion_url: str,
        *,
        poll_seconds: float = 2.0,
        processor: Callable[[str], int] | None = None,
    ) -> None:
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        self.instances_root = Path(instances_root)
        self.secrets = WindowsSecretStore(secrets_root)
        self.ingestion_url = str(ingestion_url)
        self.poll_seconds = float(poll_seconds)
        self._processor = processor or self._process_connection
        self._last_error: dict[str, tuple[type[BaseException], float]] = {}

    def _connection_ids(self) -> tuple[str, ...]:
        if not self.instances_root.exists():
            return ()
        discovered: list[str] = []
        for path in self.instances_root.iterdir():
            if not path.is_dir() or path.is_symlink():
                continue
            try:
                connection_id = canonical_uuid(path.name)
            except ValueError:
                continue
            # Secrets prove ownership, not readiness.  The provision/history handler publishes
            # `connected` only after the history bundle, the no-restart new_only handoff and the
            # first durable live poll have all succeeded.  This local gate also survives an Agent
            # restart, so a half-provisioned account can never leak historical rows through the
            # live ingestion route.
            progress_path = path / "state" / "job_progress.json"
            if progress_path.is_symlink() or not progress_path.is_file():
                continue
            try:
                progress = read_json(progress_path)
            except (OSError, ValueError):
                continue
            if (
                progress.get("connection_id") != connection_id
                or progress.get("status") != "connected"
            ):
                continue
            secret_root = self.secrets.root / connection_id
            required = ("mt5_login", "mt5_server", "bridge_token")
            if all((secret_root / f"{name}.dpapi").is_file() for name in required):
                discovered.append(connection_id)
        return tuple(sorted(discovered))

    def _process_connection(self, connection_id: str) -> int:
        root = self.instances_root / connection_id
        login = int(self.secrets.read(connection_id, "mt5_login"))
        server = self.secrets.read(connection_id, "mt5_server")
        bridge_token = self.secrets.read(connection_id, "bridge_token")
        adapter = Mql5FileMt5Adapter(
            root / "terminal" / "MQL5" / "Files" / "TradeJournal",
            connection_id,
            login,
            server,
            root / "state",
        )
        sink = TradingIngestionSink(root, self.ingestion_url, bridge_token)
        dedup = PersistentDedup(root / "state" / "live-dedup.sqlite")
        try:
            live = LiveSync(
                adapter,
                _SnapshotState(root / "state" / "live_snapshot.json"),
                dedup,
                sink,
                poll_seconds=self.poll_seconds,
                outbox=EventOutbox(str(root / "state" / "live-outbox.json")),
                excursion_store=_SnapshotState(
                    root / "state" / "position-excursions.json"
                ),
            )
            return live.poll_once()
        finally:
            dedup.close()

    def poll_once(self) -> int:
        delivered = 0
        for connection_id in self._connection_ids():
            try:
                with connection_sync_lock(connection_id):
                    delivered += self._processor(connection_id)
            except Exception as exc:
                # A stale/partially written bridge bundle is retried on the next local cycle.
                # Throttle identical warnings so a broker outage cannot flood the service log.
                now = time.monotonic()
                previous = self._last_error.get(connection_id)
                if previous is None or previous[0] is not type(exc) or now - previous[1] >= 60:
                    logger.warning(
                        "local MT5 event processing deferred (connection=%s, error=%s)",
                        connection_id,
                        type(exc).__name__,
                    )
                    self._last_error[connection_id] = (type(exc), now)
            else:
                self._last_error.pop(connection_id, None)
        return delivered

    def run(self, stop_event: threading.Event) -> None:
        if not self.ingestion_url:
            raise RuntimeError("MT5 event ingestion is not configured")
        while not stop_event.is_set():
            started = time.monotonic()
            self.poll_once()
            remaining = max(0.0, self.poll_seconds - (time.monotonic() - started))
            stop_event.wait(remaining)
