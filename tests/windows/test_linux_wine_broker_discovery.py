from __future__ import annotations

import json
import os
import signal
import subprocess
from pathlib import Path, PureWindowsPath
from typing import Any

import pytest

from windows_agent.worker.linux_wine_broker_discovery import (
    LinuxWineBrokerDiscoveryLauncher,
    LinuxWineDiscoveryError,
)
from windows_agent.worker.mt5_broker_discovery import BrokerDiscoveryResult


pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="Linux Wine launcher tests require fcntl"
)


class FakeWineProcess:
    def __init__(self, factory: "FakeWinePopen", pid: int = 7301) -> None:
        self.factory = factory
        self.pid = pid
        self.returncode: int | None = None
        self.terminated = False

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float) -> int:
        self.factory.wait_timeouts.append(timeout)
        if self.factory.timeout and not self.terminated:
            raise subprocess.TimeoutExpired(self.factory.command, timeout)
        if self.factory.interrupt and not self.terminated:
            self.factory.interrupt = False
            raise KeyboardInterrupt
        if self.terminated:
            self.returncode = -signal.SIGTERM
            return self.returncode

        request = self.factory.to_unix_path(
            self.factory.command[self.factory.command.index("--request") + 1]
        )
        result = self.factory.to_unix_path(
            self.factory.command[self.factory.command.index("--result") + 1]
        )
        self.factory.request_payload = json.loads(request.read_text(encoding="utf-8"))
        if self.factory.result_symlink:
            target = result.with_name("attacker-controlled-result.json")
            target.write_text(json.dumps(self.factory.result_payload), encoding="utf-8")
            target.chmod(0o600)
            result.symlink_to(target)
        elif self.factory.write_result:
            result.write_text(json.dumps(self.factory.result_payload), encoding="utf-8")
            result.chmod(self.factory.result_mode)
        self.returncode = self.factory.return_code
        return self.returncode


class FakeWinePopen:
    def __init__(
        self,
        prefix: Path,
        *,
        result_payload: dict[str, Any] | None = None,
        return_code: int = 0,
        timeout: bool = False,
        interrupt: bool = False,
        write_result: bool = True,
        result_symlink: bool = False,
        result_mode: int = 0o600,
    ) -> None:
        self.prefix = prefix
        self.result_payload = result_payload or {
            "ok": True,
            "code": "ok",
            "server": "Broker-Live",
            "query_index": 0,
        }
        self.return_code = return_code
        self.timeout = timeout
        self.interrupt = interrupt
        self.write_result = write_result
        self.result_symlink = result_symlink
        self.result_mode = result_mode
        self.command: list[str] = []
        self.options: dict[str, Any] = {}
        self.process: FakeWineProcess | None = None
        self.request_payload: dict[str, Any] | None = None
        self.wait_timeouts: list[float] = []
        self.signals: list[int] = []

    def __call__(self, command: list[str], **options: Any) -> FakeWineProcess:
        self.command = list(command)
        self.options = options
        self.process = FakeWineProcess(self)
        return self.process

    def kill_process_group(self, pid: int, sent_signal: int) -> None:
        assert self.process is not None
        assert pid == self.process.pid
        self.signals.append(sent_signal)
        self.process.terminated = True

    def to_unix_path(self, value: str) -> Path:
        path = PureWindowsPath(value)
        assert path.drive.casefold() == "c:"
        return self.prefix / "drive_c" / Path(*path.parts[1:])


class RuntimeTree:
    def __init__(self, root: Path) -> None:
        self.wine = root / "bin" / "wine"
        self.wine.parent.mkdir()
        self.wine.touch()
        self.wine.chmod(0o755)

        self.prefix = root / "prefix"
        self.prefix.mkdir(mode=0o700)
        (self.prefix / "drive_c").mkdir()
        (self.prefix / "dosdevices").mkdir()
        (self.prefix / "dosdevices" / "c:").symlink_to(
            "../drive_c", target_is_directory=True
        )

        self.python = self.prefix / "drive_c" / "Python311" / "python.exe"
        self.python.parent.mkdir()
        self.python.touch()

        self.terminal = (
            self.prefix
            / "drive_c"
            / "Program Files"
            / "MetaTrader 5"
            / "terminal64.exe"
        )
        self.terminal.parent.mkdir(parents=True)
        self.terminal.touch()

        self.repository = self.prefix / "drive_c" / "TradeJournal" / "source"
        helper = (
            self.repository
            / "windows_agent"
            / "worker"
            / "wine_mt5_broker_discovery.py"
        )
        helper.parent.mkdir(parents=True)
        helper.touch()

        self.exchange = self.prefix / "drive_c" / "TradeJournal" / "exchange"
        self.exchange.mkdir(mode=0o700)

    @property
    def terminal_windows_path(self) -> str:
        return r"C:\Program Files\MetaTrader 5\terminal64.exe"


