"""Crash-safe rolling rebases of isolated MT5 instances onto a golden release."""

from __future__ import annotations

import hashlib
import logging
import re
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, RLock
from typing import Any, Callable, Optional

from worker.atomic_file import durable_replace, fsync_directory

from ..mt5_lifecycle import Mt5LifecycleCoordinator
from ..mt5_recovery_window import new_only_recovery_from
from ..security import canonical_uuid, safe_child
from ..state_store import atomic_json, read_json
from ..worker.native_mt5_runtime import NativeMt5Runtime, NativeMt5Status
from .mt5_instance import InstanceProvisioner
from .process_manager import ProcessManager
from .secret_store import WindowsSecretStore

logger = logging.getLogger(__name__)

VerifiedUpdateCallback = Callable[[Path, Path, Path, str], Optional[str]]

_ROTATION_STATE_NAME = "mt5-rotation.json"
_ROTATION_SCHEMA_VERSION = 1
_RECOVERY_FROM_UNIX_FIELD = "new_only_recovery_from_unix"
_STAGING_NAME = ".terminal-maintenance-staging"
_BACKUP_NAME = ".terminal-maintenance-backup"
_EXECUTABLE_SUFFIXES = frozenset({".dll", ".exe", ".ex5"})
_WINDOWS_TRANSIENT_DIRECTORY_MOVE_ERRORS = frozenset((5, 32, 33))
_DIRECTORY_MOVE_ATTEMPTS = 5
_DIRECTORY_MOVE_BASE_DELAY_SECONDS = 0.1

# The broker's local price cache and every log/tester cache are deliberately not
# copied.  Historical trades have already been ingested and the restarted bridge
# is explicitly placed in new_only mode.  Keeping only the bridge's durable files
# makes the account stop/swap window bounded by a small, known data set.
_PRESERVED_DIRECTORIES = (Path("MQL5/Files/TradeJournal"),)
_PRESERVED_CONFIG_DIRECTORIES = (Path("Config/certificates"),)
_PRESERVED_CONFIG_NAMES = frozenset(
    {
        "accounts.dat",
        "accounts.ini",
        "agents.dat",
        "assistant.ini",
        "common.ini",
        "community.ini",
        "dnsperf.dat",
        "servers.dat",
        "signals.ini",
        "terminal.ini",
        "terminal.lic",
    }
)


def _replace_directory_with_retry(source: Path, destination: Path) -> None:
    """Atomically move one directory through short-lived Windows locks."""

    for attempt in range(_DIRECTORY_MOVE_ATTEMPTS):
        try:
            durable_replace(source, destination)
            return
        except OSError as exc:
            if (
                getattr(exc, "winerror", None)
                not in _WINDOWS_TRANSIENT_DIRECTORY_MOVE_ERRORS
                or attempt + 1 >= _DIRECTORY_MOVE_ATTEMPTS
                or not source.exists()
            ):
                raise
            time.sleep(_DIRECTORY_MOVE_BASE_DELAY_SECONDS * (2**attempt))


