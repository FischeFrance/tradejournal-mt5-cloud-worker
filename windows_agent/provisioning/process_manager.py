from __future__ import annotations

import subprocess
from pathlib import Path

from ..state_store import atomic_json, read_json


class ProcessManager:
    def __init__(self, state_path: Path) -> None:
        self.state_path = state_path

    def start(self, executable: Path, portable: bool = True) -> int:
        executable = executable.resolve()
        if not executable.is_file() or executable.name.lower() != "terminal64.exe":
            raise ValueError("invalid terminal executable")
        args = [str(executable)] + (["/portable"] if portable else [])
        process = subprocess.Popen(args, cwd=executable.parent, close_fds=True)
        atomic_json(
            self.state_path,
            {"pid": process.pid, "executable": str(executable), "portable": portable},
        )
        return process.pid

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

    def adopt(self, executable: Path) -> int:
        matches = self.find(executable)
        if len(matches) != 1:
            raise RuntimeError("expected exactly one terminal process")
        atomic_json(
            self.state_path,
            {
                "pid": matches[0],
                "executable": str(executable.resolve()),
                "portable": True,
            },
        )
        return matches[0]

    @classmethod
    def cleanup_path(cls, executable: Path, timeout: float = 15) -> bool:
        import psutil

        ok = True
        for pid in cls.find(executable):
            try:
                process = psutil.Process(pid)
                process.terminate()
                process.wait(timeout)
            except psutil.TimeoutExpired:
                process.kill()
                process.wait(5)
            except psutil.NoSuchProcess:
                pass
            except (psutil.AccessDenied, OSError):
                ok = False
        return ok and not cls.find(executable)

    def stop(self) -> bool:
        state = read_json(self.state_path)
        pid = state.get("pid")
        if not isinstance(pid, int):
            return False
        try:
            import psutil

            process = psutil.Process(pid)
            if Path(process.exe()).resolve() != Path(state["executable"]).resolve():
                raise RuntimeError("PID executable mismatch")
            process.terminate()
            process.wait(15)
        except psutil.NoSuchProcess:
            pass
        atomic_json(
            self.state_path, {"stopped": True, "executable": state.get("executable")}
        )
        return True