@pytest.fixture
def runtime_tree(tmp_path: Path) -> RuntimeTree:
    return RuntimeTree(tmp_path)


def launcher(
    tree: RuntimeTree,
    process_factory: FakeWinePopen,
    *,
    repository: Path | None = None,
    exchange: Path | None = None,
    source_environment: dict[str, str] | None = None,
) -> LinuxWineBrokerDiscoveryLauncher:
    return LinuxWineBrokerDiscoveryLauncher(
        wine_binary=tree.wine,
        windows_python=r"C:\Python311\python.exe",
        wineprefix=tree.prefix,
        display=":99",
        repository_root=repository or tree.repository,
        exchange_root=exchange or tree.exchange,
        source_environment=source_environment
        or {"PATH": "/untrusted/path", "HOME": "/untrusted/home"},
        popen_factory=process_factory,
        kill_process_group=process_factory.kill_process_group,
    )


def test_success_uses_private_c_drive_and_no_secret_transport(
    runtime_tree: RuntimeTree,
) -> None:
    process_factory = FakeWinePopen(runtime_tree.prefix)
    worker = launcher(
        runtime_tree,
        process_factory,
        source_environment={
            "PATH": "/untrusted/path",
            "HOME": "/untrusted/home",
            "LANG": "untrusted",
        },
    )

    result = worker.discover(
        terminal_path=runtime_tree.terminal_windows_path,
        expected_server="Broker-Live",
        queries=("Broker",),
        timeout_seconds=12,
    )

    assert result == BrokerDiscoveryResult(server="Broker-Live", query_index=0)
    assert process_factory.request_payload == {
        "terminal_path": runtime_tree.terminal_windows_path,
        "expected_server": "Broker-Live",
        "queries": ["Broker"],
        "timeout_seconds": 12.0,
    }
    assert "candidate_pids" not in process_factory.request_payload
    serialized_request = json.dumps(process_factory.request_payload).casefold()
    assert not any(word in serialized_request for word in ("password", "token", "secret"))

    assert process_factory.command[1:4] == [
        r"C:\Python311\python.exe",
        "-m",
        "windows_agent.worker.wine_mt5_broker_discovery",
    ]
    assert all(argument.startswith("C:\\") for argument in (
        process_factory.command[process_factory.command.index("--request") + 1],
        process_factory.command[process_factory.command.index("--result") + 1],
    ))
    assert not any("Broker" in argument for argument in process_factory.command)
    assert process_factory.options["cwd"] == runtime_tree.repository.resolve()
    assert process_factory.options["start_new_session"] is True
    assert process_factory.options["stdin"] is subprocess.DEVNULL
    assert process_factory.options["stdout"] is subprocess.DEVNULL
    assert process_factory.options["stderr"] is subprocess.DEVNULL

    environment = process_factory.options["env"]
    assert environment["WINEPREFIX"] == str(runtime_tree.prefix.resolve())
    assert environment["DISPLAY"] == ":99"
    assert environment["WINEARCH"] == "win64"
    assert environment["HOME"] == "/home/runtime"
    assert environment["PATH"] != "/untrusted/path"
    assert not any(
        marker in name.casefold()
        for name in environment
        for marker in ("password", "token", "secret", "login", "account")
    )
    assert list(runtime_tree.exchange.iterdir()) == []


