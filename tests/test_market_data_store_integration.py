"""Test di integrazione di MarketDataStore contro un Postgres reale (vedi tests/conftest.py,
fixture postgres_database_url/market_data_store): nessun mock del database, query reali,
comprese le violazioni dei CHECK/UNIQUE constraint effettivamente definiti dalle migration in
db/migrations/. Saltati automaticamente se Docker non e' disponibile nell'ambiente di test."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from market_data_source import Candle, MockMarketDataSource
from market_data_main import _sync_symbol_timeframe


def _candle(open_time, open_=Decimal("1.1000"), high=Decimal("1.1010"), low=Decimal("1.0990"), close=Decimal("1.1005")):
    return Candle(
        symbol="EURUSD",
        timeframe="M1",
        open_time=open_time,
        open=open_,
        high=high,
        low=low,
        close=close,
        tick_volume=100,
        spread=10,
        source="mock",
    )


def test_ensure_symbol_is_idempotent(market_data_store):
    first_id = market_data_store.ensure_symbol("EURUSD", "EURUSD", "mock")
    second_id = market_data_store.ensure_symbol("EURUSD", "EURUSD", "mock")
    assert first_id == second_id


def test_ensure_symbol_distinguishes_different_sources(market_data_store):
    mock_id = market_data_store.ensure_symbol("EURUSD", "EURUSD", "mock")
    mt5_id = market_data_store.ensure_symbol("EURUSD", "EURUSD", "mt5")
    assert mock_id != mt5_id


def test_upsert_candles_persists_rows(market_data_store):
    symbol_id = market_data_store.ensure_symbol("EURUSD", "EURUSD", "mock")
    open_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
    written = market_data_store.upsert_candles(symbol_id, [_candle(open_time)])
    assert written == 1
    assert market_data_store.count_candles(symbol_id, "M1") == 1


def test_upsert_same_candle_twice_does_not_duplicate(market_data_store):
    """Requisito centrale della modalita' research: una stessa candela (stesso symbol_id,
    timeframe, open_time) non deve mai produrre una seconda riga, nemmeno se il worker la invia
    due volte (es. dopo un riavvio che riparte da un checkpoint leggermente indietro)."""
    symbol_id = market_data_store.ensure_symbol("EURUSD", "EURUSD", "mock")
    open_time = datetime(2026, 1, 1, tzinfo=timezone.utc)

    market_data_store.upsert_candles(symbol_id, [_candle(open_time)])
    market_data_store.upsert_candles(symbol_id, [_candle(open_time)])

    assert market_data_store.count_candles(symbol_id, "M1") == 1


def test_upsert_same_candle_with_updated_values_overwrites_in_place(market_data_store):
    symbol_id = market_data_store.ensure_symbol("EURUSD", "EURUSD", "mock")
    open_time = datetime(2026, 1, 1, tzinfo=timezone.utc)

    market_data_store.upsert_candles(symbol_id, [_candle(open_time, close=Decimal("1.1005"))])
    market_data_store.upsert_candles(symbol_id, [_candle(open_time, close=Decimal("1.1008"))])

    assert market_data_store.count_candles(symbol_id, "M1") == 1
    checkpoint = market_data_store.get_checkpoint(symbol_id, "M1")
    assert checkpoint == open_time


def test_get_checkpoint_is_none_when_no_candles(market_data_store):
    symbol_id = market_data_store.ensure_symbol("EURUSD", "EURUSD", "mock")
    assert market_data_store.get_checkpoint(symbol_id, "M1") is None


def test_get_checkpoint_returns_latest_open_time(market_data_store):
    symbol_id = market_data_store.ensure_symbol("EURUSD", "EURUSD", "mock")
    earlier = datetime(2026, 1, 1, tzinfo=timezone.utc)
    later = datetime(2026, 1, 1, 0, 5, tzinfo=timezone.utc)

    market_data_store.upsert_candles(symbol_id, [_candle(later), _candle(earlier)])

    assert market_data_store.get_checkpoint(symbol_id, "M1") == later


def test_ohlc_check_constraint_rejects_invalid_candle(market_data_store):
    """Verifica che il vincolo sia applicato dal database stesso (non solo a livello Python):
    una candela con high < low deve essere rifiutata da Postgres."""
    import psycopg2

    symbol_id = market_data_store.ensure_symbol("EURUSD", "EURUSD", "mock")
    open_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
    invalid_candle = _candle(open_time, high=Decimal("1.0"), low=Decimal("2.0"))

    with pytest.raises(psycopg2.errors.CheckViolation):
        market_data_store.upsert_candles(symbol_id, [invalid_candle])


def test_sync_symbol_timeframe_handles_gap_without_losing_checkpoint(market_data_store):
    """Simula un buco temporale nella sorgente (indice mancante): il worker deve comunque
    avanzare il checkpoint fino all'ultima candela realmente disponibile, senza inventare dati
    per il buco ne' bloccarsi."""
    symbol_id = market_data_store.ensure_symbol("EURUSD", "EURUSD", "mock")
    source = MockMarketDataSource(gap_indices=frozenset({2}))

    written = _sync_symbol_timeframe(market_data_store, source, symbol_id, "EURUSD", "M1", limit=5)

    assert written == 5  # 5 candele richieste, il gap e' semplicemente assente, non sostituito
    assert market_data_store.count_candles(symbol_id, "M1") == 5


def test_restart_of_collector_does_not_duplicate_candles(market_data_store):
    """Simula un riavvio del market-data-worker: una seconda sincronizzazione, che riparte dal
    checkpoint persistito su Postgres, non deve ri-scrivere ne' duplicare le candele gia' salvate."""
    symbol_id = market_data_store.ensure_symbol("EURUSD", "EURUSD", "mock")
    source = MockMarketDataSource()

    first_run_written = _sync_symbol_timeframe(market_data_store, source, symbol_id, "EURUSD", "M1", limit=10)
    assert first_run_written == 10
    assert market_data_store.count_candles(symbol_id, "M1") == 10

    # "Riavvio": stessa chiamata, stesso store (checkpoint derivato da MAX(open_time) su
    # Postgres, non da uno stato in memoria del processo Python) -- since=None non e' piu'
    # rilevante perche' _sync_symbol_timeframe legge sempre il checkpoint dal database.
    second_run_written = _sync_symbol_timeframe(market_data_store, source, symbol_id, "EURUSD", "M1", limit=10)

    assert second_run_written == 10  # 10 nuove candele, in continuazione dal checkpoint
    assert market_data_store.count_candles(symbol_id, "M1") == 20  # nessuna duplicata, nessuna persa
