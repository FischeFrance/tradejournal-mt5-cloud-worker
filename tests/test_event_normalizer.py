from event_normalizer import build_event_id, normalize_event

RAW_TRADE_OPENED = {
    "event_type": "trade_opened",
    "ticket": "1",
    "symbol": "EURUSD",
    "direction": "buy",
    "volume": 0.1,
    "open_price": 1.1000,
    "stop_loss": 1.0950,
    "take_profit": 1.1100,
    "open_time": "2026-01-01T00:00:00+00:00",
    "event_time": "2026-01-01T00:00:00+00:00",
}


def test_normalize_event_produces_all_contract_fields():
    payload = normalize_event(RAW_TRADE_OPENED, account_number="12345", server="Demo-Server")

    expected_keys = {
        "event_id", "event_type", "platform", "account_number", "server", "external_trade_id",
        "symbol", "direction", "volume", "price", "open_price", "close_price", "stop_loss",
        "take_profit", "previous_stop_loss", "previous_take_profit", "profit", "commission",
        "swap", "open_time", "close_time", "event_time",
    }
    assert set(payload.keys()) == expected_keys
    assert payload["event_type"] == "trade_opened"
    assert payload["platform"] == "mt5"
    assert payload["account_number"] == "12345"
    assert payload["server"] == "Demo-Server"
    assert payload["external_trade_id"] == "1"
    assert payload["symbol"] == "EURUSD"
    assert payload["stop_loss"] == 1.0950
    assert payload["take_profit"] == 1.1100


def test_normalize_event_accepts_missing_account_number():
    payload = normalize_event(RAW_TRADE_OPENED, account_number=None, server=None)

    assert payload["account_number"] is None
    assert payload["server"] is None
    assert payload["event_id"].startswith("mt5-unknown-")


def test_normalize_event_fills_event_time_when_missing():
    raw = {**RAW_TRADE_OPENED}
    del raw["event_time"]

    payload = normalize_event(raw, account_number="12345", server="Demo-Server")

    assert payload["event_time"]


def test_event_id_is_deterministic_for_identical_events():
    id_1 = build_event_id("12345", RAW_TRADE_OPENED)
    id_2 = build_event_id("12345", RAW_TRADE_OPENED)

    assert id_1 == id_2


def test_event_id_changes_when_account_number_changes():
    id_1 = build_event_id("12345", RAW_TRADE_OPENED)
    id_2 = build_event_id("67890", RAW_TRADE_OPENED)

    assert id_1 != id_2


def test_event_id_changes_when_stop_loss_value_changes():
    modified_1 = {
        "event_type": "trade_modified", "ticket": "1", "stop_loss": 1.0975, "take_profit": 1.1100,
        "previous_stop_loss": 1.0950, "previous_take_profit": 1.1100,
    }
    modified_2 = {**modified_1, "stop_loss": 1.0980}

    id_1 = build_event_id("12345", modified_1)
    id_2 = build_event_id("12345", modified_2)

    assert id_1 != id_2


def test_event_id_is_stable_regardless_of_unrelated_field_order():
    reordered = dict(reversed(list(RAW_TRADE_OPENED.items())))

    assert build_event_id("12345", RAW_TRADE_OPENED) == build_event_id("12345", reordered)


def test_event_id_differs_across_event_types_for_same_ticket():
    opened = {"event_type": "trade_opened", "ticket": "1", "symbol": "EURUSD", "direction": "buy",
              "volume": 0.1, "open_price": 1.1, "stop_loss": 1.09, "take_profit": 1.11, "open_time": "t"}
    closed = {"event_type": "trade_closed", "ticket": "1", "close_price": 1.11, "profit": 10.0,
              "commission": -0.5, "swap": -0.1, "close_time": "t2"}

    assert build_event_id("12345", opened) != build_event_id("12345", closed)


def test_normalize_all_seven_event_types_yields_valid_payload():
    raw_events = [
        {"event_type": "trade_opened", "ticket": "1", "symbol": "EURUSD", "direction": "buy",
         "volume": 0.1, "open_price": 1.1, "stop_loss": 1.09, "take_profit": 1.11,
         "open_time": "2026-01-01T00:00:00+00:00", "event_time": "2026-01-01T00:00:00+00:00"},
        {"event_type": "trade_modified", "ticket": "1", "stop_loss": 1.095, "take_profit": 1.11,
         "previous_stop_loss": 1.09, "previous_take_profit": 1.11,
         "event_time": "2026-01-01T00:05:00+00:00"},
        {"event_type": "trade_modified", "ticket": "1", "stop_loss": 1.095, "take_profit": 1.12,
         "previous_stop_loss": 1.095, "previous_take_profit": 1.11,
         "event_time": "2026-01-01T00:10:00+00:00"},
        {"event_type": "trade_closed", "ticket": "1", "close_price": 1.12, "profit": 20.0,
         "commission": -0.5, "swap": -0.1, "close_time": "2026-01-01T01:00:00+00:00",
         "event_time": "2026-01-01T01:00:00+00:00"},
        {"event_type": "pending_order_created", "ticket": "2", "symbol": "EURUSD", "direction": "buy",
         "volume": 0.05, "price": 1.09, "stop_loss": 1.085, "take_profit": 1.10,
         "event_time": "2026-01-01T02:00:00+00:00"},
        {"event_type": "pending_order_modified", "ticket": "2", "price": 1.09, "stop_loss": 1.087,
         "take_profit": 1.10, "previous_stop_loss": 1.085, "previous_take_profit": 1.10,
         "event_time": "2026-01-01T02:05:00+00:00"},
        {"event_type": "pending_order_cancelled", "ticket": "2", "symbol": "EURUSD", "direction": "buy",
         "volume": 0.05, "price": 1.09, "event_time": "2026-01-01T02:10:00+00:00"},
    ]

    payloads = [normalize_event(e, account_number="12345", server="Demo-Server") for e in raw_events]

    assert [p["event_type"] for p in payloads] == [
        "trade_opened", "trade_modified", "trade_modified", "trade_closed",
        "pending_order_created", "pending_order_modified", "pending_order_cancelled",
    ]
    assert len({p["event_id"] for p in payloads}) == len(payloads), "ogni evento deve avere un event_id univoco"
    assert all(p["event_id"] for p in payloads)
