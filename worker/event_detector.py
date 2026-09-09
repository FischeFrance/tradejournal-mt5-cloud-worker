"""Rilevamento eventi tramite diff tra due snapshot successivi dello stato MT5.

Modulo puro (nessuna rete, nessun I/O): riceve due snapshot prodotti dal
file adapter Windows e restituisce una lista di eventi grezzi. È interamente
testabile passando dizionari costruiti a mano.

Forma di uno snapshot:
{
    "positions": {ticket: {ticket, symbol, direction, volume, open_price, stop_loss,
                            take_profit, open_time}},
    "orders": {ticket: {ticket, symbol, direction, volume, price, stop_loss, take_profit,
                         order_type}},
    "deals": {deal_ticket: {position_ticket, close_price, profit, commission, swap, close_time}},
}
"""

from __future__ import annotations

from typing import Any, Dict, List

RawEvent = Dict[str, Any]


def _base_fields(item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "symbol": item.get("symbol"),
        "direction": item.get("direction"),
        "volume": item.get("volume"),
    }


def _broker_ticket(value: Any) -> str | None:
    ticket = str(value or "").strip()
    return ticket if ticket and ticket != "0" else None


def origin_order_ticket_for_position(
    deals: Dict[str, Any],
    position_ticket: str,
) -> str | None:
    """Return the one broker order that opened a position, never a heuristic match.

    In MT5 netting mode several entry deals may become one position. The singular event contract
    can only name an origin when every known entry deal has the same non-zero DEAL_ORDER.
    """
    entry_deals = [
        deal for deal in deals.values()
        if isinstance(deal, dict)
        and str(deal.get("position_ticket", deal.get("position_id", ""))) == str(position_ticket)
        and str(deal.get("entry", "")).upper() in {"0", "IN"}
    ]
    if not entry_deals:
        return None
    order_tickets = [_broker_ticket(deal.get("order_id")) for deal in entry_deals]
    if any(ticket is None for ticket in order_tickets):
        return None
    unique_tickets = {ticket for ticket in order_tickets if ticket is not None}
    return next(iter(unique_tickets)) if len(unique_tickets) == 1 else None


def _detect_trade_opened_and_modified(previous: Dict[str, Any], current: Dict[str, Any]) -> List[RawEvent]:
    events: List[RawEvent] = []
    prev_positions = previous.get("positions", {})
    curr_positions = current.get("positions", {})
    curr_deals = current.get("deals", {})

    for ticket in sorted(curr_positions.keys()):
        pos = curr_positions[ticket]
        if ticket not in prev_positions:
            origin_order_ticket = origin_order_ticket_for_position(curr_deals, ticket)
            events.append({
                "event_type": "trade_opened",
                "ticket": ticket,
                **_base_fields(pos),
                "open_price": pos.get("open_price"),
                "stop_loss": pos.get("stop_loss"),
                "take_profit": pos.get("take_profit"),
                "open_time": pos.get("open_time"),
                **({"origin_order_ticket": origin_order_ticket} if origin_order_ticket else {}),
            })
            continue

        prev_pos = prev_positions[ticket]
        if pos.get("stop_loss") != prev_pos.get("stop_loss"):
            events.append({
                "event_type": "trade_modified",
                "ticket": ticket,
                **_base_fields(pos),
                "stop_loss": pos.get("stop_loss"),
                "take_profit": pos.get("take_profit"),
                "previous_stop_loss": prev_pos.get("stop_loss"),
                "previous_take_profit": prev_pos.get("take_profit"),
            })
        if pos.get("take_profit") != prev_pos.get("take_profit"):
            events.append({
                "event_type": "trade_modified",
                "ticket": ticket,
                **_base_fields(pos),
                "stop_loss": pos.get("stop_loss"),
                "take_profit": pos.get("take_profit"),
                "previous_stop_loss": prev_pos.get("stop_loss"),
                "previous_take_profit": prev_pos.get("take_profit"),
            })

    return events


