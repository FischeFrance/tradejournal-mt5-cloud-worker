from __future__ import annotations

import threading
import logging
from pathlib import Path

import servicemanager
import win32event
import win32service
import win32serviceutil

from windows_agent.agent_daemon import build_runner, run_forever
from windows_agent.runtime_config import load_runtime_config
from windows_agent.security import RedactionFilter


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
            config = load_runtime_config()
            runner = build_runner(config)
        except Exception as exc:
            servicemanager.LogErrorMsg(f"TradeJournal agent failed to start: {exc}")
            win32event.WaitForSingleObject(self.stop_event, win32event.INFINITE)
            return

        self._worker = threading.Thread(
            target=run_forever, args=(runner, config.poll_seconds, self._stop_signal), daemon=True
        )
        self._worker.start()
        win32event.WaitForSingleObject(self.stop_event, win32event.INFINITE)
        self._worker.join(timeout=30)
        servicemanager.LogInfoMsg("TradeJournal read-only agent stopped")


if __name__ == "__main__":
    win32serviceutil.HandleCommandLine(TradeJournalAgentService)
