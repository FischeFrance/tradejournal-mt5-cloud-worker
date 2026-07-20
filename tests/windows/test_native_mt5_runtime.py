from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import ANY, Mock, call, patch

import pytest

from windows_agent.provisioning.secret_store import WindowsSecretStore
from windows_agent.worker.native_mt5_runtime import (
    NativeMt5Error,
    NativeMt5Runtime,
    NativeMt5Status,
)


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
    loader = terminal.parent / "MQL5" / "Scripts" / "TradeJournal" / "TradeJournalLoader.ex5"
    loader.parent.mkdir(parents=True)
    loader.write_bytes(b"loader")
    loader.with_name("TradeJournalDiscovery.ex5").write_bytes(b"discovery")
    templates = terminal.parent / "Profiles" / "Templates"
    templates.mkdir(parents=True)
    (templates / "ADX.tpl").write_text(
        "<chart>\nsymbol=GBPUSD\n<window>\n</window>\n</chart>\n",
        encoding="utf-16",
    )
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
    bootstrap = runtime.state / "login-bootstrap.ini"
    discovery = runtime.state / "symbol-discovery.ini"
    startup = runtime.state / "startup.ini"
    runtime.state.mkdir()
    bootstrap.write_text("Password=not-a-real-secret")
    discovery.write_text("KeepPrivate=1")
    startup.write_text("KeepPrivate=1")
    expected = NativeMt5Status(
        pid=123,
        account={"login": "42", "server": "Demo", "trade_allowed": False},
        heartbeat={"terminal_connected": True},
        files_path=runtime.files,
    )
    with (
        patch.object(
            runtime,
            "_write_startup_config",
            side_effect=[bootstrap, discovery, startup],
        ) as write_config,
        patch.object(runtime, "_journal_checkpoint", return_value={}) as checkpoint,
        patch.object(runtime, "_start_process") as start_process,
        patch.object(runtime, "_wait_for_authorization") as wait_for_authorization,
        patch.object(runtime, "_wait_for_account_database") as wait_for_database,
        patch.object(runtime, "_wait_for_investor_sync") as wait_for_investor_sync,
        patch.object(
            runtime, "_probe_broker_symbol", return_value="EURUSD.raw"
        ) as probe_symbol,
        patch.object(runtime, "_wait_for_heartbeat", return_value=expected),
        patch.object(runtime, "stop", return_value=True) as stop,
    ):
        result = runtime.start(
            login=42,
            server="Demo",
            investor_password="not-a-real-secret",
            expert_binary=expert,
            history_mode="new_only",
        )
    assert result == expected
    assert write_config.call_args_list == [
        call(
            42,
            "Demo",
            "not-a-real-secret",
            "EURUSD",
            keep_private=True,
            start_expert=False,
            filename="login-bootstrap.ini",
        ),
        call(
            None,
            None,
            None,
            "EURUSD",
            keep_private=True,
            start_expert=False,
            script_name="TradeJournal\\TradeJournalDiscovery",
            filename="symbol-discovery.ini",
        ),
        call(
            None,
            None,
            None,
            "EURUSD.raw",
            keep_private=True,
            start_expert=False,
            script_name="TradeJournal\\TradeJournalLoader",
            filename="startup.ini",
        ),
    ]
    assert checkpoint.call_count == 3
    assert wait_for_authorization.call_args_list == [
        call({}, 42, "Demo", ANY),
        call({}, 42, "Demo", ANY),
        call({}, 42, "Demo", ANY),
    ]
    wait_for_database.assert_called_once_with(ANY)
    assert wait_for_investor_sync.call_args_list == [
        call({}, 42, ANY),
        call({}, 42, ANY),
    ]
    probe_symbol.assert_called_once_with("EURUSD")
    assert start_process.call_args_list == [
        call(bootstrap),
        call(discovery, 42),
        call(startup, 42),
    ]
    assert stop.call_count == 2
    assert not bootstrap.exists()
    assert not discovery.exists()
    assert not startup.exists()
    assert (runtime.files / "history_mode").read_text(encoding="utf-8") == "new_only"
    template_path = runtime.files / "TradeJournalBridge.tpl"
    assert template_path.read_bytes().startswith(b"\xff\xfe")
    template = template_path.read_text(encoding="utf-16")
    assert "symbol=EURUSD.raw" in template
    assert r"path=Experts\TradeJournal\TradeJournalBridge.ex5" in template
    assert "expertmode=0" in template
    assert "<inputs>\nInpTimerSeconds=2" in template
    assert "InpBackfillHours=168" in template
    assert "InpSnapshotHistoryHours=87600" in template
    assert "InpCandleBars=200\n</inputs>" in template


