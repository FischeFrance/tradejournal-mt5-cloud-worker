from __future__ import annotations

import importlib
import sys
import threading
import time
from types import ModuleType, SimpleNamespace

import pytest


def _load_service_module(monkeypatch: pytest.MonkeyPatch):
    info_messages: list[str] = []
    error_messages: list[str] = []

    servicemanager = ModuleType("servicemanager")
    servicemanager.LogInfoMsg = info_messages.append  # type: ignore[attr-defined]
    servicemanager.LogErrorMsg = error_messages.append  # type: ignore[attr-defined]

    win32event = ModuleType("win32event")
    win32event.INFINITE = -1  # type: ignore[attr-defined]
    win32event.CreateEvent = (  # type: ignore[attr-defined]
        lambda *_args: threading.Event()
    )
    win32event.SetEvent = lambda event: event.set()  # type: ignore[attr-defined]
    win32event.WaitForSingleObject = (  # type: ignore[attr-defined]
        lambda event, _timeout: event.wait(2)
    )

    win32service = ModuleType("win32service")
    win32service.SERVICE_STOP_PENDING = 3  # type: ignore[attr-defined]
    win32service.SERVICE_RUNNING = 4  # type: ignore[attr-defined]

    win32serviceutil = ModuleType("win32serviceutil")

    class ServiceFramework:
        def __init__(self, _args):
            self.reported_statuses: list[int] = []

        def ReportServiceStatus(self, status: int) -> None:
            self.reported_statuses.append(status)

    win32serviceutil.ServiceFramework = ServiceFramework  # type: ignore[attr-defined]
    win32serviceutil.HandleCommandLine = lambda _service: None  # type: ignore[attr-defined]

    for name, module in (
        ("servicemanager", servicemanager),
        ("win32event", win32event),
        ("win32service", win32service),
        ("win32serviceutil", win32serviceutil),
    ):
        monkeypatch.setitem(sys.modules, name, module)

    sys.modules.pop("windows_agent.service.windows_service", None)
    module = importlib.import_module("windows_agent.service.windows_service")
    return module, info_messages, error_messages


def test_worker_failure_wakes_service_thread_and_requests_scm_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, _info_messages, error_messages = _load_service_module(monkeypatch)
    runner = SimpleNamespace(lifecycle_coordinator=object())
    supervisor = object()
    readiness_calls: list[object] = []
    monkeypatch.setattr(module, "_configure_logging", lambda: None)
    monkeypatch.setattr(
        module,
        "_prepare_service_activation",
        lambda: object(),
    )
    monkeypatch.setattr(
        module,
        "_publish_service_readiness",
        readiness_calls.append,
    )
    monkeypatch.setattr(
        module,
        "_remove_service_readiness",
        lambda _activation: None,
    )
    monkeypatch.setattr(module, "load_runtime_config", lambda: object())
    monkeypatch.setattr(module, "build_runner", lambda _config: runner)
    monkeypatch.setattr(
        module,
        "build_event_supervisor",
        lambda _config, _lifecycle: supervisor,
    )

    def fail_worker(*_args, **_kwargs) -> None:
        raise ValueError("fixture worker failure")

    monkeypatch.setattr(module, "run_forever", fail_worker)
    service = module.TradeJournalAgentService([])

    with pytest.raises(RuntimeError, match="agent worker failed") as raised:
        service.SvcDoRun()

    assert raised.value.__cause__ is None
    assert service.stop_event.is_set()
    assert error_messages[-1] == (
        "TradeJournal agent worker failed; service recovery requested"
    )
    assert readiness_calls == []


def test_post_readiness_worker_failure_removes_readiness_and_requests_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, _info_messages, error_messages = _load_service_module(monkeypatch)
    runner = SimpleNamespace(lifecycle_coordinator=object())
    activation = object()
    published: list[object] = []
    removed: list[object] = []
    monkeypatch.setattr(module, "_configure_logging", lambda: None)
    monkeypatch.setattr(
        module,
        "_prepare_service_activation",
        lambda: activation,
    )
    monkeypatch.setattr(module, "load_runtime_config", lambda: object())
    monkeypatch.setattr(module, "build_runner", lambda _config: runner)
    monkeypatch.setattr(
        module,
        "build_event_supervisor",
        lambda _config, _lifecycle: object(),
    )
    monkeypatch.setattr(module, "_publish_service_readiness", published.append)
    monkeypatch.setattr(module, "_remove_service_readiness", removed.append)

    def fail_after_ready(*_args, **kwargs) -> None:
        kwargs["ready_event"].set()
        time.sleep(0.1)
        raise RuntimeError("fixture post-readiness failure")

    monkeypatch.setattr(module, "run_forever", fail_after_ready)
    service = module.TradeJournalAgentService([])

    with pytest.raises(RuntimeError, match="agent worker failed"):
        service.SvcDoRun()

    assert published == [activation]
    assert removed == [activation]
    assert error_messages[-1] == (
        "TradeJournal agent worker failed; service recovery requested"
    )
