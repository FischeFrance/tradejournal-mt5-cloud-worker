from __future__ import annotations

import math
import random
import time
from typing import Any, Callable

from worker.event_outbox import EventOutbox
from worker.event_detector import detect_events
from worker.event_normalizer import normalize_event, validate_event_preflight
from worker.event_sender import SendResult

from .dedup import PersistentDedup


class LiveSyncDeliveryError(RuntimeError):
    pass


class CertifiedHistoryRecoveryRequired(LiveSyncDeliveryError):
    """A position reduction lacks the authoritative MT5 DEAL_ADD close.

    A snapshot can prove that volume changed, but it cannot prove the closing
    deal, its economics, or whether a partial close actually occurred.  The
    caller must retain the source prefix and ask the bridge for its certified
    history replay rather than emitting a generic partial-close payload.
    """


class _CallableSender:
    def __init__(self, sink: Callable[[dict], None]) -> None:
        self._sink = sink

    def send(self, payload: dict) -> SendResult:
        sender = getattr(self._sink, "send", None)
        if callable(sender):
            return sender(payload)
        self._sink(payload)
        return SendResult(status="sent", attempts=1)


def _stable_mql5_source_event_id(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    parts = value.split("|")
    # The first six fields are the immutable broker identity. Older EA builds then appended the
    # local sequence; current builds append a deterministic state fingerprint. Neither belongs
    # to the identity used for overlap replay. Only terminal history events opt into this path:
    # active ORDER_UPDATE events need their state fingerprint to distinguish successive edits.
    if (
        len(parts) < 6
        or not all(parts[:6])
        or not parts[1].isdigit()
        or parts[3] not in ("DEAL_ADD", "HISTORY_ADD", "HISTORY_FILLED")
        or not parts[4].isdigit()
        or not parts[5].isdigit()
    ):
        return None
    # connection_id scopes local files but is not broker identity. Excluding it preserves
    # idempotence when the same MT5 account is reprovisioned under a new local connection UUID.
    return "|".join(parts[1:6])


_PENDING_ORDER_TYPES = frozenset(
    {
        "2", "3", "4", "5", "6", "7",
        "BUY_LIMIT", "SELL_LIMIT", "BUY_STOP", "SELL_STOP",
        "BUY_STOP_LIMIT", "SELL_STOP_LIMIT",
    }
)


def _is_pending_order_record(record: dict) -> bool:
    value = record.get("order_type", record.get("type"))
    return str(value).strip().upper() in _PENDING_ORDER_TYPES


def _usable_symbol(value: object) -> str | None:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 64
        or any(ord(character) < 32 for character in value)
    ):
        return None
    return value


def _usable_direction(value: object) -> str | None:
    return value if value in ("buy", "sell") else None


