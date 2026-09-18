import pytest

from windows_agent.real_handlers import _history_event
from windows_agent.worker.history_balance import reconstruct_trade_deals
from windows_agent.worker.live_sync import _mql5_file_event
from worker.event_normalizer import normalize_event


def _opening(ticket, moment, volume, price, commission):
    return {
        "ticket": str(ticket),
        "order_id": str(ticket + 100),
        "position_id": "position-1",
        "symbol": "EURUSD",
        "direction": "buy",
        "deal_type": 0,
        "entry": "IN",
        "volume": volume,
        "price": price,
        "profit": 0.0,
        "commission": commission,
        "swap": 0.0,
        "fee": 0.0,
        "time_msc": moment,
        "time": f"2026-01-01T00:00:0{ticket}Z",
    }


def test_scale_in_emits_native_opening_fills_even_when_balance_is_nd():
    projected = reconstruct_trade_deals(
        [
            _opening(1, 1_000, 0.1, 1.1, -1),
            _opening(2, 2_000, 0.2, 1.2, -2),
        ],
        {"balance": 997, "credit": 0, "coherent": False},
    )
    remote_rows = [row for row in projected if row["project_as_trade"]]
    events = [
        _history_event({"kind": "deals", "record": row}, "42", "Demo")
        for row in remote_rows
    ]

    assert len(events) == 2
    assert events[0]["event_type"] == "trade_opened"
    assert events[0]["volume"] == 0.1
    assert events[0]["open_price"] == 1.1
    assert events[0]["commission"] == -1
    assert events[0]["fee"] == 0
    assert "balance_before_open" not in events[0]
    assert events[1]["event_type"] == "trade_volume_changed"
    assert events[1]["previous_volume"] == 0.1
    assert events[1]["volume"] == pytest.approx(0.3)
    assert events[1]["open_price"] == pytest.approx(1.1666666666666667)
    assert events[1]["commission"] == -2
    assert events[1]["native_deal_ticket"] == "2"


def test_partial_and_final_fills_keep_position_identity_but_distinct_deal_identity():
    base_msc = 1_767_225_600_000
    rows = [
        _opening(101, base_msc, 1, 1.1, -2),
        {
            **_opening(102, base_msc + 1_000, 0.4, 1.11, -1),
            "deal_type": 1,
            "entry": "OUT",
            "direction": "sell",
            "profit": 40.0,
        },
        {
            **_opening(103, base_msc + 2_000, 0.6, 1.12, -1),
            "deal_type": 1,
            "entry": "OUT",
            "direction": "sell",
            "profit": 60,
        },
    ]
    projected = reconstruct_trade_deals(
        rows, {"balance": 1_096, "credit": 0, "coherent": True}
    )
    events = [
        _history_event({"kind": "deals", "record": row}, "42", "Demo")
        for row in projected
        if row["project_as_trade"]
    ]

    partial, final = events[1], events[2]
    assert partial["event_type"] == "trade_partial_closed"
    assert final["event_type"] == "trade_closed"
    assert partial["external_trade_id"] == final["external_trade_id"] == "position-1"
    assert partial["native_deal_ticket"] == "102"
    assert final["native_deal_ticket"] == "103"
    assert partial["time_msc"] == base_msc + 1_000
    assert partial["time_basis"] == "broker_server_unresolved"
    assert partial["event_id"] != final["event_id"]
    assert final["commission"] == -1
    assert final["total_commission"] == -4
    assert final["commission_complete"] is True
    assert partial == _history_event(
        {"kind": "deals", "record": projected[1]}, "42", "Demo"
    )


def test_live_scale_in_and_history_replay_have_the_same_event_identity():
    moment = 1_767_225_601_000
    rows = [
        _opening(3, moment - 1_000, 0.1, 1.1, -1),
        _opening(4, moment, 0.2, 1.2, -2),
    ]
    projected = reconstruct_trade_deals(
        rows, {"balance": 997, "credit": 0, "coherent": True}
    )
    history_payload = _history_event(
        {"kind": "deals", "record": projected[1]}, "42", "Demo"
    )
    live_payload = normalize_event(
        _mql5_file_event(
            {
                "event_type": "DEAL_ADD",
                "ticket": "4",
                "deal_id": "4",
                "position_id": "position-1",
                "entry": "IN",
                "symbol": "EURUSD",
                "direction": "buy",
                "volume": 0.2,
                "price": 1.2,
                "profit": 0.0,
                "commission": -2.0,
                "swap": 0.0,
                "time": "2026-01-01T00:00:01Z",
                "timestamp_msc": moment,
            },
            previous={"positions": {"position-1": {"volume": 0.1, "open_price": 1.1}}},
            current={"positions": {"position-1": {"volume": 0.3, "open_price": 1.1666666666666667}}},
        ),
        "42",
        "Demo",
    )

    assert history_payload["event_type"] == live_payload["event_type"] == "trade_volume_changed"
    assert history_payload["event_id"] == live_payload["event_id"]
    assert history_payload["commission"] == live_payload["commission"] == -2
    assert history_payload["open_price"] == pytest.approx(live_payload["open_price"])


def test_live_partial_fill_and_history_replay_have_the_same_event_identity():
    moment = 1_767_225_601_000
    close_time = "2026-01-01T00:00:01Z"
    rows = [
        {
            **_opening(201, moment - 1_000, 1, 1.1, -2),
            "position_id": "position-2",
        },
        {
            **_opening(202, moment, 0.4, 1.11, -0.75),
            "position_id": "position-2",
            "deal_type": 1,
            "entry": "OUT",
            "direction": "sell",
            "profit": 40.0,
            "fee": -0.25,
            "time": close_time,
        },
    ]
    projected = reconstruct_trade_deals(
        rows, {"balance": 1_035, "credit": 0, "coherent": True}
    )
    history_payload = _history_event(
        {"kind": "deals", "record": projected[1]}, "42", "Demo"
    )
    live_raw = _mql5_file_event(
        {
            "event_type": "DEAL_ADD",
            "ticket": "202",
            "deal_id": "202",
            "position_id": "position-2",
            "entry": "OUT",
            "symbol": "EURUSD",
            "direction": "sell",
            "volume": 0.4,
            "price": 1.11,
            "profit": 40.0,
            # The EA already publishes DEAL_COMMISSION + DEAL_FEE here.
            "commission": -1.0,
            "swap": 0.0,
            "time": close_time,
            "timestamp_msc": moment,
        },
        previous={"positions": {"position-2": {"volume": 1}}},
        current={"positions": {"position-2": {"volume": 0.6}}},
    )
    live_payload = normalize_event(live_raw, "42", "Demo")

    assert live_payload["event_type"] == history_payload["event_type"]
    assert live_payload["event_id"] == history_payload["event_id"]
    assert live_payload["native_deal_ticket"] == history_payload["native_deal_ticket"]
