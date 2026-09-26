from __future__ import annotations

import threading
from types import SimpleNamespace
from uuid import uuid4

import pytest

import windows_agent.event_supervisor as event_supervisor
from windows_agent.event_supervisor import Mt5EventSupervisor
from windows_agent.state_store import atomic_json, read_json


class FakeAdapter:
    def __init__(self, files_dir):
        self.files_dir = files_dir

    def verify_identity(self):
        return {"login": "42", "server": "Demo"}

    def snapshot(self):
        return {
            "positions": {
                "9": {
                    "ticket": "9",
                    "symbol": "EURUSD",
                    "direction": "buy",
                    "volume": 0.1,
                    "open_price": 1.1,
                    "stop_loss": 1.0,
                    "take_profit": 1.2,
                    "open_time": "2026-08-14T00:00:00Z",
                }
            },
            "orders": {},
            "deals": {},
        }

    def connection_state(self):
        return {
            "connected": True,
            "sequence": 12,
            "source_recovery_required": False,
        }

    def account_snapshot(self):
        return {"balance": 1000, "equity": 1001, "currency": "EUR", "leverage": 100}


class FakeSink:
    def __init__(self):
        self.events = []
        self.transitions = []
        self.flushes = 0
        self.pending = 0
        self.confirmed = True
        self.fail_enqueue = False

    def __call__(self, payload):
        self.events.append(payload)

    def flush_transitions(self):
        self.flushes += 1
        return SimpleNamespace(pending=self.pending)

    def pending_transition_count(self):
        return self.pending

    def transition_delivery_confirmed(self):
        return self.confirmed and self.pending == 0

    def enqueue_many(self, payloads):
        self.events.extend(payloads)
        return SimpleNamespace(pending=self.pending)

    def enqueue_connection_transition(self, *args):
        if self.fail_enqueue:
            raise RuntimeError("simulated enqueue failure")
        self.transitions.append(args)
        return 1

    def send_connection_transition(self, *args):
        self.enqueue_connection_transition(*args)
        return self.flush_transitions()


def test_event_marker_wakes_one_snapshot_diff_and_status_is_sent_only_on_change(tmp_path):
    connection_id = str(uuid4())
    root = tmp_path / "instances" / connection_id
    files_dir = root / "terminal" / "MQL5" / "Files" / "TradeJournal"
    events_dir = files_dir / "events"
    events_dir.mkdir(parents=True)
    marker = events_dir / "event-12.json"
    marker.write_text("{}", encoding="utf-8")
    sink = FakeSink()
    adapter = FakeAdapter(files_dir)
    supervisor = Mt5EventSupervisor(tmp_path / "instances", tmp_path / "secrets", "https://example.invalid")
    supervisor._components = lambda _cid: (root, adapter, sink)  # type: ignore[method-assign]

    supervisor._process(connection_id)
    assert len(sink.events) == 1
    assert sink.events[0]["event_type"] == "trade_opened"
    assert sink.events[0]["balance"] == 1000
    assert sink.events[0]["equity"] == 1001
    assert sink.events[0]["currency"] == "EUR"
    assert sink.events[0]["leverage"] == 100
    assert not marker.exists()
    assert len(sink.transitions) == 1

    supervisor._process(connection_id)
    assert len(sink.transitions) == 1
    # One initial drain per process pass plus the immediate drain after the
    # newly durable transition is enqueued.
    assert sink.flushes == 3


def test_event_supervisor_reuses_the_history_sync_live_baseline(tmp_path):
    connection_id = str(uuid4())
    root = tmp_path / "instances" / connection_id
    files_dir = root / "terminal" / "MQL5" / "Files" / "TradeJournal"
    events_dir = files_dir / "events"
    events_dir.mkdir(parents=True)
    marker = events_dir / "event-13.json"
    marker.write_text("{}", encoding="utf-8")
    sink = FakeSink()
    adapter = FakeAdapter(files_dir)
    atomic_json(root / "state" / "live_snapshot.json", adapter.snapshot())
    supervisor = Mt5EventSupervisor(
        tmp_path / "instances",
        tmp_path / "secrets",
        "https://example.invalid",
    )
    supervisor._components = lambda _cid: (root, adapter, sink)  # type: ignore[method-assign]

    supervisor._process(connection_id)

    assert sink.events == []
    assert not marker.exists()
    assert not (root / "state" / "live-snapshot.json").exists()


