from __future__ import annotations

import pytest

from worker.event_outbox import EventOutbox
from worker.event_sender import SendResult
from worker.event_normalizer import normalize_event
from windows_agent.worker.dedup import PersistentDedup
from windows_agent.worker.live_sync import (
    CertifiedHistoryRecoveryRequired,
    LiveSync,
    LiveSyncDeliveryError,
)


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


class _OpeningAdapter:
    def __init__(self) -> None:
        self.events = (
            {
                "sequence": 43,
                "event_type": "DEAL_ADD",
                "deal_id": "9002",
                "position_id": "1002",
                "order_id": "8002",
                # This deliberately lacks symbol/direction/volume. It is a
                # temporary EA/snapshot race, not a permanent bad event.
                "entry": "IN",
                "price": 23456.7,
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


class _UncertifiedReductionAdapter:
    """Managed event prefix with only a POSITION volume hint, never an OUT deal."""

    def __init__(self) -> None:
        self.events = (
            {
                "sequence": 44,
                "event_type": "POSITION",
                "position_id": "1001",
                "symbol": "DAX40",
                "direction": "buy",
                "volume": 0.4,
                "time": "2026-09-23T16:42:47Z",
            },
        )
        self.acknowledged_sequence: int | None = None
        self.recovery_requests = 0
        self.current = {
            "positions": {
                "1001": {
                    "ticket": "1001",
                    "symbol": "DAX40",
                    "direction": "buy",
                    "volume": 0.4,
                    "open_price": 23480.0,
                }
            },
            "orders": {},
            "deals": {},
        }

    def verify_identity(self) -> dict[str, str]:
        return {"login": "42", "server": "Demo"}

    def snapshot(self) -> dict:
        return self.current

    def pending_events(self) -> tuple[dict, ...]:
        return self.events

    def acknowledge_events(self, sequence: int) -> None:
        self.acknowledged_sequence = sequence
        self.events = ()

    def request_certified_history_recovery(self) -> None:
        self.recovery_requests += 1


class _UncertifiedDisappearanceAdapter(_UncertifiedReductionAdapter):
    """A complete snapshot disappearance without its native OUT callback."""

    def __init__(self) -> None:
        super().__init__()
        self.events = ()
        self.current = {"positions": {}, "orders": {}, "deals": {}}


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


def test_incomplete_opening_stays_unacked_without_outbox_snapshot_or_dedup_mutation(tmp_path):
    adapter = _OpeningAdapter()
    state = _SnapshotState({"positions": {}, "orders": {}, "deals": {}})
    outbox = EventOutbox(str(tmp_path / "live-outbox.json"))
    sender = _Sender([SendResult(status="sent", http_status=200, attempts=1)])
    dedup = PersistentDedup(tmp_path / "dedup.sqlite")
    try:
        sync = LiveSync(adapter, state, dedup, sender, outbox=outbox)
        with pytest.raises(ValueError, match="trade_opened symbol unavailable"):
            sync.poll_once()

        # No source acknowledgement, payload persistence, dedup row or
        # snapshot advance may occur before preflight accepts the predecessor.
        assert adapter.acknowledged_sequence is None
        assert adapter.events and adapter.events[0]["sequence"] == 43
        assert outbox.pending_count() == 0
        assert state.value == {"positions": {}, "orders": {}, "deals": {}}
        expected_event_id = normalize_event(
            {
                **adapter.events[0],
                "event_type": "trade_opened",
                "ticket": "1002",
                "symbol": "DAX40",
                "direction": "buy",
                "volume": 1.0,
                "open_price": 23456.7,
                "open_time": "2026-09-23T16:42:47Z",
                "native_deal_ticket": "9002",
            },
            "42",
            "Demo",
        )["event_id"]
        assert not dedup.contains(expected_event_id)

        adapter.events = (
            {
                **adapter.events[0],
                "symbol": "DAX40",
                "direction": "buy",
                "volume": 1.0,
            },
        )
        assert sync.poll_once() == 1
        assert sync.poll_once() == 0
        assert dedup.contains(expected_event_id)
    finally:
        dedup.close()

    assert adapter.acknowledged_sequence == 43
    assert [payload["event_type"] for payload in sender.payloads] == ["trade_opened"]
    assert sender.payloads[0]["external_trade_id"] == "1002"
    assert outbox.pending_count() == 0


def test_uncertified_position_reduction_requests_history_without_dead_lettering_close(tmp_path):
    adapter = _UncertifiedReductionAdapter()
    state = _SnapshotState(_previous_open_position())
    outbox = EventOutbox(str(tmp_path / "live-outbox.json"))
    sender = _Sender([SendResult(status="sent", http_status=200, attempts=1)])
    dedup = PersistentDedup(tmp_path / "dedup.sqlite")
    try:
        sync = LiveSync(adapter, state, dedup, sender, outbox=outbox)
        with pytest.raises(CertifiedHistoryRecoveryRequired):
            sync.poll_once()

        # The source prefix and old baseline remain intact. In particular no
        # generic partial-close payload can reach EventSender and be isolated
        # as a permanent API 422 dead letter.
        assert adapter.recovery_requests == 1
        assert adapter.acknowledged_sequence is None
        assert adapter.events and adapter.events[0]["sequence"] == 44
        assert state.value == _previous_open_position()
        assert outbox.pending_count() == 0
        assert outbox.dead_letter_count() == 0
        assert sender.payloads == []

        # Once the EA supplies the certified DEAL_ADD OUT in the same prefix,
        # the original POSITION hint is ignored and the economic close is sent
        # exactly as the explicit partial-close contract requires.
        adapter.events = (
            *adapter.events,
            {
                "sequence": 45,
                "event_type": "DEAL_ADD",
                "deal_id": "9001",
                "position_id": "1001",
                "order_id": "8001",
                "symbol": "DAX40",
                "direction": "sell",
                "volume": 0.6,
                "price": 23456.7,
                "entry": "OUT",
                "profit": -26.48,
                "commission": -1.0,
                "swap": 0.0,
                "time": "2026-09-23T16:42:47Z",
            },
        )
        assert sync.poll_once() == 1
    finally:
        dedup.close()

    assert adapter.acknowledged_sequence == 45
    assert [payload["event_type"] for payload in sender.payloads] == [
        "trade_partial_closed"
    ]
    assert sender.payloads[0]["partial_close"] is True
    assert outbox.dead_letter_count() == 0


def test_uncertified_position_disappearance_requests_history_without_snapshot_close(tmp_path):
    adapter = _UncertifiedDisappearanceAdapter()
    previous = _previous_open_position()
    state = _SnapshotState(previous)
    outbox = EventOutbox(str(tmp_path / "live-outbox.json"))
    sender = _Sender([SendResult(status="sent", http_status=200, attempts=1)])
    dedup = PersistentDedup(tmp_path / "dedup.sqlite")
    try:
        sync = LiveSync(adapter, state, dedup, sender, outbox=outbox)
        with pytest.raises(CertifiedHistoryRecoveryRequired):
            sync.poll_once()
    finally:
        dedup.close()

    assert adapter.recovery_requests == 1
    assert adapter.acknowledged_sequence is None
    assert state.value == previous
    assert outbox.pending_count() == 0
    assert outbox.dead_letter_count() == 0
    assert sender.payloads == []
