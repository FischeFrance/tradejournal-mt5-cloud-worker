from __future__ import annotations

import json
import socket
import subprocess
from dataclasses import FrozenInstanceError, is_dataclass
from pathlib import Path
from unittest.mock import patch

import pytest

from windows_agent.broker_registry import (
    BrokerProfile,
    BrokerRegistry,
    BrokerResolution,
    ResolutionMethod,
)


def _profile(
    *,
    profile_id: str = "atlas-live",
    broker_id: str = "atlas-markets",
    broker_name: str = "Atlas Markets Ltd",
    server: str = "AtlasMarkets-Live",
    environment: str = "live",
    aliases: list[str] | None = None,
    discovery_queries: list[str] | None = None,
    connection_target: str | None = "mt5.atlas.example:443",
    target_verified_at: str | None = "2026-07-22T10:00:00Z",
) -> dict[str, object]:
    result: dict[str, object] = {
        "profile_id": profile_id,
        "broker_id": broker_id,
        "broker_name": broker_name,
        "server": server,
        "environment": environment,
        "aliases": aliases if aliases is not None else ["Atlas MT5 Live"],
        "discovery_queries": (
            discovery_queries
            if discovery_queries is not None
            else ["Atlas Markets Ltd", "Atlas Markets"]
        ),
        "source": "unit-test",
    }
    if connection_target is not None:
        result["connection_target"] = connection_target
    if target_verified_at is not None:
        result["target_verified_at"] = target_verified_at
    return result


def _payload(*profiles: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "revision": 1,
        "profiles": list(profiles) if profiles else [_profile()],
    }


def _discovery_profile(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "profile_id": "atlas-demo",
        "server": "AtlasMarkets-Demo",
        "environment": "demo",
        "aliases": ["Atlas MT5 Demo"],
        "discovery_queries": ["Atlas Markets Ltd", "Atlas Markets"],
        "connection_target": None,
        "target_verified_at": None,
    }
    values.update(overrides)
    return _profile(**values)  # type: ignore[arg-type]


def test_resolution_method_wire_values_are_stable() -> None:
    assert ResolutionMethod.DIRECT_ENDPOINT.value == "direct_endpoint"
    assert ResolutionMethod.TERMINAL_DISCOVERY.value == "terminal_discovery"
    assert ResolutionMethod.UNRESOLVED.value == "unresolved"


def test_direct_endpoint_keeps_connection_target_separate_from_expected_server() -> None:
    resolution = BrokerRegistry.from_dict(_payload()).resolve("AtlasMarkets-Live")

    assert resolution.method is ResolutionMethod.DIRECT_ENDPOINT
    assert resolution.requested_server == "AtlasMarkets-Live"
    assert resolution.expected_server == "AtlasMarkets-Live"
    assert resolution.connection_target == "mt5.atlas.example:443"
    assert resolution.connection_target != resolution.expected_server
    assert resolution.discovery_queries == ("Atlas Markets Ltd", "Atlas Markets")
    assert resolution.profile_id == "atlas-live"
    assert resolution.broker_id == "atlas-markets"
    assert resolution.broker_name == "Atlas Markets Ltd"
    assert resolution.environment == "live"
    assert resolution.revision == 1
    assert resolution.matched_by == "exact"


def test_server_matching_supports_exact_casefold_and_explicit_alias() -> None:
    registry = BrokerRegistry.from_dict(_payload())

    exact = registry.resolve("AtlasMarkets-Live")
    folded = registry.resolve("atlasmarkets-live")
    alias = registry.resolve("Atlas MT5 Live")
    folded_alias = registry.resolve("atlas mt5 live")

    assert exact.expected_server == "AtlasMarkets-Live"
    assert exact.matched_by == "exact"
    assert folded.expected_server == "AtlasMarkets-Live"
    assert folded.matched_by == "casefold"
    assert alias.expected_server == "AtlasMarkets-Live"
    assert alias.matched_by == "alias"
    assert folded_alias.expected_server == "AtlasMarkets-Live"
    assert folded_alias.matched_by == "alias"


