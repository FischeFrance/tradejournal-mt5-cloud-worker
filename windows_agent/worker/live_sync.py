from __future__ import annotations

import random
import time
from typing import Any, Callable

from worker.event_outbox import EventOutbox
from worker.event_detector import detect_events
from worker.event_normalizer import normalize_event
from worker.event_sender import SendResult

from .dedup import PersistentDedup
from .excursion_tracker import PositionExcursionTracker


class LiveSyncDeliveryError(RuntimeError):
    pass


class _CallableSender:
    def __init__(self, sink: Callable[[dict], None]) -> None:
        self._sink = sink

    def send(self, payload: dict) -> SendResult:
        sender = getattr(self._sink, "send", None)
        if callable(sender):
            return sender(payload)
        self._sink(payload)
        return SendResult(status="sent", attempts=1)


def _mql5_file_event(record: dict, previous: dict, current: dict) -> dict:
    event_type = str(record.get("event_type", "")).upper()
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
        entry = str(record.get("entry", "")).upper()
        if entry == "IN":
            if previous_position is not None and current_position is not None:
                return {
                    **deal_base,
                    "event_type": "trade_volume_changed",
                    "volume": current_position.get("volume"),
                    "previous_volume": previous_position.get("volume"),
                    "open_price": current_position.get("open_price"),
                    # Opening/scale-in costs are deal-level values.  Keeping each native fill
                    # separate lets Supabase aggregate them once and lets history replay use
                    # the same immutable deal identity.
                    "profit": record.get("profit"),
                    "commission": record.get("commission"),
                    "swap": record.get("swap"),
                    "partial_close": False,
                }
            return {
                **deal_base,
                "event_type": "trade_opened",
                "open_price": record.get("price"),
                "profit": record.get("profit"),
                "commission": record.get("commission"),
                "swap": record.get("swap"),
                "open_time": record.get("time"),
                "balance_before_open": record.get("balance_before_open"),
            }
        if entry in ("OUT", "OUT_BY"):
            if current_position is not None:
                return {
                    **deal_base,
                    "event_type": "trade_partial_closed",
                    # A close event always carries the executed deal volume, never the
                    # remaining position volume.  This makes its fingerprint identical to
                    # the same fill replayed by the historical importer.
                    "close_price": record.get("price"),
                    "profit": record.get("profit"),
                    "commission": record.get("commission"),
                    "swap": record.get("swap"),
                    "close_time": record.get("time"),
                }
            return {
                **deal_base,
                "event_type": "trade_closed",
                "close_price": record.get("price"),
                "profit": record.get("profit"),
                "commission": record.get("commission"),
                "swap": record.get("swap"),
                "close_time": record.get("time"),
            }
        return {
            **deal_base,
            "event_type": "deal_recorded",
            "close_price": record.get("price"),
            "profit": record.get("profit"),
            "commission": record.get("commission"),
            "swap": record.get("swap"),
            "close_time": record.get("time"),
        }
    if event_type == "POSITION" and current_position is not None:
        if (
            previous_position is not None
            and previous_position.get("volume") != current_position.get("volume")
        ):
            return {
                **base,
                "event_type": "trade_volume_changed",
                "volume": current_position.get("volume"),
                "previous_volume": previous_position.get("volume"),
                "partial_close": (
                    (current_position.get("volume") or 0)
                    < (previous_position.get("volume") or 0)
                ),
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
    if event_type == "ORDER_ADD":
        return {
            **base,
            "event_type": "pending_order_created",
            "price": record.get("price"),
            "stop_loss": record.get("stop_loss"),
            "take_profit": record.get("take_profit"),
        }
    if event_type == "ORDER_UPDATE":
        return {
            **base,
            "event_type": "pending_order_modified",
            "price": record.get("price"),
            "stop_loss": record.get("stop_loss"),
            "take_profit": record.get("take_profit"),
        }
    if event_type in ("ORDER_DELETE", "HISTORY_ADD"):
        return {
            **base,
            "event_type": "pending_order_cancelled",
            "price": record.get("price"),
        }
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
    # Replay DEAL_ADD records against the position volume that existed before the batch.
    # Several fills can arrive between two polls; looking only at the final snapshot would
    # otherwise classify every fill as a final close when the position is now absent.
    tracked_volumes: dict[str, float] = {}
    tracked_open_prices: dict[str, float] = {}
    for ticket, position in previous.get("positions", {}).items():
        try:
            tracked_volumes[str(ticket)] = float(position.get("volume"))
        except (TypeError, ValueError):
            continue
        try:
            tracked_open_prices[str(ticket)] = float(position.get("open_price"))
        except (TypeError, ValueError):
            pass

    stream_events: list[dict] = []
    for record in records:
        # The immutable history archive may already contain a deal whose MT5 transaction
        # callback was queued just after the frozen snapshot committed.  Its native ticket is
        # authoritative membership proof; replaying it from the live queue would turn the same
        # opening into a false scale-in (or apply the same close twice).
        if record.get("history_archived") is True:
            continue
        event_previous, event_current = previous, current
        if str(record.get("event_type", "")).upper() == "DEAL_ADD":
            position_ticket = record.get("position_id") or record.get("position_ticket")
            position_key = str(position_ticket) if position_ticket is not None else ""
            entry = str(record.get("entry", "")).upper()
            try:
                fill_volume = float(record.get("volume"))
            except (TypeError, ValueError):
                fill_volume = 0.0
            prior_volume = tracked_volumes.get(position_key)
            if position_key and fill_volume > 0 and entry == "IN":
                remaining = (prior_volume or 0.0) + fill_volume
                try:
                    fill_price = float(record.get("price"))
                except (TypeError, ValueError):
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
                    )
                }
                event_current = {
                    "positions": {
                        position_key: {
                            "volume": remaining,
                            "open_price": average_price,
                        }
                    }
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
                        }
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
                        )
                    }
                    if remaining > 1e-8:
                        tracked_volumes[position_key] = remaining
                    else:
                        tracked_volumes.pop(position_key, None)
                        tracked_open_prices.pop(position_key, None)
        stream_events.append(_mql5_file_event(record, event_previous, event_current))

    # DEAL_ADD is the authoritative economic close.  A POSITION notification for the same
    # transition (or the snapshot diff below) is only a state echo and must not reduce the
    # volume a second time.
    economic_close_tickets = {
        str(event.get("ticket"))
        for event in stream_events
        if event.get("event_type") in ("trade_partial_closed", "trade_closed")
    }
    stream_events = [
        event
        for event in stream_events
        if not (
            event.get("event_type") == "trade_volume_changed"
            and event.get("partial_close") is True
            and str(event.get("ticket")) in economic_close_tickets
        )
    ]
    reconciliation = detect_windows_events(previous, current)
    covered = {
        (str(event.get("event_type")), str(event.get("ticket")))
        for event in stream_events
    }
    stream_deal_ids = {
        str(record.get("deal_id") or record.get("ticket"))
        for record in records
        if str(record.get("event_type", "")).upper() == "DEAL_ADD"
    }
    for event in reconciliation:
        key = (str(event.get("event_type")), str(event.get("ticket")))
        if key in covered:
            continue
        if (
            event.get("event_type") == "trade_volume_changed"
            and event.get("partial_close") is True
            and str(event.get("ticket")) in economic_close_tickets
        ):
            continue
        if (
            event.get("event_type") == "deal_recorded"
            and str(event.get("ticket")) in stream_deal_ids
        ):
            continue
        stream_events.append(event)
    return stream_events


