from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import windows_agent.agent_daemon as agent_daemon

from windows_agent.agent_daemon import (
    StartupLiveUpdateRecovery,
    _drain_available_jobs,
    build_runner,
    default_handlers,
    recover_startup_live_updates,
    run_forever,
)
from windows_agent.job_runner import JobRunner
from windows_agent.worker.native_mt5_runtime import (
    NativeMt5Error,
    NativeMt5UpdateRecovery,
)


CONNECTION_ID = "00000000-0000-4000-8000-000000000001"


class QueueApi:
    def __init__(self, jobs):
        self.jobs = list(jobs)
        self.transitions = []

    def claim(self):
        return self.jobs.pop(0) if self.jobs else {}

    def heartbeat(self, job_id, lease_id):
        return {"lease_valid": True}

    def transition(self, job_id, lease_id, status, result=None):
        self.transitions.append((status, result))
        return {"status": "failed" if status == "fail" else status}


class StartupOnlyWakeListener:
    def run(self, wake_event, stop_event):
        wake_event.set()
        stop_event.wait()


def test_run_forever_waits_without_recurring_claims(tmp_path):
    api = QueueApi([])
    runner = JobRunner(tmp_path / "state.json", api, default_handlers())
    stop_event = threading.Event()
    thread = threading.Thread(
        target=run_forever, args=(runner, stop_event, StartupOnlyWakeListener())
    )
    thread.start()
    time.sleep(0.05)
    stop_event.set()
    thread.join(timeout=2)
    assert not thread.is_alive()


def test_event_supervisor_startup_failure_is_propagated_before_readiness(
    tmp_path,
):
    runner = JobRunner(
        tmp_path / "state.json",
        QueueApi([]),
        default_handlers(),
    )
    stop_event = threading.Event()
    ready_event = threading.Event()

    class FailingSupervisor:
        @staticmethod
        def run(_stop_event, _component_ready):
            raise RuntimeError("observer failed")

    with pytest.raises(RuntimeError, match="critical agent component failed"):
        run_forever(
            runner,
            stop_event,
            StartupOnlyWakeListener(),
            FailingSupervisor(),  # type: ignore[arg-type]
            ready_event=ready_event,
            component_startup_timeout_seconds=0.5,
        )

    assert not ready_event.is_set()


def test_event_supervisor_failure_after_readiness_stops_daemon(tmp_path):
    runner = JobRunner(
        tmp_path / "state.json",
        QueueApi([]),
        default_handlers(),
    )
    stop_event = threading.Event()
    ready_event = threading.Event()

    class LateFailingSupervisor:
        @staticmethod
        def run(_stop_event, component_ready):
            component_ready.set()
            time.sleep(0.1)
            raise RuntimeError("observer died")

    with pytest.raises(RuntimeError, match="critical agent component failed"):
        run_forever(
            runner,
            stop_event,
            StartupOnlyWakeListener(),
            LateFailingSupervisor(),  # type: ignore[arg-type]
            ready_event=ready_event,
            component_startup_timeout_seconds=0.5,
        )

    assert ready_event.is_set()


def test_run_forever_fails_unimplemented_handler_safely(tmp_path):
    job = {
        "job_id": "j1",
        "job_type": "provision",
        "connection_id": "c1",
        "lease_id": "l1",
    }
    api = QueueApi([job])
    runner = JobRunner(tmp_path / "state.json", api, default_handlers())
    stop_event = threading.Event()

    def stop_after_first_claim():
        while not api.transitions:
            time.sleep(0.005)
        stop_event.set()

    watcher = threading.Thread(target=stop_after_first_claim)
    watcher.start()
    run_forever(runner, stop_event, StartupOnlyWakeListener())
    watcher.join(timeout=2)

    assert ("running", None) in api.transitions
    assert any(
        status == "fail" and result == {"error_code": "notimplementederror"}
        for status, result in api.transitions
    )


def test_default_handlers_raise_not_implemented():
    handlers = default_handlers()
    for name in ("provision", "deprovision", "historical_sync"):
        try:
            handlers[name]({})
        except NotImplementedError as exc:
            assert name in str(exc)
        else:
            raise AssertionError(
                f"{name} handler should have raised NotImplementedError"
            )


def test_due_maintenance_prevents_a_new_job_claim(tmp_path):
    api = QueueApi(
        [
            {
                "job_id": "j1",
                "job_type": "provision",
                "connection_id": "c1",
                "lease_id": "l1",
            }
        ]
    )
    runner = JobRunner(tmp_path / "state.json", api, default_handlers())

    class DueMaintenance:
        @staticmethod
        def is_due():
            return True

    runner.scheduled_maintenance = DueMaintenance()
    _drain_available_jobs(runner, threading.Event())

    assert len(api.jobs) == 1
    assert api.transitions == []


def test_jobs_are_drained_after_maintenance_consumes_the_startup_wake(tmp_path):
    stop_event = threading.Event()
    api = QueueApi(
        [
            {
                "job_id": "j1",
                "job_type": "provision",
                "connection_id": "c1",
                "lease_id": "l1",
            }
        ]
    )

    def provision(_job):
        stop_event.set()
        return {"ok": True}

    runner = JobRunner(
        tmp_path / "state.json",
        api,
        {"provision": provision},
    )

    class MaintenanceBecomesDueDuringDrain:
        def __init__(self):
            self.due = False
            self.triggered = False
            self.runs = 0

        def is_due(self):
            if not self.triggered:
                self.triggered = True
                self.due = True
            return self.due

        def run_if_due(self, _stop_event):
            if not self.due:
                return False
            self.due = False
            self.runs += 1
            return True

    maintenance = MaintenanceBecomesDueDuringDrain()
    runner.scheduled_maintenance = maintenance

    thread = threading.Thread(
        target=run_forever,
        args=(runner, stop_event, StartupOnlyWakeListener()),
    )
    thread.start()
    thread.join(timeout=2)
    if thread.is_alive():
        stop_event.set()
        thread.join(timeout=2)

    assert not thread.is_alive()
    assert maintenance.runs == 1
    assert api.jobs == []
    assert ("complete", {"result": {"ok": True}}) in api.transitions


