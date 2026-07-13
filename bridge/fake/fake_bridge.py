"""Fake bridge: implementa lo stesso contratto HTTP di mt5-bridge (vedi bridge/common.py e
bridge/windows/mt5_bridge.py) con dati sintetici deterministici, senza alcuna dipendenza da
MetaTrader5/Wine. Gira nativamente su qualunque architettura, incluso Ubuntu ARM64: usato per
validare in locale sia market-data-worker sia trade-sync worker prima di avere un vero terminale
MT5 su una VPS AMD64 (vedi i compose fake dedicati e README). Oltre alle candele espone uno
scenario trading thread-safe a otto stati (baseline vuota + sette transizioni).

Simula anche gli scenari di errore che un bridge reale puo' incontrare, tramite l'header di test
X-Mt5-Fake-Scenario (nomi validi: timeout, mt5_error, malformed_payload, duplicate_candle).
Questo header e' un dettaglio SOLO di questo fake: il client di produzione
(worker/market_data_source.py:Mt5MarketDataSource) non lo invia mai, ne' saprebbe cosa farsene un
bridge reale (che semplicemente lo ignorerebbe, non essendo nel contratto pubblico).

Solo standard library (vedi bridge/common.py): nessuna dipendenza da installare.
"""

from __future__ import annotations

import hashlib
import math
import os
import sys
import time
import copy
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from http.server import ThreadingHTTPServer
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common import (  # noqa: E402
    TIMEFRAME_SECONDS,
    BaseBridgeHandler,
    BridgeConfig,
    BridgeError,
    format_iso_utc,
    read_secret_from_env,
)

#: Epoca fissa usata solo per seminare la generazione deterministica dei prezzi (stesso stile di
#: worker/market_data_source.py, implementazione volutamente indipendente: questo servizio simula
#: un bridge esterno, non deve dipendere dal codice del worker Linux che lo interroga).
_EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)

#: Quanto dura la sleep dello scenario "timeout": abbastanza breve da non appesantire i test (che
#: configurano un client con un timeout ancora piu' corto per dimostrare l'enforcement reale),
#: abbastanza lunga da non essere confusa con la normale latenza di risposta.
TIMEOUT_SCENARIO_SLEEP_SECONDS = 1.5

_TRADING_GENERATED_AT = (
    "2026-07-13T09:59:59Z",
    "2026-07-13T10:00:01Z",
    "2026-07-13T10:05:01Z",
    "2026-07-13T10:10:01Z",
    "2026-07-13T10:15:01Z",
    "2026-07-13T10:20:01Z",
    "2026-07-13T10:25:01Z",
    "2026-07-13T10:30:01Z",
)


def _deterministic_seed(symbol: str, timeframe: str) -> int:
    digest = hashlib.sha256(f"fake-bridge:{symbol}:{timeframe}".encode("utf-8")).hexdigest()
    return int(digest[:8], 16)


def _format_price(value: float) -> str:
    # Passa per str/round prima di Decimal per evitare artefatti di rappresentazione binaria,
    # stesso principio di worker/market_data_source.py:_to_price.
    return str(Decimal(str(round(value, 5))))


def _candle_dict(symbol: str, timeframe: str, index: int) -> dict:
    step = TIMEFRAME_SECONDS[timeframe]
    open_time = _EPOCH + timedelta(seconds=index * step)
    seed = _deterministic_seed(symbol, timeframe)
    base = 1.0 + (seed % 1000) / 1000.0
    phase = (index + seed) * 0.15
    open_price = base + math.sin(phase) * 0.01
    close_price = base + math.sin(phase + 0.5) * 0.01
    wick_up = abs(math.sin(phase + 1.0)) * 0.004
    wick_down = abs(math.sin(phase + 1.5)) * 0.004
    high_price = max(open_price, close_price) + wick_up
    low_price = min(open_price, close_price) - wick_down

    return {
        "open_time": format_iso_utc(open_time),
        "open": _format_price(open_price),
        "high": _format_price(high_price),
        "low": _format_price(low_price),
        "close": _format_price(close_price),
        "tick_volume": 100 + (index % 50),
        "spread": 10 + (index % 5),
        "source": "mt5-bridge-fake",
    }


