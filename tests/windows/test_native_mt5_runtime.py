from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from windows_agent.provisioning.secret_store import WindowsSecretStore
from windows_agent.worker.native_mt5_runtime import NativeMt5Error, NativeMt5Runtime


@pytest.fixture(autouse=True)
def _no_machine_overrides(monkeypatch):
    # NativeMt5Runtime._setting() deliberately falls back to a machine-wide registry key (see its
    # own docstring/usage), not just the process environment, so it stays visible to a real
    # subprocess.Popen()-launched terminal on the same host. That means these tests are NOT
    # hermetic against whatever an operator has set on the actual VPS: TRADEJOURNAL_MT5_
    # INTERACTIVE_USER being set there (a legitimate real-flow requirement, see _start_process)
    # silently swaps the code path under test from a plain subprocess.Popen() call to a schtasks/
    # scheduled-task launch, which these tests never patch -- they fail with an unrelated
    # "Mock object does not support the context manager protocol" instead of testing what they
    # say they test. Force a clean, override-free environment so behavior here depends only on
    # what each test explicitly sets up, never on the host machine's operational configuration.
    monkeypatch.setattr(NativeMt5Runtime, "_setting", staticmethod(lambda name: ""))


def _runtime(tmp_path: Path) -> NativeMt5Runtime:
    terminal = tmp_path / "terminal" / "terminal64.exe"
    terminal.parent.mkdir()
    terminal.write_bytes(b"terminal")
    return NativeMt5Runtime(tmp_path, "00000000-0000-4000-8000-000000000001")


def _envelope(payload: dict[str, object]) -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "generated_at": "2026-07-17T00:00:00Z",
            "sequence": 1,
            "account_identity": {"login": "42", "server": "Demo"},
            "server_identity": "Demo",
            "payload": payload,
        }
    )


def test_start_uses_portable_config_and_removes_plaintext(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    expert = tmp_path / "bridge.ex5"
    expert.write_bytes(b"expert")
    runtime.files.mkdir(parents=True)
    (runtime.files / "account.json").write_text(
        _envelope({"login": "42", "server": "Demo", "trade_allowed": False})
    )
    (runtime.files / "heartbeat.json").write_text(
        _envelope({"terminal_connected": True})
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


def test_startup_config_uses_expert_name_relative_to_mql5_experts(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(WindowsSecretStore, "restrict_acl", staticmethod(lambda path: None))
    runtime = _runtime(tmp_path)
    config = runtime._write_startup_config(None, None, None, "EURUSD")
    content = config.read_text(encoding="utf-8")
    assert "Expert=TradeJournal\\TradeJournalBridge" in content
    assert str(runtime.terminal_root) not in content


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
    (runtime.files / "account.json").write_text(_envelope(account))
    (runtime.files / "heartbeat.json").write_text(
        _envelope({"terminal_connected": True})
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
