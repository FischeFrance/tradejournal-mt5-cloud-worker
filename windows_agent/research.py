from __future__ import annotations

import sqlite3
from pathlib import Path


class ResearchCollector:
    def __init__(
        self, db: Path, server_allowlisted: bool, client_requested: bool = False
    ) -> None:
        if client_requested and not server_allowlisted:
            raise PermissionError("research requires server-side allowlist")
        self.enabled = server_allowlisted
        self.connection = sqlite3.connect(db)
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS market_data(symbol TEXT, timeframe TEXT, timestamp_utc TEXT, bid REAL, ask REAL, spread REAL, open REAL, high REAL, low REAL, close REAL, tick_volume REAL, real_volume REAL)"
        )

    def add(self, record: dict) -> None:
        if not self.enabled:
            return
        columns = (
            "symbol",
            "timeframe",
            "timestamp_utc",
            "bid",
            "ask",
            "spread",
            "open",
            "high",
            "low",
            "close",
            "tick_volume",
            "real_volume",
        )
        self.connection.execute(
            f"INSERT INTO market_data({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
            tuple(record.get(k) for k in columns),
        )
        self.connection.commit()
