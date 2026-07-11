"""Configurazione del worker, letta esclusivamente da variabili d'ambiente."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping, Optional


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
    )
