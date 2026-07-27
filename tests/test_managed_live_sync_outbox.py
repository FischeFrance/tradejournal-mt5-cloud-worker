from __future__ import annotations

from pathlib import Path

import pytest

from worker.event_outbox import EventOutbox
from worker.event_sender import SendResult
from windows_agent.real_handlers import PersistentSnapshot
from windows_agent.worker.dedup import PersistentDedup
from windows_agent.worker.live_sync import LiveSync, LiveSyncDeliveryError


class Adapter:
    def __init__(self, snapshot: dict) -> None:
        self._snapshot = snapshot

    def verify_identity(self) -> dict[str, str]:
        return {"login": "42", "server": "Fixture-Demo"}

    def snapshot(self) -> dict:
        return self._snapshot


class Sender:
    def __init__(self, results: list[SendResult]) -> None:
        self.results = list(results)
        self.payloads: list[dict] = []

    def send(self, payload: dict) -> SendResult:
        self.payloads.append(payload)
        return self.results.pop(0)

    def __call__(self, payload: dict) -> None:
        result = self.send(payload)
        if result.status != "sent":
            raise RuntimeError("not sent")


class StreamAdapter(Adapter):
    def __init__(self, snapshot: dict, records: tuple[dict, ...] | None = None) -> None:
        super().__init__(snapshot)
        self.acknowledged: int | None = None
        self._records = records

    def pending_events(self) -> tuple[dict, ...]:
        return self._records or (
            {
                "sequence": 17,
                "event_type": "DEAL_ADD",
                "ticket": "900",
                "position_id": "100",
                "entry": "OUT",
                "symbol": "EURUSD",
                "direction": "sell",
                "volume": 0.1,
                "price": 1.2,
                "profit": 10.0,
                "commission": -1.0,
                "swap": 0.0,
                "time": "2026-07-27T10:05:00Z",
            },
        )

    def acknowledge_events(self, through_sequence: int) -> None:
        self.acknowledged = through_sequence


def _snapshot() -> dict:
    return {
        "positions": {
            "100": {
                "ticket": "100",
                "symbol": "EURUSD",
                "direction": "buy",
                "volume": 0.1,
                "open_price": 1.1,
                "open_time": "2026-07-27T10:00:00Z",
            }
        },
        "orders": {},
        "deals": {},
    }


def _live(root: Path, sender: Sender) -> LiveSync:
    return LiveSync(
        Adapter(_snapshot()),
        PersistentSnapshot(root / "snapshot.json"),
        PersistentDedup(root / "dedup.sqlite"),
        sender,
        outbox=EventOutbox(str(root / "outbox.json")),
    )


def test_transient_delivery_survives_snapshot_advance_and_restart(tmp_path: Path) -> None:
    first = Sender(
        [SendResult(status="failed", failure_type="transient", error="timeout")]
    )
    with pytest.raises(LiveSyncDeliveryError, match="pending=1"):
        _live(tmp_path, first).poll_once()

    persisted = EventOutbox(str(tmp_path / "outbox.json"))
    assert persisted.pending_count() == 1
    assert (tmp_path / "snapshot.json").is_file()

    second = Sender([SendResult(status="sent", http_status=200, attempts=1)])
    assert _live(tmp_path, second).poll_once() == 1
    assert EventOutbox(str(tmp_path / "outbox.json")).pending_count() == 0
    assert len(second.payloads) == 1


def test_permanent_rejection_is_visible_and_preserved_in_dead_letter(tmp_path: Path) -> None:
    sender = Sender(
        [SendResult(status="failed", failure_type="permanent", http_status=422)]
    )
    with pytest.raises(LiveSyncDeliveryError, match="dead_lettered=1"):
        _live(tmp_path, sender).poll_once()

    persisted = EventOutbox(str(tmp_path / "outbox.json"))
    assert persisted.pending_count() == 0
    assert persisted.dead_letter_count() == 1

    # A causal successor must not bypass an unresolved permanent failure.
    retry_sender = Sender([])
    with pytest.raises(LiveSyncDeliveryError, match="dead_lettered=1"):
        _live(tmp_path, retry_sender).poll_once()
    assert retry_sender.payloads == []


