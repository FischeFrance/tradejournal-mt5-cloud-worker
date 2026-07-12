-- Schema iniziale per la modalita' research (dati di mercato).
--
-- Applicata da worker/db_migrate.py (vedi quel modulo per il meccanismo di tracciamento delle
-- migration gia' applicate, tabella schema_migrations). Nessuna DDL e' emessa da altrove nel
-- codice: questo file e' l'unica fonte di verita' per lo schema.
--
-- Note di design:
-- - I prezzi usano NUMERIC (non float/double precision): un float binario a doppia precisione
--   introduce errori di arrotondamento non deterministici sulle ultime cifre decimali, che qui
--   sarebbero inaccettabili per dati storici salvati in modo permanente. NUMERIC(18,8) copre
--   simboli forex (5-6 decimali tipici) e la maggior parte dei CFD/crypto con margine.
-- - open_time e' TIMESTAMPTZ: Postgres lo normalizza e lo restituisce sempre in UTC
--   internamente; l'applicazione (market_data_source.py/market_data_store.py) e' comunque
--   responsabile di passare solo datetime timezone-aware in UTC, mai naive.
-- - Un solo indice UNIQUE su (symbol_id, timeframe, open_time) serve sia da vincolo di
--   deduplicazione sia da indice per le query di lookup/checkpoint: non serve un secondo indice
--   separato per lo stesso set di colonne.

CREATE TABLE market_symbols (
    id              BIGSERIAL PRIMARY KEY,
    canonical_symbol TEXT NOT NULL,
    broker_symbol   TEXT NOT NULL,
    source          TEXT NOT NULL,
    enabled         BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT market_symbols_identity_key
        UNIQUE (canonical_symbol, broker_symbol, source)
);

CREATE TABLE market_candles (
    id              BIGSERIAL PRIMARY KEY,
    symbol_id       BIGINT NOT NULL REFERENCES market_symbols (id) ON DELETE CASCADE,
    timeframe       TEXT NOT NULL,
    open_time       TIMESTAMPTZ NOT NULL,
    open            NUMERIC(18, 8) NOT NULL,
    high            NUMERIC(18, 8) NOT NULL,
    low             NUMERIC(18, 8) NOT NULL,
    close           NUMERIC(18, 8) NOT NULL,
    tick_volume     BIGINT,
    spread          INTEGER,
    source          TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT market_candles_ohlc_coherent CHECK (
        high >= open AND high >= close AND high >= low
        AND low <= open AND low <= close
    ),
    CONSTRAINT market_candles_tick_volume_non_negative CHECK (tick_volume IS NULL OR tick_volume >= 0),
    CONSTRAINT market_candles_spread_non_negative CHECK (spread IS NULL OR spread >= 0)
);

-- Vincolo di deduplicazione + indice di lookup/checkpoint (MAX(open_time) per symbol/timeframe).
CREATE UNIQUE INDEX market_candles_symbol_timeframe_open_time_key
    ON market_candles (symbol_id, timeframe, open_time);
