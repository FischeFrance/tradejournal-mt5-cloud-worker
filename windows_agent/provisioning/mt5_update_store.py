"""Durable, credential-free storage for verified MetaQuotes update bundles.

The native runtime first copies the narrowly allow-listed LiveUpdate payload into
an instance-owned, SYSTEM-only directory.  This module seals that directory with
the release observed before and after the update.  Only a bundle whose account
subsequently passed the login/heartbeat/read-only gate can be copied into the
shared pending store.

The source bundle deliberately remains independent from the store.  If the
Agent crashes before capture, or a required capture callback fails, its sealed
sidecar remains below the instance ``state`` directory and can be captured on a
later maintenance pass.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import uuid4

from worker.atomic_file import durable_replace, fsync_directory

from ..security import canonical_uuid
from ..state_store import atomic_json, read_json
from .mt5_instance import InstanceProvisioner
from .secret_store import WindowsSecretStore

logger = logging.getLogger(__name__)

VERIFIED_UPDATE_METADATA_NAME = "verified-update.json"
PENDING_UPDATE_RECEIPT_NAME = "receipt.json"
UPDATER_ONLY_CONFIG_NAME = "tradejournal-update.ini"
UPDATER_ONLY_CONFIG_BYTES = (
    b"[Common]\r\nKeepPrivate=0\r\nNewsEnable=0\r\n\r\n"
)

_BUNDLE_SCHEMA_VERSION = 1
_STORE_SCHEMA_VERSION = 1
_PHASE_STAGED = "staged"
_PHASE_APPLIED = "applied_pending_health"
_PHASE_HEALTHY = "health_verified"
_PHASES = frozenset({_PHASE_STAGED, _PHASE_APPLIED, _PHASE_HEALTHY})
_SHA256 = re.compile(r"[0-9a-f]{64}")
VENDOR_UPDATE_PAYLOAD_NAME = re.compile(r"[A-Za-z0-9_-]+\.[0-9]{4,6}")
_RECEIPT_ID = _SHA256
_TRANSIENT_ENTRY = re.compile(
    r"\.([0-9a-f]{64})\.([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12})\.(staging|deleting)"
)
_QUARANTINED_ENTRY = re.compile(
    r"\.([0-9a-f]{64})\.([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12})\.quarantined"
)
MAX_UPDATE_BUNDLE_FILES = 64
MAX_UPDATE_BUNDLE_FILE_BYTES = 512 * 1024 * 1024
MAX_UPDATE_BUNDLE_BYTES = 1024 * 1024 * 1024


class Mt5PendingUpdateStoreError(RuntimeError):
    """A sanitized update-bundle validation or persistence failure."""


def _restrict_private_acl(path: Path) -> None:
    if os.name == "nt":
        WindowsSecretStore.restrict_acl(path)


@dataclass(frozen=True)
class Mt5UpdateRelease:
    terminal_sha256: str
    code_manifest_sha256: str
    release_id: str

    @classmethod
    def from_terminal_root(cls, terminal_root: Path) -> "Mt5UpdateRelease":
        terminal_root = Path(terminal_root).resolve()
        try:
            if (
                InstanceProvisioner._is_reparse_point(terminal_root)
                or not terminal_root.is_dir()
            ):
                raise ValueError("unsafe terminal root")
            terminal_sha256 = InstanceProvisioner._sha256(
                terminal_root / "terminal64.exe"
            )
            code_manifest_sha256 = InstanceProvisioner._code_manifest(
                terminal_root
            )
        except (OSError, ValueError) as exc:
            raise Mt5PendingUpdateStoreError(
                "MT5 update release is invalid"
            ) from exc
        release_id = hashlib.sha256(
            f"{terminal_sha256}:{code_manifest_sha256}".encode("ascii")
        ).hexdigest()
        return cls(terminal_sha256, code_manifest_sha256, release_id)

    @classmethod
    def from_dict(cls, value: object) -> "Mt5UpdateRelease":
        if not isinstance(value, dict) or set(value) != {
            "terminal_sha256",
            "code_manifest_sha256",
            "release_id",
        }:
            raise Mt5PendingUpdateStoreError("MT5 update release is invalid")
        terminal_sha256 = value.get("terminal_sha256")
        code_manifest_sha256 = value.get("code_manifest_sha256")
        release_id = value.get("release_id")
        if not (
            isinstance(terminal_sha256, str)
            and _SHA256.fullmatch(terminal_sha256)
            and isinstance(code_manifest_sha256, str)
            and _SHA256.fullmatch(code_manifest_sha256)
            and isinstance(release_id, str)
            and _SHA256.fullmatch(release_id)
        ):
            raise Mt5PendingUpdateStoreError("MT5 update release is invalid")
        expected = hashlib.sha256(
            f"{terminal_sha256}:{code_manifest_sha256}".encode("ascii")
        ).hexdigest()
        if release_id != expected:
            raise Mt5PendingUpdateStoreError("MT5 update release is invalid")
        return cls(terminal_sha256, code_manifest_sha256, release_id)

    def as_dict(self) -> dict[str, str]:
        return {
            "terminal_sha256": self.terminal_sha256,
            "code_manifest_sha256": self.code_manifest_sha256,
            "release_id": self.release_id,
        }


@dataclass(frozen=True)
class Mt5VerifiedUpdateBundle:
    root: Path
    receipt_id: str | None
    phase: str
    source_connection_id: str
    source_release: Mt5UpdateRelease
    managed_assets_manifest_sha256: str
    target_release: Mt5UpdateRelease | None
    signer_subject: str
    updater: Path
    updater_config: Path
    bundle_manifest_sha256: str
    files: tuple[dict[str, Any], ...]
    staged_at_unix_ms: int
    applied_at_unix_ms: int
    health_verified_at_unix_ms: int

    @property
    def health_verified(self) -> bool:
        return self.phase == _PHASE_HEALTHY


@dataclass(frozen=True)
class Mt5PendingUpdateReceipt:
    root: Path
    receipt_id: str
    source_connection_id: str
    source_release: Mt5UpdateRelease
    managed_assets_manifest_sha256: str
    target_release: Mt5UpdateRelease
    signer_subject: str
    updater: Path
    updater_config: Path
    bundle_manifest_sha256: str
    captured_at_unix_ms: int


def _valid_signer(subject: object) -> bool:
    return (
        isinstance(subject, str)
        and "CN=MetaQuotes Ltd." in subject
        and "O=MetaQuotes Ltd." in subject
        and "\r" not in subject
        and "\n" not in subject
        and len(subject) <= 1024
    )


def _sha256(path: Path) -> str:
    return InstanceProvisioner._sha256(path)


def _manifest_digest(files: tuple[dict[str, Any], ...]) -> str:
    encoded = json.dumps(
        files,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _receipt_id(
    source: Mt5UpdateRelease,
    target: Mt5UpdateRelease,
    managed_assets_manifest_sha256: str,
    signer_subject: str,
    bundle_manifest_sha256: str,
) -> str:
    encoded = json.dumps(
        {
            "source_release": source.as_dict(),
            "target_release": target.as_dict(),
            "managed_assets_manifest_sha256": (
                managed_assets_manifest_sha256
            ),
            "signer_subject": signer_subject,
            "bundle_manifest_sha256": bundle_manifest_sha256,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def write_updater_only_config(path: Path) -> Path:
    """Write the one fixed updater config; it can never contain account data."""

    path = Path(path)
    if path.name != UPDATER_ONLY_CONFIG_NAME:
        raise Mt5PendingUpdateStoreError("MT5 updater config path is invalid")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(UPDATER_ONLY_CONFIG_BYTES)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise
    _restrict_private_acl(path)
    return path


def _validate_bundle_root(root: Path) -> None:
    if (
        InstanceProvisioner._is_reparse_point(root)
        or not root.is_dir()
    ):
        raise Mt5PendingUpdateStoreError("MT5 update bundle is unsafe")
    try:
        InstanceProvisioner._validate_source_tree(root)
    except ValueError as exc:
        raise Mt5PendingUpdateStoreError("MT5 update bundle is unsafe") from exc


def _bundle_files(
    root: Path,
    updater_name: str,
    config_name: str,
) -> tuple[dict[str, Any], ...]:
    _validate_bundle_root(root)
    if (
        Path(updater_name).name != updater_name
        or updater_name.casefold() != "terminal64.exe"
        or config_name != UPDATER_ONLY_CONFIG_NAME
    ):
        raise Mt5PendingUpdateStoreError("MT5 update bundle metadata is invalid")

    allowed_names = {updater_name, config_name, VERIFIED_UPDATE_METADATA_NAME}
    files: list[dict[str, Any]] = []
    total_size = 0
    for child in root.iterdir():
        if child.name == "temp":
            if (
                InstanceProvisioner._is_reparse_point(child)
                or not child.is_dir()
            ):
                raise Mt5PendingUpdateStoreError("MT5 update bundle is unsafe")
            continue
        if child.name == VERIFIED_UPDATE_METADATA_NAME:
            if (
                InstanceProvisioner._is_reparse_point(child)
                or not child.is_file()
            ):
                raise Mt5PendingUpdateStoreError("MT5 update bundle is unsafe")
            continue
        if (
            child.name not in allowed_names
            and not VENDOR_UPDATE_PAYLOAD_NAME.fullmatch(child.name)
        ):
            raise Mt5PendingUpdateStoreError(
                "MT5 update bundle contains an unsupported file"
            )
        if (
            InstanceProvisioner._is_reparse_point(child)
            or not child.is_file()
        ):
            raise Mt5PendingUpdateStoreError("MT5 update bundle is unsafe")
        try:
            size = child.stat().st_size
        except OSError as exc:
            raise Mt5PendingUpdateStoreError(
                "MT5 update bundle is unavailable"
            ) from exc
        if size <= 0 or size > MAX_UPDATE_BUNDLE_FILE_BYTES:
            raise Mt5PendingUpdateStoreError("MT5 update bundle size is invalid")
        total_size += size
        if total_size > MAX_UPDATE_BUNDLE_BYTES:
            raise Mt5PendingUpdateStoreError("MT5 update bundle size is invalid")
        files.append(
            {
                "name": child.name,
                "size": size,
                "sha256": _sha256(child),
            }
        )
    files.sort(key=lambda value: str(value["name"]).casefold())
    if (
        len(files) > MAX_UPDATE_BUNDLE_FILES
        or not any(value["name"] == updater_name for value in files)
        or not any(value["name"] == config_name for value in files)
    ):
        raise Mt5PendingUpdateStoreError("MT5 update bundle is incomplete")
    config = root / config_name
    try:
        if config.read_bytes() != UPDATER_ONLY_CONFIG_BYTES:
            raise Mt5PendingUpdateStoreError(
                "MT5 updater config is not sanitized"
            )
    except OSError as exc:
        raise Mt5PendingUpdateStoreError(
            "MT5 updater config is unavailable"
        ) from exc
    return tuple(files)


def _metadata_document(
    *,
    phase: str,
    receipt_id: str | None,
    source_connection_id: str,
    source_release: Mt5UpdateRelease,
    managed_assets_manifest_sha256: str,
    target_release: Mt5UpdateRelease | None,
    signer_subject: str,
    updater_name: str,
    updater_config_name: str,
    files: tuple[dict[str, Any], ...],
    staged_at_unix_ms: int,
    applied_at_unix_ms: int,
    health_verified_at_unix_ms: int,
) -> dict[str, Any]:
    return {
        "schema_version": _BUNDLE_SCHEMA_VERSION,
        "phase": phase,
        "receipt_id": receipt_id,
        "source_connection_id": source_connection_id,
        "source_release": source_release.as_dict(),
        "managed_assets_manifest_sha256": managed_assets_manifest_sha256,
        "target_release": (
            target_release.as_dict() if target_release is not None else None
        ),
        "signer_subject": signer_subject,
        "updater_name": updater_name,
        "updater_config_name": updater_config_name,
        "files": list(files),
        "bundle_manifest_sha256": _manifest_digest(files),
        "staged_at_unix_ms": staged_at_unix_ms,
        "applied_at_unix_ms": applied_at_unix_ms,
        "health_verified_at_unix_ms": health_verified_at_unix_ms,
    }


def stage_verified_update_bundle(
    bundle_root: Path,
    *,
    source_connection_id: str,
    source_release: Mt5UpdateRelease,
    managed_assets_manifest_sha256: str,
    signer_subject: str,
    updater: Path,
    updater_config: Path,
) -> Mt5VerifiedUpdateBundle:
    """Seal the copied updater before any source cache or terminal mutation."""

    root = Path(bundle_root).resolve()
    updater = Path(updater).resolve()
    updater_config = Path(updater_config).resolve()
    try:
        source_connection_id = canonical_uuid(source_connection_id)
    except ValueError as exc:
        raise Mt5PendingUpdateStoreError(
            "MT5 update source identity is invalid"
        ) from exc
    if (
        updater.parent != root
        or updater_config.parent != root
        or not isinstance(managed_assets_manifest_sha256, str)
        or not _SHA256.fullmatch(managed_assets_manifest_sha256)
        or not _valid_signer(signer_subject)
    ):
        raise Mt5PendingUpdateStoreError("MT5 update bundle metadata is invalid")
    files = _bundle_files(root, updater.name, updater_config.name)
    now = int(time.time() * 1000)
    atomic_json(
        root / VERIFIED_UPDATE_METADATA_NAME,
        _metadata_document(
            phase=_PHASE_STAGED,
            receipt_id=None,
            source_connection_id=source_connection_id,
            source_release=source_release,
            managed_assets_manifest_sha256=managed_assets_manifest_sha256,
            target_release=None,
            signer_subject=signer_subject,
            updater_name=updater.name,
            updater_config_name=updater_config.name,
            files=files,
            staged_at_unix_ms=now,
            applied_at_unix_ms=0,
            health_verified_at_unix_ms=0,
        ),
    )
    _restrict_private_acl(root / VERIFIED_UPDATE_METADATA_NAME)
    return load_verified_update_bundle(root)


def seal_applied_update_bundle(
    bundle_root: Path,
    target_release: Mt5UpdateRelease,
) -> Mt5VerifiedUpdateBundle:
    bundle = load_verified_update_bundle(bundle_root)
    if bundle.phase != _PHASE_STAGED or bundle.target_release is not None:
        raise Mt5PendingUpdateStoreError("MT5 update bundle phase is invalid")
    receipt_id = _receipt_id(
        bundle.source_release,
        target_release,
        bundle.managed_assets_manifest_sha256,
        bundle.signer_subject,
        bundle.bundle_manifest_sha256,
    )
    atomic_json(
        bundle.root / VERIFIED_UPDATE_METADATA_NAME,
        _metadata_document(
            phase=_PHASE_APPLIED,
            receipt_id=receipt_id,
            source_connection_id=bundle.source_connection_id,
            source_release=bundle.source_release,
            managed_assets_manifest_sha256=(
                bundle.managed_assets_manifest_sha256
            ),
            target_release=target_release,
            signer_subject=bundle.signer_subject,
            updater_name=bundle.updater.name,
            updater_config_name=bundle.updater_config.name,
            files=bundle.files,
            staged_at_unix_ms=bundle.staged_at_unix_ms,
            applied_at_unix_ms=max(
                int(time.time() * 1000),
                bundle.staged_at_unix_ms,
            ),
            health_verified_at_unix_ms=0,
        ),
    )
    _restrict_private_acl(bundle.root / VERIFIED_UPDATE_METADATA_NAME)
    return load_verified_update_bundle(bundle.root)


def mark_update_bundle_healthy(bundle_root: Path) -> Mt5VerifiedUpdateBundle:
    bundle = load_verified_update_bundle(bundle_root)
    if (
        bundle.phase != _PHASE_APPLIED
        or bundle.receipt_id is None
        or bundle.target_release is None
    ):
        raise Mt5PendingUpdateStoreError("MT5 update bundle phase is invalid")
    atomic_json(
        bundle.root / VERIFIED_UPDATE_METADATA_NAME,
        _metadata_document(
            phase=_PHASE_HEALTHY,
            receipt_id=bundle.receipt_id,
            source_connection_id=bundle.source_connection_id,
            source_release=bundle.source_release,
            managed_assets_manifest_sha256=(
                bundle.managed_assets_manifest_sha256
            ),
            target_release=bundle.target_release,
            signer_subject=bundle.signer_subject,
            updater_name=bundle.updater.name,
            updater_config_name=bundle.updater_config.name,
            files=bundle.files,
            staged_at_unix_ms=bundle.staged_at_unix_ms,
            applied_at_unix_ms=bundle.applied_at_unix_ms,
            health_verified_at_unix_ms=max(
                int(time.time() * 1000),
                bundle.applied_at_unix_ms,
            ),
        ),
    )
    _restrict_private_acl(bundle.root / VERIFIED_UPDATE_METADATA_NAME)
    return load_verified_update_bundle(bundle.root, require_healthy=True)


def _validate_files_document(value: object) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, list) or not 2 <= len(value) <= MAX_UPDATE_BUNDLE_FILES:
        raise Mt5PendingUpdateStoreError("MT5 update file manifest is invalid")
    files: list[dict[str, Any]] = []
    names: set[str] = set()
    total_size = 0
    for entry in value:
        if not isinstance(entry, dict) or set(entry) != {"name", "size", "sha256"}:
            raise Mt5PendingUpdateStoreError(
                "MT5 update file manifest is invalid"
            )
        name = entry.get("name")
        size = entry.get("size")
        digest = entry.get("sha256")
        if (
            not isinstance(name, str)
            or not name
            or Path(name).name != name
            or name in names
            or type(size) is not int
            or not 0 < size <= MAX_UPDATE_BUNDLE_FILE_BYTES
            or not isinstance(digest, str)
            or not _SHA256.fullmatch(digest)
        ):
            raise Mt5PendingUpdateStoreError(
                "MT5 update file manifest is invalid"
            )
        names.add(name)
        total_size += size
        if total_size > MAX_UPDATE_BUNDLE_BYTES:
            raise Mt5PendingUpdateStoreError(
                "MT5 update file manifest is invalid"
            )
        files.append({"name": name, "size": size, "sha256": digest})
    if files != sorted(files, key=lambda item: str(item["name"]).casefold()):
        raise Mt5PendingUpdateStoreError("MT5 update file manifest is invalid")
    return tuple(files)


def load_verified_update_bundle(
    bundle_root: Path,
    *,
    require_healthy: bool = False,
) -> Mt5VerifiedUpdateBundle:
    root = Path(bundle_root).resolve()
    _validate_bundle_root(root)
    metadata_path = root / VERIFIED_UPDATE_METADATA_NAME
    if (
        InstanceProvisioner._is_reparse_point(metadata_path)
        or not metadata_path.is_file()
    ):
        raise Mt5PendingUpdateStoreError("MT5 update bundle receipt is invalid")
    try:
        value = read_json(metadata_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise Mt5PendingUpdateStoreError(
            "MT5 update bundle receipt is invalid"
        ) from exc
    expected_fields = {
        "schema_version",
        "phase",
        "receipt_id",
        "source_connection_id",
        "source_release",
        "managed_assets_manifest_sha256",
        "target_release",
        "signer_subject",
        "updater_name",
        "updater_config_name",
        "files",
        "bundle_manifest_sha256",
        "staged_at_unix_ms",
        "applied_at_unix_ms",
        "health_verified_at_unix_ms",
    }
    phase = value.get("phase")
    receipt_id = value.get("receipt_id")
    target_raw = value.get("target_release")
    if (
        set(value) != expected_fields
        or value.get("schema_version") != _BUNDLE_SCHEMA_VERSION
        or phase not in _PHASES
        or not _valid_signer(value.get("signer_subject"))
        or not isinstance(value.get("source_connection_id"), str)
        or not isinstance(value.get("managed_assets_manifest_sha256"), str)
        or not _SHA256.fullmatch(value["managed_assets_manifest_sha256"])
        or not isinstance(value.get("updater_name"), str)
        or not isinstance(value.get("updater_config_name"), str)
        or not isinstance(value.get("bundle_manifest_sha256"), str)
        or not _SHA256.fullmatch(value["bundle_manifest_sha256"])
        or type(value.get("staged_at_unix_ms")) is not int
        or value["staged_at_unix_ms"] <= 0
        or type(value.get("applied_at_unix_ms")) is not int
        or type(value.get("health_verified_at_unix_ms")) is not int
    ):
        raise Mt5PendingUpdateStoreError("MT5 update bundle receipt is invalid")
    try:
        source_connection_id = canonical_uuid(value["source_connection_id"])
    except ValueError as exc:
        raise Mt5PendingUpdateStoreError(
            "MT5 update bundle receipt is invalid"
        ) from exc
    source_release = Mt5UpdateRelease.from_dict(value.get("source_release"))
    target_release = (
        Mt5UpdateRelease.from_dict(target_raw) if target_raw is not None else None
    )
    files = _validate_files_document(value.get("files"))
    actual_files = _bundle_files(
        root,
        value["updater_name"],
        value["updater_config_name"],
    )
    if (
        files != actual_files
        or _manifest_digest(files) != value["bundle_manifest_sha256"]
    ):
        raise Mt5PendingUpdateStoreError("MT5 update bundle integrity failed")

    if phase == _PHASE_STAGED:
        valid_phase = (
            receipt_id is None
            and target_release is None
            and value["applied_at_unix_ms"] == 0
            and value["health_verified_at_unix_ms"] == 0
        )
    elif phase == _PHASE_APPLIED:
        valid_phase = (
            isinstance(receipt_id, str)
            and _RECEIPT_ID.fullmatch(receipt_id) is not None
            and target_release is not None
            and value["applied_at_unix_ms"] >= value["staged_at_unix_ms"]
            and value["health_verified_at_unix_ms"] == 0
        )
    else:
        valid_phase = (
            isinstance(receipt_id, str)
            and _RECEIPT_ID.fullmatch(receipt_id) is not None
            and target_release is not None
            and value["applied_at_unix_ms"] >= value["staged_at_unix_ms"]
            and value["health_verified_at_unix_ms"]
            >= value["applied_at_unix_ms"]
        )
    if not valid_phase or (require_healthy and phase != _PHASE_HEALTHY):
        raise Mt5PendingUpdateStoreError("MT5 update bundle phase is invalid")
    if target_release is not None:
        expected_receipt_id = _receipt_id(
            source_release,
            target_release,
            value["managed_assets_manifest_sha256"],
            value["signer_subject"],
            value["bundle_manifest_sha256"],
        )
        if receipt_id != expected_receipt_id:
            raise Mt5PendingUpdateStoreError(
                "MT5 update bundle receipt identity is invalid"
            )
    return Mt5VerifiedUpdateBundle(
        root=root,
        receipt_id=receipt_id,
        phase=phase,
        source_connection_id=source_connection_id,
        source_release=source_release,
        managed_assets_manifest_sha256=value[
            "managed_assets_manifest_sha256"
        ],
        target_release=target_release,
        signer_subject=value["signer_subject"],
        updater=root / value["updater_name"],
        updater_config=root / value["updater_config_name"],
        bundle_manifest_sha256=value["bundle_manifest_sha256"],
        files=files,
        staged_at_unix_ms=value["staged_at_unix_ms"],
        applied_at_unix_ms=value["applied_at_unix_ms"],
        health_verified_at_unix_ms=value["health_verified_at_unix_ms"],
    )


class Mt5PendingUpdateStore:
    """Atomically publish and enumerate health-verified update receipts."""

    def __init__(self, root: Path, *, lock: RLock | None = None) -> None:
        self.root = Path(root).resolve()
        self.lock = lock or RLock()
        self._ensure_root()

    def _ensure_root(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        if (
            InstanceProvisioner._is_reparse_point(self.root)
            or not self.root.is_dir()
        ):
            raise Mt5PendingUpdateStoreError("MT5 pending update store is unsafe")
        WindowsSecretStore.restrict_shared_service_acl(self.root)
        for child in tuple(self.root.iterdir()):
            if not _TRANSIENT_ENTRY.fullmatch(child.name):
                continue
            # Staging and deleting names are never READY publications. They
            # can only survive a service/process crash and are safe to reap
            # while the store lock is held.
            self._remove_tree(child)

    @staticmethod
    def _remove_tree(path: Path) -> None:
        if not path.exists():
            return
        if InstanceProvisioner._is_reparse_point(path) or not path.is_dir():
            raise Mt5PendingUpdateStoreError(
                "MT5 pending update cleanup target is unsafe"
            )
        shutil.rmtree(path)

    def _destination(self, receipt_id: str) -> Path:
        if not isinstance(receipt_id, str) or not _RECEIPT_ID.fullmatch(receipt_id):
            raise Mt5PendingUpdateStoreError(
                "MT5 pending update identity is invalid"
            )
        destination = self.root / receipt_id
        if destination.parent != self.root:
            raise Mt5PendingUpdateStoreError(
                "MT5 pending update identity is invalid"
            )
        return destination

    @staticmethod
    def _receipt_document(
        bundle: Mt5VerifiedUpdateBundle,
        captured_at_unix_ms: int,
    ) -> dict[str, Any]:
        if bundle.receipt_id is None or bundle.target_release is None:
            raise Mt5PendingUpdateStoreError("MT5 update bundle is incomplete")
        return {
            "schema_version": _STORE_SCHEMA_VERSION,
            "status": "READY",
            "receipt_id": bundle.receipt_id,
            "source_connection_id": bundle.source_connection_id,
            "source_release": bundle.source_release.as_dict(),
            "managed_assets_manifest_sha256": (
                bundle.managed_assets_manifest_sha256
            ),
            "target_release": bundle.target_release.as_dict(),
            "signer_subject": bundle.signer_subject,
            "updater_name": bundle.updater.name,
            "updater_config_name": bundle.updater_config.name,
            "files": list(bundle.files),
            "bundle_manifest_sha256": bundle.bundle_manifest_sha256,
            "source_staged_at_unix_ms": bundle.staged_at_unix_ms,
            "source_applied_at_unix_ms": bundle.applied_at_unix_ms,
            "source_health_verified_at_unix_ms": (
                bundle.health_verified_at_unix_ms
            ),
            "captured_at_unix_ms": captured_at_unix_ms,
        }

    def capture(
        self,
        bundle_root: Path,
        updater: Path | None = None,
        updater_config: Path | None = None,
        signer_subject: str | None = None,
    ) -> Mt5PendingUpdateReceipt:
        """Copy one healthy orphan into the shared store, idempotently."""

        bundle = load_verified_update_bundle(bundle_root, require_healthy=True)
        if (
            updater is not None
            and Path(updater).resolve() != bundle.updater
            or updater_config is not None
            and Path(updater_config).resolve() != bundle.updater_config
            or signer_subject is not None
            and signer_subject != bundle.signer_subject
        ):
            raise Mt5PendingUpdateStoreError(
                "MT5 update callback metadata does not match its receipt"
            )
        assert bundle.receipt_id is not None
        with self.lock:
            self._ensure_root()
            destination = self._destination(bundle.receipt_id)
            if destination.exists():
                receipt = self._load_receipt(destination)
                if (
                    receipt.bundle_manifest_sha256
                    != bundle.bundle_manifest_sha256
                    or receipt.source_release != bundle.source_release
                    or receipt.managed_assets_manifest_sha256
                    != bundle.managed_assets_manifest_sha256
                    or receipt.target_release != bundle.target_release
                    or receipt.signer_subject != bundle.signer_subject
                ):
                    raise Mt5PendingUpdateStoreError(
                        "MT5 pending update collision detected"
                    )
                return receipt

            staging = self.root / f".{bundle.receipt_id}.{uuid4()}.staging"
            try:
                staging.mkdir()
                WindowsSecretStore.restrict_shared_service_acl(staging)
                for entry in bundle.files:
                    source = bundle.root / str(entry["name"])
                    target = staging / source.name
                    shutil.copy2(source, target)
                    if (
                        target.stat().st_size != entry["size"]
                        or _sha256(target) != entry["sha256"]
                    ):
                        raise Mt5PendingUpdateStoreError(
                            "MT5 pending update copy integrity failed"
                        )
                    WindowsSecretStore.restrict_shared_service_acl(target)
                (staging / "temp").mkdir()
                WindowsSecretStore.restrict_shared_service_acl(staging / "temp")
                captured_at = max(
                    int(time.time() * 1000),
                    bundle.health_verified_at_unix_ms,
                )
                atomic_json(
                    staging / PENDING_UPDATE_RECEIPT_NAME,
                    self._receipt_document(bundle, captured_at),
                )
                WindowsSecretStore.restrict_shared_service_acl(
                    staging / PENDING_UPDATE_RECEIPT_NAME
                )
                InstanceProvisioner._sync_tree(staging)
                self._load_receipt(staging)
                durable_replace(staging, destination)
                fsync_directory(self.root)
                WindowsSecretStore.restrict_shared_service_acl(destination)
                return self._load_receipt(destination)
            finally:
                if staging.exists():
                    self._remove_tree(staging)

    def _load_receipt(self, root: Path) -> Mt5PendingUpdateReceipt:
        if (
            InstanceProvisioner._is_reparse_point(root)
            or not root.is_dir()
        ):
            raise Mt5PendingUpdateStoreError("MT5 pending update is unsafe")
        receipt_path = root / PENDING_UPDATE_RECEIPT_NAME
        if (
            InstanceProvisioner._is_reparse_point(receipt_path)
            or not receipt_path.is_file()
        ):
            raise Mt5PendingUpdateStoreError("MT5 pending update receipt is invalid")
        try:
            value = read_json(receipt_path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise Mt5PendingUpdateStoreError(
                "MT5 pending update receipt is invalid"
            ) from exc
        expected_fields = {
            "schema_version",
            "status",
            "receipt_id",
            "source_connection_id",
            "source_release",
            "managed_assets_manifest_sha256",
            "target_release",
            "signer_subject",
            "updater_name",
            "updater_config_name",
            "files",
            "bundle_manifest_sha256",
            "source_staged_at_unix_ms",
            "source_applied_at_unix_ms",
            "source_health_verified_at_unix_ms",
            "captured_at_unix_ms",
        }
        if (
            set(value) != expected_fields
            or value.get("schema_version") != _STORE_SCHEMA_VERSION
            or value.get("status") != "READY"
            or not isinstance(value.get("receipt_id"), str)
            or not _RECEIPT_ID.fullmatch(value["receipt_id"])
            or root.name != value["receipt_id"]
            and not root.name.startswith(f".{value['receipt_id']}.")
            or not isinstance(value.get("source_connection_id"), str)
            or not isinstance(value.get("managed_assets_manifest_sha256"), str)
            or not _SHA256.fullmatch(value["managed_assets_manifest_sha256"])
            or not _valid_signer(value.get("signer_subject"))
            or value.get("updater_config_name") != UPDATER_ONLY_CONFIG_NAME
            or not isinstance(value.get("updater_name"), str)
            or str(value["updater_name"]).casefold() != "terminal64.exe"
            or not isinstance(value.get("bundle_manifest_sha256"), str)
            or not _SHA256.fullmatch(value["bundle_manifest_sha256"])
        ):
            raise Mt5PendingUpdateStoreError("MT5 pending update receipt is invalid")
        timestamps = (
            value.get("source_staged_at_unix_ms"),
            value.get("source_applied_at_unix_ms"),
            value.get("source_health_verified_at_unix_ms"),
            value.get("captured_at_unix_ms"),
        )
        if (
            any(type(item) is not int or item <= 0 for item in timestamps)
            or list(timestamps) != sorted(timestamps)
        ):
            raise Mt5PendingUpdateStoreError("MT5 pending update receipt is invalid")
        try:
            source_connection_id = canonical_uuid(value["source_connection_id"])
        except ValueError as exc:
            raise Mt5PendingUpdateStoreError(
                "MT5 pending update receipt is invalid"
            ) from exc
        source_release = Mt5UpdateRelease.from_dict(value.get("source_release"))
        target_release = Mt5UpdateRelease.from_dict(value.get("target_release"))
        files = _validate_files_document(value.get("files"))
        if (
            _manifest_digest(files) != value["bundle_manifest_sha256"]
            or _receipt_id(
                source_release,
                target_release,
                value["managed_assets_manifest_sha256"],
                value["signer_subject"],
                value["bundle_manifest_sha256"],
            )
            != value["receipt_id"]
        ):
            raise Mt5PendingUpdateStoreError("MT5 pending update receipt is invalid")
        expected_names = {
            str(entry["name"]) for entry in files
        } | {PENDING_UPDATE_RECEIPT_NAME, "temp"}
        try:
            children = {child.name: child for child in root.iterdir()}
        except OSError as exc:
            raise Mt5PendingUpdateStoreError(
                "MT5 pending update is unavailable"
            ) from exc
        if set(children) != expected_names:
            raise Mt5PendingUpdateStoreError("MT5 pending update contents are invalid")
        temp = children["temp"]
        if (
            InstanceProvisioner._is_reparse_point(temp)
            or not temp.is_dir()
            or any(temp.iterdir())
        ):
            raise Mt5PendingUpdateStoreError("MT5 pending update contents are invalid")
        for entry in files:
            path = children[str(entry["name"])]
            if (
                InstanceProvisioner._is_reparse_point(path)
                or not path.is_file()
                or path.stat().st_size != entry["size"]
                or _sha256(path) != entry["sha256"]
            ):
                raise Mt5PendingUpdateStoreError(
                    "MT5 pending update integrity failed"
                )
        updater_config = root / value["updater_config_name"]
        if updater_config.read_bytes() != UPDATER_ONLY_CONFIG_BYTES:
            raise Mt5PendingUpdateStoreError("MT5 updater config is not sanitized")
        return Mt5PendingUpdateReceipt(
            root=root,
            receipt_id=value["receipt_id"],
            source_connection_id=source_connection_id,
            source_release=source_release,
            managed_assets_manifest_sha256=value[
                "managed_assets_manifest_sha256"
            ],
            target_release=target_release,
            signer_subject=value["signer_subject"],
            updater=root / value["updater_name"],
            updater_config=updater_config,
            bundle_manifest_sha256=value["bundle_manifest_sha256"],
            captured_at_unix_ms=value["captured_at_unix_ms"],
        )

    def pending(self) -> tuple[Mt5PendingUpdateReceipt, ...]:
        with self.lock:
            self._ensure_root()
            receipts: list[Mt5PendingUpdateReceipt] = []
            for child in sorted(self.root.iterdir(), key=lambda path: path.name):
                if _TRANSIENT_ENTRY.fullmatch(child.name):
                    # A service crash can leave a never-published copy. It is
                    # not READY and is deliberately ignored until startup
                    # cleanup removes it.
                    continue
                if _QUARANTINED_ENTRY.fullmatch(child.name):
                    # A receipt whose exact source release no longer exists is
                    # retained for diagnosis but can never be promoted onto a
                    # different golden template.
                    self._load_receipt(child)
                    continue
                if not _RECEIPT_ID.fullmatch(child.name):
                    raise Mt5PendingUpdateStoreError(
                        "MT5 pending update store contains an invalid entry"
                    )
                receipts.append(self._load_receipt(child))
            return tuple(
                sorted(
                    receipts,
                    key=lambda receipt: (
                        receipt.captured_at_unix_ms,
                        receipt.receipt_id,
                    ),
                )
            )

    def quarantine(self, receipt_id: str) -> Path | None:
        """Durably hide an inapplicable receipt without deleting evidence."""

        with self.lock:
            destination = self._destination(receipt_id)
            if not destination.exists():
                return None
            self._load_receipt(destination)
            quarantined = (
                self.root
                / f".{receipt_id}.{uuid4()}.quarantined"
            )
            durable_replace(destination, quarantined)
            fsync_directory(self.root)
            logger.warning(
                "quarantined stale MT5 pending update receipt %s",
                receipt_id,
            )
            return quarantined

    def complete(self, receipt_id: str) -> None:
        """Remove a receipt only after the caller committed or skipped it."""

        with self.lock:
            destination = self._destination(receipt_id)
            if not destination.exists():
                return
            self._load_receipt(destination)
            deleting = self.root / f".{receipt_id}.{uuid4()}.deleting"
            durable_replace(destination, deleting)
            fsync_directory(self.root)
            try:
                self._remove_tree(deleting)
            except (OSError, Mt5PendingUpdateStoreError):
                # The READY name is already durably gone. Cleanup is retried by
                # _ensure_root without making a committed maintenance pass
                # appear to have failed.
                logger.warning("MT5 pending update cleanup deferred")