def test_live_and_demo_profiles_are_distinct_and_never_inferred() -> None:
    registry = BrokerRegistry.from_dict(_payload(_profile(), _discovery_profile()))

    live = registry.resolve("AtlasMarkets-Live")
    demo = registry.resolve("AtlasMarkets-Demo")
    guessed = registry.resolve("AtlasMarkets")

    assert live.environment == "live"
    assert live.method is ResolutionMethod.DIRECT_ENDPOINT
    assert demo.environment == "demo"
    assert demo.method is ResolutionMethod.TERMINAL_DISCOVERY
    assert demo.expected_server == "AtlasMarkets-Demo"
    assert guessed.method is ResolutionMethod.UNRESOLVED


def test_known_profile_without_endpoint_requires_terminal_discovery() -> None:
    resolution = BrokerRegistry.from_dict(_payload(_discovery_profile())).resolve(
        "AtlasMarkets-Demo"
    )

    assert resolution.method is ResolutionMethod.TERMINAL_DISCOVERY
    assert resolution.expected_server == "AtlasMarkets-Demo"
    assert resolution.connection_target is None
    assert resolution.discovery_queries == ("Atlas Markets Ltd", "Atlas Markets")
    assert resolution.profile_id == "atlas-demo"
    assert resolution.matched_by == "exact"


def test_unknown_server_is_unresolved_without_broker_fallback() -> None:
    resolution = BrokerRegistry.from_dict(_payload()).resolve("OtherBroker-Live")

    assert resolution.method is ResolutionMethod.UNRESOLVED
    assert resolution.requested_server == "OtherBroker-Live"
    assert resolution.expected_server is None
    assert resolution.connection_target is None
    assert resolution.discovery_queries == ()
    assert resolution.profile_id is None
    assert resolution.broker_id is None
    assert resolution.broker_name is None
    assert resolution.environment is None
    assert resolution.revision == 1
    assert resolution.matched_by == "none"


def test_models_and_collection_fields_are_immutable() -> None:
    registry = BrokerRegistry.from_dict(_payload())
    resolution = registry.resolve("AtlasMarkets-Live")

    assert is_dataclass(BrokerProfile)
    assert BrokerProfile.__dataclass_params__.frozen is True
    assert is_dataclass(BrokerResolution)
    assert BrokerResolution.__dataclass_params__.frozen is True
    assert isinstance(resolution.discovery_queries, tuple)
    with pytest.raises(FrozenInstanceError):
        resolution.expected_server = "Changed"  # type: ignore[misc]


def test_registry_does_not_retain_mutable_input_collections() -> None:
    raw = _payload()
    registry = BrokerRegistry.from_dict(raw)

    profile = raw["profiles"][0]  # type: ignore[index]
    profile["aliases"].append("Injected Alias")  # type: ignore[index,union-attr]
    profile["discovery_queries"].append("Injected Query")  # type: ignore[index,union-attr]

    assert registry.resolve("Injected Alias").method is ResolutionMethod.UNRESOLVED
    assert registry.resolve("AtlasMarkets-Live").discovery_queries == (
        "Atlas Markets Ltd",
        "Atlas Markets",
    )


@pytest.mark.parametrize(
    "profiles",
    [
        (
            _profile(),
            _profile(profile_id="other", server="AtlasMarkets-Live"),
        ),
        (
            _profile(),
            _profile(profile_id="other", server="atlasmarkets-live"),
        ),
        (
            _profile(),
            _profile(
                profile_id="other",
                server="Other-Live",
                aliases=["AtlasMarkets-Live"],
            ),
        ),
        (
            _profile(aliases=["Shared Alias"]),
            _profile(
                profile_id="other",
                server="Other-Live",
                aliases=["shared alias"],
            ),
        ),
        (
            _profile(),
            _profile(profile_id="atlas-live", server="Other-Live", aliases=[]),
        ),
    ],
    ids=[
        "exact-server",
        "casefold-server",
        "alias-vs-server",
        "casefold-alias",
        "profile-id",
    ],
)
def test_ambiguous_server_or_alias_collisions_are_rejected(
    profiles: tuple[dict[str, object], dict[str, object]],
) -> None:
    with pytest.raises(ValueError):
        BrokerRegistry.from_dict(_payload(*profiles))


