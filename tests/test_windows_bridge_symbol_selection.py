"""Selezione simboli del bridge Windows, senza Wine o terminale MT5 reali."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from common import BridgeError
from windows.mt5_bridge import _Mt5Session


_WINDOWS_TERMINAL_PATH = r"C:\Program Files\MetaTrader 5\terminal64.exe"


class FakeMt5Module:
    """Doppio minimale dell'API MT5 con tracciamento esplicito della selezione simboli."""

    TIMEFRAME_M1 = 1
    DEAL_ENTRY_OUT = 1
    DEAL_ENTRY_OUT_BY = 3

    def __init__(self) -> None:
        self.account = SimpleNamespace(
            login=123456,
            server="Broker-Demo",
            balance=10_000.0,
            equity=10_050.0,
            currency="EUR",
            leverage=100,
        )
        self.symbols = {}
        self.symbol_select_result = False
        self.rates = []

        self.initialize_calls = []
        self.login_calls = []
        self.account_info_calls = 0
        self.terminal_info_calls = 0
        self.symbol_info_calls = []
        self.symbol_select_calls = []
        self.copy_rates_from_pos_calls = []

    def initialize(self, **kwargs):
        self.initialize_calls.append(kwargs)
        return True

    def login(self, login_id, password=None, server=None):
        self.login_calls.append((login_id, password, server))
        return True

    def account_info(self):
        self.account_info_calls += 1
        return self.account

    def terminal_info(self):
        self.terminal_info_calls += 1
        return SimpleNamespace(connected=True)

    @staticmethod
    def version():
        return 5, 0, 5000, "13 Jul 2026"

    def symbol_info(self, symbol):
        self.symbol_info_calls.append(symbol)
        return self.symbols.get(symbol)

    def symbol_select(self, symbol, enabled):
        self.symbol_select_calls.append((symbol, enabled))
        return self.symbol_select_result

    def copy_rates_from_pos(self, symbol, timeframe, start_pos, count):
        self.copy_rates_from_pos_calls.append((symbol, timeframe, start_pos, count))
        return self.rates

    @staticmethod
    def positions_get():
        return []

    @staticmethod
    def orders_get():
        return []

    @staticmethod
    def history_deals_get(_date_from, _date_to):
        return []

    @staticmethod
    def last_error():
        return -1, "Terminal: Call failed (fake)"


def _config(session_mode: str) -> SimpleNamespace:
    return SimpleNamespace(
        token="fake-bridge-token",
        broker_symbol="EURUSD",
        mt5_session_mode=session_mode,
        mt5_login="123456" if session_mode == "login" else "",
        mt5_password="fake-investor-password" if session_mode == "login" else "",
        mt5_server="Broker-Demo" if session_mode == "login" else "",
        mt5_terminal_path=_WINDOWS_TERMINAL_PATH,
        mt5_expected_login="",
        mt5_expected_server="",
    )


def _connected_session(fake: FakeMt5Module, session_mode: str = "existing") -> _Mt5Session:
    session = _Mt5Session(
        _config(session_mode),
        sleep_fn=lambda _seconds: None,
        mt5_module=fake,
    )
    session.connect()
    return session


def _one_complete_rate(now: datetime) -> list[dict]:
    return [
        {
            "time": int(now.timestamp()) - 120,
            "open": 1.08,
            "high": 1.081,
            "low": 1.079,
            "close": 1.0805,
            "tick_volume": 42,
            "spread": 10,
        }
    ]


def test_existing_connect_does_not_probe_a_symbol_even_when_selection_would_fail():
    fake = FakeMt5Module()
    fake.symbol_select_result = False

    session = _connected_session(fake, "existing")

    assert session._connected is True
    assert fake.initialize_calls == [{"path": _WINDOWS_TERMINAL_PATH}]
    assert fake.account_info_calls == 1
    assert fake.login_calls == []
    assert fake.symbol_info_calls == []
    assert fake.symbol_select_calls == []


