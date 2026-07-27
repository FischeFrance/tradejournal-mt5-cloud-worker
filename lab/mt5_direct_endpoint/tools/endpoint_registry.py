"""Fail-closed, offline broker endpoint registry.

The registry stores provenance and artifact references only; it never stores
credentials and never launches a terminal.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping


REGISTRY_SCHEMA_VERSION = 1
STATUSES = frozenset({"VERIFIED", "CANDIDATE", "METAQUOTES_CDN", "EXPIRED"})
CONFIDENCES = frozenset({"LOW", "MEDIUM", "HIGH"})
VERIFIED_DISCOVERY_METHODS = frozenset({"MT5_LOGIN_DIALOG_IP", "MT5_NONINTERACTIVE_CONFIG"})
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RUN_ID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_SECRET_KEYS = re.compile(r"(?:password|passwd|token|secret|credential|hmac)", re.I)


class RegistryError(ValueError):
    """Raised when registry or provenance data is not safe to use."""


def _reject_secrets(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if _SECRET_KEYS.search(str(key)):
                raise RegistryError("secret-bearing field is forbidden")
            _reject_secrets(child)
    elif isinstance(value, list):
        for child in value:
            _reject_secrets(child)


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative or "\x00" in relative:
        raise RegistryError("artifact_relative_path is invalid")
    normalized = relative.replace("\\", "/")
    if normalized.startswith(("/", "//")) or (len(normalized) > 1 and normalized[1] == ":"):
        raise RegistryError("artifact path must be relative")
    if any(part in ("", ".", "..") for part in normalized.split("/")):
        raise RegistryError("artifact path traversal is forbidden")
    base = root.resolve(strict=True)
    candidate = base.joinpath(*normalized.split("/"))
    current = base
    for part in normalized.split("/"):
        current = current / part
        if current.is_symlink():
            raise RegistryError("artifact symlink/reparse point is forbidden")
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(base)
    except ValueError as exc:
        raise RegistryError("artifact path escapes artifact root") from exc
    return resolved


def _sha(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise RegistryError(f"{field} must be lowercase SHA-256")
    return value


def _record(record: Mapping[str, Any]) -> dict[str, Any]:
    required = {
        "host", "port", "protocol", "status", "observed_at_unix_ms",
        "discovery_method", "verification_pid", "verification_session_id",
        "confidence", "artifact_relative_path", "artifact_sha256",
    }
    if set(record) != required:
        raise RegistryError("endpoint record fields do not match registry v1")
    try:
        address = ipaddress.ip_address(str(record["host"]))
    except ValueError as exc:
        raise RegistryError("host must be a valid IP address") from exc
    if address.is_unspecified or address.is_multicast or address.is_loopback:
        raise RegistryError("host address is not a usable endpoint")
    port = record["port"]
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise RegistryError("port must be in range 1..65535")
    if record["status"] not in STATUSES:
        raise RegistryError("unknown endpoint status")
    if not isinstance(record["protocol"], str) or not record["protocol"].strip():
        raise RegistryError("protocol is required")
    if not isinstance(record["discovery_method"], str) or not record["discovery_method"].strip():
        raise RegistryError("discovery_method is required")
    if not isinstance(record["observed_at_unix_ms"], int) or record["observed_at_unix_ms"] <= 0:
        raise RegistryError("observed_at_unix_ms is invalid")
    if not isinstance(record["verification_pid"], int) or record["verification_pid"] <= 0:
        raise RegistryError("verification_pid is invalid")
    if not isinstance(record["verification_session_id"], str) or not _RUN_ID.fullmatch(record["verification_session_id"]):
        raise RegistryError("verification_session_id must be UUID4")
    if record["confidence"] not in CONFIDENCES:
        raise RegistryError("confidence is invalid")
    _sha(record["artifact_sha256"], "artifact_sha256")
    if not isinstance(record["artifact_relative_path"], str) or not record["artifact_relative_path"]:
        raise RegistryError("artifact_relative_path is required")
    return dict(record)


def _validate_manifest(artifact_root: Path, manifest_path: Path, relative: str, digest: str) -> None:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RegistryError("artifact manifest cannot be read") from exc
    if not isinstance(manifest, Mapping) or not isinstance(manifest.get("files"), list):
        raise RegistryError("artifact manifest is invalid")
    matches = [entry for entry in manifest["files"] if isinstance(entry, Mapping) and entry.get("relative_path") == relative]
    if len(matches) != 1 or matches[0].get("sha256") != digest:
        raise RegistryError("artifact is absent or digest differs from manifest")
    artifact = _safe_relative(artifact_root, relative)
    if not artifact.is_file() or artifact.stat().st_size == 0 or _digest(artifact) != digest:
        raise RegistryError("artifact is missing, empty or digest-mismatched")


def _payload(registry: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(registry, Mapping):
        raise RegistryError("registry must be an object")
    _reject_secrets(registry)
    if registry.get("schema_version") != REGISTRY_SCHEMA_VERSION:
        raise RegistryError("unsupported registry schema")
    brokers = registry.get("brokers")
    if not isinstance(brokers, Mapping):
        raise RegistryError("brokers must be an object")
    normalized: dict[str, Any] = {"schema_version": 1, "updated_at_unix_ms": registry.get("updated_at_unix_ms"), "ttl_seconds": registry.get("ttl_seconds"), "brokers": {}}
    if not isinstance(normalized["updated_at_unix_ms"], int) or normalized["updated_at_unix_ms"] <= 0:
        raise RegistryError("updated_at_unix_ms is invalid")
    if not isinstance(normalized["ttl_seconds"], int) or normalized["ttl_seconds"] <= 0:
        raise RegistryError("ttl_seconds is invalid")
    for broker, records in brokers.items():
        if not isinstance(broker, str) or not broker.strip() or not isinstance(records, list):
            raise RegistryError("broker records are invalid")
        normalized["brokers"][broker] = [_record(item) for item in records]
    return normalized


def load_registry(
    path: str | Path,
    *,
    artifact_root: str | Path | None = None,
    artifact_manifest: str | Path | None = None,
) -> dict[str, Any]:
    registry_path = Path(path)
    try:
        registry = _payload(json.loads(registry_path.read_text(encoding="utf-8")))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RegistryError("registry cannot be read") from exc
    if artifact_root is not None and artifact_manifest is None:
        raise RegistryError("artifact manifest is required with artifact root")
    if artifact_root is not None and artifact_manifest is not None:
        root = Path(artifact_root)
        for records in registry["brokers"].values():
            for record in records:
                _validate_manifest(root, Path(artifact_manifest), record["artifact_relative_path"], record["artifact_sha256"])
    return registry


def save_registry_atomic(path: str | Path, registry: Mapping[str, Any]) -> None:
    normalized = _payload(registry)
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(normalized, handle, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    except Exception:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def register_verified(
    path: str | Path,
    *,
    broker_label: str,
    host: str,
    port: int,
    protocol: str,
    observed_at_unix_ms: int,
    discovery_method: str,
    verification_pid: int,
    verification_session_id: str,
    confidence: str,
    artifact_root: str | Path,
    artifact_manifest: str | Path,
    artifact_relative_path: str,
    artifact_sha256: str,
    ttl_seconds: int = 86400,
) -> dict[str, Any]:
    if not broker_label.strip():
        raise RegistryError("broker_label is required")
    if discovery_method not in VERIFIED_DISCOVERY_METHODS:
        raise RegistryError("VERIFIED endpoint requires an MT5 login verification method")
    _validate_manifest(Path(artifact_root), Path(artifact_manifest), artifact_relative_path, artifact_sha256)
    try:
        registry = load_registry(path, artifact_root=artifact_root, artifact_manifest=artifact_manifest) if Path(path).exists() else {"schema_version": 1, "updated_at_unix_ms": int(time.time() * 1000), "ttl_seconds": ttl_seconds, "brokers": {}}
    except RegistryError:
        raise
    if registry["ttl_seconds"] != ttl_seconds and Path(path).exists():
        raise RegistryError("ttl_seconds cannot change implicitly")
    record = _record({"host": host, "port": port, "protocol": protocol, "status": "VERIFIED", "observed_at_unix_ms": observed_at_unix_ms, "discovery_method": discovery_method, "verification_pid": verification_pid, "verification_session_id": verification_session_id, "confidence": confidence, "artifact_relative_path": artifact_relative_path, "artifact_sha256": artifact_sha256})
    registry["brokers"].setdefault(broker_label, []).append(record)
    registry["updated_at_unix_ms"] = int(time.time() * 1000)
    save_registry_atomic(path, registry)
    return registry


def resolve_verified(
    path: str | Path,
    *,
    broker_label: str,
    now_unix_ms: int | None = None,
    artifact_root: str | Path | None = None,
    artifact_manifest: str | Path | None = None,
) -> list[dict[str, Any]]:
    registry = load_registry(path, artifact_root=artifact_root, artifact_manifest=artifact_manifest)
    now = int(time.time() * 1000) if now_unix_ms is None else now_unix_ms
    ttl_ms = registry["ttl_seconds"] * 1000
    return [record for record in registry["brokers"].get(broker_label, []) if record["status"] == "VERIFIED" and record["observed_at_unix_ms"] + ttl_ms >= now]