def test_resolution_is_deterministic_independent_of_profile_order() -> None:
    live = _profile()
    demo = _discovery_profile()
    forward = BrokerRegistry.from_dict(_payload(live, demo))
    reverse = BrokerRegistry.from_dict(_payload(demo, live))

    assert forward.resolve("AtlasMarkets-Live") == reverse.resolve("AtlasMarkets-Live")
    assert forward.resolve("Atlas MT5 Demo") == reverse.resolve("Atlas MT5 Demo")
    assert forward.resolve("missing") == reverse.resolve("missing")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda raw: raw.update(extra=True),
        lambda raw: raw["profiles"][0].update(extra=True),
        lambda raw: raw.pop("schema_version"),
        lambda raw: raw.pop("revision"),
        lambda raw: raw.pop("profiles"),
        lambda raw: raw.update(profiles=[]),
        lambda raw: raw["profiles"][0].pop("profile_id"),
        lambda raw: raw["profiles"][0].pop("source"),
        lambda raw: raw.update(profiles="not-a-list"),
        lambda raw: raw.update(revision=0),
        lambda raw: raw.update(revision=True),
        lambda raw: raw["profiles"][0].update(aliases="not-a-list"),
        lambda raw: raw["profiles"][0].update(discovery_queries=[]),
        lambda raw: raw["profiles"][0].update(environment="production"),
        lambda raw: raw["profiles"][0].update(broker_name=""),
        lambda raw: raw["profiles"][0].update(source=""),
    ],
    ids=[
        "unknown-top-level-field",
        "unknown-profile-field",
        "missing-schema-version",
        "missing-revision",
        "missing-profiles",
        "empty-profiles",
        "missing-profile-id",
        "missing-source",
        "profiles-not-list",
        "zero-revision",
        "boolean-revision",
        "aliases-not-list",
        "empty-discovery-queries",
        "unknown-environment",
        "empty-broker-name",
        "empty-source",
    ],
)
def test_schema_is_strict(mutate) -> None:
    raw = _payload()
    mutate(raw)

    with pytest.raises((TypeError, ValueError)):
        BrokerRegistry.from_dict(raw)


@pytest.mark.parametrize("version", [0, 2, "1", 1.0, True, None])
def test_only_integer_schema_version_one_is_accepted(version: object) -> None:
    raw = _payload()
    raw["schema_version"] = version

    with pytest.raises((TypeError, ValueError)):
        BrokerRegistry.from_dict(raw)


@pytest.mark.parametrize("revision", [0, -1, "1", 1.0, True, None])
def test_revision_must_be_a_positive_integer(revision: object) -> None:
    raw = _payload()
    raw["revision"] = revision

    with pytest.raises((TypeError, ValueError)):
        BrokerRegistry.from_dict(raw)


@pytest.mark.parametrize(
    "endpoint",
    [
        "mt5.atlas.example",
        "mt5.atlas.example:0",
        "mt5.atlas.example:65536",
        "mt5.atlas.example:not-a-port",
        "https://mt5.atlas.example:443",
        "user@mt5.atlas.example:443",
        "mt5.atlas.example:443/path",
        "mt5.atlas.example:443?query=1",
        "mt5.atlas.example:443#fragment",
        "../mt5.atlas.example:443",
        "mt5.atlas.example:443\nsecond.example:443",
        "2001:db8::1:443",
        "[2001:db8::1]",
        "[2001:db8::1]:65536",
        "",
    ],
)
def test_invalid_connection_targets_are_rejected(endpoint: str) -> None:
    with pytest.raises(ValueError):
        BrokerRegistry.from_dict(_payload(_profile(connection_target=endpoint)))


@pytest.mark.parametrize(
    "endpoint",
    [
        "mt5.atlas.example:443",
        "192.0.2.10:1950",
        "[2001:db8::10]:443",
    ],
)
def test_valid_connection_targets_are_accepted_without_dns_lookup(endpoint: str) -> None:
    with patch.object(socket, "getaddrinfo", side_effect=AssertionError("network access")):
        resolution = BrokerRegistry.from_dict(
            _payload(_profile(connection_target=endpoint))
        ).resolve("AtlasMarkets-Live")

    assert resolution.connection_target == endpoint


@pytest.mark.parametrize(
    "verified_at",
    [
        "2026-07-22",
        "2026-07-22T10:00:00",
        "2026-07-22T10:00:00+02:00",
        "2026-13-22T10:00:00Z",
        "not-a-timestamp",
        "",
    ],
)
def test_endpoint_verification_timestamp_must_be_valid_utc_rfc3339(
    verified_at: str,
) -> None:
    with pytest.raises(ValueError):
        BrokerRegistry.from_dict(
            _payload(_profile(target_verified_at=verified_at))
        )