def test_startup_live_update_preflight_returns_only_sanitized_ids(
    tmp_path: Path,
) -> None:
    instance = tmp_path / CONNECTION_ID
    instance.mkdir()

    class Runtime:
        stopped = False

        def __init__(self, root: Path, connection_id: str) -> None:
            assert root == instance
            assert connection_id == CONNECTION_ID

        @staticmethod
        def recover_interrupted_live_updates() -> NativeMt5UpdateRecovery:
            return NativeMt5UpdateRecovery(
                sealed_applied=1,
                pending_health=1,
            )

        @classmethod
        def stop(cls) -> bool:
            cls.stopped = True
            return True

    report = recover_startup_live_updates(
        tmp_path,
        runtime_factory=Runtime,  # type: ignore[arg-type]
    )

    assert report == StartupLiveUpdateRecovery(
        sealed_applied=(CONNECTION_ID,),
        restart_required=(CONNECTION_ID,),
    )
    assert Runtime.stopped is True


def test_startup_live_update_preflight_fails_closed_without_exception_text(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    (tmp_path / CONNECTION_ID).mkdir()

    class Runtime:
        def __init__(self, _root: Path, _connection_id: str) -> None:
            pass

        @staticmethod
        def recover_interrupted_live_updates() -> NativeMt5UpdateRecovery:
            raise RuntimeError("Password=investor-secret")

    with pytest.raises(
        NativeMt5Error,
        match="^mt5_update_startup_preflight_failed$",
    ):
        recover_startup_live_updates(
            tmp_path,
            runtime_factory=Runtime,  # type: ignore[arg-type]
        )

    assert "investor-secret" not in caplog.text
    assert CONNECTION_ID in caplog.text


def test_startup_live_update_preflight_defers_missing_rotation_terminal(
    tmp_path: Path,
) -> None:
    instance = tmp_path / CONNECTION_ID
    state = instance / "state"
    state.mkdir(parents=True)
    (state / "mt5-rotation.json").write_text("{}", encoding="utf-8")

    class Runtime:
        def __init__(self, _root: Path, _connection_id: str) -> None:
            raise AssertionError("rotation recovery must run first")

    report = recover_startup_live_updates(
        tmp_path,
        runtime_factory=Runtime,  # type: ignore[arg-type]
    )

    assert report == StartupLiveUpdateRecovery()


def test_build_runner_runs_live_update_preflight_before_reconciliation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class TemplateManager:
        current_sha256 = "a" * 64

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

    class Rotator:
        recovery_kwargs: dict[str, object] = {}

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        @classmethod
        def recover_incomplete(cls, **kwargs: object) -> SimpleNamespace:
            cls.recovery_kwargs = kwargs
            events.append("rotation")
            return SimpleNamespace(recovered=(), failed=())

    class PendingStore:
        roots: list[Path] = []

        def __init__(self, root: Path) -> None:
            self.roots.append(root)

        @staticmethod
        def capture(*_args: object) -> object:
            return object()

    def preflight(_instances_root: Path) -> StartupLiveUpdateRecovery:
        events.append("preflight")
        return StartupLiveUpdateRecovery()

    def reconcile(_instances_root: Path, _secrets_root: Path) -> object:
        events.append("reconcile")
        raise RuntimeError("stop-after-order-check")

    monkeypatch.setattr(agent_daemon, "build_api_client", lambda _config: object())
    monkeypatch.setattr(agent_daemon, "Mt5TemplateManager", TemplateManager)
    monkeypatch.setattr(agent_daemon, "Mt5InstanceRotator", Rotator)
    monkeypatch.setattr(agent_daemon, "Mt5PendingUpdateStore", PendingStore)
    monkeypatch.setattr(agent_daemon, "recover_startup_live_updates", preflight)
    monkeypatch.setattr(agent_daemon, "reconcile_startup_instances", reconcile)
    config = SimpleNamespace(
        source_terminal=tmp_path / "template" / "terminal64.exe",
        terminal_sha256="a" * 64,
        instances_root=tmp_path / "instances",
        secrets_root=tmp_path / "secrets",
        expert_binary=tmp_path / "bridge.ex5",
        expert_sha256="b" * 64,
        mt5_maintenance_enabled=False,
        mt5_maintenance_state_path=tmp_path / "state" / "maintenance.json",
    )

    with pytest.raises(RuntimeError, match="stop-after-order-check"):
        build_runner(
            config,  # type: ignore[arg-type]
            state_path=tmp_path / "agent-job.json",
        )

    assert events == [
        "preflight",
        "rotation",
        "preflight",
        "reconcile",
    ]
    assert PendingStore.roots == [tmp_path / "state" / "mt5-update-pending"]
    assert Rotator.recovery_kwargs["verified_update_required"] is True
    assert callable(
        Rotator.recovery_kwargs["verified_update_callback"]
    )
