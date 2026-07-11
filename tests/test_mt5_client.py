"""Test di RealMt5Client senza Wine/MT5 reali: il pacchetto `MetaTrader5` viene sostituito con
un doppio di test iniettato in sys.modules. Copre retry limitato, mapping dei campi e assenza di
segreti nei log -- non copre il comportamento reale del terminale (validato solo su Ubuntu, vedi
README)."""

import sys
from types import SimpleNamespace

import pytest

from mt5_client import Mt5ConnectionError, RealMt5Client


class FakeMt5Module:
    def __init__(self):
        self.initialize_results = [True]
        self.login_results = [True]
        self.account_info_result = None
        self.positions = []
        self.orders = []
        self.deals = []
        self.initialize_calls = 0
        self.login_calls = []
        self.last_error_value = (1, "errore fittizio")

    def initialize(self):
        self.initialize_calls += 1
        idx = min(self.initialize_calls - 1, len(self.initialize_results) - 1)
        return self.initialize_results[idx]

    def login(self, login_id, password=None, server=None):
        self.login_calls.append((login_id, password, server))
        idx = min(len(self.login_calls) - 1, len(self.login_results) - 1)
        return self.login_results[idx]

    def account_info(self):
        return self.account_info_result

    def positions_get(self):
        return self.positions

    def orders_get(self):
        return self.orders

    def history_deals_get(self, date_from, date_to):
        return self.deals

    def last_error(self):
        return self.last_error_value


@pytest.fixture
def fake_mt5(monkeypatch):
    fake = FakeMt5Module()
    monkeypatch.setitem(sys.modules, "MetaTrader5", fake)
    return fake


def _client(**overrides) -> RealMt5Client:
    defaults = dict(
        login="555444333",
        password="SuperInvestorPass123",
        server="Demo-Server",
        max_retries=3,
        retry_delay_seconds=0.0,
        sleep_fn=lambda _seconds: None,
    )
    defaults.update(overrides)
    return RealMt5Client(**defaults)


def test_connect_missing_config_raises_immediately_without_touching_mt5():
    client = RealMt5Client(login=None, password=None, server=None)

    with pytest.raises(Mt5ConnectionError, match="non configurati"):
        client.connect()


def test_connect_rejects_non_numeric_login():
    client = _client(login="not-a-number")

    with pytest.raises(Mt5ConnectionError, match="numerico"):
        client.connect()


def test_connect_success_calls_initialize_and_login(fake_mt5):
    client = _client()

    client.connect()

    assert fake_mt5.initialize_calls == 1
    assert fake_mt5.login_calls == [(555444333, "SuperInvestorPass123", "Demo-Server")]
    assert client.health_status()["connected"] is True


def test_connect_retries_transient_initialize_failure_then_succeeds(fake_mt5):
    fake_mt5.initialize_results = [False, True]
    client = _client(max_retries=3)

    client.connect()

    assert fake_mt5.initialize_calls == 2
    assert client.health_status()["connected"] is True


def test_connect_exhausts_retries_and_raises(fake_mt5):
    fake_mt5.initialize_results = [False, False, False]
    client = _client(max_retries=3)

    with pytest.raises(Mt5ConnectionError, match="fallita dopo 3 tentativi"):
        client.connect()

    assert fake_mt5.initialize_calls == 3
    assert client.health_status()["connected"] is False


def test_reconnect_invokes_connect_again(fake_mt5):
    client = _client()
    client.connect()

    client.reconnect()

    assert fake_mt5.initialize_calls == 2
    assert len(fake_mt5.login_calls) == 2


def test_account_info_maps_expected_fields(fake_mt5):
    fake_mt5.account_info_result = SimpleNamespace(
        login=555444333, server="Demo-Server", balance=10000.0, equity=10050.0,
        currency="USD", leverage=100,
    )
    client = _client()
    client.connect()

    info = client.account_info()

    assert info == {
        "login": "555444333",
        "server": "Demo-Server",
        "balance": 10000.0,
        "equity": 10050.0,
        "currency": "USD",
        "leverage": 100,
    }


def test_get_open_positions_maps_direction_and_fields(fake_mt5):
    fake_mt5.positions = [
        SimpleNamespace(ticket=1, symbol="EURUSD", type=0, volume=0.1, price_open=1.1000,
                         sl=1.0950, tp=1.1100, time=1735689600),
        SimpleNamespace(ticket=2, symbol="GBPUSD", type=1, volume=0.2, price_open=1.2500,
                         sl=1.2450, tp=1.2600, time=1735689600),
    ]
    client = _client()
    client.connect()

    positions = client.get_open_positions()

    assert positions["1"]["direction"] == "buy"
    assert positions["1"]["symbol"] == "EURUSD"
    assert positions["1"]["stop_loss"] == 1.0950
    assert positions["2"]["direction"] == "sell"
    assert positions["1"]["open_time"] is not None


def test_get_pending_orders_maps_fields(fake_mt5):
    fake_mt5.orders = [
        SimpleNamespace(ticket=10, symbol="EURUSD", type=2, volume_current=0.05,
                         price_open=1.0900, sl=1.0850, tp=1.1000),
    ]
    client = _client()
    client.connect()

    orders = client.get_pending_orders()

    assert orders["10"]["direction"] == "buy"
    assert orders["10"]["price"] == 1.0900


def test_get_recent_deals_keeps_only_exit_deals_and_maps_fields(fake_mt5):
    fake_mt5.deals = [
        SimpleNamespace(ticket=500, position_id=1, price=1.1180, profit=13.0, commission=-0.5,
                         swap=-0.1, time=1735693200, entry=1),  # uscita
        SimpleNamespace(ticket=501, position_id=2, price=1.1000, profit=0.0, commission=0.0,
                         swap=0.0, time=1735693200, entry=0),  # ingresso, da ignorare
    ]
    client = _client()
    client.connect()

    deals = client.get_recent_deals()

    assert set(deals.keys()) == {"500"}
    assert deals["500"]["position_ticket"] == "1"
    assert deals["500"]["close_price"] == 1.1180
    assert deals["500"]["profit"] == 13.0
    assert deals["500"]["close_time"] is not None


def test_methods_raise_before_connect(fake_mt5):
    client = _client()

    with pytest.raises(Mt5ConnectionError, match="chiamare connect"):
        client.account_info()
    with pytest.raises(Mt5ConnectionError, match="chiamare connect"):
        client.get_open_positions()
    with pytest.raises(Mt5ConnectionError, match="chiamare connect"):
        client.get_recent_deals()
    with pytest.raises(Mt5ConnectionError, match="chiamare connect"):
        client.get_pending_orders()


def test_password_and_login_never_appear_unmasked_in_logs_on_success(fake_mt5, caplog):
    client = _client(login="555444333", password="SuperInvestorPass123")

    with caplog.at_level("DEBUG"):
        client.connect()

    assert "SuperInvestorPass123" not in caplog.text
    assert "555444333" not in caplog.text


def test_password_never_appears_unmasked_in_logs_on_failure(fake_mt5, caplog):
    fake_mt5.login_results = [False, False, False]
    client = _client(login="555444333", password="SuperInvestorPass123", max_retries=3)

    with caplog.at_level("DEBUG"):
        with pytest.raises(Mt5ConnectionError):
            client.connect()

    assert "SuperInvestorPass123" not in caplog.text
    assert "555444333" not in caplog.text
