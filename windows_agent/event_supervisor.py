"""Local MT5 filesystem event supervision; no recurring Supabase/Edge calls."""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from worker.event_outbox import EventOutbox

from .mt5_lifecycle import Mt5LifecycleCoordinator
from .provisioning.secret_store import WindowsSecretStore
from .security import canonical_uuid
from .state_store import atomic_json, read_json
from .worker.dedup import PersistentDedup
from .worker.live_sync import LiveSync
from .worker.mql5_file_adapter import Mql5FileMt5Adapter
from .worker.trading_ingestion_sink import TradingIngestionSink

logger = logging.getLogger(__name__)


class _SnapshotState:
    def __init__(self, path: Path) -> None:
        self.path = path

    def get(self) -> dict:
        return read_json(self.path, {"positions": {}, "orders": {}, "deals": {}})

    def save(self, value: dict) -> None:
        atomic_json(self.path, value)


class _WakeHandler(FileSystemEventHandler):
    def __init__(self, owner: "Mt5EventSupervisor") -> None:
        self.owner = owner

    def on_created(self, event: FileSystemEvent) -> None:
        self.owner.notify(Path(os.fsdecode(event.src_path)))

    def on_moved(self, event: FileSystemEvent) -> None:
        self.owner.notify(Path(os.fsdecode(event.dest_path)))

    def on_modified(self, event: FileSystemEvent) -> None:
        self.owner.notify(Path(os.fsdecode(event.src_path)))


class Mt5EventSupervisor:
    def __init__(
        self,
        instances_root: Path,
        secrets_root: Path,
        ingestion_url: str,
        lifecycle_coordinator: Mt5LifecycleCoordinator | None = None,
    ) -> None:
        self.instances_root = Path(instances_root)
        self.secrets = WindowsSecretStore(secrets_root)
        self.ingestion_url = ingestion_url
        self._pending: set[str] = set()
        self._condition = threading.Condition()
        self._retry_failures: dict[str, int] = {}
        self._retry_after: dict[str, float] = {}
        self.lifecycle_coordinator = (
            lifecycle_coordinator or Mt5LifecycleCoordinator()
        )

    def notify(self, path: Path) -> None:
        try:
            relative = path.resolve().relative_to(self.instances_root.resolve())
            connection_id = canonical_uuid(relative.parts[0])
        except (ValueError, IndexError):
            return
        is_trade_event = path.parent.name == "events" and path.name.startswith("event-")
        is_heartbeat = path.name == "heartbeat.json"
        if not (is_trade_event or is_heartbeat):
            return
        with self._condition:
            self._pending.add(connection_id)
            self._condition.notify()

    def _components(self, connection_id: str) -> tuple[Path, Mql5FileMt5Adapter, TradingIngestionSink]:
        root = self.instances_root / connection_id
        login = int(self.secrets.read(connection_id, "mt5_login"))
        server = self.secrets.read(connection_id, "mt5_server")
        bridge_token = self.secrets.read(connection_id, "bridge_token")
        files_dir = root / "terminal" / "MQL5" / "Files" / "TradeJournal"
        adapter = Mql5FileMt5Adapter(files_dir, connection_id, login, server, root / "state")
        sink = TradingIngestionSink(root, self.ingestion_url, bridge_token)
        return root, adapter, sink

    def _process(self, connection_id: str) -> None:
        with self.lifecycle_coordinator.connection(connection_id):
            self._process_locked(connection_id)

    def _process_locked(self, connection_id: str) -> None:
        root, adapter, sink = self._components(connection_id)
        files_dir = adapter.files_dir
        event_files = sorted((files_dir / "events").glob("event-*.json"))
        if event_files:
            dedup = PersistentDedup(root / "state" / "live-dedup.sqlite")
            try:
                sync = LiveSync(
                    adapter,
                    _SnapshotState(root / "state" / "live-snapshot.json"),
                    dedup,
                    sink,
                    outbox=EventOutbox(
                        str(root / "state" / "event-supervisor-outbox.json")
                    ),
                )
                sync.poll_once()
            finally:
                dedup.close()
            # Each generated file is only a local wake marker. At this point every derived event
            # is durable in the outbox (or already accepted remotely), so markers can be removed.
            for event_file in event_files:
                try:
                    event_file.unlink()
                except FileNotFoundError:
                    pass
        flush_result = None
        if time.monotonic() >= self._retry_after.get(connection_id, 0):
            flush_result = sink.flush_transitions()

        state = adapter.connection_state()
        state_path = root / "state" / "connection-state.json"
        previous = read_json(state_path, {})
        pending_state = previous.get("pending")
        if isinstance(pending_state, dict) and flush_result is not None and flush_result.pending == 0:
            atomic_json(state_path, pending_state)
            previous = pending_state
            pending_state = None
        if not isinstance(pending_state, dict) and previous.get("connected") != state["connected"]:
            atomic_json(state_path, {**previous, "pending": state})
            snapshot = adapter.account_snapshot() if state["connected"] else None
            result = sink.send_connection_transition(
                connection_id,
                state["sequence"],
                state["connected"],
                snapshot,
            )
            if result.pending == 0:
                atomic_json(state_path, state)

        pending_count = sink.pending_transition_count()
        if pending_count:
            failures = self._retry_failures.get(connection_id, 0) + 1
            self._retry_failures[connection_id] = failures
            self._retry_after[connection_id] = time.monotonic() + min(2 ** failures, 300)
        else:
            self._retry_failures.pop(connection_id, None)
            self._retry_after.pop(connection_id, None)

    def _discover(self) -> None:
        if not self.instances_root.exists():
            return
        for path in self.instances_root.iterdir():
            if path.is_dir():
                try:
                    connection_id = canonical_uuid(path.name)
                except ValueError:
                    continue
                secret_root = self.secrets.root / connection_id
                required = ("mt5_login", "mt5_server", "bridge_token")
                if all((secret_root / f"{name}.dpapi").is_file() for name in required):
                    self._pending.add(connection_id)

    def run(
        self,
        stop_event: threading.Event,
        ready_event: threading.Event | None = None,
    ) -> None:
        if not self.ingestion_url:
            raise RuntimeError("MT5 event ingestion is not configured")
        self.instances_root.mkdir(parents=True, exist_ok=True)
        observer = Observer()
        observer.schedule(_WakeHandler(self), str(self.instances_root), recursive=True)
        observer.start()
        try:
            if not observer.is_alive():
                raise RuntimeError("MT5 event observer failed to start")
            with self._condition:
                self._discover()
                self._condition.notify()
            if ready_event is not None:
                ready_event.set()
            while not stop_event.is_set():
                if not observer.is_alive():
                    raise RuntimeError("MT5 event observer stopped unexpectedly")
                with self._condition:
                    if not self._pending:
                        self._condition.wait(timeout=1.0)
                    pending, self._pending = self._pending, set()
                for connection_id in sorted(pending):
                    if stop_event.is_set():
                        break
                    try:
                        self._process(connection_id)
                    except Exception as exc:
                        logger.warning(
                            "local MT5 event processing deferred (connection=%s, error=%s)",
                            connection_id,
                            type(exc).__name__,
                        )
        finally:
            observer.stop()
            observer.join(timeout=5)
