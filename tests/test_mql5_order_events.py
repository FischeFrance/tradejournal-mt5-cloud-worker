from windows_agent.worker.live_sync import _mql5_file_event
from worker.event_normalizer import build_event_id, normalize_event

LIMIT_ORDER = {
    "ticket": "616341652",
    "order_id": "616341652",
    "symbol": "DAX40",
    "direction": "buy",
    "volume": 1.0,
    "price": 25314.15,
    "stop_loss": 25280.0,
    "take_profit": 25400.0,
    "time": "2026-10-14T08:04:00Z",
}


def test_bridge_order_type_and_state_reach_the_event() -> None:
    created = _mql5_file_event(
        {**LIMIT_ORDER, "event_type": "ORDER_ADD", "order_type": 2, "order_state": 1},
        previous={"positions": {}, "orders": {}},
        current={"positions": {}, "orders": {}},
    )
    filled = _mql5_file_event(
        {**LIMIT_ORDER, "event_type": "HISTORY_ADD", "order_type": 2, "order_state": 4},
        previous={"positions": {}, "orders": {}},
        current={"positions": {}, "orders": {}},
    )
    cancelled = _mql5_file_event(
        {**LIMIT_ORDER, "event_type": "ORDER_DELETE", "order_type": 2, "order_state": 2},
        previous={"positions": {}, "orders": {}},
        current={"positions": {}, "orders": {}},
    )

    assert created["event_type"] == "pending_order_created"
    assert created["order_type"] == 2
    assert filled["event_type"] == "pending_order_filled"
    assert cancelled["event_type"] == "pending_order_cancelled"


def test_an_older_bridge_borrows_the_order_type_from_the_orders_snapshot() -> None:
    snapshot = {"positions": {}, "orders": {"616341652": {"ticket": "616341652", "order_type": 2}}}

    created = _mql5_file_event({**LIMIT_ORDER, "event_type": "ORDER_ADD"}, previous={"positions": {}, "orders": {}}, current=snapshot)
    deleted = _mql5_file_event({**LIMIT_ORDER, "event_type": "ORDER_DELETE"}, previous=snapshot, current={"positions": {}, "orders": {}})

    assert created["order_type"] == 2
    assert deleted["order_type"] == 2
    # Without ORDER_STATE an older bridge cannot prove a fill: the historical meaning stays.
    assert deleted["event_type"] == "pending_order_cancelled"


def test_a_market_execution_stays_untyped_when_nothing_names_its_order_type() -> None:
    event = _mql5_file_event(
        {**LIMIT_ORDER, "event_type": "ORDER_ADD"},
        previous={"positions": {}, "orders": {}},
        current={"positions": {}, "orders": {}},
    )

    assert "order_type" not in event


def test_order_type_is_forwarded_without_changing_the_event_id() -> None:
    raw = {
        "event_type": "pending_order_created",
        "ticket": "616341652",
        "symbol": "DAX40",
        "direction": "buy",
        "volume": 1.0,
        "price": 25314.15,
        "stop_loss": 25280.0,
        "take_profit": 25400.0,
        "event_time": "2026-10-14T08:04:00+00:00",
    }

    typed = normalize_event({**raw, "order_type": 2}, account_number="12345", server="Demo")
    plain = normalize_event(raw, account_number="12345", server="Demo")

    assert typed["order_type"] == "2"
    assert "order_type" not in plain
    assert typed["event_id"] == plain["event_id"]


def test_a_fill_and_a_cancellation_of_the_same_order_get_different_ids() -> None:
    base = {"ticket": "616341652", "symbol": "DAX40", "direction": "buy", "volume": 1.0, "price": 25314.15}

    assert build_event_id("12345", {**base, "event_type": "pending_order_filled"}) != build_event_id(
        "12345", {**base, "event_type": "pending_order_cancelled"}
    )
