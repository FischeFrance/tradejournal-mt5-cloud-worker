"""Windows-only smoke test: a real per-account worker driven through the actual JobHarness
CLI (`start-innocuous` -- the narrow, explicitly authorized exception; see
C012HostCli.RunInnocuous and lab/mt5_direct_endpoint/AGENTS.md), with a real
Job-Object-contained process, simulated crash, bounded restart, ordered stop, and cleanup
verification. Never touches MT5 -- the launcher's own allowlist only ever selects this very
process re-invoking itself harmlessly.

Skipped entirely (not just a no-op) on any platform other than Windows. Also skipped cleanly
-- not failed -- if the JobHarness build output is not present yet, since this Python suite
can run before the .NET build steps in CI.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

LAB_ROOT = Path(__file__).resolve().parents[1]
CLI = LAB_ROOT / "tools" / "labctl.py"

sys.path.insert(0, str(LAB_ROOT))

from tools.account_worker import AccountWorker, WorkerState
from tools.credential_provider import FakeCredentialProvider
from tools.worker_supervisor import JobHarnessCliLauncherHandle, Supervisor


_BLOCKED_PROCESS_NAMES = ("terminal.exe", "terminal64.exe", "metaeditor.exe", "metaeditor64.exe")


def _find_jobharness_dll() -> Path | None:
    lab_root = Path(__file__).resolve().parents[1]
    candidates = sorted((lab_root / "src" / "JobHarness" / "bin").glob("*/net8.0/JobHarness.dll"))
    return candidates[0] if candidates else None


def _wait_until(condition, *, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.2)
    return condition()


def _process_id_exists(pid: int) -> bool:
    result = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True, timeout=10, check=False,
    )
    return str(pid) in result.stdout


@unittest.skipUnless(sys.platform == "win32", "real Job Object smoke test is Windows-only")
class WorkerSupervisorWindowsSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        jobharness_dll = _find_jobharness_dll()
        if jobharness_dll is None:
            self.skipTest("JobHarness has not been built yet (src/JobHarness/bin/*/net8.0/JobHarness.dll not found)")
        self.jobharness_dll = jobharness_dll

        self.tmp = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.tmp.name)
        self.worker = AccountWorker.create(
            self.base_dir, "acc-smoke",
            credential_provider=FakeCredentialProvider(),
            registry_path=self.base_dir / "unused-registry.json",
            broker_label="FPMTrading",
        )
        self.supervisor = Supervisor(
            self.worker,
            lambda: JobHarnessCliLauncherHandle(self.jobharness_dll, self.worker.session_dir),
            max_restarts=2,
        )

    def tearDown(self) -> None:
        try:
            handle = self.supervisor._handle
            if handle is not None and handle.is_alive():
                handle.terminate()
        finally:
            self.tmp.cleanup()
        self._assert_no_blocked_processes_running()

    def _assert_no_blocked_processes_running(self) -> None:
        listing = subprocess.run(["tasklist"], capture_output=True, text=True, timeout=10, check=False).stdout.lower()
        for blocked in _BLOCKED_PROCESS_NAMES:
            self.assertNotIn(blocked, listing, f"{blocked} must never be running after a start-innocuous smoke test")

    def test_real_worker_reaches_healthy_with_a_real_job_object_contained_process(self) -> None:
        self.supervisor.start()

        self.assertEqual(self.worker.state_file.state, WorkerState.HEALTHY)
        handle = self.supervisor._handle
        self.assertTrue(handle.is_alive())
        self.assertIsNotNone(handle.host_pid)
        self.assertTrue(_process_id_exists(handle.host_pid))

    def test_simulated_crash_triggers_bounded_restart_with_a_fresh_process(self) -> None:
        self.supervisor.start()
        handle = self.supervisor._handle
        pid_before = handle.host_pid

        # External kill, deliberately bypassing supervisor.stop(): proves check_liveness()
        # detects a crash it did not itself cause, exactly like the fake-launcher tests'
        # kill_externally(), but against a real OS process here.
        subprocess.run(["taskkill", "/PID", str(pid_before), "/T", "/F"], check=False, timeout=10)
        self.assertTrue(_wait_until(lambda: not handle.is_alive(), timeout_seconds=10))

        state = self.supervisor.check_liveness()

        self.assertEqual(state, WorkerState.HEALTHY)
        self.assertEqual(self.worker.state_file.restart_count, 1)
        new_handle = self.supervisor._handle
        self.assertIsNot(new_handle, handle)
        self.assertNotEqual(new_handle.host_pid, pid_before)
        self.assertFalse(_process_id_exists(pid_before))

    def test_ordered_stop_leaves_no_process_and_cleanup_clears_session_dir(self) -> None:
        self.supervisor.start()
        pid = self.supervisor._handle.host_pid

        self.supervisor.stop()

        self.assertEqual(self.worker.state_file.state, WorkerState.STOPPED)
        self.assertFalse(_process_id_exists(pid))

        self.supervisor.cleanup()
        self.assertEqual(list(self.worker.session_dir.iterdir()), [])
        self.assertTrue(self.worker.directory.worker_state_path.exists())


@unittest.skipUnless(sys.platform == "win32", "real Job Object smoke test is Windows-only")
class PersistentWorkerHostWindowsSmokeTests(unittest.TestCase):
    """Same real Job-Object-backed launcher as above, but driven through the actual,
    separately-spawned persistent worker-host process via the real `labctl.py`
    start-worker/worker-status/stop-worker CLI -- proving the full IPC/PID-file/
    control-file stack, not just the in-process Supervisor object."""

    def setUp(self) -> None:
        jobharness_dll = _find_jobharness_dll()
        if jobharness_dll is None:
            self.skipTest("JobHarness has not been built yet (src/JobHarness/bin/*/net8.0/JobHarness.dll not found)")

        self.tmp = tempfile.TemporaryDirectory()
        self.worker_root = Path(self.tmp.name) / "workers"
        self.registry = Path(self.tmp.name) / "registry.json"
        self.env = os.environ.copy()
        self.env["LAB_WORKER_HOST_JOBHARNESS_DLL"] = str(jobharness_dll)
        self._started = False

    def tearDown(self) -> None:
        if self._started:
            self.run_cli("stop-worker", "--account-id", "acc-smoke", "--worker-root", self.worker_root)
        self.tmp.cleanup()
        listing = subprocess.run(["tasklist"], capture_output=True, text=True, timeout=10, check=False).stdout.lower()
        for blocked in _BLOCKED_PROCESS_NAMES:
            self.assertNotIn(blocked, listing, f"{blocked} must never be running after a persistent worker-host smoke test")

    def run_cli(self, *arguments: object) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(CLI), *(str(item) for item in arguments)],
            env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )

    def test_persistent_worker_host_drives_a_real_job_object_process_end_to_end(self) -> None:
        create = self.run_cli(
            "create-account", "--account-id", "acc-smoke", "--worker-root", self.worker_root,
            "--registry", self.registry, "--broker-label", "FPMTrading",
        )
        self.assertEqual(create.returncode, 0, create.stderr)

        start = self.run_cli(
            "start-worker", "--account-id", "acc-smoke", "--worker-root", self.worker_root,
            "--poll-interval-seconds", "1", "--startup-timeout-seconds", "30",
        )
        self.assertEqual(start.returncode, 0, start.stderr)
        self._started = True
        started = json.loads(start.stdout)
        self.assertTrue(started["process_alive"])
        self.assertTrue(_process_id_exists(started["pid"]))
        self.assertIsInstance(started["launcher_pid"], int)
        self.assertTrue(_process_id_exists(started["launcher_pid"]))

        status = self.run_cli("worker-status", "--account-id", "acc-smoke", "--worker-root", self.worker_root)
        self.assertEqual(status.returncode, 0, status.stderr)
        polled = json.loads(status.stdout)
        self.assertEqual(polled["state"], "RUNNING")
        self.assertTrue(polled["launcher_process_alive"])

        stop = self.run_cli("stop-worker", "--account-id", "acc-smoke", "--worker-root", self.worker_root)
        self._started = False
        self.assertEqual(stop.returncode, 0, stop.stderr)
        stopped = json.loads(stop.stdout)
        self.assertEqual(stopped["state"], "STOPPED")
        self.assertFalse(stopped["process_alive"])
        self.assertFalse(_process_id_exists(started["pid"]), "worker-host process must be gone")
        self.assertFalse(_process_id_exists(started["launcher_pid"]), "real Job-Object-contained root process must be gone too")


if __name__ == "__main__":
    unittest.main()
