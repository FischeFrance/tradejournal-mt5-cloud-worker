"""Sorgenti di dati di mercato (candele OHLC), usate esclusivamente dal market-data-worker.

Modulo indipendente da `mt5_client.py`/`mock_mt5_client.py` (eventi di trading): stessa idea di
interfaccia astratta + implementazione mock, ma nessun accoppiamento di codice, perche' le due
cose modellano domini diversi (eventi puntuali di trading vs serie storiche OHLC) e non c'e'
motivo di farle dipendere l'una dall'altra.

`MockMarketDataSource` e' deterministica: stesso symbol/timeframe/indice di candela producono
sempre esattamente lo stesso valore, senza stato condiviso tra chiamate. Questo la rende adatta
sia al backfill iniziale sia ai test di idempotenza (richiedere due volte la stessa candela deve
restituire valori identici, cosi' un upsert successivo non la duplica ne' la altera).
"""

from __future__ import annotations

import hashlib
import math
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import List, Optional

#: Durata di una candela per timeframe, in secondi. Le stesse sigle usate da MARKET_TIMEFRAMES.
TIMEFRAME_SECONDS = {
    "M1": 60,
    "M5": 5 * 60,
    "M15": 15 * 60,
    "H1": 60 * 60,
    "H4": 4 * 60 * 60,
    "D1": 24 * 60 * 60,
}

#: Epoca fissa usata come origine degli indici di candela: rende la generazione riproducibile
#: (nessuna dipendenza da datetime.now()) tra esecuzioni diverse e tra processi diversi.
_EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)


@dataclass(frozen=True)
class Candle:
    symbol: str
    timeframe: str
    open_time: datetime  # sempre timezone-aware, UTC
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    tick_volume: Optional[int]
    spread: Optional[int]
    source: str


def is_ohlc_valid(candle: Candle) -> bool:
    """Coerenza minima di una candela OHLC: high e' il massimo, low e' il minimo."""
    return (
        candle.high >= candle.open
        and candle.high >= candle.close
        and candle.high >= candle.low
        and candle.low <= candle.open
        and candle.low <= candle.close
    )


class MarketDataSource(ABC):
    """Interfaccia che ogni sorgente di dati di mercato (mock o reale) deve implementare."""

    @abstractmethod
    def get_candles(
        self,
        symbol: str,
        timeframe: str,
        since: Optional[datetime],
        limit: int,
    ) -> List[Candle]:
        """Restituisce candele OHLC per symbol/timeframe, in ordine cronologico crescente.

        `since` e' il checkpoint dell'ultima candela gia' salvata (esclusa dal risultato): se
        None, si parte dall'inizio della serie disponibile. Al piu' `limit` candele per chiamata,
        cosi' che backfill e poll incrementale condividano lo stesso metodo senza caricare serie
        arbitrariamente lunghe in memoria in un colpo solo.
        """


def _deterministic_seed(symbol: str, timeframe: str) -> int:
    digest = hashlib.sha256(f"{symbol}:{timeframe}".encode("utf-8")).hexdigest()
    return int(digest[:8], 16)


def _to_price(value: float) -> Decimal:
    # Passa per str (non per float direttamente) per evitare artefatti di rappresentazione
    # binaria nella conversione a Decimal: vedi anche market_candles.open/high/low/close (NUMERIC).
    return Decimal(str(round(value, 5)))


def _build_candle(symbol: str, timeframe: str, index: int, source: str) -> Candle:
    step_seconds = TIMEFRAME_SECONDS[timeframe]
    open_time = _EPOCH + timedelta(seconds=index * step_seconds)

    seed = _deterministic_seed(symbol, timeframe)
    base = 1.0 + (seed % 1000) / 1000.0  # prezzo base deterministico per symbol/timeframe
    phase = (index + seed) * 0.15

    open_price = base + math.sin(phase) * 0.01
    close_price = base + math.sin(phase + 0.5) * 0.01
    wick_up = abs(math.sin(phase + 1.0)) * 0.004
    wick_down = abs(math.sin(phase + 1.5)) * 0.004

    high_price = max(open_price, close_price) + wick_up
    low_price = min(open_price, close_price) - wick_down

    return Candle(
        symbol=symbol,
        timeframe=timeframe,
        open_time=open_time,
        open=_to_price(open_price),
        high=_to_price(high_price),
        low=_to_price(low_price),
        close=_to_price(close_price),
        tick_volume=100 + (index % 50),
        spread=10 + (index % 5),
        source=source,
    )


class MockMarketDataSource(MarketDataSource):
    """Sorgente mock deterministica: nessuna dipendenza da Wine/MT5/rete.

    `gap_indices` permette di simulare un buco temporale nella serie (indici di candela
    deliberatamente assenti dal risultato), utile per verificare che il market-data-worker e lo
    store non si blocchino ne' inventino dati quando una barra manca dalla sorgente.
    """

    SOURCE_NAME = "mock"

    def __init__(self, gap_indices: Optional[frozenset] = None) -> None:
        self._gap_indices = gap_indices or frozenset()

    def get_candles(
        self,
        symbol: str,
        timeframe: str,
        since: Optional[datetime],
        limit: int,
    ) -> List[Candle]:
        if timeframe not in TIMEFRAME_SECONDS:
            raise ValueError(f"Timeframe non supportato dal mock: '{timeframe}'.")
        if limit <= 0:
            return []

        step_seconds = TIMEFRAME_SECONDS[timeframe]
        if since is None:
            start_index = 0
        else:
            start_index = int((since - _EPOCH).total_seconds() // step_seconds) + 1

        candles: List[Candle] = []
        index = start_index
        # Limite di sicurezza sul numero di indici esaminati, per non entrare in loop
        # infinito se 'limit' e' alto e i gap sono numerosi: non e' un limite di business,
        # solo una rete di sicurezza contro un uso improprio del parametro gap_indices.
        max_index = start_index + limit + len(self._gap_indices) + 1000
        while len(candles) < limit and index <= max_index:
            if index not in self._gap_indices:
                candles.append(_build_candle(symbol, timeframe, index, self.SOURCE_NAME))
            index += 1
        return candles


class Mt5MarketDataSource(MarketDataSource):
    """Adapter predisposto per MT5 reale. NON implementato in questa fase.

    Solleva NotImplementedError in modo esplicito invece di restituire dati falsi o di fingere
    una connessione funzionante: MARKET_DATA_SOURCE=mt5 non e' un percorso supportato finche' non
    e' risolto lo stesso nodo architetturale gia' descritto in mt5_client.py/README per
    RealMt5Client (pacchetto MetaTrader5 nativo Windows vs worker Python Linux nativo).
    """

    SOURCE_NAME = "mt5"

    def __init__(self, *_args, **_kwargs) -> None:
        raise NotImplementedError(
            "MARKET_DATA_SOURCE=mt5 non e' implementato in questa fase. Usare "
            "MARKET_DATA_SOURCE=mock. Vedi README, sezione 'Market data reale (non "
            "ancora implementato)'."
        )

    def get_candles(
        self,
        symbol: str,
        timeframe: str,
        since: Optional[datetime],
        limit: int,
    ) -> List[Candle]:
        raise NotImplementedError("Mt5MarketDataSource non e' implementato in questa fase.")


def build_market_data_source(source_name: str) -> MarketDataSource:
    """Factory usata da market_data_main.py in base a MARKET_DATA_SOURCE."""
    if source_name == "mock":
        return MockMarketDataSource()
    if source_name == "mt5":
        return Mt5MarketDataSource()
    raise ValueError(f"MARKET_DATA_SOURCE non valido: '{source_name}'.")
