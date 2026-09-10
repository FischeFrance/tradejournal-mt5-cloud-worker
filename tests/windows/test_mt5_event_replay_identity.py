from __future__ import annotations

from windows_agent.worker.live_sync import (
    _merge_event_stream_with_snapshot,
    _mql5_file_event,
)
from worker.event_normalizer import normalize_event


def test_replayed_deal_keeps_source_identity_when_snapshot_mapping_changes():
    source_identity = (
        "00000000-0000-4000-8000-000000000001|42|Demo|"
        "DEAL_ADD|9001|1787866200000"
    )
    record = {
        "event_id": f"{source_identity}|EURUSD|buy|0.10000000|1.10000000",
        "event_type": "DEAL_ADD",
        "ticket": "9001",
        "position_id": "1001",
        "order_id": "8001",
        "deal_id": "9001",
        "symbol": "EURUSD",
        "direction": "buy",
        "volume": 0.1,
        "price": 1.1,
        "entry": "IN",
        "time": "2026-08-27T21:30:00Z",
    }
    empty = {"positions": {}, "orders": {}, "deals": {}}
    opened = {
        "positions": {
            "1001": {
                "ticket": "1001",
                "symbol": "EURUSD",
                "direction": "buy",
                "volume": 0.1,
            }
        },
        "orders": {},
        "deals": {},
    }

    first = _mql5_file_event(record, empty, opened)
    replay = _mql5_file_event(record, opened, opened)
    legacy = _mql5_file_event(
        {**record, "event_id": f"{source_identity}|77"},
        empty,
        opened,
    )
    reprovisioned = _mql5_file_event(
        {
            **record,
            "event_id": record["event_id"].replace(
                "00000000-0000-4000-8000-000000000001",
                "00000000-0000-4000-8000-000000000002",
                1,
            ),
        },
        opened,
        opened,
    )

    first_payload = normalize_event(first, "42", "Demo")
    replay_payload = normalize_event(replay, "42", "Demo")
    legacy_payload = normalize_event(legacy, "42", "Demo")
    reprovisioned_payload = normalize_event(reprovisioned, "42", "Demo")

    assert first["event_type"] == "trade_opened"
    assert first["origin_order_ticket"] == "8001"
    assert replay["event_type"] == "trade_volume_changed"
    assert first_payload["event_id"] == replay_payload["event_id"]
    assert legacy_payload["event_id"] == replay_payload["event_id"]
    assert reprovisioned_payload["event_id"] == replay_payload["event_id"]


def test_order_delete_is_ignored_and_history_add_is_canonical_cancel():
    empty = {"positions": {}, "orders": {}, "deals": {}}
    delete = {
        "event_id": (
            "00000000-0000-4000-8000-000000000001|42|Demo|"
            "ORDER_DELETE|8001|1787866200000|EURUSD"
        ),
        "event_type": "ORDER_DELETE",
        "order_type": 2,
        "ticket": "8001",
        "order_id": "8001",
        "symbol": "EURUSD",
        "direction": "buy",
        "volume": 0.1,
        "price": 1.1,
        "time": "2026-08-27T21:30:00Z",
    }
    history = {
        **delete,
        "event_id": (
            "00000000-0000-4000-8000-000000000001|42|Demo|"
            "HISTORY_ADD|8001|1787866200000|EURUSD"
        ),
        "event_type": "HISTORY_ADD",
    }

    assert _mql5_file_event(delete, empty, empty) is None
    cancelled = _mql5_file_event(history, empty, empty)
    assert cancelled is not None
    assert cancelled["event_type"] == "pending_order_cancelled"
    assert cancelled["source_event_id"] == (
        "42|Demo|HISTORY_ADD|8001|1787866200000"
    )


def test_pending_fill_source_event_uses_the_broker_order_ticket():
    empty = {"positions": {}, "orders": {}, "deals": {}}
    filled = {
        "event_id": (
            "00000000-0000-4000-8000-000000000001|42|Demo|"
            "HISTORY_FILLED|8001|1787866200000|EURUSD"
        ),
        "event_type": "HISTORY_FILLED",
        "order_type": 2,
        "ticket": "8001",
        "order_id": "8001",
        "symbol": "EURUSD",
        "direction": "buy",
        "volume": 0.1,
        "price": 1.1,
        "time": "2026-08-27T21:30:00Z",
    }

    event = _mql5_file_event(filled, empty, empty)

    assert event is not None
    assert event["event_type"] == "pending_order_filled"
    assert event["ticket"] == "8001"
    assert event["volume"] is None
    assert event["source_event_id"] == (
        "42|Demo|HISTORY_FILLED|8001|1787866200000"
    )


