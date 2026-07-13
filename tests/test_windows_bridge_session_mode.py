"""Selezione della sessione del bridge Windows, senza Wine o terminale MT5 reali."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from common import BridgeError
from windows.mt5_bridge import Mt5BridgeConfig, _Mt5Session


_CONFIG_ENV_KEYS = {
    "MT5_BRIDGE_TOKEN",
    "MT5_BRIDGE_TOKEN_FILE",
    "EURUSD_BROKER_SYMBOL",
    "HOST",
    "PORT",
    "MT5_SESSION_MODE",
    "MT5_LOGIN",
    "MT5_PASSWORD",
    "MT5_PASSWORD_FILE",
    "MT5_SERVER",
    "MT5_TERMINAL_PATH",
    "MT5_EXPECTED_LOGIN",
    "MT5_EXPECTED_SERVER",
}

_WINDOWS_TERMINAL_PATH = r"C:\Program Files\MetaTrader 5\terminal64.exe"


class FakeMt5Module:
    def __init__(self) -> None:
        self.account = SimpleNamespace(login=123456, server="Broker-Demo")
        self.initialize_calls = []
        self.login_calls = []
        self.account_info_calls = 0
        self.symbol_select_calls = []
        self.initialize_exception = None
        self.last_error_value = (500, "errore MT5 fittizio")

    def initialize(self, **kwargs):
        self.initialize_calls.append(kwargs)
        if self.initialize_exception is not None:
            raise self.initialize_exception
        return True

    def login(self, login_id, password=None, server=None):
        self.login_calls.append((login_id, password, server))
        return True

    def account_info(self):
        self.account_info_calls += 1
        return self.account

    def symbol_select(self, symbol, enabled):
        self.symbol_select_calls.append((symbol, enabled))
        return True

    def last_error(self):
        return self.last_error_value


def _config(monkeypatch, **overrides) -> Mt5BridgeConfig:
    for key in _CONFIG_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)

    values = {
        "MT5_BRIDGE_TOKEN": "bridge-token-for-session-tests",
        "MT5_LOGIN": "123456",
        "MT5_PASSWORD": "fake-investor-password",
        "MT5_SERVER": "Broker-Demo",
    }
    values.update(overrides)
    for key, value in values.items():
        if value is not None:
            monkeypatch.setenv(key, value)
    return Mt5BridgeConfig()


def _session(config: Mt5BridgeConfig, fake_mt5: FakeMt5Module) -> _Mt5Session:
    return _Mt5Session(config, sleep_fn=lambda _seconds: None, mt5_module=fake_mt5)


def test_session_mode_defaults_to_login(monkeypatch):
    config = _config(monkeypatch)

    assert config.mt5_session_mode == "login"


def test_existing_does_not_require_login_password_or_server(monkeypatch):
    config = _config(
        monkeypatch,
        MT5_SESSION_MODE="existing",
        MT5_TERMINAL_PATH=_WINDOWS_TERMINAL_PATH,
        MT5_LOGIN=None,
        MT5_PASSWORD=None,
        MT5_SERVER=None,
    )

    assert config.mt5_session_mode == "existing"
    assert config.mt5_login == ""
    assert config.mt5_password == ""
    assert config.mt5_server == ""


def test_existing_connects_successfully_without_password(monkeypatch):
    config = _config(
        monkeypatch,
        MT5_SESSION_MODE="existing",
        MT5_TERMINAL_PATH=_WINDOWS_TERMINAL_PATH,
        MT5_LOGIN=None,
        MT5_PASSWORD=None,
        MT5_SERVER=None,
    )
    fake = FakeMt5Module()

    _session(config, fake).connect()

    assert fake.initialize_calls == [{"path": _WINDOWS_TERMINAL_PATH}]
    assert fake.account_info_calls == 1
    assert fake.login_calls == []
    assert fake.symbol_select_calls == []


def test_existing_requires_terminal_path(monkeypatch):
    with pytest.raises(ValueError, match="MT5_TERMINAL_PATH.*MT5_SESSION_MODE=existing"):
        _config(monkeypatch, MT5_SESSION_MODE="existing", MT5_TERMINAL_PATH=None)


def test_existing_initializes_exact_terminal_and_never_calls_login(monkeypatch, capsys):
    config = _config(
        monkeypatch,
        MT5_SESSION_MODE="existing",
        MT5_TERMINAL_PATH=_WINDOWS_TERMINAL_PATH,
        MT5_EXPECTED_LOGIN="123456",
        MT5_EXPECTED_SERVER="Broker-Demo",
        # Anche se le credenziali legacy sono presenti, existing non deve usarle.
        MT5_LOGIN="987654",
        MT5_PASSWORD="unused-fake-password",
        MT5_SERVER="Unused-Server",
    )
    fake = FakeMt5Module()

    _session(config, fake).connect()

    assert fake.initialize_calls == [{"path": _WINDOWS_TERMINAL_PATH}]
    assert fake.account_info_calls == 1
    assert fake.login_calls == []
    assert fake.symbol_select_calls == []
    stderr = capsys.readouterr().err
    assert "123456" not in stderr
    assert "Broker-Demo" not in stderr
    assert "unused-fake-password" not in stderr


def test_existing_fails_when_account_info_is_none_without_calling_login(monkeypatch):
    config = _config(
        monkeypatch,
        MT5_SESSION_MODE="existing",
        MT5_TERMINAL_PATH=_WINDOWS_TERMINAL_PATH,
        MT5_LOGIN=None,
        MT5_PASSWORD=None,
        MT5_SERVER=None,
    )
    fake = FakeMt5Module()
    fake.account = None

    with pytest.raises(BridgeError, match=r"account_info\(\).*None") as exc_info:
        _session(config, fake).connect()

    assert exc_info.value.code == "mt5_error"
    assert fake.account_info_calls == 3
    assert fake.login_calls == []


def test_existing_fails_on_expected_login_mismatch_without_exposing_logins(monkeypatch, capsys):
    config = _config(
        monkeypatch,
        MT5_SESSION_MODE="existing",
        MT5_TERMINAL_PATH=_WINDOWS_TERMINAL_PATH,
        MT5_EXPECTED_LOGIN="999888",
        MT5_LOGIN=None,
        MT5_PASSWORD=None,
        MT5_SERVER=None,
    )
    fake = FakeMt5Module()

    with pytest.raises(BridgeError, match="MT5_EXPECTED_LOGIN") as exc_info:
        _session(config, fake).connect()

    diagnostics = capsys.readouterr().err + exc_info.value.message
    assert "999888" not in diagnostics
    assert "123456" not in diagnostics
    assert fake.login_calls == []


def test_existing_fails_on_exact_expected_server_mismatch_without_exposing_servers(
    monkeypatch, capsys
):
    config = _config(
        monkeypatch,
        MT5_SESSION_MODE="existing",
        MT5_TERMINAL_PATH=_WINDOWS_TERMINAL_PATH,
        MT5_EXPECTED_SERVER="broker-demo",
        MT5_LOGIN=None,
        MT5_PASSWORD=None,
        MT5_SERVER=None,
    )
    fake = FakeMt5Module()

    with pytest.raises(BridgeError, match="MT5_EXPECTED_SERVER") as exc_info:
        _session(config, fake).connect()

    diagnostics = capsys.readouterr().err + exc_info.value.message
    assert "broker-demo" not in diagnostics
    assert "Broker-Demo" not in diagnostics
    assert fake.login_calls == []


def test_login_mode_still_initializes_and_calls_login(monkeypatch):
    config = _config(monkeypatch, MT5_SESSION_MODE="login")
    fake = FakeMt5Module()

    _session(config, fake).connect()

    assert fake.initialize_calls == [{}]
    assert fake.login_calls == [(123456, "fake-investor-password", "Broker-Demo")]
    assert fake.account_info_calls == 1
    assert fake.symbol_select_calls == []


@pytest.mark.parametrize("missing", ["MT5_LOGIN", "MT5_PASSWORD", "MT5_SERVER"])
def test_login_mode_still_requires_legacy_credentials(monkeypatch, missing):
    with pytest.raises(ValueError, match="obbligatori.*MT5_SESSION_MODE=login"):
        _config(monkeypatch, MT5_SESSION_MODE="login", **{missing: None})


def test_invalid_session_mode_fails_clearly_at_startup(monkeypatch):
    with pytest.raises(ValueError, match="MT5_SESSION_MODE non valido.*login.*existing"):
        _config(monkeypatch, MT5_SESSION_MODE="reuse")


def test_invalid_login_does_not_leak_its_value_in_exception_chain(monkeypatch):
    invalid_login = "not-a-real-login"

    with pytest.raises(ValueError, match="identificativo numerico") as exc_info:
        _config(monkeypatch, MT5_SESSION_MODE="login", MT5_LOGIN=invalid_login)

    assert invalid_login not in str(exc_info.value)
    assert exc_info.value.__cause__ is None


def test_retry_diagnostics_redact_password_token_login_and_server(monkeypatch, capsys):
    config = _config(monkeypatch)
    fake = FakeMt5Module()
    fake.initialize_exception = RuntimeError(
        "failure fake-investor-password bridge-token-for-session-tests 123456 Broker-Demo"
    )

    with pytest.raises(BridgeError) as exc_info:
        _session(config, fake).connect()

    diagnostics = capsys.readouterr().err + exc_info.value.message
    assert "fake-investor-password" not in diagnostics
    assert "bridge-token-for-session-tests" not in diagnostics
    assert "123456" not in diagnostics
    assert "Broker-Demo" not in diagnostics
    assert "<redacted>" in diagnostics
