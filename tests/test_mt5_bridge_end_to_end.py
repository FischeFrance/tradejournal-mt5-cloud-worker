"""Test di integrazione end-to-end: fake bridge (vero server HTTP, fixture fake_bridge_server) ->
Mt5MarketDataSource (client HTTP di produzione) -> market_data_main._sync_symbol_timeframe ->
Postgres reale (fixture market_data_store, Docker). Nessun componente e' mockato: e' la stessa
catena che gira nel container market-data-worker con MARKET_DATA_SOURCE=mt5, solo assemblata a
mano nel test invece che tramite docker-compose.research-mt5-fake.yml (verificato separatamente
con Docker Compose vero, vedi README)."""

from __future__ import annotations

import logging

from market_data_main import _sync_symbol_timeframe
from market_data_source import Mt5MarketDataSource


def _mt5_source(base_url: str, token: str) -> Mt5MarketDataSource:
    return Mt5MarketDataSource(bridge_url=base_url, bridge_token=token, timeout_seconds=5.0)


def test_candles_from_fake_bridge_land_in_postgres(fake_bridge_server, market_data_store):
    base_url, token = fake_bridge_server
    source = _mt5_source(base_url, token)
    symbol_id = market_data_store.ensure_symbol("EURUSD", "EURUSD", "mt5")

    written = _sync_symbol_timeframe(market_data_store, source, symbol_id, "EURUSD", "M1", limit=25)

    assert written == 25
    assert market_data_store.count_candles(symbol_id, "M1") == 25


def test_repeated_sync_does_not_duplicate_rows(fake_bridge_server, market_data_store):
    base_url, token = fake_bridge_server
    source = _mt5_source(base_url, token)
    symbol_id = market_data_store.ensure_symbol("EURUSD", "EURUSD", "mt5")

    first = _sync_symbol_timeframe(market_data_store, source, symbol_id, "EURUSD", "M5", limit=10)
    second = _sync_symbol_timeframe(market_data_store, source, symbol_id, "EURUSD", "M5", limit=10)

    assert first == 10
    assert second == 10  # 10 nuove candele in continuazione dal checkpoint, non duplicate
    assert market_data_store.count_candles(symbol_id, "M5") == 20


def test_checkpoint_survives_a_simulated_worker_restart(fake_bridge_server, market_data_store):
    """Simula un riavvio del market-data-worker: una NUOVA istanza di Mt5MarketDataSource (come
    accadrebbe con un nuovo processo) che riparte dal checkpoint persistito su Postgres, non da
    uno stato in memoria del processo precedente."""
    base_url, token = fake_bridge_server
    symbol_id = market_data_store.ensure_symbol("EURUSD", "EURUSD", "mt5")

    first_source = _mt5_source(base_url, token)
    _sync_symbol_timeframe(market_data_store, first_source, symbol_id, "EURUSD", "H1", limit=5)

    # "Riavvio": nuova istanza del client, stesso store/database.
    second_source = _mt5_source(base_url, token)
    written_after_restart = _sync_symbol_timeframe(market_data_store, second_source, symbol_id, "EURUSD", "H1", limit=5)

    assert written_after_restart == 5
    assert market_data_store.count_candles(symbol_id, "H1") == 10


def test_wrong_bridge_token_logs_error_and_does_not_crash(fake_bridge_server, market_data_store, caplog):
    """Un token errato (es. mt5-bridge riconfigurato senza aggiornare market-data-worker) deve
    risultare in zero candele scritte per quel ciclo e un log di errore, non in un'eccezione che
    fa cadere l'intero worker (vedi market_data_main._sync_symbol_timeframe)."""
    base_url, _token = fake_bridge_server
    source = _mt5_source(base_url, "wrong-token")
    symbol_id = market_data_store.ensure_symbol("EURUSD", "EURUSD", "mt5")

    with caplog.at_level(logging.ERROR):
        written = _sync_symbol_timeframe(market_data_store, source, symbol_id, "EURUSD", "M1", limit=5)

    assert written == 0
    assert market_data_store.count_candles(symbol_id, "M1") == 0
    assert "mt5-bridge" in caplog.text


def test_no_secrets_leak_during_full_sync(fake_bridge_server, market_data_store, caplog):
    # Token reale del fake bridge (fixture), sync che riesce: verifica che anche in caso di
    # successo il token non compaia mai nei log.
    base_url, token = fake_bridge_server
    source = _mt5_source(base_url, token)
    symbol_id = market_data_store.ensure_symbol("EURUSD", "EURUSD", "mt5")

    with caplog.at_level(logging.DEBUG):
        _sync_symbol_timeframe(market_data_store, source, symbol_id, "EURUSD", "M1", limit=5)

    assert token not in caplog.text
