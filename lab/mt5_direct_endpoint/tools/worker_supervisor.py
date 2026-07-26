"""Bounded-restart process supervision for one ``AccountWorker``.

The supervisor never opens a Job Object or calls a process API itself -- it
only calls a ``LauncherHandle``. For real Windows runs that handle drives the
existing Coordinator (``c012-host``/``c012-client``) over subprocess calls;
this module never reimplements Job Object or process-supervision logic.

Liveness is checked only when the caller calls ``check_liveness()`` -- there
is no background thread and no loop inside this module, so "no infinite
loop" is structural: nothing here can iterate on its own.
"""
from __future__ import annotations

import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from .account_onboarding import FailureReason, OnboardingTrigger
from .account_worker import AccountWorker, WorkerState


DEFAULT_MAX_RESTARTS = 3
DEFAULT_START_TIMEOUT_SECONDS = 10.0
DEFAULT_POLL_INTERVAL_SECONDS = 0.05


class SupervisorError(RuntimeError):
    """Raised for a refused or failed supervisor operation."""


class LauncherHandle(Protocol):
    """One process instance behind a worker's C012 session.

    A fresh handle is created (via the supervisor's launcher factory) for
    every start and every restart -- a handle never outlives one process
    attempt.
    """

    def start(self) -> None: ...

    def is_alive(self) -> bool: ...

    def terminate(self) -> None: ...


@dataclass
class FakeClock:
    """Deterministic, test-only clock. ``advance`` doubles as a fake ``sleep``."""

    now: float = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@dataclass
class FakeLauncherHandle:
    """In-process fake launcher handle for cross-platform supervisor tests.

    Never touches the OS process table or a Job Object -- it only tracks a
    boolean "alive" flag a test can flip to simulate a crash.
    """

    fail_to_start: bool = False
    becomes_alive: bool = True
    refuses_to_die: bool = False
    alive: bool = field(default=False, init=False)
    start_calls: int = field(default=0, init=False)
    terminate_calls: int = field(default=0, init=False)

    def start(self) -> None:
        self.start_calls += 1
        if self.fail_to_start:
            raise SupervisorError("fake launcher failed to start")
        self.alive = self.becomes_alive

    def is_alive(self) -> bool:
        return self.alive

    def terminate(self) -> None:
        self.terminate_calls += 1
        if not self.refuses_to_die:
            self.alive = False

    def kill_externally(self) -> None:
        """Test helper: simulate a crash the supervisor did not cause."""
        self.alive = False


def _wait_until_alive(
    handle: LauncherHandle,
    timeout_seconds: float,
    *,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    poll_interval_seconds: float,
) -> bool:
    deadline = clock() + timeout_seconds
    while True:
        if handle.is_alive():
            return True
        if clock() >= deadline:
            return False
        sleep(poll_interval_seconds)


class Supervisor:
    """Starts, monitors, restarts (bounded), and stops one worker's process."""

    def __init__(
        self,
        worker: AccountWorker,
        launcher_factory: Callable[[], LauncherHandle],
        *,
        max_restarts: int = DEFAULT_MAX_RESTARTS,
    ) -> None:
        self.worker = worker
        self._launcher_factory = launcher_factory
        self.max_restarts = max_restarts
        self._handle: LauncherHandle | None = None
        self.log: list[str] = []

    def _record(self, message: str) -> None:
        self.log.append(f"worker={self.worker.state_file.worker_id} {message}")

    def _refuse_if_failed_closed(self) -> None:
        if self.worker.state_file.state is WorkerState.FAILED_CLOSED:
            raise SupervisorError("worker is FAILED_CLOSED; construct a new worker instead of restarting")

    def _fail_closed(self, reason: FailureReason) -> None:
        self.worker.apply_onboarding_trigger(OnboardingTrigger[reason.name])
        self.worker.set_state(WorkerState.FAILED_CLOSED)
        self._record(f"FAILED_CLOSED reason={reason.name}")

    def start(
        self,
        *,
        timeout_seconds: float = DEFAULT_START_TIMEOUT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    ) -> None:
        self._refuse_if_failed_closed()
        self.worker.set_state(WorkerState.STARTING)
        self._handle = self._launcher_factory()
        self._record("starting")
        try:
            self._handle.start()
        except Exception:
            self._fail_closed(FailureReason.PROCESS_DIED)
            raise

        if not _wait_until_alive(self._handle, timeout_seconds, clock=clock, sleep=sleep, poll_interval_seconds=poll_interval_seconds):
            self._fail_closed(FailureReason.TIMEOUT)
            raise SupervisorError("worker did not become healthy before the start timeout")

        self.worker.set_state(WorkerState.HEALTHY)
        self.worker.heartbeat()
        self._record("healthy")

    def check_liveness(self) -> WorkerState:
        if self.worker.state_file.state is WorkerState.FAILED_CLOSED:
            return WorkerState.FAILED_CLOSED

        if self._handle is not None and self._handle.is_alive():
            self.worker.set_state(WorkerState.HEALTHY)
            self.worker.heartbeat()
            return WorkerState.HEALTHY

        self._on_crash_detected()
        return self.worker.state_file.state

    def _on_crash_detected(self) -> None:
        self._record("crash detected")
        if self.worker.state_file.restart_count >= self.max_restarts:
            self._fail_closed(FailureReason.PROCESS_DIED)
            return

        self.worker.state_file.restart_count += 1
        self.worker.set_state(WorkerState.RESTARTING)
        self._record(f"restarting attempt={self.worker.state_file.restart_count}")
        self._handle = self._launcher_factory()
        try:
            self._handle.start()
        except Exception:
            self._fail_closed(FailureReason.PROCESS_DIED)
            return

        if not self._handle.is_alive():
            self._fail_closed(FailureReason.PROCESS_DIED)
            return

        self.worker.set_state(WorkerState.HEALTHY)
        self.worker.heartbeat()
        self._record("healthy after restart")

    def stop(self) -> None:
        self._record("stopping")
        if self._handle is not None:
            self._handle.terminate()
            if self._handle.is_alive():
                raise SupervisorError("launcher handle still reports alive after terminate; refusing to declare stopped")
        self.worker.apply_onboarding_trigger(OnboardingTrigger.STOP)
        self.worker.set_state(WorkerState.STOPPED)
        self._record("stopped")

    def cleanup(self) -> None:
        """Remove only the ephemeral session files this worker's run created.

        The persisted ``worker.json`` history (under ``directory.state``) is
        never touched here -- only ``directory.session`` is cleared.
        """
        session_dir = self.worker.session_dir
        for child in sorted(session_dir.iterdir()):
            if child.is_dir():
                for grandchild in sorted(child.rglob("*"), reverse=True):
                    if grandchild.is_file():
                        grandchild.unlink()
                    else:
                        grandchild.rmdir()
                child.rmdir()
            else:
                child.unlink()
        self._record("cleanup complete")