@pytest.mark.parametrize(
    ("payload", "return_code", "expected_code"),
    [
        (
            {
                "ok": True,
                "code": "ok",
                "server": "Broker-Live",
                "query_index": 0,
            },
            7,
            "helper_failed",
        ),
        (
            {
                "ok": False,
                "code": "no_exact_match",
                "message": "untrusted helper detail",
            },
            3,
            "no_exact_match",
        ),
        (
            {
                "ok": False,
                "code": "no_exact_match",
                "message": "untrusted helper detail",
            },
            0,
            "helper_failed",
        ),
        (
            {
                "ok": True,
                "code": "ok",
                "server": "Broker-Live\nspoofed",
                "query_index": 0,
            },
            0,
            "helper_failed",
        ),
        (
            {
                "ok": True,
                "code": "ok",
                "server": "Broker-Live",
                "query_index": True,
            },
            0,
            "helper_failed",
        ),
        (
            {
                "ok": True,
                "code": "ok",
                "server": "Broker-Live",
                "query_index": 0,
                "extra": "field",
            },
            0,
            "helper_failed",
        ),
    ],
)
def test_result_schema_and_exit_code_fail_closed_and_cleanup(
    runtime_tree: RuntimeTree,
    payload: dict[str, Any],
    return_code: int,
    expected_code: str,
) -> None:
    process_factory = FakeWinePopen(
        runtime_tree.prefix,
        result_payload=payload,
        return_code=return_code,
    )
    worker = launcher(runtime_tree, process_factory)

    with pytest.raises(LinuxWineDiscoveryError) as captured:
        worker.discover(
            terminal_path=runtime_tree.terminal_windows_path,
            expected_server="Broker-Live",
            queries=("Broker",),
        )

    assert captured.value.code == expected_code
    assert "untrusted" not in str(captured.value)
    assert list(runtime_tree.exchange.iterdir()) == []


def test_timeout_terminates_helper_process_group_and_cleans_files(
    runtime_tree: RuntimeTree,
) -> None:
    process_factory = FakeWinePopen(runtime_tree.prefix, timeout=True)
    worker = launcher(runtime_tree, process_factory)

    with pytest.raises(LinuxWineDiscoveryError) as captured:
        worker.discover(
            terminal_path=runtime_tree.terminal_windows_path,
            expected_server="Broker-Live",
            queries=("Broker",),
            timeout_seconds=4,
        )

    assert captured.value.code == "timeout"
    assert process_factory.signals == [signal.SIGTERM]
    assert process_factory.wait_timeouts == [14.0, 2.0]
    assert list(runtime_tree.exchange.iterdir()) == []


def test_keyboard_interrupt_also_terminates_process_group_and_cleans_files(
    runtime_tree: RuntimeTree,
) -> None:
    process_factory = FakeWinePopen(runtime_tree.prefix, interrupt=True)
    worker = launcher(runtime_tree, process_factory)

    with pytest.raises(KeyboardInterrupt):
        worker.discover(
            terminal_path=runtime_tree.terminal_windows_path,
            expected_server="Broker-Live",
            queries=("Broker",),
        )

    assert process_factory.signals == [signal.SIGTERM]
    assert list(runtime_tree.exchange.iterdir()) == []


def test_same_prefix_discovery_is_cross_process_exclusive(
    runtime_tree: RuntimeTree,
) -> None:
    first_factory = FakeWinePopen(runtime_tree.prefix)
    second_factory = FakeWinePopen(runtime_tree.prefix)
    first = launcher(runtime_tree, first_factory)
    second = launcher(runtime_tree, second_factory)
    descriptor = first._acquire_prefix_lock()
    try:
        with pytest.raises(LinuxWineDiscoveryError) as captured:
            second.discover(
                terminal_path=runtime_tree.terminal_windows_path,
                expected_server="Broker-Live",
                queries=("Broker",),
            )
    finally:
        os.close(descriptor)

    assert captured.value.code == "busy"
    assert second_factory.command == []
    assert list(runtime_tree.exchange.iterdir()) == []


def test_private_prefix_without_z_drive_is_required(runtime_tree: RuntimeTree) -> None:
    process_factory = FakeWinePopen(runtime_tree.prefix)
    runtime_tree.prefix.chmod(0o755)

    with pytest.raises(LinuxWineDiscoveryError) as captured:
        launcher(runtime_tree, process_factory)

    assert captured.value.code == "runtime_unavailable"
    runtime_tree.prefix.chmod(0o700)
    (runtime_tree.prefix / "dosdevices" / "z:").symlink_to("/")
    with pytest.raises(LinuxWineDiscoveryError) as captured:
        launcher(runtime_tree, process_factory)

    assert captured.value.code == "runtime_unavailable"


