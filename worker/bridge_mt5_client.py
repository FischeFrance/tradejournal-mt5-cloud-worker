"""Client HTTP read-only del trade-sync worker verso ``mt5-bridge``.

Il processo Linux non importa MetaTrader5: riceve account, posizioni, ordini e deal con una
singola richiesta a ``POST /v1/trading/snapshot``. L'account viene conservato insieme allo
snapshot, cosi' la successiva chiamata di ``main.py`` ad ``account_info()`` non effettua una
seconda richiesta e non puo' osservare uno stato appartenente a un poll diverso.
"""

from __future__ import annotations

import copy
import logging
import math
import time
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, Optional

import requests

from mt5_client import Mt5Client, Mt5ConnectionError

logger = logging.getLogger("mt5_worker.bridge_mt5_client")

_TOP_LEVEL_FIELDS = {"account", "positions", "orders", "deals", "generated_at"}
_ACCOUNT_FIELDS = {"login", "server", "balance", "equity", "currency", "leverage"}
_POSITION_FIELDS = {
    "ticket", "symbol", "direction", "volume", "open_price", "stop_loss", "take_profit", "open_time",
}
_ORDER_FIELDS = {
    "ticket", "symbol", "direction", "volume", "price", "stop_loss", "take_profit", "order_type",
}
_DEAL_FIELDS = {
    "deal_ticket", "position_ticket", "close_price", "profit", "commission", "swap", "close_time",
}
_HEALTH_FIELDS = {"status", "terminal_connected", "account_connected", "server", "version"}


