"""Normalizza un evento grezzo (da event_detector) nel payload atteso dall'ingestion API di
TradeJournal (vedi supabase/functions/trading-mt5-events + _shared/mt5EventProcessing.ts nel
repository principale -- consultato in sola lettura, non modificato).

Campi del payload, esattamente come richiesti dal contratto API:
event_id, event_type, platform, account_number, server, external_trade_id, symbol, direction,
volume, price, open_price, close_price, stop_loss, take_profit, previous_stop_loss,
previous_take_profit, profit, commission, swap, origin_order_ticket, order_type, open_time,
close_time, event_time. Gli eventi live possono inoltre includere lo snapshot account validato
(balance, equity, currency, leverage), senza usarlo nel fingerprint idempotente dell'evento.
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
        "order_type",
        "open_time",
        "origin_order_ticket",
    ),
    "trade_modified": (
        "stop_loss",
        "take_profit",
        "previous_stop_loss",
        "previous_take_profit",
    ),
    "trade_closed": (
        "close_price", "profit", "commission", "total_commission", "swap", "close_time"
    ),
    "trade_partial_closed": (
        "close_price", "profit", "commission", "swap", "close_time"
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
        "order_type",
    ),
    "pending_order_cancelled": ("symbol", "direction", "volume", "price", "order_type"),
    "pending_order_filled": ("symbol", "direction", "volume", "price", "order_type"),
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
    ("trade_opened", "trade_volume_changed", "trade_partial_closed", "trade_closed")
)


def validate_event_preflight(raw_event: Dict[str, Any]) -> None:
    """Fail closed before a malformed opening can be acked or dead-lettered.

    A temporary MT5 snapshot/cache race is recoverable only while its event
    file remains pending.  The ingestion API correctly rejects an opening
    without these lifecycle fields, but reaching it would turn that race into
    a permanent loss of the predecessor for a later close.
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
    account_part = account_number or "unknown"
    source_event_id = event.get("source_event_id")
    if isinstance(source_event_id, str) and source_event_id:
        # File-bridge events carry an identity built from immutable broker fields. Use it ahead
        # of the snapshot-derived fingerprint: the same DEAL_ADD may legitimately map from
        # trade_opened to trade_volume_changed when replayed against a newer snapshot, but it is
        # still the same source event and must retain the same ingestion id.
        source_digest = hashlib.sha256(
            f"{account_part}\x00{source_event_id}".encode("utf-8")
        ).hexdigest()[:24]
        return f"mt5-{account_part}-source-{source_digest}"

    event_type = event["event_type"]
    ticket = str(event.get("ticket", ""))
    # Preserve source_event_id precedence above.  It is the authoritative
    # overlap identity for current bridge files; native ticket identity adds
    # stable per-fill ids only when that source identity is unavailable.
    if event_type in _NATIVE_DEAL_EVENTS and event.get("native_deal_ticket") is not None:
        fields = ("native_deal_ticket",)
    else:
        fields = _FINGERPRINT_FIELDS.get(event_type, ())
    fingerprint_payload = {name: event.get(name) for name in fields}
    fingerprint_json = json.dumps(
        fingerprint_payload, sort_keys=True, separators=(",", ":"), default=str
    )
    digest = hashlib.sha256(fingerprint_json.encode("utf-8")).hexdigest()[:16]
    return f"mt5-{account_part}-{event_type}-{ticket}-{digest}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_event(
    raw_event: Dict[str, Any],
    account_number: Optional[str],
    server: Optional[str],
    platform: str = "mt5",
    account_snapshot: Optional[Dict[str, Any]] = None,
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
        "swap": event.get("swap"),
        "origin_order_ticket": event.get("origin_order_ticket"),
        "order_type": event.get("order_type"),
        "open_time": event.get("open_time"),
        "close_time": event.get("close_time"),
        "event_time": event_time,
    }
    # Prefer a per-fill account snapshot published by the EA.  The adapter
    # snapshot is a valid fallback for old builds, but must never overwrite the
    # balance captured alongside a live opening event.
    event_snapshot = {
        "balance": event.get("balance"),
        "equity": event.get("equity"),
        "currency": event.get("currency"),
        "leverage": event.get("leverage"),
    }
    if all(value is not None for value in event_snapshot.values()):
        payload.update(event_snapshot)
    elif account_snapshot is not None:
        payload.update(
            {
                "balance": account_snapshot["balance"],
                "equity": account_snapshot["equity"],
                "currency": account_snapshot["currency"],
                "leverage": account_snapshot.get("leverage"),
            }
        )
    # Extended MT5 economics/provenance are optional so old snapshot-only
    # producers retain the exact contract tested above.  ``fee`` is audit-only;
    # historical reconstruction has already folded it into commission and
    # total_commission exactly once when those certified values are present.
    for field in (
        "total_commission",
        "fee",
        "balance_before_open",
        "balance_before_open_source",
        "balance_before_open_reason",
        "native_deal_ticket",
        "time_msc",
        "time_basis",
        "commission_complete",
        "previous_volume",
        "partial_close",
    ):
        if event.get(field) is not None:
            payload[field] = event[field]
    return payload
