"""LocalSystem-only, forward-only deployment guard for the Windows Agent.

The deployment PowerShell script is intentionally not trusted to edit the MT5
golden template or to decide that a newly started service is healthy.  It drops
one small, ACL-protected request and invokes this module once as LocalSystem.
Every response is bound to a deployment UUID and a fresh nonce.

The activation barrier is the point of no return.  Before it exists an exact
snapshot can be restored without an interactive MT5 session.  Afterwards the
only safe direction is to start/recover the new release and converge the fleet.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping
from uuid import UUID

from worker.atomic_file import durable_replace, fsync_directory

from .interactive_identity import (
    _windows_sids_equal,
    InteractiveIdentityError,
    verify_interactive_process_identity,
    verify_interactive_task_identity,
)
from .mt5_lifecycle import Mt5LifecycleCoordinator
from .mt5_maintenance import Mt5MaintenanceCoordinator
from .provisioning.mt5_instance_pool import Mt5InstancePool
from .provisioning.mt5_instance import InstanceProvisioner
from .provisioning.mt5_instance_rotation import (
    Mt5InstanceRotator,
    Mt5TemplateRelease,
)
from .provisioning.mt5_public_release import (
    Mt5ProvisionedReleaseInventory,
    Mt5PublicReleaseProbe,
)
from .provisioning.mt5_template import Mt5TemplateManager
from .provisioning.mt5_update_store import Mt5PendingUpdateStore
from .provisioning.secret_store import WindowsSecretStore
from .release_manifest import verify_release
from .runtime_config import (
    DEDICATED_MT5_INTERACTIVE_USER,
    AgentRuntimeConfig,
    load_runtime_config,
)
from .security import canonical_uuid
from .state_store import atomic_json, read_json


SCHEMA_VERSION = 1
SERVICE_NAME = "TradeJournalMT5Agent"
FPM_SERVER_NAME = "FPMTrading-Live"
MAX_REQUEST_BYTES = 256 * 1024
MAX_HEARTBEAT_AGE_SECONDS = 30
REQUIRED_MAINTENANCE_TIMEZONE = "Europe/Rome"
REQUIRED_MAINTENANCE_HOUR = 23
REQUIRED_MAINTENANCE_MINUTE = 30
REQUIRED_MAINTENANCE_GRACE_MINUTES = 120

TRADEJOURNAL_ROOT = Path(r"C:\TradeJournal")
RELEASE_ROOT = TRADEJOURNAL_ROOT / "releases"
CURRENT_PATH = TRADEJOURNAL_ROOT / "current"
GOLDEN_ROOT = TRADEJOURNAL_ROOT / "mt5-template"
GOLDEN_EXPERT_PATH = (
    GOLDEN_ROOT / "MQL5" / "Experts" / "TradeJournal" / "TradeJournalBridge.ex5"
)
GOLDEN_MARKER_PATH = GOLDEN_ROOT / ".tradejournal-vendor-update.json"
ARTIFACT_EXPERT_PATH = (
    TRADEJOURNAL_ROOT / "artifacts" / "mql5" / "TradeJournalBridge.ex5"
)
STATE_ROOT = TRADEJOURNAL_ROOT / "state" / "deploy-guard"
REQUEST_PATH = TRADEJOURNAL_ROOT / "state" / "deploy-guard-request.json"
RESULT_PATH = TRADEJOURNAL_ROOT / "state" / "deploy-guard-result.json"
AGENT_READINESS_PATH = TRADEJOURNAL_ROOT / "state" / "agent-readiness.json"
# Public name consumed by the Windows service.  Keep the shorter alias for
# compatibility with early deployment-script drafts, but use this canonical
# name at the service boundary.
SERVICE_READINESS_PATH = AGENT_READINESS_PATH
SERVICE_REGISTRY_PATH = r"SYSTEM\CurrentControlSet\Services\TradeJournalMT5Agent"

_SHA256 = re.compile(r"[0-9a-f]{64}")
_REVISION = re.compile(r"[0-9a-f]{40}")
_ACTIONS = frozenset(
    {
        "preflight",
        "snapshot",
        "switch",
        "arm",
        "barrier",
        "barrier_status",
        "converge",
        "restore",
        "verify_active",
    }
)
_COMMON_FIELDS = frozenset(
    {
        "schema_version",
        "action",
        "nonce",
        "deployment_id",
        "source_revision",
        "payload",
    }
)
_PAYLOAD_FIELDS = {
    "preflight": frozenset(
        {
            "release_path",
            "new_expert_path",
            "next_environment",
            "fpm_connection_id",
        }
    ),
    "snapshot": frozenset(),
    "switch": frozenset(
        {
            "release_path",
            "new_expert_path",
            "next_environment",
            "previous_expert_sha256",
            "new_expert_sha256",
        }
    ),
    "arm": frozenset(),
    "barrier": frozenset(),
    "barrier_status": frozenset(),
    "converge": frozenset({"fpm_connection_id"}),
    "restore": frozenset(),
    "verify_active": frozenset(
        {"fpm_connection_id", "max_heartbeat_age_seconds"}
    ),
}


class DeployGuardError(RuntimeError):
    """A stable error code which never contains paths, credentials or payloads."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _uuid4(value: object, code: str) -> str:
    if not isinstance(value, str):
        raise DeployGuardError(code)
    try:
        parsed = UUID(value)
    except (AttributeError, TypeError, ValueError) as exc:
        raise DeployGuardError(code) from exc
    if parsed.version != 4 or str(parsed) != value:
        raise DeployGuardError(code)
    return value