def test_startup_config_uses_expert_name_relative_to_mql5_experts(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(WindowsSecretStore, "restrict_acl", staticmethod(lambda path: None))
    runtime = _runtime(tmp_path)
    config = runtime._write_startup_config(None, None, None, "EURUSD")
    content = config.read_text(encoding="utf-8")
    assert "Expert=TradeJournal\\TradeJournalBridge" in content
    assert str(runtime.terminal_root) not in content


def test_start_uses_loader_to_attach_bridge_after_account_sync(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    expert = tmp_path / "bridge.ex5"
    expert.write_bytes(b"expert")
    bootstrap = runtime.state / "login-bootstrap.ini"
    discovery = runtime.state / "symbol-discovery.ini"
    startup = runtime.state / "startup.ini"
    runtime.state.mkdir()
    bootstrap.write_text("temporary")
    discovery.write_text("temporary")
    startup.write_text("temporary")
    with (
        patch.object(
            runtime,
            "_write_startup_config",
            side_effect=[bootstrap, discovery, startup],
        ),
        patch.object(runtime, "_journal_checkpoint", return_value={}),
        patch.object(runtime, "_start_process") as start_process,
        patch.object(runtime, "_wait_for_authorization"),
        patch.object(runtime, "_wait_for_account_database"),
        patch.object(runtime, "_wait_for_investor_sync"),
        patch.object(runtime, "_probe_broker_symbol", return_value="EURUSD.raw"),
        patch.object(runtime, "_remove_readiness_files") as remove_readiness,
        patch.object(
            runtime,
            "_wait_for_heartbeat",
            side_effect=NativeMt5Error("terminal_not_ready"),
        ) as wait_for_heartbeat,
        patch.object(runtime, "stop", return_value=True) as stop,
    ):
        with pytest.raises(NativeMt5Error, match="terminal_not_ready"):
            runtime.start(
                login=42,
                server="Demo",
                investor_password="placeholder",
                expert_binary=expert,
                history_mode="new_only",
                timeout=90,
            )

    assert start_process.call_args_list == [
        call(bootstrap),
        call(discovery, 42),
        call(startup, 42),
    ]
    wait_for_heartbeat.assert_called_once_with(90.0, 42, "Demo")
    assert stop.call_count == 3
    assert remove_readiness.call_count == 1
    assert not bootstrap.exists()
    assert not discovery.exists()
    assert not startup.exists()


def test_start_process_uses_portable_config(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    config = runtime.state / "startup.ini"
    config.parent.mkdir()
    config.write_text("temporary")
    process = Mock(pid=123)
    with patch("subprocess.Popen", return_value=process) as popen:
        assert runtime._start_process(config, 42) is process
    args = popen.call_args.args[0]
    assert "/portable" in args
    assert "/login:42" in args
    assert any(value.startswith("/config:") for value in args)


def test_install_expert_rejects_unknown_history_mode(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    expert = tmp_path / "bridge.ex5"
    expert.write_bytes(b"expert")
    with pytest.raises(NativeMt5Error, match="invalid_history_mode"):
        runtime.install_expert(expert, "ten_year_snapshot")


def test_login_bootstrap_and_persisted_startup_configs(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(WindowsSecretStore, "restrict_acl", staticmethod(lambda path: None))
    runtime = _runtime(tmp_path)
    bootstrap = runtime._write_startup_config(
        42,
        "Demo",
        "investor-secret",
        "EURUSD",
        keep_private=True,
        start_expert=False,
        filename="login-bootstrap.ini",
    )
    bootstrap_content = bootstrap.read_text(encoding="utf-8")
    assert "Login=42" in bootstrap_content
    assert "Server=Demo" in bootstrap_content
    assert "Password=investor-secret" in bootstrap_content
    assert "KeepPrivate=1" in bootstrap_content
    assert "[StartUp]" not in bootstrap_content
    assert "Enabled=0" in bootstrap_content

    startup = runtime._write_startup_config(
        42,
        "Demo",
        None,
        "EURUSD",
        keep_private=True,
        start_expert=False,
        script_name="TradeJournal\\TradeJournalLoader",
        filename="startup.ini",
    )
    startup_content = startup.read_text(encoding="utf-8")
    assert "Login=42" in startup_content
    assert "Server=Demo" in startup_content
    assert "Password=" not in startup_content
    assert "KeepPrivate=1" in startup_content
    assert "Script=TradeJournal\\TradeJournalLoader" in startup_content
    assert "Expert=" not in startup_content
    assert "ShutdownTerminal=0" in startup_content


def test_startup_config_allows_configured_interactive_user_to_read(
    tmp_path: Path, monkeypatch
) -> None:
    runtime = _runtime(tmp_path)
    restricted: list[Path] = []
    monkeypatch.setattr(
        WindowsSecretStore,
        "restrict_acl",
        staticmethod(lambda path: restricted.append(Path(path))),
    )
    monkeypatch.setattr(
        NativeMt5Runtime,
        "_setting",
        staticmethod(
            lambda name: "Administrator"
            if name == "TRADEJOURNAL_MT5_INTERACTIVE_USER"
            else ""
        ),
    )
    completed = Mock(returncode=0)
    with patch("subprocess.run", return_value=completed) as run:
        config = runtime._write_startup_config(
            42,
            "Demo",
            "investor-secret",
            "EURUSD",
            keep_private=True,
            start_expert=False,
            filename="login-bootstrap.ini",
        )

    assert restricted == [config]
    run.assert_called_once_with(
        ["icacls", str(config), "/grant", "Administrator:(R)"],
        capture_output=True,
        text=True,
        check=False,
    )


def test_wait_for_authorization_reads_only_new_journal_lines(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    logs = runtime.terminal_root / "logs"
    logs.mkdir()
    journal = logs / "20260718.log"
    old = "AA\t0\t10:00:00\tNetwork\t'42': Invalid account\r\n".encode("utf-16-le")
    journal.write_bytes(old)
    checkpoint = runtime._journal_checkpoint()
    with journal.open("ab") as handle:
        handle.write(
            "BB\t0\t10:00:01\tNetwork\t'42': authorized on Demo through Access Point\r\n".encode(
                "utf-16-le"
            )
        )
    with patch.object(runtime, "_running_terminal_pids", return_value=[123]):
        runtime._wait_for_authorization(checkpoint, 42, "Demo", 1.0)


def test_wait_for_investor_sync_requires_sync_and_readonly_lines(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    logs = runtime.terminal_root / "logs"
    logs.mkdir()
    journal = logs / "20260718.log"
    checkpoint = runtime._journal_checkpoint()
    journal.write_bytes(
        (
            "AA\t0\t10:00:01\tNetwork\t'42': terminal synchronized with Demo Ltd.\r\n"
            "BB\t0\t10:00:02\tNetwork\t'42': trading has been disabled - investor mode\r\n"
        ).encode("utf-16-le")
    )
    with patch.object(runtime, "_running_terminal_pids", return_value=[123]):
        runtime._wait_for_investor_sync(checkpoint, 42, 1.0)


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
    bootstrap = runtime.state / "login-bootstrap.ini"
    discovery = runtime.state / "symbol-discovery.ini"
    startup = runtime.state / "startup.ini"
    runtime.state.mkdir()
    bootstrap.write_text("temporary")
    discovery.write_text("temporary")
    startup.write_text("temporary")
    with (
        patch.object(
            runtime,
            "_write_startup_config",
            side_effect=[bootstrap, discovery, startup],
        ),
        patch.object(runtime, "_journal_checkpoint", return_value={}),
        patch.object(runtime, "_start_process"),
        patch.object(runtime, "_wait_for_authorization"),
        patch.object(runtime, "_wait_for_account_database"),
        patch.object(runtime, "_wait_for_investor_sync"),
        patch.object(runtime, "_probe_broker_symbol", return_value="EURUSD.raw"),
        patch.object(runtime, "_remove_readiness_files"),
        patch.object(runtime, "stop", return_value=True),
    ):
        with pytest.raises(NativeMt5Error, match=code):
            runtime.start(
                login=42,
                server="Demo",
                investor_password="placeholder",
                expert_binary=expert,
            )
    assert not bootstrap.exists()
    assert not discovery.exists()
    assert not startup.exists()


def test_crashed_process_is_reported(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    process = Mock(pid=123)
    process.poll.return_value = 1
    runtime._process = process
    with patch.object(runtime, "_running_terminal_pids", return_value=[]):
        with pytest.raises(NativeMt5Error, match="mt5_process_crashed"):
            runtime._wait_for_heartbeat(1.0, 42, "Demo")
