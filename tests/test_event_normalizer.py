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
        "total_commission", "swap", "open_time", "close_time", "event_time",
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


def test_normalize_closed_trade_preserves_authoritative_total_commission():
    payload = normalize_event(
        {
            "event_type": "trade_closed",
            "ticket": "1",
            "commission": 0,
            "total_commission": -5.44,
            "event_time": "2026-01-01T02:00:00+00:00",
        },
        account_number="12345",
        server="Demo-Server",
    )

    assert payload["commission"] == 0
    assert payload["total_commission"] == -5.44


def test_normalize_event_accepts_missing_account_number():
    payload = normalize_event(RAW_TRADE_OPENED, account_number=None, server=None)

    assert payload["account_number"] is None
    assert payload["server"] is None
    assert payload["event_id"].startswith("mt5-unknown-")


def test_normalize_event_forwards_complete_account_reference_for_new_trade():
    payload = normalize_event(
        {
            **RAW_TRADE_OPENED,
            "balance": 9_999.25,
            "equity": 10_001.5,
            "currency": "EUR",
            "leverage": 100,
            "balance_before_open": 10_000.0,
        },
        account_number="12345",
        server="Demo-Server",
    )

    assert payload["balance"] == 9_999.25
    assert payload["equity"] == 10_001.5
    assert payload["currency"] == "EUR"
    assert payload["leverage"] == 100
    assert payload["balance_before_open"] == 10_000.0


def test_normalize_event_does_not_emit_a_partial_account_snapshot():
    payload = normalize_event(
        {**RAW_TRADE_OPENED, "balance": 10_000.0},
        account_number="12345",
        server="Demo-Server",
    )

    assert "balance" not in payload
    assert "equity" not in payload
    assert "currency" not in payload
    assert "leverage" not in payload


def test_normalize_event_keeps_native_fee_as_audit_only_field():
    payload = normalize_event(
        {**RAW_TRADE_OPENED, "commission": -1.25, "fee": -0.15},
        account_number="12345",
        server="Demo-Server",
    )

    assert payload["commission"] == -1.25
    assert payload["fee"] == -0.15


def test_normalize_event_fills_event_time_when_missing():
    raw = {**RAW_TRADE_OPENED}
    del raw["event_time"]

    payload = normalize_event(raw, account_number="12345", server="Demo-Server")

    assert payload["event_time"]


def test_normalize_close_forwards_final_excursion_summary_without_samples():
    payload = normalize_event(
        {
            "event_type": "trade_closed",
            "ticket": "1",
            "close_price": 1.105,
            "profit": 50,
            "close_time": "2026-09-16T12:00:00Z",
            "mae_points": 35,
            "mfe_points": 80,
            "mae_pct": 0.25,
            "mfe_pct": 0.75,
            "excursion_samples": 42,
            "excursion_sample_ms": 2000,
            "excursion_source": "mt5_snapshot",
        },
        account_number="12345",
        server="Demo-Server",
    )

    assert payload["mae_points"] == 35
    assert payload["mfe_points"] == 80
    assert payload["mae_pct"] == 0.25
    assert payload["mfe_pct"] == 0.75
    assert payload["excursion_samples"] == 42
    assert payload["excursion_sample_ms"] == 2000
    assert payload["excursion_source"] == "mt5_snapshot"


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


def test_distinct_partial_fills_have_distinct_ids_and_retries_are_stable():
    partial = {
        "event_type": "trade_partial_closed",
        "ticket": "position-1",
        "native_deal_ticket": "1001",
        "time_msc": 1_767_225_600_000,
        "time_basis": "broker_server_unresolved",
        "volume": 0.5,
        "close_price": 1.11,
        "profit": 10,
        "commission": -1,
        "swap": 0,
        "close_time": "2026-01-01T00:00:00Z",
    }
    other_fill = {**partial, "native_deal_ticket": "1002"}

    first_id = build_event_id("12345", partial)
    assert first_id == build_event_id("12345", dict(partial))
    assert first_id != build_event_id("12345", other_fill)

    payload = normalize_event(partial, account_number="12345", server="Demo-Server")
    assert payload["native_deal_ticket"] == "1001"
    assert payload["time_msc"] == 1_767_225_600_000
    assert payload["time_basis"] == "broker_server_unresolved"


def test_native_close_identity_is_stable_when_broker_economics_are_enriched():
    close = {
        "event_type": "trade_closed",
        "ticket": "position-1",
        "native_deal_ticket": "1003",
        "time_msc": 1_767_225_600_000,
        "volume": 0.5,
        "close_price": 1.11,
        "profit": 10.0,
        "commission": 0.0,
        "swap": 0.0,
        "close_time": "2026-01-01T00:00:00Z",
    }
    enriched = {
        **close,
        "profit": 9.75,
        "commission": -1.25,
        "swap": -0.10,
    }

    assert build_event_id("12345", close) == build_event_id("12345", enriched)


def test_native_scale_in_identity_is_stable_when_history_enriches_projection():
    scale_in = {
        "event_type": "trade_volume_changed",
        "ticket": "position-1",
        "native_deal_ticket": "1000",
        "volume": 0.5,
        "previous_volume": 0.25,
        "partial_close": False,
        "open_price": 1.10,
        "commission": -1,
    }
    enriched = {**scale_in, "open_price": 1.105, "commission": -1.25}

    assert build_event_id("12345", scale_in) == build_event_id("12345", enriched)


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
