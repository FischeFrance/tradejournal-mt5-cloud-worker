from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from ..interactive_identity import verify_interactive_process_identity


class ProcessManager:
    STATE_SCHEMA_VERSION = 2

    def __init__(self, state_path: Path) -> None:
        self.state_path = state_path

    @staticmethod
    def _identity(pid: int, executable: Path) -> tuple[int, str, int]:
        import psutil

        expected = executable.resolve()
        process = psutil.Process(pid)
        observed = Path(process.exe()).resolve()
        created_at_unix_ms = int(process.create_time() * 1000)
        if observed != expected or created_at_unix_ms <= 0:
            raise RuntimeError("terminal process identity mismatch")
        return pid, str(expected), created_at_unix_ms

    def _save_identity(
        self,
        pid: int,
        executable: Path,
        *,
        portable: bool,
    ) -> int:
        from ..state_store import atomic_json

        observed_pid, observed_executable, created_at_unix_ms = self._identity(
            pid,
            executable,
        )
        atomic_json(
            self.state_path,
            {
                "schema_version": self.STATE_SCHEMA_VERSION,
                "pid": observed_pid,
                "executable": observed_executable,
                "creation_time_unix_ms": created_at_unix_ms,
                "portable": portable,
            },
        )
        return observed_pid

    def start(self, executable: Path, portable: bool = True) -> int:
        executable = executable.resolve()
        if not executable.is_file() or executable.name.lower() != "terminal64.exe":
            raise ValueError("invalid terminal executable")
        args = [str(executable)] + (["/portable"] if portable else [])
        process = subprocess.Popen(args, cwd=executable.parent, close_fds=True)
        try:
            return self._save_identity(
                process.pid,
                executable,
                portable=portable,
            )
        except Exception:
            process.terminate()
            process.wait(15)
            raise

    @staticmethod
    def find(executable: Path) -> list[int]:
        import psutil

        expected = executable.resolve()
        matches = []
        for process in psutil.process_iter(("pid", "exe")):
            try:
                if (
                    process.info["exe"]
                    and Path(process.info["exe"]).resolve() == expected
                ):
                    matches.append(int(process.info["pid"]))
            except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
                continue
        return matches

    @staticmethod
    def _is_reparse_point(path: Path) -> bool:
        try:
            stat_result = os.lstat(path)
        except OSError:
            return True
        attributes = getattr(stat_result, "st_file_attributes", 0)
        return path.is_symlink() or bool(attributes & 0x400)

    @classmethod
    def _processes_under(cls, root: Path) -> list[object]:
        import psutil

        if cls._is_reparse_point(root) or not root.is_dir():
            raise ValueError("process cleanup root is unsafe")
        expected = os.path.normcase(os.fspath(root.resolve()))
        matches: list[object] = []
        for process in psutil.process_iter(("pid", "exe")):
            try:
                executable = process.info["exe"]
                if not executable:
                    continue
                observed = os.path.normcase(
                    os.fspath(Path(executable).resolve())
                )
                if os.path.commonpath((expected, observed)) == expected:
                    matches.append(process)
            except (
                psutil.AccessDenied,
                psutil.NoSuchProcess,
                OSError,
                ValueError,
            ):
                continue
        return matches

    @classmethod
    def find_under(cls, root: Path) -> list[int]:
        return sorted(
            int(process.info["pid"])
            for process in cls._processes_under(root)
        )

    def adopt(self, executable: Path) -> int:
        matches = self.find(executable)
        if len(matches) != 1:
            raise RuntimeError("expected exactly one terminal process")
        if os.name == "nt":
            interactive_user = os.environ.get(
                "TRADEJOURNAL_MT5_INTERACTIVE_USER",
                "",
            ).strip()
            if not interactive_user:
                raise RuntimeError("dedicated interactive user is unavailable")
            verify_interactive_process_identity(
                interactive_user,
                matches[0],
            )
        return self._save_identity(
            matches[0],
            executable,
            portable=True,
        )

    @classmethod
    def cleanup_path(cls, executable: Path, timeout: float = 15) -> bool:
        import psutil

        root = executable.parent
        deadline = time.monotonic() + timeout
        ok = True
        quiet_observations = 0
        while time.monotonic() < deadline:
            processes = cls._processes_under(root)
            if not processes:
                quiet_observations += 1
                if quiet_observations >= 2:
                    return ok
                time.sleep(0.1)
                continue
            quiet_observations = 0
            tracked = []
            for process in processes:
                try:
                    process.terminate()
                    tracked.append(process)
                except psutil.NoSuchProcess:
                    pass
                except (psutil.AccessDenied, OSError):
                    ok = False
            if not tracked:
                time.sleep(0.1)
                continue
            remaining = max(0.0, deadline - time.monotonic())
            _, alive = psutil.wait_procs(
                tracked,
                timeout=min(2.0, remaining),
            )
            for process in alive:
                try:
                    process.kill()
                except psutil.NoSuchProcess:
                    pass
                except (psutil.AccessDenied, OSError):
                    ok = False
            if alive:
                remaining = max(0.0, deadline - time.monotonic())
                _, alive = psutil.wait_procs(
                    alive,
                    timeout=min(2.0, remaining),
                )
                if alive:
                    ok = False
        return ok and not cls._processes_under(root)

    def stop(self) -> bool:
        from ..state_store import atomic_json, read_json

        state = read_json(self.state_path)
        pid = state.get("pid")
        if not isinstance(pid, int):
            return False
        try:
            import psutil

            process = psutil.Process(pid)
            if Path(process.exe()).resolve() != Path(state["executable"]).resolve():
                raise RuntimeError("PID executable mismatch")
            recorded_creation_time = state.get("creation_time_unix_ms")
            if recorded_creation_time is not None:
                if (
                    not isinstance(recorded_creation_time, int)
                    or isinstance(recorded_creation_time, bool)
                    or int(process.create_time() * 1000)
                    != recorded_creation_time
                ):
                    raise RuntimeError("PID creation time mismatch")
            process.terminate()
            process.wait(15)
        except psutil.NoSuchProcess:
            pass
        atomic_json(
            self.state_path,
            {
                "schema_version": self.STATE_SCHEMA_VERSION,
                "stopped": True,
                "executable": state.get("executable"),
            },
        )
        return True
