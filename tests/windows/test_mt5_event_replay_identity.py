from __future__ import annotations

import pytest

from windows_agent.worker.live_sync import (
    CertifiedHistoryRecoveryRequired,
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
    assert event["event_type"] == "trade_partial_closed"
    assert event["previous_volume"] == 1.0
    assert event["volume"] == 0.4
    assert event["partial_close"] is True
    assert event["close_price"] == 1.1
    assert event["profit"] == 12.0
    assert event["commission"] == -0.5
    assert event["swap"] == -0.1


def test_native_batch_replays_scale_in_then_partial_and_final_close_sequentially():
    previous = {
        "positions": {},
        "orders": {},
        "deals": {},
    }
    # The current snapshot is intentionally empty: every lifecycle transition
    # below has to be inferred from native per-position volume, not by mapping
    # each DEAL_ADD against the same before/after pair.
    current = {"positions": {}, "orders": {}, "deals": {}}
    prefix = "00000000-0000-4000-8000-000000000001|42|Demo|DEAL_ADD"
    records = (
        {
            "event_id": f"{prefix}|1|1000|EURUSD", "event_type": "DEAL_ADD",
            "ticket": "1", "deal_id": "1", "position_id": "77", "symbol": "EURUSD",
            "direction": "buy", "volume": 0.4, "price": 1.1, "entry": "IN",
            "profit": 0.0, "commission": -0.1, "fee": -0.1, "swap": 0.0,
            "time": "2026-09-24T10:00:00Z",
        },
        {
            "event_id": f"{prefix}|2|2000|EURUSD", "event_type": "DEAL_ADD",
            "ticket": "2", "deal_id": "2", "position_id": "77", "symbol": "EURUSD",
            "direction": "buy", "volume": 0.6, "price": 1.2, "entry": "IN",
            "profit": 0.0, "commission": -0.2, "fee": -0.1, "swap": 0.0,
            "time": "2026-09-24T10:01:00Z",
        },
        {
            "event_id": f"{prefix}|3|3000|EURUSD", "event_type": "DEAL_ADD",
            "ticket": "3", "deal_id": "3", "position_id": "77", "symbol": "EURUSD",
            "direction": "sell", "volume": 0.4, "price": 1.25, "entry": "OUT",
            "profit": 4.0, "commission": -0.4, "fee": -0.1, "swap": -0.2,
            "time": "2026-09-24T10:02:00Z",
        },
        {
            "event_id": f"{prefix}|4|4000|EURUSD", "event_type": "DEAL_ADD",
            "ticket": "4", "deal_id": "4", "position_id": "77", "symbol": "EURUSD",
            "direction": "sell", "volume": 0.6, "price": 1.3, "entry": "OUT",
            "profit": 6.0, "commission": -0.6, "fee": -0.1, "swap": -0.3,
            "time": "2026-09-24T10:03:00Z",
        },
    )

    events = _merge_event_stream_with_snapshot(records, previous, current)

    assert [event["event_type"] for event in events] == [
        "trade_opened",
        "trade_volume_changed",
        "trade_partial_closed",
        "trade_closed",
    ]
    assert events[1]["previous_volume"] == 0.4
    assert events[1]["volume"] == 1.0
    assert events[2]["previous_volume"] == 1.0
    assert events[2]["volume"] == 0.4
    assert events[2]["partial_close"] is True
    assert events[3]["ticket"] == "77"
    # The native OUT is explicitly a partial close so downstream balance/P&L
    # reconciliation accepts its economics even though MT5 reports `sell` for
    # a reduction of a buy position. Verify the complete per-fill payload.
    partial = normalize_event(events[2], "42", "Demo")
    final = normalize_event(events[3], "42", "Demo")
    assert partial["external_trade_id"] == "77"
    assert partial["event_type"] == "trade_partial_closed"
    assert partial["partial_close"] is True
    assert partial["volume"] == 0.4
    assert partial["close_price"] == 1.25
    assert partial["profit"] == 4.0
    assert partial["commission"] == -0.5
    assert partial["fee"] == -0.1
    assert partial["swap"] == -0.2
    assert partial["event_id"] != final["event_id"]
    assert final["event_type"] == "trade_closed"
    assert final["profit"] == 6.0
    assert final["commission"] == -0.7
    payloads = [normalize_event(event, "42", "Demo") for event in events]
    assert {payload["external_trade_id"] for payload in payloads} == {"77"}
    assert [payload["event_type"] for payload in payloads] == [
        "trade_opened",
        "trade_volume_changed",
        "trade_partial_closed",
        "trade_closed",
    ]
    # The two economic close legs and opening/scale-in costs are all retained
    # under one stable trade id. Fees are folded into commission once, while
    # the audit fee remains available separately for every native fill.
    assert abs(
        sum(
            float(payload.get("profit") or 0)
            + float(payload.get("commission") or 0)
            + float(payload.get("swap") or 0)
            for payload in payloads
        )
        - 7.8
    ) < 1e-12
    assert [payload.get("fee") for payload in payloads] == [-0.1, -0.1, -0.1, -0.1]


def test_native_partial_close_with_invalid_closed_volume_is_not_ackable():
    record = {
        "event_id": "00000000-0000-4000-8000-000000000001|42|Demo|DEAL_ADD|3|3000|EURUSD",
        "event_type": "DEAL_ADD",
        "ticket": "3",
        "deal_id": "3",
        "position_id": "77",
        "symbol": "EURUSD",
        "direction": "sell",
        "volume": "nan",
        "price": 1.25,
        "entry": "OUT",
        "time": "2026-09-24T10:02:00Z",
    }
    with pytest.raises(CertifiedHistoryRecoveryRequired, match="partial close volume"):
        _merge_event_stream_with_snapshot(
            (record,),
            {"positions": {"77": {"volume": 1.0}}, "orders": {}, "deals": {}},
            {"positions": {"77": {"volume": 0.6}}, "orders": {}, "deals": {}},
        )


def test_position_reduction_without_native_out_requires_certified_history():
    previous = {
        "positions": {"1001": {"volume": 1.0, "symbol": "EURUSD", "direction": "buy"}},
        "orders": {}, "deals": {},
    }
    current = {
        "positions": {"1001": {"volume": 0.4, "symbol": "EURUSD", "direction": "buy"}},
        "orders": {}, "deals": {},
    }
    position_only = ({
        "event_type": "POSITION", "position_id": "1001", "symbol": "EURUSD",
        "direction": "buy", "volume": 0.4,
    },)

    with pytest.raises(CertifiedHistoryRecoveryRequired, match="position reduction"):
        _merge_event_stream_with_snapshot(position_only, previous, current)