@pytest.mark.parametrize("recovery_flag", [True, None, "false"])
def test_source_recovery_capability_keeps_wake_marker_and_remote_state_untouched(
    tmp_path, recovery_flag
):
    connection_id = str(uuid4())
    root = tmp_path / "instances" / connection_id
    files_dir = root / "terminal" / "MQL5" / "Files" / "TradeJournal"
    events_dir = files_dir / "events"
    events_dir.mkdir(parents=True)
    marker = events_dir / "event-12.json"
    marker.write_text("{}", encoding="utf-8")
    sink = FakeSink()
    adapter = FakeAdapter(files_dir)
    adapter.connection_state = lambda: {
        "connected": False,
        "sequence": 12,
        "source_recovery_required": recovery_flag,
    }
    supervisor = Mt5EventSupervisor(
        tmp_path / "instances", tmp_path / "secrets", "https://example.invalid"
    )
    supervisor._components = lambda _cid: (root, adapter, sink)  # type: ignore[method-assign]

    supervisor._process(connection_id)

    assert marker.exists()
    assert sink.events == []
    assert sink.transitions == []
    assert sink.flushes == 0


def test_v2_request_ack_mismatch_cannot_reassert_connected(tmp_path):
    connection_id = str(uuid4())
    root = tmp_path / "instances" / connection_id
    files_dir = root / "terminal" / "MQL5" / "Files" / "TradeJournal"
    events_dir = files_dir / "events"
    events_dir.mkdir(parents=True)
    marker = events_dir / "event-12.json"
    marker.write_text("{}", encoding="utf-8")
    sink = FakeSink()
    adapter = FakeAdapter(files_dir)
    observed_state = {
        "connected": True,
        "sequence": 12,
        "source_recovery_required": False,
        "source_recovery_protocol_version": 2,
        "source_recovery_request_generation": 9,
        "source_recovery_ack_generation": 8,
    }
    adapter.connection_state = lambda: dict(observed_state)
    supervisor = Mt5EventSupervisor(
        tmp_path / "instances", tmp_path / "secrets", "https://example.invalid"
    )
    supervisor._components = lambda _cid: (root, adapter, sink)  # type: ignore[method-assign]

    supervisor._process(connection_id)

    assert marker.exists()
    assert sink.events == []
    assert sink.transitions == []
    assert read_json(root / "state" / "connection-state.json")[
        "_source_recovery_observed_sequence"
    ] == 12

    # A newer clean heartbeat with the exact ack is the earliest legal
    # reassertion; the pre-request clean state above is never sent remotely.
    observed_state.update(
        sequence=13,
        source_recovery_request_generation=9,
        source_recovery_ack_generation=9,
    )
    supervisor._process(connection_id)

    assert len(sink.transitions) == 1
    assert sink.transitions[0][1:4] == (13, True, None)


def test_legacy_recovery_capability_cannot_reassert_connected(tmp_path):
    connection_id = str(uuid4())
    root = tmp_path / "instances" / connection_id
    files_dir = root / "terminal" / "MQL5" / "Files" / "TradeJournal"
    events_dir = files_dir / "events"
    events_dir.mkdir(parents=True)
    marker = events_dir / "event-12.json"
    marker.write_text("{}", encoding="utf-8")
    sink = FakeSink()
    adapter = FakeAdapter(files_dir)
    # Real Mql5FileMt5Adapter returns this explicit v1 capability tuple for a
    # legacy heartbeat.  The supervisor must not treat it like its all-fields-
    # absent unit-test seam: a new Agent cannot prove the v2 ack fence there.
    adapter.connection_state = lambda: {
        "connected": True,
        "sequence": 12,
        "source_recovery_required": False,
        "source_recovery_protocol_version": 1,
        "source_recovery_request_generation": 0,
        "source_recovery_ack_generation": 0,
    }
    supervisor = Mt5EventSupervisor(
        tmp_path / "instances", tmp_path / "secrets", "https://example.invalid"
    )
    supervisor._components = lambda _cid: (root, adapter, sink)  # type: ignore[method-assign]

    supervisor._process(connection_id)

    assert marker.exists()
    assert sink.events == []
    assert sink.transitions == []
    assert read_json(root / "state" / "connection-state.json")[
        "_source_recovery_observed_sequence"
    ] == 12


