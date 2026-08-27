"""HTTP delivery of live_sync events/heartbeats to the trading-mt5-events ingestion endpoint.

Reuses worker.event_sender.EventSender as-is (masked logging, exponential backoff on transient
errors, no retry on permanent 4xx rejections) -- the exact same class the self-hosted worker
connector already relies on in production, so managed (mt5_managed) connections get identical
delivery semantics instead of a second, parallel implementation.
"""

from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Any

from worker.event_outbox import DrainResult, EventOutbox
from worker.event_sender import EventSender

from .local_event_sink import LocalEventSink

logger = logging.getLogger(__name__)


class TradingIngestionSink:
    """Callable sink for LiveSync: durably logs every detected event locally first (never
    silently discarded, same guarantee LocalEventSink already provides), then attempts HTTP
    delivery. ``send`` returns the complete SendResult to the persistent outbox: transient
    failures remain pending in FIFO order, permanent failures move to its durable dead-letter,
    and the live-sync job fails instead of reporting a false success. Heartbeats likewise require
    an explicit acknowledgement.
    """

    def __init__(self, root: Path, api_url: str, bridge_token: str) -> None:
        self._local = LocalEventSink(root / "data" / "live.jsonl")
        self._sender = EventSender(api_url=api_url, bridge_token=bridge_token, dry_run=False)
        self._transition_outbox = EventOutbox(
            str(root / "state" / "connection-transition-outbox.json")
        )

    def send(self, payload: dict[str, Any]) -> Any:
        self._local(payload)
        result = self._sender.send(payload)
        if result.status == "failed":
            logger.error(
                "live_sync event delivery failed for event_id=%s (error=%s) -- kept in local "
                "audit log only",
                payload.get("event_id"),
                result.error,
            )
        return result

    def __call__(self, payload: dict[str, Any]) -> None:
        result = self.send(payload)
        if result.status != "sent":
            raise RuntimeError("ingestion delivery was not acknowledged")

    def flush_transitions(self) -> DrainResult:
        return self._transition_outbox.drain(self._sender)

    def pending_transition_count(self) -> int:
        return self._transition_outbox.pending_count()

    def send_connection_transition(
        self,
        connection_id: str,
        sequence: int,
        connected: bool,
        account_snapshot: dict[str, Any] | None = None,
    ) -> DrainResult:
        payload: dict[str, Any] = {
            "event_id": (
                f"connection-transition:{connection_id}:{sequence}:{int(connected)}"
            ),
            "event_type": "heartbeat",
            "connected": connected,
        }
        if connected and account_snapshot:
            payload.update(account_snapshot)
        self._transition_outbox.enqueue_many([payload])
        return self.flush_transitions()

    def send_heartbeat(self, account_info: Any | None = None) -> bool:
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
        result = self._sender.send(payload)
        if result.status == "failed":
            logger.warning("live_sync heartbeat delivery failed (error=%s)", result.error)
            return False
        return True