def _digest(value: object, code: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise DeployGuardError(code)
    return value


def _path_text(path: Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _require_exact_path(value: object, expected: Path, code: str) -> Path:
    if not isinstance(value, str) or _path_text(Path(value)) != _path_text(expected):
        raise DeployGuardError(code)
    return expected


def _deployment_root(deployment_id: str) -> Path:
    return STATE_ROOT / deployment_id


def _record_path(deployment_id: str, name: str) -> Path:
    return _deployment_root(deployment_id) / f"{name}.json"


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path, code: str) -> str:
    try:
        if InstanceProvisioner._is_reparse_point(path) or not path.is_file():
            raise OSError
        return InstanceProvisioner._sha256(path)
    except (OSError, ValueError) as exc:
        raise DeployGuardError(code) from exc


def _json_bytes(document: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _restrict_shared(path: Path) -> None:
    if os.name == "nt":
        WindowsSecretStore.restrict_shared_service_acl(path)


def _ensure_shared_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _restrict_shared(path)


def _atomic_bytes(path: Path, payload: bytes) -> None:
    _ensure_shared_directory(path.parent)
    temporary = path.parent / f".{path.name}.{os.getpid()}.tmp"
    if temporary.exists():
        raise DeployGuardError("deployment_state_collision")
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        _restrict_shared(temporary)
        durable_replace(temporary, path)
        _restrict_shared(path)
        fsync_directory(path.parent)
    except DeployGuardError:
        raise
    except OSError as exc:
        raise DeployGuardError("deployment_state_write_failed") from exc
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _exclusive_json(
    path: Path,
    document: Mapping[str, Any],
    *,
    system_only: bool = False,
) -> None:
    _ensure_shared_directory(path.parent)
    payload = _json_bytes(document)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name == "nt" and system_only:
            WindowsSecretStore.restrict_acl(path)
        else:
            _restrict_shared(path)
        fsync_directory(path.parent)
    except FileExistsError:
        raise
    except OSError as exc:
        raise DeployGuardError("deployment_state_write_failed") from exc


def _read_json_exact(path: Path, fields: frozenset[str], code: str) -> dict[str, Any]:
    try:
        if (
            InstanceProvisioner._is_reparse_point(path)
            or not path.is_file()
            or path.stat().st_size > MAX_REQUEST_BYTES
        ):
            raise OSError
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DeployGuardError(code) from exc
    if not isinstance(value, dict) or set(value) != fields:
        raise DeployGuardError(code)
    return value


def _assert_local_system() -> None:
    if os.name != "nt":
        raise DeployGuardError("local_system_required")
    try:
        import win32api
        import win32con
        import win32security

        token = win32security.OpenProcessToken(
            win32api.GetCurrentProcess(), win32con.TOKEN_QUERY
        )
        try:
            observed = win32security.GetTokenInformation(
                token, win32security.TokenUser
            )[0]
        finally:
            token.Close()
        expected = win32security.CreateWellKnownSid(
            win32security.WinLocalSystemSid, None
        )
        if not _windows_sids_equal(win32security, observed, expected):
            raise DeployGuardError("local_system_required")
    except DeployGuardError:
        raise
    except Exception as exc:
        raise DeployGuardError("local_system_probe_failed") from exc


def _assert_request_acl(path: Path) -> None:
    """Reject a request writable by a principal other than SYSTEM/admins."""

    if os.name != "nt":
        return
    try:
        import win32security

        descriptor = win32security.GetNamedSecurityInfo(
            str(path),
            win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION,
        )
        dacl = descriptor.GetSecurityDescriptorDacl()
        if dacl is None:
            raise DeployGuardError("request_acl_invalid")
        allowed_sids = (
            win32security.CreateWellKnownSid(
                win32security.WinLocalSystemSid, None
            ),
            win32security.CreateWellKnownSid(
                win32security.WinBuiltinAdministratorsSid, None
            ),
        )
        observed_allowed: list[Any] = []
        for index in range(dacl.GetAceCount()):
            ace = dacl.GetAce(index)
            ace_type = int(ace[0][0])
            sid = ace[2]
            if ace_type in (
                win32security.ACCESS_ALLOWED_ACE_TYPE,
                win32security.ACCESS_ALLOWED_OBJECT_ACE_TYPE,
            ):
                if not any(
                    _windows_sids_equal(win32security, sid, item)
                    for item in allowed_sids
                ):
                    raise DeployGuardError("request_acl_invalid")
                observed_allowed.append(sid)
        if not all(
            any(
                _windows_sids_equal(win32security, sid, expected)
                for sid in observed_allowed
            )
            for expected in allowed_sids
        ):
            raise DeployGuardError("request_acl_invalid")
    except DeployGuardError:
        raise
    except Exception as exc:
        raise DeployGuardError("request_acl_probe_failed") from exc


def _load_request() -> dict[str, Any]:
    _assert_request_acl(REQUEST_PATH)
    value = _read_json_exact(REQUEST_PATH, _COMMON_FIELDS, "request_invalid")
    if value.get("schema_version") != SCHEMA_VERSION:
        raise DeployGuardError("request_invalid")
    action = value.get("action")
    if not isinstance(action, str) or action not in _ACTIONS:
        raise DeployGuardError("request_invalid")
    value["nonce"] = _uuid4(value.get("nonce"), "request_nonce_invalid")
    value["deployment_id"] = _uuid4(
        value.get("deployment_id"), "deployment_id_invalid"
    )
    revision = value.get("source_revision")
    if not isinstance(revision, str) or _REVISION.fullmatch(revision) is None:
        raise DeployGuardError("source_revision_invalid")
    payload = value.get("payload")
    if not isinstance(payload, dict) or set(payload) != _PAYLOAD_FIELDS[action]:
        raise DeployGuardError("request_payload_invalid")
    return value


def _environment_map(value: object) -> tuple[list[str], dict[str, str]]:
    if (
        not isinstance(value, list)
        or len(value) > 128
        or not all(isinstance(item, str) and 0 < len(item) <= 8192 for item in value)
    ):
        raise DeployGuardError("service_environment_invalid")
    result: dict[str, str] = {}
    canonical_names: set[str] = set()
    for entry in value:
        separator = entry.find("=")
        if separator <= 0 or "\x00" in entry:
            raise DeployGuardError("service_environment_invalid")
        name = entry[:separator]
        folded = name.casefold()
        if folded in canonical_names:
            raise DeployGuardError("service_environment_invalid")
        canonical_names.add(folded)
        result[name] = entry[separator + 1 :]
    return list(value), result


def _effective_environment(overrides: Mapping[str, str]) -> dict[str, str]:
    """Merge Machine/process environment with service REG_MULTI_SZ overrides.

    Windows service environment entries override the inherited Machine block
    case-insensitively.  Only the override list itself is snapshotted/restored;
    inherited values are neither persisted nor returned by this module.
    """

    effective = dict(os.environ)
    names = {name.casefold(): name for name in effective}
    for name, value in overrides.items():
        previous = names.get(name.casefold())
        if previous is not None and previous != name:
            effective.pop(previous, None)
        effective[name] = value
        names[name.casefold()] = name
    return effective


def _get_service_environment() -> list[str]:
    if os.name != "nt":
        raise DeployGuardError("service_registry_unavailable")
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            SERVICE_REGISTRY_PATH,
            0,
            winreg.KEY_READ,
        ) as key:
            value, kind = winreg.QueryValueEx(key, "Environment")
        if kind != winreg.REG_MULTI_SZ:
            raise DeployGuardError("service_environment_invalid")
        entries, _ = _environment_map(value)
        return entries
    except DeployGuardError:
        raise
    except Exception as exc:
        raise DeployGuardError("service_registry_unavailable") from exc


def _set_service_environment(entries: list[str]) -> None:
    values, _ = _environment_map(entries)
    if os.name != "nt":
        raise DeployGuardError("service_registry_unavailable")
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            SERVICE_REGISTRY_PATH,
            0,
            winreg.KEY_SET_VALUE,
        ) as key:
            winreg.SetValueEx(key, "Environment", 0, winreg.REG_MULTI_SZ, values)
    except DeployGuardError:
        raise
    except Exception as exc:
        raise DeployGuardError("service_registry_write_failed") from exc


def _current_target() -> Path:
    try:
        if not CURRENT_PATH.exists() or not InstanceProvisioner._is_reparse_point(
            CURRENT_PATH
        ):
            raise OSError
        return CURRENT_PATH.resolve(strict=True)
    except OSError as exc:
        raise DeployGuardError("current_release_invalid") from exc


def _current_target_optional() -> Path | None:
    if not CURRENT_PATH.exists() and not CURRENT_PATH.is_symlink():
        return None
    return _current_target()


def _remove_current_target() -> None:
    try:
        if not CURRENT_PATH.exists() and not CURRENT_PATH.is_symlink():
            return
        if not InstanceProvisioner._is_reparse_point(CURRENT_PATH):
            raise OSError
        if os.name == "nt":
            os.rmdir(CURRENT_PATH)
        else:
            CURRENT_PATH.unlink()
        fsync_directory(CURRENT_PATH.parent)
    except OSError as exc:
        raise DeployGuardError("current_release_switch_failed") from exc


