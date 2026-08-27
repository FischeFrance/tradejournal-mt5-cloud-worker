"""Independent, read-only consumer for the verified broker endpoint registry.

Publication and invalidation use a separate fail-closed component. Resolution
never mutates records and never imports laboratory code.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


_REGISTRY_SCHEMA_VERSION = 3
_MAX_JSON_BYTES = 1024 * 1024
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RUN_ID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_SECRET_KEYS = re.compile(
    r"(?:password|passwd|token|secret|credential|hmac)", re.IGNORECASE
)
_RECORD_FIELDS = {
    "server_name",
    "host",
    "port",
    "protocol",
    "status",
    "observed_at_unix_ms",
    "discovery_method",
    "verification_pid",
    "process_creation_time_unix_ms",
    "verification_session_id",
    "confidence",
    "artifact_relative_path",
    "artifact_sha256",
    "invalidated_at_unix_ms",
    "invalidation_reason",
    "invalidation_event_id",
}
_VERIFIED_METHODS = {
    "MT5_LOGIN_DIALOG_IP",
    "MT5_NONINTERACTIVE_CONFIG",
    "MT5_MANAGED_INVESTOR_LOGIN",
}
_CONFIDENCES = {"LOW", "MEDIUM", "HIGH"}
_STATUSES = {
    "VERIFIED",
    "INVALID",
    "SUPERSEDED",
    "CANDIDATE",
    "METAQUOTES_CDN",
}
_INVALIDATION_REASONS = {
    "ENDPOINT_CONNECTION_FAILED",
    "ENDPOINT_CONNECTION_REFUSED",
    "ENDPOINT_PROTOCOL_INCOMPATIBLE",
    "ENDPOINT_SERVER_UNRECOGNIZED",
    "SERVER_IDENTITY_MISMATCH",
    "SUPERSEDED_BY_NEW_VERIFICATION",
    "LEGACY_PROVENANCE_INSUFFICIENT",
    "LEGACY_STATUS_MIGRATION",
}


class BrokerEndpointResolutionError(ValueError):
    """The registry cannot produce one trustworthy endpoint."""


@dataclass(frozen=True)
class VerifiedBrokerEndpoint:
    broker_label: str
    server_name: str | None
    host: str
    port: int
    protocol: str
    observed_at_unix_ms: int
    discovery_method: str
    verification_pid: int
    process_creation_time_unix_ms: int | None
    verification_session_id: str
    confidence: str
    artifact_relative_path: str
    artifact_sha256: str

    @property
    def server_address(self) -> str:
        address = ipaddress.ip_address(self.host)
        if address.version == 6:
            return f"[{address.compressed}]:{self.port}"
        return f"{address.compressed}:{self.port}"


def _is_reparse_point(path: Path) -> bool:
    try:
        value = path.lstat()
    except OSError as exc:
        raise BrokerEndpointResolutionError("registry path cannot be inspected") from exc
    attributes = getattr(value, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return path.is_symlink() or bool(attributes & reparse_flag)


def _read_json(path: Path, name: str) -> Mapping[str, Any]:
    if _is_reparse_point(path) or not path.is_file():
        raise BrokerEndpointResolutionError(f"{name} must be a regular file")
    try:
        if path.stat().st_size <= 0 or path.stat().st_size > _MAX_JSON_BYTES:
            raise BrokerEndpointResolutionError(f"{name} size is invalid")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BrokerEndpointResolutionError(f"{name} cannot be read") from exc
    if not isinstance(value, Mapping):
        raise BrokerEndpointResolutionError(f"{name} must be an object")
    return value


def _reject_secrets(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if _SECRET_KEYS.search(str(key)):
                raise BrokerEndpointResolutionError("secret-bearing registry field is forbidden")
            _reject_secrets(child)
    elif isinstance(value, list):
        for child in value:
            _reject_secrets(child)


def _artifact_path(root: Path, relative: Any) -> Path:
    if not isinstance(relative, str) or not relative or "\x00" in relative:
        raise BrokerEndpointResolutionError("artifact_relative_path is invalid")
    normalized = relative.replace("\\", "/")
    parts = normalized.split("/")
    if (
        normalized.startswith(("/", "//"))
        or (len(normalized) > 1 and normalized[1] == ":")
        or any(part in ("", ".", "..") for part in parts)
    ):
        raise BrokerEndpointResolutionError("artifact path is unsafe")
    if _is_reparse_point(root):
        raise BrokerEndpointResolutionError("artifact root is unsafe")
    try:
        base = root.resolve(strict=True)
    except OSError as exc:
        raise BrokerEndpointResolutionError("artifact root cannot be resolved") from exc
    if not base.is_dir() or _is_reparse_point(base):
        raise BrokerEndpointResolutionError("artifact root is unsafe")
    current = base
    for part in parts:
        current /= part
        if current.exists() and _is_reparse_point(current):
            raise BrokerEndpointResolutionError("artifact reparse point is forbidden")
    try:
        current.resolve(strict=False).relative_to(base)
    except (OSError, ValueError) as exc:
        raise BrokerEndpointResolutionError("artifact path escapes root") from exc
    return current


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise BrokerEndpointResolutionError("artifact cannot be read") from exc
    return digest.hexdigest()


def _record(broker_label: str, value: Any) -> VerifiedBrokerEndpoint:
    if not isinstance(value, Mapping) or set(value) != _RECORD_FIELDS:
        raise BrokerEndpointResolutionError("endpoint record fields do not match registry v3")
    try:
        address = ipaddress.ip_address(str(value["host"]))
    except ValueError as exc:
        raise BrokerEndpointResolutionError("endpoint host is invalid") from exc
    if (
        address.is_unspecified
        or address.is_multicast
        or address.is_loopback
        or address.is_link_local
    ):
        raise BrokerEndpointResolutionError("endpoint host is not usable")
    port = value["port"]
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise BrokerEndpointResolutionError("endpoint port is invalid")
    if value["protocol"] != "TCP/TLS":
        raise BrokerEndpointResolutionError("endpoint protocol is unsupported")
    status = value["status"]
    if status not in _STATUSES:
        raise BrokerEndpointResolutionError("endpoint status is invalid")
    discovery_method = value["discovery_method"]
    if not isinstance(discovery_method, str) or not discovery_method:
        raise BrokerEndpointResolutionError("endpoint discovery method is invalid")
    if status == "VERIFIED" and discovery_method not in _VERIFIED_METHODS:
        raise BrokerEndpointResolutionError("verified endpoint method is not verifiable")
    server_name = value["server_name"]
    legacy_invalid = (
        status == "INVALID"
        and value.get("invalidation_reason")
        in {
            "LEGACY_PROVENANCE_INSUFFICIENT",
            "LEGACY_STATUS_MIGRATION",
        }
    )
    if legacy_invalid and server_name is None:
        normalized_server_name = None
    elif (
        not isinstance(server_name, str)
        or not server_name.strip()
        or len(server_name) > 128
        or any(character in server_name for character in "\r\n")
    ):
        raise BrokerEndpointResolutionError("endpoint server name is invalid")
    else:
        normalized_server_name = server_name.strip()
    observed = value["observed_at_unix_ms"]
    if not isinstance(observed, int) or isinstance(observed, bool) or observed <= 0:
        raise BrokerEndpointResolutionError("endpoint observation timestamp is invalid")
    pid = value["verification_pid"]
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise BrokerEndpointResolutionError("endpoint verification PID is invalid")
    process_creation_time = value["process_creation_time_unix_ms"]
    if legacy_invalid and process_creation_time is None:
        normalized_process_creation_time = None
    elif (
        not isinstance(process_creation_time, int)
        or isinstance(process_creation_time, bool)
        or process_creation_time <= 0
    ):
        raise BrokerEndpointResolutionError(
            "endpoint process creation time is invalid"
        )
    else:
        normalized_process_creation_time = process_creation_time
    session_id = value["verification_session_id"]
    if not isinstance(session_id, str) or not _RUN_ID.fullmatch(session_id):
        raise BrokerEndpointResolutionError("endpoint verification session is invalid")
    confidence = value["confidence"]
    if confidence not in _CONFIDENCES:
        raise BrokerEndpointResolutionError("endpoint confidence is invalid")
    digest = value["artifact_sha256"]
    if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
        raise BrokerEndpointResolutionError("endpoint artifact digest is invalid")
    relative = value["artifact_relative_path"]
    if not isinstance(relative, str) or not relative:
        raise BrokerEndpointResolutionError("endpoint artifact path is invalid")
    invalidated_at = value["invalidated_at_unix_ms"]
    invalidation_reason = value["invalidation_reason"]
    invalidation_event_id = value["invalidation_event_id"]
    if status in {"INVALID", "SUPERSEDED"}:
        if (
            not isinstance(invalidated_at, int)
            or isinstance(invalidated_at, bool)
            or invalidated_at <= 0
            or invalidation_reason not in _INVALIDATION_REASONS
            or not isinstance(invalidation_event_id, str)
            or not _RUN_ID.fullmatch(invalidation_event_id)
        ):
            raise BrokerEndpointResolutionError(
                "endpoint invalidation metadata is invalid"
            )
        if (
            status == "SUPERSEDED"
            and invalidation_reason != "SUPERSEDED_BY_NEW_VERIFICATION"
        ):
            raise BrokerEndpointResolutionError(
                "superseded endpoint reason is invalid"
            )
        if (
            status == "INVALID"
            and invalidation_reason == "SUPERSEDED_BY_NEW_VERIFICATION"
        ):
            raise BrokerEndpointResolutionError(
                "invalid endpoint reason is invalid"
            )
    elif (
        invalidated_at is not None
        or invalidation_reason is not None
        or invalidation_event_id is not None
    ):
        raise BrokerEndpointResolutionError(
            "active endpoint contains invalidation metadata"
        )
    return VerifiedBrokerEndpoint(
        broker_label=broker_label,
        server_name=normalized_server_name,
        host=address.compressed,
        port=port,
        protocol=value["protocol"],
        observed_at_unix_ms=observed,
        discovery_method=discovery_method,
        verification_pid=pid,
        process_creation_time_unix_ms=normalized_process_creation_time,
        verification_session_id=session_id,
        confidence=confidence,
        artifact_relative_path=relative,
        artifact_sha256=digest,
    )


def resolve_verified_broker_endpoint(
    registry_path: str | Path,
    *,
    broker_label: str | None,
    server_name: str,
    artifact_root: str | Path,
    artifact_manifest: str | Path,
) -> VerifiedBrokerEndpoint:
    """Return exactly one VERIFIED endpoint with intact provenance.

    Verified endpoints do not expire with time. They remain eligible until an
    endpoint-specific failed attempt atomically changes their status.
    """

    requested_key: str | None = None
    if broker_label is not None:
        requested = broker_label.strip()
        if (
            not requested
            or len(requested) > 128
            or any(character in requested for character in "\r\n")
        ):
            raise BrokerEndpointResolutionError("broker label is invalid")
        requested_key = "".join(
            character
            for character in requested.casefold()
            if character.isalnum()
        )
        if not requested_key:
            raise BrokerEndpointResolutionError("broker label is invalid")
    requested_server = server_name.strip()
    if (
        not requested_server
        or len(requested_server) > 128
        or any(character in requested_server for character in "\r\n")
    ):
        raise BrokerEndpointResolutionError("server name is invalid")
    registry = _read_json(Path(registry_path), "endpoint registry")
    manifest = _read_json(Path(artifact_manifest), "artifact manifest")
    _reject_secrets(registry)
    _reject_secrets(manifest)
    if set(registry) != {"schema_version", "updated_at_unix_ms", "brokers"}:
        raise BrokerEndpointResolutionError(
            "registry fields do not match registry v3"
        )
    if registry["schema_version"] != _REGISTRY_SCHEMA_VERSION:
        raise BrokerEndpointResolutionError("registry schema is unsupported")
    if (
        not isinstance(registry["updated_at_unix_ms"], int)
        or isinstance(registry["updated_at_unix_ms"], bool)
        or registry["updated_at_unix_ms"] <= 0
    ):
        raise BrokerEndpointResolutionError("registry timestamp is invalid")
    brokers = registry["brokers"]
    if not isinstance(brokers, Mapping):
        raise BrokerEndpointResolutionError("registry brokers are invalid")
    validated_brokers: list[
        tuple[str, str, list[Mapping[str, Any]], list[VerifiedBrokerEndpoint]]
    ] = []
    for label, raw_records in brokers.items():
        if (
            not isinstance(label, str)
            or not label.strip()
            or len(label) > 128
            or any(character in label for character in "\r\n")
            or not isinstance(raw_records, list)
        ):
            raise BrokerEndpointResolutionError("registry broker entry is invalid")
        normalized_label = "".join(
            character for character in label.casefold() if character.isalnum()
        )
        if not normalized_label:
            raise BrokerEndpointResolutionError("registry broker label is invalid")
        records = [_record(label, item) for item in raw_records]
        validated_brokers.append(
            (normalized_label, label, raw_records, records)
        )
    matches = [
        (normalized_label, label, raw_records, records)
        for normalized_label, label, raw_records, records in validated_brokers
        if requested_key is None or normalized_label == requested_key
    ]
    if requested_key is not None and len(matches) != 1:
        raise BrokerEndpointResolutionError("broker endpoint is missing or ambiguous")
    verified: list[VerifiedBrokerEndpoint] = []
    for _, _, raw_records, records in matches:
        verified.extend(
            record
            for raw, record in zip(raw_records, records)
            if raw["status"] == "VERIFIED"
            and isinstance(record.server_name, str)
            and record.server_name.casefold()
            == requested_server.casefold()
        )
    if len(verified) != 1:
        raise BrokerEndpointResolutionError("verified broker endpoint is missing or ambiguous")
    selected = verified[0]

    files = manifest.get("files")
    if not isinstance(files, list):
        raise BrokerEndpointResolutionError("artifact manifest is invalid")
    manifest_matches = [
        entry
        for entry in files
        if isinstance(entry, Mapping)
        and entry.get("relative_path") == selected.artifact_relative_path
    ]
    if (
        len(manifest_matches) != 1
        or manifest_matches[0].get("sha256") != selected.artifact_sha256
    ):
        raise BrokerEndpointResolutionError("artifact manifest binding is invalid")
    artifact = _artifact_path(Path(artifact_root), selected.artifact_relative_path)
    if (
        not artifact.is_file()
        or artifact.stat().st_size <= 0
        or _sha256(artifact) != selected.artifact_sha256
    ):
        raise BrokerEndpointResolutionError("artifact digest verification failed")
    return selected
