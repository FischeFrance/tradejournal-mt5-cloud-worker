"""Test per worker/market_data_source.py: interfaccia MarketDataSource e MockMarketDataSource.

Nessuno di questi test tocca Postgres o rete: MockMarketDataSource e' una funzione pura
dell'indice di candela (vedi market_data_source._build_candle), quindi interamente testabile
senza dipendenze esterne."""

from __future__ import annotations


from market_data_source import (
    TIMEFRAME_SECONDS,
    MockMarketDataSource,
    build_market_data_source,
    is_ohlc_valid,
)


def test_mock_source_produces_requested_number_of_candles():
    source = MockMarketDataSource()
    candles = source.get_candles("EURUSD", "M1", since=None, limit=10)
    assert len(candles) == 10


def test_mock_source_candles_are_chronologically_ordered_and_spaced_by_timeframe():
    source = MockMarketDataSource()
    candles = source.get_candles("EURUSD", "M5", since=None, limit=5)
    step = TIMEFRAME_SECONDS["M5"]
    for previous, current in zip(candles, candles[1:]):
        delta = (current.open_time - previous.open_time).total_seconds()
        assert delta == step


def test_mock_source_open_time_is_utc_aware():
    source = MockMarketDataSource()
    candle = source.get_candles("EURUSD", "H1", since=None, limit=1)[0]
    assert candle.open_time.tzinfo is not None
    assert candle.open_time.utcoffset().total_seconds() == 0


def test_mock_source_candles_have_valid_ohlc():
    source = MockMarketDataSource()
    for timeframe in TIMEFRAME_SECONDS:
        for candle in source.get_candles("EURUSD", timeframe, since=None, limit=20):
            assert is_ohlc_valid(candle), f"OHLC non coerente per {timeframe}: {candle}"


def test_mock_source_supports_multiple_symbols_with_different_values():
    source = MockMarketDataSource()
    eurusd = source.get_candles("EURUSD", "M1", since=None, limit=1)[0]
    gbpusd = source.get_candles("GBPUSD", "M1", since=None, limit=1)[0]
    # Simboli diversi devono produrre serie diverse (altrimenti il mock sarebbe inutile per
    # verificare che il worker gestisca correttamente piu' simboli in parallelo).
    assert eurusd.open != gbpusd.open or eurusd.close != gbpusd.close


def test_mock_source_is_deterministic_across_independent_instances():
    """Chiamare due volte la stessa candela (stesso indice, cioe' stesso since/limit su due
    istanze indipendenti) deve restituire valori identici: e' il requisito di idempotenza alla
    base dell'upsert su Postgres (stesso open_time => stessa riga, mai una riga diversa)."""
    first_call = MockMarketDataSource().get_candles("EURUSD", "M1", since=None, limit=3)
    second_call = MockMarketDataSource().get_candles("EURUSD", "M1", since=None, limit=3)
    assert first_call == second_call


def test_mock_source_since_excludes_already_seen_candles():
    source = MockMarketDataSource()
    first_batch = source.get_candles("EURUSD", "M1", since=None, limit=5)
    checkpoint = first_batch[-1].open_time
    next_batch = source.get_candles("EURUSD", "M1", since=checkpoint, limit=5)
    assert next_batch[0].open_time > checkpoint
    seen_times = {c.open_time for c in first_batch}
    assert all(c.open_time not in seen_times for c in next_batch)


def test_mock_source_can_simulate_a_gap():
    """Un indice mancante nella serie non deve bloccare la generazione: il gap e' semplicemente
    assente dal risultato, cosi' come lo sarebbe un buco reale nello storico di un broker."""
    source = MockMarketDataSource(gap_indices=frozenset({2}))
    candles = source.get_candles("EURUSD", "M1", since=None, limit=5)
    step = TIMEFRAME_SECONDS["M1"]
    open_times = [c.open_time for c in candles]
    # L'indice 2 manca: il salto tra le candele attorno al gap e' di 2 step, non 1.
    deltas = [
        (later - earlier).total_seconds() / step for earlier, later in zip(open_times, open_times[1:])
    ]
    assert 2 in deltas
    assert len(candles) == 5  # il gap non riduce il numero di candele richieste, solo la densita'


def test_build_market_data_source_mock():
    source = build_market_data_source("mock")
    assert isinstance(source, MockMarketDataSource)


def test_build_market_data_source_mt5_without_bridge_config_raises():
    # MARKET_DATA_SOURCE=mt5 e' ora implementato (client HTTP verso mt5-bridge, vedi
    # tests/test_mt5_market_data_source.py per la copertura completa): qui verifichiamo solo che
    # la factory rifiuti di costruire il client senza bridge_url/bridge_token, invece di fingere
    # una configurazione valida.
    import pytest

    with pytest.raises(ValueError):
        build_market_data_source("mt5")


def test_build_market_data_source_invalid_raises_value_error():
    import pytest

    with pytest.raises(ValueError):
        build_market_data_source("bloomberg")
