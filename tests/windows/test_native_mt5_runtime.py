from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from windows_agent.worker.native_mt5_runtime import NativeMt5Error, NativeMt5Runtime


def _runtime(tmp_path: Path) -> NativeMt5Runtime:
    terminal = tmp_path / "terminal" / "terminal64.exe"
    terminal.parent.mkdir()
    terminal.write_bytes(b"terminal")
    return NativeMt5Runtime(tmp_path, "00000000-0000-4000-8000-000000000001")


def test_start_uses_portable_config_and_removes_plaintext(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    expert = tmp_path / "bridge.ex5"
    expert.write_bytes(b"expert")
    runtime.files.mkdir(parents=True)
    (runtime.files / "account.json").write_text(
        json.dumps({"login": "42", "server": "Demo", "trade_allowed": False})
    )
    (runtime.files / "heartbeat.json").write_text(
        json.dumps({"terminal_connected": True})
    )
    process = Mock(pid=123)
    process.poll.return_value = None
    with (
        patch("subprocess.Popen", return_value=process) as popen,
        patch.object(runtime, "_write_startup_config") as write_config,
    ):
        config = runtime.state / "startup.ini"
        config.parent.mkdir()
        config.write_text("Password=not-a-real-secret")
        write_config.return_value = config
        result = runtime.start(
            login=42,
            server="Demo",
            investor_password="not-a-real-secret",
            expert_binary=expert,
        )
    assert result.pid == 123
    args = popen.call_args.args[0]
    assert "/portable" in args
    assert any(value.startswith("/config:") for value in args)
    assert not config.exists()


@pytest.mark.parametrize(
    ("account", "code"),
    [
        ({"login": "99", "server": "Demo", "trade_allowed": False}, "identity_mismatch"),
        ({"login": "42", "server": "Other", "trade_allowed": False}, "server_identity_mismatch"),
        ({"login": "42", "server": "Demo", "trade_allowed": True}, "investor_readonly_not_verified"),
    ],
)
def test_identity_and_readonly_guards(
    tmp_path: Path, account: dict[str, object], code: str
) -> None:
    runtime = _runtime(tmp_path)
    expert = tmp_path / "bridge.ex5"
    expert.write_bytes(b"expert")
    runtime.files.mkdir(parents=True)
    (runtime.files / "account.json").write_text(json.dumps(account))
    (runtime.files / "heartbeat.json").write_text(
        json.dumps({"terminal_connected": True})
    )
    process = Mock(pid=123)
    process.poll.return_value = None
    with (
        patch("subprocess.Popen", return_value=process),
        patch.object(runtime, "_write_startup_config") as write_config,
    ):
        config = runtime.state / "startup.ini"
        config.parent.mkdir()
        config.write_text("temporary")
        write_config.return_value = config
        with pytest.raises(NativeMt5Error, match=code):
            runtime.start(
                login=42,
                server="Demo",
                investor_password="placeholder",
                expert_binary=expert,
            )
    assert not config.exists()


def test_crashed_process_is_reported(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    expert = tmp_path / "bridge.ex5"
    expert.write_bytes(b"expert")
    process = Mock(pid=123)
    process.poll.return_value = 1
    with (
        patch("subprocess.Popen", return_value=process),
        patch.object(runtime, "_write_startup_config") as write_config,
    ):
        config = runtime.state / "startup.ini"
        config.parent.mkdir()
        config.write_text("temporary")
        write_config.return_value = config
        with pytest.raises(NativeMt5Error, match="mt5_process_crashed"):
            runtime.start(
                login=42,
                server="Demo",
                investor_password="placeholder",
                expert_binary=expert,
            )

