from __future__ import annotations

"""Validated, side-effect-free broker server resolution.

The registry intentionally does not discover brokers, start MetaTrader, or accept
credentials.  It only turns a broker server label into one of three explicit
outcomes that a caller can act on.
"""

import argparse
import ipaddress
import json
import re
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = 1
DEFAULT_REGISTRY_PATH = Path(__file__).with_name("broker_registry.v1.json")

_TOP_LEVEL_KEYS = frozenset({"schema_version", "revision", "profiles"})
_PROFILE_REQUIRED_KEYS = frozenset(
    {
        "profile_id",
        "broker_id",
        "broker_name",
        "server",
        "environment",
        "aliases",
        "discovery_queries",
        "source",
    }
)
_PROFILE_OPTIONAL_KEYS = frozenset({"connection_target", "target_verified_at"})
_ID_PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?\Z")
# Keep this compatible with the server validation already used by the Windows
# agent.  In particular, paths, URL schemes, control characters, and shell
# punctuation are not valid server labels.
_SERVER_PATTERN = re.compile(r"[A-Za-z0-9._ -]{1,128}\Z")
_DNS_LABEL_PATTERN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")
_BRACKETED_ENDPOINT_PATTERN = re.compile(r"\[([^\]]+)\]:([1-9][0-9]{0,4})\Z")
_UTC_TIMESTAMP_PATTERN = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z\Z"
)


class ResolutionMethod(str, Enum):
    DIRECT_ENDPOINT = "direct_endpoint"
    TERMINAL_DISCOVERY = "terminal_discovery"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class BrokerProfile:
    profile_id: str
    broker_id: str
    broker_name: str
    server: str
    environment: str
    aliases: tuple[str, ...]
    discovery_queries: tuple[str, ...]
    source: str
    connection_target: str | None = None
    target_verified_at: str | None = None

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "profile_id": self.profile_id,
            "broker_id": self.broker_id,
            "broker_name": self.broker_name,
            "server": self.server,
            "environment": self.environment,
            "aliases": list(self.aliases),
            "discovery_queries": list(self.discovery_queries),
            "source": self.source,
        }
        if self.connection_target is not None:
            result["connection_target"] = self.connection_target
            result["target_verified_at"] = self.target_verified_at
        return result


@dataclass(frozen=True)
class BrokerResolution:
    requested_server: str
    method: ResolutionMethod
    expected_server: str | None
    connection_target: str | None
    discovery_queries: tuple[str, ...]
    profile_id: str | None
    broker_id: str | None
    broker_name: str | None
    environment: str | None
    revision: int
    matched_by: str

    def to_dict(self) -> dict[str, object]:
        return {
            "method": self.method.value,
            "requested_server": self.requested_server,
            "expected_server": self.expected_server,
            "connection_target": self.connection_target,
            "discovery_queries": list(self.discovery_queries),
            "profile_id": self.profile_id,
            "broker_id": self.broker_id,
            "broker_name": self.broker_name,
            "environment": self.environment,
            "revision": self.revision,
            "matched_by": self.matched_by,
        }