def test_first_clean_heartbeat_after_source_recovery_reasserts_connected_once(
    tmp_path,
):
    connection_id = str(uuid4())
    root = tmp_path / "instances" / connection_id
    files_dir = root / "terminal" / "MQL5" / "Files" / "TradeJournal"
    files_dir.mkdir(parents=True)
    state_path = root / "state" / "connection-state.json"
    atomic_json(
        state_path,
        {
            "connected": True,
            "sequence": 10,
            "source_recovery_required": False,
        },
    )
    sink = FakeSink()
    adapter = FakeAdapter(files_dir)
    observed_state = {
        "connected": False,
        "sequence": 11,
        "source_recovery_required": True,
    }
    adapter.connection_state = lambda: dict(observed_state)
    supervisor = Mt5EventSupervisor(
        tmp_path / "instances", tmp_path / "secrets", "https://example.invalid"
    )
    supervisor._components = lambda _cid: (root, adapter, sink)  # type: ignore[method-assign]

    supervisor._process(connection_id)

    assert sink.transitions == []
    assert read_json(state_path)["_source_recovery_observed_sequence"] == 11

    # A service restart must not lose the required post-recovery proof. The
    # durable marker, rather than a process-local flag, forces the reassertion.
    restarted = Mt5EventSupervisor(
        tmp_path / "instances", tmp_path / "secrets", "https://example.invalid"
    )
    restarted._components = lambda _cid: (root, adapter, sink)  # type: ignore[method-assign]
    observed_state.update(
        connected=True,
        sequence=12,
        source_recovery_required=False,
    )

    restarted._process(connection_id)
    restarted._process(connection_id)

    assert len(sink.transitions) == 1
    assert sink.transitions[0][0] == connection_id
    assert sink.transitions[0][1] == 12
    assert sink.transitions[0][2] is True
    # A post-recovery reassertion intentionally omits the account snapshot so
    # the Edge endpoint routes it through the authenticated connection-
    # transition RPC that releases the parked recovery job.
    assert sink.transitions[0][3] is None
    persisted = read_json(state_path)
    assert "_source_recovery_observed_sequence" not in persisted
    assert "pending" not in persisted


@pytest.mark.parametrize("clean_sequence", [10, 9, None, True])
def test_stale_clean_heartbeat_cannot_clear_source_recovery(
    tmp_path, clean_sequence
):
    connection_id = str(uuid4())
    root = tmp_path / "instances" / connection_id
    files_dir = root / "terminal" / "MQL5" / "Files" / "TradeJournal"
    events_dir = files_dir / "events"
    events_dir.mkdir(parents=True)
    marker = events_dir / "event-10.json"
    marker.write_text("{}", encoding="utf-8")
    state_path = root / "state" / "connection-state.json"
    atomic_json(
        state_path,
        {
            "connected": True,
            "sequence": 8,
            "source_recovery_required": False,
            "_source_recovery_observed_sequence": 10,
        },
    )
    sink = FakeSink()
    adapter = FakeAdapter(files_dir)
    adapter.connection_state = lambda: {
        "connected": True,
        "sequence": clean_sequence,
        "source_recovery_required": False,
    }
    supervisor = Mt5EventSupervisor(
        tmp_path / "instances", tmp_path / "secrets", "https://example.invalid"
    )
    supervisor._components = lambda _cid: (root, adapter, sink)  # type: ignore[method-assign]

    supervisor._process(connection_id)

    assert marker.exists()
    assert sink.events == []
    assert sink.transitions == []
    assert read_json(state_path)["_source_recovery_observed_sequence"] == 10


