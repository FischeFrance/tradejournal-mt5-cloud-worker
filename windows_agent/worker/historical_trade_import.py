"""Reconstruct journal trades from MT5 deal history.

MT5 stores executions (deals), while TradeJournal stores one review row per
position. This module folds all entry/exit deals for a position into one
deterministic open event and, when applicable, one deterministic close event.
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any, Iterable

from worker.event_normalizer import normalize_event


_ENTRY_IN = frozenset(("0", "IN"))
_ENTRY_OUT = frozenset(("1", "2", "3", "OUT", "INOUT", "OUT_BY"))


def _number(value: object, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def _entry(value: object) -> str:
    return str(value).strip().upper() if value is not None else ""


def _time(row: dict[str, Any]) -> str:
    value = row.get("time", row.get("close_time"))
    return str(value) if value is not None else ""


def _weighted_price(rows: list[dict[str, Any]]) -> float | None:
    total = sum(max(0.0, _number(row.get("volume"))) for row in rows)
    if total <= 0:
        return None
    return sum(
        max(0.0, _number(row.get("volume"))) * _number(row.get("price"))
        for row in rows
    ) / total


def build_historical_trade_events(
    deals: Iterable[dict[str, Any]],
    account_number: str,
    server: str,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    skipped = 0
    for source in deals:
        row = dict(source)
        position_id = row.get("position_id", row.get("position_ticket"))
        symbol = str(row.get("symbol", "")).strip()
        if position_id in (None, "", 0, "0") or not symbol:
            skipped += 1
            continue
        groups[str(position_id)].append(row)

    events: list[dict[str, Any]] = []
    imported_positions = 0
    for position_id, rows in groups.items():
        rows.sort(key=lambda row: (_time(row), str(row.get("ticket", ""))))
        entries = [row for row in rows if _entry(row.get("entry")) in _ENTRY_IN]
        exits = [row for row in rows if _entry(row.get("entry")) in _ENTRY_OUT]
        if not entries:
            skipped += len(rows)
            continue

        first = entries[0]
        direction = str(first.get("direction", "")).strip().lower()
        if direction not in ("buy", "sell"):
            deal_type = first.get("type")
            direction = "buy" if deal_type in (0, "0") else "sell"
        open_time = _time(first)
        open_volume = sum(max(0.0, _number(row.get("volume"))) for row in entries)
        open_price = _weighted_price(entries)
        if not open_time or open_volume <= 0 or open_price is None:
            skipped += len(rows)
            continue

        open_raw = {
            "event_type": "trade_opened",
            "ticket": position_id,
            "symbol": str(first["symbol"]),
            "direction": direction,
            "volume": open_volume,
            "open_price": open_price,
            "open_time": open_time,
            "event_time": open_time,
        }
        events.append(normalize_event(open_raw, account_number, server))

        if exits:
            close_time = max(_time(row) for row in exits)
            close_price = _weighted_price(exits)
            if close_time and close_price is not None:
                close_raw = {
                    "event_type": "trade_closed",
                    "ticket": position_id,
                    "symbol": str(first["symbol"]),
                    "direction": direction,
                    "volume": open_volume,
                    "close_price": close_price,
                    "profit": sum(_number(row.get("profit")) for row in rows),
                    "commission": sum(_number(row.get("commission")) for row in rows),
                    "swap": sum(_number(row.get("swap")) for row in rows),
                    "close_time": close_time,
                    "event_time": close_time,
                }
                events.append(normalize_event(close_raw, account_number, server))
        imported_positions += 1

    events.sort(key=lambda event: (str(event["event_time"]), event["event_type"] == "trade_closed"))
    return events, {
        "positions": imported_positions,
        "events": len(events),
        "skipped_deals": skipped,
    }
