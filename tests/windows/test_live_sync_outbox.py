from __future__ import annotations

import pytest

from worker.event_outbox import EventOutbox
from worker.event_sender import SendResult
from windows_agent.worker.dedup import PersistentDedup
from windows_agent.worker.live_sync import LiveSync, LiveSyncDeliveryError


class _SnapshotState:
    def __init__(self, value: dict) -> None:
        self.value = value

    def get(self) -> dict:
        return self.value

    def save(self, value: dict) -> None:
        self.value = value


class _ClosingAdapter:
    def __init__(self) -> None:
        self.events = (
            {
                "sequence": 42,
                "event_type": "DEAL_ADD",
                "deal_id": "9001",
                "position_id": "1001",
                "order_id": "8001",
                "symbol": "DAX40",
                "direction": "sell",
                "volume": 1.0,
                "price": 23456.7,
                "entry": "OUT",
                "profit": -26.48,
                "commission": -1.0,
                "swap": 0.0,
                "time": "2026-09-23T16:42:47Z",
            },
        )
        self.acknowledged_sequence: int | None = None
        self.current = {"positions": {}, "orders": {}, "deals": {}}

    def verify_identity(self) -> dict[str, str]:
        return {"login": "42", "server": "Demo"}

    def snapshot(self) -> dict:
        return self.current

    def pending_events(self) -> tuple[dict, ...]:
        return self.events

    def acknowledge_events(self, sequence: int) -> None:
        self.acknowledged_sequence = sequence
        self.events = ()


class _Sender:
    def __init__(self, results: list[SendResult]) -> None:
        self.results = list(results)
        self.payloads: list[dict] = []

    def send(self, payload: dict) -> SendResult:
        self.payloads.append(payload)
        return self.results.pop(0)


class _NeverCalledAdapter:
    def verify_identity(self) -> dict[str, str]:
        raise AssertionError("the pending causal prefix must be delivered first")


def _previous_open_position() -> dict:
    return {
        "positions": {
            "1001": {
                "ticket": "1001",
                "symbol": "DAX40",
                "direction": "buy",
                "volume": 1.0,
                "open_price": 23480.0,
            }
        },
        "orders": {},
        "deals": {},
    }


def _payload(event_id: str, event_type: str) -> dict:
    return {"event_id": event_id, "event_type": event_type}


def test_prior_permanent_dead_letter_does_not_block_later_deal_add_close(tmp_path):
    outbox_path = tmp_path / "event-supervisor-outbox.json"
    rejected = _payload("pending-order-filled-rejected", "pending_order_filled")
    outbox = EventOutbox(str(outbox_path))
    outbox.enqueue_many([rejected])
    rejected_sender = _Sender([
        SendResult(
            status="failed",
            http_status=422,
            error="rejected_by_api",
            attempts=1,
            failure_type="permanent",
        )
    ])
    assert outbox.drain(rejected_sender).dead_lettered == 1

    # Reopen the persisted outbox to model the next supervisor wake after the permanent failure.
    persisted_outbox = EventOutbox(str(outbox_path))
    assert persisted_outbox.pending_count() == 0
    assert persisted_outbox.dead_letter_count() == 1
    adapter = _ClosingAdapter()
    sender = _Sender([SendResult(status="sent", http_status=200, attempts=1)])
    state = _SnapshotState(_previous_open_position())
    dedup = PersistentDedup(tmp_path / "dedup.sqlite")
    try:
        sync = LiveSync(adapter, state, dedup, sender, outbox=persisted_outbox)
        assert sync.poll_once() == 1
    finally:
        dedup.close()

    assert adapter.acknowledged_sequence == 42
    assert state.value == adapter.current
    assert [payload["event_type"] for payload in sender.payloads] == ["trade_closed"]
    assert sender.payloads[0]["external_trade_id"] == "1001"
    assert sender.payloads[0]["close_time"] == "2026-09-23T16:42:47Z"
    assert persisted_outbox.pending_count() == 0
    assert persisted_outbox.dead_letter_count() == 1


def test_transient_pending_delivery_still_blocks_live_sync(tmp_path):
    outbox = EventOutbox(str(tmp_path / "event-supervisor-outbox.json"))
    outbox.enqueue_many([_payload("trade-closed-pending", "trade_closed")])
    sender = _Sender([
        SendResult(
            status="failed",
            error="http_503",
            attempts=3,
            failure_type="transient",
        )
    ])
    dedup = PersistentDedup(tmp_path / "dedup.sqlite")
    try:
        sync = LiveSync(
            _NeverCalledAdapter(),
            _SnapshotState(_previous_open_position()),
            dedup,
            sender,
            outbox=outbox,
        )
        with pytest.raises(LiveSyncDeliveryError, match="pending=1"):
            sync.poll_once()
    finally:
        dedup.close()

    assert outbox.pending_count() == 1
    assert outbox.dead_letter_count() == 0
