from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.account_worker import AccountWorker, WorkerState
from tools.credential_provider import FakeCredentialProvider
from tools.worker_supervisor import FakeClock, FakeLauncherHandle, Supervisor, SupervisorError


def _new_worker(base_dir: Path, account_id: str = "acc-1", *, credential_provider=None) -> AccountWorker:
    return AccountWorker.create(
        base_dir, account_id,
        credential_provider=credential_provider or FakeCredentialProvider(),
        registry_path=base_dir / "unused-registry.json",
        broker_label="FPMTrading",
    )


class SupervisorLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_start_keeps_worker_alive_and_healthy(self):
        worker = _new_worker(self.base)
        handle = FakeLauncherHandle()
        supervisor = Supervisor(worker, lambda: handle)
        supervisor.start()
        self.assertEqual(worker.state_file.state, WorkerState.HEALTHY)
        self.assertEqual(supervisor.check_liveness(), WorkerState.HEALTHY)
        self.assertEqual(worker.state_file.state, WorkerState.HEALTHY)

    def test_heartbeat_advances_through_liveness_checks(self):
        worker = _new_worker(self.base)
        supervisor = Supervisor(worker, lambda: FakeLauncherHandle())
        supervisor.start()
        first_heartbeat = worker.state_file.last_heartbeat_unix_ms
        supervisor.check_liveness()
        self.assertGreaterEqual(worker.state_file.last_heartbeat_unix_ms, first_heartbeat)

    def test_crash_triggers_bounded_restart(self):
        worker = _new_worker(self.base)
        handles = [FakeLauncherHandle(), FakeLauncherHandle()]
        supervisor = Supervisor(worker, lambda: handles.pop(0), max_restarts=3)
        supervisor.start()
        live_handle = supervisor._handle
        live_handle.kill_externally()

        state = supervisor.check_liveness()

        self.assertEqual(state, WorkerState.HEALTHY)
        self.assertEqual(worker.state_file.restart_count, 1)

    def test_restart_cap_reaches_failed_closed_and_never_restarts_again(self):
        worker = _new_worker(self.base)
        handles = [FakeLauncherHandle() for _ in range(10)]
        supervisor = Supervisor(worker, lambda: handles.pop(0), max_restarts=2)
        supervisor.start()

        for _ in range(2):
            supervisor._handle.kill_externally()
            supervisor.check_liveness()
        self.assertEqual(worker.state_file.state, WorkerState.HEALTHY)
        self.assertEqual(worker.state_file.restart_count, 2)

        supervisor._handle.kill_externally()
        state = supervisor.check_liveness()
        self.assertEqual(state, WorkerState.FAILED_CLOSED)

        remaining_before = len(handles)
        supervisor.check_liveness()
        self.assertEqual(len(handles), remaining_before, "no further handle should ever be requested")
        with self.assertRaises(SupervisorError):
            supervisor.start()
        self.assertEqual(len(handles), remaining_before, "start() must not touch the launcher factory once FAILED_CLOSED")

    def test_timeout_waiting_for_healthy_fails_closed(self):
        worker = _new_worker(self.base)
        handle = FakeLauncherHandle(becomes_alive=False)
        supervisor = Supervisor(worker, lambda: handle)
        clock = FakeClock()

        with self.assertRaises(SupervisorError):
            supervisor.start(timeout_seconds=1.0, clock=clock, sleep=clock.advance, poll_interval_seconds=0.25)

        self.assertEqual(worker.state_file.state, WorkerState.FAILED_CLOSED)

    def test_ordered_stop_calls_terminate_before_declaring_stopped(self):
        worker = _new_worker(self.base)
        handle = FakeLauncherHandle()
        supervisor = Supervisor(worker, lambda: handle)
        supervisor.start()

        supervisor.stop()

        self.assertEqual(handle.terminate_calls, 1)
        self.assertEqual(worker.state_file.state, WorkerState.STOPPED)

    def test_stop_escalates_if_handle_still_alive_after_terminate(self):
        worker = _new_worker(self.base)
        handle = FakeLauncherHandle(refuses_to_die=True)
        supervisor = Supervisor(worker, lambda: handle)
        supervisor.start()

        with self.assertRaises(SupervisorError):
            supervisor.stop()

        self.assertEqual(handle.terminate_calls, 1)
        self.assertNotEqual(worker.state_file.state, WorkerState.STOPPED)

    def test_cleanup_removes_ephemeral_session_files_but_keeps_worker_state_history(self):
        worker = _new_worker(self.base)
        supervisor = Supervisor(worker, lambda: FakeLauncherHandle())
        supervisor.start()
        (worker.session_dir / "session.secret").write_text("ephemeral", encoding="utf-8")
        (worker.session_dir / "session.sequence").write_text("0", encoding="utf-8")
        supervisor.stop()

        supervisor.cleanup()

        self.assertEqual(list(worker.session_dir.iterdir()), [])
        self.assertTrue(worker.directory.worker_state_path.exists())

    def test_two_workers_never_share_directory_or_launcher_handle(self):
        credentials = FakeCredentialProvider()
        worker_a = _new_worker(self.base, "acc-a", credential_provider=credentials)
        worker_b = _new_worker(self.base, "acc-b", credential_provider=credentials)
        handle_a = FakeLauncherHandle()
        handle_b = FakeLauncherHandle()
        supervisor_a = Supervisor(worker_a, lambda: handle_a)
        supervisor_b = Supervisor(worker_b, lambda: handle_b)
        supervisor_a.start()
        supervisor_b.start()

        handle_a.kill_externally()
        supervisor_a.check_liveness()

        self.assertNotEqual(worker_a.directory.root, worker_b.directory.root)
        self.assertIsNot(supervisor_a._handle, supervisor_b._handle)
        self.assertEqual(worker_b.state_file.state, WorkerState.HEALTHY)
        self.assertEqual(worker_b.state_file.restart_count, 0)

    def test_failed_closed_is_absorbing_and_never_touches_launcher_again(self):
        worker = _new_worker(self.base)
        handles = [FakeLauncherHandle()]
        supervisor = Supervisor(worker, lambda: handles.pop(0), max_restarts=0)
        supervisor.start()
        supervisor._handle.kill_externally()
        supervisor.check_liveness()
        self.assertEqual(worker.state_file.state, WorkerState.FAILED_CLOSED)

        self.assertEqual(supervisor.check_liveness(), WorkerState.FAILED_CLOSED)
        with self.assertRaises(SupervisorError):
            supervisor.start()
        self.assertEqual(handles, [])

    def test_no_secret_substring_anywhere_in_log_across_full_cycle(self):
        credentials = FakeCredentialProvider()
        secret_value = "hunter2-super-secret-password"
        credentials.register("acc-1", login="900123", password=secret_value)
        worker = _new_worker(self.base, credential_provider=credentials)
        # Touch the credential the way a real caller would, to prove the act
        # of holding/using it never leaks into the supervisor's own output.
        _ = worker.credential_provider.get("acc-1")

        handles = [FakeLauncherHandle(), FakeLauncherHandle()]
        supervisor = Supervisor(worker, lambda: handles.pop(0), max_restarts=1)
        supervisor.start()
        supervisor._handle.kill_externally()
        supervisor.check_liveness()
        supervisor.stop()

        rendered_log = "\n".join(supervisor.log)
        state_file_text = worker.directory.worker_state_path.read_text(encoding="utf-8")
        self.assertNotIn(secret_value, rendered_log)
        self.assertNotIn(secret_value, state_file_text)
        self.assertNotIn("900123", rendered_log)
        self.assertNotIn("900123", state_file_text)


if __name__ == "__main__":
    unittest.main()
