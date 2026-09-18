"""Normalizza un evento grezzo (da event_detector) nel payload atteso dall'ingestion API di
TradeJournal (vedi supabase/functions/trading-mt5-events + _shared/mt5EventProcessing.ts nel
repository principale -- consultato in sola lettura, non modificato).

Campi del payload, esattamente come richiesti dal contratto API:
event_id, event_type, platform, account_number, server, external_trade_id, symbol, direction,
volume, price, open_price, close_price, stop_loss, take_profit, previous_stop_loss,
previous_take_profit, profit, commission, total_commission, swap, open_time, close_time,
event_time.
When supplied by the native MT5 bridge, the sanitized account snapshot and the balance captured
immediately before opening are forwarded as optional fields.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from typing import Any, Dict, Optional

# Per tipo di evento, quali campi del raw event determinano l'identita' dell'evento stesso.
# Un evento con la stessa impronta produce lo stesso event_id (idempotenza sui retry); un
# cambiamento reale di stato (es. un secondo modify SL con un valore diverso) produce un
# fingerprint -- e quindi un event_id -- diverso.
_FINGERPRINT_FIELDS = {
    "trade_opened": (
        "symbol",
        "direction",
        "volume",
        "open_price",
        "stop_loss",
        "take_profit",
        "open_time",
    ),
    "trade_modified": (
        "stop_loss",
        "take_profit",
        "previous_stop_loss",
        "previous_take_profit",
    ),
    "trade_closed": (
        "native_deal_ticket", "time_msc", "volume",
        "close_price", "profit", "commission", "total_commission", "swap", "close_time",
        "mae_points", "mfe_points", "excursion_samples",
    ),
    "trade_partial_closed": (
        "native_deal_ticket", "time_msc", "volume",
        "close_price", "profit", "commission", "swap", "close_time",
    ),
    "pending_order_created": (
        "symbol",
        "direction",
        "volume",
        "price",
        "stop_loss",
        "take_profit",
    ),
    "pending_order_modified": (
        "price",
        "stop_loss",
        "take_profit",
        "previous_stop_loss",
        "previous_take_profit",
    ),
    "pending_order_cancelled": ("symbol", "direction", "volume", "price"),
    "trade_volume_changed": ("volume", "previous_volume", "partial_close"),
    "deal_recorded": (
        "position_ticket",
        "close_price",
        "profit",
        "commission",
        "swap",
        "close_time",
    ),
}

_NATIVE_DEAL_EVENTS = frozenset(
    (
        "trade_opened",
        "trade_volume_changed",
        "trade_partial_closed",
        "trade_closed",
    )
)


def validate_event_preflight(raw_event: Dict[str, Any]) -> None:
    """Reject malformed opens before they can acquire an outbox identity.

    The ingestion route requires these three lifecycle fields.  In particular, accepting an
    empty symbol locally would acknowledge the bridge file and turn a recoverable MT5 cache race
    into a permanent HTTP 422 dead-letter.
    """

    if raw_event.get("event_type") != "trade_opened":
        return

    symbol = raw_event.get("symbol")
    if (
        not isinstance(symbol, str)
        or not symbol
        or symbol != symbol.strip()
        or len(symbol) > 64
        or any(ord(character) < 32 for character in symbol)
    ):
        raise ValueError("trade_opened symbol unavailable")

    if raw_event.get("direction") not in ("buy", "sell"):
        raise ValueError("trade_opened direction unavailable")

    volume = raw_event.get("volume")
    try:
        numeric_volume = float(volume)
    except (OverflowError, TypeError, ValueError):
        numeric_volume = math.nan
    if (
        isinstance(volume, bool)
        or not isinstance(volume, (int, float))
        or not math.isfinite(numeric_volume)
        or numeric_volume <= 0.0
    ):
        raise ValueError("trade_opened volume unavailable")


def build_event_id(account_number: Optional[str], event: Dict[str, Any]) -> str:
    """Genera un event_id deterministico e idempotente.

    Stesso account + stesso tipo evento + stesso ticket + stesso fingerprint dei campi
    rilevanti => stesso event_id, cosi' che un retry (o un doppio invio dello stesso poll)
    venga deduplicato dall'API invece di creare un evento duplicato.
    """
    event_type = event["event_type"]
    ticket = str(event.get("ticket", ""))
    # DEAL_TICKET is immutable and unique inside one MT5 account.  Economics may be posted or
    # corrected after the transaction (especially commission/swap), so including those mutable
    # values would turn an enrichment of the same native fill into a second remote event.  Old
    # snapshot-only producers have no native identity and retain the legacy state fingerprint.
    if event_type in _NATIVE_DEAL_EVENTS and event.get("native_deal_ticket") is not None:
        fields = ("native_deal_ticket",)
    else:
        fields = _FINGERPRINT_FIELDS.get(event_type, ())
    fingerprint_payload = {name: event.get(name) for name in fields}
    fingerprint_json = json.dumps(
        fingerprint_payload, sort_keys=True, separators=(",", ":"), default=str
    )
    digest = hashlib.sha256(fingerprint_json.encode("utf-8")).hexdigest()[:16]
    account_part = account_number or "unknown"
    return f"mt5-{account_part}-{event_type}-{ticket}-{digest}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_event(
    raw_event: Dict[str, Any],
    account_number: Optional[str],
    server: Optional[str],
    platform: str = "mt5",
) -> Dict[str, Any]:
    """Converte un evento grezzo rilevato da event_detector nel payload dell'ingestion API."""
    validate_event_preflight(raw_event)
    event_time = raw_event.get("event_time") or _now_iso()
    event = {**raw_event, "event_time": event_time}

    payload = {
        "event_id": build_event_id(account_number, event),
        "event_type": event["event_type"],
        "platform": platform,
        "account_number": account_number,
        "server": server,
        "external_trade_id": str(event.get("ticket")),
        "symbol": event.get("symbol"),
        "direction": event.get("direction"),
        "volume": event.get("volume"),
        "price": event.get("price"),
        "open_price": event.get("open_price"),
        "close_price": event.get("close_price"),
        "stop_loss": event.get("stop_loss"),
        "take_profit": event.get("take_profit"),
        "previous_stop_loss": event.get("previous_stop_loss"),
        "previous_take_profit": event.get("previous_take_profit"),
        "profit": event.get("profit"),
        "commission": event.get("commission"),
        "total_commission": event.get("total_commission"),
        "swap": event.get("swap"),
        "open_time": event.get("open_time"),
        "close_time": event.get("close_time"),
        "event_time": event_time,
    }
    for field in (
        "mae_price_delta", "mfe_price_delta", "mae_points", "mfe_points",
        "mae_money", "mfe_money", "mae_pct", "mfe_pct", "mae_at", "mfe_at",
        "excursion_samples", "excursion_sample_ms", "excursion_source",
    ):
        if event.get(field) is not None:
            payload[field] = event[field]
    account_snapshot = {
        "balance": event.get("balance"),
        "equity": event.get("equity"),
        "currency": event.get("currency"),
        "leverage": event.get("leverage"),
    }
    if all(value is not None for value in account_snapshot.values()):
        payload.update(account_snapshot)
    if event.get("balance_before_open") is not None:
        payload["balance_before_open"] = event["balance_before_open"]
    if event.get("native_deal_ticket") is not None:
        payload["native_deal_ticket"] = str(event["native_deal_ticket"])
    if event.get("time_msc") is not None:
        payload["time_msc"] = event["time_msc"]
    if event.get("time_basis") is not None:
        payload["time_basis"] = event["time_basis"]
    if event.get("commission_complete") is not None:
        payload["commission_complete"] = bool(event["commission_complete"])
    if event.get("previous_volume") is not None:
        payload["previous_volume"] = event["previous_volume"]
    if event.get("partial_close") is not None:
        payload["partial_close"] = bool(event["partial_close"])
    # `commission` is already the effective MT5 cost (commission + fee).
    # Preserve the native fee separately for audit, never for a second sum.
    if event.get("fee") is not None:
        payload["fee"] = event["fee"]
    return payload
