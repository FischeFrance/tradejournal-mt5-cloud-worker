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


_SOURCE_RECOVERY_OBSERVED_SEQUENCE = "_source_recovery_observed_sequence"
_SOURCE_RECOVERY_REASSERTS_THROUGH_SEQUENCE = (
    "_source_recovery_reasserts_through_sequence"
)
_TRANSITION_ENQUEUED = "_transition_enqueued"


def _source_recovery_v2_acknowledged(state: dict) -> bool:
    """Whether an optional v2 recovery fence is coherent.

    ``Mql5FileMt5Adapter`` requires these fields for every real bridge after
    the v2 rollout.  Keeping an all-fields-absent branch preserves the narrow
    fake/legacy adapter seam used by isolated unit tests; a partially supplied
    or mismatched fence is never treated as a clean source.
    """

    names = (
        "source_recovery_protocol_version",
        "source_recovery_request_generation",
        "source_recovery_ack_generation",
    )
    values = tuple(state.get(name) for name in names)
    if values == (None, None, None):
        return True
    protocol, request_generation, ack_generation = values
    return (
        isinstance(protocol, int)
        and not isinstance(protocol, bool)
        and protocol == 2
        and isinstance(request_generation, int)
        and not isinstance(request_generation, bool)
        and request_generation >= 0
        and isinstance(ack_generation, int)
        and not isinstance(ack_generation, bool)
        and ack_generation >= 0
        and request_generation == ack_generation
    )


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
        # A persisted connection-state.json records what the previous service instance
        # already delivered.  It must not suppress the first *verified* state report
        # after this supervisor starts: a terminal maintenance job can fail after the
        # bridge has recovered and leave the remote row stale until a real transition.
        # This set is deliberately process-local, so it causes one reassertion per
        # supervisor start rather than a second heartbeat loop.
        self._startup_reassertions: set[str] = set()
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
        # A signed history archive may have been accepted while its frozen
        # live baseline is still awaiting the in-process new_only handoff.
        # Do not let the generic filesystem watcher consume/ack the same
        # source prefix before that durable activation commits.
        if (root / "state" / "history-handoff-pending.json").exists():
            return
        state = adapter.connection_state()
        # The adapter already derives source_recovery_required from the v2
        # control files.  Keep the supervisor's transition path independently
        # fail-closed as well: a hand-built/stale state with a mismatched
        # request/ack pair can never reassert connected remotely.
        if not _source_recovery_v2_acknowledged(state):
            state = {
                **state,
                "connected": False,
                "source_recovery_required": True,
            }
        state_path = root / "state" / "connection-state.json"
        previous = read_json(state_path, {})
        event_files = sorted((files_dir / "events").glob("event-*.json"))
        # Missing/nonboolean is not a compatible healthy state during the
        # continuity rollout. Leave the local wake files untouched until the
        # EA explicitly certifies that source recovery is clear. Do not even
        # emit a remote connection transition from this ambiguous state: the
        # next clean heartbeat is the only authority that may reactivate it.
        if state.get("source_recovery_required") is not False:
            observed_sequence = state.get("sequence")
            previous_sequence = previous.get(_SOURCE_RECOVERY_OBSERVED_SEQUENCE)
            if (
                not isinstance(observed_sequence, int)
                or isinstance(observed_sequence, bool)
                or observed_sequence < 0
            ):
                observed_sequence = (
                    previous_sequence
                    if isinstance(previous_sequence, int)
                    and not isinstance(previous_sequence, bool)
                    and previous_sequence >= 0
                    else 0
                )
            if (
                not isinstance(previous_sequence, int)
                or isinstance(previous_sequence, bool)
                or observed_sequence > previous_sequence
            ):
                atomic_json(
                    state_path,
                    {
                        **previous,
                        _SOURCE_RECOVERY_OBSERVED_SEQUENCE: observed_sequence,
                    },
                )
            return
        observed_recovery_sequence = previous.get(
            _SOURCE_RECOVERY_OBSERVED_SEQUENCE
        )
        if (
            isinstance(observed_recovery_sequence, int)
            and not isinstance(observed_recovery_sequence, bool)
        ):
            clean_sequence = state.get("sequence")
            # Clearing the capability bit in an old/regressed heartbeat is not
            # proof that the EA advanced beyond the recovery observation.  Keep
            # both wake files and the durable marker untouched until a strictly
            # newer clean heartbeat arrives.
            if (
                not isinstance(clean_sequence, int)
                or isinstance(clean_sequence, bool)
                or clean_sequence <= observed_recovery_sequence
            ):
                return
        if event_files:
            dedup = PersistentDedup(root / "state" / "live-dedup.sqlite")
            try:
                sync = LiveSync(
                    adapter,
                    # Provisioning, history-sync activation and the filesystem supervisor must
                    # advance one canonical baseline. Using a second hyphenated filename made a
                    # later history job rediscover an already-open position and emit another
                    # trade_opened without the original DEAL_ORDER ticket.
                    _SnapshotState(root / "state" / "live_snapshot.json"),
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
        pending_state = previous.get("pending")
        if (
            isinstance(pending_state, dict)
            and pending_state.get(_TRANSITION_ENQUEUED) is not True
        ):
            pending_sequence = pending_state.get("sequence")
            pending_connected = pending_state.get("connected")
            if (
                not isinstance(pending_sequence, int)
                or isinstance(pending_sequence, bool)
                or pending_sequence < 0
                or not isinstance(pending_connected, bool)
            ):
                # A corrupt local intent cannot be acknowledged or replaced by
                # the current heartbeat without losing its causal boundary.
                return
            pending_recovery_reassertion = isinstance(
                pending_state.get(_SOURCE_RECOVERY_REASSERTS_THROUGH_SEQUENCE),
                int,
            ) and not isinstance(
                pending_state.get(_SOURCE_RECOVERY_REASSERTS_THROUGH_SEQUENCE),
                bool,
            )
            pending_snapshot = (
                adapter.account_snapshot()
                if pending_connected and not pending_recovery_reassertion
                else None
            )
            # Enqueue and checkpoint are deliberately separate.  A crash after
            # the durable enqueue simply re-enqueues the deterministic event id;
            # a crash before it leaves this flag false and cannot be mistaken
            # for a remote acknowledgement on the next empty flush.
            sink.enqueue_connection_transition(
                connection_id,
                pending_sequence,
                pending_connected,
                pending_snapshot,
            )
            pending_state = {**pending_state, _TRANSITION_ENQUEUED: True}
            previous = {**previous, "pending": pending_state}
            atomic_json(state_path, previous)

        flush_result = None
        if time.monotonic() >= self._retry_after.get(connection_id, 0):
            flush_result = sink.flush_transitions()

        pending_state = previous.get("pending")
        if (
            isinstance(pending_state, dict)
            and pending_state.get(_TRANSITION_ENQUEUED) is True
            and flush_result is not None
            and sink.transition_delivery_confirmed()
        ):
            committed_state = {
                key: value
                for key, value in pending_state.items()
                if key
                not in (
                    _SOURCE_RECOVERY_REASSERTS_THROUGH_SEQUENCE,
                    _TRANSITION_ENQUEUED,
                )
            }
            observed_sequence = previous.get(_SOURCE_RECOVERY_OBSERVED_SEQUENCE)
            reasserted_through = pending_state.get(
                _SOURCE_RECOVERY_REASSERTS_THROUGH_SEQUENCE
            )
            recovery_transition_was_enqueued = (
                pending_state.get(_TRANSITION_ENQUEUED) is True
            )
            recovery_reasserted = (
                recovery_transition_was_enqueued
                and isinstance(observed_sequence, int)
                and not isinstance(observed_sequence, bool)
                and isinstance(reasserted_through, int)
                and not isinstance(reasserted_through, bool)
                and reasserted_through >= observed_sequence
            )
            if (
                isinstance(observed_sequence, int)
                and not isinstance(observed_sequence, bool)
                and not recovery_reasserted
            ):
                committed_state[_SOURCE_RECOVERY_OBSERVED_SEQUENCE] = (
                    observed_sequence
                )
            atomic_json(state_path, committed_state)
            previous = committed_state
            pending_state = None
            # The persisted pending state was acknowledged during this service
            # lifetime, so it already constitutes the required startup reassertion.
            self._startup_reassertions.discard(connection_id)
        observed_sequence = previous.get(_SOURCE_RECOVERY_OBSERVED_SEQUENCE)
        source_recovery_reassertion = (
            state.get("connected") is True
            and isinstance(observed_sequence, int)
            and not isinstance(observed_sequence, bool)
        )
        should_reassert = (
            connection_id in self._startup_reassertions
            or source_recovery_reassertion
        )
        if not isinstance(pending_state, dict) and (
            previous.get("connected") != state["connected"] or should_reassert
        ):
            pending_transition = dict(state)
            pending_transition[_TRANSITION_ENQUEUED] = False
            if source_recovery_reassertion:
                pending_transition[_SOURCE_RECOVERY_REASSERTS_THROUGH_SEQUENCE] = (
                    observed_sequence
                )
            persisted_transition = {**previous, "pending": pending_transition}
            atomic_json(state_path, persisted_transition)
            # The recovery acknowledgement must take the dedicated,
            # token-authenticated connection-transition path.  Attaching an
            # account snapshot would route it through the account-heartbeat
            # RPC instead, leaving the control-plane recovery marker (and its
            # parked history job) uncleared.  Ordinary connection transitions
            # still carry their snapshot exactly as before.
            snapshot = (
                adapter.account_snapshot()
                if state["connected"] and not source_recovery_reassertion
                else None
            )
            sink.enqueue_connection_transition(
                connection_id,
                state["sequence"],
                state["connected"],
                snapshot,
            )
            pending_transition[_TRANSITION_ENQUEUED] = True
            atomic_json(
                state_path,
                {**previous, "pending": pending_transition},
            )
            sink.flush_transitions()
            if sink.transition_delivery_confirmed():
                committed_state = dict(state)
                if (
                    isinstance(observed_sequence, int)
                    and not isinstance(observed_sequence, bool)
                    and not source_recovery_reassertion
                ):
                    committed_state[_SOURCE_RECOVERY_OBSERVED_SEQUENCE] = (
                        observed_sequence
                    )
                atomic_json(state_path, committed_state)
                self._startup_reassertions.discard(connection_id)

        pending_count = sink.pending_transition_count()
        if pending_count:
            failures = self._retry_failures.get(connection_id, 0) + 1
            self._retry_failures[connection_id] = failures
            self._retry_after[connection_id] = time.monotonic() + min(2 ** failures, 300)
        else:
            self._retry_failures.pop(connection_id, None)
            self._retry_after.pop(connection_id, None)

    def _discover(self, *, reassert: bool = False) -> None:
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
                    if reassert:
                        self._startup_reassertions.add(connection_id)

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
                self._discover(reassert=True)
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
