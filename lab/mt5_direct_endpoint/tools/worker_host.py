"""Long-lived, persistent worker-host process for one account.

``run_worker_host`` is the process's main loop: resume the account, start a
harmless launcher (never MT5, never a real Job Object unless the caller
explicitly injects one -- only the Windows smoke test does that) via
``worker_supervisor.Supervisor``, then tick forever -- heartbeat, crash
detection, bounded restart, ``FAILED_CLOSED`` after the limit -- until a stop
is requested through the on-disk control file written by
``request_stop_and_wait`` (what ``labctl.py stop-worker`` calls), or until
``FAILED_CLOSED`` is reached on its own.

This is deliberately a plain, testable, foreground-capable OS process, not an
installed Windows service: ``start_persistent_worker`` (what ``labctl.py
start-worker`` calls) spawns it detached; ``labctl.py worker-host`` can also
be run directly in the foreground for testing. IPC between the short-lived
``start-worker``/``stop-worker``/``worker-status`` CLI invocations and the
long-lived host reuses this project's existing convention of a directory as
the single source of truth for session-scoped control state (the same
principle ``C012SessionPaths`` already uses for the C012 Named Pipe session,
applied here at the Python/OS-process level): a PID file plus a stop-request
file, both under the worker's own ``session/`` directory, both written
atomically, both cleared by ``Supervisor.cleanup()`` on every exit path.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from .account_worker import AccountWorker, WorkerState
from .credential_provider import CredentialProvider, FakeCredentialProvider
from .worker_supervisor import LauncherHandle, Supervisor, SupervisorError


DEFAULT_POLL_INTERVAL_SECONDS = 2.0
DEFAULT_MAX_RESTARTS = 3
DEFAULT_HARMLESS_SLEEPER_SECONDS = 3600.0
DEFAULT_STARTUP_TIMEOUT_SECONDS = 15.0
DEFAULT_STOP_TIMEOUT_SECONDS = 20.0

EXIT_STOPPED = 0
EXIT_FAILED_CLOSED = 1
EXIT_ALREADY_RUNNING = 3
EXIT_STARTUP_FAILURE = 4


class SubprocessSleeperLauncherHandle:
    """Cross-platform, harmless, real child OS process.

    No MT5, no MetaEditor/metatester, no Job Object -- a plain Python
    ``time.sleep`` in a separate process. This is the default launcher for
    the persistent worker-host. A bounded ``lifetime_seconds`` lets tests
    deterministically simulate a crash (the child simply exits on its own)
    without needing OS-level kill signals or platform-specific process
    control; production usage leaves it at its large default.
    """

    def __init__(self, *, lifetime_seconds: float = DEFAULT_HARMLESS_SLEEPER_SECONDS) -> None:
        self._lifetime_seconds = lifetime_seconds
        self._process: subprocess.Popen | None = None

    def start(self) -> None:
        self._process = subprocess.Popen(
            [sys.executable, "-c", f"import time; time.sleep({self._lifetime_seconds!r})"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def is_alive(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def terminate(self) -> None:
        if self._process is None or self._process.poll() is not None:
            return
        self._process.terminate()
        try:
            self._process.wait(timeout=10.0)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait(timeout=10.0)

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process is not None else None


def _pid_path(worker: AccountWorker) -> Path:
    return worker.session_dir / "worker_host.pid"


def _control_path(worker: AccountWorker) -> Path:
    return worker.session_dir / "control.stop"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _write_pid_file(path: Path, pid: int) -> None:
    _atomic_write(path, json.dumps({"pid": pid, "started_at_unix_ms": int(time.time() * 1000)}))


def _read_pid(path: Path) -> int | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    pid = payload.get("pid")
    return pid if isinstance(pid, int) and pid > 0 else None


def _try_unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def pid_is_alive(pid: int | None) -> bool:
    """Best-effort, stdlib-only OS process liveness check (POSIX and Windows)."""
    if pid is None or pid <= 0:
        return False
    if sys.platform == "win32":
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True, timeout=10, check=False,
        )
        return str(pid) in result.stdout
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _force_kill(pid: int) -> None:
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], check=False, timeout=10)
        return
    import signal

    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def read_persistent_status(worker: AccountWorker) -> dict[str, Any]:
    """The worker's real, currently-observed status: persisted FSM state plus a live,
    OS-level check of whether its worker-host process is actually running -- never just
    trusting the last state a possibly-long-gone process happened to write."""
    pid = _read_pid(_pid_path(worker))
    return {
        "account_id": worker.state_file.account_id,
        "worker_id": worker.state_file.worker_id,
        "state": worker.state_file.state.name,
        "onboarding_state": worker.onboarding.current_state.name,
        "restart_count": worker.state_file.restart_count,
        "endpoint": worker.state_file.endpoint,
        "directory": str(worker.directory.root),
        "pid": pid,
        "process_alive": pid_is_alive(pid),
        "launcher_pid": worker.state_file.launcher_pid,
        "launcher_process_alive": pid_is_alive(worker.state_file.launcher_pid),
    }


def _launcher_pid_of(handle: LauncherHandle | None) -> int | None:
    if handle is None:
        return None
    # Different LauncherHandle implementations expose their child PID under different
    # names (SubprocessSleeperLauncherHandle.pid, JobHarnessCliLauncherHandle.host_pid) --
    # duck-typed here rather than widening the shared Protocol for a single extra property.
    return getattr(handle, "pid", None) or getattr(handle, "host_pid", None)


def _sync_process_metadata(worker: AccountWorker, supervisor: Supervisor) -> None:
    # Supervisor's own contract (tested independently in test_worker_supervisor.py) uses
    # HEALTHY; the persistent host translates that to RUNNING for anything CLI-visible,
    # since "a live, ticking, externally-observable worker-host" is specifically what
    # RUNNING means at this layer -- HEALTHY stays Supervisor's internal vocabulary.
    if worker.state_file.state is WorkerState.HEALTHY:
        worker.set_state(WorkerState.RUNNING)
    worker.set_launcher_pid(_launcher_pid_of(supervisor._handle))


def _default_launcher_factory(worker: AccountWorker, *, harmless_sleeper_seconds: float) -> Callable[[], LauncherHandle]:
    # LAB_WORKER_HOST_JOBHARNESS_DLL is a test-only hook, read only here -- never exposed as
    # a public `worker-host`/`start-worker` CLI flag, so `--help` and every normal caller
    # stay on the harmless, cross-platform default. Only the Windows smoke test sets it, to
    # point a real, separately-spawned worker-host process at the real, Job-Object-backed
    # launcher (the narrow, explicitly authorized `start-innocuous` exception) instead of
    # the harmless subprocess sleeper.
    dll = os.environ.get("LAB_WORKER_HOST_JOBHARNESS_DLL")
    if not dll:
        return lambda: SubprocessSleeperLauncherHandle(lifetime_seconds=harmless_sleeper_seconds)

    from .worker_supervisor import JobHarnessCliLauncherHandle

    dll_path = Path(dll)
    session_dir = worker.session_dir
    return lambda: JobHarnessCliLauncherHandle(dll_path, session_dir)


def _install_termination_signal_flag() -> Callable[[], bool]:
    """Best-effort extra stop trigger: a direct SIGTERM/SIGINT (an operator or CI cancelling
    this process directly, bypassing stop-worker's control file) still reaches the same
    ordered supervisor.stop()/cleanup() path instead of leaving an orphaned launcher
    process. POSIX-only -- Windows' primary, well-tested stop path is the control-file +
    escalation-to-taskkill mechanism stop-worker already uses; this is additive robustness
    for POSIX, not a requirement, and is skipped on Windows rather than risking a fragile
    signal handler there.
    """
    if sys.platform == "win32":
        return lambda: False

    import signal

    flag = {"signalled": False}

    def _handler(signum: int, frame: object) -> None:
        flag["signalled"] = True

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):
            pass  # not the main thread, or unsupported here -- best effort only

    return lambda: flag["signalled"]


def run_worker_host(
    base_dir: str | Path,
    account_id: str,
    *,
    credential_provider: CredentialProvider | None = None,
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    max_restarts: int = DEFAULT_MAX_RESTARTS,
    harmless_sleeper_seconds: float = DEFAULT_HARMLESS_SLEEPER_SECONDS,
    launcher_factory: Callable[[], LauncherHandle] | None = None,
) -> int:
    """The persistent worker-host process's entire lifetime. Blocks until stopped or
    FAILED_CLOSED. Intended to run as its own OS process (see ``start_persistent_worker``),
    but is a plain function so tests and the Windows smoke test can also drive it directly.

    ``launcher_factory`` is an escape hatch for direct callers (tests) that want full
    control; when omitted, the default is the harmless cross-platform sleeper, unless
    ``LAB_WORKER_HOST_JOBHARNESS_DLL`` is set (test-only, see ``_default_launcher_factory``).
    """
    worker = AccountWorker.resume(base_dir, account_id, credential_provider=credential_provider or FakeCredentialProvider())
    if launcher_factory is None:
        launcher_factory = _default_launcher_factory(worker, harmless_sleeper_seconds=harmless_sleeper_seconds)

    pid_path = _pid_path(worker)
    existing_pid = _read_pid(pid_path)
    if existing_pid is not None and pid_is_alive(existing_pid):
        sys.stderr.write(f"ERROR: a worker-host for {account_id!r} is already running (pid {existing_pid})\n")
        return EXIT_ALREADY_RUNNING

    control_path = _control_path(worker)
    _try_unlink(control_path)
    _write_pid_file(pid_path, os.getpid())

    factory = launcher_factory or (lambda: SubprocessSleeperLauncherHandle())
    supervisor = Supervisor(worker, factory, max_restarts=max_restarts)
    signalled = _install_termination_signal_flag()

    try:
        try:
            supervisor.start()
        except SupervisorError as exc:
            sys.stderr.write(f"ERROR: worker-host failed to start: {exc}\n")
            return EXIT_STARTUP_FAILURE

        _sync_process_metadata(worker, supervisor)

        while True:
            if control_path.exists() or signalled():
                supervisor.stop()
                return EXIT_STOPPED

            state = supervisor.check_liveness()
            if state is WorkerState.FAILED_CLOSED:
                return EXIT_FAILED_CLOSED
            _sync_process_metadata(worker, supervisor)

            time.sleep(poll_interval_seconds)
    finally:
        try:
            supervisor.cleanup()
        except Exception:
            pass
        worker.set_launcher_pid(None)
        _try_unlink(pid_path)
        _try_unlink(control_path)


def _detach_kwargs() -> dict[str, Any]:
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def start_persistent_worker(
    worker: AccountWorker,
    *,
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    max_restarts: int = DEFAULT_MAX_RESTARTS,
    harmless_sleeper_seconds: float = DEFAULT_HARMLESS_SLEEPER_SECONDS,
    startup_timeout_seconds: float = DEFAULT_STARTUP_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Spawn ``labctl.py worker-host`` detached and wait (bounded) for it to report alive.

    Idempotent: if a worker-host for this account is already running, this
    does not spawn a second one (one worker-host per account, ever).
    """
    if worker.state_file.state is WorkerState.FAILED_CLOSED:
        raise SupervisorError("worker is FAILED_CLOSED; create a new account instead of starting it")

    base_dir = worker.directory.root.parent
    pid_path = _pid_path(worker)
    existing_pid = _read_pid(pid_path)
    if existing_pid is not None and pid_is_alive(existing_pid):
        return read_persistent_status(worker)

    log_path = worker.directory.logs / "worker-host.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    labctl_path = Path(__file__).resolve().parent / "labctl.py"
    command = [
        sys.executable, str(labctl_path), "worker-host",
        "--account-id", worker.state_file.account_id,
        "--worker-root", str(base_dir),
        "--poll-interval-seconds", str(poll_interval_seconds),
        "--max-restarts", str(max_restarts),
        "--harmless-sleeper-seconds", str(harmless_sleeper_seconds),
    ]

    with open(log_path, "ab") as log_handle:
        subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=log_handle,
            **_detach_kwargs(),
        )

    deadline = time.monotonic() + startup_timeout_seconds
    while time.monotonic() < deadline:
        pid = _read_pid(pid_path)
        if pid is not None and pid_is_alive(pid):
            break
        time.sleep(0.2)

    fresh = AccountWorker.resume(base_dir, worker.state_file.account_id, credential_provider=worker.credential_provider)
    return read_persistent_status(fresh)


