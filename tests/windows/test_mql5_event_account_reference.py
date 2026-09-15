from windows_agent.worker.live_sync import _mql5_file_event


def test_open_event_keeps_the_balance_captured_before_the_trade() -> None:
    event = _mql5_file_event(
        {
            "event_type": "DEAL_ADD",
            "position_id": "position-1",
            "ticket": "deal-1",
            "entry": "IN",
            "symbol": "EURUSD",
            "direction": "buy",
            "volume": 0.5,
            "price": 1.1,
            "time": "2026-09-15T10:00:00Z",
            "balance": 9_998.5,
            "equity": 9_997.0,
            "currency": "EUR",
            "leverage": 100,
            "balance_before_open": 10_000.0,
        },
        previous={"positions": {}},
        current={"positions": {"position-1": {"volume": 0.5}}},
    )

    assert event["event_type"] == "trade_opened"
    assert event["balance"] == 9_998.5
    assert event["equity"] == 9_997.0
    assert event["currency"] == "EUR"
    assert event["leverage"] == 100
    assert event["balance_before_open"] == 10_000.0
