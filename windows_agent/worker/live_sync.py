from __future__ import annotations

import random
import time
from typing import Any, Callable

from worker.event_normalizer import normalize_event, validate_event_preflight
from worker.event_outbox import DrainResult, EventOutbox
from worker.event_sender import SendResult

from .dedup import PersistentDedup
from .excursion_tracker import PositionExcursionTracker
from .live_sync_events import (
    _authoritative_symbol,
    _merge_event_stream_with_snapshot,
)
from .live_sync_events import (
    _mql5_file_event as _mql5_file_event,
)
from .live_sync_events import (
    detect_windows_events as detect_windows_events,
)


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

    def _drain_outbox(self) -> DrainResult:
        # A permanent rejection is durably quarantined by EventOutbox.drain() and is no longer
        # treated as a barrier: automatic recovery (no operator action) is a deliberate choice
        # for this read-only reporting pipeline, so a single unprocessable event never freezes
        # every real trade reported for this account after it. A durable 429 is a deferred
        # local cycle, not an acknowledgement. Other pending errors/dry-run remain failures.
        result = self.outbox.drain(_CallableSender(self.sink))
        if (result.pending or result.dry_run) and not result.rate_limited:
            raise LiveSyncDeliveryError(
                "event delivery incomplete: "
                f"pending={result.pending}, dry_run={result.dry_run}"
            )
        return result

    def poll_once(self) -> int:
        # Finish a previously persisted causal prefix before reading newer source events. This
        # makes crash recovery and transient delivery failures preserve open -> modify -> close.
        delivery = self._drain_outbox()
        delivered = delivery.sent
        if delivery.rate_limited:
            # No server acknowledgement is invented. The durable FIFO resumes on a later
            # local cycle, without failing a provision/history job merely for backpressure.
            return delivered
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
        for event in events:
            if event.get("event_type") != "trade_opened":
                continue
            symbol = _authoritative_symbol(event, previous, current)
            if symbol is None:
                # Do not persist, enqueue or acknowledge an event that the ingestion contract
                # will reject.  The bridge file remains available for the next authoritative
                # snapshot instead of becoming an unrecoverable 422 dead-letter.
                raise LiveSyncDeliveryError("trade_opened symbol unavailable")
            event["symbol"] = symbol
            try:
                validate_event_preflight(event)
            except ValueError as exc:
                raise LiveSyncDeliveryError(str(exc)) from exc
        # MAE/MFE stays local for the whole lifetime of the position.  Only the final extrema are
        # attached to the already-existing close event, so this adds zero Supabase calls per poll.
        self.excursions.observe_open_events(events)
        self.excursions.observe_snapshot(current)
        closed_excursions = self.excursions.enrich_close_events(events)
        offset_reader = getattr(self.adapter, "broker_utc_offset", None)
        broker_offset = offset_reader() if callable(offset_reader) else None
        payloads = []
        for event in events:
            if broker_offset is not None:
                event = {**event, **broker_offset.event_fields()}
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
        delivery = self._drain_outbox()
        delivered += delivery.sent
        if delivery.rate_limited:
            return delivered
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
