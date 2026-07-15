from __future__ import annotations

import threading

import servicemanager
import win32event
import win32service
import win32serviceutil

from ..agent_daemon import build_runner, run_forever
from ..runtime_config import load_runtime_config


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
        servicemanager.LogInfoMsg("TradeJournal read-only agent started")
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