def test_stream_fill_suppresses_a_stale_snapshot_cancellation_and_keeps_ids_distinct():
    source_prefix = "00000000-0000-4000-8000-000000000001|42|Demo"
    previous = {
        "positions": {},
        "orders": {
            "8001": {
                "ticket": "8001", "symbol": "EURUSD", "direction": "buy",
                "volume": 0.1, "price": 1.1, "stop_loss": 1.09, "take_profit": 1.11,
            }
        },
        "deals": {},
    }
    current = {
        "positions": {
            "1001": {
                "ticket": "1001", "symbol": "EURUSD", "direction": "buy",
                "volume": 0.1, "open_price": 1.1, "stop_loss": 1.09, "take_profit": 1.11,
            }
        },
        "orders": {},
        "deals": {},  # The periodic snapshot is deliberately stale while source events arrive.
    }
    records = (
        {
            "event_id": f"{source_prefix}|DEAL_ADD|9001|1787866200000|EURUSD",
            "event_type": "DEAL_ADD",
            "ticket": "9001",
            "position_id": "1001",
            "order_id": "8001",
            "deal_id": "9001",
            "symbol": "EURUSD",
            "direction": "buy",
            "volume": 0.1,
            "price": 1.1,
            "entry": "IN",
            "time": "2026-08-27T21:30:00Z",
        },
        {
            "event_id": f"{source_prefix}|HISTORY_FILLED|8001|1787866200001|EURUSD",
            "event_type": "HISTORY_FILLED",
            "order_type": 2,
            "ticket": "8001",
            "order_id": "8001",
            "symbol": "EURUSD",
            "direction": "buy",
            "volume": 0.1,
            "price": 1.1,
            "time": "2026-08-27T21:30:00Z",
        },
    )

    events = _merge_event_stream_with_snapshot(records, previous, current)
    payloads = [normalize_event(event, "42", "Demo") for event in events]

    assert [event["event_type"] for event in events] == [
        "trade_opened",
        "pending_order_filled",
    ]
    assert events[0]["origin_order_ticket"] == "8001"
    assert payloads[0]["event_id"] != payloads[1]["event_id"]
    assert [payload["event_id"] for payload in payloads] == [
        normalize_event(event, "42", "Demo")["event_id"]
        for event in _merge_event_stream_with_snapshot(records, previous, current)
    ]


def test_market_orders_never_enter_pending_order_projection():
    empty = {"positions": {}, "orders": {}, "deals": {}}
    market = {
        "event_type": "ORDER_ADD", "ticket": "9001", "order_id": "9001",
        "order_type": 0, "symbol": "USDCAD", "direction": "buy", "volume": 2.79,
        "price": 1.3782, "time": "2026-09-09T12:58:34Z",
    }
    pending = {
        **market, "ticket": "8001", "order_id": "8001", "order_type": 3,
        "direction": "sell",
    }

    assert _mql5_file_event(market, empty, empty) is None
    created = _mql5_file_event(pending, empty, empty)
    assert created is not None
    assert created["event_type"] == "pending_order_created"
    assert created["order_type"] == 3


def test_unchanged_active_pending_order_is_reasserted_with_placement_time():
    order = {
        "ticket": "8001", "symbol": "EURUSD", "direction": "buy", "volume": 0.1,
        "price": 1.1, "stop_loss": 1.09, "take_profit": 1.12, "order_type": 2,
        "placed_at": "2026-08-27T21:00:00Z",
    }
    snapshot = {"positions": {}, "orders": {"8001": order}, "deals": {}}

    events = _merge_event_stream_with_snapshot((), snapshot, snapshot)

    assert events == [{
        "event_type": "pending_order_modified", "ticket": "8001", "symbol": "EURUSD",
        "direction": "buy", "volume": 0.1, "price": 1.1, "stop_loss": 1.09,
        "take_profit": 1.12, "order_type": 2,
        "event_time": "2026-08-27T21:00:00Z",
    }]


def test_full_close_wins_over_stale_position_snapshot_and_keeps_economics():
    position = {
        "positions": {
            "1001": {
                "ticket": "1001",
                "symbol": "USDCAD",
                "direction": "sell",
                "volume": 2.79,
            }
        },
        "orders": {},
        "deals": {},
    }
    record = {
        "event_type": "DEAL_ADD",
        "position_id": "1001",
        "deal_id": "9001",
        "symbol": "USDCAD",
        "direction": "buy",
        "volume": 2.79,
        "price": 1.37815,
        "entry": "OUT",
        "profit": -27.9,
        "commission": -7.25,
        "swap": 0.0,
        "time": "2026-09-09T12:58:34Z",
    }

    event = _mql5_file_event(record, position, position)

    assert event is not None
    assert event["event_type"] == "trade_closed"
    assert event["close_price"] == 1.37815
    assert event["profit"] == -27.9
    assert event["commission"] == -7.25
    assert event["swap"] == 0.0


def test_partial_close_uses_deal_volume_when_position_snapshot_is_stale():
    position = {
        "positions": {
            "1001": {
                "ticket": "1001",
                "symbol": "EURUSD",
                "direction": "buy",
                "volume": 1.0,
            }
        },
        "orders": {},
        "deals": {},
    }
    record = {
        "event_type": "DEAL_ADD",
        "position_id": "1001",
        "deal_id": "9002",
        "symbol": "EURUSD",
        "direction": "sell",
        "volume": 0.4,
        "price": 1.1,
        "entry": "OUT_BY",
        "profit": 12.0,
        "commission": -0.5,
        "swap": -0.1,
        "time": "2026-09-09T13:00:00Z",
    }

    event = _mql5_file_event(record, position, position)

    assert event is not None
    assert event["event_type"] == "trade_volume_changed"
    assert event["previous_volume"] == 1.0
    assert event["volume"] == 0.6
    assert event["partial_close"] is True
    assert event["close_price"] == 1.1
    assert event["profit"] == 12.0
    assert event["commission"] == -0.5
    assert event["swap"] == -0.1
