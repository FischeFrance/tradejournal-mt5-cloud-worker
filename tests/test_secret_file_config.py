"""Caricamento secret ``*_FILE`` per bridge e worker, senza Wine o Docker reali."""

from __future__ import annotations

import pytest

import common
import fake_bridge
from common import read_secret_from_env
from config import ConfigError, _read_secret, load_config
from windows.mt5_bridge import Mt5BridgeConfig


_BRIDGE_ENV_NAMES = {
    "MT5_BRIDGE_TOKEN",
    "MT5_BRIDGE_TOKEN_FILE",
    "MT5_LOGIN",
    "MT5_PASSWORD",
    "MT5_PASSWORD_FILE",
    "MT5_SERVER",
    "MT5_SESSION_MODE",
    "MT5_TERMINAL_PATH",
    "EURUSD_BROKER_SYMBOL",
    "HOST",
    "PORT",
}


def _secret_file(tmp_path, name: str, content: str, mode: int = 0o600):
    path = tmp_path / name
    path.write_bytes(content.encode("utf-8"))
    path.chmod(mode)
    return path


def _bridge_reader(source):
    return read_secret_from_env("TEST_SECRET", source)


def _worker_reader(source):
    return _read_secret(source, "TEST_SECRET")


@pytest.mark.parametrize("reader", [_bridge_reader, _worker_reader])
def test_direct_secret_env_remains_backward_compatible(reader):
    value = "  valore locale con spazi  "

    assert reader({"TEST_SECRET": value}) == value


@pytest.mark.parametrize("reader", [_bridge_reader, _worker_reader])
def test_secret_file_removes_one_final_line_ending_and_rejects_multiline(reader, tmp_path):
    path = _secret_file(tmp_path, "secret", "  valore con spazi  \r\n")
    assert reader({"TEST_SECRET_FILE": str(path)}) == "  valore con spazi  "

    multiline = _secret_file(tmp_path, "multiline", "prima\nseconda\n")
    with pytest.raises(ValueError, match="una sola riga"):
        reader({"TEST_SECRET_FILE": str(multiline)})

    two_newlines = _secret_file(tmp_path, "two-newlines", "valore\n\n")
    with pytest.raises(ValueError, match="una sola riga"):
        reader({"TEST_SECRET_FILE": str(two_newlines)})


@pytest.mark.parametrize("reader", [_bridge_reader, _worker_reader])
@pytest.mark.parametrize("value", ["prima\nseconda", "valore\r", "valore\x00coda"])
def test_direct_secret_rejects_control_characters(reader, value):
    with pytest.raises(ValueError, match="una sola riga"):
        reader({"TEST_SECRET": value})


@pytest.mark.parametrize("reader", [_bridge_reader, _worker_reader])
def test_direct_and_file_secret_are_rejected_even_if_direct_value_is_empty(
    reader, tmp_path
):
    path = _secret_file(tmp_path, "secret", "valore\n")

    with pytest.raises(ValueError, match="Configurazione ambigua.*TEST_SECRET.*TEST_SECRET_FILE"):
        reader({"TEST_SECRET": "", "TEST_SECRET_FILE": str(path)})


@pytest.mark.parametrize("reader", [_bridge_reader, _worker_reader])
def test_missing_secret_file_fails_without_echoing_path_or_content(reader, tmp_path):
    missing = tmp_path / "nome-potenzialmente-sensibile"

    with pytest.raises(ValueError, match="TEST_SECRET_FILE.*inesistente") as exc_info:
        reader({"TEST_SECRET_FILE": str(missing)})

    assert str(missing) not in str(exc_info.value)


@pytest.mark.parametrize("reader", [_bridge_reader, _worker_reader])
def test_non_regular_secret_file_is_rejected(reader, tmp_path):
    directory = tmp_path / "not-a-file"
    directory.mkdir(mode=0o700)

    with pytest.raises(ValueError, match="TEST_SECRET_FILE.*file regolare"):
        reader({"TEST_SECRET_FILE": str(directory)})


@pytest.mark.parametrize("reader", [_bridge_reader, _worker_reader])
@pytest.mark.parametrize("mode", [0o640, 0o604, 0o644, 0o620, 0o602, 0o610, 0o601])
def test_any_group_or_world_permission_is_rejected_without_leaking_value(
    reader, tmp_path, mode
):
    secret_value = "contenuto-test-da-non-stampare"
    path = _secret_file(tmp_path, f"secret-{mode:o}", secret_value, mode=mode)

    with pytest.raises(ValueError, match="TEST_SECRET_FILE.*permessi non sicuri") as exc_info:
        reader({"TEST_SECRET_FILE": str(path)})

    assert secret_value not in str(exc_info.value)


def test_windows_wine_mode_limit_emits_safe_warning_instead_of_rejecting(
    monkeypatch, capsys, tmp_path
):
    secret_value = "contenuto-wine-da-non-stampare"
    path = _secret_file(tmp_path, "wine-secret", secret_value, mode=0o644)
    monkeypatch.setattr(common, "_POSIX_SECRET_MODE_CHECK_SUPPORTED", False)

    assert read_secret_from_env("TEST_SECRET", {"TEST_SECRET_FILE": str(path)}) == secret_value

    warning = capsys.readouterr().err
    assert "Python Windows/Wine" in warning
    assert "0400/0600" in warning
    assert secret_value not in warning
    assert str(path) not in warning