def _require_object(value: Any, path: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise Mt5ConnectionError(f"Payload mt5-bridge non valido: '{path}' deve essere un oggetto JSON.")
    return value


def _require_exact_fields(value: Dict[str, Any], expected: set[str], path: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise Mt5ConnectionError(
            f"Payload mt5-bridge non valido: schema '{path}' inatteso "
            f"(campi mancanti={missing}, campi extra={extra})."
        )


def _require_string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value:
        raise Mt5ConnectionError(f"Payload mt5-bridge non valido: '{path}' deve essere una stringa non vuota.")
    return value


def _require_number(value: Any, path: str, *, positive: bool = False) -> Any:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise Mt5ConnectionError(f"Payload mt5-bridge non valido: '{path}' deve essere un numero finito.")
    if positive and value <= 0:
        raise Mt5ConnectionError(f"Payload mt5-bridge non valido: '{path}' deve essere positivo.")
    return value


def _require_integer(value: Any, path: str, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise Mt5ConnectionError(f"Payload mt5-bridge non valido: '{path}' deve essere un intero.")
    if positive and value <= 0:
        raise Mt5ConnectionError(f"Payload mt5-bridge non valido: '{path}' deve essere positivo.")
    return value


def _require_utc_timestamp(value: Any, path: str) -> str:
    text = _require_string(value, path)
    normalized = f"{text[:-1]}+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise Mt5ConnectionError(
            f"Payload mt5-bridge non valido: '{path}' non e' un timestamp ISO8601 valido."
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise Mt5ConnectionError(f"Payload mt5-bridge non valido: '{path}' deve essere in UTC.")
    return text


def _parse_account(raw: Any) -> Dict[str, Any]:
    account = _require_object(raw, "account")
    _require_exact_fields(account, _ACCOUNT_FIELDS, "account")
    return {
        "login": _require_string(account["login"], "account.login"),
        "server": _require_string(account["server"], "account.server"),
        "balance": _require_number(account["balance"], "account.balance"),
        "equity": _require_number(account["equity"], "account.equity"),
        "currency": _require_string(account["currency"], "account.currency"),
        "leverage": _require_integer(account["leverage"], "account.leverage", positive=True),
    }


def _parse_position(raw: Any, index: int) -> Dict[str, Any]:
    path = f"positions[{index}]"
    item = _require_object(raw, path)
    _require_exact_fields(item, _POSITION_FIELDS, path)
    direction = _require_string(item["direction"], f"{path}.direction")
    if direction not in ("buy", "sell"):
        raise Mt5ConnectionError(f"Payload mt5-bridge non valido: '{path}.direction' deve essere buy o sell.")
    return {
        "ticket": _require_string(item["ticket"], f"{path}.ticket"),
        "symbol": _require_string(item["symbol"], f"{path}.symbol"),
        "direction": direction,
        "volume": _require_number(item["volume"], f"{path}.volume", positive=True),
        "open_price": _require_number(item["open_price"], f"{path}.open_price"),
        "stop_loss": _require_number(item["stop_loss"], f"{path}.stop_loss"),
        "take_profit": _require_number(item["take_profit"], f"{path}.take_profit"),
        "open_time": _require_utc_timestamp(item["open_time"], f"{path}.open_time"),
    }


def _parse_order(raw: Any, index: int) -> Dict[str, Any]:
    path = f"orders[{index}]"
    item = _require_object(raw, path)
    _require_exact_fields(item, _ORDER_FIELDS, path)
    direction = _require_string(item["direction"], f"{path}.direction")
    if direction not in ("buy", "sell"):
        raise Mt5ConnectionError(f"Payload mt5-bridge non valido: '{path}.direction' deve essere buy o sell.")
    return {
        "ticket": _require_string(item["ticket"], f"{path}.ticket"),
        "symbol": _require_string(item["symbol"], f"{path}.symbol"),
        "direction": direction,
        "volume": _require_number(item["volume"], f"{path}.volume", positive=True),
        "price": _require_number(item["price"], f"{path}.price"),
        "stop_loss": _require_number(item["stop_loss"], f"{path}.stop_loss"),
        "take_profit": _require_number(item["take_profit"], f"{path}.take_profit"),
        "order_type": _require_integer(item["order_type"], f"{path}.order_type"),
    }


def _parse_deal(raw: Any, index: int) -> Dict[str, Any]:
    path = f"deals[{index}]"
    item = _require_object(raw, path)
    _require_exact_fields(item, _DEAL_FIELDS, path)
    return {
        "deal_ticket": _require_string(item["deal_ticket"], f"{path}.deal_ticket"),
        "position_ticket": _require_string(item["position_ticket"], f"{path}.position_ticket"),
        "close_price": _require_number(item["close_price"], f"{path}.close_price"),
        "profit": _require_number(item["profit"], f"{path}.profit"),
        "commission": _require_number(item["commission"], f"{path}.commission"),
        "swap": _require_number(item["swap"], f"{path}.swap"),
        "close_time": _require_utc_timestamp(item["close_time"], f"{path}.close_time"),
    }


def _index_items(raw: Any, path: str, parser: Callable[[Any, int], Dict[str, Any]], key: str) -> Dict[str, Dict[str, Any]]:
    if not isinstance(raw, list):
        raise Mt5ConnectionError(f"Payload mt5-bridge non valido: '{path}' deve essere un array JSON.")
    result: Dict[str, Dict[str, Any]] = {}
    for index, raw_item in enumerate(raw):
        item = parser(raw_item, index)
        ticket = item[key]
        if ticket in result:
            raise Mt5ConnectionError(f"Payload mt5-bridge non valido: ticket duplicato in '{path}'.")
        result[ticket] = item
    return result


def parse_trading_snapshot(raw: Any) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """Valida l'intero payload e lo converte nella forma indicizzata attesa dal detector."""
    data = _require_object(raw, "snapshot")
    _require_exact_fields(data, _TOP_LEVEL_FIELDS, "snapshot")
    account = _parse_account(data["account"])
    snapshot = {
        "positions": _index_items(data["positions"], "positions", _parse_position, "ticket"),
        "orders": _index_items(data["orders"], "orders", _parse_order, "ticket"),
        "deals": _index_items(data["deals"], "deals", _parse_deal, "deal_ticket"),
    }
    _require_utc_timestamp(data["generated_at"], "generated_at")
    return account, snapshot


class BridgeMt5Client(Mt5Client):
    """Implementazione di :class:`Mt5Client` che usa soltanto HTTP."""

    def __init__(
        self,
        bridge_url: Optional[str],
        bridge_token: Optional[str],
        timeout_seconds: float = 10.0,
        deal_lookback_hours: int = 24,
        max_retries: int = 3,
        backoff_base_seconds: float = 0.5,
        max_backoff_seconds: float = 8.0,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        if not bridge_url:
            raise ValueError("BridgeMt5Client richiede MT5_BRIDGE_URL non vuoto.")
        if not bridge_token:
            raise ValueError("BridgeMt5Client richiede MT5_BRIDGE_TOKEN non vuoto.")
        if timeout_seconds <= 0:
            raise ValueError("MT5_BRIDGE_TIMEOUT_SECONDS deve essere positivo.")
        if isinstance(deal_lookback_hours, bool) or not isinstance(deal_lookback_hours, int) or not 1 <= deal_lookback_hours <= 168:
            raise ValueError("MT5_DEAL_LOOKBACK_HOURS deve essere compreso tra 1 e 168.")
        if max_retries <= 0:
            raise ValueError("max_retries deve essere positivo.")

        self._bridge_url = bridge_url.rstrip("/")
        self._bridge_token = bridge_token
        self._timeout_seconds = timeout_seconds
        self._deal_lookback_hours = deal_lookback_hours
        self._max_retries = max_retries
        self._backoff_base_seconds = backoff_base_seconds
        self._max_backoff_seconds = max_backoff_seconds
        self._sleep = sleep_fn
        self._connected = False
        self._health_detail = "non connesso"
        self._account_cache: Optional[Dict[str, Any]] = None
        self._snapshot_cache: Optional[Dict[str, Any]] = None

    def _headers(self) -> Dict[str, str]:
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._bridge_token}",
        }

    def _request_json(self, method: str, path: str, payload: Optional[Dict[str, Any]] = None) -> Any:
        last_error = "errore transitorio"
        for attempt in range(1, self._max_retries + 1):
            try:
                response = requests.request(
                    method,
                    f"{self._bridge_url}{path}",
                    json=payload,
                    headers=self._headers(),
                    timeout=self._timeout_seconds,
                    allow_redirects=False,
                )
            except (
                requests.exceptions.InvalidSchema,
                requests.exceptions.InvalidURL,
                requests.exceptions.MissingSchema,
                requests.exceptions.TooManyRedirects,
            ) as exc:
                raise Mt5ConnectionError(
                    f"Configurazione MT5_BRIDGE_URL non valida ({type(exc).__name__}); "
                    "nessun retry eseguito."
                ) from exc
            except requests.RequestException as exc:
                last_error = type(exc).__name__
                logger.warning(
                    "Tentativo %s/%s verso mt5-bridge fallito per errore di rete (%s).",
                    attempt,
                    self._max_retries,
                    last_error,
                )
            else:
                if response.status_code == 200:
                    try:
                        return response.json()
                    except ValueError as exc:
                        raise Mt5ConnectionError("mt5-bridge ha restituito JSON non valido.") from exc
                if 500 <= response.status_code <= 599:
                    last_error = f"http_{response.status_code}"
                    logger.warning(
                        "Tentativo %s/%s verso mt5-bridge fallito (status=%s).",
                        attempt,
                        self._max_retries,
                        response.status_code,
                    )
                else:
                    raise Mt5ConnectionError(
                        f"mt5-bridge ha rifiutato la richiesta (status={response.status_code}, non ritentabile)."
                    )

            if attempt < self._max_retries:
                backoff = min(self._backoff_base_seconds * (2 ** (attempt - 1)), self._max_backoff_seconds)
                self._sleep(backoff)

        raise Mt5ConnectionError(
            f"mt5-bridge non raggiungibile dopo {self._max_retries} tentativi ({last_error})."
        )

    def connect(self) -> None:
        self._connected = False
        self._health_detail = "connessione"
        raw = self._request_json("GET", "/health")
        health = _require_object(raw, "health")
        _require_exact_fields(health, _HEALTH_FIELDS, "health")
        _require_string(health["status"], "health.status")
        if not isinstance(health["terminal_connected"], bool) or not isinstance(health["account_connected"], bool):
            raise Mt5ConnectionError("Payload mt5-bridge non valido: stato health non booleano.")
        _require_string(health["server"], "health.server")
        _require_string(health["version"], "health.version")
        if health["status"] != "ok" or not health["terminal_connected"] or not health["account_connected"]:
            raise Mt5ConnectionError("mt5-bridge risponde ma il terminale/account MT5 non e' disponibile.")
        self._connected = True
        self._health_detail = "ok"

    def reconnect(self) -> None:
        self._connected = False
        self._health_detail = "riconnessione"
        self.connect()

    def health_status(self) -> Dict[str, Any]:
        return {"connected": self._connected, "detail": self._health_detail}

    def snapshot(self) -> Dict[str, Any]:
        self._ensure_connected()
        raw = self._request_json(
            "POST",
            "/v1/trading/snapshot",
            {"deal_lookback_hours": self._deal_lookback_hours},
        )
        account, snapshot = parse_trading_snapshot(raw)
        self._account_cache = account
        self._snapshot_cache = snapshot
        return copy.deepcopy(snapshot)

    def account_info(self) -> Dict[str, Any]:
        self._ensure_snapshot_available()
        assert self._account_cache is not None
        return copy.deepcopy(self._account_cache)

    def get_open_positions(self) -> Dict[str, Dict[str, Any]]:
        return self._cached_snapshot_part("positions")

    def get_recent_deals(self) -> Dict[str, Dict[str, Any]]:
        return self._cached_snapshot_part("deals")

    def get_pending_orders(self) -> Dict[str, Dict[str, Any]]:
        return self._cached_snapshot_part("orders")

    def _cached_snapshot_part(self, name: str) -> Dict[str, Dict[str, Any]]:
        self._ensure_snapshot_available()
        assert self._snapshot_cache is not None
        return copy.deepcopy(self._snapshot_cache[name])

    def _ensure_connected(self) -> None:
        if not self._connected:
            raise Mt5ConnectionError("BridgeMt5Client non connesso: chiamare connect() prima.")

    def _ensure_snapshot_available(self) -> None:
        self._ensure_connected()
        if self._snapshot_cache is None or self._account_cache is None:
            raise Mt5ConnectionError("Nessuno snapshot disponibile: chiamare snapshot() prima.")
