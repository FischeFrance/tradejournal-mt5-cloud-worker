"""Rilevamento eventi tramite diff tra due snapshot successivi dello stato MT5.

Modulo puro (nessuna rete, nessun I/O): riceve due snapshot (dict) prodotti da
`mt5_client`/`mock_mt5_client` tramite `snapshot_store` e restituisce una lista di eventi
grezzi. Essendo puro, e' interamente testabile passando dizionari costruiti a mano -- la stessa
logica funziona identicamente sia con il mock sia (in futuro) con MetaTrader5 reale, perche'
entrambi i client espongono la stessa forma di snapshot.

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


def _detect_trade_opened_and_modified(previous: Dict[str, Any], current: Dict[str, Any]) -> List[RawEvent]:
    events: List[RawEvent] = []
    prev_positions = previous.get("positions", {})
    curr_positions = current.get("positions", {})

    for ticket in sorted(curr_positions.keys()):
        pos = curr_positions[ticket]
        if ticket not in prev_positions:
            events.append({
                "event_type": "trade_opened",
                "ticket": ticket,
                **_base_fields(pos),
                "open_price": pos.get("open_price"),
                "stop_loss": pos.get("stop_loss"),
                "take_profit": pos.get("take_profit"),
                "open_time": pos.get("open_time"),
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
    curr_positions = current.get("positions", {})

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
        # Se il ticket e' diventato una posizione, non e' una cancellazione ma un'esecuzione
        # dell'ordine pendente -- fuori scope per questo POC (non richiesto tra gli eventi).
        if ticket in curr_positions:
            continue
        order = prev_orders[ticket]
        events.append({
            "event_type": "pending_order_cancelled",
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
