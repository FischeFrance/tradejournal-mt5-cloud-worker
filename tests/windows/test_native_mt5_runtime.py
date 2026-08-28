from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, Mock, call, patch

import pytest

from windows_agent.provisioning.secret_store import WindowsSecretStore
from windows_agent.worker.native_mt5_runtime import (
    _AuthFailureMonitor,
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
    (terminal.parent / "Profiles" / "Charts").mkdir(parents=True)
    (templates / "ADX.tpl").write_text(
        "<chart>\nsymbol=GBPUSD\n<window>\n</window>\n</chart>\n",
        encoding="utf-16",
    )
    return NativeMt5Runtime(
        tmp_path,
        "00000000-0000-4000-8000-000000000001",
        symbol_hint_root=tmp_path / ".broker-symbol-hints",
    )


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
    startup = runtime.state / "startup.ini"
    runtime.state.mkdir()
    bootstrap.write_text("Password=not-a-real-secret")
    startup.write_text("Password=not-a-real-secret")
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
            side_effect=[bootstrap, startup],
        ) as write_config,
        patch.object(
            runtime,
            "_start_and_wait_for_authorization",
            side_effect=[({}, "Demo"), ({}, "Demo")],
        ) as start_and_authorize,
        patch.object(runtime, "_wait_for_account_database") as wait_for_database,
        patch.object(runtime, "_wait_for_discovery_start", return_value=True),
        patch.object(
            runtime, "_probe_broker_symbol", return_value="EURUSD.raw"
        ) as probe_symbol,
            patch.object(runtime, "_wait_for_heartbeat", return_value=expected),
            patch.object(runtime, "_running_terminal_pids", return_value=[]),
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
            42,
            "Demo",
            "not-a-real-secret",
            "EURUSD",
            keep_private=True,
            start_expert=False,
            script_name="TradeJournal\\TradeJournalDiscovery",
            filename="startup.ini",
        ),
    ]
    assert start_and_authorize.call_args_list == [
        call(bootstrap, 42, "Demo", ANY, "Demo"),
        call(startup, 42, "Demo", ANY),
    ]
    wait_for_database.assert_called_once_with(ANY)
    probe_symbol.assert_called_once_with("EURUSD", 42, "Demo", ANY)
    assert stop.call_count == 1
    assert not bootstrap.exists()
    assert not startup.exists()
    assert (runtime.files / "history_mode").read_text(encoding="utf-8") == "new_only"
    assert (runtime.files / "history_from_unix").read_text(encoding="utf-8") == "0"
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


