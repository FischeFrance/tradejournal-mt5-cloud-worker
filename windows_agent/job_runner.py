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
                # A transport failure makes lease ownership unknowable. Continuing would allow
                # this agent to overlap a server-side reclaim, so uncertainty is treated exactly
                # like an explicit lease loss.
                self._lease_lost.set()
                return
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

    def require_valid(self) -> None:
        if self.lease_lost:
            raise LeaseLost("lease ownership is lost or uncertain")


class JobRunner:
    def __init__(
        self,
        state: Path,
        api: Any,
        handlers: dict[str, Callable[[dict], dict]],
        heartbeat_interval_seconds: float = HEARTBEAT_INTERVAL_SECONDS,
        background_workers: tuple[
            Callable[[threading.Event], None], ...
        ] = (),
    ) -> None:
        self.state, self.api, self.handlers = state, api, handlers
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        self.background_workers = background_workers

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
        try:
            self._transition(job, "running")
            keeper = _LeaseKeeper(
                self.api, job["job_id"], job["lease_id"], self.heartbeat_interval_seconds
            )
            with keeper:
                # Real handlers consult this private, in-process guard at every explicit lease
                # checkpoint. It is never serialized or sent to the control plane.
                job["_lease_guard"] = keeper.require_valid
                result = self.handlers[minimal["action"]](job)
            keeper.require_valid()
            heartbeat = self.api.heartbeat(job["job_id"], job["lease_id"])
            if not heartbeat.get("lease_valid", False):
                raise LeaseLost("lease lost")
            self._transition(job, "complete", {"result": result})
            atomic_json(self.state, {**minimal, "status": "complete"})
        except LeaseLost:
            atomic_json(self.state, {**minimal, "status": "lease_lost"})
            return False
        except Exception as exc:
            error_code = getattr(exc, "error_code", None) or type(exc).__name__.lower()
            # Full detail (message, chained cause, traceback) stays in the local log only -- the
            # control plane only ever receives the sanitized error_code (see agent_errors.py).
            logger.exception("job %s failed (job_type=%s): sending error_code=%s", job["job_id"], minimal["action"], error_code)
            try:
                self._transition(job, "fail", {"error_code": error_code})
            except Exception:
                # The handler failed, but without an acknowledged terminal transition the server
                # remains authoritative. Record uncertainty rather than a false local "failed".
                atomic_json(self.state, {**minimal, "status": "lease_lost"})
                return False
            try:
                atomic_json(
                    self.state, {**minimal, "status": "failed", "error": type(exc).__name__}
                )
            except ValueError:
                # atomic_json's secrets-forbidden guard matches any quoted JSON string that
                # *starts* with a marker word (see state_store.py) -- a coincidental case, not an
                # actual secret: SecretStoreFailed itself starts with "secret". The control plane
                # already has the real, sanitized error_code from the transition() call above;
                # local diagnostic state losing the class name here is a cosmetic downgrade, not a
                # reason to let this crash escape run_once() and mask the job's real failure.
                atomic_json(self.state, {**minimal, "status": "failed", "error": "redacted"})
        return True

    @staticmethod
    def _require_transition(response: object, expected_status: str) -> None:
        if not isinstance(response, dict):
            raise LeaseLost("transition response is not an object")
        if response.get("error_code") == "lease_lost":
            raise LeaseLost("lease lost during transition")
        if response.get("status") != expected_status:
            raise LeaseLost("transition was not acknowledged")

    def _transition(
        self, job: dict, status: str, result: dict | None = None
    ) -> None:
        try:
            response = self.api.transition(
                job["job_id"], job["lease_id"], status, result
            )
        except Exception as exc:
            raise LeaseLost("transition acknowledgement is uncertain") from exc
        expected = "failed" if status == "fail" else status
        self._require_transition(response, expected)

    def recover(self) -> dict:
        return read_json(self.state)