def _generate_candles(symbol: str, timeframe: str, since: Optional[datetime], now: datetime, limit: int) -> list:
    step = TIMEFRAME_SECONDS[timeframe]
    start_index = 0 if since is None else int((since - _EPOCH).total_seconds() // step) + 1
    # La candela "corrente" (in formazione a 'now') non e' mai completa: esclusa esattamente come
    # dovra' fare bridge/windows/mt5_bridge.py con un vero terminale MT5 (vedi quel modulo).
    last_complete_index = int((now - _EPOCH).total_seconds() // step) - 1

    candles = []
    index = start_index
    while len(candles) < limit and index <= last_complete_index:
        candles.append(_candle_dict(symbol, timeframe, index))
        index += 1
    return candles


def _trading_account() -> dict:
    return {
        "login": "123456",
        "server": "FakeBridge-Demo",
        "balance": 10000.0,
        "equity": 10010.0,
        "currency": "EUR",
        "leverage": 100,
    }


def _build_trading_steps() -> list:
    """Scenario cumulativo che produce esattamente i sette eventi attesi dal worker.

    Lo stato zero e' deliberatamente vuoto: consente al primo poll di stabilire una baseline
    senza inventare eventi. Ogni risposta successiva introduce una sola transizione osservabile.
    """
    position = {
        "ticket": "10001",
        "symbol": "EURUSD",
        "direction": "buy",
        "volume": 0.10,
        "open_price": 1.17000,
        "stop_loss": 1.16800,
        "take_profit": 1.17400,
        "open_time": "2026-07-13T10:00:00Z",
    }
    modified_sl = {**position, "stop_loss": 1.16900}
    modified_tp = {**modified_sl, "take_profit": 1.17500}
    deal = {
        "deal_ticket": "30001",
        "position_ticket": "10001",
        "close_price": 1.17200,
        "profit": 20.0,
        "commission": -0.5,
        "swap": 0.0,
        "close_time": "2026-07-13T10:15:00Z",
    }
    order = {
        "ticket": "20001",
        "symbol": "EURUSD",
        "direction": "buy",
        "volume": 0.10,
        "price": 1.16800,
        "stop_loss": 1.16600,
        "take_profit": 1.17200,
        "order_type": 2,
    }
    modified_order = {
        **order,
        "price": 1.16750,
        "stop_loss": 1.16550,
        "take_profit": 1.17250,
    }
    return [
        {"positions": [], "orders": [], "deals": []},
        {"positions": [position], "orders": [], "deals": []},
        {"positions": [modified_sl], "orders": [], "deals": []},
        {"positions": [modified_tp], "orders": [], "deals": []},
        {"positions": [], "orders": [], "deals": [deal]},
        {"positions": [], "orders": [order], "deals": [deal]},
        {"positions": [], "orders": [modified_order], "deals": [deal]},
        {"positions": [], "orders": [], "deals": [deal]},
    ]


class FakeTradingScenario:
    """Macchina a stati per il solo fake, isolata per istanza di server e thread-safe."""

    def __init__(self) -> None:
        self._steps = _build_trading_steps()
        self._index = 0
        self._lock = threading.Lock()

    def next_snapshot(self) -> dict:
        with self._lock:
            index = self._index
            state = copy.deepcopy(self._steps[index])
            if self._index < len(self._steps) - 1:
                self._index += 1
        return {
            "account": _trading_account(),
            **state,
            "generated_at": _TRADING_GENERATED_AT[index],
        }


class Handler(BaseBridgeHandler):
    trading_scenario: FakeTradingScenario

    def do_GET(self) -> None:
        if self.path != "/health":
            self.send_bridge_error(BridgeError(404, "not_found", f"Path non trovato: {self.path}"))
            return
        if not self.check_auth():
            return
        self.send_json(200, {
            "status": "ok",
            "terminal_connected": True,
            "account_connected": True,
            "server": "FakeBridge-Demo",
            "version": "fake-1.0",
        })

    def do_POST(self) -> None:
        if self.path not in ("/v1/candles", "/v1/trading/snapshot"):
            self.send_bridge_error(BridgeError(404, "not_found", f"Path non trovato: {self.path}"))
            return
        if not self.check_auth():
            return

        if self.path == "/v1/trading/snapshot":
            self._handle_trading_snapshot()
            return

        scenario = (self.headers.get("X-Mt5-Fake-Scenario") or "").strip().lower()

        if scenario == "timeout":
            time.sleep(TIMEOUT_SCENARIO_SLEEP_SECONDS)

        if scenario == "mt5_error":
            self.send_bridge_error(BridgeError(502, "mt5_error", "Simulazione: il terminale MT5 non risponde."))
            return

        if scenario == "malformed_payload":
            self._send_malformed_payload()
            return

        try:
            request = self.read_json_body()
            symbol, timeframe, since, now, limit = self.parse_candles_request(request)
        except BridgeError as exc:
            self.send_bridge_error(exc)
            return

        candles = _generate_candles(symbol, timeframe, since, now, limit)

        if scenario == "duplicate_candle" and candles:
            candles.append(dict(candles[-1]))

        self.send_json(200, {"symbol": symbol, "timeframe": timeframe, "candles": candles})

    def _handle_trading_snapshot(self) -> None:
        try:
            request = self.read_json_body()
            self.parse_trading_snapshot_request(request)
        except BridgeError as exc:
            self.send_bridge_error(exc)
            return
        self.send_json(200, self.trading_scenario.next_snapshot())

    def _send_malformed_payload(self) -> None:
        # Deliberatamente non conforme al contratto (open_time non ISO8601, open numerico invece
        # di stringa, nessun campo high/low/close): usato solo per verificare che
        # Mt5MarketDataSource rifiuti esplicitamente un payload cosi', invece di derivarne dati
        # silenziosamente sbagliati o una lista vuota.
        body = b'{"symbol": "EURUSD", "timeframe": "M1", "candles": [{"open_time": "not-a-date", "open": 1.1}]}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def make_config_from_env() -> BridgeConfig:
    return BridgeConfig(
        token=read_secret_from_env("MT5_BRIDGE_TOKEN"),
        broker_symbol=os.environ.get("EURUSD_BROKER_SYMBOL") or "EURUSD",
        port=int(os.environ.get("PORT", "8080")),
        host=os.environ.get("HOST", "0.0.0.0"),
    )


def make_server(config: BridgeConfig) -> ThreadingHTTPServer:
    handler_cls = type(
        "_FakeBridgeHandler",
        (Handler,),
        {"config": config, "trading_scenario": FakeTradingScenario()},
    )
    return ThreadingHTTPServer((config.host, config.port), handler_cls)


def main() -> None:
    config = make_config_from_env()
    server = make_server(config)
    print(f"[mt5-bridge-fake] in ascolto su {config.host}:{config.port} (broker_symbol={config.broker_symbol})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