def test_failed_post_recovery_reassertion_remains_durable_without_spam(tmp_path):
    connection_id = str(uuid4())
    root = tmp_path / "instances" / connection_id
    files_dir = root / "terminal" / "MQL5" / "Files" / "TradeJournal"
    files_dir.mkdir(parents=True)
    state_path = root / "state" / "connection-state.json"
    atomic_json(
        state_path,
        {
            "connected": True,
            "sequence": 20,
            "source_recovery_required": False,
        },
    )
    sink = FakeSink()
    adapter = FakeAdapter(files_dir)
    observed_state = {
        "connected": False,
        "sequence": 21,
        "source_recovery_required": True,
    }
    adapter.connection_state = lambda: dict(observed_state)
    supervisor = Mt5EventSupervisor(
        tmp_path / "instances", tmp_path / "secrets", "https://example.invalid"
    )
    supervisor._components = lambda _cid: (root, adapter, sink)  # type: ignore[method-assign]
    supervisor._process(connection_id)

    observed_state.update(
        connected=True,
        sequence=22,
        source_recovery_required=False,
    )
    sink.confirmed = False
    sink.pending = 1
    supervisor._process(connection_id)
    supervisor._process(connection_id)

    assert len(sink.transitions) == 1
    failed_state = read_json(state_path)
    assert failed_state["_source_recovery_observed_sequence"] == 21
    assert failed_state["pending"]["_transition_enqueued"] is True

    # The durable outbox retry acknowledges the already-enqueued transition;
    # it must clear the marker without generating a second heartbeat.
    sink.confirmed = True
    sink.pending = 0
    supervisor._retry_after[connection_id] = 0
    supervisor._process(connection_id)

    assert len(sink.transitions) == 1
    persisted = read_json(state_path)
    assert "_source_recovery_observed_sequence" not in persisted
    assert "pending" not in persisted


def test_pending_history_handoff_blocks_supervisor_until_activation_commits(tmp_path):
    connection_id = str(uuid4())
    root = tmp_path / "instances" / connection_id
    files_dir = root / "terminal" / "MQL5" / "Files" / "TradeJournal"
    events_dir = files_dir / "events"
    events_dir.mkdir(parents=True)
    marker = events_dir / "event-12.json"
    marker.write_text("{}", encoding="utf-8")
    # The archive has been accepted, but its exact frozen baseline has not
    # yet switched/acknowledged in the same EA process. The generic watcher
    # must not consume this source prefix in the interval.
    atomic_json(
        root / "state" / "history-handoff-pending.json",
        {"schema_version": 1, "delivery": {"inserted": 1, "duplicates": 0}},
    )
    sink = FakeSink()
    adapter = FakeAdapter(files_dir)
    supervisor = Mt5EventSupervisor(
        tmp_path / "instances", tmp_path / "secrets", "https://example.invalid"
    )
    supervisor._components = lambda _cid: (root, adapter, sink)  # type: ignore[method-assign]

    supervisor._process(connection_id)

    assert marker.exists()
    assert sink.events == []
    assert sink.transitions == []
    assert sink.flushes == 0


def test_failed_transition_is_not_resent_on_every_local_heartbeat(tmp_path):
    connection_id = str(uuid4())
    root = tmp_path / "instances" / connection_id
    files_dir = root / "terminal" / "MQL5" / "Files" / "TradeJournal"
    files_dir.mkdir(parents=True)
    sink = FakeSink()
    sink.pending = 1
    adapter = FakeAdapter(files_dir)
    supervisor = Mt5EventSupervisor(tmp_path / "instances", tmp_path / "secrets", "https://example.invalid")
    supervisor._components = lambda _cid: (root, adapter, sink)  # type: ignore[method-assign]

    supervisor._process(connection_id)
    supervisor._process(connection_id)

    assert len(sink.transitions) == 1
    assert sink.flushes == 2


def test_startup_reasserts_a_verified_connected_state_even_when_it_is_persisted(tmp_path):
    connection_id = str(uuid4())
    root = tmp_path / "instances" / connection_id
    files_dir = root / "terminal" / "MQL5" / "Files" / "TradeJournal"
    files_dir.mkdir(parents=True)
    sink = FakeSink()
    adapter = FakeAdapter(files_dir)
    atomic_json(root / "state" / "connection-state.json", adapter.connection_state())
    supervisor = Mt5EventSupervisor(tmp_path / "instances", tmp_path / "secrets", "https://example.invalid")
    supervisor._components = lambda _cid: (root, adapter, sink)  # type: ignore[method-assign]
    supervisor._startup_reassertions.add(connection_id)

    supervisor._process(connection_id)

    assert len(sink.transitions) == 1
    assert sink.transitions[0][2] is True
    assert sink.transitions[0][3] == adapter.account_snapshot()
    assert connection_id not in supervisor._startup_reassertions
    assert (root / "state" / "connection-state.json").exists()