class JobHarnessCliLauncherHandle:
    """Windows-only real launcher handle, used only by the Windows smoke test.

    Drives the actual, already-built JobHarness executable/DLL over subprocess
    calls: `c012-host start-innocuous --target self-sleeper` (the narrow,
    explicitly authorized exception -- see C012HostCli.RunInnocuous and
    lab/mt5_direct_endpoint/AGENTS.md) to get a real Job-Object-contained
    process, then `c012-client c0-start` to drive it to C0Retained. Never
    touches the OS process table or a Job Object directly -- all of that
    lives in the existing, already-verified Coordinator/launcher C# code.
    No cross-platform test ever constructs this class.
    """

    _START_TIMEOUT_SECONDS = 20.0
    _POLL_INTERVAL_SECONDS = 0.2
    _CLIENT_CALL_TIMEOUT_SECONDS = 10.0

    def __init__(self, jobharness_path: Path, session_dir: Path, *, dotnet_executable: str = "dotnet") -> None:
        self._jobharness_path = jobharness_path
        self._session_root = session_dir
        self._dotnet_executable = dotnet_executable
        self._host_process: subprocess.Popen | None = None
        self._session_dir: Path | None = None

    @property
    def host_pid(self) -> int | None:
        """PID of the `start-innocuous` host process itself (not the root process it
        contains) -- known to Python directly since it started the process, no new CLI
        surface needed. Used only by the Windows smoke test to simulate an external kill
        and to verify the process is really gone after stop()."""
        return self._host_process.pid if self._host_process is not None else None

    def _command(self, *args: str) -> list[str]:
        if self._jobharness_path.suffix.lower() == ".dll":
            return [self._dotnet_executable, str(self._jobharness_path), *args]
        return [str(self._jobharness_path), *args]

    def _run_client(self, verb: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            self._command("c012-client", verb, "--session-dir", str(self._session_dir)),
            capture_output=True,
            text=True,
            timeout=self._CLIENT_CALL_TIMEOUT_SECONDS,
        )

    def start(self) -> None:
        # A fresh, uniquely-named subdirectory per attempt: C012HostCli refuses to reuse a
        # --session-dir that already holds a prior session's files (session.id in particular
        # is a permanent record that is never deleted), so restarting with the same directory
        # a second time would make the new host fail closed at startup rather than restart.
        # Each JobHarnessCliLauncherHandle instance is itself fresh per restart (the
        # Supervisor's launcher_factory is called anew each time), so this only ever creates
        # one subdirectory per instance.
        self._session_dir = self._session_root / f"attempt-{uuid.uuid4().hex}"
        self._session_dir.mkdir(parents=True)
        self._host_process = subprocess.Popen(
            self._command(
                "c012-host", "start-innocuous",
                "--session-dir", str(self._session_dir), "--target", "self-sleeper",
            ),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        deadline = time.monotonic() + self._START_TIMEOUT_SECONDS
        last_error = ""
        while time.monotonic() < deadline:
            if self._host_process.poll() is not None:
                stderr = self._host_process.stderr.read().decode("utf-8", "replace") if self._host_process.stderr else ""
                raise SupervisorError(f"start-innocuous host exited before accepting a connection: {stderr}")

            result = self._run_client("c0-start")
            if result.returncode == 0 and "resulting_state=C0Retained" in result.stdout:
                return
            last_error = result.stderr or result.stdout
            time.sleep(self._POLL_INTERVAL_SECONDS)

        raise SupervisorError(f"start-innocuous never reached C0Retained: {last_error}")

    def is_alive(self) -> bool:
        return self._host_process is not None and self._host_process.poll() is None

    def terminate(self) -> None:
        if self._host_process is None or self._host_process.poll() is not None:
            return
        # Killing the host process (rather than sending c2-submit) is deliberate: it proves
        # KILL_ON_JOB_CLOSE tears down the real root process even on an ungraceful stop,
        # exactly like RealHostProcessCrashKillsRootViaKillOnJobClose already proves in the
        # C# test suite -- this is the same guarantee, exercised from the Python side.
        self._host_process.terminate()
        try:
            self._host_process.wait(timeout=self._CLIENT_CALL_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            self._host_process.kill()
            self._host_process.wait(timeout=self._CLIENT_CALL_TIMEOUT_SECONDS)