def test_endpoint_and_verification_timestamp_must_appear_together() -> None:
    endpoint_without_timestamp = _profile(target_verified_at=None)
    timestamp_without_endpoint = _discovery_profile(
        target_verified_at="2026-07-22T10:00:00Z"
    )

    with pytest.raises(ValueError):
        BrokerRegistry.from_dict(_payload(endpoint_without_timestamp))
    with pytest.raises(ValueError):
        BrokerRegistry.from_dict(_payload(timestamp_without_endpoint))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("profile_id", "../atlas-live"),
        ("profile_id", "atlas/live"),
        ("broker_id", "..\\atlas"),
        ("server", "../AtlasMarkets-Live"),
        ("server", "AtlasMarkets-Live\\config"),
        ("server", "AtlasMarkets-Live\nOther-Live"),
        ("broker_name", "Atlas Markets\x00Ltd"),
        ("source", "unit-test\rforged"),
    ],
)
def test_control_characters_and_path_traversal_are_rejected(
    field: str, value: str
) -> None:
    profile = _profile()
    profile[field] = value

    with pytest.raises(ValueError):
        BrokerRegistry.from_dict(_payload(profile))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("aliases", ["Alias\nInjected"]),
        ("aliases", ["..\\Alias"]),
        ("discovery_queries", ["Atlas Markets\rOther Broker"]),
        ("discovery_queries", ["../Atlas Markets"]),
    ],
)
def test_nested_text_fields_reject_controls_and_traversal(
    field: str, value: list[str]
) -> None:
    profile = _profile()
    profile[field] = value

    with pytest.raises(ValueError):
        BrokerRegistry.from_dict(_payload(profile))


@pytest.mark.parametrize(
    "sensitive_key", ["password", "login", "account", "credentials", "secret"]
)
def test_schema_rejects_credential_or_account_material(sensitive_key: str) -> None:
    raw = _payload()
    raw["profiles"][0][sensitive_key] = "must-not-be-stored"  # type: ignore[index]

    with pytest.raises(ValueError):
        BrokerRegistry.from_dict(raw)


def test_serialized_resolution_and_default_registry_contain_no_credentials() -> None:
    registry = BrokerRegistry.default()
    resolution = registry.resolve("FPMTrading-Live")

    serialized = json.dumps(
        {"registry": registry.to_dict(), "resolution": resolution.to_dict()},
        sort_keys=True,
    ).casefold()
    for forbidden in ("password", "credential", "secret", "account_login"):
        assert forbidden not in serialized


def test_from_path_applies_the_same_schema_validation(tmp_path: Path) -> None:
    valid_path = tmp_path / "brokers.json"
    valid_path.write_text(json.dumps(_payload()), encoding="utf-8")
    assert BrokerRegistry.from_path(valid_path).resolve(
        "AtlasMarkets-Live"
    ).method is ResolutionMethod.DIRECT_ENDPOINT

    invalid_path = tmp_path / "invalid.json"
    invalid_path.write_text('{"schema_version": 1}', encoding="utf-8")
    with pytest.raises(ValueError):
        BrokerRegistry.from_path(invalid_path)


def test_from_dict_and_resolve_are_pure_and_never_spawn_or_connect() -> None:
    with (
        patch.object(socket, "getaddrinfo", side_effect=AssertionError("DNS access")),
        patch.object(
            socket, "create_connection", side_effect=AssertionError("network access")
        ),
        patch.object(subprocess, "run", side_effect=AssertionError("subprocess access")),
        patch.object(subprocess, "Popen", side_effect=AssertionError("subprocess access")),
    ):
        registry = BrokerRegistry.from_dict(_payload())
        assert (
            registry.resolve("AtlasMarkets-Live").method
            is ResolutionMethod.DIRECT_ENDPOINT
        )
        assert registry.resolve("missing").method is ResolutionMethod.UNRESOLVED


def test_default_registry_contains_fpm_live_as_discovery_only_profile() -> None:
    resolution = BrokerRegistry.default().resolve("FPMTrading-Live")

    assert resolution.method is ResolutionMethod.TERMINAL_DISCOVERY
    assert resolution.expected_server == "FPMTrading-Live"
    assert resolution.connection_target is None
    assert resolution.environment == "live"
    assert resolution.broker_id == "fpm-trading"
    assert any("FPM" in query for query in resolution.discovery_queries)
