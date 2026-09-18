"""Deterministic MT5 historical-ledger reconstruction.

The current account balance is an authoritative *end* anchor.  Walking every
balance-affecting deal backwards yields the balance immediately before an
opening deal, including overlapping positions and deposits/withdrawals.  We
only publish a value when the exported ledger contains the precision and
economics needed to prove it; otherwise the affected opening is explicitly
marked unavailable.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

TRADE_DEAL_TYPES = frozenset({0, 1})
CREDIT_DEAL_TYPE = 3
CANCELED_TRADE_DEAL_TYPES = frozenset({13, 14})
# Explicit MT5 ENUM_DEAL_TYPE values whose current fields still describe their complete account
# effect. BUY_CANCELED/SELL_CANCELED are deliberately excluded: MT5 mutates the original deal
# and posts a separate balance compensation, so the original historical effect is no longer
# recoverable from the current ledger. Unknown future enum values are fail-closed too.
KNOWN_BALANCE_DEAL_TYPES = frozenset(
    {0, 1, 2, 4, 5, 6, 7, 8, 9, 10, 11, 12, 15, 16, 17}
)
# MT5 can post commission as a dedicated balance deal instead of attaching it to BUY/SELL.
# Only an explicit position/order link is safe for per-trade attribution; account-level daily or
# monthly rows without such a link make net per-trade economics unavailable.
SEPARATE_COMMISSION_DEAL_TYPES = frozenset({7, 8, 9, 10, 11})
VALID_ENTRIES = frozenset({"0", "1", "2", "3", "IN", "OUT", "INOUT", "OUT_BY"})
OPEN_ENTRIES = frozenset({"0", "IN"})
EXIT_ENTRIES = frozenset({"1", "2", "3", "OUT", "INOUT", "OUT_BY"})
ECONOMIC_FIELDS = ("profit", "commission", "swap", "fee")
VOLUME_TOLERANCE = Decimal("0.00000001")
BALANCE_LEDGER_SNAPSHOT_PREFIX = "history-balance-ledger-"


def balance_ledger_snapshot_filename(job_id: str, snapshot_sha256: str) -> str:
    job_key = hashlib.sha256(job_id.encode("utf-8")).hexdigest()[:16]
    return f"{BALANCE_LEDGER_SNAPSHOT_PREFIX}{job_key}-{snapshot_sha256}.json"


def balance_ledger_snapshot_bytes(document: dict[str, Any]) -> bytes:
    """Return the exact canonical bytes persisted by ``state_store.atomic_json``."""

    return (
        json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def balance_ledger_snapshot_sha256(document: dict[str, Any]) -> str:
    return hashlib.sha256(balance_ledger_snapshot_bytes(document)).hexdigest()


def build_balance_ledger_snapshot(
    rows: Iterable[dict[str, Any]],
    *,
    connection_id: str,
    job_id: str,
    account_number: str,
    server: str,
    through: str,
    captured_at_utc: str,
    anchor: dict[str, Any],
) -> dict[str, Any]:
    """Freeze the exact full-ledger projection used by privileged repair tooling."""

    materialized = [dict(row) for row in rows]
    return {
        "schema_version": 1,
        "job_id": job_id,
        "connection_id": connection_id,
        "account_number": account_number,
        "server": server,
        "history_mode": "all_available",
        "through": through,
        "captured_at_utc": captured_at_utc,
        "deal_count": len(materialized),
        "anchor": dict(anchor),
        "rows": materialized,
    }


def _decimal(value: object) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


def _number(value: Decimal) -> float:
    # The wire contract carries JSON numbers. Preserve the exported MT5 precision here and let
    # presentation code round for display; quantizing every ledger step would accumulate drift.
    return float(value)


def _ticket(row: dict[str, Any]) -> int | None:
    try:
        value = int(str(row.get("ticket", "")))
    except ValueError:
        return None
    return value if value > 0 else None


def _deal_type(row: dict[str, Any]) -> int | None:
    try:
        return int(row["deal_type"])
    except (KeyError, TypeError, ValueError):
        return None


def _effect(row: dict[str, Any]) -> tuple[Decimal | None, Decimal | None]:
    """Return (balance_effect, credit_effect), preserving unknown as ``None``."""
    deal_type = _deal_type(row)
    values = [_decimal(row.get(field)) for field in ECONOMIC_FIELDS]
    if deal_type is None or any(value is None for value in values):
        return None, None
    total = sum((value for value in values if value is not None), Decimal("0"))
    if deal_type == CREDIT_DEAL_TYPE:
        return Decimal("0"), total
    if deal_type in KNOWN_BALANCE_DEAL_TYPES:
        return total, Decimal("0")
    return None, None


def reconstruct_trade_deals(
    deals: Iterable[dict[str, Any]], anchor: dict[str, Any] | None
) -> tuple[dict[str, Any], ...]:
    """Enrich trade deals with opening balance and full-position costs.

    No estimate is made. Missing fee/economic fields or an incoherent anchor
    makes only the causally affected openings unavailable. Ledger order is the
    sequence returned by MT5, not broker wall-clock time.
    """
    rows = [dict(row) for row in deals]
    indexed = list(enumerate(rows))
    balance = _decimal((anchor or {}).get("balance"))
    credit = _decimal((anchor or {}).get("credit"))
    anchor_valid = (
        balance is not None
        and credit is not None
        and (anchor or {}).get("coherent") is True
    )

    before_by_index: dict[int, tuple[Decimal | None, str]] = {}
    if not anchor_valid:
        for index, _ in indexed:
            before_by_index[index] = (None, "anchor_unavailable")
    else:
        assert balance is not None and credit is not None
        # HistoryDealGetTicket(index) already supplies the authoritative ledger
        # sequence. Broker server timestamps can repeat or move backwards at a
        # DST transition, so they are audit/identity data, never the ordering key.
        running_balance, running_credit = balance, credit
        ledger_known = True
        for index, row in reversed(indexed):
            balance_effect, credit_effect = _effect(row)
            if balance_effect is None or credit_effect is None:
                before_by_index[index] = (None, "economics_incomplete")
                ledger_known = False
                continue
            if not ledger_known:
                before_by_index[index] = (None, "ledger_order_or_economics_incomplete")
                continue
            before_balance = running_balance - balance_effect
            running_credit -= credit_effect
            before_by_index[index] = (before_balance, "mt5_ledger_exact")
            running_balance = before_balance

    costs_by_position: dict[str, Decimal] = {}
    costs_complete: dict[str, bool] = {}
    for row in rows:
        position_id = str(row.get("position_id", "")).strip()
        if not position_id or position_id == "0" or _deal_type(row) not in TRADE_DEAL_TYPES:
            continue
        commission, fee = _decimal(row.get("commission")), _decimal(row.get("fee"))
        complete = commission is not None and fee is not None
        costs_complete[position_id] = costs_complete.get(position_id, True) and complete
        if complete:
            costs_by_position[position_id] = costs_by_position.get(position_id, Decimal("0")) + commission + fee

    position_indices: dict[str, list[int]] = {}
    for index, row in indexed:
        position_id = str(row.get("position_id", "")).strip()
        if position_id and position_id != "0" and _deal_type(row) in TRADE_DEAL_TYPES:
            position_indices.setdefault(position_id, []).append(index)

    order_positions: dict[str, set[str]] = {}
    for position_id, indices in position_indices.items():
        for index in indices:
            order_id = str(rows[index].get("order_id", "")).strip()
            if order_id and order_id != "0":
                order_positions.setdefault(order_id, set()).add(position_id)

    unresolved_separate_commission = False
    for row in rows:
        if _deal_type(row) not in SEPARATE_COMMISSION_DEAL_TYPES:
            continue
        values = [_decimal(row.get(field)) for field in ECONOMIC_FIELDS]
        if any(value is None for value in values):
            unresolved_separate_commission = True
            continue
        amount = sum((value for value in values if value is not None), Decimal("0"))
        if amount == 0:
            continue
        position_id = str(row.get("position_id", "")).strip()
        target = position_id if position_id in position_indices else None
        if target is None:
            order_id = str(row.get("order_id", "")).strip()
            candidates = order_positions.get(order_id, set())
            if len(candidates) == 1:
                target = next(iter(candidates))
        if target is None:
            unresolved_separate_commission = True
            continue
        costs_by_position[target] = costs_by_position.get(target, Decimal("0")) + amount
        costs_complete[target] = costs_complete.get(target, True)

    if unresolved_separate_commission:
        for position_id in position_indices:
            costs_complete[position_id] = False

    first_open_index: dict[str, int] = {}
    opening_state: dict[int, dict[str, float]] = {}
    exit_event_type: dict[int, str] = {}
    unsafe_position: dict[str, str] = {}
    for position_id, indices in position_indices.items():
        opening_indices = [
            index
            for index in indices
            if str(rows[index].get("entry", "")).strip().upper() in OPEN_ENTRIES
        ]
        if not opening_indices:
            unsafe_position[position_id] = "opening_deal_unavailable"
            continue
        if any(_ticket(rows[index]) is None for index in indices):
            unsafe_position[position_id] = "deal_identity_unavailable"
            continue
        first_open_index[position_id] = opening_indices[0]
        if any(
            str(rows[index].get("entry", "")).strip().upper() == "INOUT"
            for index in indices
        ):
            unsafe_position[position_id] = "inout_reversal_not_reconstructible"
            continue
        exit_indices = [
            index
            for index in indices
            if str(rows[index].get("entry", "")).strip().upper() in EXIT_ENTRIES
        ]
        directions = {str(rows[index].get("direction", "")).lower() for index in opening_indices}
        volumes = [_decimal(rows[index].get("volume")) for index in opening_indices]
        prices = [_decimal(rows[index].get("price")) for index in opening_indices]
        opening_economics = {
            field: [_decimal(rows[index].get(field)) for index in opening_indices]
            for field in ECONOMIC_FIELDS
        }
        if (
            len(directions) != 1
            or any(value is None or value <= 0 for value in volumes)
            or any(value is None for value in prices)
            or any(value is None for values in opening_economics.values() for value in values)
        ):
            unsafe_position[position_id] = "opening_aggregation_incomplete"
            continue
        exit_volumes = [_decimal(rows[index].get("volume")) for index in exit_indices]
        if any(value is None or value <= 0 for value in exit_volumes):
            unsafe_position[position_id] = "exit_volume_incomplete"
            continue

        opening_volume_by_index = dict(zip(opening_indices, volumes))
        opening_price_by_index = dict(zip(opening_indices, prices))
        exit_volume_by_index = dict(zip(exit_indices, exit_volumes))
        current_volume = Decimal("0")
        current_notional = Decimal("0")
        position_closed = False
        for index in indices:
            entry = str(rows[index].get("entry", "")).strip().upper()
            if entry in OPEN_ENTRIES:
                opening_volume = opening_volume_by_index[index]
                opening_price = opening_price_by_index[index]
                assert opening_volume is not None and opening_price is not None
                if position_closed:
                    unsafe_position[position_id] = "position_reopened_not_reconstructible"
                    break
                previous_volume = current_volume
                current_volume += opening_volume
                current_notional += opening_volume * opening_price
                opening_state[index] = {
                    "previous_volume": _number(previous_volume),
                    "volume": _number(current_volume),
                    "open_price": _number(current_notional / current_volume),
                }
                continue
            if entry not in EXIT_ENTRIES:
                continue
            exit_volume = exit_volume_by_index[index]
            assert exit_volume is not None
            if (
                position_closed
                or current_volume <= VOLUME_TOLERANCE
                or exit_volume > current_volume + VOLUME_TOLERANCE
            ):
                unsafe_position[position_id] = "exit_volume_incoherent"
                break
            if abs(exit_volume - current_volume) <= VOLUME_TOLERANCE:
                exit_event_type[index] = "trade_closed"
                position_closed = True
                current_volume = Decimal("0")
                current_notional = Decimal("0")
            else:
                exit_event_type[index] = "trade_partial_closed"
                average_price = current_notional / current_volume
                current_volume -= exit_volume
                current_notional = average_price * current_volume
        if position_id in unsafe_position:
            for exit_index in exit_indices:
                exit_event_type.pop(exit_index, None)

    projected: list[dict[str, Any]] = []
    for index, row in indexed:
        position_id = str(row.get("position_id", "")).strip()
        entry = str(row.get("entry", "")).strip().upper()
        symbol = row.get("symbol")
        if (
            _deal_type(row) not in TRADE_DEAL_TYPES
            or not position_id
            or position_id == "0"
            or not isinstance(symbol, str)
            or not symbol.strip()
            or entry not in VALID_ENTRIES
        ):
            continue
        item = dict(row)
        item["project_as_trade"] = position_id not in unsafe_position
        commission, fee = _decimal(row.get("commission")), _decimal(row.get("fee"))
        if commission is not None and fee is not None:
            item["commission_raw"] = _number(commission)
            item["commission"] = _number(commission + fee)
        if entry in OPEN_ENTRIES and first_open_index.get(position_id) == index:
            item["history_event_type"] = "trade_opened"
            state = opening_state.get(index)
            if state is not None:
                item["volume"] = state["volume"]
                item["open_price"] = state["open_price"]
            value, reason = before_by_index.get(index, (None, "ledger_unavailable"))
            unsafe_reason = unsafe_position.get(position_id)
            if unsafe_reason is not None:
                item["balance_before_open"] = None
                item["balance_before_open_source"] = "not_available"
                item["balance_before_open_reason"] = unsafe_reason
                item["projection_reason"] = unsafe_reason
            elif value is not None and value > 0:
                item["balance_before_open"] = _number(value)
                item["balance_before_open_source"] = "mt5_historical_ledger"
            else:
                item["balance_before_open"] = None
                item["balance_before_open_source"] = "not_available"
                item["balance_before_open_reason"] = reason if value is None else "non_positive_balance"
        elif entry in OPEN_ENTRIES:
            item["history_event_type"] = "trade_volume_changed"
            state = opening_state.get(index)
            if state is not None:
                item["previous_volume"] = state["previous_volume"]
                item["volume"] = state["volume"]
                item["open_price"] = state["open_price"]
            item["partial_close"] = False
        if entry == "INOUT":
            item["project_as_trade"] = False
            item["projection_reason"] = "inout_reversal_not_reconstructible"
        elif entry in EXIT_ENTRIES:
            item["history_event_type"] = exit_event_type.get(index, "trade_partial_closed")
        if item.get("history_event_type") == "trade_closed":
            commission_complete = costs_complete.get(position_id, False)
            item["commission_complete"] = commission_complete
            if commission_complete:
                item["total_commission"] = _number(
                    costs_by_position.get(position_id, Decimal("0"))
                )
        projected.append(item)
    return tuple(projected)


def build_balance_backfill_report(
    projected_deals: Iterable[dict[str, Any]],
    *,
    connection_id: str,
    account_number: str,
    server: str,
    anchor: dict[str, Any] | None,
    ledger_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    entries: dict[str, dict[str, Any]] = {}
    for row in projected_deals:
        if str(row.get("entry", "")).upper() not in OPEN_ENTRIES:
            continue
        external_id = str(row.get("position_id", "")).strip()
        if not external_id or external_id in entries:
            continue
        value = row.get("balance_before_open")
        entries[external_id] = {
            "balance_before_open": value,
            "source": row.get("balance_before_open_source", "not_available"),
            "reason": row.get("balance_before_open_reason"),
            "opening_deal_ticket": str(row.get("ticket", "")),
            "opening_order_id": str(row.get("order_id", "")),
            "opening_time_msc": row.get("time_msc"),
        }
    report = {
        "schema_version": 1,
        "connection_id": connection_id,
        "account_number": account_number,
        "server": server,
        "anchor": dict(anchor or {}),
        "source": "mt5_historical_ledger",
        "entries": entries,
    }
    if ledger_snapshot is not None:
        report["ledger_snapshot"] = dict(ledger_snapshot)
    return report
