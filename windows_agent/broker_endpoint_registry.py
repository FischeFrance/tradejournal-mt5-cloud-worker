"""Fail-closed publication of broker endpoints verified by a managed MT5 login.

The publisher never sees credentials.  It accepts only the sanitized login
verification artifact produced after investor-mode identity checks, binds it to
the exact process generation and publishes the registry last.  Consequently an
interrupted publication can leave an unreferenced immutable artifact, but never
an endpoint that the read-only resolver can consume without its provenance.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import stat
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from worker.atomic_file import durable_replace

from .broker_endpoint_resolver import (
    BrokerEndpointResolutionError,
    VerifiedBrokerEndpoint,
    _record,
    _reject_secrets,
    resolve_verified_broker_endpoint,
)
from .state_store import atomic_json


REGISTRY_SCHEMA_VERSION = 3
_MAX_JSON_BYTES = 1024 * 1024
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_UUID4 = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_V1_RECORD_FIELDS = {
    "host",
    "port",
    "protocol",
    "status",
    "observed_at_unix_ms",
    "discovery_method",
    "verification_pid",
    "verification_session_id",
    "confidence",
    "artifact_relative_path",
    "artifact_sha256",
}
_V2_RECORD_FIELDS = _V1_RECORD_FIELDS | {
    "server_name",
    "process_creation_time_unix_ms",
}
ENDPOINT_FAILURE_REASONS = frozenset(
    {
        "ENDPOINT_CONNECTION_FAILED",
        "ENDPOINT_CONNECTION_REFUSED",
        "ENDPOINT_PROTOCOL_INCOMPATIBLE",
        "ENDPOINT_SERVER_UNRECOGNIZED",
        "SERVER_IDENTITY_MISMATCH",
    }
)


class BrokerEndpointObservationError(RuntimeError):
    """The MT5 process does not expose one unambiguous established endpoint."""


class BrokerEndpointPublicationError(RuntimeError):
    """The endpoint or its provenance cannot be published safely."""


@dataclass(frozen=True)
class ObservedProcessEndpoint:
    host: str
    port: int
    pid: int
    process_creation_time_unix_ms: int
    observed_at_unix_ms: int
    protocol: str = "TCP/TLS"

    @property
    def server_address(self) -> str:
        address = ipaddress.ip_address(self.host)
        if address.version == 6:
            return f"[{address.compressed}]:{self.port}"
        return f"{address.compressed}:{self.port}"


@dataclass(frozen=True)
class EndpointPromotion:
    broker_label: str
    server_name: str
    verification_session_id: str
    verification_artifact: Path
    verification_artifact_sha256: str
    provenance_artifact: Path
    provenance_artifact_sha256: str
    observation: ObservedProcessEndpoint
    confidence: str = "HIGH"


@dataclass(frozen=True)
class EndpointInvalidation:
    endpoint: VerifiedBrokerEndpoint
    reason: str
    invalidated_at_unix_ms: int
    event_id: str


def _usable_address(value: Any) -> str:
    try:
        address = ipaddress.ip_address(str(value))
    except ValueError as exc:
        raise BrokerEndpointObservationError(
            "process endpoint address is invalid"
        ) from exc
    if (
        address.is_unspecified
        or address.is_multicast
        or address.is_loopback
        or address.is_link_local
    ):
        raise BrokerEndpointObservationError(
            "process endpoint address is not usable"
        )
    return address.compressed


def observe_process_endpoint(
    pid: int,
    *,
    process_factory: Callable[[int], Any] | None = None,
    now_unix_ms: int | None = None,
    sample_count: int = 3,
    sample_interval_seconds: float = 0.25,
    sleep: Callable[[float], None] = time.sleep,
    expected_host: str | None = None,
    expected_port: int | None = None,
) -> ObservedProcessEndpoint:
    """Return a stable established TCP endpoint owned by one PID.

    The intersection across a short bounded observation window removes
    transient updater/CDN sockets. With no expected binding, zero or multiple
    stable endpoints are intentionally ambiguous. A direct MTAPI attempt may
    instead bind the observation to its exact candidate, allowing a broker
    socket to be verified even while MT5 keeps a MetaQuotes CDN socket open.
    """

    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise BrokerEndpointObservationError("process PID is invalid")
    if (
        not isinstance(sample_count, int)
        or isinstance(sample_count, bool)
        or not 1 <= sample_count <= 10
        or not isinstance(sample_interval_seconds, (int, float))
        or isinstance(sample_interval_seconds, bool)
        or not 0 <= sample_interval_seconds <= 2
    ):
        raise BrokerEndpointObservationError(
            "observation window is invalid"
        )
    if (expected_host is None) != (expected_port is None):
        raise BrokerEndpointObservationError("expected endpoint binding is incomplete")
    expected_endpoint: tuple[str, int] | None = None
    if expected_host is not None:
        if (
            not isinstance(expected_port, int)
            or isinstance(expected_port, bool)
            or not 1 <= expected_port <= 65535
        ):
            raise BrokerEndpointObservationError("expected endpoint binding is invalid")
        expected_endpoint = (_usable_address(expected_host), expected_port)
    try:
        import psutil

        process = (
            psutil.Process(pid)
            if process_factory is None
            else process_factory(pid)
        )
        created_at = int(process.create_time() * 1000)
        connections_method = getattr(process, "net_connections", None)
        if not callable(connections_method):
            connections_method = getattr(process, "connections", None)
        if not callable(connections_method):
            raise BrokerEndpointObservationError(
                "process connection inspection is unavailable"
            )
    except BrokerEndpointObservationError:
        raise
    except Exception as exc:
        raise BrokerEndpointObservationError(
            "process connection inspection failed"
        ) from exc
    if created_at <= 0:
        raise BrokerEndpointObservationError(
            "process creation time is unavailable"
        )

    snapshots: list[set[tuple[str, int]]] = []
    try:
        for sample_index in range(sample_count):
            endpoints: set[tuple[str, int]] = set()
            for connection in connections_method(kind="tcp"):
                status = str(getattr(connection, "status", "")).upper()
                established = str(
                    getattr(psutil, "CONN_ESTABLISHED", "")
                ).upper()
                if status not in {"ESTABLISHED", established}:
                    continue
                remote = getattr(connection, "raddr", None)
                if not remote:
                    continue
                host = getattr(remote, "ip", None)
                port = getattr(remote, "port", None)
                if (
                    host is None
                    and isinstance(remote, (tuple, list))
                    and len(remote) >= 2
                ):
                    host, port = remote[0], remote[1]
                if (
                    not isinstance(port, int)
                    or isinstance(port, bool)
                    or not 1 <= port <= 65535
                ):
                    continue
                try:
                    normalized_host = _usable_address(host)
                except BrokerEndpointObservationError:
                    continue
                endpoints.add((normalized_host, port))
            snapshots.append(endpoints)
            if sample_index + 1 < sample_count:
                sleep(float(sample_interval_seconds))
        if int(process.create_time() * 1000) != created_at:
            raise BrokerEndpointObservationError(
                "process generation changed during observation"
            )
    except BrokerEndpointObservationError:
        raise
    except Exception as exc:
        raise BrokerEndpointObservationError(
            "process connection inspection failed"
        ) from exc
    stable_endpoints = set.intersection(*snapshots)
    if expected_endpoint is not None:
        if expected_endpoint not in stable_endpoints:
            raise BrokerEndpointObservationError("expected process endpoint is missing")
        host, port = expected_endpoint
    elif len(stable_endpoints) != 1:
        raise BrokerEndpointObservationError(
            "process endpoint is missing or ambiguous"
        )
    else:
        host, port = next(iter(stable_endpoints))
    observed_at = int(time.time() * 1000) if now_unix_ms is None else now_unix_ms
    if not isinstance(observed_at, int) or isinstance(observed_at, bool) or observed_at <= 0:
        raise BrokerEndpointObservationError("observation timestamp is invalid")
    return ObservedProcessEndpoint(
        host=host,
        port=port,
        pid=pid,
        process_creation_time_unix_ms=created_at,
        observed_at_unix_ms=observed_at,
    )


def _is_reparse_point(path: Path) -> bool:
    value = path.lstat()
    attributes = getattr(value, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return path.is_symlink() or bool(attributes & reparse_flag)


def _read_json(path: Path, name: str) -> dict[str, Any]:
    try:
        if _is_reparse_point(path) or not path.is_file():
            raise BrokerEndpointPublicationError(
                f"{name} must be a regular file"
            )
        size = path.stat().st_size
        if size <= 0 or size > _MAX_JSON_BYTES:
            raise BrokerEndpointPublicationError(f"{name} size is invalid")
        value = json.loads(path.read_text(encoding="utf-8"))
    except BrokerEndpointPublicationError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BrokerEndpointPublicationError(f"{name} cannot be read") from exc
    if not isinstance(value, dict):
        raise BrokerEndpointPublicationError(f"{name} must be an object")
    _reject_secrets(value)
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise BrokerEndpointPublicationError("artifact cannot be read") from exc
    return digest.hexdigest()


def _validate_text(value: Any, name: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 128
        or any(character in value for character in "\r\n")
    ):
        raise BrokerEndpointPublicationError(f"{name} is invalid")
    return value.strip()


def _broker_key(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def _validate_v3_registry(value: Mapping[str, Any]) -> dict[str, Any]:
    if set(value) != {
        "schema_version",
        "updated_at_unix_ms",
        "brokers",
    }:
        raise BrokerEndpointPublicationError(
            "registry fields do not match registry v3"
        )
    if value["schema_version"] != REGISTRY_SCHEMA_VERSION:
        raise BrokerEndpointPublicationError("registry schema is unsupported")
    updated = value["updated_at_unix_ms"]
    brokers = value["brokers"]
    if not isinstance(updated, int) or isinstance(updated, bool) or updated <= 0:
        raise BrokerEndpointPublicationError("registry timestamp is invalid")
    if not isinstance(brokers, Mapping):
        raise BrokerEndpointPublicationError("registry brokers are invalid")
    normalized = dict(value)
    normalized["brokers"] = {}
    seen_keys: set[str] = set()
    for broker_label, records in brokers.items():
        broker = _validate_text(broker_label, "broker label")
        key = _broker_key(broker)
        if not key or key in seen_keys or not isinstance(records, list):
            raise BrokerEndpointPublicationError(
                "registry broker entry is invalid or ambiguous"
            )
        seen_keys.add(key)
        try:
            parsed_records = [_record(broker, record) for record in records]
        except BrokerEndpointResolutionError as exc:
            raise BrokerEndpointPublicationError(
                "registry endpoint record is invalid"
            ) from exc
        normalized["brokers"][broker] = [
            dict(record) for record in records
        ]
        if len(parsed_records) != len(records):
            raise BrokerEndpointPublicationError(
                "registry endpoint record is invalid"
            )
    return normalized


def _migrate_v1_registry(value: Mapping[str, Any]) -> dict[str, Any]:
    if set(value) != {
        "schema_version",
        "updated_at_unix_ms",
        "ttl_seconds",
        "brokers",
    } or value.get("schema_version") != 1:
        raise BrokerEndpointPublicationError("legacy registry is invalid")
    updated = value.get("updated_at_unix_ms")
    ttl = value.get("ttl_seconds")
    brokers = value.get("brokers")
    if (
        not isinstance(updated, int)
        or isinstance(updated, bool)
        or updated <= 0
        or not isinstance(ttl, int)
        or isinstance(ttl, bool)
        or ttl <= 0
        or not isinstance(brokers, Mapping)
    ):
        raise BrokerEndpointPublicationError("legacy registry is invalid")
    migrated: dict[str, Any] = {
        "schema_version": REGISTRY_SCHEMA_VERSION,
        "updated_at_unix_ms": updated,
        "brokers": {},
    }
    seen_keys: set[str] = set()
    for raw_broker, raw_records in brokers.items():
        broker = _validate_text(raw_broker, "legacy broker label")
        key = _broker_key(broker)
        if not key or key in seen_keys or not isinstance(raw_records, list):
            raise BrokerEndpointPublicationError(
                "legacy registry broker is invalid or ambiguous"
            )
        seen_keys.add(key)
        converted: list[dict[str, Any]] = []
        for raw in raw_records:
            if not isinstance(raw, Mapping) or set(raw) != _V1_RECORD_FIELDS:
                raise BrokerEndpointPublicationError(
                    "legacy registry endpoint record is invalid"
                )
            # Validate all v1 values through the v3 parser, but deliberately
            # invalidate the record because v1 had no server/process-generation
            # binding and therefore can never be reused safely.
            candidate = dict(raw)
            candidate["status"] = "INVALID"
            candidate["server_name"] = None
            candidate["process_creation_time_unix_ms"] = None
            candidate["invalidated_at_unix_ms"] = updated
            candidate["invalidation_reason"] = (
                "LEGACY_PROVENANCE_INSUFFICIENT"
            )
            candidate["invalidation_event_id"] = candidate[
                "verification_session_id"
            ]
            try:
                _record(broker, candidate)
            except BrokerEndpointResolutionError as exc:
                raise BrokerEndpointPublicationError(
                    "legacy registry endpoint record is invalid"
                ) from exc
            converted.append(candidate)
        migrated["brokers"][broker] = converted
    return migrated


def _migrate_v2_registry(value: Mapping[str, Any]) -> dict[str, Any]:
    if set(value) != {
        "schema_version",
        "updated_at_unix_ms",
        "ttl_seconds",
        "brokers",
    } or value.get("schema_version") != 2:
        raise BrokerEndpointPublicationError("legacy registry is invalid")
    updated = value.get("updated_at_unix_ms")
    ttl = value.get("ttl_seconds")
    brokers = value.get("brokers")
    if (
        not isinstance(updated, int)
        or isinstance(updated, bool)
        or updated <= 0
        or not isinstance(ttl, int)
        or isinstance(ttl, bool)
        or ttl <= 0
        or not isinstance(brokers, Mapping)
    ):
        raise BrokerEndpointPublicationError("legacy registry is invalid")
    migrated: dict[str, Any] = {
        "schema_version": REGISTRY_SCHEMA_VERSION,
        "updated_at_unix_ms": updated,
        "brokers": {},
    }
    seen_keys: set[str] = set()
    for raw_broker, raw_records in brokers.items():
        broker = _validate_text(raw_broker, "legacy broker label")
        key = _broker_key(broker)
        if not key or key in seen_keys or not isinstance(raw_records, list):
            raise BrokerEndpointPublicationError(
                "legacy registry broker is invalid or ambiguous"
            )
        seen_keys.add(key)
        converted: list[dict[str, Any]] = []
        for raw in raw_records:
            if not isinstance(raw, Mapping) or set(raw) != _V2_RECORD_FIELDS:
                raise BrokerEndpointPublicationError(
                    "legacy registry endpoint record is invalid"
                )
            candidate = dict(raw)
            if candidate["status"] == "EXPIRED":
                candidate["status"] = "INVALID"
                candidate["invalidated_at_unix_ms"] = updated
                candidate["invalidation_reason"] = (
                    "LEGACY_STATUS_MIGRATION"
                )
                candidate["invalidation_event_id"] = candidate[
                    "verification_session_id"
                ]
            else:
                candidate["invalidated_at_unix_ms"] = None
                candidate["invalidation_reason"] = None
                candidate["invalidation_event_id"] = None
            try:
                _record(broker, candidate)
            except BrokerEndpointResolutionError as exc:
                raise BrokerEndpointPublicationError(
                    "legacy registry endpoint record is invalid"
                ) from exc
            converted.append(candidate)
        migrated["brokers"][broker] = converted
    return migrated


def _load_registry(path: Path) -> dict[str, Any]:
    if not path.exists():
        now = int(time.time() * 1000)
        return {
            "schema_version": REGISTRY_SCHEMA_VERSION,
            "updated_at_unix_ms": now,
            "brokers": {},
        }
    document = _read_json(path, "endpoint registry")
    if document.get("schema_version") == 1:
        return _migrate_v1_registry(document)
    if document.get("schema_version") == 2:
        return _migrate_v2_registry(document)
    return _validate_v3_registry(document)


def _load_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"files": []}
    manifest = _read_json(path, "artifact manifest")
    if set(manifest) != {"files"} or not isinstance(manifest["files"], list):
        raise BrokerEndpointPublicationError("artifact manifest is invalid")
    seen: set[str] = set()
    for entry in manifest["files"]:
        relative = (
            entry.get("relative_path")
            if isinstance(entry, Mapping)
            else None
        )
        normalized = (
            relative.replace("\\", "/")
            if isinstance(relative, str)
            else ""
        )
        parts = normalized.split("/")
        if (
            not isinstance(entry, Mapping)
            or set(entry) != {"relative_path", "sha256"}
            or not normalized
            or normalized.startswith(("/", "//"))
            or (len(normalized) > 1 and normalized[1] == ":")
            or any(part in ("", ".", "..") for part in parts)
            or entry["relative_path"] in seen
            or not isinstance(entry["sha256"], str)
            or not _SHA256.fullmatch(entry["sha256"])
        ):
            raise BrokerEndpointPublicationError(
                "artifact manifest entry is invalid"
            )
        seen.add(entry["relative_path"])
    return {"files": [dict(entry) for entry in manifest["files"]]}


def _safe_artifact_destination(root: Path, relative: str) -> Path:
    normalized = relative.replace("\\", "/")
    parts = normalized.split("/")
    if (
        not normalized
        or normalized.startswith(("/", "//"))
        or (len(normalized) > 1 and normalized[1] == ":")
        or any(part in ("", ".", "..") for part in parts)
    ):
        raise BrokerEndpointPublicationError("artifact path is unsafe")
    root.mkdir(parents=True, exist_ok=True)
    if _is_reparse_point(root) or not root.is_dir():
        raise BrokerEndpointPublicationError("artifact root is unsafe")
    base = root.resolve(strict=True)
    current = base
    for part in parts:
        current /= part
        if current.exists() and _is_reparse_point(current):
            raise BrokerEndpointPublicationError(
                "artifact reparse point is forbidden"
            )
    try:
        current.resolve(strict=False).relative_to(base)
    except (OSError, ValueError) as exc:
        raise BrokerEndpointPublicationError(
            "artifact path escapes root"
        ) from exc
    return current


def _atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        dir=destination.parent,
    )
    try:
        with source.open("rb") as reader, os.fdopen(descriptor, "wb") as writer:
            for chunk in iter(lambda: reader.read(1024 * 1024), b""):
                writer.write(chunk)
            writer.flush()
            os.fsync(writer.fileno())
        durable_replace(temporary_name, destination)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


class BrokerEndpointRegistryPublisher:
    def __init__(
        self,
        registry_path: str | Path,
        *,
        artifact_root: str | Path,
        artifact_manifest: str | Path,
    ) -> None:
        self.registry_path = Path(registry_path)
        self.artifact_root = Path(artifact_root)
        self.artifact_manifest = Path(artifact_manifest)

    def publish(self, promotion: EndpointPromotion) -> VerifiedBrokerEndpoint:
        broker_label = _validate_text(promotion.broker_label, "broker label")
        server_name = _validate_text(promotion.server_name, "server name")
        run_id = promotion.verification_session_id
        if not isinstance(run_id, str) or not _UUID4.fullmatch(run_id):
            raise BrokerEndpointPublicationError(
                "verification session id is invalid"
            )
        if promotion.confidence not in {"MEDIUM", "HIGH"}:
            raise BrokerEndpointPublicationError(
                "verified endpoint confidence is invalid"
            )
        observation = promotion.observation
        try:
            host = _usable_address(observation.host)
        except BrokerEndpointObservationError as exc:
            raise BrokerEndpointPublicationError(
                "observed endpoint address is invalid"
            ) from exc
        if (
            not isinstance(observation.port, int)
            or isinstance(observation.port, bool)
            or not 1 <= observation.port <= 65535
            or observation.protocol != "TCP/TLS"
            or observation.pid <= 0
            or observation.process_creation_time_unix_ms <= 0
            or observation.observed_at_unix_ms <= 0
        ):
            raise BrokerEndpointPublicationError(
                "observed endpoint binding is invalid"
            )
        source = Path(promotion.verification_artifact)
        if (
            not source.is_file()
            or _is_reparse_point(source)
            or not _SHA256.fullmatch(
                promotion.verification_artifact_sha256
            )
            or _sha256(source)
            != promotion.verification_artifact_sha256
        ):
            raise BrokerEndpointPublicationError(
                "verification artifact digest is invalid"
            )
        provenance_source = Path(promotion.provenance_artifact)
        if (
            not provenance_source.is_file()
            or _is_reparse_point(provenance_source)
            or not _SHA256.fullmatch(promotion.provenance_artifact_sha256)
            or _sha256(provenance_source)
            != promotion.provenance_artifact_sha256
        ):
            raise BrokerEndpointPublicationError(
                "verification provenance digest is invalid"
            )
        _read_json(provenance_source, "verification provenance")
        evidence = _read_json(source, "verification artifact")
        expected_evidence = {
            "schema_version": 3,
            "verification_session_id": run_id,
            "server_name": server_name,
            "broker_label": broker_label,
            "protocol": "TCP/TLS",
            "verification_method": "managed_investor_login",
            "login_verified": True,
            "investor_read_only_verified": True,
            "verification_pid": observation.pid,
            "process_creation_time_unix_ms": (
                observation.process_creation_time_unix_ms
            ),
            "verified_at_unix_ms": observation.observed_at_unix_ms,
            "remote_host": host,
            "remote_port": observation.port,
            "provenance_kind": evidence.get("provenance_kind"),
            "provenance_artifact_sha256": promotion.provenance_artifact_sha256,
        }
        if expected_evidence["provenance_kind"] not in {"BROKER_WIZARD", "MTAPI_SEARCH"}:
            raise BrokerEndpointPublicationError("verification provenance kind is invalid")
        if evidence != expected_evidence:
            raise BrokerEndpointPublicationError(
                "verification artifact binding is invalid"
            )

        original_registry = (
            _read_json(self.registry_path, "endpoint registry")
            if self.registry_path.exists()
            else None
        )
        original_manifest = (
            _read_json(self.artifact_manifest, "artifact manifest")
            if self.artifact_manifest.exists()
            else None
        )
        registry = _load_registry(self.registry_path)
        manifest = _load_manifest(self.artifact_manifest)
        for entry in manifest["files"]:
            existing_artifact = _safe_artifact_destination(
                self.artifact_root,
                entry["relative_path"],
            )
            if (
                not existing_artifact.is_file()
                or _is_reparse_point(existing_artifact)
                or _sha256(existing_artifact) != entry["sha256"]
            ):
                raise BrokerEndpointPublicationError(
                    "existing artifact manifest binding is invalid"
                )
        relative = (
            f"artifacts/{run_id}/endpoint-verification.json"
        )
        provenance_relative = f"artifacts/{run_id}/login-provenance.json"
        destination = _safe_artifact_destination(
            self.artifact_root,
            relative,
        )
        provenance_destination = _safe_artifact_destination(
            self.artifact_root,
            provenance_relative,
        )
        files = [
            entry
            for entry in manifest["files"]
            if entry["relative_path"] not in {relative, provenance_relative}
        ]
        files.append(
            {
                "relative_path": relative,
                "sha256": promotion.verification_artifact_sha256,
            }
        )
        files.append(
            {
                "relative_path": provenance_relative,
                "sha256": promotion.provenance_artifact_sha256,
            }
        )
        files.sort(key=lambda entry: entry["relative_path"])
        next_manifest = {"files": files}

        broker_matches = [
            label
            for label in registry["brokers"]
            if _broker_key(label) == _broker_key(broker_label)
        ]
        if len(broker_matches) > 1:
            raise BrokerEndpointPublicationError(
                "registry broker identity is ambiguous"
            )
        canonical_broker = (
            broker_matches[0] if broker_matches else broker_label
        )
        records = registry["brokers"].setdefault(canonical_broker, [])
        superseded_at = observation.observed_at_unix_ms
        for record in records:
            if (
                record["status"] == "VERIFIED"
                and isinstance(record["server_name"], str)
                and record["server_name"].casefold()
                == server_name.casefold()
            ):
                record["status"] = "SUPERSEDED"
                record["invalidated_at_unix_ms"] = superseded_at
                record["invalidation_reason"] = (
                    "SUPERSEDED_BY_NEW_VERIFICATION"
                )
                record["invalidation_event_id"] = run_id
        record = {
            "server_name": server_name,
            "host": host,
            "port": observation.port,
            "protocol": "TCP/TLS",
            "status": "VERIFIED",
            "observed_at_unix_ms": observation.observed_at_unix_ms,
            "discovery_method": "MT5_MANAGED_INVESTOR_LOGIN",
            "verification_pid": observation.pid,
            "process_creation_time_unix_ms": (
                observation.process_creation_time_unix_ms
            ),
            "verification_session_id": run_id,
            "confidence": promotion.confidence,
            "artifact_relative_path": relative,
            "artifact_sha256": promotion.verification_artifact_sha256,
            "invalidated_at_unix_ms": None,
            "invalidation_reason": None,
            "invalidation_event_id": None,
        }
        try:
            _record(canonical_broker, record)
        except BrokerEndpointResolutionError as exc:
            raise BrokerEndpointPublicationError(
                "promoted endpoint record is invalid"
            ) from exc
        records.append(record)
        registry["schema_version"] = REGISTRY_SCHEMA_VERSION
        registry["updated_at_unix_ms"] = int(time.time() * 1000)

        destination_existed = destination.exists()
        if destination_existed and (
            _is_reparse_point(destination)
            or _sha256(destination)
            != promotion.verification_artifact_sha256
        ):
            raise BrokerEndpointPublicationError(
                "immutable verification artifact conflicts"
            )
        provenance_existed = provenance_destination.exists()
        if provenance_existed and (
            _is_reparse_point(provenance_destination)
            or _sha256(provenance_destination)
            != promotion.provenance_artifact_sha256
        ):
            raise BrokerEndpointPublicationError(
                "immutable verification provenance conflicts"
            )
        manifest_written = False
        registry_written = False
        try:
            if not destination_existed:
                _atomic_copy(source, destination)
            if not provenance_existed:
                _atomic_copy(provenance_source, provenance_destination)
            # Manifest first, registry last: the read-only resolver cannot
            # observe the endpoint before every immutable artifact exists.
            atomic_json(self.artifact_manifest, next_manifest)
            manifest_written = True
            atomic_json(self.registry_path, registry)
            registry_written = True
            return resolve_verified_broker_endpoint(
                self.registry_path,
                broker_label=canonical_broker,
                server_name=server_name,
                artifact_root=self.artifact_root,
                artifact_manifest=self.artifact_manifest,
            )
        except Exception as exc:
            # Best-effort rollback for ordinary failures.  Crash safety is
            # provided independently by publishing the resolver-visible
            # registry last.
            if registry_written:
                try:
                    if original_registry is None:
                        self.registry_path.unlink(missing_ok=True)
                    else:
                        atomic_json(self.registry_path, original_registry)
                except Exception:
                    pass
            if manifest_written:
                try:
                    if original_manifest is None:
                        self.artifact_manifest.unlink(missing_ok=True)
                    else:
                        atomic_json(
                            self.artifact_manifest,
                            original_manifest,
                        )
                except Exception:
                    pass
            if not destination_existed:
                try:
                    destination.unlink(missing_ok=True)
                except OSError:
                    pass
            if not provenance_existed:
                try:
                    provenance_destination.unlink(missing_ok=True)
                except OSError:
                    pass
            if isinstance(exc, BrokerEndpointPublicationError):
                raise
            if isinstance(exc, BrokerEndpointResolutionError):
                raise BrokerEndpointPublicationError(
                    "published endpoint failed independent resolution"
                ) from exc
            raise BrokerEndpointPublicationError(
                "endpoint publication failed"
            ) from exc

    def invalidate(self, invalidation: EndpointInvalidation) -> None:
        """Atomically invalidate only the exact verified endpoint attempted.

        Authentication failures and environmental failures must never call
        this method. The caller supplies one of the endpoint-specific,
        sanitized reasons in ``ENDPOINT_FAILURE_REASONS``.
        """

        endpoint = invalidation.endpoint
        broker_label = _validate_text(endpoint.broker_label, "broker label")
        server_name = _validate_text(endpoint.server_name, "server name")
        if invalidation.reason not in ENDPOINT_FAILURE_REASONS:
            raise BrokerEndpointPublicationError(
                "endpoint invalidation reason is not endpoint-specific"
            )
        invalidated_at = invalidation.invalidated_at_unix_ms
        if (
            not isinstance(invalidated_at, int)
            or isinstance(invalidated_at, bool)
            or invalidated_at <= 0
            or not isinstance(invalidation.event_id, str)
            or not _UUID4.fullmatch(invalidation.event_id)
        ):
            raise BrokerEndpointPublicationError(
                "endpoint invalidation binding is invalid"
            )
        if not self.registry_path.exists():
            raise BrokerEndpointPublicationError(
                "endpoint registry is unavailable"
            )

        original_registry = _read_json(
            self.registry_path,
            "endpoint registry",
        )
        registry = _load_registry(self.registry_path)
        if invalidated_at < endpoint.observed_at_unix_ms:
            raise BrokerEndpointPublicationError(
                "endpoint invalidation timestamp is stale"
            )
        broker_matches = [
            label
            for label in registry["brokers"]
            if _broker_key(label) == _broker_key(broker_label)
        ]
        if len(broker_matches) != 1:
            raise BrokerEndpointPublicationError(
                "registry broker identity is missing or ambiguous"
            )
        records = registry["brokers"][broker_matches[0]]
        matches = [
            record
            for record in records
            if record["status"] == "VERIFIED"
            and isinstance(record["server_name"], str)
            and record["server_name"].casefold() == server_name.casefold()
            and record["host"] == endpoint.host
            and record["port"] == endpoint.port
            and record["verification_session_id"]
            == endpoint.verification_session_id
            and record["artifact_sha256"] == endpoint.artifact_sha256
        ]
        if len(matches) != 1:
            raise BrokerEndpointPublicationError(
                "verified endpoint invalidation target is missing or ambiguous"
            )
        target = matches[0]
        target["status"] = "INVALID"
        target["invalidated_at_unix_ms"] = invalidated_at
        target["invalidation_reason"] = invalidation.reason
        target["invalidation_event_id"] = invalidation.event_id
        registry["schema_version"] = REGISTRY_SCHEMA_VERSION
        registry["updated_at_unix_ms"] = max(
            registry["updated_at_unix_ms"],
            invalidated_at,
        )
        _validate_v3_registry(registry)

        try:
            atomic_json(self.registry_path, registry)
            persisted = _read_json(
                self.registry_path,
                "endpoint registry",
            )
            _validate_v3_registry(persisted)
            persisted_records = persisted["brokers"][broker_matches[0]]
            persisted_matches = [
                record
                for record in persisted_records
                if record["status"] == "INVALID"
                and record["server_name"] == target["server_name"]
                and record["host"] == target["host"]
                and record["port"] == target["port"]
                and record["verification_session_id"]
                == target["verification_session_id"]
                and record["invalidation_event_id"]
                == invalidation.event_id
            ]
            if len(persisted_matches) != 1:
                raise BrokerEndpointPublicationError(
                    "endpoint invalidation verification failed"
                )
        except Exception as exc:
            try:
                atomic_json(self.registry_path, original_registry)
            except Exception:
                pass
            if isinstance(exc, BrokerEndpointPublicationError):
                raise
            raise BrokerEndpointPublicationError(
                "endpoint invalidation failed"
            ) from exc
