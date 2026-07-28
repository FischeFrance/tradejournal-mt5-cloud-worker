from __future__ import annotations

"""Fail-closed production bridge to the MT5 broker census wizard.

The service process never drives another desktop directly.  It publishes a
credential-free request and starts a one-shot helper as the dedicated
TradeJournalMT5 interactive identity.  The helper may modify only the isolated
portable terminal belonging to the current connection.
"""

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping
from uuid import UUID, uuid4

from .provisioning.process_manager import ProcessManager
from .state_store import atomic_json


_LABEL = re.compile(r"^[A-Za-z0-9&'()._ /+-]{1,128}$")
_SERVER = re.compile(r"^[A-Za-z0-9._ -]{1,128}$")
_SAFE_USER = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RESULT_FIELDS = {
    "schema_version",
    "run_id",
    "status",
    "failure_reason",
    "expected_server_name",
    "selected_broker_label",
    "censused_server_names",
    "terminal_pid",
    "completed_at_unix_ms",
}
_FAILURE_REASONS = {
    "invalid_request",
    "terminal_start_failed",
    "terminal_process_ambiguous",
    "wizard_not_found",
    "broker_not_found",
    "server_not_found",
    "driver_failure",
    "timeout",
    "cleanup_failed",
}


class BrokerWizardError(RuntimeError):
    """Sanitized wizard failure; messages never contain UI text or credentials."""


def _default_python_executable() -> Path:
    """Resolve the venv interpreter from both CLI and pywin32 service hosts."""

    executable = Path(sys.executable)
    candidates = (
        executable.parent / "Scripts" / "python.exe",
        executable.with_name("python.exe"),
        Path(sys.prefix) / "Scripts" / "python.exe",
        Path(__file__).resolve().parent.parent
        / ".venv"
        / "Scripts"
        / "python.exe",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]


def _broker_key(value: str) -> str:
    return "".join(
        character for character in value.casefold() if character.isalnum()
    )


@dataclass(frozen=True)
class BrokerWizardEvidence:
    run_id: str
    expected_server_name: str
    selected_broker_label: str
    censused_server_names: tuple[str, ...]
    terminal_pid: int
    completed_at_unix_ms: int
    artifact_path: Path
    artifact_sha256: str


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise BrokerWizardError("wizard evidence cannot be read") from exc
    return digest.hexdigest()


def _uuid4(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise BrokerWizardError(f"{name} is invalid")
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError) as exc:
        raise BrokerWizardError(f"{name} is invalid") from exc
    if parsed.version != 4 or str(parsed) != value.lower():
        raise BrokerWizardError(f"{name} is invalid")
    return str(parsed)


