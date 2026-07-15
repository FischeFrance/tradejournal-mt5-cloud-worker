from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Callable

from .state_store import atomic_json, read_json

logger = logging.getLogger(__name__)

HEARTBEAT_INTERVAL_SECONDS = 20.0


class LeaseLost(RuntimeError):
    pass


class _LeaseKeeper:
    """Renews the job lease on a background timer while a handler runs.

    Without this, a handler that runs longer than the server's lease window
    (60s) would have its job silently reclaimed by another agent (the claim
    RPC treats any job whose lease_expires_at has passed as claimable again),
    causing two agents to work the same job concurrently. Heartbeat calls are
    pure HTTP (no MT5 IPC), so running them from a background thread while the
    handler drives MT5 on the main thread is safe.
    """

    def __init__(self, api: Any, job_id: str, lease_id: str, interval_seconds: float) -> None:
        self._api, self._job_id, self._lease_id = api, job_id, lease_id
        self._interval = interval_seconds
        self._stop = threading.Event()
        self._lease_lost = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                response = self._api.heartbeat(self._job_id, self._lease_id)
            except Exception:
                continue
            if not response.get("lease_valid", False):
                self._lease_lost.set()
                return

    def __enter__(self) -> "_LeaseKeeper":
        self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._stop.set()
        self._thread.join(timeout=self._interval)

    @property
    def lease_lost(self) -> bool:
        return self._lease_lost.is_set()


class JobRunner:
    def __init__(
        self,
        state: Path,
        api: Any,
        handlers: dict[str, Callable[[dict], dict]],
        heartbeat_interval_seconds: float = HEARTBEAT_INTERVAL_SECONDS,
    ) -> None:
        self.state, self.api, self.handlers = state, api, handlers
        self.heartbeat_interval_seconds = heartbeat_interval_seconds

    def run_once(self) -> bool:
        job = self.api.claim()
        if not job:
            return False
        minimal = {
            "job_id": job["job_id"],
            "action": job.get("job_type", job.get("action")),
            "connection_id": job["connection_id"],
            "lease_id": job["lease_id"],
            "status": "running",
        }
        atomic_json(self.state, minimal)
        self.api.transition(job["job_id"], job["lease_id"], "running")
        try:
            keeper = _LeaseKeeper(
                self.api, job["job_id"], job["lease_id"], self.heartbeat_interval_seconds
            )
            with keeper:
                result = self.handlers[minimal["action"]](job)
            if keeper.lease_lost:
                raise LeaseLost("lease lost during handler execution")
            heartbeat = self.api.heartbeat(job["job_id"], job["lease_id"])
            if not heartbeat.get("lease_valid", False):
                raise LeaseLost("lease lost")
            self.api.transition(job["job_id"], job["lease_id"], "complete", {"result": result})
            atomic_json(self.state, {**minimal, "status": "complete"})
        except LeaseLost:
            atomic_json(self.state, {**minimal, "status": "lease_lost"})
            return False
        except Exception as exc:
            error_code = getattr(exc, "error_code", None) or type(exc).__name__.lower()
            # Full detail (message, chained cause, traceback) stays in the local log only -- the
            # control plane only ever receives the sanitized error_code (see agent_errors.py).
            logger.exception("job %s failed (job_type=%s): sending error_code=%s", job["job_id"], minimal["action"], error_code)
            self.api.transition(job["job_id"], job["lease_id"], "fail", {"error_code": error_code})
            atomic_json(
                self.state, {**minimal, "status": "failed", "error": type(exc).__name__}
            )
        return True

    def recover(self) -> dict:
        return read_json(self.state)
