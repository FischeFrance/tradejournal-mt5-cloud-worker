"""Verified, service-owned rotation of the credential-free MT5 template."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Callable

from worker.atomic_file import durable_replace, fsync_directory

from ..state_store import atomic_json, read_json
from .mt5_instance import (
    InstanceProvisioner,
    MT5_GENERATED_EXAMPLE_DIRS,
    _MANAGED_RUNTIME_ASSETS,
)
from .mt5_update_store import (
    PENDING_UPDATE_RECEIPT_NAME,
    UPDATER_ONLY_CONFIG_BYTES,
    UPDATER_ONLY_CONFIG_NAME,
    VERIFIED_UPDATE_METADATA_NAME,
)
from .secret_store import WindowsSecretStore


logger = logging.getLogger(__name__)

_MARKER_NAME = ".tradejournal-vendor-update.json"
_WINDOWS_TRANSIENT_DIRECTORY_MOVE_ERRORS = frozenset((5, 32, 33))
_DIRECTORY_MOVE_ATTEMPTS = 5
_DIRECTORY_MOVE_BASE_DELAY_SECONDS = 0.1
_VENDOR_PAYLOAD_NAME = re.compile(r"[A-Za-z0-9_-]+\.[0-9]{4,6}")
_MAX_VENDOR_BUNDLE_FILE_BYTES = 512 * 1024 * 1024
_MAX_VENDOR_BUNDLE_BYTES = 1024 * 1024 * 1024
_PRIVATE_DIRECTORIES = (
    Path("Bases"),
    Path("Logs"),
    Path("MQL5/Files"),
    Path("MQL5/Logs"),
    Path("Tester/cache"),
    Path("Tester/logs"),
)
_PRIVATE_CONFIG_NAMES = frozenset(
    {
        "accounts.dat",
        "accounts.ini",
        "certificates",
        "community.ini",
        "signals.ini",
    }
)


def _restrict_private_acl(path: Path) -> None:
    if os.name == "nt":
        WindowsSecretStore.restrict_acl(path)


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


class Mt5TemplateError(RuntimeError):
    """A sanitized failure while validating or rotating the golden template."""


class Mt5TemplateRecoveryRequired(Mt5TemplateError):
    """A rotation failed in an ambiguous state that only recovery may mutate."""


@dataclass(frozen=True)
class PreparedMt5Template:
    """A fully verified candidate that has not replaced the golden yet."""

    root: Path
    source_terminal_sha256: str
    source_code_manifest_sha256: str
    target_terminal_sha256: str
    target_code_manifest_sha256: str
    candidate_manifest_sha256: str
    signer_subject: str


class Mt5TemplateManager:
    """Maintain an Authenticode-verified MT5 template without touching live roots.

    ``configured_sha256`` remains the operator-controlled trust anchor.  A
    successful MetaQuotes LiveUpdate may rotate the active terminal digest;
    that rotation is recorded *inside* the atomically published template so a
    service restart can validate it without weakening the original anchor.
    """

    def __init__(
        self,
        source_terminal: Path,
        configured_sha256: str,
        *,
        lock: RLock | None = None,
        process_launcher: Callable[..., subprocess.Popen] = subprocess.Popen,
    ) -> None:
        self.source_terminal = Path(source_terminal).resolve()
        self.template_root = self.source_terminal.parent
        self.configured_sha256 = configured_sha256.strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", self.configured_sha256):
            raise ValueError("configured MT5 template digest is invalid")
        self.lock = lock or RLock()
        self._process_launcher = process_launcher
        self._current_sha256 = ""
        self._prepared: PreparedMt5Template | None = None
        self._recovery_required = False

    @property
    def current_sha256(self) -> str:
        with self.lock:
            if self._recovery_required:
                return self.recover_interrupted_rotation()
            if not self._current_sha256:
                self._current_sha256 = self._validate_current_locked()
            return self._current_sha256

    @staticmethod
    def _verify_metaquotes_signature(path: Path) -> str:
        script = (
            "$s=Get-AuthenticodeSignature -LiteralPath "
            "$env:TRADEJOURNAL_SIGNATURE_PATH;"
            "[pscustomobject]@{Status=[string]$s.Status;"
            "Subject=[string]$s.SignerCertificate.Subject}|ConvertTo-Json -Compress"
        )
        environment = os.environ.copy()
        environment["TRADEJOURNAL_SIGNATURE_PATH"] = str(path)
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
        try:
            identity = json.loads(completed.stdout.strip())
        except (AttributeError, json.JSONDecodeError) as exc:
            raise Mt5TemplateError("MT5 template signature is invalid") from exc
        subject = identity.get("Subject") if isinstance(identity, dict) else None
        if (
            completed.returncode != 0
            or not isinstance(identity, dict)
            or identity.get("Status") != "Valid"
            or not isinstance(subject, str)
            or "CN=MetaQuotes Ltd." not in subject
            or "O=MetaQuotes Ltd." not in subject
        ):
            raise Mt5TemplateError("MT5 template signature is invalid")
        return subject

    def _recover_interrupted_rotation_locked(self) -> None:
        parent = self.template_root.parent
        staging = parent / f".{self.template_root.name}.vendor-staging"
        backup = parent / f".{self.template_root.name}.vendor-backup"
        working = parent / f".{self.template_root.name}.vendor-working"
        recovery_roots = tuple(
            path for path in (working, staging, backup) if path.exists()
        )
        for path in recovery_roots:
            if InstanceProvisioner._is_reparse_point(path) or not path.is_dir():
                raise Mt5TemplateError("MT5 template rotation path is unsafe")
        interrupted_roots = tuple(
            path for path in (working, staging) if path.exists()
        )
        if interrupted_roots:
            self._stop_interrupted_rotation_processes(
                (
                    working / "terminal64.exe",
                    staging / "terminal64.exe",
                )
            )
        if not self.template_root.exists() and backup.is_dir():
            self._validate_template_root(backup)
            _replace_directory_with_retry(backup, self.template_root)
            fsync_directory(parent)
        elif self.template_root.exists() and backup.is_dir():
            try:
                self._validate_template_root(
                    self.template_root,
                    remove_obsolete_marker=True,
                )
            except Mt5TemplateError as candidate_exc:
                try:
                    self._validate_template_root(backup)
                    self._stop_interrupted_rotation_processes(
                        (
                            self.template_root / "terminal64.exe",
                            backup / "terminal64.exe",
                        )
                    )
                    self._remove_tree(self.template_root)
                    _replace_directory_with_retry(backup, self.template_root)
                    fsync_directory(parent)
                except Exception as rollback_exc:
                    raise Mt5TemplateError(
                        "MT5 template startup rollback failed"
                    ) from rollback_exc
                logger.warning(
                    "restored the previous MT5 template after invalid publication: %s",
                    type(candidate_exc).__name__,
                )
            else:
                # Only a fully validated published candidate makes the old
                # golden redundant. A crash between the directory swaps can
                # therefore never destroy the last known-good template.
                self._remove_tree(backup)
        if staging.exists():
            self._remove_tree(staging)
        if working.exists():
            self._remove_tree(working)

    @staticmethod
    def _remove_tree(path: Path) -> None:
        if not path.exists():
            return
        if InstanceProvisioner._is_reparse_point(path) or not path.is_dir():
            raise Mt5TemplateError("MT5 template rotation path is unsafe")
        shutil.rmtree(path)

    def _validate_template_root(
        self,
        root: Path,
        *,
        remove_obsolete_marker: bool = False,
    ) -> str:
        terminal = root / "terminal64.exe"
        if (
            InstanceProvisioner._is_reparse_point(root)
            or not root.is_dir()
            or not terminal.is_file()
        ):
            raise Mt5TemplateError("MT5 template is unavailable")
        try:
            InstanceProvisioner._validate_source_tree(root)
            digest = InstanceProvisioner._sha256(terminal)
        except (OSError, ValueError) as exc:
            raise Mt5TemplateError("MT5 template integrity is invalid") from exc
        marker_path = root / _MARKER_NAME
        marker = read_json(marker_path, {}) if marker_path.is_file() else {}
        if not marker:
            if marker_path.exists():
                raise Mt5TemplateError("MT5 template rotation record is invalid")
            if digest != self.configured_sha256:
                raise Mt5TemplateError("MT5 template digest mismatch")
            return digest
        if marker.get("base_terminal_sha256") != self.configured_sha256:
            # A deliberate operator deployment may replace both the template
            # and its configured anchor.  In that exact case the old rotation
            # marker is obsolete and the new binary becomes the fresh base.
            if digest == self.configured_sha256:
                if remove_obsolete_marker:
                    marker_path.unlink()
                return digest
            raise Mt5TemplateError("MT5 template rotation anchor mismatch")
        schema_version = marker.get("schema_version")
        if (
            schema_version not in (1, 2)
            or marker.get("terminal_sha256") != digest
            or not isinstance(marker.get("verified_at_unix_ms"), int)
            or marker["verified_at_unix_ms"] <= 0
            or "MetaQuotes Ltd." not in str(marker.get("signer_subject", ""))
        ):
            raise Mt5TemplateError("MT5 template rotation record is invalid")
        if schema_version == 2:
            try:
                code_manifest = InstanceProvisioner._code_manifest(root)
            except (OSError, ValueError) as exc:
                raise Mt5TemplateError("MT5 template integrity is invalid") from exc
            if marker.get("code_manifest_sha256") != code_manifest:
                raise Mt5TemplateError("MT5 template rotation record is invalid")
        signer = self._verify_metaquotes_signature(terminal)
        if signer != marker["signer_subject"]:
            raise Mt5TemplateError("MT5 template signer mismatch")
        return digest

    def _validate_current_locked(self) -> str:
        # A prepared candidate intentionally occupies vendor-staging while its
        # canaries are being tested.  After a process restart ``_prepared`` is
        # empty and the same path is correctly treated as an interrupted,
        # unpublished candidate and removed.
        if self._prepared is None:
            self._recover_interrupted_rotation_locked()
        return self._validate_template_root(
            self.template_root,
            remove_obsolete_marker=True,
        )

    def recover_interrupted_rotation(self) -> str:
        """Resolve crash/rollback debris and refresh the in-process digest cache."""

        with self.lock:
            # A process-local candidate is never authoritative after the
            # caller reports an ambiguous publication or cleanup. Recovery
            # first proves exact updater processes are gone, then chooses the
            # valid golden/backup and only afterwards removes private debris.
            self._prepared = None
            self._current_sha256 = ""
            self._recover_interrupted_rotation_locked()
            digest = self._validate_template_root(
                self.template_root,
                remove_obsolete_marker=True,
            )
            self._current_sha256 = digest
            self._recovery_required = False
            return digest

    def validate_current_quiesced(self) -> str:
        """Validate the golden without recovering or deleting deployment state."""

        with self.lock:
            parent = self.template_root.parent
            if self._prepared is not None or self._recovery_required or any(
                (
                    parent / f".{self.template_root.name}.{suffix}"
                ).exists()
                for suffix in (
                    "vendor-staging",
                    "vendor-backup",
                    "vendor-working",
                )
            ):
                raise Mt5TemplateRecoveryRequired(
                    "MT5 template rotation requires recovery"
                )
            digest = self._validate_template_root(self.template_root)
            self._current_sha256 = digest
            return digest

    @staticmethod
    def _code_manifest_with_digest_override(
        root: Path,
        relative_override: Path,
        digest_override: str,
    ) -> str:
        InstanceProvisioner._validate_source_tree(root)
        entries: list[str] = []
        override_name = relative_override.as_posix().casefold()
        override_found = False
        for path in sorted(root.rglob("*"), key=lambda value: value.as_posix()):
            if not path.is_file() or path.suffix.casefold() not in {
                ".dll",
                ".exe",
                ".ex5",
            }:
                continue
            relative = path.relative_to(root).as_posix()
            digest = InstanceProvisioner._sha256(path)
            if relative.casefold() == override_name:
                digest = digest_override
                override_found = True
            entries.append(f"{relative}:{digest}")
        if not entries or not override_found:
            raise ValueError("managed MT5 asset is unavailable")
        return hashlib.sha256("\n".join(entries).encode("utf-8")).hexdigest()

    def reseal_managed_code_deployment(
        self,
        previous_expert: Path,
        expected_previous_expert_sha256: str,
        expected_new_expert_sha256: str,
    ) -> str:
        """Atomically reseal one operator-approved Bridge replacement.

        The service must be stopped. For schema-2 rotated templates, replacing
        the old Bridge digest in the *current* tree must reconstruct the exact
        previously pinned code manifest; this proves no companion executable
        changed alongside the deployment.
        """

        previous_digest = self._required_digest(
            expected_previous_expert_sha256,
            "previous expert",
        )
        next_digest = self._required_digest(
            expected_new_expert_sha256,
            "new expert",
        )
        if previous_digest is None or next_digest is None:
            raise Mt5TemplateError("MT5 managed deployment binding is incomplete")
        previous_expert = Path(previous_expert).resolve()
        managed_relative = _MANAGED_RUNTIME_ASSETS[0]
        managed_expert = self.template_root / managed_relative
        with self.lock:
            if self._prepared is not None or self._recovery_required:
                raise Mt5TemplateRecoveryRequired(
                    "MT5 template rotation requires recovery"
                )
            if (
                InstanceProvisioner._is_reparse_point(previous_expert)
                or not previous_expert.is_file()
                or InstanceProvisioner._is_reparse_point(managed_expert)
                or not managed_expert.is_file()
            ):
                raise Mt5TemplateError("MT5 managed deployment asset is unsafe")
            if (
                InstanceProvisioner._sha256(previous_expert) != previous_digest
                or InstanceProvisioner._sha256(managed_expert) != next_digest
            ):
                raise Mt5TemplateError("MT5 managed deployment digest mismatch")
            terminal = self.template_root / "terminal64.exe"
            terminal_digest = InstanceProvisioner._sha256(terminal)
            current_code = InstanceProvisioner._code_manifest(self.template_root)
            previous_code = self._code_manifest_with_digest_override(
                self.template_root,
                managed_relative,
                previous_digest,
            )
            marker_path = self.template_root / _MARKER_NAME
            marker = read_json(marker_path, {}) if marker_path.is_file() else {}
            if marker:
                if (
                    marker.get("schema_version") not in (1, 2)
                    or marker.get("base_terminal_sha256")
                    != self.configured_sha256
                    or marker.get("terminal_sha256") != terminal_digest
                    or not isinstance(marker.get("verified_at_unix_ms"), int)
                    or marker["verified_at_unix_ms"] <= 0
                    or "MetaQuotes Ltd."
                    not in str(marker.get("signer_subject", ""))
                ):
                    raise Mt5TemplateError(
                        "MT5 template rotation record is invalid"
                    )
                signer = self._verify_metaquotes_signature(terminal)
                if signer != marker.get("signer_subject"):
                    raise Mt5TemplateError("MT5 template signer mismatch")
                if (
                    marker.get("schema_version") == 2
                    and marker.get("code_manifest_sha256") == current_code
                ):
                    # The SYSTEM helper can lose its result after the atomic
                    # marker write.  Replaying the same old/new digest-bound
                    # request is an idempotent success, never an invitation to
                    # weaken the marker or rewrite its ACL as another user.
                    self._current_sha256 = terminal_digest
                    return current_code
                if (
                    marker.get("schema_version") == 2
                    and marker.get("code_manifest_sha256") != previous_code
                ):
                    raise Mt5TemplateError(
                        "MT5 managed deployment changed companion code"
                    )
                previous_terminal = marker.get(
                    "previous_terminal_sha256",
                    terminal_digest,
                )
            else:
                if terminal_digest != self.configured_sha256:
                    raise Mt5TemplateError("MT5 template digest mismatch")
                signer = self._verify_metaquotes_signature(terminal)
                previous_terminal = terminal_digest
            atomic_json(
                marker_path,
                {
                    "schema_version": 2,
                    "base_terminal_sha256": self.configured_sha256,
                    "previous_terminal_sha256": previous_terminal,
                    "terminal_sha256": terminal_digest,
                    "code_manifest_sha256": current_code,
                    "signer_subject": signer,
                    "verified_at_unix_ms": int(time.time() * 1000),
                },
            )
            WindowsSecretStore.restrict_acl(marker_path)
            self._current_sha256 = terminal_digest
            return current_code

    @staticmethod
    def _sanitize_template(root: Path) -> None:
        for relative in _PRIVATE_DIRECTORIES:
            path = root / relative
            if path.exists():
                if InstanceProvisioner._is_reparse_point(path) or not path.is_dir():
                    raise Mt5TemplateError("updated MT5 template contains unsafe data")
                shutil.rmtree(path)
        config = root / "Config"
        if config.exists():
            if InstanceProvisioner._is_reparse_point(config) or not config.is_dir():
                raise Mt5TemplateError("updated MT5 template config is unsafe")
            for child in tuple(config.iterdir()):
                if child.name.casefold() not in _PRIVATE_CONFIG_NAMES:
                    continue
                if child.is_dir():
                    Mt5TemplateManager._remove_tree(child)
                elif child.is_file() and not InstanceProvisioner._is_reparse_point(
                    child
                ):
                    child.unlink()
                else:
                    raise Mt5TemplateError("updated MT5 template config is unsafe")
        for relative in MT5_GENERATED_EXAMPLE_DIRS:
            path = root / relative
            if not path.exists():
                continue
            if InstanceProvisioner._is_reparse_point(path) or not path.is_dir():
                raise Mt5TemplateError(
                    "updated MT5 template contains unsafe generated examples"
                )
            try:
                InstanceProvisioner._validate_source_tree(path)
            except (OSError, ValueError) as exc:
                raise Mt5TemplateError(
                    "updated MT5 template contains unsafe generated examples"
                ) from exc
            shutil.rmtree(path)

    @staticmethod
    def _stop_staged_terminal(
        terminal: Path,
        updater: Path | None = None,
    ) -> None:
        executables = (terminal,) if updater is None else (terminal, updater)
        Mt5TemplateManager._stop_interrupted_rotation_processes(executables)

    @staticmethod
    def _stop_interrupted_rotation_processes(
        executables: tuple[Path, ...],
    ) -> None:
        """Stop exact updater/candidate processes before deleting crash debris."""

        try:
            import psutil
        except ImportError as exc:
            raise Mt5TemplateError(
                "MT5 template recovery process scan failed"
            ) from exc

        expected = {
            os.path.normcase(os.fspath(executable.resolve()))
            for executable in executables
        }

        def exact_processes() -> list[object]:
            matches: list[object] = []
            for process in psutil.process_iter(("pid", "exe")):
                try:
                    executable = process.info.get("exe")
                    if executable and os.path.normcase(
                        os.fspath(Path(str(executable)).resolve())
                    ) in expected:
                        matches.append(process)
                except psutil.NoSuchProcess:
                    continue
                except (psutil.AccessDenied, OSError, ValueError) as exc:
                    raise Mt5TemplateError(
                        "MT5 template recovery process scan failed"
                    ) from exc
            return matches

        try:
            quiet_scans = 0
            # Require three consecutive empty scans. This covers the common
            # LiveUpdate pattern where the updater exits just before its child
            # terminal becomes visible, while remaining strictly bounded.
            for _attempt in range(16):
                controlled = []
                for process in exact_processes():
                    try:
                        process.terminate()
                        controlled.append(process)
                    except psutil.NoSuchProcess:
                        continue
                if controlled:
                    quiet_scans = 0
                    _, alive = psutil.wait_procs(controlled, timeout=15.0)
                    killed = []
                    for process in alive:
                        try:
                            process.kill()
                            killed.append(process)
                        except psutil.NoSuchProcess:
                            continue
                    if killed:
                        _, still_alive = psutil.wait_procs(killed, timeout=5.0)
                        if still_alive:
                            raise Mt5TemplateError(
                                "MT5 template recovery process stop failed"
                            )
                    continue
                quiet_scans += 1
                if quiet_scans >= 3:
                    return
                time.sleep(0.1)
            raise Mt5TemplateError(
                "MT5 template recovery process stop failed"
            )
        except Mt5TemplateError:
            raise
        except Exception as exc:
            raise Mt5TemplateError(
                "MT5 template recovery process stop failed"
            ) from exc

    @staticmethod
    def _required_digest(value: str | None, label: str) -> str | None:
        if value is None:
            return None
        digest = value.strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise Mt5TemplateError(f"{label} digest is invalid")
        return digest

    @classmethod
    def _copy_update_working_set(
        cls,
        bundle_root: Path,
        updater: Path,
        config: Path,
        working: Path,
    ) -> tuple[Path, Path]:
        """Copy only updater inputs so a consumable receipt stays immutable."""

        if updater.name.casefold() != "terminal64.exe":
            raise Mt5TemplateError("MT5 update bundle is unsafe")
        working.mkdir()
        _restrict_private_acl(working)
        total_size = 0
        copied_names: set[str] = set()
        ignored_metadata = {
            PENDING_UPDATE_RECEIPT_NAME,
            VERIFIED_UPDATE_METADATA_NAME,
        }
        try:
            for child in bundle_root.iterdir():
                if child.name in ignored_metadata:
                    if (
                        InstanceProvisioner._is_reparse_point(child)
                        or not child.is_file()
                    ):
                        raise Mt5TemplateError("MT5 update bundle is unsafe")
                    continue
                if child.name == "temp":
                    if (
                        InstanceProvisioner._is_reparse_point(child)
                        or not child.is_dir()
                        or any(child.iterdir())
                    ):
                        raise Mt5TemplateError("MT5 update bundle is unsafe")
                    continue
                if child.name not in {updater.name, config.name} and not (
                    _VENDOR_PAYLOAD_NAME.fullmatch(child.name)
                ):
                    raise Mt5TemplateError("MT5 update bundle is unsafe")
                if (
                    InstanceProvisioner._is_reparse_point(child)
                    or not child.is_file()
                ):
                    raise Mt5TemplateError("MT5 update bundle is unsafe")
                size = child.stat().st_size
                if size <= 0 or size > _MAX_VENDOR_BUNDLE_FILE_BYTES:
                    raise Mt5TemplateError("MT5 update bundle size is invalid")
                total_size += size
                if total_size > _MAX_VENDOR_BUNDLE_BYTES:
                    raise Mt5TemplateError("MT5 update bundle size is invalid")
                digest = InstanceProvisioner._sha256(child)
                destination = working / child.name
                shutil.copy2(child, destination)
                if (
                    destination.stat().st_size != size
                    or InstanceProvisioner._sha256(destination) != digest
                ):
                    raise Mt5TemplateError("MT5 update bundle copy failed")
                _restrict_private_acl(destination)
                copied_names.add(child.name)
            if not {updater.name, config.name}.issubset(copied_names):
                raise Mt5TemplateError("MT5 update bundle is incomplete")
            temp = working / "temp"
            temp.mkdir()
            _restrict_private_acl(temp)
            InstanceProvisioner._sync_tree(working)
            return working / updater.name, working / config.name
        except Exception:
            cls._remove_tree(working)
            raise

    def prepare_verified_distribution(
        self,
        distribution_root: Path,
        *,
        expected_terminal_sha256: str,
        expected_distribution_manifest_sha256: str,
        cancel_check: Callable[[], None] | None = None,
    ) -> PreparedMt5Template:
        """Build an unpublished golden candidate from a public distribution.

        The official web installer produces a vendor-only tree, whereas every
        TradeJournal template must contain the three pinned bridge assets.  A
        fresh distribution is therefore copied into the normal private
        staging path, stripped of private/default data, and grafted with the
        *current golden's* managed assets before the candidate is sealed.
        Nothing in this method publishes the candidate or touches a live
        account; broker canaries remain the mandatory next gate.
        """

        # Keep the lexical path for the reparse-point check. ``resolve()``
        # here would follow a junction first and could hide the unsafe source
        # that the public-release cache is required to reject.
        distribution_root = Path(
            os.path.abspath(os.fspath(distribution_root))
        )
        expected_terminal = self._required_digest(
            expected_terminal_sha256,
            "public terminal",
        )
        expected_distribution = self._required_digest(
            expected_distribution_manifest_sha256,
            "public distribution manifest",
        )
        if expected_terminal is None or expected_distribution is None:
            raise Mt5TemplateError("MT5 public distribution binding is incomplete")

        with self.lock:
            if self._recovery_required:
                raise Mt5TemplateRecoveryRequired(
                    "MT5 template rotation requires recovery"
                )
            if self._prepared is not None:
                raise Mt5TemplateError("MT5 template candidate already exists")
            current = self._validate_current_locked()
            try:
                current_code = InstanceProvisioner._code_manifest(
                    self.template_root
                )
                managed_assets = (
                    InstanceProvisioner._managed_runtime_assets_manifest(
                        self.template_root
                    )
                )
                if (
                    InstanceProvisioner._is_reparse_point(distribution_root)
                    or not distribution_root.is_dir()
                    or distribution_root == self.template_root
                ):
                    raise ValueError("public distribution root invalid")
                InstanceProvisioner._validate_source_tree(distribution_root)
                distribution_terminal = distribution_root / "terminal64.exe"
                if (
                    InstanceProvisioner._is_reparse_point(distribution_terminal)
                    or not distribution_terminal.is_file()
                    or InstanceProvisioner._sha256(distribution_terminal)
                    != expected_terminal
                    or InstanceProvisioner._tree_manifest(distribution_root)
                    != expected_distribution
                ):
                    raise ValueError("public distribution binding mismatch")
                signer = self._verify_metaquotes_signature(distribution_terminal)
            except Mt5TemplateError:
                raise
            except (OSError, ValueError) as exc:
                raise Mt5TemplateError(
                    "MT5 public distribution is invalid"
                ) from exc

            parent = self.template_root.parent
            staging = parent / f".{self.template_root.name}.vendor-staging"
            backup = parent / f".{self.template_root.name}.vendor-backup"
            working = parent / f".{self.template_root.name}.vendor-working"
            self._remove_tree(staging)
            self._remove_tree(backup)
            self._remove_tree(working)
            try:
                if cancel_check is not None:
                    cancel_check()
                manifest_before = InstanceProvisioner._tree_manifest(
                    distribution_root
                )
                shutil.copytree(distribution_root, staging, symlinks=False)
                if cancel_check is not None:
                    cancel_check()
                if (
                    InstanceProvisioner._tree_manifest(distribution_root)
                    != manifest_before
                    or InstanceProvisioner._tree_manifest(staging)
                    != manifest_before
                ):
                    raise Mt5TemplateError(
                        "MT5 public distribution changed during staging"
                    )

                # The public distribution must not get to introduce files in
                # the private TradeJournal namespaces.  Recreate those two
                # directories exclusively from the already pinned golden.
                managed_parents = {
                    relative.parent for relative in _MANAGED_RUNTIME_ASSETS
                }
                for relative in managed_parents:
                    self._remove_tree(staging / relative)
                for relative in _MANAGED_RUNTIME_ASSETS:
                    source = self.template_root / relative
                    destination = staging / relative
                    if (
                        InstanceProvisioner._is_reparse_point(source)
                        or not source.is_file()
                    ):
                        raise Mt5TemplateError(
                            "MT5 managed runtime asset is unavailable"
                        )
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, destination)

                self._sanitize_template(staging)
                if (
                    InstanceProvisioner._managed_runtime_assets_manifest(staging)
                    != managed_assets
                ):
                    raise Mt5TemplateError(
                        "MT5 public distribution changed managed runtime assets"
                    )
                staged_terminal = staging / "terminal64.exe"
                if (
                    InstanceProvisioner._sha256(staged_terminal)
                    != expected_terminal
                    or self._verify_metaquotes_signature(staged_terminal) != signer
                ):
                    raise Mt5TemplateError(
                        "MT5 public distribution signer changed"
                    )
                next_code = InstanceProvisioner._code_manifest(staging)
                atomic_json(
                    staging / _MARKER_NAME,
                    {
                        "schema_version": 2,
                        "base_terminal_sha256": self.configured_sha256,
                        "previous_terminal_sha256": current,
                        "terminal_sha256": expected_terminal,
                        "code_manifest_sha256": next_code,
                        "signer_subject": signer,
                        "verified_at_unix_ms": int(time.time() * 1000),
                    },
                )
                WindowsSecretStore.restrict_acl(staging / _MARKER_NAME)
                InstanceProvisioner._sync_tree(staging)
                prepared = PreparedMt5Template(
                    root=staging,
                    source_terminal_sha256=current,
                    source_code_manifest_sha256=current_code,
                    target_terminal_sha256=expected_terminal,
                    target_code_manifest_sha256=next_code,
                    candidate_manifest_sha256=(
                        InstanceProvisioner._tree_manifest(staging)
                    ),
                    signer_subject=signer,
                )
                self._prepared = prepared
                return prepared
            except Exception:
                self._remove_tree(staging)
                raise

    def prepare_verified_update(
        self,
        bundle_root: Path,
        updater: Path,
        config: Path,
        signer_subject: str,
        *,
        expected_source_terminal_sha256: str | None = None,
        expected_source_code_manifest_sha256: str | None = None,
        expected_target_terminal_sha256: str | None = None,
        expected_target_code_manifest_sha256: str | None = None,
        cancel_check: Callable[[], None] | None = None,
    ) -> PreparedMt5Template:
        """Build a verified candidate without exposing it to pool/job claims."""

        bundle_root = Path(bundle_root).resolve()
        updater = Path(updater).resolve()
        config = Path(config).resolve()
        expected_source_terminal_sha256 = self._required_digest(
            expected_source_terminal_sha256,
            "source terminal",
        )
        expected_source_code_manifest_sha256 = self._required_digest(
            expected_source_code_manifest_sha256,
            "source code manifest",
        )
        expected_target_terminal_sha256 = self._required_digest(
            expected_target_terminal_sha256,
            "target terminal",
        )
        expected_target_code_manifest_sha256 = self._required_digest(
            expected_target_code_manifest_sha256,
            "target code manifest",
        )
        if (expected_source_terminal_sha256 is None) != (
            expected_source_code_manifest_sha256 is None
        ) or (expected_target_terminal_sha256 is None) != (
            expected_target_code_manifest_sha256 is None
        ):
            raise Mt5TemplateError("MT5 update release binding is incomplete")
        with self.lock:
            if self._recovery_required:
                raise Mt5TemplateRecoveryRequired(
                    "MT5 template rotation requires recovery"
                )
            if self._prepared is not None:
                raise Mt5TemplateError("MT5 template candidate already exists")
            current = self._validate_current_locked()
            try:
                current_code = InstanceProvisioner._code_manifest(
                    self.template_root
                )
            except (OSError, ValueError) as exc:
                raise Mt5TemplateError("MT5 template integrity is invalid") from exc
            if (
                expected_source_terminal_sha256 is not None
                and (
                    current != expected_source_terminal_sha256
                    or current_code != expected_source_code_manifest_sha256
                )
            ):
                raise Mt5TemplateError("MT5 update source release mismatch")
            if (
                InstanceProvisioner._is_reparse_point(bundle_root)
                or not bundle_root.is_dir()
                or updater.parent != bundle_root
                or not updater.is_file()
                or config.parent != bundle_root
                or config.name != UPDATER_ONLY_CONFIG_NAME
                or not config.is_file()
                or "MetaQuotes Ltd." not in signer_subject
            ):
                raise Mt5TemplateError("MT5 update bundle is unsafe")
            try:
                if config.read_bytes() != UPDATER_ONLY_CONFIG_BYTES:
                    raise Mt5TemplateError("MT5 updater config is not sanitized")
            except OSError as exc:
                raise Mt5TemplateError("MT5 updater config is unavailable") from exc
            if self._verify_metaquotes_signature(updater) != signer_subject:
                raise Mt5TemplateError("MT5 updater signer mismatch")

            parent = self.template_root.parent
            staging = parent / f".{self.template_root.name}.vendor-staging"
            backup = parent / f".{self.template_root.name}.vendor-backup"
            working = parent / f".{self.template_root.name}.vendor-working"
            self._remove_tree(staging)
            self._remove_tree(backup)
            self._remove_tree(working)
            cleanup_confirmed = True
            try:
                shutil.copytree(self.template_root, staging, symlinks=False)
                assets_before = InstanceProvisioner._managed_runtime_assets_manifest(
                    staging
                )
                staged_updater, staged_config = self._copy_update_working_set(
                    bundle_root,
                    updater,
                    config,
                    working,
                )
                command = [
                    str(staged_updater),
                    "/update",
                    f"/path:{staging}",
                    "/portable",
                    f"/config:{staged_config}",
                ]
                process = self._process_launcher(
                    command,
                    cwd=working,
                    close_fds=True,
                )
                deadline = time.monotonic() + 240.0
                while process.poll() is None and time.monotonic() < deadline:
                    if cancel_check is not None:
                        try:
                            cancel_check()
                        except Exception as exc:
                            process.kill()
                            process.wait(5)
                            raise Mt5TemplateError(
                                "MT5 template update was interrupted"
                            ) from exc
                    time.sleep(0.5)
                if process.poll() is None:
                    process.kill()
                    process.wait(5)
                    raise Mt5TemplateError("MT5 template update timed out")
                if process.returncode not in (0, None):
                    raise Mt5TemplateError("MT5 template update failed")
                staged_terminal = staging / "terminal64.exe"
                self._stop_staged_terminal(staged_terminal, staged_updater)
                self._sanitize_template(staging)
                assets_after = InstanceProvisioner._managed_runtime_assets_manifest(
                    staging
                )
                if assets_after != assets_before:
                    raise Mt5TemplateError("MT5 update changed managed runtime assets")
                if self._verify_metaquotes_signature(staged_terminal) != signer_subject:
                    raise Mt5TemplateError("updated MT5 template signer mismatch")
                next_digest = InstanceProvisioner._sha256(staged_terminal)
                next_code = InstanceProvisioner._code_manifest(staging)
                if (
                    expected_target_terminal_sha256 is not None
                    and (
                        next_digest != expected_target_terminal_sha256
                        or next_code != expected_target_code_manifest_sha256
                    )
                ):
                    raise Mt5TemplateError("MT5 update target release mismatch")
                atomic_json(
                    staging / _MARKER_NAME,
                    {
                        "schema_version": 2,
                        "base_terminal_sha256": self.configured_sha256,
                        "previous_terminal_sha256": current,
                        "terminal_sha256": next_digest,
                        "code_manifest_sha256": next_code,
                        "signer_subject": signer_subject,
                        "verified_at_unix_ms": int(time.time() * 1000),
                    },
                )
                WindowsSecretStore.restrict_acl(staging / _MARKER_NAME)
                InstanceProvisioner._sync_tree(staging)
                prepared = PreparedMt5Template(
                    root=staging,
                    source_terminal_sha256=current,
                    source_code_manifest_sha256=current_code,
                    target_terminal_sha256=next_digest,
                    target_code_manifest_sha256=next_code,
                    candidate_manifest_sha256=(
                        InstanceProvisioner._tree_manifest(staging)
                    ),
                    signer_subject=signer_subject,
                )
                self._prepared = prepared
                return prepared
            except Exception:
                try:
                    self._stop_interrupted_rotation_processes(
                        (
                            working / "terminal64.exe",
                            staging / "terminal64.exe",
                        )
                    )
                except Exception as cleanup_exc:
                    # Preserve both roots for the startup recovery path.  It
                    # will repeat the exact-process scan before deleting any
                    # bytes; removing a tree while its updater or relaunched
                    # candidate is still alive is never safe on Windows.
                    cleanup_confirmed = False
                    self._recovery_required = True
                    raise Mt5TemplateRecoveryRequired(
                        "MT5 template update cleanup failed"
                    ) from cleanup_exc
                self._remove_tree(staging)
                raise
            finally:
                if cleanup_confirmed:
                    self._remove_tree(working)

    def commit_prepared_update(self, prepared: PreparedMt5Template) -> str:
        """Atomically publish a candidate only after external canary approval."""

        with self.lock:
            if self._recovery_required:
                raise Mt5TemplateRecoveryRequired(
                    "MT5 template rotation requires recovery"
                )
            if self._prepared != prepared or prepared.root != (
                self.template_root.parent
                / f".{self.template_root.name}.vendor-staging"
            ):
                raise Mt5TemplateError("MT5 template candidate is invalid")
            try:
                current = self._validate_current_locked()
                current_code = InstanceProvisioner._code_manifest(
                    self.template_root
                )
                if (
                    current != prepared.source_terminal_sha256
                    or current_code != prepared.source_code_manifest_sha256
                    or InstanceProvisioner._tree_manifest(prepared.root)
                    != prepared.candidate_manifest_sha256
                    or InstanceProvisioner._sha256(
                        prepared.root / "terminal64.exe"
                    )
                    != prepared.target_terminal_sha256
                    or InstanceProvisioner._code_manifest(prepared.root)
                    != prepared.target_code_manifest_sha256
                    or self._verify_metaquotes_signature(
                        prepared.root / "terminal64.exe"
                    )
                    != prepared.signer_subject
                ):
                    raise Mt5TemplateError("MT5 template candidate changed")
            except Mt5TemplateError:
                raise
            except (OSError, ValueError) as exc:
                raise Mt5TemplateError("MT5 template candidate is invalid") from exc

            backup = (
                self.template_root.parent
                / f".{self.template_root.name}.vendor-backup"
            )
            self._remove_tree(backup)
            _replace_directory_with_retry(self.template_root, backup)
            try:
                try:
                    _replace_directory_with_retry(
                        prepared.root,
                        self.template_root,
                    )
                    fsync_directory(self.template_root.parent)
                except Exception as publish_exc:
                    # A directory rename is atomic but the explicit parent
                    # fsync can still fail after the candidate became visible.
                    # Move that candidate back to its original unpublished
                    # path before restoring the old golden; never attempt to
                    # rename the backup over an existing directory.
                    try:
                        if self.template_root.exists():
                            if prepared.root.exists():
                                raise Mt5TemplateError(
                                    "MT5 template rollback state is ambiguous"
                                )
                            _replace_directory_with_retry(
                                self.template_root,
                                prepared.root,
                            )
                        _replace_directory_with_retry(
                            backup,
                            self.template_root,
                        )
                        fsync_directory(self.template_root.parent)
                    except Exception as rollback_exc:
                        self._recovery_required = True
                        raise Mt5TemplateRecoveryRequired(
                            "MT5 template publication rollback failed"
                        ) from rollback_exc
                    raise publish_exc
                self._current_sha256 = prepared.target_terminal_sha256
                self._prepared = None
                try:
                    self._remove_tree(backup)
                except Exception:
                    logger.warning("verified MT5 template cleanup deferred")
                logger.info(
                    "promoted canary-approved MetaQuotes update to the MT5 template"
                )
                return prepared.target_terminal_sha256
            except Exception:
                # If rollback itself failed, retain both surviving directories
                # for deterministic startup recovery/operator inspection.
                self._current_sha256 = ""
                raise

    def advance_prepared_update(
        self,
        prepared: PreparedMt5Template,
        bundle_root: Path,
        updater: Path,
        config: Path,
        signer_subject: str,
        *,
        expected_source_terminal_sha256: str,
        expected_source_code_manifest_sha256: str,
        expected_target_terminal_sha256: str,
        expected_target_code_manifest_sha256: str,
        cancel_check: Callable[[], None] | None = None,
    ) -> PreparedMt5Template:
        """Apply one more verified edge to an unpublished candidate.

        The golden remains untouched while a release chain (A->B->C) is
        materialized.  A failed later edge leaves the original candidate
        registered so the caller can discard the whole private tree without
        ever publishing an intermediate build.
        """

        bundle_root = Path(bundle_root).resolve()
        updater = Path(updater).resolve()
        config = Path(config).resolve()
        source_terminal = self._required_digest(
            expected_source_terminal_sha256,
            "source terminal",
        )
        source_code = self._required_digest(
            expected_source_code_manifest_sha256,
            "source code manifest",
        )
        target_terminal = self._required_digest(
            expected_target_terminal_sha256,
            "target terminal",
        )
        target_code = self._required_digest(
            expected_target_code_manifest_sha256,
            "target code manifest",
        )
        if None in (source_terminal, source_code, target_terminal, target_code):
            raise Mt5TemplateError("MT5 update release binding is incomplete")

        with self.lock:
            if self._recovery_required:
                raise Mt5TemplateRecoveryRequired(
                    "MT5 template rotation requires recovery"
                )
            expected_root = (
                self.template_root.parent
                / f".{self.template_root.name}.vendor-staging"
            )
            if self._prepared != prepared or prepared.root != expected_root:
                raise Mt5TemplateError("MT5 template candidate is invalid")
            try:
                if (
                    InstanceProvisioner._tree_manifest(prepared.root)
                    != prepared.candidate_manifest_sha256
                    or InstanceProvisioner._sha256(
                        prepared.root / "terminal64.exe"
                    )
                    != prepared.target_terminal_sha256
                    or InstanceProvisioner._code_manifest(prepared.root)
                    != prepared.target_code_manifest_sha256
                ):
                    raise Mt5TemplateError("MT5 template candidate changed")
            except Mt5TemplateError:
                raise
            except (OSError, ValueError) as exc:
                raise Mt5TemplateError("MT5 template candidate is invalid") from exc
            if (
                source_terminal != prepared.target_terminal_sha256
                or source_code != prepared.target_code_manifest_sha256
            ):
                raise Mt5TemplateError("MT5 update source release mismatch")
            if (
                InstanceProvisioner._is_reparse_point(bundle_root)
                or not bundle_root.is_dir()
                or updater.parent != bundle_root
                or not updater.is_file()
                or config.parent != bundle_root
                or config.name != UPDATER_ONLY_CONFIG_NAME
                or not config.is_file()
                or "MetaQuotes Ltd." not in signer_subject
            ):
                raise Mt5TemplateError("MT5 update bundle is unsafe")
            try:
                if config.read_bytes() != UPDATER_ONLY_CONFIG_BYTES:
                    raise Mt5TemplateError("MT5 updater config is not sanitized")
            except OSError as exc:
                raise Mt5TemplateError("MT5 updater config is unavailable") from exc
            if self._verify_metaquotes_signature(updater) != signer_subject:
                raise Mt5TemplateError("MT5 updater signer mismatch")

            working = (
                self.template_root.parent
                / f".{self.template_root.name}.vendor-working"
            )
            self._remove_tree(working)
            cleanup_confirmed = True
            try:
                assets_before = (
                    InstanceProvisioner._managed_runtime_assets_manifest(
                        prepared.root
                    )
                )
                staged_updater, staged_config = self._copy_update_working_set(
                    bundle_root,
                    updater,
                    config,
                    working,
                )
                process = self._process_launcher(
                    [
                        str(staged_updater),
                        "/update",
                        f"/path:{prepared.root}",
                        "/portable",
                        f"/config:{staged_config}",
                    ],
                    cwd=working,
                    close_fds=True,
                )
                deadline = time.monotonic() + 240.0
                while process.poll() is None and time.monotonic() < deadline:
                    if cancel_check is not None:
                        try:
                            cancel_check()
                        except Exception as exc:
                            process.kill()
                            process.wait(5)
                            raise Mt5TemplateError(
                                "MT5 template update was interrupted"
                            ) from exc
                    time.sleep(0.5)
                if process.poll() is None:
                    process.kill()
                    process.wait(5)
                    raise Mt5TemplateError("MT5 template update timed out")
                if process.returncode not in (0, None):
                    raise Mt5TemplateError("MT5 template update failed")

                staged_terminal = prepared.root / "terminal64.exe"
                self._stop_staged_terminal(staged_terminal, staged_updater)
                self._sanitize_template(prepared.root)
                assets_after = (
                    InstanceProvisioner._managed_runtime_assets_manifest(
                        prepared.root
                    )
                )
                if assets_after != assets_before:
                    raise Mt5TemplateError(
                        "MT5 update changed managed runtime assets"
                    )
                if (
                    self._verify_metaquotes_signature(staged_terminal)
                    != signer_subject
                ):
                    raise Mt5TemplateError(
                        "updated MT5 template signer mismatch"
                    )
                next_digest = InstanceProvisioner._sha256(staged_terminal)
                next_code = InstanceProvisioner._code_manifest(prepared.root)
                if next_digest != target_terminal or next_code != target_code:
                    raise Mt5TemplateError("MT5 update target release mismatch")
                atomic_json(
                    prepared.root / _MARKER_NAME,
                    {
                        "schema_version": 2,
                        "base_terminal_sha256": self.configured_sha256,
                        "previous_terminal_sha256": source_terminal,
                        "terminal_sha256": next_digest,
                        "code_manifest_sha256": next_code,
                        "signer_subject": signer_subject,
                        "verified_at_unix_ms": int(time.time() * 1000),
                    },
                )
                WindowsSecretStore.restrict_acl(prepared.root / _MARKER_NAME)
                InstanceProvisioner._sync_tree(prepared.root)
                advanced = PreparedMt5Template(
                    root=prepared.root,
                    source_terminal_sha256=(
                        prepared.source_terminal_sha256
                    ),
                    source_code_manifest_sha256=(
                        prepared.source_code_manifest_sha256
                    ),
                    target_terminal_sha256=next_digest,
                    target_code_manifest_sha256=next_code,
                    candidate_manifest_sha256=(
                        InstanceProvisioner._tree_manifest(prepared.root)
                    ),
                    signer_subject=signer_subject,
                )
                self._prepared = advanced
                return advanced
            except Exception:
                try:
                    self._stop_interrupted_rotation_processes(
                        (
                            working / "terminal64.exe",
                            prepared.root / "terminal64.exe",
                        )
                    )
                except Exception as cleanup_exc:
                    cleanup_confirmed = False
                    self._recovery_required = True
                    raise Mt5TemplateRecoveryRequired(
                        "MT5 template update cleanup failed"
                    ) from cleanup_exc
                raise
            finally:
                if cleanup_confirmed:
                    self._remove_tree(working)

    def discard_prepared_update(self, prepared: PreparedMt5Template) -> None:
        with self.lock:
            if self._recovery_required:
                raise Mt5TemplateRecoveryRequired(
                    "MT5 template rotation requires recovery"
                )
            if self._prepared != prepared:
                raise Mt5TemplateError("MT5 template candidate is invalid")
            self._remove_tree(prepared.root)
            self._prepared = None

    def promote_verified_update(
        self,
        bundle_root: Path,
        updater: Path,
        config: Path,
        signer_subject: str,
    ) -> str:
        """Compatibility path: prepare and immediately publish one bundle."""

        prepared = self.prepare_verified_update(
            bundle_root,
            updater,
            config,
            signer_subject,
        )
        try:
            return self.commit_prepared_update(prepared)
        except Exception:
            with self.lock:
                if (
                    not self._recovery_required
                    and self._prepared == prepared
                    and prepared.root.exists()
                ):
                    self._remove_tree(prepared.root)
                    self._prepared = None
            raise