def test_start_hands_discovery_chart_directly_to_bridge(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    expert = tmp_path / "bridge.ex5"
    expert.write_bytes(b"expert")
    bootstrap = runtime.state / "login-bootstrap.ini"
    startup = runtime.state / "startup.ini"
    runtime.state.mkdir()
    bootstrap.write_text("temporary")
    startup.write_text("temporary")
    with (
        patch.object(
            runtime,
            "_write_startup_config",
            side_effect=[bootstrap, startup],
        ),
        patch.object(
            runtime,
            "_start_and_wait_for_authorization",
            side_effect=[({}, "Demo"), ({}, "Demo")],
        ),
        patch.object(runtime, "_wait_for_account_database"),
        patch.object(runtime, "_wait_for_discovery_start", return_value=True),
        patch.object(runtime, "_probe_broker_symbol", return_value="EURUSD.raw"),
        patch.object(runtime, "_remove_readiness_files") as remove_readiness,
        patch.object(runtime, "_publish_bridge_handoff") as publish_handoff,
            patch.object(
                runtime,
                "_wait_for_heartbeat",
                side_effect=NativeMt5Error("terminal_not_ready"),
            ) as wait_for_heartbeat,
            patch.object(runtime, "_running_terminal_pids", return_value=[]),
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

    wait_for_heartbeat.assert_called_once_with(90.0, 42, "Demo")
    publish_handoff.assert_called_once_with()
    assert stop.call_count == 2
    assert remove_readiness.call_count == 1
    assert not bootstrap.exists()
    assert not startup.exists()


def test_start_process_uses_portable_config(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    config = runtime.state / "startup.ini"
    config.parent.mkdir()
    config.write_text("temporary")
    process = Mock(pid=123)
    # Exercise the direct-process fallback deliberately on every host.  The
    # production Windows path correctly requires the dedicated interactive
    # identity and is covered by the scheduled-task tests below.
    with (
        patch("windows_agent.worker.native_mt5_runtime.os.name", "posix"),
        patch("subprocess.Popen", return_value=process) as popen,
    ):
        assert runtime._start_process(config, 42) is process
    args = popen.call_args.args[0]
    assert "/portable" in args
    assert "/profile:TradeJournal" in args
    assert "/login:42" in args
    assert any(value.startswith("/config:") for value in args)


def test_scheduled_terminal_task_revalidates_identity_before_create_and_run(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    config = runtime.state / "startup.ini"
    config.parent.mkdir()
    config.write_text("temporary")
    completed = Mock(returncode=0)
    with (
        patch.object(
            runtime,
            "_interactive_user",
            return_value="TradeJournalMT5",
        ),
        patch.object(runtime, "_wait_for_interactive_session"),
        patch.object(runtime, "_prepare_interactive_runtime_acl"),
        patch.object(runtime, "_grant_interactive_acl"),
        patch.object(
            runtime,
            "_verify_interactive_task_identity",
        ) as identity_gate,
        patch("subprocess.run", return_value=completed) as run,
    ):
        assert runtime._start_process(config, 42) is None

    assert identity_gate.call_args_list == [
        call("TradeJournalMT5"),
        call("TradeJournalMT5"),
    ]
    assert run.call_args_list[0].args[0][:2] == ["schtasks", "/Create"]
    assert run.call_args_list[1].args[0][:2] == ["schtasks", "/Run"]


def test_scheduled_terminal_task_is_deleted_when_second_identity_gate_fails(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    config = runtime.state / "startup.ini"
    config.parent.mkdir()
    config.write_text("temporary")
    completed = Mock(returncode=0)
    with (
        patch.object(runtime, "_interactive_user", return_value="TradeJournalMT5"),
        patch.object(runtime, "_wait_for_interactive_session"),
        patch.object(runtime, "_prepare_interactive_runtime_acl"),
        patch.object(runtime, "_grant_interactive_acl"),
        patch.object(
            runtime,
            "_verify_interactive_task_identity",
            side_effect=(None, NativeMt5Error("interactive_session_token_not_standard")),
        ),
        patch("subprocess.run", return_value=completed) as run,
    ):
        with pytest.raises(
            NativeMt5Error,
            match="interactive_session_token_not_standard",
        ):
            runtime._start_process(config, 42)

    assert [item.args[0][1] for item in run.call_args_list] == [
        "/Create",
        "/End",
        "/Delete",
    ]
    assert runtime._interactive_task is None


def test_scheduled_terminal_task_is_deleted_when_run_fails(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    config = runtime.state / "startup.ini"
    config.parent.mkdir()
    config.write_text("temporary")
    with (
        patch.object(runtime, "_interactive_user", return_value="TradeJournalMT5"),
        patch.object(runtime, "_wait_for_interactive_session"),
        patch.object(runtime, "_prepare_interactive_runtime_acl"),
        patch.object(runtime, "_grant_interactive_acl"),
        patch.object(runtime, "_verify_interactive_task_identity"),
        patch(
            "subprocess.run",
            side_effect=(
                Mock(returncode=0),
                Mock(returncode=1),
                Mock(returncode=0),
                Mock(returncode=0),
            ),
        ) as run,
    ):
        with pytest.raises(NativeMt5Error, match="interactive_task_run_failed"):
            runtime._start_process(config, 42)

    assert [item.args[0][1] for item in run.call_args_list] == [
        "/Create",
        "/Run",
        "/End",
        "/Delete",
    ]
    assert runtime._interactive_task is None


def test_install_expert_rejects_unknown_history_mode(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    expert = tmp_path / "bridge.ex5"
    expert.write_bytes(b"expert")
    with pytest.raises(NativeMt5Error, match="invalid_history_mode"):
        runtime.install_expert(expert, "ten_year_snapshot")


def test_install_expert_persists_from_date_as_unix_timestamp(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    expert = tmp_path / "bridge.ex5"
    expert.write_bytes(b"expert")
    history_from = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)

    runtime.install_expert(expert, "from_date", history_from)

    assert (runtime.files / "history_mode").read_text(encoding="utf-8") == "from_date"
    assert (runtime.files / "history_from_unix").read_text(encoding="utf-8") == str(
        int(history_from.timestamp())
    )


def test_install_expert_persists_new_only_recovery_cutoff(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    expert = tmp_path / "bridge.ex5"
    expert.write_bytes(b"expert")
    recovery_from = datetime(2026, 8, 27, 21, 29, 30, tzinfo=timezone.utc)

    runtime.install_expert(expert, "new_only", recovery_from)

    assert (runtime.files / "history_mode").read_text(encoding="utf-8") == "new_only"
    assert (runtime.files / "history_from_unix").read_text(encoding="utf-8") == str(
        int(recovery_from.timestamp())
    )


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
            lambda name: "TradeJournalMT5"
            if name == "TRADEJOURNAL_MT5_INTERACTIVE_USER"
            else ""
        ),
    )
    monkeypatch.setattr(
        runtime,
        "_verify_local_standard_interactive_user",
        lambda interactive_user: None,
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
        ["icacls", str(config), "/grant", "TradeJournalMT5:(R)"],
        capture_output=True,
        text=True,
        check=False,
    )


def test_runtime_rejects_generic_operator_as_interactive_identity(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime = _runtime(tmp_path)
    monkeypatch.setattr(
        NativeMt5Runtime,
        "_setting",
        staticmethod(
            lambda name: "Alice"
            if name == "TRADEJOURNAL_MT5_INTERACTIVE_USER"
            else ""
        ),
    )

    with pytest.raises(
        NativeMt5Error,
        match="interactive_user_not_dedicated",
    ):
        runtime._interactive_user()


def test_windows_identity_gate_rejects_disabled_or_admin_local_user() -> None:
    for document in (
        '{"Name":"TradeJournalMT5","Enabled":false,'
        '"SID":"S-1-5-21-1","IsAdministrator":false,'
        '"ConsentPromptBehaviorUser":0}',
        '{"Name":"TradeJournalMT5","Enabled":true,'
        '"SID":"S-1-5-21-1","IsAdministrator":true,'
        '"ConsentPromptBehaviorUser":0}',
    ):
        completed = Mock(returncode=0, stdout=document)
        with (
            patch("subprocess.run", return_value=completed),
            pytest.raises(
                NativeMt5Error,
                match="interactive_user_not_dedicated",
            ),
        ):
            NativeMt5Runtime._verify_local_standard_interactive_user(
                "TradeJournalMT5"
            )


@pytest.mark.parametrize(
    ("elevation_type", "token_groups", "accepted"),
    (
        (1, (("users", 4),), True),
        (3, (("users", 4),), False),
        # A stale full Administrator token can report Default when UAC is
        # disabled. Deny-only membership is also forbidden, so attributes are
        # deliberately ignored by the verifier.
        (1, (("administrators", 0x10),), False),
    ),
)
def test_interactive_session_requires_a_fresh_standard_user_token(
    elevation_type: int,
    token_groups: tuple[tuple[str, int], ...],
    accepted: bool,
) -> None:
    token = Mock()
    win32ts = SimpleNamespace(
        WTSActive=0,
        WTSDisconnected=4,
        WTSUserName=5,
        WTSEnumerateSessions=Mock(
            return_value=[{"State": 0, "SessionId": 2}]
        ),
        WTSQuerySessionInformation=Mock(return_value="TradeJournalMT5"),
        WTSQueryUserToken=Mock(return_value=token),
    )
    win32security = SimpleNamespace(
        TokenGroups=2,
        TokenUser=1,
        WinBuiltinAdministratorsSid=26,
        GetTokenInformation=Mock(
            side_effect=lambda _token, information_class: (
                elevation_type
                if information_class == 18
                else token_groups
            )
        ),
        CreateWellKnownSid=Mock(return_value="administrators"),
        ConvertSidToStringSid=Mock(side_effect=str),
    )

    with (
        patch.dict(
            sys.modules,
            {"win32ts": win32ts, "win32security": win32security},
        ),
        patch(
            "windows_agent.interactive_identity.os",
            SimpleNamespace(name="nt", environ=os.environ),
        ),
    ):
        if accepted:
            assert NativeMt5Runtime._interactive_session_present(
                "TradeJournalMT5"
            )
        else:
            with pytest.raises(
                NativeMt5Error,
                match="interactive_session_token_not_standard",
            ):
                NativeMt5Runtime._interactive_session_present(
                    "TradeJournalMT5"
                )

    assert win32security.GetTokenInformation.call_args_list == [
        call(token, 18),
        call(token, 2),
    ]
    token.Close.assert_called_once_with()


def test_windows_identity_gate_accepts_uac_auto_deny_policy() -> None:
    completed = Mock(
        returncode=0,
        stdout=(
            '{"Name":"TradeJournalMT5","Enabled":true,'
            '"SID":"S-1-5-21-1","IsAdministrator":false,'
            '"ConsentPromptBehaviorUser":0}'
        ),
    )
    with patch("subprocess.run", return_value=completed) as run:
        NativeMt5Runtime._verify_local_standard_interactive_user(
            "TradeJournalMT5"
        )

    assert (
        "ConsentPromptBehaviorUser"
        in run.call_args.args[0][-1]
    )


@pytest.mark.parametrize("policy", [1, 3, 5, None, False])
def test_windows_identity_gate_rejects_non_auto_deny_uac_policy(
    policy: int | bool | None,
) -> None:
    completed = Mock(
        returncode=0,
        stdout=json.dumps(
            {
                "Name": "TradeJournalMT5",
                "Enabled": True,
                "SID": "S-1-5-21-1",
                "IsAdministrator": False,
                "ConsentPromptBehaviorUser": policy,
            }
        ),
    )
    with (
        patch("subprocess.run", return_value=completed),
        pytest.raises(
            NativeMt5Error,
            match="interactive_user_uac_policy_not_auto_deny",
        ),
    ):
        NativeMt5Runtime._verify_local_standard_interactive_user(
            "TradeJournalMT5"
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


def test_wait_for_authorization_fails_immediately_when_dialog_monitor_detects_rejection(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    monitor = _AuthFailureMonitor(
        "task",
        123,
        456,
        tmp_path / "helper.ps1",
        tmp_path / "request.json",
        tmp_path / "result.json",
        tmp_path / "launcher.cmd",
    )
    with (
        patch.object(runtime, "_authentication_failure_detected", return_value=True),
        patch.object(runtime, "_running_terminal_pids", return_value=[]),
        pytest.raises(NativeMt5Error, match="authorization_failed"),
    ):
        runtime._wait_for_authorization(
            {},
            42,
            "Demo",
            30.0,
            auth_monitor=monitor,
        )


def test_authentication_dialog_result_is_bound_to_the_exact_terminal_process(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    result = tmp_path / "result.json"
    monitor = _AuthFailureMonitor(
        "task",
        123,
        456,
        tmp_path / "helper.ps1",
        tmp_path / "request.json",
        result,
        tmp_path / "launcher.cmd",
    )
    result.write_text(json.dumps({
        "schema_version": 1,
        "success": True,
        "detected": True,
        "error_code": "authorization_failed",
        "process_id": 123,
        "creation_time_unix_ms": 456,
    }))

    assert runtime._authentication_failure_detected(monitor) is True

    document = json.loads(result.read_text())
    document["process_id"] = 999
    result.write_text(json.dumps(document))
    with pytest.raises(NativeMt5Error, match="authentication_monitor_result_invalid"):
        runtime._authentication_failure_detected(monitor)


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
    startup = runtime.state / "startup.ini"
    runtime.state.mkdir()
    bootstrap.write_text("temporary")
    startup.write_text("temporary")
    with (
        patch.object(
            runtime,
            "_write_startup_config",
            side_effect=[bootstrap, startup],
        ),
        patch.object(
            runtime,
            "_start_and_wait_for_authorization",
            side_effect=[({}, "Demo"), ({}, "Demo")],
        ),
        patch.object(runtime, "_wait_for_account_database"),
        patch.object(runtime, "_wait_for_discovery_start", return_value=True),
            patch.object(runtime, "_probe_broker_symbol", return_value="EURUSD.raw"),
            patch.object(runtime, "_remove_readiness_files"),
            patch.object(runtime, "_running_terminal_pids", return_value=[]),
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
    assert not startup.exists()


def test_crashed_process_is_reported(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    process = Mock(pid=123)
    process.poll.return_value = 1
    runtime._process = process
    with patch.object(runtime, "_running_terminal_pids", return_value=[]):
        with pytest.raises(NativeMt5Error, match="mt5_process_crashed"):
            runtime._wait_for_heartbeat(1.0, 42, "Demo")


def test_readiness_cleanup_fails_closed_when_stale_file_cannot_be_removed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    runtime.files.mkdir(parents=True)
    stale = runtime.files / "heartbeat.json"
    stale.write_text(
        _envelope({"terminal_connected": True}),
        encoding="utf-8",
    )
    original_unlink = Path.unlink

    def guarded_unlink(path: Path, *args: object, **kwargs: object) -> None:
        if path == stale:
            raise PermissionError("fixture locked file")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", guarded_unlink)

    with pytest.raises(NativeMt5Error, match="^readiness_cleanup_failed$"):
        runtime._remove_readiness_files()

    assert stale.is_file()


def test_heartbeat_from_before_current_launch_is_rejected(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    runtime.files.mkdir(parents=True)
    (runtime.files / "account.json").write_text(
        _envelope(
            {
                "login": "42",
                "server": "Demo",
                "trade_allowed": False,
            }
        ),
        encoding="utf-8",
    )
    (runtime.files / "heartbeat.json").write_text(
        _envelope({"terminal_connected": True}),
        encoding="utf-8",
    )
    runtime._readiness_not_before = datetime.now(timezone.utc)

    with (
        patch.object(runtime, "_running_terminal_pids", return_value=[123]),
        patch("windows_agent.worker.native_mt5_runtime.time.sleep"),
        pytest.raises(NativeMt5Error, match="^terminal_not_ready$"),
    ):
        runtime._wait_for_heartbeat(0.01, 42, "Demo")
