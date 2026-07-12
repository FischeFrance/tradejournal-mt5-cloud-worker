"""Persistenza delle candele OHLC su Postgres per il market-data-worker.

Ogni query usa parametri psycopg2 (%(name)s), mai interpolazione di stringhe: anche se i valori
oggi provengono solo da configurazione locale (MARKET_SYMBOLS/MARKET_TIMEFRAMES) e da una
sorgente dati fidata, la disciplina resta la stessa indipendentemente dalla provenienza del dato.

L'upsert su market_candles usa ON CONFLICT sull'indice unique (symbol_id, timeframe, open_time):
salvare due volte la stessa candela aggiorna la riga esistente invece di duplicarla (vedi
db/migrations/0001_initial_schema.sql). Il checkpoint per riprendere il polling dopo un riavvio
non e' una tabella separata: si deriva con MAX(open_time), cosi' lo schema resta minimo e non
puo' mai disallinearsi dai dati realmente salvati.
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterable, Optional

from market_data_source import Candle

_UPSERT_SYMBOL_SQL = """
INSERT INTO market_symbols (canonical_symbol, broker_symbol, source, enabled)
VALUES (%(canonical_symbol)s, %(broker_symbol)s, %(source)s, TRUE)
ON CONFLICT (canonical_symbol, broker_symbol, source)
DO UPDATE SET updated_at = now()
RETURNING id;
"""

_UPSERT_CANDLE_SQL = """
INSERT INTO market_candles
    (symbol_id, timeframe, open_time, open, high, low, close, tick_volume, spread, source)
VALUES
    (%(symbol_id)s, %(timeframe)s, %(open_time)s, %(open)s, %(high)s, %(low)s, %(close)s,
     %(tick_volume)s, %(spread)s, %(source)s)
ON CONFLICT (symbol_id, timeframe, open_time)
DO UPDATE SET
    open = EXCLUDED.open,
    high = EXCLUDED.high,
    low = EXCLUDED.low,
    close = EXCLUDED.close,
    tick_volume = EXCLUDED.tick_volume,
    spread = EXCLUDED.spread,
    source = EXCLUDED.source,
    updated_at = now();
"""

_CHECKPOINT_SQL = """
SELECT MAX(open_time) FROM market_candles
WHERE symbol_id = %(symbol_id)s AND timeframe = %(timeframe)s;
"""

_COUNT_SQL = """
SELECT COUNT(*) FROM market_candles
WHERE symbol_id = %(symbol_id)s AND timeframe = %(timeframe)s;
"""


class MarketDataStore:
    def __init__(self, database_url: str) -> None:
        self._database_url = database_url
        self._conn = None

    def connect(self) -> None:
        try:
            import psycopg2  # type: ignore[import-not-found]  # lazy: vedi db_migrate.apply_migrations
        except ImportError as exc:
            raise RuntimeError(
                "Pacchetto 'psycopg2' non disponibile: richiesto solo per il market-data-worker "
                "(vedi requirements-research.txt)."
            ) from exc
        self._conn = psycopg2.connect(self._database_url)

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "MarketDataStore":
        self.connect()
        return self

    def __exit__(self, *_exc_info) -> None:
        self.close()

    def _ensure_connected(self):
        if self._conn is None:
            raise RuntimeError("MarketDataStore non connesso: chiamare connect() prima.")
        return self._conn

    def ensure_symbol(self, canonical_symbol: str, broker_symbol: str, source: str) -> int:
        """Upsert del simbolo (identita' = canonical_symbol+broker_symbol+source), ritorna l'id."""
        conn = self._ensure_connected()
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    _UPSERT_SYMBOL_SQL,
                    {
                        "canonical_symbol": canonical_symbol,
                        "broker_symbol": broker_symbol,
                        "source": source,
                    },
                )
                row = cur.fetchone()
        return int(row[0])

    def get_checkpoint(self, symbol_id: int, timeframe: str) -> Optional[datetime]:
        """Ultimo open_time gia' salvato per symbol_id/timeframe, o None se non ce n'e' ancora."""
        conn = self._ensure_connected()
        with conn:
            with conn.cursor() as cur:
                cur.execute(_CHECKPOINT_SQL, {"symbol_id": symbol_id, "timeframe": timeframe})
                row = cur.fetchone()
        return row[0] if row else None

    def upsert_candles(self, symbol_id: int, candles: Iterable[Candle]) -> int:
        """Upsert idempotente: stessa (symbol_id, timeframe, open_time) aggiorna la riga
        esistente invece di duplicarla. Ritorna il numero di candele processate."""
        conn = self._ensure_connected()
        count = 0
        with conn:
            with conn.cursor() as cur:
                for candle in candles:
                    cur.execute(
                        _UPSERT_CANDLE_SQL,
                        {
                            "symbol_id": symbol_id,
                            "timeframe": candle.timeframe,
                            "open_time": candle.open_time,
                            "open": candle.open,
                            "high": candle.high,
                            "low": candle.low,
                            "close": candle.close,
                            "tick_volume": candle.tick_volume,
                            "spread": candle.spread,
                            "source": candle.source,
                        },
                    )
                    count += 1
        return count

    def count_candles(self, symbol_id: int, timeframe: str) -> int:
        """Numero di righe salvate per symbol_id/timeframe. Usato dai test per verificare che un
        upsert ripetuto non produca duplicati (il conteggio non deve crescere)."""
        conn = self._ensure_connected()
        with conn:
            with conn.cursor() as cur:
                cur.execute(_COUNT_SQL, {"symbol_id": symbol_id, "timeframe": timeframe})
                row = cur.fetchone()
        return int(row[0])
