"""Ship structured log records to Grafana Cloud Loki, without ever risking the trade pipeline.

This handler is deliberately fail-silent and non-blocking: MT5 event processing must never stall,
crash, or lose data because an observability backend is slow, misconfigured, or unreachable. If
Grafana Cloud credentials were never provisioned (the common case until an operator runs
store_agent_secret.py for the three grafana_loki_* names), attaching this handler is a no-op.

Loki push API: https://grafana.com/docs/loki/latest/reference/loki-http-api/#push-log-entries
Grafana Cloud authenticates the push endpoint with HTTP Basic Auth (username = numeric stack/
instance id, password = the API token) -- both stored the same DPAPI way as every other
credential in this codebase (see windows_agent/agent_secrets.py, windows_agent/store_agent_secret.py).
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from pathlib import Path
from typing import Any

import requests

from ..agent_secrets import (
    AGENT_SCOPE_ID,
    GRAFANA_LOKI_TOKEN_SECRET_NAME,
    GRAFANA_LOKI_URL_SECRET_NAME,
    GRAFANA_LOKI_USER_SECRET_NAME,
)
from ..provisioning.secret_store import WindowsSecretStore
from ..security import RedactionFilter

# A slow or unreachable Loki must never make the queue grow without bound (memory) or block the
# caller (a full queue drops the newest record instead of waiting -- see emit()). Losing some log
# lines during an outage is an acceptable trade-off; destabilizing the agent process is not.
_MAX_QUEUE_SIZE = 5000
_MAX_BATCH_SIZE = 200
_FLUSH_INTERVAL_SECONDS = 5.0
_HTTP_TIMEOUT_SECONDS = 10.0

# This module's own failures are reported here, never through the handler being installed on the
# root logger (that would risk feeding a Loki-delivery failure back into the Loki-delivery path).
_self_logger = logging.getLogger("mt5_worker.observability.log_shipper")


def load_loki_credentials(secrets_root: Path) -> tuple[str, str, str] | None:
    """Returns (push_url, username, token), or None if never provisioned.

    A missing secret is the expected, common state (Phase 1 has not been set up yet on this VPS,
    or on a fresh install) -- never raise for it.
    """
    store = WindowsSecretStore(secrets_root)
    try:
        url = store.read(AGENT_SCOPE_ID, GRAFANA_LOKI_URL_SECRET_NAME).strip()
        user = store.read(AGENT_SCOPE_ID, GRAFANA_LOKI_USER_SECRET_NAME).strip()
        token = store.read(AGENT_SCOPE_ID, GRAFANA_LOKI_TOKEN_SECRET_NAME).strip()
    except (FileNotFoundError, OSError, ValueError):
        return None
    if not url or not user or not token:
        return None
    return url, user, token


def _stream_labels(record: logging.LogRecord) -> dict[str, str]:
    labels = {
        "service": "tradejournal-mt5-agent",
        "level": record.levelname.lower(),
        "logger": record.name,
    }
    connection_id = getattr(record, "connection_id", None)
    if isinstance(connection_id, str) and connection_id:
        labels["connection_id"] = connection_id
    return labels


class LokiLogHandler(logging.Handler):
    """A logging.Handler that batches records and pushes them to Grafana Cloud Loki.

    All network I/O happens on a single background thread; emit() only ever puts onto a bounded
    in-memory queue and returns immediately.
    """

    def __init__(
        self,
        push_url: str,
        username: str,
        token: str,
        *,
        max_batch_size: int = _MAX_BATCH_SIZE,
        flush_interval_seconds: float = _FLUSH_INTERVAL_SECONDS,
        session: Any | None = None,
    ) -> None:
        super().__init__()
        self._endpoint = push_url.rstrip("/") + "/loki/api/v1/push"
        self._auth = (username, token)
        self._queue: "queue.Queue[logging.LogRecord]" = queue.Queue(maxsize=_MAX_QUEUE_SIZE)
        self._max_batch_size = max_batch_size
        self._flush_interval_seconds = flush_interval_seconds
        self._session = session or requests.Session()
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="loki-log-shipper", daemon=True
        )
        self._thread.start()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._queue.put_nowait(record)
        except queue.Full:
            # Drop the newest record rather than block the caller or evict something already
            # queued -- a full queue means Loki is already behind, not a reason to make the
            # trade-processing thread wait on it.
            pass

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=self._flush_interval_seconds * 2)
        super().close()

    def _run(self) -> None:
        while not self._stop.is_set():
            batch = self._drain_batch()
            if batch:
                self._send(batch)
            else:
                self._stop.wait(self._flush_interval_seconds)

    def _drain_batch(self) -> list[logging.LogRecord]:
        batch: list[logging.LogRecord] = []
        deadline = time.monotonic() + self._flush_interval_seconds
        while len(batch) < self._max_batch_size and time.monotonic() < deadline:
            remaining = max(0.0, deadline - time.monotonic())
            try:
                batch.append(self._queue.get(timeout=remaining))
            except queue.Empty:
                break
        return batch

    def _send(self, batch: list[logging.LogRecord]) -> None:
        streams: dict[tuple[tuple[str, str], ...], list[list[str]]] = {}
        for record in batch:
            try:
                line = self.format(record)
            except Exception:  # noqa: BLE001 - formatting must never break shipping
                line = record.getMessage()
            labels = tuple(sorted(_stream_labels(record).items()))
            timestamp_ns = str(int(record.created * 1_000_000_000))
            streams.setdefault(labels, []).append([timestamp_ns, line])

        payload = {
            "streams": [
                {"stream": dict(labels), "values": values}
                for labels, values in streams.items()
            ]
        }
        try:
            response = self._session.post(
                self._endpoint,
                data=json.dumps(payload),
                headers={"Content-Type": "application/json"},
                auth=self._auth,
                timeout=_HTTP_TIMEOUT_SECONDS,
            )
            if response.status_code >= 300:
                _self_logger.warning(
                    "Loki push rejected (status=%s), dropping %d buffered record(s).",
                    response.status_code,
                    len(batch),
                )
        except Exception as exc:  # noqa: BLE001 - a delivery failure must never reach the caller
            # Broad on purpose: a malformed payload, a library bug, a DNS failure, or a network
            # timeout must all be treated identically here -- lose this one batch, keep the
            # background thread (and the actual trade pipeline it must never affect) alive.
            _self_logger.warning(
                "Loki push failed (%s), dropping %d buffered record(s).",
                type(exc).__name__,
                len(batch),
            )


def install_loki_handler(
    secrets_root: Path, *, level: int = logging.INFO
) -> LokiLogHandler | None:
    """Attach a LokiLogHandler to the root logger if Grafana Cloud credentials are provisioned.

    Returns the handler (so the caller can close() it on shutdown) or None when unconfigured --
    the agent runs exactly as it did before Phase 1 in that case.
    """
    credentials = load_loki_credentials(secrets_root)
    if credentials is None:
        _self_logger.info(
            "Grafana Cloud Loki credentials not provisioned; log shipping stays disabled."
        )
        return None
    url, user, token = credentials
    handler = LokiLogHandler(url, user, token)
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    # Grafana Cloud is a third-party destination: never ship a bearer token or a signed URL,
    # same redaction already applied to the local service log file.
    handler.addFilter(RedactionFilter())
    logging.getLogger().addHandler(handler)
    _self_logger.info("Grafana Cloud Loki log shipping enabled.")
    return handler