def test_login_connect_still_logs_in_without_probing_a_symbol():
    fake = FakeMt5Module()
    fake.symbol_select_result = False

    session = _connected_session(fake, "login")

    assert session._connected is True
    assert fake.login_calls == [(123456, "fake-investor-password", "Broker-Demo")]
    assert fake.account_info_calls == 1
    assert fake.symbol_info_calls == []
    assert fake.symbol_select_calls == []


def test_health_uses_only_terminal_and_account_state_without_selecting_a_symbol():
    fake = FakeMt5Module()
    session = _connected_session(fake)

    health = session.health()

    assert health["status"] == "ok"
    assert health["terminal_connected"] is True
    assert health["account_connected"] is True
    assert fake.terminal_info_calls == 1
    assert fake.account_info_calls == 2  # connect + health
    assert fake.symbol_info_calls == []
    assert fake.symbol_select_calls == []


def test_trading_snapshot_never_selects_a_symbol():
    fake = FakeMt5Module()
    session = _connected_session(fake)

    snapshot = session.get_trading_snapshot(24)

    assert snapshot["positions"] == []
    assert snapshot["orders"] == []
    assert snapshot["deals"] == []
    assert fake.symbol_info_calls == []
    assert fake.symbol_select_calls == []


def test_candles_checks_and_selects_only_the_exact_requested_symbol():
    requested_symbol = "EURUSD.a"
    now = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
    fake = FakeMt5Module()
    fake.symbols[requested_symbol] = SimpleNamespace(visible=False)
    fake.symbol_select_result = True
    fake.rates = _one_complete_rate(now)
    session = _connected_session(fake)

    candles = session.get_candles(requested_symbol, "M1", None, now, 10)

    assert len(candles) == 1
    assert fake.symbol_info_calls == [requested_symbol]
    assert fake.symbol_select_calls == [(requested_symbol, True)]
    assert fake.copy_rates_from_pos_calls == [(requested_symbol, fake.TIMEFRAME_M1, 0, 10)]


def test_candles_does_not_select_an_already_visible_requested_symbol():
    requested_symbol = "EURUSD.a"
    now = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
    fake = FakeMt5Module()
    fake.symbols[requested_symbol] = SimpleNamespace(visible=True)
    fake.rates = _one_complete_rate(now)
    session = _connected_session(fake)

    session.get_candles(requested_symbol, "M1", None, now, 10)

    assert fake.symbol_info_calls == [requested_symbol]
    assert fake.symbol_select_calls == []
    assert fake.copy_rates_from_pos_calls == [(requested_symbol, fake.TIMEFRAME_M1, 0, 10)]


def test_candles_fails_clearly_when_exact_requested_symbol_does_not_exist():
    requested_symbol = "DOES.NOT.EXIST"
    now = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
    fake = FakeMt5Module()
    session = _connected_session(fake)

    with pytest.raises(BridgeError) as exc_info:
        session.get_candles(requested_symbol, "M1", None, now, 10)

    assert requested_symbol in exc_info.value.message
    assert "simbol" in exc_info.value.message.lower()
    assert fake.symbol_info_calls
    assert set(fake.symbol_info_calls) == {requested_symbol}
    assert fake.symbol_select_calls == []
    assert fake.copy_rates_from_pos_calls == []


def test_candles_fails_clearly_when_exact_requested_symbol_cannot_be_selected():
    requested_symbol = "EURUSD.a"
    now = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
    fake = FakeMt5Module()
    fake.symbols[requested_symbol] = SimpleNamespace(visible=False)
    fake.symbol_select_result = False
    session = _connected_session(fake)

    with pytest.raises(BridgeError) as exc_info:
        session.get_candles(requested_symbol, "M1", None, now, 10)

    assert requested_symbol in exc_info.value.message
    assert "selezion" in exc_info.value.message.lower()
    assert fake.symbol_info_calls
    assert set(fake.symbol_info_calls) == {requested_symbol}
    assert fake.symbol_select_calls
    assert set(fake.symbol_select_calls) == {(requested_symbol, True)}
    assert fake.copy_rates_from_pos_calls == []