def _usable_volume(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        volume = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return volume if volume > 0 else None


_VOLUME_EPSILON = 1e-8


def _finite_nonnegative_volume(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        volume = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(volume) or volume < 0:
        return None
    return volume


def _position_volume_reduced(previous_position: dict, current_position: dict) -> bool:
    """Fail closed if a changed position cannot prove a non-reduction."""

    previous_volume = _finite_nonnegative_volume(previous_position.get("volume"))
    current_volume = _finite_nonnegative_volume(current_position.get("volume"))
    if previous_volume is None or current_volume is None:
        return True
    return current_volume < previous_volume - _VOLUME_EPSILON


def _snapshot_position_reduction_tickets(previous: dict, current: dict) -> set[str]:
    """Return reductions that snapshots alone are not allowed to close."""

    before = previous.get("positions", {}) if isinstance(previous, dict) else {}
    after = current.get("positions", {}) if isinstance(current, dict) else {}
    if not isinstance(before, dict) or not isinstance(after, dict):
        return set()
    # A position disappearing entirely is the terminal form of the same
    # reduction.  Snapshot absence still cannot prove the closing deal or its
    # economics, so it requires the identical certified-history path unless a
    # native DEAL_ADD OUT/OUT_BY exists in the pending source prefix.
    reductions: set[str] = {str(ticket) for ticket in before.keys() - after.keys()}
    for ticket in before.keys() & after.keys():
        old, new = before[ticket], after[ticket]
        if not isinstance(old, dict) or not isinstance(new, dict):
            continue
        if old.get("volume") == new.get("volume"):
            continue
        if _position_volume_reduced(old, new):
            reductions.add(str(ticket))
    return reductions


def _certified_close_position_tickets(records: tuple[dict, ...]) -> set[str]:
    """Extract only authoritative native OUT callbacks for a partial close."""

    tickets: set[str] = set()
    for record in records:
        if str(record.get("event_type", "")).upper() != "DEAL_ADD":
            continue
        if str(record.get("entry", "")).upper() not in ("OUT", "OUT_BY"):
            continue
        value = (
            record.get("position_id")
            or record.get("position_ticket")
            or record.get("ticket")
        )
        if value not in (None, "", 0, "0"):
            tickets.add(str(value))
    return tickets


def _effective_commission(record: dict) -> object:
    """Return MT5's net cost once, retaining ``fee`` only as audit evidence.

    MT5 exposes DEAL_COMMISSION and DEAL_FEE separately.  The ingestion/UI
    contract uses ``profit + commission + swap``; clients must not add the
    optional audit ``fee`` again.  Historical ledger reconstruction already
    follows this rule, so live fills must do the identical conversion.
    Legacy producers without a finite fee retain their original commission.
    """
    raw = record.get("commission")
    fee = record.get("fee")
    if isinstance(raw, bool) or isinstance(fee, bool):
        return raw
    try:
        commission_value = float(raw)
        fee_value = float(fee)
    except (TypeError, ValueError, OverflowError):
        return raw
    if not math.isfinite(commission_value) or not math.isfinite(fee_value):
        return raw
    return commission_value + fee_value


def _record_identifiers(record: dict) -> set[str]:
    identifiers: set[str] = set()
    for field in ("position_id", "position_ticket", "ticket"):
        value = record.get(field)
        if value is None or isinstance(value, bool):
            continue
        text = str(value).strip()
        if text and text != "0":
            identifiers.add(text)
    return identifiers


def _opening_snapshot_fields(record: dict, previous: dict, current: dict) -> dict:
    """Fill only unambiguous missing opening fields from a related position.

    The event file remains unacknowledged when no coherent snapshot exists.
    This is deliberately narrow: pending orders retain their established path,
    and a conflicting position match is never used as a guess.
    """
    if (
        str(record.get("event_type", "")).upper() != "DEAL_ADD"
        or str(record.get("entry", "")).upper() != "IN"
    ):
        return record
    identifiers = _record_identifiers(record)
    if not identifiers:
        return record

    candidates: list[dict] = []
    for snapshot in (current, previous):
        positions = snapshot.get("positions", {}) if isinstance(snapshot, dict) else {}
        if not isinstance(positions, dict):
            continue
        for key, position in positions.items():
            if not isinstance(position, dict):
                continue
            position_ids = {str(key)}
            for field in ("ticket", "position_id", "position_ticket"):
                value = position.get(field)
                if value not in (None, "", 0, "0"):
                    position_ids.add(str(value))
            if not identifiers.isdisjoint(position_ids):
                candidates.append(position)

    def one_value(field: str, normalize):
        values = {
            normalized
            for candidate in candidates
            if (normalized := normalize(candidate.get(field))) is not None
        }
        return next(iter(values)) if len(values) == 1 else None

    patched = dict(record)
    if _usable_symbol(patched.get("symbol")) is None:
        value = one_value("symbol", _usable_symbol)
        if value is not None:
            patched["symbol"] = value
    if _usable_direction(patched.get("direction")) is None:
        value = one_value("direction", _usable_direction)
        if value is not None:
            patched["direction"] = value
    if _usable_volume(patched.get("volume")) is None:
        value = one_value("volume", _usable_volume)
        if value is not None:
            patched["volume"] = value
    return patched


def _mql5_file_event(
    record: dict,
    previous: dict,
    current: dict,
) -> dict | None:
    event_type = str(record.get("event_type", "")).upper()
    if event_type == "ORDER" + "_DELETE":
        # Older Bridge builds emitted this both for true cancellations and for
        # orders that were merely executed. HISTORY_ADD is state-filtered by
        # the EA and is the sole canonical cancellation event/replay identity.
        return None
    if event_type == "HISTORY_FILLED":
        if not _is_pending_order_record(record):
            return None
        order_ticket = record.get("order_id") or record.get("ticket")
        order_ticket_text = str(order_ticket or "")
        base = {
            "ticket": order_ticket_text,
            "symbol": record.get("symbol"),
            "direction": record.get("direction"),
            # MT5 reports ORDER_VOLUME_CURRENT as zero once an order is fully filled. Keep it
            # null so the pending-order lifecycle projection retains the original order volume.
            "volume": None,
            "event_time": record.get("time"),
            "order_type": record.get("order_type", record.get("type")),
        }
        source_event_id = _stable_mql5_source_event_id(record.get("event_id"))
        if source_event_id is not None:
            base["source_event_id"] = source_event_id
        return {
            **base,
            "event_type": "pending_order_filled",
            "price": record.get("price"),
            "stop_loss": record.get("stop_loss"),
            "take_profit": record.get("take_profit"),
        }
    position_ticket = record.get("position_id") or record.get("position_ticket")
    ticket = position_ticket or record.get("order_id") or record.get("ticket")
    ticket_text = str(ticket)
    previous_position = previous.get("positions", {}).get(ticket_text)
    current_position = current.get("positions", {}).get(ticket_text)
    base = {
        "ticket": ticket_text,
        "symbol": record.get("symbol"),
        "direction": record.get("direction"),
        "volume": record.get("volume"),
        "event_time": record.get("time"),
        "balance": record.get("balance"),
        "equity": record.get("equity"),
        "currency": record.get("currency"),
        "leverage": record.get("leverage"),
    }
    if record.get("order_type") is not None:
        base["order_type"] = record.get("order_type")
    source_event_id = _stable_mql5_source_event_id(record.get("event_id"))
    if source_event_id is not None:
        # The EA identity is derived from immutable broker fields (deal/order ticket and
        # timestamp), so it remains stable when an overlap replay observes a newer snapshot.
        base["source_event_id"] = source_event_id
    if event_type == "DEAL_ADD":
        time_msc = record.get("timestamp_msc")
        if time_msc is None:
            time_msc = record.get("time_msc")
        deal_base = {
            **base,
            "native_deal_ticket": record.get("deal_id") or record.get("ticket"),
            "time_msc": time_msc,
            "time_basis": "broker_server_unresolved",
        }
        effective_commission = _effective_commission(record)
        entry = str(record.get("entry", "")).upper()
        if entry == "IN":
            if previous_position is not None and current_position is not None:
                return {
                    **deal_base,
                    "event_type": "trade_volume_changed",
                    "volume": current_position.get("volume"),
                    "previous_volume": previous_position.get("volume"),
                    "open_price": current_position.get("open_price"),
                    "profit": record.get("profit"),
                    "commission": effective_commission,
                    "fee": record.get("fee"),
                    "swap": record.get("swap"),
                    "partial_close": False,
                }
            opened = {
                **deal_base,
                "event_type": "trade_opened",
                "open_price": record.get("price"),
                "profit": record.get("profit"),
                "commission": effective_commission,
                "fee": record.get("fee"),
                "swap": record.get("swap"),
                "open_time": record.get("time"),
            }
            # An EA callback can be queued/replayed after another fill or cash
            # movement.  Its current ACCOUNT_BALANCE is not event-time proof,
            # so live ingestion must never label it as an exact opening
            # denominator.  The immutable full-history ledger later supplies
            # ``history_reconstructed`` provenance where it can prove one.
            origin_order_ticket = str(record.get("order_id") or "").strip()
            if origin_order_ticket and origin_order_ticket != "0":
                opened["origin_order_ticket"] = origin_order_ticket
            return opened
        if entry in ("OUT", "OUT_BY"):
            previous_volume = (
                previous_position.get("volume")
                if previous_position is not None
                else None
            )
            current_volume = (
                current_position.get("volume")
                if current_position is not None
                else None
            )
            deal_volume = record.get("volume")
            try:
                previous_volume_number = float(previous_volume)
                deal_volume_number = float(deal_volume)
            except (TypeError, ValueError):
                previous_volume_number = None
                deal_volume_number = None

            # The DEAL_ADD file can become visible before the terminal snapshot drops the
            # closed position. In that race, presence in ``current`` is not proof of a partial
            # close. The deal volume is authoritative: closing the whole previous volume is a
            # final execution even while the snapshot still contains the stale position.
            closes_previous_volume = (
                previous_volume_number is not None
                and deal_volume_number is not None
                and deal_volume_number >= previous_volume_number - 1e-8
            )
            if current_position is not None and not closes_previous_volume:
                remaining_volume = current_volume
                if (
                    previous_volume_number is not None
                    and deal_volume_number is not None
                ):
                    expected_remaining = max(
                        0.0, previous_volume_number - deal_volume_number
                    )
                    try:
                        snapshot_is_stale = (
                            float(current_volume) >= previous_volume_number - 1e-8
                        )
                    except (TypeError, ValueError):
                        snapshot_is_stale = True
                    if snapshot_is_stale:
                        remaining_volume = expected_remaining
                return {
                    **deal_base,
                    # A DEAL_ADD OUT has realized economics and may carry the
                    # opposite MT5 deal direction from the original position.
                    # It must therefore be an explicit partial-close event,
                    # not a generic volume hint: downstream reconciliation
                    # otherwise drops its P&L before the final close arrives.
                    "event_type": "trade_partial_closed",
                    # Explicit partial-close consumers interpret ``volume``
                    # as the quantity closed, never the position remainder.
                    # The next snapshot still carries the 0.6 remaining side
                    # of a 1.0 -> 0.6 close of 0.4.
                    "volume": deal_volume_number,
                    "previous_volume": previous_volume,
                    "partial_close": True,
                    "close_price": record.get("price"),
                    "profit": record.get("profit"),
                    "commission": effective_commission,
                    "fee": record.get("fee"),
                    "swap": record.get("swap"),
                    "close_time": record.get("time"),
                }
            return {
                **deal_base,
                "event_type": "trade_closed",
                "close_price": record.get("price"),
                "profit": record.get("profit"),
                "commission": effective_commission,
                "fee": record.get("fee"),
                "swap": record.get("swap"),
                "close_time": record.get("time"),
            }
        return {
            **deal_base,
            "event_type": "deal_recorded",
            "close_price": record.get("price"),
            "profit": record.get("profit"),
            "commission": effective_commission,
            "fee": record.get("fee"),
            "swap": record.get("swap"),
            "close_time": record.get("time"),
        }
    if event_type == "POSITION" and current_position is not None:
        if (
            previous_position is not None
            and previous_position.get("volume") != current_position.get("volume")
        ):
            if _position_volume_reduced(previous_position, current_position):
                # A POSITION callback contains no deal economics.  The
                # certified DEAL_ADD OUT must arrive (or a source replay will
                # be requested by the enclosing batch) before any close is
                # sent to the ingestion API.
                return None
            return {
                **base,
                "event_type": "trade_volume_changed",
                "volume": current_position.get("volume"),
                "previous_volume": previous_position.get("volume"),
                "partial_close": False,
            }
        return {
            **base,
            "event_type": "trade_modified",
            "volume": current_position.get("volume"),
            "stop_loss": current_position.get("stop_loss"),
            "take_profit": current_position.get("take_profit"),
            "previous_stop_loss": (
                previous_position.get("stop_loss")
                if previous_position is not None
                else None
            ),
            "previous_take_profit": (
                previous_position.get("take_profit")
                if previous_position is not None
                else None
            ),
        }
    if event_type == "ORDER_ADD" and _is_pending_order_record(record):
        return {
            **base,
            "event_type": "pending_order_created",
            "price": record.get("price"),
            "stop_loss": record.get("stop_loss"),
            "take_profit": record.get("take_profit"),
        }
    if event_type == "ORDER_UPDATE" and _is_pending_order_record(record):
        return {
            **base,
            "event_type": "pending_order_modified",
            "price": record.get("price"),
            "stop_loss": record.get("stop_loss"),
            "take_profit": record.get("take_profit"),
        }
    if event_type == "HISTORY_ADD" and _is_pending_order_record(record):
        return {
            **base,
            "event_type": "pending_order_cancelled",
            "price": record.get("price"),
        }
    if event_type in ("ORDER_ADD", "ORDER_UPDATE", "HISTORY_ADD", "HISTORY_FILLED"):
        return None
    return {
        **base,
        "event_type": "deal_recorded",
        "close_price": record.get("price"),
        "profit": record.get("profit"),
        "commission": record.get("commission"),
        "swap": record.get("swap"),
        "close_time": record.get("time"),
    }


def _merge_event_stream_with_snapshot(
    records: tuple[dict, ...], previous: dict, current: dict
) -> list[dict]:
    # Replay DEAL_ADD events in native source order. A final snapshot is only a
    # boundary; it cannot classify multiple fills that occurred between polls.
    # Positions are keyed by MT5 POSITION_IDENTIFIER (with a legacy ticket
    # fallback already normalized by the file adapter).
    tracked_volumes: dict[str, float] = {}
    tracked_open_prices: dict[str, float] = {}
    for ticket, position in previous.get("positions", {}).items():
        if not isinstance(position, dict):
            continue
        try:
            volume = float(position.get("volume"))
        except (TypeError, ValueError, OverflowError):
            continue
        if not math.isfinite(volume) or volume <= 0:
            continue
        position_key = str(position.get("position_id") or ticket)
        tracked_volumes[position_key] = volume
        try:
            price = float(position.get("open_price"))
        except (TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(price):
            tracked_open_prices[position_key] = price

    # A position reduction must have a DEAL_ADD OUT/OUT_BY in the same
    # unacknowledged native prefix.  Never let a POSITION callback or a final
    # snapshot manufacture a generic partial-close event; its P&L cannot be
    # reconstructed from volume alone.
    unverified_reductions = (
        _snapshot_position_reduction_tickets(previous, current)
        - _certified_close_position_tickets(records)
    )
    if unverified_reductions:
        raise CertifiedHistoryRecoveryRequired(
            "certified history recovery required for position reduction"
        )

    stream_events: list[dict] = []
    for record in records:
        # A queued callback already included in the frozen history archive is
        # acknowledged with this poll but never mapped as a duplicate live fill.
        if record.get("history_archived") is True:
            continue
        # An EA event may be observable just before its correlated position
        # snapshot. On a later poll retain the same source_event_id but use
        # that now-coherent position to complete only required opening fields.
        record = _opening_snapshot_fields(record, previous, current)
        event_previous, event_current = previous, current
        if str(record.get("event_type", "")).upper() == "DEAL_ADD":
            position_id = record.get("position_id") or record.get("position_ticket")
            position_key = (
                str(position_id) if position_id not in (None, "", 0, "0") else ""
            )
            entry = str(record.get("entry", "")).upper()
            try:
                fill_volume = float(record.get("volume"))
            except (TypeError, ValueError, OverflowError):
                fill_volume = 0.0
            if not math.isfinite(fill_volume):
                fill_volume = 0.0
            if entry in ("OUT", "OUT_BY") and (
                not position_key or fill_volume <= 0
            ):
                # Do not let an invalid native reduction get acknowledged as
                # a generic snapshot change. Explicit partial-close consumers
                # treat its volume as closed quantity, so a null/NaN value
                # could otherwise consume the full remaining position.
                raise CertifiedHistoryRecoveryRequired(
                    "native partial close volume unavailable"
                )
            prior_volume = tracked_volumes.get(position_key)
            if position_key and fill_volume > 0 and entry == "IN":
                remaining = (prior_volume or 0.0) + fill_volume
                try:
                    fill_price = float(record.get("price"))
                except (TypeError, ValueError, OverflowError):
                    fill_price = None
                if fill_price is not None and not math.isfinite(fill_price):
                    fill_price = None
                prior_price = tracked_open_prices.get(position_key)
                average_price = fill_price
                if (
                    prior_volume is not None
                    and prior_volume > 0
                    and prior_price is not None
                    and fill_price is not None
                ):
                    average_price = (
                        (prior_volume * prior_price) + (fill_volume * fill_price)
                    ) / remaining
                event_previous = {
                    "positions": (
                        {
                            position_key: {
                                "volume": prior_volume,
                                "open_price": prior_price,
                            }
                        }
                        if prior_volume is not None
                        else {}
                    ),
                    "orders": {},
                    "deals": {},
                }
                event_current = {
                    "positions": {
                        position_key: {
                            "volume": remaining,
                            "open_price": average_price,
                        }
                    },
                    "orders": {},
                    "deals": {},
                }
                tracked_volumes[position_key] = remaining
                if average_price is not None:
                    tracked_open_prices[position_key] = average_price
            elif position_key and fill_volume > 0 and entry in ("OUT", "OUT_BY"):
                if prior_volume is not None:
                    remaining = prior_volume - fill_volume
                    prior_price = tracked_open_prices.get(position_key)
                    event_previous = {
                        "positions": {
                            position_key: {
                                "volume": prior_volume,
                                "open_price": prior_price,
                            }
                        },
                        "orders": {},
                        "deals": {},
                    }
                    event_current = {
                        "positions": (
                            {
                                position_key: {
                                    "volume": remaining,
                                    "open_price": prior_price,
                                }
                            }
                            if remaining > 1e-8
                            else {}
                        ),
                        "orders": {},
                        "deals": {},
                    }
                    if remaining > 1e-8:
                        tracked_volumes[position_key] = remaining
                    else:
                        tracked_volumes.pop(position_key, None)
                        tracked_open_prices.pop(position_key, None)
        event = _mql5_file_event(record, event_previous, event_current)
        if event is not None:
            stream_events.append(event)
    reconciliation = detect_windows_events(previous, current)
    covered = {
        (str(event.get("event_type")), str(event.get("ticket")))
        for event in stream_events
    }
    terminal_pending_order_tickets = {
        str(event.get("ticket"))
        for event in stream_events
        if event.get("event_type") in ("pending_order_filled", "pending_order_cancelled")
    }
    economic_close_tickets = {
        str(event.get("ticket"))
        for event in stream_events
        if event.get("event_type") in ("trade_partial_closed", "trade_closed")
    }
    stream_deal_ids = {
        str(record.get("deal_id") or record.get("ticket"))
        for record in records
        if str(record.get("event_type", "")).upper() == "DEAL_ADD"
        and record.get("history_archived") is not True
    }
    for event in reconciliation:
        key = (str(event.get("event_type")), str(event.get("ticket")))
        if key in covered:
            continue
        if (
            event.get("event_type") == "pending_order_cancelled"
            and str(event.get("ticket")) in terminal_pending_order_tickets
        ):
            continue
        if (
            event.get("event_type") == "trade_volume_changed"
            and event.get("partial_close") is True
        ):
            if str(event.get("ticket")) in economic_close_tickets:
                continue
            # Defense in depth for a future snapshot detector change: a
            # generic partial close is never safe to enqueue or dead-letter.
            raise CertifiedHistoryRecoveryRequired(
                "snapshot partial close has no certified native deal"
            )
        if (
            event.get("event_type") == "deal_recorded"
            and str(event.get("ticket")) in stream_deal_ids
        ):
            continue
        stream_events.append(event)

    represented_pending_tickets = {
        str(event.get("ticket"))
        for event in stream_events
        if str(event.get("event_type", "")).startswith("pending_order_")
    }
    # Re-assert unchanged active orders once per state fingerprint. This upgrades orders observed
    # by an older bridge with the authoritative order_type and preserves their placement time.
    for ticket, order in current.get("orders", {}).items():
        if str(ticket) in represented_pending_tickets or not _is_pending_order_record(order):
            continue
        stream_events.append({
            "event_type": "pending_order_modified",
            "ticket": str(ticket),
            "symbol": order.get("symbol"),
            "direction": order.get("direction"),
            "volume": order.get("volume"),
            "price": order.get("price"),
            "stop_loss": order.get("stop_loss"),
            "take_profit": order.get("take_profit"),
            "order_type": order.get("order_type"),
            "event_time": order.get("placed_at"),
        })
    return stream_events


def detect_windows_events(previous: dict, current: dict) -> list[dict]:
    events = detect_events(previous, current)
    before, after = previous.get("positions", {}), current.get("positions", {})
    for ticket in sorted(before.keys() & after.keys()):
        old, new = before[ticket], after[ticket]
        if old.get("volume") != new.get("volume"):
            if _position_volume_reduced(old, new):
                # Snapshot-only reductions must be repaired from the EA's
                # native history source.  This pure helper has no adapter to
                # request it; LiveSync.poll_once performs that durable step.
                continue
            events.append(
                {
                    "event_type": "trade_volume_changed",
                    "ticket": ticket,
                    "symbol": new.get("symbol"),
                    "direction": new.get("direction"),
                    "volume": new.get("volume"),
                    "previous_volume": old.get("volume"),
                    "partial_close": False,
                }
            )
    for ticket in sorted(
        current.get("deals", {}).keys() - previous.get("deals", {}).keys()
    ):
        deal = current["deals"][ticket]
        events.append({"event_type": "deal_recorded", "ticket": ticket, **deal})
    return events


class LiveSync:
    def __init__(
        self,
        adapter: Any,
        snapshot_store: Any,
        dedup: PersistentDedup,
        sink: Callable[[dict], None],
        poll_seconds: float = 2.0,
        outbox: EventOutbox | None = None,
    ) -> None:
        self.adapter, self.snapshot_store, self.dedup, self.sink = (
            adapter,
            snapshot_store,
            dedup,
            sink,
        )
        self.poll_seconds, self.stop_requested = poll_seconds, False
        self.outbox = outbox or EventOutbox()

    def _drain_outbox(self) -> int:
        result = self.outbox.drain(_CallableSender(self.sink))
        # A permanent rejection has already been durably isolated by EventOutbox. It is no
        # longer part of the causal pending prefix, so a historical dead-letter must not poison
        # every later MT5 event (for example, a DEAL_ADD OUT closing a position). Only work that
        # remains pending, or an explicit dry-run, prevents safely advancing the live stream.
        if result.pending or result.dry_run:
            raise LiveSyncDeliveryError(
                "event delivery incomplete: "
                f"pending={result.pending}, dry_run={result.dry_run}"
            )
        return result.sent

    def _request_certified_history_recovery(self) -> None:
        """Durably ask a managed MQL5 bridge for an authoritative replay.

        The request is intentionally adapter-scoped.  A legacy/direct adapter
        cannot pretend that an inferred close is safe; it simply retains the
        failing source work for its normal retry path.
        """

        request = getattr(self.adapter, "request_certified_history_recovery", None)
        if not callable(request):
            return
        try:
            request()
        except Exception as exc:
            # The original source prefix and snapshot remain untouched, so a
            # transient marker write is also retryable continuity work rather
            # than an account-breaking delivery failure.
            raise CertifiedHistoryRecoveryRequired(
                "certified history recovery request failed"
            ) from exc

    def poll_once(self) -> int:
        # Finish a previously persisted causal prefix before reading newer source events. This
        # makes crash recovery and transient delivery failures preserve open -> modify -> close.
        delivered = self._drain_outbox()
        account = self.adapter.verify_identity()
        current = self.adapter.snapshot()
        previous = self.snapshot_store.get()
        pending_events = getattr(self.adapter, "pending_events", None)
        records = pending_events() if callable(pending_events) else ()
        try:
            if records:
                events = _merge_event_stream_with_snapshot(records, previous, current)
            else:
                if _snapshot_position_reduction_tickets(previous, current):
                    raise CertifiedHistoryRecoveryRequired(
                        "snapshot position reduction requires certified history"
                    )
                events = detect_windows_events(previous, current)
        except CertifiedHistoryRecoveryRequired:
            # This happens before outbox persistence, source acknowledgement,
            # snapshot advance, and dedup mutation.  A 422 can therefore never
            # turn a real broker close into a dead-lettered false close.
            self._request_certified_history_recovery()
            raise
        account_snapshot_reader = getattr(self.adapter, "account_snapshot", None)
        account_snapshot = (
            account_snapshot_reader()
            if events and callable(account_snapshot_reader)
            else None
        )
        payloads = []
        for event in events:
            # This deliberately happens before event-id construction, outbox
            # persistence and source acknowledgement.  An incomplete opening
            # is a temporary MT5 snapshot race, not a poison event: retain its
            # file and retry once a coherent snapshot can name the predecessor.
            validate_event_preflight(event)
            payload = normalize_event(
                event,
                account["login"],
                account["server"],
                account_snapshot=account_snapshot,
            )
            if not self.dedup.contains(payload["event_id"]):
                payloads.append(payload)
        # Persist the complete causal batch before advancing the snapshot. A crash after either
        # operation regenerates stable event_ids or resumes the outbox; neither path loses data.
        self.outbox.enqueue_many(payloads)
        if records:
            acknowledge = getattr(self.adapter, "acknowledge_events", None)
            if not callable(acknowledge):
                raise LiveSyncDeliveryError("event stream cannot be acknowledged")
            acknowledge(max(int(record["sequence"]) for record in records))
        self.snapshot_store.save(current)
        delivered += self._drain_outbox()
        for payload in payloads:
            self.dedup.add(payload["event_id"])
        return delivered

    def run(self) -> None:
        while not self.stop_requested:
            try:
                self.poll_once()
            except Exception:
                time.sleep(min(self.poll_seconds + random.uniform(0, 0.25), 10))
                continue
            time.sleep(self.poll_seconds)

    def stop(self) -> None:
        self.stop_requested = True
