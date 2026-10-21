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


class CountingApi(QueueApi):
    def __init__(self, jobs=()):
        super().__init__(jobs)
        self.claims = 0

    def claim(self):
        self.claims += 1
        return super().claim()


class FakeListener:
    def __init__(self, joined):
        self.joined = joined
        self.wake_event = None
        self.ready = threading.Event()

    def run(self, wake_event, stop_event):
        self.wake_event = wake_event
        self.ready.set()
        stop_event.wait()


class CountingMaintenance:
    def __init__(self):
        self.checks = 0

    def run_if_due(self, stop_event):
        self.checks += 1
        return False


def _run_for(runner, seconds, **kwargs):
    stop_event = threading.Event()
    thread = threading.Thread(target=run_forever, args=(runner, 0.01, stop_event), kwargs=kwargs)
    thread.start()
    time.sleep(seconds)
    stop_event.set()
    thread.join(timeout=3)
    assert not thread.is_alive()


def test_without_a_joined_channel_every_tick_still_claims(tmp_path):
    # Realtime down or refused: the Agent must keep today's short poll.
    api = CountingApi()
    runner = JobRunner(tmp_path / "state.json", api, default_handlers())
    _run_for(runner, 0.3, wake_listener=FakeListener(joined=False), wake_fallback_seconds=60)
    assert api.claims >= 5


def test_a_joined_channel_spaces_idle_claims_but_keeps_maintenance_on_every_tick(tmp_path):
    api = CountingApi()
    maintenance = CountingMaintenance()
    runner = JobRunner(tmp_path / "state.json", api, default_handlers(), scheduled_maintenance=maintenance)
    _run_for(runner, 0.3, wake_listener=FakeListener(joined=True), wake_fallback_seconds=60)
    assert api.claims == 1  # the start-up claim; the next safety claim is a minute away
    assert maintenance.checks >= 5


def test_a_joined_channel_still_claims_when_the_safety_cadence_is_due(tmp_path):
    api = CountingApi()
    runner = JobRunner(tmp_path / "state.json", api, default_handlers())
    _run_for(runner, 0.4, wake_listener=FakeListener(joined=True), wake_fallback_seconds=0.05)
    assert api.claims >= 3


def test_a_wake_up_claims_at_once_and_drains_every_waiting_job(tmp_path):
    api = CountingApi()
    runner = JobRunner(tmp_path / "state.json", api, default_handlers())
    listener = FakeListener(joined=True)
    stop_event = threading.Event()
    thread = threading.Thread(
        target=run_forever,
        args=(runner, 0.01, stop_event),
        kwargs={"wake_listener": listener, "wake_fallback_seconds": 60},
    )
    thread.start()
    assert listener.ready.wait(timeout=1)
    time.sleep(0.1)
    assert api.claims == 1
    for number in (1, 2):
        api.jobs.append({"job_id": f"j{number}", "job_type": "provision", "connection_id": "c1", "lease_id": f"l{number}"})
    listener.wake_event.set()
    deadline = time.monotonic() + 2
    while api.claims < 4 and time.monotonic() < deadline:
        time.sleep(0.01)
    stop_event.set()
    thread.join(timeout=3)
    # Two jobs claimed back to back, then the empty claim that ends the drain.
    assert api.claims == 4
    assert [status for status, _ in api.transitions].count("running") == 2


def test_losing_the_channel_returns_to_the_short_poll(tmp_path):
    api = CountingApi()
    runner = JobRunner(tmp_path / "state.json", api, default_handlers())
    listener = FakeListener(joined=True)
    stop_event = threading.Event()
    thread = threading.Thread(
        target=run_forever,
        args=(runner, 0.01, stop_event),
        kwargs={"wake_listener": listener, "wake_fallback_seconds": 60},
    )
    thread.start()
    time.sleep(0.2)
    assert api.claims == 1
    listener.joined = False
    time.sleep(0.3)
    stop_event.set()
    thread.join(timeout=3)
    assert api.claims >= 5


