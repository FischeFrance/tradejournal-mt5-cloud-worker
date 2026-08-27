"""Verified, service-owned rotation of the credential-free MT5 template."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from threading import RLock
from typing import Callable

from worker.atomic_file import durable_replace, fsync_directory

from ..state_store import atomic_json, read_json
from .mt5_instance import InstanceProvisioner
from .secret_store import WindowsSecretStore


logger = logging.getLogger(__name__)

_MARKER_NAME = ".tradejournal-vendor-update.json"
_PRIVATE_DIRECTORIES = (
    Path("Bases"),
    Path("Logs"),
    Path("MQL5/Files"),
    Path("MQL5/Logs"),
    Path("Tester/cache"),
    Path("Tester/logs"),
)
_PRIVATE_CONFIG_NAMES = frozenset(
    {"accounts.dat", "accounts.ini", "community.ini", "signals.ini"}
)


class Mt5TemplateError(RuntimeError):
    """A sanitized failure while validating or rotating the golden template."""


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

    @property
    def current_sha256(self) -> str:
        with self.lock:
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
        if not self.template_root.exists() and backup.is_dir():
            durable_replace(backup, self.template_root)
            fsync_directory(parent)
        if staging.exists():
            self._remove_tree(staging)
        if backup.exists() and self.template_root.exists():
            self._remove_tree(backup)

    @staticmethod
    def _remove_tree(path: Path) -> None:
        if not path.exists():
            return
        if InstanceProvisioner._is_reparse_point(path) or not path.is_dir():
            raise Mt5TemplateError("MT5 template rotation path is unsafe")
        shutil.rmtree(path)

    def _validate_current_locked(self) -> str:
        self._recover_interrupted_rotation_locked()
        if (
            InstanceProvisioner._is_reparse_point(self.template_root)
            or not self.source_terminal.is_file()
        ):
            raise Mt5TemplateError("MT5 template is unavailable")
        try:
            InstanceProvisioner._validate_source_tree(self.template_root)
            digest = InstanceProvisioner._sha256(self.source_terminal)
        except (OSError, ValueError) as exc:
            raise Mt5TemplateError("MT5 template integrity is invalid") from exc
        marker_path = self.template_root / _MARKER_NAME
        marker = read_json(marker_path, {}) if marker_path.is_file() else {}
        if not marker:
            if digest != self.configured_sha256:
                raise Mt5TemplateError("MT5 template digest mismatch")
            return digest
        if marker.get("base_terminal_sha256") != self.configured_sha256:
            # A deliberate operator deployment may replace both the template
            # and its configured anchor.  In that exact case the old rotation
            # marker is obsolete and the new binary becomes the fresh base.
            if digest == self.configured_sha256:
                marker_path.unlink()
                return digest
            raise Mt5TemplateError("MT5 template rotation anchor mismatch")
        if (
            marker.get("schema_version") != 1
            or marker.get("terminal_sha256") != digest
            or not isinstance(marker.get("verified_at_unix_ms"), int)
            or marker["verified_at_unix_ms"] <= 0
            or "MetaQuotes Ltd." not in str(marker.get("signer_subject", ""))
        ):
            raise Mt5TemplateError("MT5 template rotation record is invalid")
        signer = self._verify_metaquotes_signature(self.source_terminal)
        if signer != marker["signer_subject"]:
            raise Mt5TemplateError("MT5 template signer mismatch")
        return digest

    @staticmethod
    def _sanitize_template(root: Path) -> None:
        for relative in _PRIVATE_DIRECTORIES:
            path = root / relative
            if path.exists():
                if InstanceProvisioner._is_reparse_point(path) or not path.is_dir():
                    raise Mt5TemplateError("updated MT5 template contains unsafe data")
                shutil.rmtree(path)
        config = root / "Config"
        if config.is_dir():
            for child in tuple(config.iterdir()):
                if child.name.casefold() not in _PRIVATE_CONFIG_NAMES:
                    continue
                if child.is_dir():
                    Mt5TemplateManager._remove_tree(child)
                elif child.is_file() and not InstanceProvisioner._is_reparse_point(child):
                    child.unlink()
                else:
                    raise Mt5TemplateError("updated MT5 template config is unsafe")

    @staticmethod
    def _stop_staged_terminal(terminal: Path) -> None:
        try:
            import psutil
        except ImportError:
            return
        for process in psutil.process_iter(("exe",)):
            try:
                executable = process.info.get("exe")
                if executable and Path(str(executable)).resolve() == terminal.resolve():
                    process.terminate()
                    try:
                        process.wait(15)
                    except psutil.TimeoutExpired:
                        process.kill()
                        process.wait(5)
            except (psutil.AccessDenied, psutil.NoSuchProcess, OSError, ValueError):
                continue

    def promote_verified_update(
        self,
        bundle_root: Path,
        updater: Path,
        config: Path,
        signer_subject: str,
    ) -> str:
        """Apply one already verified vendor bundle to a clean template clone."""

        bundle_root = Path(bundle_root).resolve()
        updater = Path(updater).resolve()
        config = Path(config).resolve()
        with self.lock:
            current = self._validate_current_locked()
            if (
                InstanceProvisioner._is_reparse_point(bundle_root)
                or not bundle_root.is_dir()
                or updater.parent != bundle_root
                or not updater.is_file()
                or not config.is_file()
                or "MetaQuotes Ltd." not in signer_subject
            ):
                raise Mt5TemplateError("MT5 update bundle is unsafe")
            if self._verify_metaquotes_signature(updater) != signer_subject:
                raise Mt5TemplateError("MT5 updater signer mismatch")

            parent = self.template_root.parent
            staging = parent / f".{self.template_root.name}.vendor-staging"
            backup = parent / f".{self.template_root.name}.vendor-backup"
            self._remove_tree(staging)
            self._remove_tree(backup)
            try:
                shutil.copytree(self.template_root, staging, symlinks=False)
                assets_before = InstanceProvisioner._managed_runtime_assets_manifest(
                    staging
                )
                staged_updater = bundle_root / updater.name
                command = [
                    str(staged_updater),
                    "/update",
                    f"/path:{staging}",
                    "/portable",
                    f"/config:{config}",
                ]
                process = self._process_launcher(
                    command,
                    cwd=bundle_root,
                    close_fds=True,
                )
                deadline = time.monotonic() + 240.0
                while process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.5)
                if process.poll() is None:
                    process.kill()
                    process.wait(5)
                    raise Mt5TemplateError("MT5 template update timed out")
                if process.returncode not in (0, None):
                    raise Mt5TemplateError("MT5 template update failed")
                staged_terminal = staging / "terminal64.exe"
                self._stop_staged_terminal(staged_terminal)
                self._sanitize_template(staging)
                assets_after = InstanceProvisioner._managed_runtime_assets_manifest(
                    staging
                )
                if assets_after != assets_before:
                    raise Mt5TemplateError("MT5 update changed managed runtime assets")
                if self._verify_metaquotes_signature(staged_terminal) != signer_subject:
                    raise Mt5TemplateError("updated MT5 template signer mismatch")
                next_digest = InstanceProvisioner._sha256(staged_terminal)
                atomic_json(
                    staging / _MARKER_NAME,
                    {
                        "schema_version": 1,
                        "base_terminal_sha256": self.configured_sha256,
                        "previous_terminal_sha256": current,
                        "terminal_sha256": next_digest,
                        "signer_subject": signer_subject,
                        "verified_at_unix_ms": int(time.time() * 1000),
                    },
                )
                WindowsSecretStore.restrict_acl(staging / _MARKER_NAME)
                InstanceProvisioner._sync_tree(staging)
                durable_replace(self.template_root, backup)
                try:
                    durable_replace(staging, self.template_root)
                    fsync_directory(parent)
                except Exception:
                    durable_replace(backup, self.template_root)
                    fsync_directory(parent)
                    raise
                self._remove_tree(backup)
                self._current_sha256 = next_digest
                logger.info("promoted verified MetaQuotes update to the MT5 template")
                return next_digest
            finally:
                if staging.exists():
                    self._remove_tree(staging)
                if backup.exists() and self.template_root.exists():
                    self._remove_tree(backup)