def _replace_current_target(target: Path) -> None:
    try:
        if not target.is_dir() or InstanceProvisioner._is_reparse_point(target):
            raise OSError
        _remove_current_target()
        if os.name == "nt":
            completed = subprocess.run(
                ["cmd.exe", "/d", "/c", "mklink", "/J", str(CURRENT_PATH), str(target)],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if completed.returncode != 0:
                raise OSError
        else:
            CURRENT_PATH.symlink_to(target, target_is_directory=True)
        if _path_text(_current_target()) != _path_text(target):
            raise OSError
        fsync_directory(CURRENT_PATH.parent)
    except DeployGuardError:
        raise
    except (OSError, subprocess.SubprocessError) as exc:
        raise DeployGuardError("current_release_switch_failed") from exc


def _service_status() -> tuple[str, int]:
    try:
        import psutil

        record = psutil.win_service_get(SERVICE_NAME).as_dict()
        status = str(record.get("status", "")).casefold()
        pid = record.get("pid") or 0
        if not isinstance(pid, int) or isinstance(pid, bool) or pid < 0:
            raise ValueError
        return status, pid
    except Exception as exc:
        raise DeployGuardError("service_status_probe_failed") from exc


def _assert_service_stopped() -> None:
    status, pid = _service_status()
    if status != "stopped" or pid != 0:
        raise DeployGuardError("service_must_be_stopped")


def _release_path(revision: str, raw: object) -> Path:
    expected = RELEASE_ROOT / f"agent-{revision[:12]}"
    release = _require_exact_path(raw, expected, "release_path_invalid")
    try:
        document = verify_release(release)
    except Exception as exc:
        raise DeployGuardError("release_verification_failed") from exc
    if document.get("source_revision") != revision:
        raise DeployGuardError("release_revision_mismatch")
    return release


def _validated_next_config(
    request: Mapping[str, Any], payload: Mapping[str, Any]
) -> tuple[list[str], dict[str, str], AgentRuntimeConfig, Path]:
    release = _release_path(request["source_revision"], payload.get("release_path"))
    entries, overrides = _environment_map(payload.get("next_environment"))
    environment = _effective_environment(overrides)
    required = {
        "TRADEJOURNAL_AGENT_RELEASE_REVISION": request["source_revision"],
        "TRADEJOURNAL_AGENT_DEPLOYMENT_ID": request["deployment_id"],
        "TRADEJOURNAL_AGENT_READINESS_PATH": str(AGENT_READINESS_PATH),
    }
    for name, expected in required.items():
        if environment.get(name) != expected:
            raise DeployGuardError("deployment_environment_binding_invalid")
    python_path = environment.get("PYTHONPATH", "").split(";", 1)[0]
    if _path_text(Path(python_path)) != _path_text(release):
        raise DeployGuardError("deployment_environment_binding_invalid")
    try:
        config = load_runtime_config(environment)
    except Exception as exc:
        raise DeployGuardError("runtime_config_invalid") from exc
    if (
        config.mt5_interactive_user.casefold()
        != DEDICATED_MT5_INTERACTIVE_USER.casefold()
        or _path_text(config.source_terminal.parent) != _path_text(GOLDEN_ROOT)
        or _path_text(config.expert_binary) != _path_text(GOLDEN_EXPERT_PATH)
    ):
        raise DeployGuardError("runtime_config_binding_invalid")
    _assert_maintenance_policy(config)
    return entries, environment, config, release


def _assert_maintenance_policy(config: AgentRuntimeConfig) -> None:
    scheduled = config.mt5_maintenance_local_time
    if (
        not config.mt5_maintenance_enabled
        or config.mt5_maintenance_timezone != REQUIRED_MAINTENANCE_TIMEZONE
        or scheduled.hour != REQUIRED_MAINTENANCE_HOUR
        or scheduled.minute != REQUIRED_MAINTENANCE_MINUTE
        or scheduled.second != 0
        or config.mt5_maintenance_grace_minutes
        != REQUIRED_MAINTENANCE_GRACE_MINUTES
    ):
        raise DeployGuardError("maintenance_policy_invalid")


def _iter_terminal_processes(config: AgentRuntimeConfig) -> list[tuple[int, Path]]:
    try:
        import psutil

        observed: list[tuple[int, Path]] = []
        observed_executables: set[str] = set()
        instances = _path_text(config.instances_root)
        for process in psutil.process_iter(("pid", "name", "exe")):
            try:
                name = str(process.info.get("name") or "").casefold()
                executable_raw = process.info.get("exe")
                executable_name = (
                    Path(executable_raw).name.casefold()
                    if isinstance(executable_raw, str) and executable_raw
                    else ""
                )
                name = executable_name or name
                if name not in ("terminal64.exe", "metaeditor64.exe") and not (
                    isinstance(executable_raw, str)
                    and "liveupdate" in executable_raw.casefold()
                ):
                    continue
                if not isinstance(executable_raw, str) or not executable_raw:
                    raise DeployGuardError("mt5_process_probe_failed")
                executable = Path(executable_raw)
                if name != "terminal64.exe" or "liveupdate" in executable_raw.casefold():
                    raise DeployGuardError("mt5_update_process_running")
                relative = os.path.relpath(_path_text(executable), instances)
                parts = Path(relative).parts
                if (
                    len(parts) != 3
                    or parts[1].casefold() != "terminal"
                    or parts[2].casefold() != "terminal64.exe"
                ):
                    raise DeployGuardError("unexpected_mt5_process")
                canonical_uuid(parts[0])
                pid = int(process.info["pid"])
                normalized_executable = _path_text(executable)
                if normalized_executable in observed_executables:
                    raise DeployGuardError("duplicate_mt5_process")
                instance_root = config.instances_root / parts[0]
                try:
                    if (
                        InstanceProvisioner._is_reparse_point(instance_root)
                        or read_json(instance_root / "state" / "instance.json", {}).get(
                            "status"
                        )
                        != "provisioned"
                    ):
                        raise ValueError
                except (OSError, ValueError) as exc:
                    raise DeployGuardError("unexpected_mt5_process") from exc
                verify_interactive_process_identity(config.mt5_interactive_user, pid)
                observed_executables.add(normalized_executable)
                observed.append((pid, executable))
            except DeployGuardError:
                raise
            except InteractiveIdentityError as exc:
                raise DeployGuardError(exc.code) from exc
            except Exception as exc:
                raise DeployGuardError("mt5_process_probe_failed") from exc
        return observed
    except DeployGuardError:
        raise
    except InteractiveIdentityError as exc:
        raise DeployGuardError(exc.code) from exc
    except Exception as exc:
        raise DeployGuardError("mt5_process_probe_failed") from exc


def _validate_fpm_id(value: object) -> str:
    return _uuid4(value, "fpm_connection_id_invalid")


def _optional_fpm_id(value: object) -> str:
    if value == "":
        return ""
    return _validate_fpm_id(value)


def _binding(request: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "deployment_id": request["deployment_id"],
        "source_revision": request["source_revision"],
    }


def _assert_record_binding(record: Mapping[str, Any], request: Mapping[str, Any]) -> None:
    if any(record.get(name) != value for name, value in _binding(request).items()):
        raise DeployGuardError("deployment_binding_mismatch")


def _read_deployment_record(
    request: Mapping[str, Any], name: str, fields: frozenset[str]
) -> dict[str, Any]:
    record = _read_json_exact(
        _record_path(request["deployment_id"], name),
        fields,
        f"deployment_{name}_invalid",
    )
    _assert_record_binding(record, request)
    return record


_SNAPSHOT_FIELDS = frozenset(
    {
        "schema_version",
        "deployment_id",
        "source_revision",
        "nonce",
        "created_at_unix_ms",
        "bridge_sha256",
        "marker_present",
        "marker_sha256",
        "environment",
        "current_target",
        "snapshot_sha256",
    }
)
_PREFLIGHT_FIELDS = frozenset(
    {
        "schema_version",
        "deployment_id",
        "source_revision",
        "nonce",
        "checked_at_unix_ms",
        "golden_terminal_sha256",
        "old_expert_sha256",
        "old_marker_present",
        "old_marker_sha256",
        "old_service_environment_sha256",
        "old_release_present",
        "old_release_path",
        "new_expert_sha256",
        "service_environment_sha256",
        "release_path",
        "fpm_connection_id",
        "live_instance_count",
        "provisioned_instance_count",
    }
)
_SWITCH_FIELDS = frozenset(
    {
        "schema_version",
        "deployment_id",
        "source_revision",
        "nonce",
        "switched_at_unix_ms",
        "previous_expert_sha256",
        "new_expert_sha256",
        "code_manifest_sha256",
        "release_path",
        "service_environment_sha256",
    }
)
_ARM_FIELDS = frozenset(
    {
        "schema_version",
        "deployment_id",
        "source_revision",
        "nonce",
        "armed_at_unix_ms",
        "window_started_at_unix_ms",
        "window_ends_at_unix_ms",
    }
)
_BARRIER_FIELDS = frozenset(
    {
        "schema_version",
        "deployment_id",
        "source_revision",
        "nonce",
        "activation_started_at_unix_ms",
    }
)
_RESTORE_FIELDS = frozenset(
    {
        "schema_version",
        "deployment_id",
        "source_revision",
        "nonce",
        "restored_at_unix_ms",
        "snapshot_sha256",
    }
)
_CONVERGE_FIELDS = frozenset(
    {
        "schema_version",
        "deployment_id",
        "source_revision",
        "nonce",
        "converged_at_unix_ms",
        "release_id",
        "pool_ready_count",
        "fleet_count",
        "fpm_connection_id",
    }
)
_CONVERGE_ATTEMPT_FIELDS = frozenset(
    {
        "schema_version",
        "deployment_id",
        "source_revision",
        "nonce",
        "status",
        "scheduled_local_date",
        "started_at_unix_ms",
        "finished_at_unix_ms",
        "attempts",
    }
)


def _snapshot_digest(
    bridge: bytes, marker: bytes | None, environment: list[str], target: str | None
) -> str:
    document = {
        "bridge_sha256": _sha256_bytes(bridge),
        "marker_present": marker is not None,
        "marker_sha256": _sha256_bytes(marker) if marker is not None else None,
        "environment": environment,
        "current_target": target,
    }
    return _sha256_bytes(_json_bytes(document))


def _provisioned_instance_count(config: AgentRuntimeConfig) -> int:
    count = 0
    if not config.instances_root.exists():
        return 0
    try:
        for child in sorted(config.instances_root.iterdir(), key=lambda item: item.name):
            try:
                canonical_uuid(child.name)
            except ValueError:
                continue
            if InstanceProvisioner._is_reparse_point(child) or not child.is_dir():
                raise DeployGuardError("fleet_state_invalid")
            state = read_json(child / "state" / "instance.json", {})
            if state.get("status") == "provisioned":
                count += 1
    except DeployGuardError:
        raise
    except (OSError, ValueError) as exc:
        raise DeployGuardError("fleet_state_invalid") from exc
    return count


def _preflight(request: dict[str, Any]) -> dict[str, Any]:
    payload = request["payload"]
    _, _, config, _ = _validated_next_config(request, payload)
    new_expert = _require_exact_path(
        payload.get("new_expert_path"), ARTIFACT_EXPERT_PATH, "expert_path_invalid"
    )
    expert_sha256 = _sha256_file(new_expert, "new_expert_invalid")
    if expert_sha256 != config.expert_sha256:
        raise DeployGuardError("new_expert_digest_mismatch")
    try:
        manager = Mt5TemplateManager(config.source_terminal, config.terminal_sha256)
        golden_digest = manager.validate_current_quiesced()
        verify_interactive_task_identity(config.mt5_interactive_user)
    except InteractiveIdentityError as exc:
        raise DeployGuardError(exc.code) from exc
    except Exception as exc:
        raise DeployGuardError("deployment_preflight_failed") from exc
    observed = _iter_terminal_processes(config)
    provisioned_count = _provisioned_instance_count(config)
    fpm = _optional_fpm_id(payload.get("fpm_connection_id"))
    if fpm:
        expected_fpm = _path_text(
            config.instances_root / fpm / "terminal" / "terminal64.exe"
        )
        if not any(_path_text(path) == expected_fpm for _, path in observed):
            raise DeployGuardError("fpm_process_unavailable")
    elif provisioned_count != 0 or observed:
        # An empty canary is a narrowly scoped first-install mode, never a way
        # to skip the requested FPM proof on an existing fleet.
        raise DeployGuardError("fpm_connection_id_required")
    old_release = _current_target_optional()
    if old_release is not None:
        try:
            verify_release(old_release)
        except Exception as exc:
            raise DeployGuardError("current_release_invalid") from exc
    try:
        old_expert_sha256 = _sha256_file(
            GOLDEN_EXPERT_PATH, "golden_expert_invalid"
        )
        if GOLDEN_MARKER_PATH.exists() and not GOLDEN_MARKER_PATH.is_file():
            raise OSError
        marker = (
            GOLDEN_MARKER_PATH.read_bytes()
            if GOLDEN_MARKER_PATH.is_file()
            else None
        )
        old_environment = _get_service_environment()
    except OSError as exc:
        raise DeployGuardError("deployment_preflight_failed") from exc
    details = {
        "golden_terminal_sha256": golden_digest,
        "live_instance_count": len(observed),
        "provisioned_instance_count": provisioned_count,
    }
    record = {
        **_binding(request),
        "nonce": request["nonce"],
        "checked_at_unix_ms": int(time.time() * 1000),
        "golden_terminal_sha256": golden_digest,
        "old_expert_sha256": old_expert_sha256,
        "old_marker_present": marker is not None,
        "old_marker_sha256": _sha256_bytes(marker) if marker is not None else None,
        "old_service_environment_sha256": _sha256_bytes(
            _json_bytes({"environment": old_environment})
        ),
        "old_release_present": old_release is not None,
        "old_release_path": str(old_release) if old_release is not None else None,
        "new_expert_sha256": expert_sha256,
        "service_environment_sha256": _sha256_bytes(
            _json_bytes({"environment": payload["next_environment"]})
        ),
        "release_path": str(RELEASE_ROOT / f"agent-{request['source_revision'][:12]}"),
        "fpm_connection_id": fpm,
        "live_instance_count": len(observed),
        "provisioned_instance_count": provisioned_count,
    }
    path = _record_path(request["deployment_id"], "preflight")
    if path.exists():
        previous = _read_deployment_record(request, "preflight", _PREFLIGHT_FIELDS)
        comparable = set(_PREFLIGHT_FIELDS) - {"nonce", "checked_at_unix_ms"}
        if any(previous.get(name) != record.get(name) for name in comparable):
            raise DeployGuardError("deployment_preflight_drifted")
    else:
        try:
            _exclusive_json(path, record)
        except FileExistsError as exc:
            raise DeployGuardError("deployment_preflight_already_exists") from exc
    return details


def _snapshot(request: dict[str, Any]) -> dict[str, Any]:
    _assert_service_stopped()
    preflight = _read_deployment_record(request, "preflight", _PREFLIGHT_FIELDS)
    root = _deployment_root(request["deployment_id"])
    snapshot_path = _record_path(request["deployment_id"], "snapshot")
    if snapshot_path.exists():
        record = _read_deployment_record(request, "snapshot", _SNAPSHOT_FIELDS)
        return {"snapshot_sha256": record["snapshot_sha256"]}
    try:
        bridge = GOLDEN_EXPERT_PATH.read_bytes()
        if GOLDEN_MARKER_PATH.exists() and not GOLDEN_MARKER_PATH.is_file():
            raise OSError
        marker = GOLDEN_MARKER_PATH.read_bytes() if GOLDEN_MARKER_PATH.is_file() else None
    except OSError as exc:
        raise DeployGuardError("golden_snapshot_failed") from exc
    if not bridge:
        raise DeployGuardError("golden_snapshot_failed")
    environment = _get_service_environment()
    current = _current_target_optional()
    current_target = str(current) if current is not None else None
    if current is not None:
        try:
            verify_release(current)
        except Exception as exc:
            raise DeployGuardError("current_release_invalid") from exc
    if (
        _sha256_bytes(bridge) != preflight.get("old_expert_sha256")
        or (marker is not None) is not preflight.get("old_marker_present")
        or (_sha256_bytes(marker) if marker is not None else None)
        != preflight.get("old_marker_sha256")
        or _sha256_bytes(_json_bytes({"environment": environment}))
        != preflight.get("old_service_environment_sha256")
        or (current is not None) is not preflight.get("old_release_present")
        or (
            current is not None
            and _path_text(current)
            != _path_text(Path(str(preflight.get("old_release_path", ""))))
        )
    ):
        raise DeployGuardError("deployment_preflight_drifted")
    _ensure_shared_directory(root)
    _atomic_bytes(root / "bridge.bin", bridge)
    if marker is not None:
        _atomic_bytes(root / "marker.bin", marker)
    digest = _snapshot_digest(bridge, marker, environment, current_target)
    record = {
        **_binding(request),
        "nonce": request["nonce"],
        "created_at_unix_ms": int(time.time() * 1000),
        "bridge_sha256": _sha256_bytes(bridge),
        "marker_present": marker is not None,
        "marker_sha256": _sha256_bytes(marker) if marker is not None else None,
        "environment": environment,
        "current_target": current_target,
        "snapshot_sha256": digest,
    }
    try:
        _exclusive_json(snapshot_path, record)
    except FileExistsError as exc:
        raise DeployGuardError("snapshot_already_exists") from exc
    return {"snapshot_sha256": digest}


def _load_snapshot(request: Mapping[str, Any]) -> tuple[dict[str, Any], bytes, bytes | None]:
    record = _read_deployment_record(request, "snapshot", _SNAPSHOT_FIELDS)
    root = _deployment_root(request["deployment_id"])
    try:
        bridge = (root / "bridge.bin").read_bytes()
        marker = (root / "marker.bin").read_bytes() if record["marker_present"] else None
    except OSError as exc:
        raise DeployGuardError("snapshot_integrity_invalid") from exc
    if (
        not bridge
        or _sha256_bytes(bridge) != record["bridge_sha256"]
        or (marker is None) != (not record["marker_present"])
        or (
            marker is not None and _sha256_bytes(marker) != record["marker_sha256"]
        )
        or _snapshot_digest(
            bridge, marker, record["environment"], record["current_target"]
        )
        != record["snapshot_sha256"]
    ):
        raise DeployGuardError("snapshot_integrity_invalid")
    return record, bridge, marker


def _switch(request: dict[str, Any]) -> dict[str, Any]:
    _assert_service_stopped()
    preflight = _read_deployment_record(request, "preflight", _PREFLIGHT_FIELDS)
    if _record_path(request["deployment_id"], "restore").exists():
        raise DeployGuardError("deployment_was_restored")
    payload = request["payload"]
    entries, _, config, release = _validated_next_config(request, payload)
    new_expert = _require_exact_path(
        payload.get("new_expert_path"), ARTIFACT_EXPERT_PATH, "expert_path_invalid"
    )
    previous_digest = _digest(
        payload.get("previous_expert_sha256"), "previous_expert_digest_invalid"
    )
    next_digest = _digest(payload.get("new_expert_sha256"), "new_expert_digest_invalid")
    if next_digest != config.expert_sha256 or _sha256_file(
        new_expert, "new_expert_invalid"
    ) != next_digest:
        raise DeployGuardError("new_expert_digest_mismatch")
    if (
        preflight.get("new_expert_sha256") != next_digest
        or preflight.get("service_environment_sha256")
        != _sha256_bytes(_json_bytes({"environment": entries}))
        or _path_text(Path(str(preflight.get("release_path", ""))))
        != _path_text(release)
    ):
        raise DeployGuardError("deployment_preflight_drifted")
    snapshot, bridge, _ = _load_snapshot(request)
    if previous_digest != snapshot["bridge_sha256"]:
        raise DeployGuardError("previous_expert_digest_mismatch")
    switch_path = _record_path(request["deployment_id"], "switch")
    if switch_path.exists():
        record = _read_deployment_record(request, "switch", _SWITCH_FIELDS)
        if (
            record.get("previous_expert_sha256") != previous_digest
            or record.get("new_expert_sha256") != next_digest
        ):
            raise DeployGuardError("deployment_switch_already_completed")
        if (
            _sha256_file(GOLDEN_EXPERT_PATH, "golden_expert_invalid") != next_digest
            or _path_text(_current_target()) != _path_text(release)
            or _get_service_environment() != entries
            or record.get("service_environment_sha256")
            != _sha256_bytes(_json_bytes({"environment": entries}))
        ):
            raise DeployGuardError("deployment_switch_drifted")
        return {"code_manifest_sha256": record["code_manifest_sha256"]}
    observed = _sha256_file(GOLDEN_EXPERT_PATH, "golden_expert_invalid")
    if observed not in (previous_digest, next_digest):
        raise DeployGuardError("golden_expert_drifted")
    if observed == previous_digest:
        try:
            _atomic_bytes(GOLDEN_EXPERT_PATH, new_expert.read_bytes())
        except OSError as exc:
            raise DeployGuardError("golden_expert_switch_failed") from exc
    try:
        manager = Mt5TemplateManager(config.source_terminal, config.terminal_sha256)
        code_manifest = manager.reseal_managed_code_deployment(
            _deployment_root(request["deployment_id"]) / "bridge.bin",
            previous_digest,
            next_digest,
        )
    except Exception as exc:
        raise DeployGuardError("golden_expert_reseal_failed") from exc
    _set_service_environment(entries)
    _replace_current_target(release)
    record = {
        **_binding(request),
        "nonce": request["nonce"],
        "switched_at_unix_ms": int(time.time() * 1000),
        "previous_expert_sha256": previous_digest,
        "new_expert_sha256": next_digest,
        "code_manifest_sha256": code_manifest,
        "release_path": str(release),
        "service_environment_sha256": _sha256_bytes(
            _json_bytes({"environment": entries})
        ),
    }
    try:
        _exclusive_json(switch_path, record)
    except FileExistsError as exc:
        raise DeployGuardError("deployment_switch_already_completed") from exc
    return {"code_manifest_sha256": code_manifest}


def _maintenance_window(
    config: AgentRuntimeConfig, now: datetime | None = None
) -> tuple[datetime, datetime]:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise DeployGuardError("maintenance_clock_invalid")
    zone = config.mt5_maintenance_timezone
    try:
        from zoneinfo import ZoneInfo

        local = current.astimezone(ZoneInfo(zone))
        candidates = []
        for day in (local.date(), local.date() - timedelta(days=1)):
            start = datetime.combine(
                day, config.mt5_maintenance_local_time, tzinfo=ZoneInfo(zone)
            )
            end = start + timedelta(minutes=config.mt5_maintenance_grace_minutes)
            if start <= local <= end:
                candidates.append((start, end))
        if len(candidates) != 1:
            raise DeployGuardError("outside_maintenance_window")
        return candidates[0]
    except DeployGuardError:
        raise
    except Exception as exc:
        raise DeployGuardError("maintenance_clock_invalid") from exc


def _assert_switch_intact(request: Mapping[str, Any]) -> tuple[AgentRuntimeConfig, dict[str, Any]]:
    """Re-prove every byte selected by switch immediately before the PONR."""

    switch = _read_deployment_record(request, "switch", _SWITCH_FIELDS)
    expected_release = RELEASE_ROOT / f"agent-{request['source_revision'][:12]}"
    if (
        _path_text(Path(str(switch.get("release_path", ""))))
        != _path_text(expected_release)
        or _path_text(_current_target()) != _path_text(expected_release)
        or _sha256_file(GOLDEN_EXPERT_PATH, "golden_expert_invalid")
        != switch.get("new_expert_sha256")
    ):
        raise DeployGuardError("deployment_switch_drifted")
    entries = _get_service_environment()
    if _sha256_bytes(_json_bytes({"environment": entries})) != switch.get(
        "service_environment_sha256"
    ):
        raise DeployGuardError("deployment_switch_drifted")
    _, overrides = _environment_map(entries)
    environment = _effective_environment(overrides)
    try:
        config = load_runtime_config(environment)
        _assert_maintenance_policy(config)
        manager = Mt5TemplateManager(config.source_terminal, config.terminal_sha256)
        manager.validate_current_quiesced()
        code_manifest = InstanceProvisioner._code_manifest(config.source_terminal.parent)
        verify_release(expected_release)
    except DeployGuardError:
        raise
    except Exception as exc:
        raise DeployGuardError("deployment_switch_drifted") from exc
    if code_manifest != switch.get("code_manifest_sha256"):
        raise DeployGuardError("deployment_switch_drifted")
    return config, switch


def _assert_pre_barrier_identity(request: Mapping[str, Any]) -> None:
    """A durable preflight is necessary but a fresh token proof is decisive."""

    preflight = _read_deployment_record(request, "preflight", _PREFLIGHT_FIELDS)
    config, _ = _assert_switch_intact(request)
    try:
        verify_interactive_task_identity(config.mt5_interactive_user)
    except Exception as exc:
        raise DeployGuardError("interactive_identity_invalid") from exc
    observed = _iter_terminal_processes(config)
    expected_live_count = preflight.get("live_instance_count")
    expected_provisioned_count = preflight.get("provisioned_instance_count")
    if (
        not isinstance(expected_live_count, int)
        or isinstance(expected_live_count, bool)
        or expected_live_count < 0
        or not isinstance(expected_provisioned_count, int)
        or isinstance(expected_provisioned_count, bool)
        or expected_provisioned_count < 0
        or len(observed) != expected_live_count
        or _provisioned_instance_count(config) != expected_provisioned_count
    ):
        raise DeployGuardError("deployment_fleet_drifted")
    fpm = str(preflight["fpm_connection_id"])
    if not fpm:
        if observed or expected_provisioned_count != 0:
            raise DeployGuardError("fpm_connection_id_required")
        return
    expected_fpm = _path_text(
        config.instances_root / fpm / "terminal" / "terminal64.exe"
    )
    if not any(_path_text(path) == expected_fpm for _, path in observed):
        raise DeployGuardError("fpm_process_unavailable")


def _arm(request: dict[str, Any]) -> dict[str, Any]:
    _assert_service_stopped()
    if _record_path(request["deployment_id"], "restore").exists():
        raise DeployGuardError("deployment_was_restored")
    _assert_switch_intact(request)
    _assert_pre_barrier_identity(request)
    arm_path = _record_path(request["deployment_id"], "arm")
    if arm_path.exists():
        record = _read_deployment_record(request, "arm", _ARM_FIELDS)
        return {"activation_started_at_unix_ms": record["armed_at_unix_ms"]}
    _, overrides = _environment_map(_get_service_environment())
    environment = _effective_environment(overrides)
    try:
        config = load_runtime_config(environment)
    except Exception as exc:
        raise DeployGuardError("runtime_config_invalid") from exc
    _assert_maintenance_policy(config)
    start, end = _maintenance_window(config)
    now_ms = int(time.time() * 1000)
    record = {
        **_binding(request),
        "nonce": request["nonce"],
        "armed_at_unix_ms": now_ms,
        "window_started_at_unix_ms": int(start.timestamp() * 1000),
        "window_ends_at_unix_ms": int(end.timestamp() * 1000),
    }
    try:
        _exclusive_json(arm_path, record)
    except FileExistsError as exc:
        raise DeployGuardError("deployment_already_armed") from exc
    return {"activation_started_at_unix_ms": now_ms}


def _barrier(request: dict[str, Any]) -> dict[str, Any]:
    _assert_service_stopped()
    if _record_path(request["deployment_id"], "restore").exists():
        raise DeployGuardError("deployment_was_restored")
    _assert_switch_intact(request)
    _assert_pre_barrier_identity(request)
    _remove_stale_readiness()
    arm = _read_deployment_record(request, "arm", _ARM_FIELDS)
    barrier_path = _record_path(request["deployment_id"], "barrier")
    if barrier_path.exists():
        record = _read_deployment_record(request, "barrier", _BARRIER_FIELDS)
        return {
            "activation_started_at_unix_ms": record[
                "activation_started_at_unix_ms"
            ]
        }
    now_ms = int(time.time() * 1000)
    if not arm["window_started_at_unix_ms"] <= now_ms <= arm["window_ends_at_unix_ms"]:
        raise DeployGuardError("outside_maintenance_window")
    record = {
        **_binding(request),
        "nonce": request["nonce"],
        "activation_started_at_unix_ms": now_ms,
    }
    try:
        _exclusive_json(barrier_path, record, system_only=True)
    except FileExistsError as exc:
        raise DeployGuardError("activation_barrier_already_exists") from exc
    return {"activation_started_at_unix_ms": now_ms}


def _remove_stale_readiness() -> None:
    try:
        if not AGENT_READINESS_PATH.exists():
            return
        if (
            InstanceProvisioner._is_reparse_point(AGENT_READINESS_PATH)
            or not AGENT_READINESS_PATH.is_file()
        ):
            raise OSError
        AGENT_READINESS_PATH.unlink()
        fsync_directory(AGENT_READINESS_PATH.parent)
    except OSError as exc:
        raise DeployGuardError("stale_service_readiness_cleanup_failed") from exc


def _barrier_status(request: dict[str, Any]) -> dict[str, Any]:
    path = _record_path(request["deployment_id"], "barrier")
    if not path.exists():
        return {
            "activation_barrier_crossed": False,
            "activation_started_at_unix_ms": None,
        }
    record = _barrier_for_request(request)
    return {
        "activation_barrier_crossed": True,
        "activation_started_at_unix_ms": record[
            "activation_started_at_unix_ms"
        ],
    }


def _barrier_for_request(request: Mapping[str, Any]) -> dict[str, Any]:
    barrier = _read_deployment_record(request, "barrier", _BARRIER_FIELDS)
    started = barrier.get("activation_started_at_unix_ms")
    if not isinstance(started, int) or isinstance(started, bool) or started <= 0:
        raise DeployGuardError("activation_barrier_invalid")
    return barrier


def _activation_config(request: Mapping[str, Any]) -> AgentRuntimeConfig:
    _barrier_for_request(request)
    _read_deployment_record(request, "arm", _ARM_FIELDS)
    _, overrides = _environment_map(_get_service_environment())
    try:
        config = load_runtime_config(_effective_environment(overrides))
    except Exception as exc:
        raise DeployGuardError("runtime_config_invalid") from exc
    _assert_maintenance_policy(config)
    # A failed post-barrier attempt may be retried only in a later approved
    # nightly window.  It is not permanently tied to the original arm date.
    _maintenance_window(config)
    if (
        _path_text(_current_target())
        != _path_text(RELEASE_ROOT / f"agent-{request['source_revision'][:12]}")
    ):
        raise DeployGuardError("active_release_mismatch")
    return config


def _build_activation_coordinator(
    config: AgentRuntimeConfig,
) -> tuple[Mt5MaintenanceCoordinator, Mt5InstancePool | None]:
    """Build the same maintenance graph as the daemon, without starting it."""

    template_lock = threading.RLock()
    manager = Mt5TemplateManager(
        config.source_terminal,
        config.terminal_sha256,
        lock=template_lock,
    )
    current_digest = manager.current_sha256
    lifecycle = Mt5LifecycleCoordinator()
    pending = Mt5PendingUpdateStore(
        config.mt5_maintenance_state_path.parent / "mt5-update-pending"
    )
    rotator = Mt5InstanceRotator(
        instances_root=config.instances_root,
        secrets_root=config.secrets_root,
        source_terminal=config.source_terminal,
        expert_binary=config.expert_binary,
        expert_sha256=config.expert_sha256,
        lifecycle=lifecycle,
        template_lock=template_lock,
    )
    pool: Mt5InstancePool | None = None
    if config.instance_pool_target_size > 0:
        pool = Mt5InstancePool(
            pool_root=config.instance_pool_root,
            instances_root=config.instances_root,
            secrets_root=config.secrets_root,
            source_terminal=config.source_terminal,
            expected_terminal_sha256=current_digest,
            target_size=config.instance_pool_target_size,
            max_size=config.instance_pool_max_size,
            template_lock=template_lock,
        )
        pool.recover_incomplete()
    public_release_probe = Mt5PublicReleaseProbe(
        config.mt5_maintenance_state_path.parent / "mt5-public-releases"
    )
    return (
        Mt5MaintenanceCoordinator(
            instances_root=config.instances_root,
            expert_binary=config.expert_binary,
            template_manager=manager,
            rotator=rotator,
            lifecycle=lifecycle,
            template_lock=template_lock,
            instance_pool=pool,
            pending_update_store=pending,
            public_release_probe=public_release_probe,
            public_release_inventory=(
                Mt5ProvisionedReleaseInventory(
                    config.instances_root,
                    trusted_template_root=config.source_terminal.parent,
                )
            ),
        ),
        pool,
    )


def _scheduler_timestamp(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _write_activation_schedule_state(
    config: AgentRuntimeConfig,
    request: Mapping[str, Any],
    *,
    status: str,
) -> None:
    start, end = _maintenance_window(config)
    now = datetime.now(timezone.utc)
    record: dict[str, Any] = {
        "schema_version": 1,
        "scheduled_local_date": start.date().isoformat(),
        "scheduled_at_local": start.isoformat(),
        "timezone": REQUIRED_MAINTENANCE_TIMEZONE,
        "status": status,
        # Two prevents the normal scheduler from immediately repeating a
        # failed post-barrier rollout during the same closed-market window.
        "attempts": 1 if status == "completed" else 2,
        "run_id": request["deployment_id"],
        "started_at_utc": _scheduler_timestamp(now),
        "finished_at_utc": _scheduler_timestamp(now),
        "activation_deployment_id": request["deployment_id"],
        "activation_source_revision": request["source_revision"],
    }
    if status == "failed":
        record["next_retry_at_utc"] = _scheduler_timestamp(
            end.astimezone(timezone.utc) + timedelta(minutes=10)
        )
    try:
        atomic_json(config.mt5_maintenance_state_path, record)
        _restrict_shared(config.mt5_maintenance_state_path)
    except Exception as exc:
        raise DeployGuardError("maintenance_schedule_state_write_failed") from exc


def _write_system_record(path: Path, document: Mapping[str, Any]) -> None:
    _atomic_bytes(path, _json_bytes(document))
    if os.name == "nt":
        WindowsSecretStore.restrict_acl(path)


def _convergence_attempt(
    config: AgentRuntimeConfig,
    request: Mapping[str, Any],
) -> tuple[Path, dict[str, Any]]:
    start, _ = _maintenance_window(config)
    path = _record_path(
        request["deployment_id"],
        f"converge-attempt-{start.date().isoformat()}",
    )
    record = {
        **_binding(request),
        "nonce": request["nonce"],
        "status": "running",
        "scheduled_local_date": start.date().isoformat(),
        "started_at_unix_ms": int(time.time() * 1000),
        "finished_at_unix_ms": None,
        "attempts": 1,
    }
    if path.exists():
        previous = _read_json_exact(
            path,
            _CONVERGE_ATTEMPT_FIELDS,
            "deployment_convergence_attempt_invalid",
        )
        _assert_record_binding(previous, request)
        attempts = previous.get("attempts")
        if (
            previous.get("scheduled_local_date") != start.date().isoformat()
            or previous.get("status") not in ("running", "failed")
            or not isinstance(attempts, int)
            or isinstance(attempts, bool)
            or not 1 <= attempts < 3
        ):
            raise DeployGuardError("deployment_convergence_attempt_exhausted")
        record = {
            **previous,
            "nonce": request["nonce"],
            "status": "running",
            "finished_at_unix_ms": None,
            "attempts": attempts + 1,
        }
        _write_system_record(path, record)
        return path, record
    try:
        _exclusive_json(path, record, system_only=True)
    except FileExistsError as exc:
        raise DeployGuardError("deployment_convergence_retry_deferred") from exc
    return path, record


def _fleet_postconditions(
    config: AgentRuntimeConfig,
    target: Mt5TemplateRelease,
    *,
    minimum_fleet_count: int = 1,
) -> tuple[int, int]:
    fleet_count = 0
    try:
        children = (
            sorted(config.instances_root.iterdir(), key=lambda item: item.name)
            if config.instances_root.exists()
            else ()
        )
        for child in children:
            try:
                canonical_uuid(child.name)
            except ValueError:
                continue
            if InstanceProvisioner._is_reparse_point(child) or not child.is_dir():
                raise DeployGuardError("fleet_state_invalid")
            state = read_json(child / "state" / "instance.json", {})
            if state.get("status") != "provisioned":
                continue
            _validate_instance_release(child, target)
            fleet_count += 1
    except DeployGuardError:
        raise
    except (OSError, ValueError) as exc:
        raise DeployGuardError("fleet_state_invalid") from exc
    if fleet_count < minimum_fleet_count:
        raise DeployGuardError("fleet_state_invalid")

    ready_root = config.instance_pool_root / "ready"
    pool_ready_count = 0
    if config.instance_pool_target_size == 0 and not ready_root.exists():
        return fleet_count, 0
    try:
        if InstanceProvisioner._is_reparse_point(ready_root) or not ready_root.is_dir():
            raise DeployGuardError("pool_state_invalid")
        for child in sorted(ready_root.iterdir(), key=lambda item: item.name):
            if InstanceProvisioner._is_reparse_point(child) or not child.is_dir():
                raise DeployGuardError("pool_state_invalid")
            try:
                canonical_uuid(child.name)
            except ValueError as exc:
                raise DeployGuardError("pool_state_invalid") from exc
            pool = read_json(child / "state" / "pool.json")
            instance = read_json(child / "state" / "instance.json")
            if (
                pool.get("slot_id") != child.name
                or pool.get("status") != "READY"
                or instance.get("connection_id") != child.name
                or instance.get("status") != "provisioned"
                or pool.get("terminal_sha256") != target.terminal_sha256
                or pool.get("code_manifest_sha256") != target.code_manifest_sha256
                or instance.get("terminal_sha256") != target.terminal_sha256
                or instance.get("template_code_manifest_sha256")
                != target.code_manifest_sha256
            ):
                raise DeployGuardError("pool_release_mismatch")
            InstanceProvisioner._validate_published_instance(
                child,
                instance,
                target.terminal_sha256,
            )
            if (
                InstanceProvisioner._tree_manifest(child / "terminal")
                != pool.get("template_manifest_sha256")
                or InstanceProvisioner._code_manifest(child / "terminal")
                != target.code_manifest_sha256
            ):
                raise DeployGuardError("pool_release_mismatch")
            pool_ready_count += 1
    except DeployGuardError:
        raise
    except (OSError, ValueError) as exc:
        raise DeployGuardError("pool_state_invalid") from exc
    if pool_ready_count < config.instance_pool_target_size:
        raise DeployGuardError("pool_target_not_reached")
    return fleet_count, pool_ready_count


def _converge(request: dict[str, Any]) -> dict[str, Any]:
    """Run one barrier-bound MT5 pass regardless of daily scheduler state."""

    _assert_service_stopped()
    config = _activation_config(request)
    fpm = _optional_fpm_id(request["payload"].get("fpm_connection_id"))
    preflight = _read_deployment_record(request, "preflight", _PREFLIGHT_FIELDS)
    if preflight.get("fpm_connection_id") != fpm:
        raise DeployGuardError("fpm_connection_id_mismatch")
    expected_fleet_count = preflight.get("provisioned_instance_count")
    if (
        not isinstance(expected_fleet_count, int)
        or isinstance(expected_fleet_count, bool)
        or expected_fleet_count < 0
        or (expected_fleet_count > 0 and not fpm)
    ):
        raise DeployGuardError("deployment_preflight_invalid")
    path = _record_path(request["deployment_id"], "converge")
    if path.exists():
        record = _read_deployment_record(request, "converge", _CONVERGE_FIELDS)
        try:
            manager = Mt5TemplateManager(config.source_terminal, config.terminal_sha256)
            target = Mt5TemplateRelease.from_template(
                config.source_terminal, manager.current_sha256
            )
            fleet_count, pool_count = _fleet_postconditions(
                config,
                target,
                minimum_fleet_count=expected_fleet_count,
            )
        except DeployGuardError:
            raise
        except Exception as exc:
            raise DeployGuardError("deployment_convergence_drifted") from exc
        if (
            record.get("release_id") != target.release_id
            or record.get("fpm_connection_id") != fpm
            or fleet_count != record.get("fleet_count")
            or pool_count < record.get("pool_ready_count", 0)
        ):
            raise DeployGuardError("deployment_convergence_drifted")
        return {
            "release_id": target.release_id,
            "pool_ready_count": pool_count,
            "fleet_count": fleet_count,
            "fpm_connection_id": fpm,
        }
    attempt_path, attempt = _convergence_attempt(config, request)
    try:
        verify_interactive_task_identity(config.mt5_interactive_user)
        coordinator, pool = _build_activation_coordinator(config)
        stop_event = threading.Event()
        selected = {
            canary.connection_id
            for canary in coordinator._canaries(coordinator._current_release())
        }
        explicitly_verified = False
        if fpm and fpm not in selected:
            try:
                root = coordinator.rotator._instance_root(fpm)
                state = read_json(root / "state" / "instance.json", {})
                login = int(coordinator.secrets.read(fpm, "mt5_login"))
                server = coordinator.secrets.read(fpm, "mt5_server")
                if (
                    state.get("status") != "provisioned"
                    or login <= 0
                    or not isinstance(server, str)
                    or server.casefold() != FPM_SERVER_NAME.casefold()
                ):
                    raise ValueError
                # _probe is the exact restart/login/heartbeat/read-only gate
                # used by run_once.  Probe this requested connection explicitly
                # when another account on the same server won normal selection.
                coordinator._probe(
                    SimpleNamespace(
                        connection_id=fpm,
                        root=root,
                        login=login,
                        server=server,
                    ),
                    stop_event,
                )
                explicitly_verified = True
            except Exception as exc:
                raise DeployGuardError("fpm_canary_not_verified") from exc
        report = coordinator.run_once(stop_event)
        if fpm and not explicitly_verified and fpm not in report.checked_connections:
            raise DeployGuardError("fpm_canary_not_verified")
        target = report.current_release
        fleet_count, pool_count = _fleet_postconditions(
            config,
            target,
            minimum_fleet_count=expected_fleet_count,
        )
        if fleet_count != expected_fleet_count:
            raise DeployGuardError("fleet_state_invalid")
        if pool is not None and pool.ready_count() != pool_count:
            raise DeployGuardError("pool_state_invalid")
        _write_activation_schedule_state(config, request, status="completed")
    except DeployGuardError:
        try:
            _write_system_record(
                attempt_path,
                {
                    **attempt,
                    "status": "failed",
                    "finished_at_unix_ms": int(time.time() * 1000),
                },
            )
        except DeployGuardError:
            pass
        try:
            _write_activation_schedule_state(config, request, status="failed")
        except DeployGuardError:
            pass
        raise
    except Exception as exc:
        try:
            _write_system_record(
                attempt_path,
                {
                    **attempt,
                    "status": "failed",
                    "finished_at_unix_ms": int(time.time() * 1000),
                },
            )
        except DeployGuardError:
            pass
        try:
            _write_activation_schedule_state(config, request, status="failed")
        except DeployGuardError:
            pass
        raise DeployGuardError("deployment_convergence_failed") from exc
    record = {
        **_binding(request),
        "nonce": request["nonce"],
        "converged_at_unix_ms": int(time.time() * 1000),
        "release_id": target.release_id,
        "pool_ready_count": pool_count,
        "fleet_count": fleet_count,
        "fpm_connection_id": fpm,
    }
    try:
        _exclusive_json(path, record, system_only=True)
    except FileExistsError:
        # Another one-shot SYSTEM helper cannot legitimately run concurrently.
        # Treat this as ambiguous instead of trusting a record not yet checked.
        raise DeployGuardError("deployment_convergence_ambiguous") from None
    try:
        _write_system_record(
            attempt_path,
            {
                **attempt,
                "status": "completed",
                "finished_at_unix_ms": int(time.time() * 1000),
            },
        )
    except DeployGuardError:
        # converge.json is the stronger, immutable success evidence. Losing
        # this diagnostic transition cannot turn a committed success into a
        # second fleet restart.
        pass
    return {
        "release_id": target.release_id,
        "pool_ready_count": pool_count,
        "fleet_count": fleet_count,
        "fpm_connection_id": fpm,
    }


def _restore(request: dict[str, Any]) -> dict[str, Any]:
    _assert_service_stopped()
    if _record_path(request["deployment_id"], "barrier").exists():
        raise DeployGuardError("rollback_forbidden_after_barrier")
    restore_path = _record_path(request["deployment_id"], "restore")
    snapshot, bridge, marker = _load_snapshot(request)
    _atomic_bytes(GOLDEN_EXPERT_PATH, bridge)
    if marker is None:
        try:
            GOLDEN_MARKER_PATH.unlink(missing_ok=True)
        except OSError as exc:
            raise DeployGuardError("snapshot_restore_failed") from exc
    else:
        _atomic_bytes(GOLDEN_MARKER_PATH, marker)
    _set_service_environment(snapshot["environment"])
    raw_old_target = snapshot["current_target"]
    old_target = Path(raw_old_target) if isinstance(raw_old_target, str) else None
    if old_target is not None:
        try:
            verify_release(old_target)
        except Exception as exc:
            raise DeployGuardError("snapshot_restore_target_invalid") from exc
        _replace_current_target(old_target)
    else:
        _remove_current_target()
    # Re-read every restored byte.  This is deliberately independent of an
    # interactive user/session so a failed pre-barrier rollout is recoverable.
    observed_marker = GOLDEN_MARKER_PATH.read_bytes() if GOLDEN_MARKER_PATH.is_file() else None
    if (
        GOLDEN_EXPERT_PATH.read_bytes() != bridge
        or observed_marker != marker
        or _get_service_environment() != snapshot["environment"]
        or (
            old_target is None
            and _current_target_optional() is not None
        )
        or (
            old_target is not None
            and _path_text(_current_target()) != _path_text(old_target)
        )
    ):
        raise DeployGuardError("snapshot_restore_failed")
    if restore_path.exists():
        record = _read_deployment_record(request, "restore", _RESTORE_FIELDS)
        if record.get("snapshot_sha256") != snapshot["snapshot_sha256"]:
            raise DeployGuardError("deployment_was_restored")
    else:
        try:
            _exclusive_json(
                restore_path,
                {
                    **_binding(request),
                    "nonce": request["nonce"],
                    "restored_at_unix_ms": int(time.time() * 1000),
                    "snapshot_sha256": snapshot["snapshot_sha256"],
                },
            )
        except FileExistsError as exc:
            raise DeployGuardError("deployment_was_restored") from exc
    return {"restored": True}


def assert_service_activation_allowed(
    revision: str, deployment_id: str
) -> dict[str, Any]:
    """Fail closed unless this exact service release crossed its barrier."""

    if not isinstance(revision, str) or _REVISION.fullmatch(revision) is None:
        raise DeployGuardError("service_revision_invalid")
    deployment_id = _uuid4(deployment_id, "service_deployment_id_invalid")
    if (
        os.environ.get("TRADEJOURNAL_AGENT_RELEASE_REVISION") != revision
        or os.environ.get("TRADEJOURNAL_AGENT_DEPLOYMENT_ID") != deployment_id
    ):
        raise DeployGuardError("service_environment_binding_invalid")
    environment_path = os.environ.get("TRADEJOURNAL_AGENT_READINESS_PATH", "")
    if _path_text(Path(environment_path)) != _path_text(AGENT_READINESS_PATH):
        raise DeployGuardError("service_readiness_path_invalid")
    fields = _BARRIER_FIELDS
    path = _record_path(deployment_id, "barrier")
    record = _read_json_exact(path, fields, "activation_barrier_invalid")
    if (
        record.get("schema_version") != SCHEMA_VERSION
        or record.get("source_revision") != revision
        or record.get("deployment_id") != deployment_id
        or not isinstance(record.get("activation_started_at_unix_ms"), int)
        or isinstance(record.get("activation_started_at_unix_ms"), bool)
        or record["activation_started_at_unix_ms"] <= 0
    ):
        raise DeployGuardError("activation_barrier_invalid")
    convergence = _read_json_exact(
        _record_path(deployment_id, "converge"),
        _CONVERGE_FIELDS,
        "deployment_convergence_invalid",
    )
    if (
        convergence.get("schema_version") != SCHEMA_VERSION
        or convergence.get("source_revision") != revision
        or convergence.get("deployment_id") != deployment_id
        or not isinstance(convergence.get("release_id"), str)
        or _SHA256.fullmatch(convergence["release_id"]) is None
        or not isinstance(convergence.get("fleet_count"), int)
        or isinstance(convergence.get("fleet_count"), bool)
        or convergence["fleet_count"] < 0
        or not isinstance(convergence.get("pool_ready_count"), int)
        or isinstance(convergence.get("pool_ready_count"), bool)
        or convergence["pool_ready_count"] < 0
        or not isinstance(convergence.get("fpm_connection_id"), str)
        or (
            convergence["fleet_count"] > 0
            and not convergence["fpm_connection_id"]
        )
    ):
        raise DeployGuardError("deployment_convergence_invalid")
    if convergence["fpm_connection_id"]:
        try:
            _validate_fpm_id(convergence["fpm_connection_id"])
        except DeployGuardError as exc:
            raise DeployGuardError("deployment_convergence_invalid") from exc
    return {
        "schema_version": SCHEMA_VERSION,
        "source_revision": revision,
        "deployment_id": deployment_id,
        "activation_started_at_unix_ms": record[
            "activation_started_at_unix_ms"
        ],
    }


def _read_envelope(
    path: Path, code: str
) -> tuple[dict[str, Any], dict[str, Any], int, int]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(value, dict):
            raise ValueError
        generated = value.get("generated_at")
        parsed = datetime.fromisoformat(str(generated).replace("Z", "+00:00"))
        parsed = parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
        identity = value.get("account_identity")
        payload = value.get("payload")
        sequence = value.get("sequence")
        if (
            value.get("schema_version") != 1
            or not isinstance(identity, dict)
            or not isinstance(identity.get("login"), str)
            or not isinstance(identity.get("server"), str)
            or not isinstance(value.get("server_identity"), str)
            or not isinstance(payload, dict)
            or not isinstance(sequence, int)
            or isinstance(sequence, bool)
            or sequence <= 0
        ):
            raise ValueError
        if str(value["server_identity"]).casefold() != str(identity["server"]).casefold():
            raise ValueError
        return identity, payload, int(parsed.timestamp() * 1000), sequence
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise DeployGuardError(code) from exc


def _validate_instance_release(root: Path, target: Mt5TemplateRelease) -> dict[str, Any]:
    try:
        state = read_json(root / "state" / "instance.json")
        if not Mt5InstanceRotator._matches_target(root, state, target):
            raise DeployGuardError("fleet_release_mismatch")
        return state
    except DeployGuardError:
        raise
    except Exception as exc:
        raise DeployGuardError("fleet_state_invalid") from exc


def _verify_active(request: dict[str, Any]) -> dict[str, Any]:
    payload = request["payload"]
    max_age = payload.get("max_heartbeat_age_seconds")
    if (
        not isinstance(max_age, int)
        or isinstance(max_age, bool)
        or not 5 <= max_age <= MAX_HEARTBEAT_AGE_SECONDS
    ):
        raise DeployGuardError("heartbeat_age_invalid")
    # This helper is a separate LocalSystem scheduled task and does not inherit
    # service-specific registry overrides.  Bind directly to its strict request;
    # the service process itself uses assert_service_activation_allowed().
    barrier = _barrier_for_request(request)
    convergence = _read_deployment_record(
        request, "converge", _CONVERGE_FIELDS
    )
    status, service_pid = _service_status()
    if status != "running" or service_pid <= 0:
        raise DeployGuardError("service_not_running")
    readiness = _read_json_exact(
        AGENT_READINESS_PATH,
        frozenset(
            {
                "schema_version",
                "source_revision",
                "deployment_id",
                "service_process_id",
                "activation_started_at_unix_ms",
                "ready_at_unix_ms",
            }
        ),
        "service_readiness_invalid",
    )
    now_ms = int(time.time() * 1000)
    if (
        readiness.get("schema_version") != SCHEMA_VERSION
        or readiness.get("source_revision") != request["source_revision"]
        or readiness.get("deployment_id") != request["deployment_id"]
        or readiness.get("service_process_id") != service_pid
        or readiness.get("activation_started_at_unix_ms")
        != barrier["activation_started_at_unix_ms"]
        or not isinstance(readiness.get("ready_at_unix_ms"), int)
        or isinstance(readiness.get("ready_at_unix_ms"), bool)
        or not barrier["activation_started_at_unix_ms"]
        <= readiness["ready_at_unix_ms"]
        <= now_ms
        or now_ms - readiness["ready_at_unix_ms"] > 10 * 60 * 1000
    ):
        raise DeployGuardError("service_readiness_invalid")
    try:
        import psutil

        service_created_at = int(psutil.Process(service_pid).create_time() * 1000)
    except Exception as exc:
        raise DeployGuardError("service_process_identity_invalid") from exc
    if not (
        barrier["activation_started_at_unix_ms"] - 2000
        <= service_created_at
        <= readiness["ready_at_unix_ms"]
    ):
        raise DeployGuardError("service_process_identity_invalid")
    _, overrides = _environment_map(_get_service_environment())
    environment = _effective_environment(overrides)
    try:
        config = load_runtime_config(environment)
        manager = Mt5TemplateManager(config.source_terminal, config.terminal_sha256)
        terminal_digest = manager.validate_current_quiesced()
        target = Mt5TemplateRelease.from_template(config.source_terminal, terminal_digest)
    except Exception as exc:
        raise DeployGuardError("active_template_invalid") from exc
    expected_release = RELEASE_ROOT / f"agent-{request['source_revision'][:12]}"
    if _path_text(_current_target()) != _path_text(expected_release):
        raise DeployGuardError("active_release_mismatch")
    if convergence.get("release_id") != target.release_id:
        raise DeployGuardError("deployment_convergence_drifted")

    fpm = _optional_fpm_id(payload.get("fpm_connection_id"))
    pid: int | None = None
    process_created_at: int | None = None
    heartbeat_ms: int | None = None
    heartbeat_sequence: int | None = None
    if fpm:
        fpm_root = config.instances_root / fpm
        _validate_instance_release(fpm_root, target)
        process_record = _read_json_exact(
            fpm_root / "state" / "terminal-process.json",
            frozenset(
                {
                    "schema_version",
                    "pid",
                    "executable",
                    "creation_time_unix_ms",
                    "portable",
                }
            ),
            "fpm_process_state_invalid",
        )
        pid = process_record.get("pid")
        process_created_at = process_record.get("creation_time_unix_ms")
        executable = fpm_root / "terminal" / "terminal64.exe"
        if (
            process_record.get("schema_version") != 2
            or not isinstance(pid, int)
            or isinstance(pid, bool)
            or pid <= 0
            or _path_text(Path(str(process_record.get("executable", ""))))
            != _path_text(executable)
            or process_record.get("portable") is not True
            or not isinstance(process_created_at, int)
            or isinstance(process_created_at, bool)
            or process_created_at <= 0
        ):
            raise DeployGuardError("fpm_process_state_invalid")
        try:
            process = psutil.Process(pid)
            if (
                _path_text(Path(process.exe())) != _path_text(executable)
                or int(process.create_time() * 1000) != process_created_at
            ):
                raise DeployGuardError("fpm_process_identity_invalid")
            verify_interactive_process_identity(config.mt5_interactive_user, pid)
        except DeployGuardError:
            raise
        except Exception as exc:
            raise DeployGuardError("fpm_process_identity_invalid") from exc

        files = fpm_root / "terminal" / "MQL5" / "Files" / "TradeJournal"
        account_identity, account, account_ms, account_sequence = _read_envelope(
            files / "account.json", "fpm_account_invalid"
        )
        heartbeat_identity, heartbeat, heartbeat_ms, heartbeat_sequence = (
            _read_envelope(files / "heartbeat.json", "fpm_heartbeat_invalid")
        )
        try:
            expected_login = str(
                int(WindowsSecretStore(config.secrets_root).read(fpm, "mt5_login"))
            )
            expected_server = WindowsSecretStore(config.secrets_root).read(
                fpm, "mt5_server"
            )
        except Exception as exc:
            raise DeployGuardError("fpm_stored_identity_invalid") from exc
        if (
            expected_login in ("", "0")
            or expected_server.casefold() != FPM_SERVER_NAME.casefold()
            or account_identity != heartbeat_identity
            or account_sequence != heartbeat_sequence
            or account_identity.get("login") != expected_login
            or account_identity.get("server", "").casefold()
            != expected_server.casefold()
            or str(account.get("login", "")) != expected_login
            or account.get("server", "").casefold() != expected_server.casefold()
            or account.get("trade_allowed") is not False
            or heartbeat.get("terminal_connected") is not True
            or heartbeat.get("account_trade_allowed") is not False
            or now_ms - min(account_ms, heartbeat_ms) > max_age * 1000
            or max(account_ms, heartbeat_ms) > now_ms + 2000
            or not barrier["activation_started_at_unix_ms"] - 2000
            <= process_created_at
            <= min(
                account_ms,
                heartbeat_ms,
                readiness["ready_at_unix_ms"],
                now_ms,
            )
        ):
            raise DeployGuardError("fpm_readonly_health_invalid")
    elif convergence.get("fleet_count") != 0:
        raise DeployGuardError("fpm_connection_id_required")

    fleet_count, pool_ready_count = _fleet_postconditions(
        config,
        target,
        minimum_fleet_count=convergence.get("fleet_count", 0),
    )
    if (
        fleet_count != convergence.get("fleet_count")
        or pool_ready_count < convergence.get("pool_ready_count", 0)
        or convergence.get("fpm_connection_id") != fpm
    ):
        raise DeployGuardError("deployment_convergence_drifted")
    return {
        "service_process_id": service_pid,
        "fpm_connection_id": fpm,
        "fpm_process_id": pid,
        "fpm_process_creation_time_unix_ms": process_created_at,
        "heartbeat_generated_at_unix_ms": heartbeat_ms,
        "heartbeat_sequence": heartbeat_sequence,
        "pool_ready_count": pool_ready_count,
        "fleet_count": fleet_count,
    }


def run_request(request: dict[str, Any]) -> dict[str, Any]:
    handlers = {
        "preflight": _preflight,
        "snapshot": _snapshot,
        "switch": _switch,
        "arm": _arm,
        "barrier": _barrier,
        "barrier_status": _barrier_status,
        "converge": _converge,
        "restore": _restore,
        "verify_active": _verify_active,
    }
    return handlers[request["action"]](request)


def _result(
    request: Mapping[str, Any] | None,
    *,
    success: bool,
    code: str,
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "action": str(request.get("action", "invalid")) if request else "invalid",
        "nonce": str(request.get("nonce", "00000000-0000-4000-8000-000000000000"))
        if request
        else "00000000-0000-4000-8000-000000000000",
        "deployment_id": str(
            request.get("deployment_id", "00000000-0000-4000-8000-000000000000")
        )
        if request
        else "00000000-0000-4000-8000-000000000000",
        "source_revision": str(request.get("source_revision", "0" * 40))
        if request
        else "0" * 40,
        "success": success,
        "code": code,
        "details": dict(details or {}),
    }


def main(argv: list[str] | None = None) -> int:
    if list(sys.argv[1:] if argv is None else argv):
        return 2
    request: dict[str, Any] | None = None
    try:
        _assert_local_system()
        request = _load_request()
        details = run_request(request)
        result = _result(request, success=True, code="ok", details=details)
        exit_code = 0
    except DeployGuardError as exc:
        result = _result(request, success=False, code=exc.code)
        exit_code = 1
    except Exception:
        result = _result(request, success=False, code="deploy_guard_internal_error")
        exit_code = 1
    try:
        _atomic_bytes(RESULT_PATH, _json_bytes(result))
    except Exception:
        return 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
