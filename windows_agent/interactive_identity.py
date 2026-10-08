"""Fail-closed identity checks for interactive MT5 scheduled tasks.

Windows applies ``/RL LIMITED`` to the token of the user session that already
exists.  It does not turn a stale, linked Administrator session into a true
standard-user session.  Every task launcher therefore uses this module to
validate both the local account and the exact WTS token before creating or
running a task.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from typing import Any


DEDICATED_INTERACTIVE_USER = "TradeJournalMT5"


class InteractiveIdentityError(RuntimeError):
    """Sanitized identity failure shared by all interactive launchers."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class LocalInteractiveIdentity:
    name: str
    sid: str


@dataclass(frozen=True)
class VerifiedInteractiveSession:
    name: str
    sid: str
    session_id: int


def _windows_sids_equal(win32security: Any, left: Any, right: Any) -> bool:
    """Compare canonical SID strings using the API exposed by pywin32 311."""

    left_value = win32security.ConvertSidToStringSid(left)
    right_value = win32security.ConvertSidToStringSid(right)
    if not isinstance(left_value, str) or not isinstance(right_value, str):
        raise TypeError("Windows SID conversion failed")
    return left_value.casefold() == right_value.casefold()


def verify_local_standard_interactive_user(
    interactive_user: str,
) -> LocalInteractiveIdentity:
    """Require the one enabled, non-Administrator local MT5 identity."""

    if interactive_user.casefold() != DEDICATED_INTERACTIVE_USER.casefold():
        raise InteractiveIdentityError("interactive_user_not_dedicated")
    script = (
        "$u=Get-LocalUser -Name $env:TRADEJOURNAL_IDENTITY_USER "
        "-ErrorAction Stop;"
        "$g=Get-LocalGroup -SID 'S-1-5-32-544' -ErrorAction Stop;"
        "$a=@(Get-LocalGroupMember -Group $g.Name -ErrorAction Stop|"
        "Where-Object {[string]$_.SID -eq [string]$u.SID}).Count -gt 0;"
        "$p=Get-ItemProperty -LiteralPath "
        "'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Policies\\System' "
        "-Name ConsentPromptBehaviorUser -ErrorAction SilentlyContinue;"
        "$c=$null;if($null -ne $p){$c=$p.ConsentPromptBehaviorUser};"
        "[pscustomobject]@{Name=[string]$u.Name;Enabled=[bool]$u.Enabled;"
        "SID=[string]$u.SID;IsAdministrator=[bool]$a;"
        "ConsentPromptBehaviorUser=$c}|"
        "ConvertTo-Json -Compress"
    )
    environment = os.environ.copy()
    environment["TRADEJOURNAL_IDENTITY_USER"] = interactive_user
    try:
        completed = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                script,
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=environment,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise InteractiveIdentityError(
            "interactive_user_not_dedicated"
        ) from exc
    try:
        identity = json.loads(completed.stdout.strip())
    except (AttributeError, json.JSONDecodeError) as exc:
        raise InteractiveIdentityError(
            "interactive_user_not_dedicated"
        ) from exc
    if (
        completed.returncode != 0
        or not isinstance(identity, dict)
        or str(identity.get("Name", "")).casefold()
        != DEDICATED_INTERACTIVE_USER.casefold()
        or identity.get("Enabled") is not True
        or identity.get("IsAdministrator") is not False
        or not str(identity.get("SID", "")).startswith("S-1-5-21-")
    ):
        raise InteractiveIdentityError("interactive_user_not_dedicated")
    consent_policy = identity.get("ConsentPromptBehaviorUser")
    if type(consent_policy) is not int or consent_policy != 0:
        raise InteractiveIdentityError(
            "interactive_user_uac_policy_not_auto_deny"
        )
    return LocalInteractiveIdentity(
        name=DEDICATED_INTERACTIVE_USER,
        sid=str(identity["SID"]),
    )


def _token_contains_sid(
    win32security: Any,
    groups: Any,
    expected_sid: Any,
) -> bool:
    if not isinstance(groups, (list, tuple)):
        raise TypeError("token groups are invalid")
    for group in groups:
        if not isinstance(group, (list, tuple)) or len(group) < 1:
            raise TypeError("token group is invalid")
        # Inspect every group regardless of attributes. In particular,
        # SE_GROUP_USE_FOR_DENY_ONLY still proves this is a linked admin token.
        if _windows_sids_equal(win32security, group[0], expected_sid):
            return True
    return False