def test_windows_bridge_reads_token_and_password_files(monkeypatch, tmp_path):
    for name in _BRIDGE_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    token = _secret_file(tmp_path, "bridge-token", "bridge-token-da-file\n")
    password = _secret_file(tmp_path, "investor-password", "  password con spazi  \n")
    monkeypatch.setenv("MT5_BRIDGE_TOKEN_FILE", str(token))
    monkeypatch.setenv("MT5_PASSWORD_FILE", str(password))
    monkeypatch.setenv("MT5_LOGIN", "123456")
    monkeypatch.setenv("MT5_SERVER", "Broker-Demo")

    config = Mt5BridgeConfig()

    assert config.token == "bridge-token-da-file"
    assert config.mt5_password == "  password con spazi  "


@pytest.mark.parametrize(
    ("direct_name", "file_name"),
    [
        ("MT5_BRIDGE_TOKEN", "MT5_BRIDGE_TOKEN_FILE"),
        ("MT5_PASSWORD", "MT5_PASSWORD_FILE"),
    ],
)
def test_windows_bridge_rejects_ambiguous_secret_sources(
    monkeypatch, tmp_path, direct_name, file_name
):
    for name in _BRIDGE_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    token = _secret_file(tmp_path, "default-token", "token-da-file\n")
    password = _secret_file(tmp_path, "default-password", "password-da-file\n")
    monkeypatch.setenv("MT5_BRIDGE_TOKEN_FILE", str(token))
    monkeypatch.setenv("MT5_PASSWORD_FILE", str(password))
    monkeypatch.setenv("MT5_LOGIN", "123456")
    monkeypatch.setenv("MT5_SERVER", "Broker-Demo")
    monkeypatch.setenv(direct_name, "valore-diretto-test")
    monkeypatch.setenv(file_name, str(token if "TOKEN" in file_name else password))

    with pytest.raises(ValueError, match=f"{direct_name}.*{file_name}") as exc_info:
        Mt5BridgeConfig()

    assert "valore-diretto-test" not in str(exc_info.value)


def test_fake_bridge_reads_token_file(monkeypatch, tmp_path):
    monkeypatch.delenv("MT5_BRIDGE_TOKEN", raising=False)
    token = _secret_file(tmp_path, "fake-bridge-token", "fake-token-da-file\n")
    monkeypatch.setenv("MT5_BRIDGE_TOKEN_FILE", str(token))

    config = fake_bridge.make_config_from_env()

    assert config.token == "fake-token-da-file"


def test_worker_reads_both_token_files_and_keeps_them_distinct(tmp_path):
    mt5_token = _secret_file(tmp_path, "mt5-token", "token-mt5\n")
    ingestion_token = _secret_file(tmp_path, "ingestion-token", "token-ingestion\n")

    config = load_config(
        {
            "MOCK_MODE": "false",
            "MT5_BRIDGE_URL": "http://mt5-runtime:8090",
            "MT5_BRIDGE_TOKEN_FILE": str(mt5_token),
            "TRADEJOURNAL_BRIDGE_TOKEN_FILE": str(ingestion_token),
        }
    )

    assert config.mt5_bridge_token == "token-mt5"
    assert config.tradejournal_bridge_token == "token-ingestion"


def test_worker_direct_reads_mt5_password_file(tmp_path):
    password = _secret_file(tmp_path, "mt5-password", "  investor da file  \n")

    config = load_config(
        {
            "MOCK_MODE": "false",
            "MT5_CLIENT_SOURCE": "direct",
            "MT5_LOGIN": "123456",
            "MT5_PASSWORD_FILE": str(password),
            "MT5_SERVER": "Broker-Demo",
        }
    )

    assert config.mt5_password == "  investor da file  "


def test_worker_rejects_direct_and_file_mt5_password_conflict(tmp_path):
    password = _secret_file(tmp_path, "mt5-password", "investor da file\n")

    with pytest.raises(ConfigError, match="MT5_PASSWORD.*MT5_PASSWORD_FILE") as exc_info:
        load_config(
            {
                "MT5_CLIENT_SOURCE": "direct",
                "MT5_PASSWORD": "investor-diretta",
                "MT5_PASSWORD_FILE": str(password),
            }
        )

    assert "investor-diretta" not in str(exc_info.value)


@pytest.mark.parametrize(
    "name", ["MT5_BRIDGE_TOKEN", "TRADEJOURNAL_BRIDGE_TOKEN"]
)
def test_worker_rejects_direct_and_file_token_conflict(name, tmp_path):
    path = _secret_file(tmp_path, f"{name.lower()}-file", "token-da-file\n")

    with pytest.raises(ConfigError, match=f"{name}.*{name}_FILE") as exc_info:
        load_config({name: "token-diretto-test", f"{name}_FILE": str(path)})

    assert "token-diretto-test" not in str(exc_info.value)


def test_worker_distinct_token_check_uses_file_contents(tmp_path):
    shared_value = "stesso-token-test"
    mt5_token = _secret_file(tmp_path, "mt5-token", f"{shared_value}\n")
    ingestion_token = _secret_file(tmp_path, "ingestion-token", f"{shared_value}\n")

    with pytest.raises(ConfigError, match="devono essere distinti") as exc_info:
        load_config(
            {
                "MOCK_MODE": "false",
                "MT5_BRIDGE_URL": "http://mt5-runtime:8090",
                "MT5_BRIDGE_TOKEN_FILE": str(mt5_token),
                "TRADEJOURNAL_BRIDGE_TOKEN_FILE": str(ingestion_token),
            }
        )

    assert shared_value not in str(exc_info.value)


def test_empty_file_is_treated_like_empty_existing_env(tmp_path):
    empty = _secret_file(tmp_path, "empty-token", "\n")

    with pytest.raises(ConfigError, match="MT5_BRIDGE_TOKEN non vuoto"):
        load_config(
            {
                "MOCK_MODE": "false",
                "MT5_BRIDGE_URL": "http://mt5-runtime:8090",
                "MT5_BRIDGE_TOKEN_FILE": str(empty),
            }
        )
