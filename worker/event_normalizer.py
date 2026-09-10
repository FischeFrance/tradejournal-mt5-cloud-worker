"""Normalizza un evento grezzo (da event_detector) nel payload atteso dall'ingestion API di
TradeJournal (vedi supabase/functions/trading-mt5-events + _shared/mt5EventProcessing.ts nel
repository principale -- consultato in sola lettura, non modificato).

Campi del payload, esattamente come richiesti dal contratto API:
event_id, event_type, platform, account_number, server, external_trade_id, symbol, direction,
volume, price, open_price, close_price, stop_loss, take_profit, previous_stop_loss,
previous_take_profit, profit, commission, swap, origin_order_ticket, order_type, open_time,
close_time, event_time.
"""

from __future__ import annotations

import hashlib
import json
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
    "trade_closed": ("close_price", "profit", "commission", "swap", "close_time"),
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
) -> Dict[str, Any]:
    """Converte un evento grezzo rilevato da event_detector nel payload dell'ingestion API."""
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
    return payload
