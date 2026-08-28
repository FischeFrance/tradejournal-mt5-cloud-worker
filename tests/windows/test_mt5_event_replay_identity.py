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
