from __future__ import annotations

"""Pure, zero-license broker planning for provisioning.

This module deliberately performs no network access, browser automation, MetaTrader
startup, credential handling, or persistence.  It converts validated, non-secret
identity input into a fail-closed plan that a later discovery executor can consume.
"""

import ipaddress
import re
import unicodedata
from typing import Protocol

from .broker_registry import (
    BrokerRegistry,
    BrokerResolution,
    ResolutionMethod,
)


class BrokerPlanResolver(Protocol):
    def resolve(
        self, expected_server: str, *, broker_hint: object = None
    ) -> BrokerResolution:
        """Return one fully validated plan without inspecting credentials."""


class ZeroLicenseBrokerResolver:
    """Resolve known profiles locally and route valid unknown servers to MT5.

    Unknown servers are never promoted to a direct network endpoint.  The exact
    server supplied by the customer is retained as both the canonical identity and
    the mandatory first discovery query.  An optional broker/company hint can add a
    second query, but cannot replace that exact identity.
    """

    def __init__(self, registry: BrokerRegistry | None = None) -> None:
        self._registry = registry if registry is not None else BrokerRegistry.default()

    def resolve(
        self, expected_server: str, *, broker_hint: object = None
    ) -> BrokerResolution:
        local = self._registry.resolve(expected_server)
        hint = _validated_optional_hint(broker_hint)

        if local.method is not ResolutionMethod.UNRESOLVED:
            _reject_conflicting_known_hint(local, hint)
            return local

        reject_network_target(expected_server, field="expected_server")

        queries = [expected_server]
        if hint is not None and hint.casefold() != expected_server.casefold():
            queries.append(hint)

        return BrokerResolution(
            requested_server=expected_server,
            method=ResolutionMethod.TERMINAL_DISCOVERY,
            expected_server=expected_server,
            connection_target=None,
            discovery_queries=tuple(queries),
            profile_id=None,
            broker_id=None,
            broker_name=None,
            environment=None,
            revision=self._registry.revision,
            matched_by="generic_exact_server",
        )


def validate_resolution_plan(
    plan: object, *, requested_server: str
) -> BrokerResolution:
    """Validate an injected resolver's output before credentials are decrypted."""

    if not isinstance(plan, BrokerResolution):
        raise ValueError("broker resolver returned an invalid plan")
    if plan.method is ResolutionMethod.UNRESOLVED:
        raise ValueError("broker resolver returned an unusable plan")
    if plan.requested_server != requested_server:
        raise ValueError("broker resolver changed the requested server")
    if not plan.expected_server or not plan.discovery_queries:
        raise ValueError("broker resolver returned an incomplete plan")

    # Identity changes and direct network targets are authority-bearing decisions,
    # not merely strings to syntax-check.  They are accepted only when the shipped,
    # validated registry independently produces the exact same plan.  A generic or
    # dependency-injected resolver may still request terminal discovery for the
    # customer's exact server, but cannot self-certify an arbitrary alias/endpoint.
    try:
        trusted_plan = BrokerRegistry.default().resolve(requested_server)
    except (OSError, TypeError, ValueError) as exc:
        raise ValueError("trusted broker registry is unavailable") from exc
    changes_identity = (
        plan.expected_server.casefold() != requested_server.casefold()
    )
    if plan.method is ResolutionMethod.DIRECT_ENDPOINT or changes_identity:
        if plan != trusted_plan:
            raise ValueError("broker resolver returned an untrusted routing plan")
    if plan.method is ResolutionMethod.TERMINAL_DISCOVERY:
        reject_network_target(plan.expected_server, field="expected_server")
        for query in plan.discovery_queries:
            reject_network_target(query, field="discovery_query")

    # Reuse the registry's strict server and endpoint validators without depending
    # on private helpers.  A tiny synthetic registry also detects malformed direct
    # targets and keeps this boundary fail-closed for dependency-injected resolvers.
    validation_registry = BrokerRegistry.from_dict(
        {
            "schema_version": 1,
            "revision": 1,
            "profiles": [
                {
                    "profile_id": "injected-plan",
                    "broker_id": "injected-plan",
                    "broker_name": "Injected broker plan",
                    "server": plan.expected_server,
                    "environment": "live",
                    "aliases": [],
                    "discovery_queries": list(plan.discovery_queries),
                    "source": "dependency-injected-resolver",
                    **(
                        {
                            "connection_target": plan.connection_target,
                            "target_verified_at": "2000-01-01T00:00:00Z",
                        }
                        if plan.connection_target is not None
                        else {}
                    ),
                }
            ],
        }
    )
    validated = validation_registry.resolve(plan.expected_server)

    if plan.method is ResolutionMethod.DIRECT_ENDPOINT:
        if plan.connection_target is None:
            raise ValueError("direct broker plan has no connection target")
    elif plan.method is ResolutionMethod.TERMINAL_DISCOVERY:
        if plan.connection_target is not None:
            raise ValueError("terminal discovery plan cannot carry a direct target")
    else:
        raise ValueError("broker resolver returned an unsupported method")
    if validated.connection_target != plan.connection_target:
        raise ValueError("broker resolver returned an invalid connection target")
    return plan


def _validated_optional_hint(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not 1 <= len(value) <= 128:
        raise ValueError("broker_hint is invalid")
    if value != value.strip() or "/" in value or "\\" in value:
        raise ValueError("broker_hint is invalid")
    if any(
        unicodedata.category(character).startswith("C") for character in value
    ):
        raise ValueError("broker_hint is invalid")
    reject_network_target(value, field="broker_hint")
    return value


_DNS_NAME = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+\.?\Z"
)


def reject_network_target(value: str, *, field: str) -> None:
    """Reject browser-supplied addresses before MT5 can interpret them as probes."""

    folded = value.casefold().rstrip(".")
    if (
        ":" in value
        or "[" in value
        or "]" in value
        or folded == "localhost"
        or folded.endswith((".localhost", ".local", ".internal"))
        or re.fullmatch(r"[0-9.]+", value) is not None
        or re.fullmatch(r"0x[0-9a-f]+", folded) is not None
        or (
            not any(character.isspace() for character in value)
            and _DNS_NAME.fullmatch(value)
        )
    ):
        raise ValueError(f"{field} cannot be a network target")
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return
    raise ValueError(f"{field} cannot be a network target")


def _reject_conflicting_known_hint(
    plan: BrokerResolution, hint: str | None
) -> None:
    if hint is None:
        return
    accepted = {
        plan.expected_server.casefold() if plan.expected_server else "",
        plan.broker_name.casefold() if plan.broker_name else "",
        *(query.casefold() for query in plan.discovery_queries),
    }
    if hint.casefold() not in accepted:
        raise ValueError("broker_hint conflicts with the known broker profile")