class Mt5InstanceRotationError(RuntimeError):
    """A sanitized failure while rotating one or more MT5 instances."""

    def __init__(self, message: str, *, failed: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.failed = failed


@dataclass(frozen=True)
class Mt5TemplateRelease:
    terminal_sha256: str
    code_manifest_sha256: str
    release_id: str

    @classmethod
    def from_template(
        cls,
        source_terminal: Path,
        expected_terminal_sha256: str,
    ) -> "Mt5TemplateRelease":
        source_terminal = Path(source_terminal).resolve()
        expected = expected_terminal_sha256.strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise Mt5InstanceRotationError("MT5 template digest is invalid")
        try:
            terminal_sha256 = InstanceProvisioner._sha256(source_terminal)
            code_manifest_sha256 = InstanceProvisioner._code_manifest(
                source_terminal.parent
            )
        except (OSError, ValueError) as exc:
            raise Mt5InstanceRotationError("MT5 template is invalid") from exc
        if terminal_sha256 != expected:
            raise Mt5InstanceRotationError("MT5 template digest mismatch")
        release_id = hashlib.sha256(
            f"{terminal_sha256}:{code_manifest_sha256}".encode("ascii")
        ).hexdigest()
        return cls(terminal_sha256, code_manifest_sha256, release_id)

    def as_dict(self) -> dict[str, str]:
        return {
            "terminal_sha256": self.terminal_sha256,
            "code_manifest_sha256": self.code_manifest_sha256,
            "release_id": self.release_id,
        }


@dataclass(frozen=True)
class Mt5FleetRotationReport:
    target_release: Mt5TemplateRelease
    migrated: tuple[str, ...]
    already_current: tuple[str, ...]


@dataclass(frozen=True)
class Mt5RotationRecoveryReport:
    recovered: tuple[str, ...]
    failed: tuple[str, ...]


@dataclass(frozen=True)
class _StagedTemplate:
    root: Path
    template_manifest_sha256: str
    runtime_assets_manifest_sha256: str


class Mt5InstanceRotator:
    """Replace terminal directories one at a time, retaining account state."""

    def __init__(
        self,
        *,
        instances_root: Path,
        secrets_root: Path,
        source_terminal: Path,
        expert_binary: Path,
        expert_sha256: str,
        lifecycle: Mt5LifecycleCoordinator,
        template_lock: RLock | None = None,
        runtime_factory: Callable[[Path, str], NativeMt5Runtime] = NativeMt5Runtime,
        process_factory: Callable[[Path], Any] = ProcessManager,
        secret_store: Any | None = None,
    ) -> None:
        self.instances_root = Path(instances_root).resolve()
        self.secrets_root = Path(secrets_root).resolve()
        self.secrets = secret_store or WindowsSecretStore(self.secrets_root)
        self.source_terminal = Path(source_terminal).resolve()
        self.expert_binary = Path(expert_binary).resolve()
        self.expert_sha256 = expert_sha256.strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", self.expert_sha256):
            raise ValueError("configured MT5 expert digest is invalid")
        self.lifecycle = lifecycle
        self.template_lock = template_lock or RLock()
        self.runtime_factory = runtime_factory
        self.process_factory = process_factory

    def for_source_terminal(self, source_terminal: Path) -> "Mt5InstanceRotator":
        """Clone this rotator for a verified candidate template."""

        return type(self)(
            instances_root=self.instances_root,
            secrets_root=self.secrets_root,
            source_terminal=source_terminal,
            expert_binary=self.expert_binary,
            expert_sha256=self.expert_sha256,
            lifecycle=self.lifecycle,
            template_lock=self.template_lock,
            runtime_factory=self.runtime_factory,
            process_factory=self.process_factory,
            secret_store=self.secrets,
        )

    def current_release(self, expected_terminal_sha256: str) -> Mt5TemplateRelease:
        with self.template_lock:
            return Mt5TemplateRelease.from_template(
                self.source_terminal,
                expected_terminal_sha256,
            )

    def _instance_root(self, connection_id: str) -> Path:
        connection_id = canonical_uuid(connection_id)
        # safe_child rejects a UUID-shaped link whose resolved target leaves the
        # configured instances root.  The lstat check additionally rejects an
        # in-root junction instead of operating through its resolved alias.
        try:
            safe_child(self.instances_root, connection_id)
        except ValueError as exc:
            raise Mt5InstanceRotationError("MT5 instance root is unsafe") from exc
        root = self.instances_root / connection_id
        if InstanceProvisioner._is_reparse_point(root) or not root.is_dir():
            raise Mt5InstanceRotationError("MT5 instance root is unsafe")
        return root

    @staticmethod
    def _remove_tree(path: Path) -> None:
        if not path.exists():
            return
        if InstanceProvisioner._is_reparse_point(path) or not path.is_dir():
            raise Mt5InstanceRotationError("MT5 rotation path is unsafe")
        shutil.rmtree(path)

    @staticmethod
    def _release_from_state(state: dict[str, Any]) -> Mt5TemplateRelease | None:
        terminal_sha256 = state.get("terminal_sha256")
        code_manifest_sha256 = state.get("template_code_manifest_sha256")
        if not (
            isinstance(terminal_sha256, str)
            and re.fullmatch(r"[0-9a-f]{64}", terminal_sha256)
            and isinstance(code_manifest_sha256, str)
            and re.fullmatch(r"[0-9a-f]{64}", code_manifest_sha256)
        ):
            return None
        release_id = hashlib.sha256(
            f"{terminal_sha256}:{code_manifest_sha256}".encode("ascii")
        ).hexdigest()
        return Mt5TemplateRelease(
            terminal_sha256,
            code_manifest_sha256,
            release_id,
        )

    @staticmethod
    def _release_from_journal(value: object) -> Mt5TemplateRelease | None:
        if not isinstance(value, dict) or set(value) != {
            "terminal_sha256",
            "code_manifest_sha256",
            "release_id",
        }:
            return None
        terminal_sha256 = value.get("terminal_sha256")
        code_manifest_sha256 = value.get("code_manifest_sha256")
        release_id = value.get("release_id")
        if not (
            isinstance(terminal_sha256, str)
            and re.fullmatch(r"[0-9a-f]{64}", terminal_sha256)
            and isinstance(code_manifest_sha256, str)
            and re.fullmatch(r"[0-9a-f]{64}", code_manifest_sha256)
            and isinstance(release_id, str)
            and re.fullmatch(r"[0-9a-f]{64}", release_id)
        ):
            return None
        expected_release_id = hashlib.sha256(
            f"{terminal_sha256}:{code_manifest_sha256}".encode("ascii")
        ).hexdigest()
        if release_id != expected_release_id:
            return None
        return Mt5TemplateRelease(
            terminal_sha256,
            code_manifest_sha256,
            release_id,
        )

    @staticmethod
    def _validate_terminal_against_state(
        terminal_root: Path,
        state: dict[str, Any],
    ) -> None:
        release = Mt5InstanceRotator._release_from_state(state)
        terminal = terminal_root / "terminal64.exe"
        if release is None:
            raise Mt5InstanceRotationError("MT5 instance release pin is invalid")
        try:
            if (
                InstanceProvisioner._sha256(terminal) != release.terminal_sha256
                or InstanceProvisioner._code_manifest(terminal_root)
                != release.code_manifest_sha256
            ):
                raise Mt5InstanceRotationError("MT5 instance release integrity failed")
            runtime_assets = state.get("runtime_assets_manifest_sha256")
            if runtime_assets is not None and (
                not isinstance(runtime_assets, str)
                or not re.fullmatch(r"[0-9a-f]{64}", runtime_assets)
                or InstanceProvisioner._managed_runtime_assets_manifest(terminal_root)
                != runtime_assets
            ):
                raise Mt5InstanceRotationError("MT5 managed runtime assets are invalid")
        except Mt5InstanceRotationError:
            raise
        except (OSError, ValueError) as exc:
            raise Mt5InstanceRotationError(
                "MT5 instance release integrity failed"
            ) from exc

    @staticmethod
    def _is_verified_vendor_release_state(
        state: dict[str, Any],
        release: Mt5TemplateRelease,
    ) -> bool:
        vendor_update = state.get("vendor_update")
        signer = (
            vendor_update.get("signer_subject")
            if isinstance(vendor_update, dict)
            else None
        )
        return (
            isinstance(vendor_update, dict)
            and vendor_update.get("schema_version") == 1
            and vendor_update.get("terminal_sha256")
            == release.terminal_sha256
            and vendor_update.get("code_manifest_sha256")
            == release.code_manifest_sha256
            and type(vendor_update.get("verified_at_unix_ms")) is int
            and vendor_update["verified_at_unix_ms"] > 0
            and isinstance(signer, str)
            and "CN=MetaQuotes Ltd." in signer
            and "O=MetaQuotes Ltd." in signer
        )

    @classmethod
    def _validate_resumed_release(
        cls,
        root: Path,
        state: dict[str, Any],
        expected: Mt5TemplateRelease | None,
        *,
        allow_verified_vendor_update: bool,
        changed_error: str,
    ) -> None:
        observed = cls._release_from_state(state)
        if observed is None:
            raise Mt5InstanceRotationError(
                "MT5 resumed release pin is invalid"
            )
        if observed != expected and not (
            allow_verified_vendor_update
            and cls._is_verified_vendor_release_state(state, observed)
        ):
            raise Mt5InstanceRotationError(changed_error)
        cls._validate_terminal_against_state(root / "terminal", state)

    @staticmethod
    def _matches_target(
        root: Path,
        state: dict[str, Any],
        target: Mt5TemplateRelease,
    ) -> bool:
        if Mt5InstanceRotator._release_from_state(state) != target:
            return False
        try:
            Mt5InstanceRotator._validate_terminal_against_state(
                root / "terminal",
                state,
            )
        except Mt5InstanceRotationError:
            return False
        return True

    def matches_target(
        self,
        connection_id: str,
        target: Mt5TemplateRelease,
    ) -> bool:
        root = self._instance_root(connection_id)
        return self._matches_target(
            root,
            read_json(root / "state" / "instance.json", {}),
            target,
        )

    def _validate_expert(self) -> None:
        try:
            digest = InstanceProvisioner._sha256(self.expert_binary)
        except OSError as exc:
            raise Mt5InstanceRotationError("MT5 expert is unavailable") from exc
        if digest != self.expert_sha256:
            raise Mt5InstanceRotationError("MT5 expert digest mismatch")

    @staticmethod
    def _validate_status(status: NativeMt5Status) -> None:
        if (
            status.pid <= 0
            or status.heartbeat.get("terminal_connected") is not True
            or status.heartbeat.get("account_trade_allowed") is not False
        ):
            raise Mt5InstanceRotationError("rotated MT5 health check failed")

    @staticmethod
    def _validate_private_data_tree(source: Path) -> None:
        InstanceProvisioner._validate_source_tree(source)
        if any(
            path.is_file() and path.suffix.casefold() in _EXECUTABLE_SUFFIXES
            for path in source.rglob("*")
        ):
            raise Mt5InstanceRotationError(
                "private MT5 data contains executable content"
            )

    @classmethod
    def _copy_directory(cls, source: Path, destination: Path) -> None:
        if not source.exists():
            return
        if InstanceProvisioner._is_reparse_point(source) or not source.is_dir():
            raise Mt5InstanceRotationError("private MT5 directory is unsafe")
        cls._validate_private_data_tree(source)
        parent = destination.parent
        if not parent.exists():
            ancestor = parent.parent
            if InstanceProvisioner._is_reparse_point(ancestor) or not ancestor.is_dir():
                raise Mt5InstanceRotationError("staged MT5 directory is unsafe")
            parent.mkdir()
        if InstanceProvisioner._is_reparse_point(parent) or not parent.is_dir():
            raise Mt5InstanceRotationError("staged MT5 directory is unsafe")
        cls._remove_tree(destination)
        shutil.copytree(source, destination, symlinks=False)
        cls._validate_private_data_tree(destination)

    @staticmethod
    def _copy_config(source_root: Path, destination_root: Path) -> None:
        source = source_root / "Config"
        if not source.exists():
            return
        if InstanceProvisioner._is_reparse_point(source) or not source.is_dir():
            raise Mt5InstanceRotationError("private MT5 config is unsafe")
        destination = destination_root / "Config"
        if not destination.exists():
            if (
                InstanceProvisioner._is_reparse_point(destination_root)
                or not destination_root.is_dir()
            ):
                raise Mt5InstanceRotationError("staged MT5 config is unsafe")
            destination.mkdir()
        if (
            InstanceProvisioner._is_reparse_point(destination)
            or not destination.is_dir()
        ):
            raise Mt5InstanceRotationError("staged MT5 config is unsafe")
        for child in source.iterdir():
            if child.name.casefold() not in _PRESERVED_CONFIG_NAMES:
                continue
            if InstanceProvisioner._is_reparse_point(child) or not child.is_file():
                raise Mt5InstanceRotationError("private MT5 config is unsafe")
            shutil.copy2(child, destination / child.name)

    def _stage_template(
        self,
        root: Path,
        target: Mt5TemplateRelease,
    ) -> _StagedTemplate:
        staging = root / _STAGING_NAME
        template_root = self.source_terminal.parent
        with self.template_lock:
            try:
                InstanceProvisioner._validate_source_tree(template_root)
                manifest_before = InstanceProvisioner._tree_manifest(template_root)
                current = Mt5TemplateRelease.from_template(
                    self.source_terminal,
                    target.terminal_sha256,
                )
                if current != target:
                    raise Mt5InstanceRotationError("MT5 template release changed")
                shutil.copytree(template_root, staging, symlinks=False)
                manifest_after = InstanceProvisioner._tree_manifest(template_root)
                copied_manifest = InstanceProvisioner._tree_manifest(staging)
                if not (manifest_before == manifest_after == copied_manifest):
                    raise Mt5InstanceRotationError(
                        "MT5 template changed during staging"
                    )
                staged_release = Mt5TemplateRelease.from_template(
                    staging / "terminal64.exe",
                    target.terminal_sha256,
                )
                if staged_release != target:
                    raise Mt5InstanceRotationError("staged MT5 release mismatch")
                runtime_assets = InstanceProvisioner._managed_runtime_assets_manifest(
                    staging
                )
                # Flush the large, immutable golden copy while the old account
                # is still running.  The post-stop path only syncs small private
                # files before the two directory renames.
                InstanceProvisioner._sync_tree(staging)
            except Mt5InstanceRotationError:
                raise
            except (OSError, ValueError) as exc:
                raise Mt5InstanceRotationError("MT5 template staging failed") from exc
        return _StagedTemplate(staging, copied_manifest, runtime_assets)

    def _graft_private_state(self, terminal: Path, staging: Path) -> None:
        self._copy_config(terminal, staging)
        for relative in _PRESERVED_CONFIG_DIRECTORIES:
            self._copy_directory(terminal / relative, staging / relative)
        for relative in _PRESERVED_DIRECTORIES:
            self._copy_directory(terminal / relative, staging / relative)

    @staticmethod
    def _sync_private_state(staging: Path) -> None:
        candidates = [staging / "Config"] + [
            staging / relative for relative in _PRESERVED_DIRECTORIES
        ]
        for candidate in candidates:
            if candidate.exists():
                InstanceProvisioner._sync_tree(candidate)
        fsync_directory(staging)

    def _new_instance_state(
        self,
        root: Path,
        staged: _StagedTemplate,
        previous: dict[str, Any],
        target: Mt5TemplateRelease,
    ) -> dict[str, Any]:
        if (
            InstanceProvisioner._sha256(staged.root / "terminal64.exe")
            != target.terminal_sha256
            or InstanceProvisioner._managed_runtime_assets_manifest(staged.root)
            != staged.runtime_assets_manifest_sha256
        ):
            raise Mt5InstanceRotationError("staged MT5 integrity mismatch")
        next_state = dict(previous)
        next_state.update(
            {
                "status": "provisioned",
                "terminal": str(root / "terminal" / "terminal64.exe"),
                "terminal_sha256": target.terminal_sha256,
                "template_manifest_sha256": staged.template_manifest_sha256,
                "template_code_manifest_sha256": target.code_manifest_sha256,
                "runtime_assets_manifest_sha256": (
                    staged.runtime_assets_manifest_sha256
                ),
                "runtime_assets_manifest_version": 1,
                "template_release_id": target.release_id,
                "template_rotated_at_unix_ms": int(time.time() * 1000),
            }
        )
        next_state.pop("vendor_update", None)
        return next_state

    @staticmethod
    def _write_journal(
        path: Path,
        *,
        connection_id: str,
        phase: str,
        previous_state: dict[str, Any],
        target: Mt5TemplateRelease,
        recovery_from: datetime | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "schema_version": _ROTATION_SCHEMA_VERSION,
            "connection_id": connection_id,
            "phase": phase,
            "previous_instance_state": previous_state,
            "target_release": target.as_dict(),
            "updated_at_unix_ms": int(time.time() * 1000),
        }
        if recovery_from is not None:
            if recovery_from.tzinfo is None:
                raise Mt5InstanceRotationError("MT5 recovery cutoff is invalid")
            recovery_from_unix = int(recovery_from.timestamp())
            if recovery_from_unix <= 0:
                raise Mt5InstanceRotationError("MT5 recovery cutoff is invalid")
            payload[_RECOVERY_FROM_UNIX_FIELD] = recovery_from_unix
        atomic_json(
            path,
            payload,
        )

    @staticmethod
    def _recovery_from_journal(journal: dict[str, Any], root: Path) -> datetime:
        raw = journal.get(_RECOVERY_FROM_UNIX_FIELD)
        if raw is None:
            # Backward-compatible recovery for a journal written before the
            # cutoff became part of the crash-safe transaction record.
            return new_only_recovery_from(root)
        if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
            raise Mt5InstanceRotationError("MT5 rotation recovery cutoff is invalid")
        try:
            return datetime.fromtimestamp(raw, timezone.utc)
        except (OverflowError, OSError, ValueError) as exc:
            raise Mt5InstanceRotationError(
                "MT5 rotation recovery cutoff is invalid"
            ) from exc

    @staticmethod
    def _assert_clean_transaction(root: Path) -> None:
        paths = (
            root / "state" / _ROTATION_STATE_NAME,
            root / _STAGING_NAME,
            root / _BACKUP_NAME,
        )
        if any(path.exists() for path in paths):
            raise Mt5InstanceRotationError("unfinished MT5 rotation requires recovery")

    def _rollback_filesystem(
        self,
        root: Path,
        previous_state: dict[str, Any],
    ) -> None:
        terminal = root / "terminal"
        staging = root / _STAGING_NAME
        backup = root / _BACKUP_NAME
        if backup.exists():
            if InstanceProvisioner._is_reparse_point(backup) or not backup.is_dir():
                raise Mt5InstanceRotationError("MT5 rotation backup is unsafe")
            self._validate_terminal_against_state(backup, previous_state)
            self._remove_tree(terminal)
            _replace_directory_with_retry(backup, terminal)
            fsync_directory(root)
        else:
            self._validate_terminal_against_state(terminal, previous_state)
        self._remove_tree(staging)
        atomic_json(root / "state" / "instance.json", previous_state)
        self._validate_terminal_against_state(terminal, previous_state)

    def _adopt(self, root: Path) -> None:
        try:
            self.process_factory(root / "state" / "terminal-process.json").adopt(
                root / "terminal" / "terminal64.exe"
            )
        except AttributeError:
            # Isolated tests may inject a minimal process double.
            return
        except RuntimeError as exc:
            raise Mt5InstanceRotationError("MT5 process adoption failed") from exc

    @staticmethod
    def _configure_runtime(
        runtime: Any,
        callback: VerifiedUpdateCallback | None,
        *,
        required: bool,
    ) -> None:
        if callback is None:
            if required:
                raise Mt5InstanceRotationError(
                    "verified MT5 update callback is required"
                )
            return
        setter = getattr(runtime, "set_verified_vendor_update_callback", None)
        if not callable(setter):
            if required:
                raise Mt5InstanceRotationError(
                    "verified MT5 update callback is unavailable"
                )
            return
        setter(callback, required=required)

    def recover_incomplete(
        self,
        *,
        verified_update_callback: VerifiedUpdateCallback | None = None,
        verified_update_required: bool = False,
    ) -> Mt5RotationRecoveryReport:
        """Recover every journal before generic process reconciliation."""

        recovered: list[str] = []
        failed: list[str] = []
        if not self.instances_root.exists():
            return Mt5RotationRecoveryReport((), ())
        for candidate in sorted(
            self.instances_root.iterdir(), key=lambda value: value.name
        ):
            try:
                connection_id = canonical_uuid(candidate.name)
            except ValueError:
                continue
            try:
                root = self._instance_root(connection_id)
                journal_path = root / "state" / _ROTATION_STATE_NAME
                if not journal_path.exists():
                    continue
                with self.lifecycle.connection(connection_id):
                    journal = read_json(journal_path)
                    previous_state = journal.get("previous_instance_state")
                    target = self._release_from_journal(journal.get("target_release"))
                    phase = journal.get("phase")
                    if (
                        journal.get("schema_version") != _ROTATION_SCHEMA_VERSION
                        or journal.get("connection_id") != connection_id
                        or not isinstance(previous_state, dict)
                        or target is None
                        or phase
                        not in {
                            "preparing",
                            "stopped",
                            "old_moved",
                            "swapped",
                            "committed",
                            "rollback_failed",
                        }
                    ):
                        raise Mt5InstanceRotationError(
                            "MT5 rotation journal is invalid"
                        )
                    backup = root / _BACKUP_NAME
                    if phase == "committed":
                        published_state = read_json(
                            root / "state" / "instance.json", {}
                        )
                        if self._matches_target(root, published_state, target):
                            self._remove_tree(root / _STAGING_NAME)
                            self._remove_tree(backup)
                            journal_path.unlink(missing_ok=True)
                            recovered.append(connection_id)
                            continue
                        if not backup.exists():
                            raise Mt5InstanceRotationError(
                                "committed MT5 rotation is inconsistent"
                            )

                    recovery_from = self._recovery_from_journal(journal, root)
                    self._validate_expert()
                    terminal = root / "terminal" / "terminal64.exe"
                    if terminal.parent.exists():
                        try:
                            stopped = self.process_factory(
                                root / "state" / "terminal-process.json"
                            ).cleanup_path(terminal)
                        except AttributeError:
                            stopped = True
                        if not stopped:
                            raise Mt5InstanceRotationError(
                                "MT5 rotation recovery could not stop terminal"
                            )
                    self._rollback_filesystem(root, previous_state)
                    self._write_journal(
                        journal_path,
                        connection_id=connection_id,
                        phase="rollback_failed",
                        previous_state=previous_state,
                        target=target,
                        recovery_from=recovery_from,
                    )
                    try:
                        login = int(self.secrets.read(connection_id, "mt5_login"))
                        server = self.secrets.read(connection_id, "mt5_server")
                    except Exception as exc:
                        raise Mt5InstanceRotationError(
                            "MT5 recovery identity is unavailable"
                        ) from exc
                    runtime = self.runtime_factory(root, connection_id)
                    self._configure_runtime(
                        runtime,
                        verified_update_callback,
                        required=verified_update_required,
                    )
                    status = runtime.resume(
                        login=login,
                        server=server,
                        expert_binary=self.expert_binary,
                        history_mode="new_only",
                        history_from=recovery_from,
                    )
                    self._validate_status(status)
                    restored_state = read_json(
                        root / "state" / "instance.json",
                        {},
                    )
                    self._validate_resumed_release(
                        root,
                        restored_state,
                        self._release_from_state(previous_state),
                        allow_verified_vendor_update=(
                            verified_update_required
                            and verified_update_callback is not None
                        ),
                        changed_error=(
                            "recovered MT5 release changed during restart"
                        ),
                    )
                    self._adopt(root)
                    journal_path.unlink(missing_ok=True)
                    recovered.append(connection_id)
            except Exception as exc:
                failed.append(connection_id)
                logger.error(
                    "MT5 rotation recovery failed (connection=%s, error=%s)",
                    connection_id,
                    type(exc).__name__,
                )
        return Mt5RotationRecoveryReport(tuple(recovered), tuple(failed))

    def rotate_one(
        self,
        connection_id: str,
        target: Mt5TemplateRelease,
        *,
        expected_source: Mt5TemplateRelease | None = None,
        force: bool = False,
        history_from: datetime | None = None,
        verified_update_callback: VerifiedUpdateCallback | None = None,
        verified_update_required: bool = False,
    ) -> bool:
        connection_id = canonical_uuid(connection_id)
        root = self._instance_root(connection_id)
        with self.lifecycle.connection(connection_id):
            self._assert_clean_transaction(root)
            self._validate_expert()
            state_path = root / "state" / "instance.json"
            previous_state = read_json(state_path)
            if (
                previous_state.get("connection_id") != connection_id
                or previous_state.get("status") != "provisioned"
            ):
                raise Mt5InstanceRotationError("MT5 instance state is invalid")
            self._validate_terminal_against_state(root / "terminal", previous_state)
            if (
                expected_source is not None
                and self._release_from_state(previous_state) != expected_source
            ):
                raise Mt5InstanceRotationError(
                    "MT5 instance source release changed"
                )
            if not force and self._matches_target(root, previous_state, target):
                return False
            try:
                login = int(self.secrets.read(connection_id, "mt5_login"))
                server = self.secrets.read(connection_id, "mt5_server")
            except Exception as exc:
                raise Mt5InstanceRotationError(
                    "MT5 stored identity is unavailable"
                ) from exc
            if login <= 0 or not server or server != server.strip():
                raise Mt5InstanceRotationError("MT5 stored identity is invalid")
            runtime = self.runtime_factory(root, connection_id)
            self._configure_runtime(
                runtime,
                verified_update_callback,
                required=verified_update_required,
            )
            if history_from is not None and (
                not isinstance(history_from, datetime)
                or history_from.tzinfo is None
            ):
                raise Mt5InstanceRotationError(
                    "MT5 recovery cutoff is invalid"
                )
            recovery_from = history_from or new_only_recovery_from(root)
            journal_path = root / "state" / _ROTATION_STATE_NAME
            self._write_journal(
                journal_path,
                connection_id=connection_id,
                phase="preparing",
                previous_state=previous_state,
                target=target,
                recovery_from=recovery_from,
            )
            try:
                staged = self._stage_template(root, target)
            except Exception:
                try:
                    self._remove_tree(root / _STAGING_NAME)
                    journal_path.unlink(missing_ok=True)
                except Exception:
                    pass
                raise
            if history_from is None:
                recovery_from = new_only_recovery_from(root)
            self._write_journal(
                journal_path,
                connection_id=connection_id,
                phase="preparing",
                previous_state=previous_state,
                target=target,
                recovery_from=recovery_from,
            )
            if not runtime.stop():
                self._remove_tree(staged.root)
                journal_path.unlink(missing_ok=True)
                raise Mt5InstanceRotationError("MT5 terminal stop failed")
            self._write_journal(
                journal_path,
                connection_id=connection_id,
                phase="stopped",
                previous_state=previous_state,
                target=target,
                recovery_from=recovery_from,
            )
            committed = False
            try:
                terminal = root / "terminal"
                self._graft_private_state(terminal, staged.root)
                next_state = self._new_instance_state(
                    root,
                    staged,
                    previous_state,
                    target,
                )
                self._sync_private_state(staged.root)
                backup = root / _BACKUP_NAME
                _replace_directory_with_retry(terminal, backup)
                self._write_journal(
                    journal_path,
                    connection_id=connection_id,
                    phase="old_moved",
                    previous_state=previous_state,
                    target=target,
                    recovery_from=recovery_from,
                )
                _replace_directory_with_retry(staged.root, terminal)
                fsync_directory(root)
                self._write_journal(
                    journal_path,
                    connection_id=connection_id,
                    phase="swapped",
                    previous_state=previous_state,
                    target=target,
                    recovery_from=recovery_from,
                )
                atomic_json(state_path, next_state)
                status = runtime.resume(
                    login=login,
                    server=server,
                    expert_binary=self.expert_binary,
                    history_mode="new_only",
                    history_from=recovery_from,
                )
                self._validate_status(status)
                published_state = read_json(state_path)
                if not self._matches_target(root, published_state, target):
                    raise Mt5InstanceRotationError(
                        "rotated MT5 release changed during restart"
                    )
                self._adopt(root)
                self._write_journal(
                    journal_path,
                    connection_id=connection_id,
                    phase="committed",
                    previous_state=previous_state,
                    target=target,
                    recovery_from=recovery_from,
                )
                committed = True
            except Exception as exc:
                if committed:
                    raise
                try:
                    if not runtime.stop():
                        raise Mt5InstanceRotationError(
                            "MT5 rollback could not stop terminal"
                        )
                    self._rollback_filesystem(root, previous_state)
                    rollback_status = runtime.resume(
                        login=login,
                        server=server,
                        expert_binary=self.expert_binary,
                        history_mode="new_only",
                        history_from=recovery_from,
                    )
                    self._validate_status(rollback_status)
                    restored_state = read_json(state_path)
                    self._validate_resumed_release(
                        root,
                        restored_state,
                        self._release_from_state(previous_state),
                        allow_verified_vendor_update=(
                            verified_update_required
                            and verified_update_callback is not None
                        ),
                        changed_error=(
                            "MT5 rollback release changed during restart"
                        ),
                    )
                    self._adopt(root)
                    journal_path.unlink(missing_ok=True)
                except Exception as rollback_exc:
                    self._write_journal(
                        journal_path,
                        connection_id=connection_id,
                        phase="rollback_failed",
                        previous_state=previous_state,
                        target=target,
                        recovery_from=recovery_from,
                    )
                    raise Mt5InstanceRotationError(
                        "MT5 instance rotation and rollback failed"
                    ) from rollback_exc
                raise Mt5InstanceRotationError(
                    "MT5 instance rotation failed and was rolled back"
                ) from exc

            # Commit is the point of no return.  Cleanup failures deliberately
            # leave a committed journal for recovery; they must never restore
            # the old state pin over the successfully verified new files.
            try:
                self._remove_tree(root / _BACKUP_NAME)
                journal_path.unlink(missing_ok=True)
            except Exception:
                logger.warning(
                    "committed MT5 rotation cleanup deferred (connection=%s)",
                    connection_id,
                )
            logger.info(
                "rotated MT5 instance %s to release %s",
                connection_id,
                target.release_id,
            )
            return True

    def rotate_all(
        self,
        target: Mt5TemplateRelease,
        stop_event: Event | None = None,
        *,
        verified_update_callback: VerifiedUpdateCallback | None = None,
        verified_update_required: bool = False,
    ) -> Mt5FleetRotationReport:
        migrated: list[str] = []
        already_current: list[str] = []
        failed: list[str] = []
        if not self.instances_root.exists():
            return Mt5FleetRotationReport(target, (), ())
        for candidate in sorted(
            self.instances_root.iterdir(), key=lambda value: value.name
        ):
            try:
                connection_id = canonical_uuid(candidate.name)
            except ValueError:
                continue
            if stop_event is not None and stop_event.is_set():
                raise Mt5InstanceRotationError("MT5 fleet rotation was interrupted")
            try:
                root = self._instance_root(connection_id)
                state = read_json(root / "state" / "instance.json", {})
                if state.get("status") != "provisioned":
                    continue
                if not (root / "terminal" / "terminal64.exe").is_file():
                    raise Mt5InstanceRotationError(
                        "provisioned MT5 terminal is missing"
                    )
                if self._matches_target(root, state, target):
                    already_current.append(connection_id)
                    continue
                if self.rotate_one(
                    connection_id,
                    target,
                    verified_update_callback=verified_update_callback,
                    verified_update_required=verified_update_required,
                ):
                    migrated.append(connection_id)
            except Exception:
                failed.append(connection_id)
                logger.exception(
                    "MT5 fleet member rotation failed (connection=%s)",
                    connection_id,
                )
        if failed:
            raise Mt5InstanceRotationError(
                f"MT5 fleet rotation failed for {len(failed)} connection(s)",
                failed=tuple(failed),
            )
        return Mt5FleetRotationReport(
            target,
            tuple(migrated),
            tuple(already_current),
        )
