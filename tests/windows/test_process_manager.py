from pathlib import Path

import psutil
import pytest

from windows_agent.provisioning.process_manager import ProcessManager
from windows_agent.state_store import atomic_json, read_json


def test_stale_pid_cleanup_is_idempotent(tmp_path: Path) -> None:
    manager = ProcessManager(tmp_path / "state.json")
    manager.state_path.write_text(
        '{"pid":2147483647,"executable":"C:\\\\missing\\\\terminal64.exe"}',
        encoding="utf-8",
    )

    assert manager.stop() is True
    assert manager.stop() is False


def test_adopt_persists_pid_creation_time_generation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    terminal = tmp_path / "terminal64.exe"
    terminal.write_bytes(b"fixture")
    manager = ProcessManager(tmp_path / "state.json")

    class Process:
        @staticmethod
        def exe() -> str:
            return str(terminal)

        @staticmethod
        def create_time() -> float:
            return 1_785_230_000.125

    monkeypatch.setattr(ProcessManager, "find", staticmethod(lambda _path: [4321]))
    monkeypatch.setattr(psutil, "Process", lambda _pid: Process())

    assert manager.adopt(terminal) == 4321

    state = read_json(manager.state_path)
    assert state == {
        "schema_version": 2,
        "pid": 4321,
        "executable": str(terminal.resolve()),
        "creation_time_unix_ms": 1_785_230_000_125,
        "portable": True,
    }


def test_stop_rejects_reused_pid_generation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    terminal = tmp_path / "terminal64.exe"
    terminal.write_bytes(b"fixture")
    manager = ProcessManager(tmp_path / "state.json")
    atomic_json(
        manager.state_path,
        {
            "schema_version": 2,
            "pid": 4321,
            "executable": str(terminal.resolve()),
            "creation_time_unix_ms": 1_000,
            "portable": True,
        },
    )
    terminated: list[bool] = []

    class Process:
        @staticmethod
        def exe() -> str:
            return str(terminal)

        @staticmethod
        def create_time() -> float:
            return 2.0

        @staticmethod
        def terminate() -> None:
            terminated.append(True)

    monkeypatch.setattr(psutil, "Process", lambda _pid: Process())

    with pytest.raises(RuntimeError, match="creation time"):
        manager.stop()

    assert terminated == []


def test_cleanup_path_terminates_every_process_inside_instance_terminal(
    tmp_path: Path,
    monkeypatch,
) -> None:
    terminal_root = tmp_path / "instance" / "terminal"
    terminal_root.mkdir(parents=True)
    terminal = terminal_root / "terminal64.exe"
    metaeditor = terminal_root / "metaeditor64.exe"
    outside = tmp_path / "other" / "metaeditor64.exe"
    terminal.write_bytes(b"terminal")
    metaeditor.write_bytes(b"metaeditor")
    outside.parent.mkdir()
    outside.write_bytes(b"outside")

    class Process:
        def __init__(self, pid: int, executable: Path) -> None:
            self.info = {"pid": pid, "exe": str(executable)}
            self.alive = True
            self.terminated = False
            self.killed = False

        def terminate(self) -> None:
            self.terminated = True
            self.alive = False

        def kill(self) -> None:
            self.killed = True
            self.alive = False

    terminal_process = Process(1001, terminal)
    compiler_process = Process(1002, metaeditor)
    unrelated_process = Process(2001, outside)
    processes = [
        terminal_process,
        compiler_process,
        unrelated_process,
    ]

    monkeypatch.setattr(
        psutil,
        "process_iter",
        lambda _attributes: [
            process for process in processes if process.alive
        ],
    )
    monkeypatch.setattr(
        psutil,
        "wait_procs",
        lambda candidates, timeout: (
            [process for process in candidates if not process.alive],
            [process for process in candidates if process.alive],
        ),
    )

    assert ProcessManager.find_under(terminal_root) == [1001, 1002]
    assert ProcessManager.cleanup_path(terminal, timeout=1) is True
    assert terminal_process.terminated is True
    assert compiler_process.terminated is True
    assert unrelated_process.terminated is False
    assert unrelated_process.alive is True


def test_cleanup_path_rejects_reparse_terminal_root(
    tmp_path: Path,
) -> None:
    real_root = tmp_path / "real"
    real_root.mkdir()
    linked_root = tmp_path / "linked"
    linked_root.symlink_to(real_root, target_is_directory=True)

    with pytest.raises(ValueError, match="unsafe"):
        ProcessManager.cleanup_path(
            linked_root / "terminal64.exe",
            timeout=1,
        )
