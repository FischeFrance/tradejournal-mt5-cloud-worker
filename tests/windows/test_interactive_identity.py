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
        ConvertSidToStringSid=Mock(side_effect=str),
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

    assert win32security.ConvertSidToStringSid.call_args_list[-2:] == [
        call("local-user"),
        call("local-user"),
    ]
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
    win32ts.ProcessIdToSessionId = Mock(return_value=2)

    with (
        patch("subprocess.run", return_value=_local_identity_result()),
        patch.dict(
            sys.modules,
            {
                "win32api": win32api,
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






