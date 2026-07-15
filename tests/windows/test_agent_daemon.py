from __future__ import annotations

import threading
import time

from windows_agent.agent_daemon import default_handlers, run_forever
from windows_agent.job_runner import JobRunner


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
        return {}


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