def _find_closing_deal(deals: Dict[str, Any], ticket: str) -> Dict[str, Any]:
    for deal in deals.values():
        if str(deal.get("position_ticket")) == str(ticket):
            return deal
    return {}


def _detect_trade_closed(previous: Dict[str, Any], current: Dict[str, Any]) -> List[RawEvent]:
    events: List[RawEvent] = []
    prev_positions = previous.get("positions", {})
    curr_positions = current.get("positions", {})
    curr_deals = current.get("deals", {})

    for ticket in sorted(prev_positions.keys()):
        if ticket in curr_positions:
            continue
        pos = prev_positions[ticket]
        deal = _find_closing_deal(curr_deals, ticket)
        events.append({
            "event_type": "trade_closed",
            "ticket": ticket,
            **_base_fields(pos),
            "open_price": pos.get("open_price"),
            "close_price": deal.get("close_price"),
            "profit": deal.get("profit"),
            "commission": deal.get("commission"),
            "swap": deal.get("swap"),
            "close_time": deal.get("close_time"),
        })

    return events


def _detect_pending_order_events(previous: Dict[str, Any], current: Dict[str, Any]) -> List[RawEvent]:
    events: List[RawEvent] = []
    prev_orders = previous.get("orders", {})
    curr_orders = current.get("orders", {})
    curr_deals = current.get("deals", {})

    for ticket in sorted(curr_orders.keys()):
        order = curr_orders[ticket]
        if ticket not in prev_orders:
            events.append({
                "event_type": "pending_order_created",
                "ticket": ticket,
                **_base_fields(order),
                "price": order.get("price"),
                "stop_loss": order.get("stop_loss"),
                "take_profit": order.get("take_profit"),
            })
            continue

        prev_order = prev_orders[ticket]
        changed = (
            order.get("price") != prev_order.get("price")
            or order.get("stop_loss") != prev_order.get("stop_loss")
            or order.get("take_profit") != prev_order.get("take_profit")
        )
        if changed:
            events.append({
                "event_type": "pending_order_modified",
                "ticket": ticket,
                **_base_fields(order),
                "price": order.get("price"),
                "stop_loss": order.get("stop_loss"),
                "take_profit": order.get("take_profit"),
                "previous_stop_loss": prev_order.get("stop_loss"),
                "previous_take_profit": prev_order.get("take_profit"),
            })

    for ticket in sorted(prev_orders.keys()):
        if ticket in curr_orders:
            continue
        order = prev_orders[ticket]
        # A filled pending order gets a different position ticket. DEAL_ORDER on an entry deal
        # is the broker-confirmed terminal state; never infer a fill from symbol, price, volume,
        # timestamp, or an unrelated new position.
        is_filled = any(
            isinstance(deal, dict)
            and str(deal.get("entry", "")).upper() in {"0", "IN"}
            and _broker_ticket(deal.get("order_id")) == str(ticket)
            for deal in curr_deals.values()
        )
        events.append({
            "event_type": "pending_order_filled" if is_filled else "pending_order_cancelled",
            "ticket": ticket,
            **_base_fields(order),
            "price": order.get("price"),
            "stop_loss": order.get("stop_loss"),
            "take_profit": order.get("take_profit"),
        })

    return events


def detect_events(previous: Dict[str, Any], current: Dict[str, Any]) -> List[RawEvent]:
    """Confronta due snapshot e restituisce la lista di eventi grezzi rilevati.

    L'ordine restituito e' deterministico (aperture, modifiche, chiusure, poi eventi sugli
    ordini pendenti nello stesso ordine) cosi' da rendere i test riproducibili.
    """
    events: List[RawEvent] = []
    events.extend(_detect_trade_opened_and_modified(previous, current))
    events.extend(_detect_trade_closed(previous, current))
    events.extend(_detect_pending_order_events(previous, current))
    return events