def test_build_wake_listener_respects_the_switch_and_the_api():
    from windows_agent.agent_daemon import build_wake_listener
    from windows_agent.realtime_wake import RealtimeWakeListener
    from windows_agent.runtime_config import AgentRuntimeConfig

    class SessionApi(QueueApi):
        def session(self):
            return {}

    enabled = AgentRuntimeConfig(base_url="https://agent.example/trading-agent", poll_seconds=5, secrets_root=Mock())
    disabled = AgentRuntimeConfig(
        base_url="https://agent.example/trading-agent", poll_seconds=5, secrets_root=Mock(), realtime_wake_enabled=False,
    )
    assert isinstance(build_wake_listener(enabled, SessionApi([])), RealtimeWakeListener)
    assert build_wake_listener(disabled, SessionApi([])) is None
    assert build_wake_listener(enabled, QueueApi([])) is None  # an API without session()


def test_build_wake_listener_falls_back_to_polling_without_websocket_client(monkeypatch):
    import builtins

    from windows_agent.agent_daemon import build_wake_listener
    from windows_agent.runtime_config import AgentRuntimeConfig

    real_import = builtins.__import__

    def without_realtime(name, *args, **kwargs):
        if name.endswith("realtime_wake") or (args and args[2] and "realtime_wake" in args[2]):
            raise ImportError("No module named 'websocket'")
        return real_import(name, *args, **kwargs)

    class SessionApi(QueueApi):
        def session(self):
            return {}

    monkeypatch.setattr(builtins, "__import__", without_realtime)
    config = AgentRuntimeConfig(base_url="https://agent.example/trading-agent", poll_seconds=5, secrets_root=Mock())
    assert build_wake_listener(config, SessionApi([])) is None


class LeaseLostApi(CountingApi):
    """The first job's running transition is refused: run_once() returns False with a job found."""

    def transition(self, job_id, lease_id, status, result=None):
        self.transitions.append((status, result))
        if job_id == "lost":
            return {"api_version": "1", "error_code": "lease_lost"}
        return {"status": "failed" if status == "fail" else status}


def test_a_lost_lease_with_a_joined_channel_claims_again_on_the_next_tick(tmp_path):
    api = LeaseLostApi([
        {"job_id": "lost", "job_type": "provision", "connection_id": "c1", "lease_id": "l1"},
        {"job_id": "next", "job_type": "provision", "connection_id": "c2", "lease_id": "l2"},
    ])
    runner = JobRunner(tmp_path / "state.json", api, default_handlers())
    _run_for(runner, 0.3, wake_listener=FakeListener(joined=True), wake_fallback_seconds=60)
    # The queued job behind the lost one is not left waiting for the one-minute safety claim.
    assert api.claims >= 3
    assert ("running", None) in api.transitions[1:]


class FlakyApi(CountingApi):
    def claim(self):
        self.claims += 1
        if self.claims == 2:
            raise RuntimeError("transport error after retries")
        return super(CountingApi, self).claim()


def test_a_failed_claim_with_a_joined_channel_is_retried_on_the_next_tick(tmp_path):
    api = FlakyApi()
    runner = JobRunner(tmp_path / "state.json", api, default_handlers())
    listener = FakeListener(joined=True)
    stop_event = threading.Event()
    thread = threading.Thread(
        target=run_forever,
        args=(runner, 0.01, stop_event),
        kwargs={"wake_listener": listener, "wake_fallback_seconds": 60},
    )
    thread.start()
    assert listener.ready.wait(timeout=1)
    time.sleep(0.05)
    listener.wake_event.set()  # second claim raises
    time.sleep(0.3)
    stop_event.set()
    thread.join(timeout=3)
    assert api.claims == 3  # start-up, the failed wake-up claim, its retry on the next tick


def test_job_runner_tells_an_empty_queue_from_a_lost_job(tmp_path):
    api = LeaseLostApi([{"job_id": "lost", "job_type": "provision", "connection_id": "c1", "lease_id": "l1"}])
    runner = JobRunner(tmp_path / "state.json", api, default_handlers())
    assert runner.run_once() is False and runner.last_claim_found_job is True
    assert runner.run_once() is False and runner.last_claim_found_job is False