class BrokerRegistry:
    """An immutable view of a fully validated version-1 registry."""

    def __init__(self, revision: int, profiles: tuple[BrokerProfile, ...]) -> None:
        self._revision = revision
        self._profiles = profiles

        canonical: dict[str, BrokerProfile] = {}
        aliases: dict[str, BrokerProfile] = {}
        claimed_labels: dict[str, str] = {}
        profile_ids: set[str] = set()

        for profile in profiles:
            if profile.profile_id in profile_ids:
                raise ValueError("duplicate profile_id")
            profile_ids.add(profile.profile_id)

            server_key = _server_key(profile.server)
            _claim_label(claimed_labels, server_key, profile.profile_id)
            canonical[server_key] = profile

            for alias in profile.aliases:
                alias_key = _server_key(alias)
                _claim_label(claimed_labels, alias_key, profile.profile_id)
                aliases[alias_key] = profile

        self._canonical = MappingProxyType(canonical)
        self._aliases = MappingProxyType(aliases)

    @property
    def schema_version(self) -> int:
        return SCHEMA_VERSION

    @property
    def revision(self) -> int:
        return self._revision

    @property
    def profiles(self) -> tuple[BrokerProfile, ...]:
        return self._profiles

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> BrokerRegistry:
        if not isinstance(payload, Mapping):
            raise ValueError("registry must be an object")
        _require_exact_keys(payload, _TOP_LEVEL_KEYS, "registry")

        schema_version = payload["schema_version"]
        if type(schema_version) is not int or schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported registry schema_version")

        revision = payload["revision"]
        if type(revision) is not int or revision < 1:
            raise ValueError("registry revision must be a positive integer")

        raw_profiles = payload["profiles"]
        if not isinstance(raw_profiles, list):
            raise ValueError("registry profiles must be a list")
        if not raw_profiles:
            raise ValueError("registry profiles cannot be empty")

        profiles = tuple(_parse_profile(item) for item in raw_profiles)
        return cls(revision=revision, profiles=profiles)

    @classmethod
    def from_path(cls, path: str | Path) -> BrokerRegistry:
        registry_path = Path(path)
        try:
            payload = json.loads(
                registry_path.read_text(encoding="utf-8"),
                object_pairs_hook=_unique_json_object,
            )
        except (json.JSONDecodeError, _DuplicateJsonKey) as exc:
            raise ValueError("registry is not valid JSON") from exc
        return cls.from_dict(payload)

    @classmethod
    def default(cls) -> BrokerRegistry:
        return cls.from_path(DEFAULT_REGISTRY_PATH)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "revision": self.revision,
            "profiles": [profile.to_dict() for profile in self.profiles],
        }

    def resolve(self, requested_server: str) -> BrokerResolution:
        requested_server = _validated_server(requested_server, "requested_server")
        key = _server_key(requested_server)

        profile = self._canonical.get(key)
        if profile is not None:
            matched_by = "exact" if requested_server == profile.server else "casefold"
        else:
            profile = self._aliases.get(key)
            matched_by = "alias" if profile is not None else "none"

        if profile is None:
            return BrokerResolution(
                requested_server=requested_server,
                method=ResolutionMethod.UNRESOLVED,
                expected_server=None,
                connection_target=None,
                discovery_queries=(),
                profile_id=None,
                broker_id=None,
                broker_name=None,
                environment=None,
                revision=self.revision,
                matched_by=matched_by,
            )

        method = (
            ResolutionMethod.DIRECT_ENDPOINT
            if profile.connection_target is not None
            else ResolutionMethod.TERMINAL_DISCOVERY
        )
        return BrokerResolution(
            requested_server=requested_server,
            method=method,
            expected_server=profile.server,
            connection_target=profile.connection_target,
            discovery_queries=profile.discovery_queries,
            profile_id=profile.profile_id,
            broker_id=profile.broker_id,
            broker_name=profile.broker_name,
            environment=profile.environment,
            revision=self.revision,
            matched_by=matched_by,
        )


def _parse_profile(raw: object) -> BrokerProfile:
    if not isinstance(raw, Mapping):
        raise ValueError("registry profile must be an object")
    _require_allowed_and_required_keys(
        raw,
        required=_PROFILE_REQUIRED_KEYS,
        optional=_PROFILE_OPTIONAL_KEYS,
        context="registry profile",
    )

    profile_id = _validated_id(raw["profile_id"], "profile_id")
    broker_id = _validated_id(raw["broker_id"], "broker_id")
    broker_name = _validated_clean_text(raw["broker_name"], "broker_name", 128)
    server = _validated_server(raw["server"], "server")

    environment = raw["environment"]
    if not isinstance(environment, str) or environment not in {"live", "demo"}:
        raise ValueError("environment must be live or demo")

    aliases = _validated_server_list(raw["aliases"], "aliases")
    discovery_queries = _validated_text_list(
        raw["discovery_queries"], "discovery_queries", max_length=128
    )
    if not discovery_queries:
        raise ValueError("discovery_queries cannot be empty")
    source = _validated_clean_text(raw["source"], "source", 256)

    has_target = "connection_target" in raw
    has_timestamp = "target_verified_at" in raw
    if has_target != has_timestamp:
        raise ValueError("connection_target and target_verified_at must be provided together")

    connection_target: str | None = None
    target_verified_at: str | None = None
    if has_target:
        connection_target = _validated_connection_target(raw["connection_target"])
        target_verified_at = _validated_utc_timestamp(raw["target_verified_at"])

    return BrokerProfile(
        profile_id=profile_id,
        broker_id=broker_id,
        broker_name=broker_name,
        server=server,
        environment=environment,
        aliases=aliases,
        discovery_queries=discovery_queries,
        source=source,
        connection_target=connection_target,
        target_verified_at=target_verified_at,
    )


def _require_exact_keys(
    payload: Mapping[str, Any], expected: frozenset[str], context: str
) -> None:
    keys = set(payload.keys())
    if keys != expected:
        raise ValueError(f"{context} has missing or unsupported fields")


def _require_allowed_and_required_keys(
    payload: Mapping[str, Any],
    *,
    required: frozenset[str],
    optional: frozenset[str],
    context: str,
) -> None:
    keys = set(payload.keys())
    if not required.issubset(keys) or not keys.issubset(required | optional):
        raise ValueError(f"{context} has missing or unsupported fields")


