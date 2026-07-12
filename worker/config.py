"""Configurazione del worker, letta esclusivamente da variabili d'ambiente.

Due processi condividono questo modulo: il trade-sync worker (main.py, invariato) e il
market-data-worker (market_data_main.py, nuovo). I campi APP_MODE/ENABLE_MARKET_DATA/... sono
rilevanti solo per il secondo: il primo non li legge mai, quindi il loro default (APP_MODE=client,
ENABLE_MARKET_DATA=false) e' scelto apposta per non cambiare nulla nel comportamento del
trade-sync worker quando questi env var non sono impostati (installazioni cliente esistenti).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Mapping, Optional, Tuple

_VALID_APP_MODES = ("client", "research")
_VALID_MARKET_DATA_SOURCES = ("mock", "mt5")
_DEFAULT_MARKET_SYMBOLS = ("EURUSD",)
_DEFAULT_MARKET_TIMEFRAMES = ("M1", "M5", "M15", "H1", "H4", "D1")


class ConfigError(ValueError):
    """Configurazione non valida: il processo deve fermarsi subito (fail fast), non degradare
    silenziosamente a un default che potrebbe mascherare un errore di deployment."""


def _as_bool(raw: Optional[str], default: bool) -> bool:
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _as_int(raw: Optional[str], default: int) -> int:
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def _as_tuple(raw: Optional[str], default: Tuple[str, ...]) -> Tuple[str, ...]:
    if raw is None or raw.strip() == "":
        return default
    return tuple(item.strip() for item in raw.split(",") if item.strip())


@dataclass(frozen=True)
class Config:
    mock_mode: bool
    dry_run: bool
    mt5_login: Optional[str]
    mt5_password: Optional[str]
    mt5_server: Optional[str]
    tradejournal_api_url: Optional[str]
    tradejournal_bridge_token: Optional[str]
    poll_interval_seconds: int
    log_level: str
    app_mode: str = "client"
    enable_market_data: bool = False
    database_url: Optional[str] = None
    market_symbols: Tuple[str, ...] = field(default_factory=lambda: _DEFAULT_MARKET_SYMBOLS)
    market_timeframes: Tuple[str, ...] = field(default_factory=lambda: _DEFAULT_MARKET_TIMEFRAMES)
    market_data_poll_seconds: int = 60
    market_data_source: str = "mock"

    def __post_init__(self) -> None:
        if self.app_mode not in _VALID_APP_MODES:
            raise ConfigError(
                f"APP_MODE non valido: '{self.app_mode}'. Valori ammessi: {list(_VALID_APP_MODES)}."
            )
        if self.market_data_source not in _VALID_MARKET_DATA_SOURCES:
            raise ConfigError(
                f"MARKET_DATA_SOURCE non valido: '{self.market_data_source}'. "
                f"Valori ammessi: {list(_VALID_MARKET_DATA_SOURCES)}."
            )
        if self.enable_market_data and self.app_mode != "research":
            raise ConfigError(
                "ENABLE_MARKET_DATA=true richiede APP_MODE=research (installazioni client non "
                "devono mai raccogliere dati di mercato)."
            )
        if self.enable_market_data:
            if not self.database_url:
                raise ConfigError("ENABLE_MARKET_DATA=true richiede DATABASE_URL non vuoto.")
            if not self.market_symbols:
                raise ConfigError("ENABLE_MARKET_DATA=true richiede almeno un simbolo in MARKET_SYMBOLS.")
            if not self.market_timeframes:
                raise ConfigError("ENABLE_MARKET_DATA=true richiede almeno un timeframe in MARKET_TIMEFRAMES.")
            if self.market_data_poll_seconds <= 0:
                raise ConfigError("MARKET_DATA_POLL_SECONDS deve essere un intero positivo.")

    @property
    def has_api_target(self) -> bool:
        return bool(self.tradejournal_api_url) and bool(self.tradejournal_bridge_token)


def load_config(env: Optional[Mapping[str, str]] = None) -> Config:
    """Costruisce la configurazione dalle variabili d'ambiente (o da un mapping per i test)."""
    source = env if env is not None else os.environ

    def get(name: str) -> Optional[str]:
        value = source.get(name)
        return value if value not in (None, "") else None

    return Config(
        mock_mode=_as_bool(source.get("MOCK_MODE"), True),
        dry_run=_as_bool(source.get("DRY_RUN"), True),
        mt5_login=get("MT5_LOGIN"),
        mt5_password=get("MT5_PASSWORD"),
        mt5_server=get("MT5_SERVER"),
        tradejournal_api_url=get("TRADEJOURNAL_API_URL"),
        tradejournal_bridge_token=get("TRADEJOURNAL_BRIDGE_TOKEN"),
        poll_interval_seconds=_as_int(source.get("POLL_INTERVAL_SECONDS"), 5),
        log_level=get("LOG_LEVEL") or "INFO",
        app_mode=(get("APP_MODE") or "client").strip().lower(),
        enable_market_data=_as_bool(source.get("ENABLE_MARKET_DATA"), False),
        database_url=get("DATABASE_URL"),
        market_symbols=_as_tuple(source.get("MARKET_SYMBOLS"), _DEFAULT_MARKET_SYMBOLS),
        market_timeframes=_as_tuple(source.get("MARKET_TIMEFRAMES"), _DEFAULT_MARKET_TIMEFRAMES),
        market_data_poll_seconds=_as_int(source.get("MARKET_DATA_POLL_SECONDS"), 60),
        market_data_source=(get("MARKET_DATA_SOURCE") or "mock").strip().lower(),
    )
