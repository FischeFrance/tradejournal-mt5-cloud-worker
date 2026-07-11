"""Client MT5 simulato: nessuna dipendenza da Wine/MT5, usato quando MOCK_MODE=true.

Espone la stessa interfaccia di `mt5_client.Mt5Client` e riproduce, un passo alla volta (un
avanzamento per ogni ciclo di poll, tramite `tick()`), la sequenza di eventi richiesta:

    1. trade_opened
    2. trade_modified (modifica SL)
    3. trade_modified (modifica TP)
    4. trade_closed
    5. pending_order_created
    6. pending_order_modified
    7. pending_order_cancelled

Raggiunto l'ultimo passo, lo stato resta stabile (nessun nuovo evento) finche' il processo non
viene riavviato -- e' un mock deterministico per test/demo, non un generatore infinito di
rumore casuale.
"""

from __future__ import annotations

import copy
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from mt5_client import Mt5Client

logger = logging.getLogger("mt5_worker.mock_mt5_client")

_SYMBOL = "EURUSD"
_POSITION_TICKET = "900001"
_DEAL_TICKET = "800001"
_ORDER_TICKET = "900002"


def _iso(offset_seconds: int = 0) -> str:
    return (datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=offset_seconds)).isoformat()


def _empty_snapshot() -> Dict[str, Any]:
    return {"positions": {}, "orders": {}, "deals": {}}


def _build_scenario() -> List[Dict[str, Any]]:
    """Costruisce la lista di snapshot cumulativi, uno per ciascuna fase dello scenario.

    L'indice 0 e' lo stato iniziale (vuoto), usato come base per rilevare il primo evento.
    """
    steps: List[Dict[str, Any]] = [_empty_snapshot()]

    # 1. trade_opened
    state = copy.deepcopy(steps[-1])
    state["positions"][_POSITION_TICKET] = {
        "ticket": _POSITION_TICKET,
        "symbol": _SYMBOL,
        "direction": "buy",
        "volume": 0.10,
        "open_price": 1.10500,
        "stop_loss": 1.10000,
        "take_profit": 1.11500,
        "open_time": _iso(0),
    }
    steps.append(state)

    # 2. trade_modified: modifica SL
    state = copy.deepcopy(steps[-1])
    state["positions"][_POSITION_TICKET]["stop_loss"] = 1.10200
    steps.append(state)

    # 3. trade_modified: modifica TP
    state = copy.deepcopy(steps[-1])
    state["positions"][_POSITION_TICKET]["take_profit"] = 1.11800
    steps.append(state)

    # 4. trade_closed
    state = copy.deepcopy(steps[-1])
    del state["positions"][_POSITION_TICKET]
    state["deals"][_DEAL_TICKET] = {
        "position_ticket": _POSITION_TICKET,
        "close_price": 1.11800,
        "profit": 130.0,
        "commission": -2.0,
        "swap": -0.5,
        "close_time": _iso(3600),
    }
    steps.append(state)

    # 5. pending_order_created
    state = copy.deepcopy(steps[-1])
    state["orders"][_ORDER_TICKET] = {
        "ticket": _ORDER_TICKET,
        "symbol": _SYMBOL,
        "direction": "buy",
        "volume": 0.05,
        "price": 1.09500,
        "stop_loss": 1.09000,
        "take_profit": 1.10500,
        "order_type": "buy_limit",
    }
    steps.append(state)

    # 6. pending_order_modified
    state = copy.deepcopy(steps[-1])
    state["orders"][_ORDER_TICKET]["stop_loss"] = 1.09200
    state["orders"][_ORDER_TICKET]["take_profit"] = 1.10700
    steps.append(state)

    # 7. pending_order_cancelled
    state = copy.deepcopy(steps[-1])
    del state["orders"][_ORDER_TICKET]
    steps.append(state)

    return steps


class MockMt5Client(Mt5Client):
    def __init__(self, login: str = "99999999", server: str = "MockServer-Demo") -> None:
        self._login = login
        self._server = server
        self._steps = _build_scenario()
        self._index = 0
        self._connected = False

    @property
    def is_scenario_finished(self) -> bool:
        return self._index >= len(self._steps) - 1

    def connect(self) -> None:
        self._connected = True
        logger.info("MockMt5Client connesso (nessun terminale MT5 reale coinvolto).")

    def reconnect(self) -> None:
        logger.info("MockMt5Client: riconnessione simulata (sempre riuscita).")
        self._connected = True

    def health_status(self) -> Dict[str, Any]:
        return {"connected": self._connected, "detail": "mock"}

    def account_info(self) -> Dict[str, Any]:
        return {
            "login": self._login,
            "server": self._server,
            "balance": 10000.0,
            "equity": 10000.0,
            "currency": "USD",
            "leverage": 100,
        }

    def get_open_positions(self) -> Dict[str, Dict[str, Any]]:
        return copy.deepcopy(self._steps[self._index]["positions"])

    def get_recent_deals(self) -> Dict[str, Dict[str, Any]]:
        return copy.deepcopy(self._steps[self._index]["deals"])

    def get_pending_orders(self) -> Dict[str, Dict[str, Any]]:
        return copy.deepcopy(self._steps[self._index]["orders"])

    def tick(self) -> None:
        if not self.is_scenario_finished:
            self._index += 1
            logger.debug("MockMt5Client: avanzato al passo %s/%s", self._index, len(self._steps) - 1)