def request_stop_and_wait(
    worker: AccountWorker,
    *,
    timeout_seconds: float = DEFAULT_STOP_TIMEOUT_SECONDS,
    poll_interval_seconds: float = 0.25,
) -> dict[str, Any]:
    """Ask a running worker-host to stop, wait for it, escalate to a forced kill if it
    does not exit within ``timeout_seconds``. Idempotent: a no-op if nothing is running."""
    pid_path = _pid_path(worker)
    pid = _read_pid(pid_path)
    if pid is None or not pid_is_alive(pid):
        return read_persistent_status(worker)

    _atomic_write(_control_path(worker), json.dumps({"request": "STOP", "requested_at_unix_ms": int(time.time() * 1000)}))

    deadline = time.monotonic() + timeout_seconds
    graceful = False
    while time.monotonic() < deadline:
        if not pid_is_alive(pid):
            graceful = True
            break
        time.sleep(poll_interval_seconds)

    fresh = AccountWorker.resume(
        worker.directory.root.parent, worker.state_file.account_id, credential_provider=worker.credential_provider,
    )
    if not graceful:
        # The host did not exit on its own within the grace period: escalate to a forced
        # kill. It gets no chance to run its own finally block, so this caller must finish
        # the job it would have done -- mark STOPPED and clear the ephemeral session files
        # itself, exactly mirroring Supervisor.stop()+cleanup()'s ordered-stop discipline.
        _force_kill(pid)
        fresh.set_state(WorkerState.STOPPED)
        fresh.set_launcher_pid(None)
        _try_unlink(_pid_path(fresh))
        _try_unlink(_control_path(fresh))

    return read_persistent_status(fresh)
