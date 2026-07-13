"""Mapping read-only del bridge Windows, senza Wine/terminale MT5 reali."""

from __future__ import annotations

import threading
import time
from datetime import timedelta
from types import SimpleNamespace

import pytest
import requests
from common import BridgeConfig, BridgeError
from windows.mt5_bridge import _Mt5Session, make_server


class FakeMt5:
    DEAL_ENTRY_OUT = 1
    DEAL_ENTRY_OUT_BY = 3

    def __init__(self) -> None:
        self.account = SimpleNamespace(
            login=123456,
            server="Broker-Demo",
            balance=10000.0,
            equity=10010.0,
            currency="EUR",
            leverage=100,
        )
        self.positions = [
            SimpleNamespace(
                ticket=10001,
                symbol="EURUSD",
                type=0,
                volume=0.1,
                price_open=1.17,
                sl=1.168,
                tp=1.174,
                time=1783936800,
            ),
            SimpleNamespace(
                ticket=10002,
                symbol="GBPUSD",
                type=1,
                volume=0.2,
                price_open=1.25,
                sl=1.24,
                tp=1.27,
                time=1783936860,
            ),
        ]
        self.orders = [
            SimpleNamespace(
                ticket=20001,
                symbol="EURUSD",
                type=2,
                volume_current=0.1,
                price_open=1.168,
                sl=1.166,
                tp=1.172,
            ),
            SimpleNamespace(
                ticket=20002,
                symbol="EURUSD",
                type=3,
                volume_current=0.2,
                price_open=1.18,
                sl=1.19,
                tp=1.16,
            ),
        ]
        self.deals = [
            self._deal(30000, 0),
            self._deal(30001, self.DEAL_ENTRY_OUT),
            self._deal(30002, self.DEAL_ENTRY_OUT_BY),
            self._deal(30003, 2),
        ]
        self.calls = {"account": 0, "positions": 0, "orders": 0, "deals": 0}
        self.history_range = None

    @staticmethod
    def _deal(ticket, entry):
        return SimpleNamespace(
            ticket=ticket,
            position_id=10001,
            price=1.172,
            profit=20.0,
            commission=-0.5,
            swap=0.0,
            time=1783937700,
            entry=entry,
        )

    def account_info(self):
        self.calls["account"] += 1
        return self.account

    def positions_get(self):
        self.calls["positions"] += 1
        return self.positions

    def orders_get(self):
        self.calls["orders"] += 1
        return self.orders

    def history_deals_get(self, date_from, date_to):
        self.calls["deals"] += 1
        self.history_range = (date_from, date_to)
        return self.deals

    @staticmethod
    def last_error():
        return 500, "errore MT5 di test"


def _connected_session(fake_mt5: FakeMt5) -> _Mt5Session:
    session = _Mt5Session(SimpleNamespace(), sleep_fn=lambda _seconds: None)
    session._mt5 = fake_mt5
    session._connected = True
    return session


def test_real_bridge_maps_complete_trading_snapshot_with_utc_and_string_tickets():
    fake = FakeMt5()
    snapshot = _connected_session(fake).get_trading_snapshot(24)

    assert snapshot["account"] == {
        "login": "123456",
        "server": "Broker-Demo",
        "balance": 10000.0,
        "equity": 10010.0,
        "currency": "EUR",
        "leverage": 100,
    }
    assert snapshot["positions"][0] == {
        "ticket": "10001",
        "symbol": "EURUSD",
        "direction": "buy",
        "volume": 0.1,
        "open_price": 1.17,
        "stop_loss": 1.168,
        "take_profit": 1.174,
        "open_time": "2026-07-13T10:00:00Z",
    }
    assert snapshot["positions"][1]["direction"] == "sell"
    assert snapshot["positions"][1]["ticket"] == "10002"
    assert snapshot["orders"][0]["direction"] == "buy"
    assert snapshot["orders"][0]["order_type"] == 2
    assert snapshot["orders"][1]["direction"] == "sell"
    assert snapshot["orders"][1]["ticket"] == "20002"

    # Solo OUT e OUT_BY; ingresso (0) e INOUT (2) non rappresentano chiusure nel contratto.
    assert [deal["deal_ticket"] for deal in snapshot["deals"]] == ["30001", "30002"]
    assert all(deal["position_ticket"] == "10001" for deal in snapshot["deals"])
    assert all(deal["close_time"].endswith("Z") for deal in snapshot["deals"])
    assert snapshot["generated_at"].endswith("Z")
    assert fake.calls == {"account": 1, "positions": 1, "orders": 1, "deals": 1}

    date_from, date_to = fake.history_range
    assert date_from.utcoffset() == timedelta(0)
    assert date_to.utcoffset() == timedelta(0)
    assert date_to - date_from == timedelta(hours=24)


