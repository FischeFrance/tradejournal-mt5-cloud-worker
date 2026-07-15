import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from windows_agent.provisioning.process_manager import ProcessManager
from windows_agent.worker.direct_mt5_adapter import DirectMt5Adapter, Mt5IpcError
from windows_agent.worker.direct_mt5_adapter import Mt5VersionMismatch


class Module:
    COPY_TICKS_ALL = 0

    def __init__(self, results, error=(-10005, "IPC timeout")):
        self.results = list(results)
        self.error = error
        self.calls = []
        self.shutdown_count = 0

    def initialize(self, *args, **kwargs):
        if args:
            kwargs["path"] = args[0]
        self.calls.append(kwargs)
        return self.results.pop(0)

    def last_error(self):
        return self.error

    def shutdown(self):
        self.shutdown_count += 1


def terminal(tmp_path: Path) -> Path:
    path = tmp_path / "path with spaces" / "terminal64.exe"
    path.parent.mkdir()
    path.touch()
    return path


def test_initialize_launches_once_with_portable_and_space_path(tmp_path):
    module = Module([True])
    adapter = DirectMt5Adapter(terminal(tmp_path), 1, "Demo", module=module)
    adapter.initialize()
    assert len(module.calls) == 1
    assert module.calls[0]["portable"] is True
    assert "path with spaces" in module.calls[0]["path"]
    assert module.calls[0]["timeout"] == 60_000


@pytest.mark.parametrize("layout", ("global", "isolated"))
def test_initialize_supported_for_global_and_isolated_mock(tmp_path, layout):
    path = tmp_path / layout / "terminal64.exe"
    path.parent.mkdir()
    path.touch()
    module = Module([True])
    DirectMt5Adapter(path, 1, "Demo", module=module).initialize()
    assert module.calls[0]["path"] == str(path.resolve())


def test_readiness_retry_has_limited_backoff(tmp_path, monkeypatch):
    module = Module([False, True])
    sleeps = []
    monkeypatch.setattr(
        "windows_agent.worker.direct_mt5_adapter.time.sleep", sleeps.append
    )
    DirectMt5Adapter(
        terminal(tmp_path), 1, "Demo", module=module, retries=2, retry_delay=0.25
    ).initialize()
    assert len(module.calls) == 2 and sleeps == [0.25]
    assert module.shutdown_count == 1


def test_last_error_maps_ipc_timeout(tmp_path):
    module = Module([False, False])
    with pytest.raises(Mt5IpcError, match="-10005"):
        DirectMt5Adapter(
            terminal(tmp_path), 1, "Demo", module=module, retries=2, retry_delay=0
        ).initialize()


def test_confirmed_version_mismatch_fails_fast(tmp_path, monkeypatch):
    module = Module([True])
    module.__version__ = "5.0.5735"
    fake = SimpleNamespace(GetFileVersionInfo=lambda *_args: {"FileVersionLS": 5836})
    monkeypatch.setitem(sys.modules, "win32api", fake)
    with pytest.raises(Mt5VersionMismatch, match="5836"):
        DirectMt5Adapter(terminal(tmp_path), 1, "Demo", module=module).initialize()
    assert module.calls == []


def test_customer_lifecycle_never_prestarts_terminal(tmp_path):
    from tests.windows.test_customer_flow import Process, run

    original = Process.start
    Process.start = lambda *_args: (_ for _ in ()).throw(AssertionError("double start"))
    try:
        assert run(tmp_path)["final_result"] == "PASS"
    finally:
        Process.start = original


def test_stale_pid_cleanup_is_idempotent(tmp_path):
    manager = ProcessManager(tmp_path / "state.json")
    manager.state_path.write_text(
        '{"pid":2147483647,"executable":"C:\\\\missing\\\\terminal64.exe"}',
        encoding="utf-8",
    )
    assert manager.stop() is True
    assert manager.stop() is False


def test_probe_hard_timeout_is_sanitized_and_cleans_new_process(tmp_path, monkeypatch):
    from windows_agent.initialize_probe import run_probe

    class Child:
        pid = 2147483647

        def communicate(self, timeout):
            raise subprocess.TimeoutExpired("probe", timeout)

    monkeypatch.setattr(subprocess, "Popen", lambda *_a, **_k: Child())
    monkeypatch.setattr(ProcessManager, "find", lambda _path: [])
    report = run_probe(terminal(tmp_path), hard_timeout=1)
    assert report["last_error_code"] == -10005
    assert report["last_error_message"] == "IPC supervisor timeout"
    assert report["process_cleanup_succeeded"] is True