def detect_windows_events(previous: dict, current: dict) -> list[dict]:
    events = detect_events(previous, current)
    before, after = previous.get("positions", {}), current.get("positions", {})
    for ticket in sorted(before.keys() & after.keys()):
        old, new = before[ticket], after[ticket]
        if old.get("volume") != new.get("volume"):
            events.append(
                {
                    "event_type": "trade_volume_changed",
                    "ticket": ticket,
                    "symbol": new.get("symbol"),
                    "direction": new.get("direction"),
                    "volume": new.get("volume"),
                    "previous_volume": old.get("volume"),
                    "partial_close": (new.get("volume") or 0)
                    < (old.get("volume") or 0),
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
        excursion_store: Any | None = None,
    ) -> None:
        self.adapter, self.snapshot_store, self.dedup, self.sink = (
            adapter,
            snapshot_store,
            dedup,
            sink,
        )
        self.poll_seconds, self.stop_requested = poll_seconds, False
        self.outbox = outbox or EventOutbox()
        self.excursions = PositionExcursionTracker(
            excursion_store,
            sample_ms=max(250, int(poll_seconds * 1_000)),
        )

    def _drain_outbox(self) -> int:
        result = self.outbox.drain(_CallableSender(self.sink))
        dead_lettered = self.outbox.dead_letter_count()
        if result.pending or dead_lettered or result.dry_run:
            raise LiveSyncDeliveryError(
                "event delivery incomplete: "
                f"pending={result.pending}, dead_lettered={dead_lettered}, "
                f"dry_run={result.dry_run}"
            )
        return result.sent

    def poll_once(self) -> int:
        # Finish a previously persisted causal prefix before reading newer source events. This
        # makes crash recovery and transient delivery failures preserve open -> modify -> close.
        delivered = self._drain_outbox()
        account = self.adapter.verify_identity()
        current = self.adapter.snapshot()
        previous = self.snapshot_store.get()
        pending_events = getattr(self.adapter, "pending_events", None)
        records = pending_events() if callable(pending_events) else ()
        events = (
            _merge_event_stream_with_snapshot(records, previous, current)
            if records
            else detect_windows_events(previous, current)
        )
        # MAE/MFE stays local for the whole lifetime of the position.  Only the final extrema are
        # attached to the already-existing close event, so this adds zero Supabase calls per poll.
        self.excursions.observe_open_events(events)
        self.excursions.observe_snapshot(current)
        closed_excursions = self.excursions.enrich_close_events(events)
        payloads = []
        for event in events:
            payload = normalize_event(event, account["login"], account["server"])
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
        # At this point the enriched close payload is durable in the outbox.  Removing the local
        # accumulator cannot lose the event even if delivery fails immediately afterwards.
        self.excursions.discard(closed_excursions)
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