def _verified_interactive_session_id(
    interactive_user: str,
    *,
    expected_user_sid: str | None = None,
) -> int | None:
    """Validate the sole logged-on WTS token selected by ``/IT`` tasks."""

    if os.name != "nt":
        return None
    try:
        import win32security
        import win32ts
    except ImportError as exc:
        raise InteractiveIdentityError(
            "interactive_session_probe_unavailable"
        ) from exc
    expected_name = interactive_user.casefold()
    try:
        sessions = win32ts.WTSEnumerateSessions(None, 1, 0)
        matching_sessions = []
        for session in sessions:
            if session.get("State") not in (
                win32ts.WTSActive,
                win32ts.WTSDisconnected,
            ):
                continue
            observed = win32ts.WTSQuerySessionInformation(
                None,
                int(session["SessionId"]),
                win32ts.WTSUserName,
            )
            if isinstance(observed, str) and observed.casefold() == expected_name:
                matching_sessions.append(int(session["SessionId"]))
        if len(matching_sessions) > 1:
            raise InteractiveIdentityError("interactive_session_ambiguous")
        if not matching_sessions:
            return None

        token = win32ts.WTSQueryUserToken(matching_sessions[0])
        try:
            # TokenElevationTypeDefault (1) has no linked elevated token.
            elevation_type = win32security.GetTokenInformation(token, 18)
            token_groups = win32security.GetTokenInformation(
                token,
                win32security.TokenGroups,
            )
            administrators_sid = win32security.CreateWellKnownSid(
                win32security.WinBuiltinAdministratorsSid,
                None,
            )
            token_user = None
            if expected_user_sid is not None:
                token_user = win32security.GetTokenInformation(
                    token,
                    win32security.TokenUser,
                )[0]
                expected_sid = win32security.ConvertStringSidToSid(
                    expected_user_sid
                )
            if (
                int(elevation_type) != 1
                or _token_contains_sid(
                    win32security,
                    token_groups,
                    administrators_sid,
                )
                or (
                    expected_user_sid is not None
                    and not _windows_sids_equal(
                        win32security,
                        token_user,
                        expected_sid,
                    )
                )
            ):
                raise InteractiveIdentityError(
                    "interactive_session_token_not_standard"
                )
        finally:
            token.Close()
        return matching_sessions[0]
    except InteractiveIdentityError:
        raise
    except (IndexError, KeyError, OSError, TypeError, ValueError) as exc:
        raise InteractiveIdentityError(
            "interactive_session_probe_failed"
        ) from exc


def verified_interactive_session_present(
    interactive_user: str,
    *,
    expected_user_sid: str | None = None,
) -> bool:
    return _verified_interactive_session_id(
        interactive_user,
        expected_user_sid=expected_user_sid,
    ) is not None


def verify_interactive_task_identity(
    interactive_user: str,
) -> VerifiedInteractiveSession:
    """Gate one scheduled-task create/run operation without cached state."""

    identity = verify_local_standard_interactive_user(interactive_user)
    session_id = _verified_interactive_session_id(
        identity.name,
        expected_user_sid=identity.sid,
    )
    if session_id is None:
        raise InteractiveIdentityError("interactive_session_unavailable")
    return VerifiedInteractiveSession(
        name=identity.name,
        sid=identity.sid,
        session_id=session_id,
    )


def verify_interactive_process_identity(
    interactive_user: str,
    pid: int,
) -> None:
    """Bind an adopted MT5 process to the exact fresh standard-user token."""

    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise InteractiveIdentityError("interactive_process_identity_invalid")
    session = verify_interactive_task_identity(interactive_user)
    try:
        import win32api
        import win32security
        import win32ts

        if int(win32ts.ProcessIdToSessionId(pid)) != session.session_id:
            raise InteractiveIdentityError(
                "interactive_process_session_mismatch"
            )
        process = win32api.OpenProcess(0x1000, False, pid)
        try:
            token = win32security.OpenProcessToken(
                process,
                win32security.TOKEN_QUERY,
            )
            try:
                token_user = win32security.GetTokenInformation(
                    token,
                    win32security.TokenUser,
                )[0]
                token_groups = win32security.GetTokenInformation(
                    token,
                    win32security.TokenGroups,
                )
                elevation_type = win32security.GetTokenInformation(token, 18)
                expected_sid = win32security.ConvertStringSidToSid(session.sid)
                administrators_sid = win32security.CreateWellKnownSid(
                    win32security.WinBuiltinAdministratorsSid,
                    None,
                )
                if (
                    not _windows_sids_equal(
                        win32security,
                        token_user,
                        expected_sid,
                    )
                    or int(elevation_type) != 1
                    or _token_contains_sid(
                        win32security,
                        token_groups,
                        administrators_sid,
                    )
                ):
                    raise InteractiveIdentityError(
                        "interactive_process_token_not_standard"
                    )
            finally:
                token.Close()
        finally:
            process.Close()
    except InteractiveIdentityError:
        raise
    except Exception as exc:
        raise InteractiveIdentityError(
            "interactive_process_identity_probe_failed"
        ) from exc
