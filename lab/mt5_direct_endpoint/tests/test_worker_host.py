"""Cross-platform integration tests for the persistent worker-host process, driven entirely
through the real `labctl.py` CLI (real, separate OS processes) -- never MT5, never a real
Job Object (that is Windows-only and covered by test_worker_supervisor_windows_smoke.py).
Uses a short --harmless-sleeper-seconds and a fast --poll-interval-seconds so crash/restart
scenarios are deterministic and fast without any external kill signal.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

LAB_ROOT = Path(__file__).resolve().parents[1]
CLI = LAB_ROOT / "tools" / "labctl.py"

sys.path.insert(0, str(LAB_ROOT))

from tools.worker_host import pid_is_alive  # noqa: E402


class WorkerHostLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.worker_root = Path(self.tmp.name) / "workers"
        self.registry = Path(self.tmp.name) / "registry.json"
        self._started_accounts: list[str] = []

    def tearDown(self) -> None:
        for account_id in self._started_accounts:
            self.run_cli("stop-worker", "--account-id", account_id, "--worker-root", self.worker_root)
        self.tmp.cleanup()

    def run_cli(self, *arguments: object) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(CLI), *(str(item) for item in arguments)],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )

    def create_account(self, account_id: str) -> None:
        result = self.run_cli(
            "create-account", "--account-id", account_id, "--worker-root", self.worker_root,
            "--registry", self.registry, "--broker-label", "FPMTrading",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def start_worker(self, account_id: str, **extra: object) -> dict:
        args = [
            "start-worker", "--account-id", account_id, "--worker-root", self.worker_root,
            "--poll-interval-seconds", "0.3",
        ]
        for flag, value in extra.items():
            args += [f"--{flag.replace('_', '-')}", str(value)]
        result = self.run_cli(*args)
        self.assertEqual(result.returncode, 0, result.stderr)
        self._started_accounts.append(account_id)
        return json.loads(result.stdout)

    def status(self, account_id: str) -> dict:
        result = self.run_cli("worker-status", "--account-id", account_id, "--worker-root", self.worker_root)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def stop(self, account_id: str) -> dict:
        result = self.run_cli("stop-worker", "--account-id", account_id, "--worker-root", self.worker_root)
        self.assertEqual(result.returncode, 0, result.stderr)
        if account_id in self._started_accounts:
            self._started_accounts.remove(account_id)
        return json.loads(result.stdout)

    # 1. start mantiene il processo vivo
    def test_start_keeps_a_real_process_alive(self) -> None:
        self.create_account("acc-1")
        started = self.start_worker("acc-1")

        self.assertTrue(started["process_alive"])
        self.assertIsInstance(started["pid"], int)
        self.assertTrue(pid_is_alive(started["pid"]))

        time.sleep(1.0)
        still = self.status("acc-1")
        self.assertTrue(still["process_alive"])
        self.assertEqual(still["pid"], started["pid"])

    # 2. status segnala RUNNING
    def test_status_reports_running(self) -> None:
        self.create_account("acc-1")
        self.start_worker("acc-1")

        status = self.status("acc-1")

        self.assertEqual(status["state"], "RUNNING")
        self.assertTrue(status["process_alive"])
        self.assertTrue(status["launcher_process_alive"])

    # 3. stop termina correttamente
    def test_stop_terminates_the_process_in_an_ordered_way(self) -> None:
        self.create_account("acc-1")
        started = self.start_worker("acc-1")
        host_pid = started["pid"]

        stopped = self.stop("acc-1")

        self.assertEqual(stopped["state"], "STOPPED")
        self.assertFalse(stopped["process_alive"])
        self.assertFalse(pid_is_alive(host_pid))
        self.assertIsNone(stopped["pid"])

    # 4. crash provoca restart
    def test_crash_of_the_harmless_child_triggers_a_restart(self) -> None:
        self.create_account("acc-1")
        self.start_worker("acc-1", harmless_sleeper_seconds=1, max_restarts=5)

        # Generous budget: each poll spawns a fresh `worker-status` interpreter, and a
        # loaded/slower CI runner (this runs on windows-latest) can make process-spawn
        # overhead -- on both the polling and the crash/restart side -- dominate a tight
        # deadline. This is a correctness check, not a timing benchmark: it returns as soon
        # as the condition is observed either way.
        deadline = time.monotonic() + 45.0
        restarted = False
        while time.monotonic() < deadline:
            status = self.status("acc-1")
            if status["restart_count"] >= 1 and status["state"] == "RUNNING":
                restarted = True
                break
            time.sleep(0.3)

        self.assertTrue(restarted, "expected restart_count to increment while staying RUNNING")

    # 5. limite restart porta a FAILED_CLOSED
    def test_restart_cap_reaches_failed_closed_and_is_absorbing(self) -> None:
        self.create_account("acc-1")
        self.start_worker("acc-1", harmless_sleeper_seconds=1, max_restarts=1)

        deadline = time.monotonic() + 60.0
        failed_closed = False
        while time.monotonic() < deadline:
            status = self.status("acc-1")
            if status["state"] == "FAILED_CLOSED":
                failed_closed = True
                break
            time.sleep(0.3)

        self.assertTrue(failed_closed)
        status = self.status("acc-1")
        self.assertFalse(status["process_alive"])
        self.assertIsNone(status["pid"])

        # Absorbing: a further start-worker must refuse, not spawn a second host.
        refused = self.run_cli("start-worker", "--account-id", "acc-1", "--worker-root", self.worker_root)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("FAILED_CLOSED", refused.stderr)
        self.assertIsNone(self.status("acc-1")["pid"])

    # 6. due account restano isolati
    def test_two_accounts_stay_isolated(self) -> None:
        self.create_account("acc-a")
        self.create_account("acc-b")
        started_a = self.start_worker("acc-a")
        started_b = self.start_worker("acc-b")

        self.assertNotEqual(started_a["pid"], started_b["pid"])
        self.assertNotEqual(started_a["directory"], started_b["directory"])

        self.stop("acc-a")

        status_a = self.status("acc-a")
        status_b = self.status("acc-b")
        self.assertEqual(status_a["state"], "STOPPED")
        self.assertFalse(status_a["process_alive"])
        self.assertEqual(status_b["state"], "RUNNING")
        self.assertTrue(status_b["process_alive"])
        self.assertTrue(pid_is_alive(started_b["pid"]))

    # 7. nessun processo residuo
    def test_stop_leaves_no_residual_process(self) -> None:
        self.create_account("acc-1")
        started = self.start_worker("acc-1")
        host_pid = started["pid"]
        launcher_pid = self.status("acc-1")["launcher_pid"]
        self.assertIsInstance(launcher_pid, int)
        self.assertTrue(pid_is_alive(launcher_pid))

        self.stop("acc-1")

        self.assertFalse(pid_is_alive(host_pid), "worker-host process must be gone after stop")
        self.assertFalse(pid_is_alive(launcher_pid), "harmless child process must be gone after stop, no orphan")

        session_dir = Path(self.status("acc-1")["directory"]) / "session"
        self.assertEqual(list(session_dir.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
