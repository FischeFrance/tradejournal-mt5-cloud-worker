"""Local MAE/MFE accumulator for managed MT5 positions.

The tracker intentionally lives on the VPS.  It observes the two-second bridge snapshots and
only enriches the existing ``trade_closed`` event; no sample is sent to Supabase.  The persisted
state also lets an open position survive an Agent restart without resetting its extrema.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any


_VERSION = 1


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed and abs(parsed) != float("inf") else None


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class PositionExcursionTracker:
    """Accumulates one compact record per open position.

    ``store`` follows the same ``get()/save(dict)`` protocol as PersistentSnapshot.  Keeping the
    persistence dependency injected makes the calculation independently testable.
    """

    def __init__(self, store: Any | None = None, sample_ms: int = 2_000) -> None:
        self.store = store
        raw = store.get() if store is not None else {}
        positions = raw.get("positions", {}) if isinstance(raw, dict) else {}
        self.positions: dict[str, dict[str, Any]] = (
            deepcopy(positions) if isinstance(positions, dict) else {}
        )
        self.sample_ms = max(250, int(sample_ms))

    def _persist(self) -> None:
        if self.store is not None:
            self.store.save({"version": _VERSION, "positions": self.positions})

    def observe_open_events(self, events: list[dict[str, Any]]) -> None:
        changed = False
        for event in events:
            if event.get("event_type") != "trade_opened":
                continue
            ticket = str(event.get("ticket") or "")
            if not ticket:
                continue
            state = self.positions.setdefault(ticket, {})
            for key in ("symbol", "direction", "open_price", "balance_before_open"):
                value = event.get(key)
                if value is not None and state.get(key) is None:
                    state[key] = value
                    changed = True
            state.setdefault("samples", 0)
        if changed:
            self._persist()

    def observe_snapshot(self, snapshot: dict[str, Any]) -> None:
        changed = False
        observed_at = str(snapshot.get("generated_at") or _iso_now())
        for ticket, position in snapshot.get("positions", {}).items():
            if not isinstance(position, dict):
                continue
            # DEAL_POSITION_ID maps to MT5 POSITION_IDENTIFIER. POSITION_TICKET can change after
            # some broker operations (for example swap reopening), so the stable identifier is
            # the key that must meet the eventual close event.
            ticket_text = str(position.get("position_id") or ticket)
            state = self.positions.setdefault(ticket_text, {})
            for key in ("symbol", "direction", "open_price", "point", "digits"):
                value = position.get(key)
                if value is not None:
                    state[key] = value

            open_price = _number(state.get("open_price"))
            current_price = _number(position.get("current_price"))
            point = _number(position.get("point"))
            profit = _number(position.get("floating_profit"))
            if open_price is None or current_price is None:
                continue

            direction = str(state.get("direction") or "").lower()
            factor = -1.0 if direction in ("sell", "short") else 1.0
            favorable_delta = (current_price - open_price) * factor
            self._observe_value(state, favorable_delta, point, profit, observed_at)
            changed = True
        if changed:
            self._persist()

    @staticmethod
    def _observe_value(
        state: dict[str, Any],
        favorable_delta: float,
        point: float | None,
        profit: float | None,
        observed_at: str,
    ) -> None:
        previous_mfe = _number(state.get("mfe_price_delta")) or 0.0
        previous_mae = _number(state.get("mae_price_delta")) or 0.0
        favorable = max(0.0, favorable_delta)
        adverse = max(0.0, -favorable_delta)
        if favorable >= previous_mfe:
            state["mfe_price_delta"] = favorable
            state["mfe_at"] = observed_at
        if adverse >= previous_mae:
            state["mae_price_delta"] = adverse
            state["mae_at"] = observed_at
        if point is not None and point > 0:
            state["mfe_points"] = (_number(state.get("mfe_price_delta")) or 0.0) / point
            state["mae_points"] = (_number(state.get("mae_price_delta")) or 0.0) / point
        if profit is not None:
            state["mfe_money"] = max(_number(state.get("mfe_money")) or 0.0, profit, 0.0)
            state["mae_money"] = max(_number(state.get("mae_money")) or 0.0, -profit, 0.0)
        state["samples"] = int(state.get("samples") or 0) + 1

    def enrich_close_events(self, events: list[dict[str, Any]]) -> list[str]:
        closed: list[str] = []
        for event in events:
            if event.get("event_type") != "trade_closed":
                continue
            ticket = str(event.get("ticket") or "")
            state = self.positions.get(ticket)
            if not ticket or not state:
                continue

            open_price = _number(state.get("open_price"))
            close_price = _number(event.get("close_price"))
            if open_price is not None and close_price is not None:
                direction = str(state.get("direction") or event.get("direction") or "").lower()
                factor = -1.0 if direction in ("sell", "short") else 1.0
                self._observe_value(
                    state,
                    (close_price - open_price) * factor,
                    _number(state.get("point")),
                    _number(event.get("profit")),
                    str(event.get("close_time") or event.get("event_time") or _iso_now()),
                )

            balance = _number(state.get("balance_before_open"))
            event.update(
                {
                    "mae_price_delta": _number(state.get("mae_price_delta")),
                    "mfe_price_delta": _number(state.get("mfe_price_delta")),
                    "mae_points": _number(state.get("mae_points")),
                    "mfe_points": _number(state.get("mfe_points")),
                    "mae_money": _number(state.get("mae_money")),
                    "mfe_money": _number(state.get("mfe_money")),
                    "mae_pct": (
                        (_number(state.get("mae_money")) or 0.0) / balance * 100.0
                        if balance is not None and balance > 0
                        else None
                    ),
                    "mfe_pct": (
                        (_number(state.get("mfe_money")) or 0.0) / balance * 100.0
                        if balance is not None and balance > 0
                        else None
                    ),
                    "mae_at": state.get("mae_at"),
                    "mfe_at": state.get("mfe_at"),
                    "excursion_samples": int(state.get("samples") or 0),
                    "excursion_sample_ms": self.sample_ms,
                    "excursion_source": "mt5_snapshot",
                }
            )
            closed.append(ticket)
        return closed

    def discard(self, tickets: list[str]) -> None:
        changed = False
        for ticket in tickets:
            if self.positions.pop(str(ticket), None) is not None:
                changed = True
        if changed:
            self._persist()
