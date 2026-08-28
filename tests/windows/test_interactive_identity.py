from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

import pytest

import windows_agent.provisioning.process_manager as process_manager_module
from windows_agent.broker_wizard import BrokerWizardError, HiddenSessionBrokerWizard
from windows_agent.interactive_identity import (
    InteractiveIdentityError,
    verify_interactive_process_identity,
    verify_interactive_task_identity,
)
from windows_agent.provisioning.process_manager import ProcessManager


def _local_identity_result() -> Mock:
    return Mock(
        returncode=0,
        stdout=(
            '{"Name":"TradeJournalMT5","Enabled":true,'
            '"SID":"S-1-5-21-1","IsAdministrator":false,'
            '"ConsentPromptBehaviorUser":0}'
        ),
    )


def _wts_modules(
    *,
    elevation_type: int = 1,
    groups: tuple[tuple[str, int], ...] = (("users", 4),),
) -> tuple[SimpleNamespace, SimpleNamespace, Mock]:
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
            side_effect=lambda _token, information_class: {
                18: elevation_type,
                2: groups,
                1: ("local-user", 0),
            }[information_class]
        ),
        CreateWellKnownSid=Mock(return_value="administrators"),
        ConvertStringSidToSid=Mock(return_value="local-user"),
        EqualSid=Mock(side_effect=lambda left, right: left == right),
    )
    return win32ts, win32security, token


def test_task_gate_rejects_stale_admin_token_even_when_elevation_is_default() -> None:
    win32ts, win32security, token = _wts_modules(
        elevation_type=1,
        groups=(("administrators", 0x10),),
    )

    with (
        patch("subprocess.run", return_value=_local_identity_result()),
        patch.dict(
            sys.modules,
            {"win32ts": win32ts, "win32security": win32security},
        ),
        patch(
            "windows_agent.interactive_identity.os",
            SimpleNamespace(name="nt", environ=os.environ),
        ),
        pytest.raises(
            InteractiveIdentityError,
            match="interactive_session_token_not_standard",
        ),
    ):
        verify_interactive_task_identity("TradeJournalMT5")

    assert win32security.GetTokenInformation.call_args_list == [
        call(token, 18),
        call(token, 2),
        call(token, 1),
    ]
    token.Close.assert_called_once_with()


def test_task_gate_accepts_only_the_exact_local_standard_user_token() -> None:
    win32ts, win32security, token = _wts_modules()

    with (
        patch("subprocess.run", return_value=_local_identity_result()),
        patch.dict(
            sys.modules,
            {"win32ts": win32ts, "win32security": win32security},
        ),
        patch(
            "windows_agent.interactive_identity.os",
            SimpleNamespace(name="nt", environ=os.environ),
        ),
    ):
        verify_interactive_task_identity("TradeJournalMT5")

    assert win32security.EqualSid.call_args_list[-1] == call(
        "local-user",
        "local-user",
    )
    token.Close.assert_called_once_with()


def test_process_gate_rejects_admin_token_in_the_verified_standard_session() -> None:
    win32ts, win32security, session_token = _wts_modules()
    process_token = Mock()
    process_handle = Mock()

    def token_information(token: object, information_class: int) -> object:
        if token is session_token:
            return {
                18: 1,
                2: (("users", 4),),
                1: ("local-user", 0),
            }[information_class]
        return {
            18: 1,
            2: (("administrators", 0x10),),
            1: ("local-user", 0),
        }[information_class]

    win32security.GetTokenInformation.side_effect = token_information
    win32security.TOKEN_QUERY = 8
    win32security.OpenProcessToken = Mock(return_value=process_token)
    win32api = SimpleNamespace(OpenProcess=Mock(return_value=process_handle))
    win32process = SimpleNamespace(ProcessIdToSessionId=Mock(return_value=2))

    with (
        patch("subprocess.run", return_value=_local_identity_result()),
        patch.dict(
            sys.modules,
            {
                "win32api": win32api,
                "win32process": win32process,
                "win32ts": win32ts,
                "win32security": win32security,
            },
        ),
        patch(
            "windows_agent.interactive_identity.os",
            SimpleNamespace(name="nt", environ=os.environ),
        ),
        pytest.raises(
            InteractiveIdentityError,
            match="interactive_process_token_not_standard",
        ),
    ):
        verify_interactive_process_identity("TradeJournalMT5", 321)

    process_token.Close.assert_called_once_with()
    process_handle.Close.assert_called_once_with()


def test_process_manager_verifies_token_before_adopting_on_windows(
    tmp_path: Path,
) -> None:
    manager = ProcessManager(tmp_path / "terminal-process.json")
    terminal = tmp_path / "terminal64.exe"
    terminal.write_bytes(b"terminal")
    environment = {"TRADEJOURNAL_MT5_INTERACTIVE_USER": "TradeJournalMT5"}
    with (
        patch.object(ProcessManager, "find", return_value=[321]),
        patch.object(manager, "_save_identity", return_value=321) as save,
        patch(
            "windows_agent.provisioning.process_manager.verify_interactive_process_identity"
        ) as verify,
        patch.object(
            process_manager_module,
            "os",
            SimpleNamespace(name="nt", environ=environment),
        ),
    ):
        assert manager.adopt(terminal) == 321

    verify.assert_called_once_with("TradeJournalMT5", 321)
    save.assert_called_once_with(321, terminal, portable=True)


def test_broker_wizard_refuses_task_creation_when_identity_gate_fails(
    tmp_path: Path,
) -> None:
    root = tmp_path / "instance"
    terminal = root / "terminal" / "terminal64.exe"
    terminal.parent.mkdir(parents=True)
    terminal.write_bytes(b"terminal")
    python = tmp_path / "python.exe"
    helper = tmp_path / "broker_wizard_ui.py"
    python.write_bytes(b"python")
    helper.write_text("# helper\n", encoding="utf-8")
    wizard = HiddenSessionBrokerWizard(
        interactive_user="TradeJournalMT5",
        python_executable=python,
        helper_script=helper,
        timeout_seconds=30,
    )

    with (
        patch(
            "windows_agent.broker_wizard.os",
            SimpleNamespace(name="nt"),
        ),
        patch.object(wizard, "_grant"),
        patch(
            "windows_agent.broker_wizard.verify_interactive_task_identity",
            side_effect=InteractiveIdentityError(
                "interactive_session_token_not_standard"
            ),
        ) as identity_gate,
        patch(
            "windows_agent.broker_wizard.ProcessManager.cleanup_path",
            return_value=True,
        ),
        patch("windows_agent.broker_wizard.subprocess.run") as run,
        pytest.raises(
            BrokerWizardError,
            match="wizard interactive identity is unavailable",
        ),
    ):
        wizard(root, "FPM", "FPM Trading", "FPMTrading-Live")

    identity_gate.assert_called_once_with("TradeJournalMT5")
    assert not any(
        invocation.args
        and invocation.args[0][:2] == ["schtasks", "/Create"]
        for invocation in run.call_args_list
    )


def test_broker_wizard_accepts_only_the_dedicated_identity(tmp_path: Path) -> None:
    with pytest.raises(BrokerWizardError, match="must be dedicated"):
        HiddenSessionBrokerWizard(
            interactive_user="Alice",
            python_executable=tmp_path / "python.exe",
            helper_script=tmp_path / "helper.py",
        )
