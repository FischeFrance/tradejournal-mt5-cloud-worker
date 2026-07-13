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
import logging
import math
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger("mt5_worker.market_data_source")

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


class Mt5BridgeError(RuntimeError):
    """Errore di comunicazione con mt5-bridge (rete, timeout, payload non valido, o richiesta
    rifiutata dal bridge). Mai nascosto dietro una lista vuota: chi chiama get_candles() deve
    gestirlo esplicitamente (vedi market_data_main._sync_symbol_timeframe)."""


class Mt5BridgeAuthError(Mt5BridgeError):
    """mt5-bridge ha rifiutato l'autenticazione (401/403): MT5_BRIDGE_TOKEN errato o assente."""


def _format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_utc_timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise Mt5BridgeError(
            f"'open_time' non valido: atteso una stringa ISO8601, ricevuto {type(value).__name__!r}."
        )
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise Mt5BridgeError(f"'open_time' non e' un timestamp ISO8601 valido: '{value}'.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise Mt5BridgeError(f"'open_time' deve essere in UTC (offset zero): '{value}'.")
    return parsed


def _decimal_field(raw: Dict[str, Any], name: str) -> Decimal:
    value = raw.get(name)
    if not isinstance(value, str):
        raise Mt5BridgeError(f"Campo '{name}' non valido: atteso una stringa decimale, ricevuto {value!r}.")
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise Mt5BridgeError(f"Campo '{name}' non e' un numero decimale valido: '{value}'.") from exc


def _error_summary(response: "requests.Response") -> str:
    try:
        data = response.json()
    except ValueError:
        return f"<risposta non-JSON, {len(response.content)} byte>"
    if isinstance(data, dict) and isinstance(data.get("error"), dict):
        return str(data["error"].get("message", data["error"]))
    return f"<{len(response.content)} byte>"


class Mt5MarketDataSource(MarketDataSource):
    """Client HTTP verso mt5-bridge (fake o reale, stesso contratto -- vedi bridge/common.py,
    bridge/fake/fake_bridge.py, bridge/windows/mt5_bridge.py e README). Non importa mai
    MetaTrader5 ne' apre connessioni Wine/IPC direttamente: parla solo HTTP con un servizio
    separato, cosi' il market-data-worker resta un processo Linux puro indipendentemente da dove
    e come gira il bridge.

    Non nasconde mai un errore dietro una lista vuota: ogni fallimento (timeout, autenticazione,
    payload non valido, OHLC incoerente) solleva Mt5BridgeError o Mt5BridgeAuthError. Applica
    retry con backoff esponenziale solo per errori transitori (rete, 5xx): un 401/403/4xx e'
    permanente finche' non si corregge la configurazione, quindi non viene ritentato (stesso
    principio gia' usato in event_sender.py per l'invio verso TradeJournal).
    """

    SOURCE_NAME = "mt5"

    def __init__(
        self,
        bridge_url: Optional[str],
        bridge_token: Optional[str],
        timeout_seconds: float = 10.0,
        max_retries: int = 3,
        backoff_base_seconds: float = 0.5,
        max_backoff_seconds: float = 8.0,
        sleep_fn=time.sleep,
    ) -> None:
        if not bridge_url:
            raise ValueError("Mt5MarketDataSource richiede un bridge_url non vuoto (MT5_BRIDGE_URL).")
        if not bridge_token:
            raise ValueError("Mt5MarketDataSource richiede un bridge_token non vuoto (MT5_BRIDGE_TOKEN).")
        self._bridge_url = bridge_url.rstrip("/")
        self._bridge_token = bridge_token
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries
        self._backoff_base_seconds = backoff_base_seconds
        self._max_backoff_seconds = max_backoff_seconds
        self._sleep = sleep_fn

    def _headers(self) -> Dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._bridge_token}",
        }

    def health(self) -> Dict[str, Any]:
        """Diagnostica opzionale (non fa parte dell'interfaccia MarketDataSource): interroga
        GET /health. Solleva Mt5BridgeError in caso di fallimento; il chiamante
        (market_data_main.run()) decide se trattarlo come fatale o solo informativo."""
        url = f"{self._bridge_url}/health"
        try:
            response = requests.get(url, headers=self._headers(), timeout=self._timeout_seconds)
        except requests.RequestException as exc:
            raise Mt5BridgeError(f"mt5-bridge non raggiungibile su /health: {exc}") from exc
        if response.status_code != 200:
            raise Mt5BridgeError(f"mt5-bridge /health ha risposto status={response.status_code}.")
        try:
            return response.json()
        except ValueError as exc:
            raise Mt5BridgeError(f"mt5-bridge /health ha restituito un payload non valido: {exc}") from exc

    def get_candles(
        self,
        symbol: str,
        timeframe: str,
        since: Optional[datetime],
        limit: int,
    ) -> List[Candle]:
        if timeframe not in TIMEFRAME_SECONDS:
            raise ValueError(f"Timeframe non supportato: '{timeframe}'.")
        if limit <= 0:
            return []

        payload = {
            "symbol": symbol,
            "timeframe": timeframe,
            "since": _format_utc(since) if since is not None else None,
            "limit": limit,
        }
        url = f"{self._bridge_url}/v1/candles"

        last_error: Optional[str] = None
        for attempt in range(1, self._max_retries + 1):
            try:
                response = requests.post(url, json=payload, headers=self._headers(), timeout=self._timeout_seconds)
            except requests.RequestException as exc:
                last_error = str(exc)
                logger.warning(
                    "Tentativo %s/%s verso mt5-bridge fallito (errore di rete) per %s/%s: %s",
                    attempt, self._max_retries, symbol, timeframe, exc,
                )
            else:
                if response.status_code == 200:
                    candles = self._parse_response(response, symbol, timeframe)
                    return self._filter_and_sort(candles, since, limit)
                if response.status_code in (401, 403):
                    raise Mt5BridgeAuthError(
                        f"mt5-bridge ha rifiutato l'autenticazione (status={response.status_code})."
                    )
                if 400 <= response.status_code < 500:
                    raise Mt5BridgeError(
                        f"mt5-bridge ha rifiutato la richiesta per {symbol}/{timeframe} "
                        f"(status={response.status_code}): {_error_summary(response)}"
                    )
                last_error = f"http_{response.status_code}"
                logger.warning(
                    "Tentativo %s/%s verso mt5-bridge fallito (status=%s) per %s/%s",
                    attempt, self._max_retries, response.status_code, symbol, timeframe,
                )

            if attempt < self._max_retries:
                backoff = min(self._backoff_base_seconds * (2 ** (attempt - 1)), self._max_backoff_seconds)
                self._sleep(backoff)

        raise Mt5BridgeError(
            f"mt5-bridge irraggiungibile per {symbol}/{timeframe} dopo {self._max_retries} tentativi: {last_error}"
        )

    def _parse_response(self, response: "requests.Response", expected_symbol: str, expected_timeframe: str) -> List[Candle]:
        try:
            data = response.json()
        except ValueError as exc:
            raise Mt5BridgeError(f"mt5-bridge ha restituito un payload non valido (JSON malformato): {exc}") from exc

        if not isinstance(data, dict) or not isinstance(data.get("candles"), list):
            raise Mt5BridgeError("mt5-bridge ha restituito un payload senza una lista 'candles' valida.")

        return [self._parse_candle(raw, expected_symbol, expected_timeframe) for raw in data["candles"]]

    def _parse_candle(self, raw: Any, symbol: str, timeframe: str) -> Candle:
        if not isinstance(raw, dict):
            raise Mt5BridgeError(f"mt5-bridge ha restituito una candela non valida: {raw!r}")

        open_time = _parse_utc_timestamp(raw.get("open_time"))
        open_price = _decimal_field(raw, "open")
        high_price = _decimal_field(raw, "high")
        low_price = _decimal_field(raw, "low")
        close_price = _decimal_field(raw, "close")

        tick_volume = raw.get("tick_volume")
        spread = raw.get("spread")
        if tick_volume is not None and not isinstance(tick_volume, int):
            raise Mt5BridgeError(f"Campo 'tick_volume' non valido: {tick_volume!r}.")
        if spread is not None and not isinstance(spread, int):
            raise Mt5BridgeError(f"Campo 'spread' non valido: {spread!r}.")

        candle = Candle(
            symbol=symbol,
            timeframe=timeframe,
            open_time=open_time,
            open=open_price,
            high=high_price,
            low=low_price,
            close=close_price,
            tick_volume=tick_volume,
            spread=spread,
            source=raw.get("source") or self.SOURCE_NAME,
        )
        if not is_ohlc_valid(candle):
            raise Mt5BridgeError(
                f"mt5-bridge ha restituito OHLC incoerente per {symbol}/{timeframe} @ {open_time.isoformat()}."
            )
        return candle

    @staticmethod
    def _filter_and_sort(candles: List[Candle], since: Optional[datetime], limit: int) -> List[Candle]:
        # Difesa in profondita': anche se il bridge sbagliasse (since incluso invece di
        # esclusivo, ordine non cronologico, piu' candele del limit richiesto), il client non si
        # fida ciecamente del contratto lato server.
        filtered = [c for c in candles if since is None or c.open_time > since]
        filtered.sort(key=lambda c: c.open_time)
        return filtered[:limit]


def build_market_data_source(
    source_name: str,
    *,
    mt5_bridge_url: Optional[str] = None,
    mt5_bridge_token: Optional[str] = None,
    mt5_bridge_timeout_seconds: float = 10.0,
) -> MarketDataSource:
    """Factory usata da market_data_main.py in base a MARKET_DATA_SOURCE. Riceve la
    configurazione del bridge esplicitamente (nessuna lettura di env var qui dentro): chi chiama
    e' responsabile di passare cio' che serve, cosi' la factory resta testabile senza dover
    manipolare l'ambiente di processo con variabili globali nascoste."""
    if source_name == "mock":
        return MockMarketDataSource()
    if source_name == "mt5":
        return Mt5MarketDataSource(
            bridge_url=mt5_bridge_url,
            bridge_token=mt5_bridge_token,
            timeout_seconds=mt5_bridge_timeout_seconds,
        )
    raise ValueError(f"MARKET_DATA_SOURCE non valido: '{source_name}'.")