def _validated_id(value: object, field: str) -> str:
    if not isinstance(value, str) or _ID_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field} is invalid")
    return value


def _validated_server(value: object, field: str) -> str:
    if not isinstance(value, str) or _SERVER_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field} is invalid")
    if value != value.strip():
        raise ValueError(f"{field} cannot have surrounding whitespace")
    return value


def _validated_clean_text(value: object, field: str, max_length: int) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= max_length:
        raise ValueError(f"{field} is invalid")
    contains_control = any(
        ord(character) < 32 or ord(character) == 127 for character in value
    )
    if value != value.strip() or contains_control:
        raise ValueError(f"{field} is invalid")
    return value


def _validated_server_list(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list")
    result = tuple(_validated_server(item, field) for item in value)
    if len({_server_key(item) for item in result}) != len(result):
        raise ValueError(f"{field} contains duplicates")
    return result


def _validated_text_list(value: object, field: str, max_length: int) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list")
    result = tuple(_validated_clean_text(item, field, max_length) for item in value)
    if any("/" in item or "\\" in item for item in result):
        raise ValueError(f"{field} cannot contain path separators")
    if len({item.casefold() for item in result}) != len(result):
        raise ValueError(f"{field} contains duplicates")
    return result


def _validated_connection_target(value: object) -> str:
    if not isinstance(value, str) or value != value.strip() or not value:
        raise ValueError("connection_target is invalid")

    bracketed = _BRACKETED_ENDPOINT_PATTERN.fullmatch(value)
    if bracketed is not None:
        host, port_text = bracketed.groups()
        if "%" in host:
            raise ValueError("connection_target contains an invalid IPv6 address")
        try:
            ipaddress.IPv6Address(host)
        except ipaddress.AddressValueError as exc:
            raise ValueError("connection_target contains an invalid IPv6 address") from exc
        _validated_port(port_text)
        return value

    if value.count(":") != 1:
        raise ValueError("connection_target must use host:port")
    host, port_text = value.rsplit(":", 1)
    _validated_port(port_text)
    _validated_host(host)
    return value


def _validated_host(host: str) -> None:
    if not host or len(host) > 253 or host.endswith("."):
        raise ValueError("connection_target contains an invalid host")

    try:
        ipaddress.IPv4Address(host)
        return
    except ipaddress.AddressValueError:
        # An all-numeric dotted value is intended as IPv4 and must not be
        # accepted as a DNS name after IPv4 validation fails.
        if re.fullmatch(r"[0-9.]+", host):
            raise ValueError("connection_target contains an invalid IPv4 address")

    labels = host.split(".")
    if any(_DNS_LABEL_PATTERN.fullmatch(label) is None for label in labels):
        raise ValueError("connection_target contains an invalid DNS name")


def _validated_port(port_text: str) -> int:
    if re.fullmatch(r"[1-9][0-9]{0,4}", port_text) is None:
        raise ValueError("connection_target contains an invalid port")
    port = int(port_text)
    if port > 65535:
        raise ValueError("connection_target contains an invalid port")
    return port


def _validated_utc_timestamp(value: object) -> str:
    if not isinstance(value, str) or _UTC_TIMESTAMP_PATTERN.fullmatch(value) is None:
        raise ValueError("target_verified_at must be an ISO-8601 UTC timestamp ending in Z")
    try:
        datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError("target_verified_at is not a valid timestamp") from exc
    return value


def _server_key(server: str) -> str:
    return server.casefold()


def _claim_label(claimed: dict[str, str], key: str, profile_id: str) -> None:
    if key in claimed:
        raise ValueError("server and alias labels must be unique after normalization")
    claimed[key] = profile_id


class _DuplicateJsonKey(ValueError):
    pass


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey("registry JSON contains duplicate fields")
        result[key] = value
    return result


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Resolve a MetaTrader server label using the local broker registry."
    )
    parser.add_argument(
        "--server",
        required=True,
        help="Exact server label supplied by the customer",
    )
    parser.add_argument(
        "--registry",
        type=Path,
        help="Optional path to a version-1 registry JSON file",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        registry = (
            BrokerRegistry.from_path(args.registry)
            if args.registry
            else BrokerRegistry.default()
        )
        resolution = registry.resolve(args.server)
    except (OSError, TypeError, ValueError):
        # Deliberately do not print paths, registry contents, or untrusted input.
        print(json.dumps({"error": "invalid_registry_or_request", "method": "unresolved"}))
        return 3

    print(json.dumps(resolution.to_dict(), sort_keys=True))
    if resolution.method is ResolutionMethod.DIRECT_ENDPOINT:
        return 0
    if resolution.method is ResolutionMethod.TERMINAL_DISCOVERY:
        return 2
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
