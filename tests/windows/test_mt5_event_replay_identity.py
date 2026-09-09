from __future__ import annotations

from windows_agent.worker.live_sync import _mql5_file_event
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
