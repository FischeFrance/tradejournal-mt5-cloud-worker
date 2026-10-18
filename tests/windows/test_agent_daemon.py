from __future__ import annotations

import threading
import time
from unittest.mock import Mock

import pytest

from windows_agent.agent_daemon import default_handlers, run_forever
from windows_agent.job_runner import JobRunner


@pytest.mark.parametrize("enabled", [False, True])
def test_build_runner_passes_single_start_gate_to_native_factory(tmp_path, monkeypatch, enabled):
    import windows_agent.agent_daemon as daemon
    from windows_agent.runtime_config import AgentRuntimeConfig

    config = AgentRuntimeConfig(
        base_url="https://agent.example/trading-agent", poll_seconds=5, secrets_root=tmp_path / "secrets",
        instances_root=tmp_path / "instances", instance_pool_target_size=0,
        mt5_single_start_enabled=enabled,
    )
    monkeypatch.setattr(daemon, "build_api_client", lambda _config: QueueApi([]))
    monkeypatch.setattr(daemon, "reconcile_startup_instances", lambda *_args: Mock(
        adopted=(), missing=(), terminated=(), blocked=()
    ))
    handlers = Mock(return_value={"provision": lambda _job: {}})
    monkeypatch.setattr(daemon, "build_real_handlers", handlers)
    daemon.build_runner(config, state_path=tmp_path / "state.json")
    factory = handlers.call_args.kwargs["runtime_factory"]
    runtime = factory(tmp_path / "isolated", "00000000-0000-4000-8000-000000000001")
    assert runtime._single_start_enabled is enabled
    assert runtime._bootstrap_symbol_cache.root == tmp_path / "state" / "broker-bootstrap-symbols"
    assert not runtime._bootstrap_symbol_cache.root.exists()


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


def test_run_forever_polls_until_stop_event(tmp_path):
    api = QueueApi([])
    runner = JobRunner(tmp_path / "state.json", api, default_handlers())
    stop_event = threading.Event()
    thread = threading.Thread(target=run_forever, args=(runner, 0.01, stop_event))
    thread.start()
    time.sleep(0.05)
    stop_event.set()
    thread.join(timeout=2)
    assert not thread.is_alive()


def test_run_forever_fails_unimplemented_handler_safely(tmp_path):
    job = {"job_id": "j1", "job_type": "provision", "connection_id": "c1", "lease_id": "l1"}
    api = QueueApi([job])
    runner = JobRunner(tmp_path / "state.json", api, default_handlers())
    stop_event = threading.Event()

    def stop_after_first_claim():
        while not api.transitions:
            time.sleep(0.005)
        stop_event.set()

    watcher = threading.Thread(target=stop_after_first_claim)
    watcher.start()
    run_forever(runner, 0.01, stop_event)
    watcher.join(timeout=2)

    assert ("running", None) in api.transitions
    assert any(status == "fail" and result == {"error_code": "notimplementederror"} for status, result in api.transitions)


def test_default_handlers_raise_not_implemented():
    handlers = default_handlers()
    for name in ("provision", "deprovision", "historical_sync"):
        try:
            handlers[name]({})
        except NotImplementedError as exc:
            assert name in str(exc)
        else:
            raise AssertionError(f"{name} handler should have raised NotImplementedError")


def test_run_forever_starts_and_stops_background_worker(tmp_path):
    api = QueueApi([])
    started = threading.Event()
    stopped = threading.Event()

    def background_worker(stop_event):
        started.set()
        stop_event.wait()
        stopped.set()

    runner = JobRunner(
        tmp_path / "state.json",
        api,
        default_handlers(),
        background_workers=(background_worker,),
    )
    stop_event = threading.Event()
    thread = threading.Thread(
        target=run_forever,
        args=(runner, 0.01, stop_event),
    )
    thread.start()

    assert started.wait(timeout=1)
    stop_event.set()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert stopped.is_set()
