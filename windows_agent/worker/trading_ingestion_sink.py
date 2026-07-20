"""HTTP delivery of live_sync events/heartbeats to the trading-mt5-events ingestion endpoint.

Reuses worker.event_sender.EventSender as-is (masked logging, exponential backoff on transient
errors, no retry on permanent 4xx rejections) -- the exact same class the self-hosted worker
connector already relies on in production, so managed (mt5_managed) connections get identical
delivery semantics instead of a second, parallel implementation.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from worker.event_sender import EventSender

from .local_event_sink import LocalEventSink

logger = logging.getLogger(__name__)


class TradingIngestionSink:
    """Callable sink for LiveSync: durably logs every detected event locally first (never
    silently discarded, same guarantee LocalEventSink already provides), then attempts HTTP
    delivery. A delivery that ultimately fails (after EventSender's own retries) is logged and
    swallowed rather than raised -- it stays in the local audit log but does not fail the whole
    live_sync job over a single transient network hiccup. If delivery is failing consistently
    (e.g. the VPS's egress or the bridge token is broken), heartbeats fail the same way,
    last_seen_at stops advancing, and the staleness safety-net cron correctly flips the
    connection to 'disconnected' -- an accurate reflection of reality, without extra plumbing
    here to detect "is delivery broken" separately.
    """

    def __init__(self, root: Path, api_url: str, bridge_token: str) -> None:
        self._local = LocalEventSink(root / "data" / "live.jsonl")
        self._sender = EventSender(api_url=api_url, bridge_token=bridge_token, dry_run=False)

    def __call__(self, payload: dict[str, Any]) -> None:
        self._local(payload)
        result = self._sender.send(payload)
        if result.status == "failed":
            logger.error(
                "live_sync event delivery failed for event_id=%s (error=%s) -- kept in local "
                "audit log only",
                payload.get("event_id"),
                result.error,
            )

    def send_heartbeat(self) -> bool:
        result = self._sender.send({"event_type": "heartbeat"})
        if result.status == "failed":
            logger.warning("live_sync heartbeat delivery failed (error=%s)", result.error)
            return False
        return True
