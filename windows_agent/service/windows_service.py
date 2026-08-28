from __future__ import annotations

import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import servicemanager
import win32event
import win32service
import win32serviceutil

from windows_agent.agent_daemon import build_event_supervisor, build_runner, run_forever
from windows_agent.deploy_guard import (
    SERVICE_READINESS_PATH,
    assert_service_activation_allowed,
)
from windows_agent.provisioning.secret_store import WindowsSecretStore
from windows_agent.release_manifest import verify_release
from windows_agent.runtime_config import load_runtime_config
from windows_agent.security import RedactionFilter, canonical_uuid
from windows_agent.state_store import atomic_json

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _ServiceActivation:
    source_revision: str
    deployment_id: str
    activation_started_at_unix_ms: int
    readiness_path: Path


def _prepare_service_activation() -> _ServiceActivation:
    """Bind this process to the write-once deployment barrier before startup."""

    revision = os.environ.get(
        "TRADEJOURNAL_AGENT_RELEASE_REVISION",
        "",
    ).strip().lower()
    deployment_id = os.environ.get(
        "TRADEJOURNAL_AGENT_DEPLOYMENT_ID",
        "",
    ).strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise RuntimeError("service release revision is unavailable")
    try:
        deployment_id = canonical_uuid(deployment_id)
    except ValueError as exc:
        raise RuntimeError("service deployment identity is unavailable") from exc
    release_root = Path(__file__).resolve().parents[2]
    manifest = verify_release(release_root)
    if manifest.get("source_revision") != revision:
        raise RuntimeError("service release manifest does not match activation")
    barrier = assert_service_activation_allowed(revision, deployment_id)
    activation_started = barrier.get("activation_started_at_unix_ms")
    if (
        not isinstance(activation_started, int)
        or isinstance(activation_started, bool)
        or activation_started <= 0
    ):
        raise RuntimeError("service activation barrier is invalid")
    readiness_path = Path(
        os.environ.get(
            "TRADEJOURNAL_AGENT_READINESS_PATH",
            str(SERVICE_READINESS_PATH),
        ).strip()
    ).resolve()
    if readiness_path != SERVICE_READINESS_PATH.resolve():
        raise RuntimeError("service readiness path is invalid")
    try:
        readiness_path.unlink(missing_ok=True)
    except OSError as exc:
        raise RuntimeError("stale service readiness cannot be removed") from exc
    return _ServiceActivation(
        revision,
        deployment_id,
        activation_started,
        readiness_path,
    )


def _publish_service_readiness(activation: _ServiceActivation) -> None:
    atomic_json(
        activation.readiness_path,
        {
            "schema_version": 1,
            "source_revision": activation.source_revision,
            "deployment_id": activation.deployment_id,
            "service_process_id": os.getpid(),
            "activation_started_at_unix_ms": (
                activation.activation_started_at_unix_ms
            ),
            "ready_at_unix_ms": int(time.time() * 1000),
        },
    )
    WindowsSecretStore.restrict_acl(activation.readiness_path)


def _remove_service_readiness(activation: _ServiceActivation) -> None:
    try:
        activation.readiness_path.unlink(missing_ok=True)
    except OSError:
        logger.error("TradeJournal service readiness cleanup failed")


def _configure_logging() -> None:
    """Persist service failures locally without allowing credentials into logs."""
    root = logging.getLogger()
    if any(getattr(handler, "_tradejournal_service", False) for handler in root.handlers):
        return
    Path(r"C:\TradeJournal\logs").mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(r"C:\TradeJournal\logs\agent-service.log", encoding="utf-8")
    handler._tradejournal_service = True  # type: ignore[attr-defined]
    handler.addFilter(RedactionFilter())
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root.addHandler(handler)
    root.setLevel(logging.INFO)


class TradeJournalAgentService(win32serviceutil.ServiceFramework):
    _svc_name_ = "TradeJournalMT5Agent"
    _svc_display_name_ = "TradeJournal MT5 Read-Only Agent"

    def __init__(self, args):
        super().__init__(args)
        self.stop_event = win32event.CreateEvent(None, 0, 0, None)
        self._stop_signal = threading.Event()
        self._worker: threading.Thread | None = None

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        self._stop_signal.set()
        win32event.SetEvent(self.stop_event)

    def SvcDoRun(self):
        _configure_logging()
        servicemanager.LogInfoMsg("TradeJournal read-only agent started")
        # Build-up includes loading DPAPI-protected credentials and the MT5 runtime.  Tell the
        # Service Control Manager immediately that the process is alive so its fixed start
        # timeout cannot kill an otherwise healthy agent during that work.
        self.ReportServiceStatus(win32service.SERVICE_RUNNING)
        try:
            activation = _prepare_service_activation()
            config = load_runtime_config()
            runner = build_runner(config)
            event_supervisor = build_event_supervisor(
                config,
                runner.lifecycle_coordinator,
            )
        except Exception:
            # Do not leave a process that SCM considers healthy but that can
            # never claim jobs.  A deterministic non-zero exit is required so
            # the service recovery policy can restart the Agent.  The detailed
            # exception is deliberately kept out of the event log because a
            # configuration failure may contain sensitive deployment paths.
            servicemanager.LogErrorMsg(
                "TradeJournal agent failed to start; service recovery requested"
            )
            raise RuntimeError("TradeJournal agent startup failed")

        worker_failed = threading.Event()
        worker_ready = threading.Event()

        def run_worker() -> None:
            try:
                run_forever(
                    runner,
                    self._stop_signal,
                    event_supervisor=event_supervisor,
                    ready_event=worker_ready,
                )
            except BaseException as exc:
                # Log only the exception class. Arbitrary exception text and
                # tracebacks can include deployment paths or process arguments.
                logger.error(
                    "TradeJournal worker stopped unexpectedly (error=%s)",
                    type(exc).__name__,
                )
                worker_failed.set()
            else:
                if not self._stop_signal.is_set():
                    worker_failed.set()
            finally:
                # Wake the service thread even when the daemon fails before an
                # operator stop. Otherwise SCM would keep reporting RUNNING
                # while no worker remains to claim durable jobs.
                win32event.SetEvent(self.stop_event)

        self._worker = threading.Thread(target=run_worker, daemon=True)
        self._worker.start()
        startup_deadline = time.monotonic() + 45.0
        while not worker_ready.wait(0.05):
            if worker_failed.is_set() or self._stop_signal.is_set():
                break
            if time.monotonic() >= startup_deadline:
                worker_failed.set()
                self._stop_signal.set()
                win32event.SetEvent(self.stop_event)
                break
        if worker_ready.is_set() and not worker_failed.is_set():
            try:
                _publish_service_readiness(activation)
            except Exception:
                self._stop_signal.set()
                win32event.SetEvent(self.stop_event)
                worker_failed.set()
        try:
            win32event.WaitForSingleObject(
                self.stop_event,
                win32event.INFINITE,
            )
            self._worker.join(timeout=30)
            if self._worker.is_alive():
                worker_failed.set()
            if worker_failed.is_set():
                servicemanager.LogErrorMsg(
                    "TradeJournal agent worker failed; service recovery requested"
                )
                raise RuntimeError("TradeJournal agent worker failed") from None
            servicemanager.LogInfoMsg("TradeJournal read-only agent stopped")
        finally:
            _remove_service_readiness(activation)


if __name__ == "__main__":
    win32serviceutil.HandleCommandLine(TradeJournalAgentService)
