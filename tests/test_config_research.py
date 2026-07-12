"""Test per l'estensione di config.py introdotta dalla modalita' research (APP_MODE,
ENABLE_MARKET_DATA, DATABASE_URL, MARKET_SYMBOLS, MARKET_TIMEFRAMES, MARKET_DATA_SOURCE).

test_default_config_matches_current_client_behavior e' il test piu' importante di questo file:
verifica che un .env "vecchio" (senza nessuna delle nuove chiavi, come quelli di qualunque
installazione cliente esistente) produca esattamente lo stesso comportamento di prima."""

from __future__ import annotations

import pytest
from config import ConfigError, load_config


def test_default_config_matches_current_client_behavior():
    """Nessuna delle nuove env var e' impostata: deve comportarsi esattamente come prima."""
    config = load_config({})
    assert config.app_mode == "client"
    assert config.enable_market_data is False
    assert config.database_url is None
    assert config.market_data_source == "mock"
    # Campi pre-esistenti, invariati.
    assert config.mock_mode is True
    assert config.dry_run is True


def test_app_mode_client_is_default_and_valid():
    config = load_config({"APP_MODE": "client"})
    assert config.app_mode == "client"
    assert config.enable_market_data is False


def test_app_mode_research_alone_is_valid_with_market_data_disabled():
    config = load_config({"APP_MODE": "research"})
    assert config.app_mode == "research"
    assert config.enable_market_data is False


def test_app_mode_invalid_value_raises():
    with pytest.raises(ConfigError, match="APP_MODE"):
        load_config({"APP_MODE": "admin"})


def test_app_mode_is_case_insensitive():
    config = load_config({"APP_MODE": "Research", "ENABLE_MARKET_DATA": "true", "DATABASE_URL": "postgresql://x"})
    assert config.app_mode == "research"


def test_enable_market_data_true_requires_research_mode():
    with pytest.raises(ConfigError, match="ENABLE_MARKET_DATA"):
        load_config({"APP_MODE": "client", "ENABLE_MARKET_DATA": "true", "DATABASE_URL": "postgresql://x"})


def test_enable_market_data_true_with_research_and_database_url_is_valid():
    config = load_config({
        "APP_MODE": "research",
        "ENABLE_MARKET_DATA": "true",
        "DATABASE_URL": "postgresql://user:pass@localhost:5432/db",
    })
    assert config.enable_market_data is True
    assert config.database_url == "postgresql://user:pass@localhost:5432/db"


def test_enable_market_data_true_without_database_url_raises():
    with pytest.raises(ConfigError, match="DATABASE_URL"):
        load_config({"APP_MODE": "research", "ENABLE_MARKET_DATA": "true"})


def test_market_symbols_parsed_from_csv():
    config = load_config({"MARKET_SYMBOLS": "EURUSD, GBPUSD ,XAUUSD"})
    assert config.market_symbols == ("EURUSD", "GBPUSD", "XAUUSD")


def test_market_symbols_default_is_eurusd():
    config = load_config({})
    assert config.market_symbols == ("EURUSD",)


def test_market_timeframes_parsed_from_csv():
    config = load_config({"MARKET_TIMEFRAMES": "M1,H1,D1"})
    assert config.market_timeframes == ("M1", "H1", "D1")


def test_market_timeframes_default():
    config = load_config({})
    assert config.market_timeframes == ("M1", "M5", "M15", "H1", "H4", "D1")


def test_enable_market_data_requires_non_empty_symbols():
    # Una MARKET_SYMBOLS vuota o assente ricade sul default (EURUSD, coerente con _as_bool/
    # _as_int altrove in questo modulo): solo un valore fatto di soli separatori produce
    # davvero una tupla vuota, il caso che la validazione deve intercettare.
    with pytest.raises(ConfigError, match="MARKET_SYMBOLS"):
        load_config({
            "APP_MODE": "research",
            "ENABLE_MARKET_DATA": "true",
            "DATABASE_URL": "postgresql://x",
            "MARKET_SYMBOLS": " , ,",
        })


def test_market_data_poll_seconds_default_and_parsing():
    assert load_config({}).market_data_poll_seconds == 60
    assert load_config({"MARKET_DATA_POLL_SECONDS": "15"}).market_data_poll_seconds == 15


def test_enable_market_data_requires_positive_poll_seconds():
    with pytest.raises(ConfigError, match="MARKET_DATA_POLL_SECONDS"):
        load_config({
            "APP_MODE": "research",
            "ENABLE_MARKET_DATA": "true",
            "DATABASE_URL": "postgresql://x",
            "MARKET_DATA_POLL_SECONDS": "0",
        })


def test_market_data_source_default_is_mock():
    assert load_config({}).market_data_source == "mock"


def test_market_data_source_accepts_mt5_value_but_not_arbitrary_strings():
    assert load_config({"MARKET_DATA_SOURCE": "mt5"}).market_data_source == "mt5"
    with pytest.raises(ConfigError, match="MARKET_DATA_SOURCE"):
        load_config({"MARKET_DATA_SOURCE": "bloomberg"})