def _is_reparse_point(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return True
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return path.is_symlink() or bool(attributes & reparse_flag)


def _read_json(path: Path) -> Mapping[str, Any]:
    if _is_reparse_point(path) or not path.is_file():
        raise BrokerWizardError("wizard evidence must be a regular file")
    try:
        if path.stat().st_size <= 0 or path.stat().st_size > 64 * 1024:
            raise BrokerWizardError("wizard evidence size is invalid")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BrokerWizardError("wizard evidence cannot be read") from exc
    if not isinstance(value, Mapping):
        raise BrokerWizardError("wizard evidence must be an object")
    return value


def load_wizard_evidence(
    path: str | Path,
    *,
    expected_run_id: str,
    expected_server_name: str,
) -> BrokerWizardEvidence:
    artifact = Path(path)
    document = _read_json(artifact)
    if set(document) != _RESULT_FIELDS:
        raise BrokerWizardError("wizard evidence fields do not match schema v1")
    if document["schema_version"] != 1:
        raise BrokerWizardError("wizard evidence schema is unsupported")
    run_id = _uuid4(document["run_id"], "wizard run id")
    if run_id != _uuid4(expected_run_id, "expected wizard run id"):
        raise BrokerWizardError("wizard run id mismatch")
    if document["status"] != "SUCCESS":
        reason = document["failure_reason"]
        if reason not in _FAILURE_REASONS:
            raise BrokerWizardError("wizard failure reason is invalid")
        raise BrokerWizardError("wizard did not succeed")
    if document["failure_reason"] is not None:
        raise BrokerWizardError("wizard success contains a failure reason")

    expected_server = expected_server_name.strip()
    if not _SERVER.fullmatch(expected_server):
        raise BrokerWizardError("expected server is invalid")
    if document["expected_server_name"] != expected_server:
        raise BrokerWizardError("wizard expected server mismatch")
    broker = document["selected_broker_label"]
    if not isinstance(broker, str) or not _LABEL.fullmatch(broker):
        raise BrokerWizardError("wizard broker label is invalid")
    raw_servers = document["censused_server_names"]
    if (
        not isinstance(raw_servers, list)
        or not raw_servers
        or any(
            not isinstance(server, str) or not _SERVER.fullmatch(server)
            for server in raw_servers
        )
    ):
        raise BrokerWizardError("wizard server list is invalid")
    normalized_servers = tuple(
        sorted(
            {server.strip() for server in raw_servers},
            key=str.casefold,
        )
    )
    if not any(
        server.casefold() == expected_server.casefold()
        for server in normalized_servers
    ):
        raise BrokerWizardError("wizard expected server was not censused")
    terminal_pid = document["terminal_pid"]
    if (
        not isinstance(terminal_pid, int)
        or isinstance(terminal_pid, bool)
        or terminal_pid <= 0
    ):
        raise BrokerWizardError("wizard terminal PID is invalid")
    completed_at = document["completed_at_unix_ms"]
    if (
        not isinstance(completed_at, int)
        or isinstance(completed_at, bool)
        or completed_at <= 0
    ):
        raise BrokerWizardError("wizard completion time is invalid")
    digest = _sha256(artifact)
    if not _SHA256.fullmatch(digest):
        raise BrokerWizardError("wizard evidence digest is invalid")
    return BrokerWizardEvidence(
        run_id=run_id,
        expected_server_name=expected_server,
        selected_broker_label=broker.strip(),
        censused_server_names=normalized_servers,
        terminal_pid=terminal_pid,
        completed_at_unix_ms=completed_at,
        artifact_path=artifact,
        artifact_sha256=digest,
    )


def write_login_verification_artifact(
    state_root: str | Path,
    *,
    evidence: BrokerWizardEvidence,
    verification_pid: int,
    verified_at_unix_ms: int | None = None,
) -> tuple[Path, str]:
    if (
        not isinstance(verification_pid, int)
        or isinstance(verification_pid, bool)
        or verification_pid <= 0
    ):
        raise BrokerWizardError("login verification PID is invalid")
    run_id = _uuid4(evidence.run_id, "wizard run id")
    if not _SHA256.fullmatch(evidence.artifact_sha256):
        raise BrokerWizardError("wizard evidence digest is invalid")
    observed = (
        int(time.time() * 1000)
        if verified_at_unix_ms is None
        else verified_at_unix_ms
    )
    if not isinstance(observed, int) or isinstance(observed, bool) or observed <= 0:
        raise BrokerWizardError("login verification time is invalid")
    destination = Path(state_root) / f"endpoint-verification-{run_id}.json"
    atomic_json(
        destination,
        {
            "schema_version": 1,
            "verification_session_id": run_id,
            "server_name": evidence.expected_server_name,
            "broker_label": evidence.selected_broker_label,
            "protocol": "TCP/TLS",
            "verification_method": "managed_investor_login",
            "login_verified": True,
            "investor_read_only_verified": True,
            "verification_pid": verification_pid,
            "verified_at_unix_ms": observed,
            "wizard_artifact_sha256": evidence.artifact_sha256,
        },
    )
    digest = _sha256(destination)
    return destination, digest


class HiddenSessionBrokerWizard:
    """Run the credential-free wizard helper inside the dedicated desktop."""

    def __init__(
        self,
        *,
        interactive_user: str,
        python_executable: str | Path | None = None,
        helper_script: str | Path | None = None,
        timeout_seconds: int = 180,
    ) -> None:
        if not _SAFE_USER.fullmatch(interactive_user):
            raise BrokerWizardError("interactive user is invalid")
        if interactive_user.casefold() in {
            "administrator",
            "system",
            "localsystem",
            "localservice",
            "networkservice",
        }:
            raise BrokerWizardError("interactive user must be dedicated")
        if not 30 <= timeout_seconds <= 300:
            raise BrokerWizardError("wizard timeout is invalid")
        self._python = Path(
            python_executable or _default_python_executable()
        )
        self._helper = Path(
            helper_script or Path(__file__).with_name("broker_wizard_ui.py")
        )
        self._interactive_user = interactive_user
        self._timeout = timeout_seconds

    @staticmethod
    def _grant(path: Path, user: str, access: str) -> None:
        completed = subprocess.run(
            ["icacls", str(path), "/grant:r", f"{user}:{access}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise BrokerWizardError("wizard ACL preparation failed")

    @staticmethod
    def _delete_task(task_name: str) -> None:
        completed = subprocess.run(
            ["schtasks", "/Delete", "/TN", task_name, "/F"],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise BrokerWizardError("wizard task cleanup failed")

    def __call__(
        self,
        instance_root: Path,
        search_text: str,
        suggested_broker_label: str,
        expected_server_name: str,
        cancel_check: Callable[[], None] | None = None,
    ) -> BrokerWizardEvidence:
        if os.name != "nt":
            raise BrokerWizardError("broker wizard requires Windows")
        root = Path(instance_root).resolve()
        terminal_root = root / "terminal"
        terminal = terminal_root / "terminal64.exe"
        state = root / "state"
        if (
            _is_reparse_point(root)
            or not terminal.is_file()
            or _is_reparse_point(terminal)
            or not self._python.is_file()
            or not self._helper.is_file()
        ):
            raise BrokerWizardError("wizard runtime is unavailable")
        search = search_text.strip()
        suggested = suggested_broker_label.strip()
        expected = expected_server_name.strip()
        if (
            not _LABEL.fullmatch(search)
            or not _LABEL.fullmatch(suggested)
            or not _SERVER.fullmatch(expected)
        ):
            raise BrokerWizardError("wizard request is invalid")

        run_id = str(uuid4())
        work = state / "broker-wizard" / run_id
        request_path = work / "request.json"
        result_path = work / "result.json"
        launcher = work / "run-wizard.cmd"
        task_name = f"TradeJournalBrokerWizard-{run_id}"
        task_created = False
        work.mkdir(parents=True, exist_ok=False)
        atomic_json(
            request_path,
            {
                "schema_version": 1,
                "run_id": run_id,
                "terminal_path": str(terminal),
                "expected_server_name": expected,
                "suggested_broker_label": suggested,
                "search_terms": list(
                    dict.fromkeys((search, suggested, expected))
                ),
                "timeout_seconds": self._timeout,
            },
        )
        command = subprocess.list2cmdline(
            [
                str(self._python),
                str(self._helper),
                str(request_path),
                str(result_path),
            ]
        )
        launcher.write_text(
            f"@echo off\r\n{command}\r\nexit /b %ERRORLEVEL%\r\n",
            encoding="utf-8",
        )
        try:
            self._grant(root, self._interactive_user, "(RX)")
            self._grant(state, self._interactive_user, "(RX)")
            self._grant(terminal_root, self._interactive_user, "(OI)(CI)(M)")
            self._grant(work, self._interactive_user, "(OI)(CI)(M)")
            self._grant(self._helper, self._interactive_user, "(RX)")
            self._grant(
                self._python.parent.parent,
                self._interactive_user,
                "(OI)(CI)(RX)",
            )
            create = subprocess.run(
                [
                    "schtasks",
                    "/Create",
                    "/TN",
                    task_name,
                    "/SC",
                    "ONCE",
                    "/ST",
                    "23:59",
                    "/RU",
                    self._interactive_user,
                    "/IT",
                    "/RL",
                    "LIMITED",
                    "/TR",
                    str(launcher),
                    "/F",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if create.returncode != 0:
                raise BrokerWizardError("wizard task creation failed")
            task_created = True
            start = subprocess.run(
                ["schtasks", "/Run", "/TN", task_name],
                capture_output=True,
                text=True,
                check=False,
            )
            if start.returncode != 0:
                raise BrokerWizardError("wizard task start failed")

            deadline = time.monotonic() + self._timeout + 30
            while time.monotonic() < deadline:
                if cancel_check is not None:
                    cancel_check()
                if result_path.is_file():
                    evidence = load_wizard_evidence(
                        result_path,
                        expected_run_id=run_id,
                        expected_server_name=expected,
                    )
                    if _broker_key(evidence.selected_broker_label) != _broker_key(
                        suggested
                    ):
                        raise BrokerWizardError("wizard broker identity mismatch")
                    return evidence
                time.sleep(0.25)
            raise BrokerWizardError("wizard timed out")
        finally:
            cleanup_error: BrokerWizardError | None = None
            if task_created:
                subprocess.run(
                    ["schtasks", "/End", "/TN", task_name],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                try:
                    self._delete_task(task_name)
                except BrokerWizardError as exc:
                    cleanup_error = exc
            try:
                if not ProcessManager.cleanup_path(terminal):
                    cleanup_error = BrokerWizardError(
                        "wizard terminal cleanup failed"
                    )
            except Exception:
                cleanup_error = BrokerWizardError(
                    "wizard terminal cleanup failed"
                )
            request_path.unlink(missing_ok=True)
            launcher.unlink(missing_ok=True)
            if cleanup_error is not None:
                raise cleanup_error