def test_startup_reassertion_waits_for_durable_acknowledgement(tmp_path):
    connection_id = str(uuid4())
    root = tmp_path / "instances" / connection_id
    files_dir = root / "terminal" / "MQL5" / "Files" / "TradeJournal"
    files_dir.mkdir(parents=True)
    sink = FakeSink()
    sink.pending = 1
    adapter = FakeAdapter(files_dir)
    atomic_json(root / "state" / "connection-state.json", adapter.connection_state())
    supervisor = Mt5EventSupervisor(tmp_path / "instances", tmp_path / "secrets", "https://example.invalid")
    supervisor._components = lambda _cid: (root, adapter, sink)  # type: ignore[method-assign]
    supervisor._startup_reassertions.add(connection_id)

    supervisor._process(connection_id)
    assert len(sink.transitions) == 1
    assert connection_id in supervisor._startup_reassertions

    sink.pending = 0
    supervisor._retry_after[connection_id] = 0
    supervisor._process(connection_id)

    assert len(sink.transitions) == 1
    assert connection_id not in supervisor._startup_reassertions


def test_startup_reassertion_cannot_ack_an_intent_that_failed_before_enqueue(
    tmp_path,
):
    connection_id = str(uuid4())
    root = tmp_path / "instances" / connection_id
    files_dir = root / "terminal" / "MQL5" / "Files" / "TradeJournal"
    files_dir.mkdir(parents=True)
    sink = FakeSink()
    sink.fail_enqueue = True
    adapter = FakeAdapter(files_dir)
    atomic_json(root / "state" / "connection-state.json", adapter.connection_state())
    supervisor = Mt5EventSupervisor(
        tmp_path / "instances", tmp_path / "secrets", "https://example.invalid"
    )
    supervisor._components = lambda _cid: (root, adapter, sink)  # type: ignore[method-assign]
    supervisor._startup_reassertions.add(connection_id)

    with pytest.raises(RuntimeError, match="simulated enqueue failure"):
        supervisor._process(connection_id)

    failed_state = read_json(root / "state" / "connection-state.json")
    assert failed_state["pending"]["_transition_enqueued"] is False
    assert sink.transitions == []
    assert connection_id in supervisor._startup_reassertions

    sink.fail_enqueue = False
    supervisor._process(connection_id)

    assert len(sink.transitions) == 1
    assert "pending" not in read_json(root / "state" / "connection-state.json")
    assert connection_id not in supervisor._startup_reassertions


def test_startup_reassertion_does_not_treat_a_dead_letter_as_delivery(tmp_path):
    connection_id = str(uuid4())
    root = tmp_path / "instances" / connection_id
    files_dir = root / "terminal" / "MQL5" / "Files" / "TradeJournal"
    files_dir.mkdir(parents=True)
    sink = FakeSink()
    sink.confirmed = False
    adapter = FakeAdapter(files_dir)
    atomic_json(root / "state" / "connection-state.json", adapter.connection_state())
    supervisor = Mt5EventSupervisor(tmp_path / "instances", tmp_path / "secrets", "https://example.invalid")
    supervisor._components = lambda _cid: (root, adapter, sink)  # type: ignore[method-assign]
    supervisor._startup_reassertions.add(connection_id)

    supervisor._process(connection_id)
    supervisor._process(connection_id)

    assert len(sink.transitions) == 1
    assert connection_id in supervisor._startup_reassertions


def test_run_fails_fast_when_observer_dies_after_readiness(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    class DyingObserver:
        def __init__(self):
            self.alive_checks = 0
            self.stopped = False
            self.joined = False

        @staticmethod
        def schedule(*_args, **_kwargs):
            return None

        @staticmethod
        def start():
            return None

        def is_alive(self):
            self.alive_checks += 1
            return self.alive_checks == 1

        def stop(self):
            self.stopped = True

        def join(self, timeout):
            assert timeout == 5
            self.joined = True

    observer = DyingObserver()
    monkeypatch.setattr(event_supervisor, "Observer", lambda: observer)
    supervisor = Mt5EventSupervisor(
        tmp_path / "instances",
        tmp_path / "secrets",
        "https://example.invalid",
    )
    ready_event = threading.Event()

    with pytest.raises(RuntimeError, match="observer stopped unexpectedly"):
        supervisor.run(threading.Event(), ready_event)

    assert ready_event.is_set()
    assert observer.stopped is True
    assert observer.joined is True