def test_mql5_event_stream_is_primary_and_acknowledged_after_outbox_persist(tmp_path: Path) -> None:
    adapter = StreamAdapter({"positions": {}, "orders": {}, "deals": {}})
    sender = Sender([SendResult(status="sent", http_status=200, attempts=1)])
    live = LiveSync(
        adapter,
        PersistentSnapshot(tmp_path / "snapshot.json"),
        PersistentDedup(tmp_path / "dedup.sqlite"),
        sender,
        outbox=EventOutbox(str(tmp_path / "outbox.json")),
    )

    assert live.poll_once() == 1
    assert adapter.acknowledged == 17
    assert sender.payloads[0]["event_type"] == "trade_closed"
    assert sender.payloads[0]["external_trade_id"] == "100"


def test_partial_close_is_volume_change_and_not_a_duplicate_close(tmp_path: Path) -> None:
    previous = _snapshot()
    previous["positions"]["100"]["volume"] = 0.2
    PersistentSnapshot(tmp_path / "snapshot.json").save(previous)
    current = _snapshot()
    records = (
        {
            "sequence": 18,
            "event_type": "DEAL_ADD",
            "ticket": "901",
            "deal_id": "901",
            "position_id": "100",
            "entry": "OUT",
            "symbol": "EURUSD",
            "direction": "sell",
            "volume": 0.1,
            "price": 1.2,
            "time": "2026-07-27T10:06:00Z",
        },
    )
    adapter = StreamAdapter(current, records)
    sender = Sender([SendResult(status="sent", http_status=200, attempts=1)])
    live = LiveSync(
        adapter,
        PersistentSnapshot(tmp_path / "snapshot.json"),
        PersistentDedup(tmp_path / "dedup.sqlite"),
        sender,
        outbox=EventOutbox(str(tmp_path / "outbox.json")),
    )

    assert live.poll_once() == 1
    assert [payload["event_type"] for payload in sender.payloads] == [
        "trade_volume_changed"
    ]
    assert sender.payloads[0]["volume"] == 0.1


def test_position_stream_event_preserves_snapshot_modification(tmp_path: Path) -> None:
    previous = _snapshot()
    PersistentSnapshot(tmp_path / "snapshot.json").save(previous)
    current = _snapshot()
    current["positions"]["100"]["stop_loss"] = 1.05
    records = (
        {
            "sequence": 19,
            "event_type": "POSITION",
            "ticket": "100",
            "position_id": "100",
            "symbol": "EURUSD",
            "direction": "buy",
            "volume": 0.1,
            "stop_loss": 1.05,
            "time": "2026-07-27T10:07:00Z",
        },
    )
    adapter = StreamAdapter(current, records)
    sender = Sender([SendResult(status="sent", http_status=200, attempts=1)])
    live = LiveSync(
        adapter,
        PersistentSnapshot(tmp_path / "snapshot.json"),
        PersistentDedup(tmp_path / "dedup.sqlite"),
        sender,
        outbox=EventOutbox(str(tmp_path / "outbox.json")),
    )

    assert live.poll_once() == 1
    assert [payload["event_type"] for payload in sender.payloads] == [
        "trade_modified"
    ]
    assert sender.payloads[0]["stop_loss"] == 1.05


def test_stream_open_and_snapshot_open_are_merged_without_duplication(
    tmp_path: Path,
) -> None:
    current = _snapshot()
    records = (
        {
            "sequence": 20,
            "event_type": "DEAL_ADD",
            "ticket": "902",
            "deal_id": "902",
            "position_id": "100",
            "entry": "IN",
            "symbol": "EURUSD",
            "direction": "buy",
            "volume": 0.1,
            "price": 1.1,
            "time": "2026-07-27T10:00:00Z",
        },
    )
    adapter = StreamAdapter(current, records)
    sender = Sender([SendResult(status="sent", http_status=200, attempts=1)])
    live = LiveSync(
        adapter,
        PersistentSnapshot(tmp_path / "snapshot.json"),
        PersistentDedup(tmp_path / "dedup.sqlite"),
        sender,
        outbox=EventOutbox(str(tmp_path / "outbox.json")),
    )

    assert live.poll_once() == 1
    assert [payload["event_type"] for payload in sender.payloads] == [
        "trade_opened"
    ]
