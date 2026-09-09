from __future__ import annotations

import random
import time
from typing import Any, Callable

from worker.event_outbox import EventOutbox
from worker.event_detector import detect_events
from worker.event_normalizer import normalize_event
from worker.event_sender import SendResult

from .dedup import PersistentDedup


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
        or parts[3] not in ("DEAL_ADD", "HISTORY_ADD")
        or not parts[4].isdigit()
        or not parts[5].isdigit()
    ):
        return None
    # connection_id scopes local files but is not broker identity. Excluding it preserves
    # idempotence when the same MT5 account is reprovisioned under a new local connection UUID.
    return "|".join(parts[1:6])


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
    }
    source_event_id = _stable_mql5_source_event_id(record.get("event_id"))
    if source_event_id is not None:
        # The EA identity is derived from immutable broker fields (deal/order ticket and
        # timestamp), so it remains stable when an overlap replay observes a newer snapshot.
        base["source_event_id"] = source_event_id
    if event_type == "DEAL_ADD":
        entry = str(record.get("entry", "")).upper()
        if entry == "IN":
            if previous_position is not None and current_position is not None:
                return {
                    **base,
                    "event_type": "trade_volume_changed",
                    "volume": current_position.get("volume"),
                    "previous_volume": previous_position.get("volume"),
                    "partial_close": False,
                }
            return {
                **base,
                "event_type": "trade_opened",
                "open_price": record.get("price"),
                "open_time": record.get("time"),
            }
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
                    **base,
                    "event_type": "trade_volume_changed",
                    "volume": remaining_volume,
                    "previous_volume": previous_volume,
                    "partial_close": True,
                    "close_price": record.get("price"),
                    "profit": record.get("profit"),
                    "commission": record.get("commission"),
                    "swap": record.get("swap"),
                    "close_time": record.get("time"),
                }
            return {
                **base,
                "event_type": "trade_closed",
                "close_price": record.get("price"),
                "profit": record.get("profit"),
                "commission": record.get("commission"),
                "swap": record.get("swap"),
                "close_time": record.get("time"),
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
    if event_type == "HISTORY_ADD":
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
    stream_events = []
    for record in records:
        event = _mql5_file_event(record, previous, current)
        if event is not None:
            stream_events.append(event)
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
