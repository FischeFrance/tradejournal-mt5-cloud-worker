from __future__ import annotations

import threading
from types import SimpleNamespace
from uuid import uuid4

import pytest

import windows_agent.event_supervisor as event_supervisor
from windows_agent.event_supervisor import Mt5EventSupervisor


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
        return {"connected": True, "sequence": 12}

    def account_snapshot(self):
        return {"balance": 1000, "equity": 1001, "currency": "EUR", "leverage": 100}


class FakeSink:
    def __init__(self):
        self.events = []
        self.transitions = []
        self.flushes = 0
        self.pending = 0

    def __call__(self, payload):
        self.events.append(payload)

    def flush_transitions(self):
        self.flushes += 1
        return SimpleNamespace(pending=self.pending)

    def pending_transition_count(self):
        return self.pending

    def enqueue_many(self, payloads):
        self.events.extend(payloads)
        return SimpleNamespace(pending=self.pending)

    def send_connection_transition(self, *args):
        self.transitions.append(args)
        return SimpleNamespace(pending=0)


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
    assert sink.flushes == 2


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
    assert sink.flushes == 1


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
