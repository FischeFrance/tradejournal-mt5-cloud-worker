"""HTTP delivery of live_sync events/heartbeats to the trading-mt5-events ingestion endpoint.

Reuses worker.event_sender.EventSender as-is (masked logging, exponential backoff on transient
errors, no retry on permanent 4xx rejections) -- the exact same class the self-hosted worker
connector already relies on in production, so managed (mt5_managed) connections get identical
delivery semantics instead of a second, parallel implementation.
"""

from __future__ import annotations

import logging
import math
import time
from pathlib import Path
from typing import Any

from worker.event_outbox import DrainResult, EventOutbox
from worker.event_sender import EventSender, SendResult

from .local_event_sink import LocalEventSink

logger = logging.getLogger(__name__)


class _HeartbeatSender:
    def __init__(self, sender: EventSender) -> None:
        self.sender = sender

    def send(self, payload: dict[str, Any]) -> SendResult:
        # The fixed ID deduplicates/coalesces only the local presence queue, never a trade.
        return self.sender.send({key: value for key, value in payload.items() if key != "event_id"})


class TradingIngestionSink:
    """Callable sink for LiveSync: durably logs every detected event locally first (never
    silently discarded, same guarantee LocalEventSink already provides), then attempts HTTP
    delivery. ``send`` returns the complete SendResult to the persistent outbox: transient
    failures remain pending in FIFO order, permanent failures move to its durable dead-letter,
    and only confirmed events are counted as delivered. A 429 defers the durable queue without
    failing the lifecycle job. ``send_heartbeat`` still requires an explicit acknowledgement;
    ``heartbeat_may_continue`` is a separate local-cycle decision, never an acknowledgement.
    """

    def __init__(
        self, root: Path, api_url: str, bridge_token: str, *,
        clock_fn=time.time, monotonic_fn=time.monotonic,
    ) -> None:
        self._local = LocalEventSink(root / "data" / "live.jsonl")
        self._sender = EventSender(api_url=api_url, bridge_token=bridge_token, dry_run=False)
        self._heartbeats = EventOutbox(
            str(root / "state" / "heartbeat-outbox.json"),
            clock_fn=clock_fn, monotonic_fn=monotonic_fn,
        )
        self.heartbeat_deferred = False
        self.heartbeat_retry_at: float | None = self._heartbeats.retry_at

    def send(self, payload: dict[str, Any]) -> Any:
        self._local(payload)
        result = self._sender.send(payload)
        if result.status == "failed":
            logger.error(
                "live_sync event delivery failed (error=%s); durable outbox handles retry "
                "or quarantine",
                result.error,
            )
        return result

    def __call__(self, payload: dict[str, Any]) -> None:
        result = self.send(payload)
        if result.status != "sent":
            raise RuntimeError("ingestion delivery was not acknowledged")

    def send_heartbeat(self, account_info: Any | None = None) -> bool:
        """True only when the endpoint acknowledges this presence sample."""
        return self._deliver_heartbeat(account_info).sent == 1

    def heartbeat_may_continue(self, account_info: Any | None = None) -> bool:
        """Allow a locally verified cycle on durable 429 deferral, not on auth/lease failures.

        Callers must not interpret this decision as server presence: deferred/retry_at are
        explicit attributes and the server's last_seen_at is unchanged until an actual 2xx.
        """
        delivery = self._deliver_heartbeat(account_info)
        return delivery.sent == 1 or (delivery.rate_limited and delivery.pending == 1)

    def _deliver_heartbeat(self, account_info: Any | None) -> DrainResult:
        payload: dict[str, Any] = {"event_type": "heartbeat"}
        if account_info is not None:
            balance = float(getattr(account_info, "balance"))
            equity = float(getattr(account_info, "equity"))
            currency = str(getattr(account_info, "currency")).strip().upper()
            leverage = getattr(account_info, "leverage")
            if (
                not math.isfinite(balance)
                or not math.isfinite(equity)
                or not currency.isalnum()
                or not 3 <= len(currency) <= 12
                or not isinstance(leverage, int)
                or isinstance(leverage, bool)
                or not 1 <= leverage <= 1_000_000
            ):
                raise ValueError("invalid account snapshot")
            payload.update(
                {
                    "balance": balance,
                    "equity": equity,
                    "currency": currency,
                    "leverage": leverage,
                }
            )
        self._heartbeats.coalesce_heartbeat({"event_id": "presence", **payload})
        delivery = self._heartbeats.drain(_HeartbeatSender(self._sender))
        self.heartbeat_deferred = delivery.rate_limited
        self.heartbeat_retry_at = self._heartbeats.retry_at
        if delivery.rate_limited:
            logger.info("live_sync heartbeat durably deferred; server presence not acknowledged")
        elif not delivery.sent:
            logger.warning("live_sync heartbeat delivery was not acknowledged")
        return delivery