def test_real_bridge_always_returns_arrays_when_mt5_collections_are_empty():
    fake = FakeMt5()
    fake.positions = []
    fake.orders = []
    fake.deals = []
    snapshot = _connected_session(fake).get_trading_snapshot(1)
    assert snapshot["positions"] == []
    assert snapshot["orders"] == []
    assert snapshot["deals"] == []


@pytest.mark.parametrize(
    ("attribute", "expected_description"),
    [
        ("account", "account_info"),
        ("positions", "positions_get"),
        ("orders", "orders_get"),
        ("deals", "history_deals_get"),
    ],
)
def test_real_bridge_none_result_becomes_structured_mt5_error(attribute, expected_description):
    fake = FakeMt5()
    setattr(fake, attribute, None)
    session = _connected_session(fake)

    with pytest.raises(BridgeError) as exc_info:
        session.get_trading_snapshot(24)

    assert exc_info.value.status == 502
    assert exc_info.value.code == "mt5_error"
    assert expected_description in exc_info.value.message
    assert "errore MT5 di test" in exc_info.value.message


def test_real_bridge_serializes_complete_snapshot_operations_across_http_threads():
    fake = FakeMt5()
    state_lock = threading.Lock()
    active = 0
    max_active = 0
    original_account_info = fake.account_info

    def slow_account_info():
        nonlocal active, max_active
        with state_lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.03)
        try:
            return original_account_info()
        finally:
            with state_lock:
                active -= 1

    fake.account_info = slow_account_info
    session = _connected_session(fake)
    errors = []

    def fetch():
        try:
            session.get_trading_snapshot(24)
        except Exception as exc:  # pragma: no cover - raccolta diagnostica per il thread
            errors.append(exc)

    threads = [threading.Thread(target=fetch), threading.Thread(target=fetch)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert errors == []
    assert max_active == 1


def test_windows_http_handler_exposes_snapshot_and_passes_clamped_lookback():
    class StubSession:
        def __init__(self):
            self.lookbacks = []

        def get_trading_snapshot(self, lookback):
            self.lookbacks.append(lookback)
            return {
                "account": {
                    "login": "1", "server": "Demo", "balance": 1.0, "equity": 1.0,
                    "currency": "EUR", "leverage": 100,
                },
                "positions": [], "orders": [], "deals": [],
                "generated_at": "2026-07-13T10:00:00Z",
            }

    session = StubSession()
    config = BridgeConfig(token="windows-handler-token", broker_symbol="EURUSD", host="127.0.0.1", port=0)
    server = make_server(config, session)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}/v1/trading/snapshot"
    headers = {"Authorization": "Bearer windows-handler-token"}
    try:
        assert requests.post(url, json={}, headers=headers, timeout=5).status_code == 200
        assert requests.post(
            url, json={"deal_lookback_hours": 1000}, headers=headers, timeout=5
        ).status_code == 200
        invalid = requests.post(url, json={"deal_lookback_hours": 0}, headers=headers, timeout=5)
        assert invalid.status_code == 422
        assert invalid.json()["error"]["code"] == "invalid_deal_lookback_hours"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert session.lookbacks == [24, 168]