def test_wine_c_mapping_must_match_validated_drive_c(
    runtime_tree: RuntimeTree, tmp_path: Path
) -> None:
    process_factory = FakeWinePopen(runtime_tree.prefix)
    c_drive = runtime_tree.prefix / "dosdevices" / "c:"
    c_drive.unlink()

    with pytest.raises(LinuxWineDiscoveryError) as captured:
        launcher(runtime_tree, process_factory)
    assert captured.value.code == "runtime_unavailable"

    outside = tmp_path / "different-drive-c"
    outside.mkdir()
    c_drive.symlink_to(outside, target_is_directory=True)
    with pytest.raises(LinuxWineDiscoveryError) as captured:
        launcher(runtime_tree, process_factory)
    assert captured.value.code == "runtime_unavailable"


def test_repository_and_exchange_must_be_confined_to_drive_c(
    runtime_tree: RuntimeTree, tmp_path: Path
) -> None:
    outside_repository = tmp_path / "outside-source"
    helper = (
        outside_repository
        / "windows_agent"
        / "worker"
        / "wine_mt5_broker_discovery.py"
    )
    helper.parent.mkdir(parents=True)
    helper.touch()
    process_factory = FakeWinePopen(runtime_tree.prefix)

    with pytest.raises(LinuxWineDiscoveryError) as captured:
        launcher(runtime_tree, process_factory, repository=outside_repository)
    assert captured.value.code == "runtime_unavailable"

    outside_exchange = tmp_path / "outside-exchange"
    outside_exchange.mkdir(mode=0o700)
    with pytest.raises(LinuxWineDiscoveryError) as captured:
        launcher(runtime_tree, process_factory, exchange=outside_exchange)
    assert captured.value.code == "runtime_unavailable"


def test_symlinked_terminal_and_traversal_are_rejected_before_launch(
    runtime_tree: RuntimeTree,
) -> None:
    runtime_tree.terminal.unlink()
    real_terminal = runtime_tree.terminal.with_name("real-terminal64.exe")
    real_terminal.touch()
    runtime_tree.terminal.symlink_to(real_terminal)
    process_factory = FakeWinePopen(runtime_tree.prefix)
    worker = launcher(runtime_tree, process_factory)

    with pytest.raises(LinuxWineDiscoveryError) as captured:
        worker.discover(
            terminal_path=runtime_tree.terminal_windows_path,
            expected_server="Broker-Live",
            queries=("Broker",),
        )
    assert captured.value.code == "runtime_unavailable"
    assert process_factory.command == []

    with pytest.raises(LinuxWineDiscoveryError) as captured:
        worker.discover(
            terminal_path=r"C:\Program Files\..\terminal64.exe",
            expected_server="Broker-Live",
            queries=("Broker",),
        )
    assert captured.value.code == "invalid_request"
    assert list(runtime_tree.exchange.iterdir()) == []


def test_symlink_or_world_readable_result_is_rejected_and_cleaned(
    runtime_tree: RuntimeTree,
) -> None:
    for process_factory in (
        FakeWinePopen(runtime_tree.prefix, result_symlink=True),
        FakeWinePopen(runtime_tree.prefix, result_mode=0o644),
    ):
        worker = launcher(runtime_tree, process_factory)
        with pytest.raises(LinuxWineDiscoveryError) as captured:
            worker.discover(
                terminal_path=runtime_tree.terminal_windows_path,
                expected_server="Broker-Live",
                queries=("Broker",),
            )
        assert captured.value.code == "helper_failed"
        assert list(runtime_tree.exchange.iterdir()) == []


def test_sensitive_parent_environment_is_rejected(runtime_tree: RuntimeTree) -> None:
    process_factory = FakeWinePopen(runtime_tree.prefix)

    with pytest.raises(LinuxWineDiscoveryError) as captured:
        launcher(
            runtime_tree,
            process_factory,
            source_environment={"PATH": "/usr/bin", "MT5_PASSWORD_FILE": "/secret"},
        )

    assert captured.value.code == "runtime_unavailable"
    assert process_factory.command == []
