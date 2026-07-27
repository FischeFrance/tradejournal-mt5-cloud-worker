"""Resolve an MT4/MT5 server identifier to a broker-name suggestion.

The OpenAI-backed provider may search the public web, but its output is never
treated as authoritative.  A successful result is only a bounded input for the
Windows broker wizard.  MT5 must still confirm that the exact original server
belongs to exactly one broker result before the broker can be selected.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Mapping, Protocol
from urllib.parse import urlsplit, urlunsplit


IDENTITY_RESOLUTION_SCHEMA_VERSION = 1
DEFAULT_OPENAI_MODEL = "gpt-5.6"
_ALLOWED_CONFIDENCE = frozenset({"HIGH", "MEDIUM", "LOW", "INCONCLUSIVE"})
_USABLE_CONFIDENCE = frozenset({"HIGH", "MEDIUM"})
_SERVER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._ -]{0,158}[A-Za-z0-9]|[A-Za-z0-9]")
_LABEL_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9 .,&'()+_\-/]{0,119}")


class BrokerIdentityError(ValueError):
    """Raised when an input or provider response cannot be handled safely."""


@dataclass(frozen=True)
class ProviderIdentityResult:
    broker_label: str | None
    search_text: str | None
    aliases: tuple[str, ...]
    confidence: str
    source_urls: tuple[str, ...]
    observed_source_urls: tuple[str, ...]
    ambiguous: bool = False


class ServerBrokerProvider(Protocol):
    def resolve(self, server_identifier: str) -> ProviderIdentityResult: ...


class FakeServerBrokerProvider:
    """Test-only provider with no network or environment dependencies."""

    def __init__(self, result: ProviderIdentityResult) -> None:
        self.result = result
        self.calls: list[str] = []

    def resolve(self, server_identifier: str) -> ProviderIdentityResult:
        self.calls.append(server_identifier)
        return self.result


def _normalize_server_identifier(value: str) -> str:
    if not isinstance(value, str):
        raise BrokerIdentityError("server identifier is invalid")
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > 160
        or any(ord(character) < 32 or ord(character) == 127 for character in normalized)
    ):
        raise BrokerIdentityError("server identifier is invalid")

    server_name = normalized
    if ":" in normalized:
        if normalized.count(":") != 1:
            raise BrokerIdentityError("server identifier is invalid")
        server_name, port_text = normalized.rsplit(":", 1)
        if not port_text.isascii() or not port_text.isdigit():
            raise BrokerIdentityError("server port is invalid")
        port = int(port_text)
        if not 1 <= port <= 65535:
            raise BrokerIdentityError("server port is invalid")
    if not _SERVER_PATTERN.fullmatch(server_name):
        raise BrokerIdentityError("server identifier is invalid")
    return normalized


def _normalize_label(value: str | None, field_name: str) -> str:
    if not isinstance(value, str):
        raise BrokerIdentityError(f"{field_name} is invalid")
    normalized = value.strip()
    if not _LABEL_PATTERN.fullmatch(normalized):
        raise BrokerIdentityError(f"{field_name} is invalid")
    return normalized


def _normalize_source_url(value: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) > 2048
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise BrokerIdentityError("source URL is invalid")
    parsed = urlsplit(value.strip())
    if parsed.scheme.lower() != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise BrokerIdentityError("source URL is invalid")
    hostname = parsed.hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith(".localhost") or "." not in hostname:
        raise BrokerIdentityError("source URL is invalid")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        if not address.is_global:
            raise BrokerIdentityError("source URL is invalid")
    # Search providers may return tracking query parameters. They are neither
    # needed for provenance comparison nor safe to persist in public output.
    return urlunsplit(("https", parsed.netloc.lower(), parsed.path or "/", "", ""))


def _ordered_unique(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _inconclusive(
    server_identifier: str,
    now_unix_ms: int,
    *,
    confidence: str,
    reasons: tuple[str, ...],
    sources: tuple[str, ...] = (),
) -> dict[str, object]:
    return {
        "schema_version": IDENTITY_RESOLUTION_SCHEMA_VERSION,
        "server_identifier": server_identifier,
        "generated_at_unix_ms": now_unix_ms,
        "outcome": "INCONCLUSIVE",
        "status": "SUGGESTION_ONLY",
        "promotion_allowed": False,
        "broker_label": None,
        "search_text": None,
        "aliases": [],
        "confidence": confidence,
        "source_urls": list(sources),
        "reasons": list(reasons),
        "wizard_request": None,
    }


def resolve_broker_for_wizard(
    server_identifier: str,
    provider: ServerBrokerProvider,
    *,
    now_unix_ms: int | None = None,
) -> dict[str, object]:
    """Produce a suggestion-only broker-wizard request.

    The provider sees only the normalized server identifier.  Its claimed
    source URLs must be a subset of URLs independently exposed by the hosted
    search response.  This prevents a model-authored URL from becoming
    provenance merely because it appeared in model text.
    """
    server = _normalize_server_identifier(server_identifier)
    now = int(time.time() * 1000) if now_unix_ms is None else now_unix_ms
    if not isinstance(now, int) or now < 0:
        raise BrokerIdentityError("generated timestamp is invalid")

    result = provider.resolve(server)
    if not isinstance(result, ProviderIdentityResult):
        raise BrokerIdentityError("provider response is invalid")
    confidence = result.confidence.strip().upper() if isinstance(result.confidence, str) else ""
    if confidence not in _ALLOWED_CONFIDENCE:
        raise BrokerIdentityError("provider confidence is invalid")

    try:
        sources = _ordered_unique(tuple(_normalize_source_url(url) for url in result.source_urls))
        observed = frozenset(_normalize_source_url(url) for url in result.observed_source_urls)
    except BrokerIdentityError:
        return _inconclusive(
            server,
            now,
            confidence=confidence,
            reasons=("SOURCE_PROVENANCE_INVALID",),
        )

    if result.ambiguous:
        return _inconclusive(
            server,
            now,
            confidence=confidence,
            reasons=("BROKER_IDENTITY_AMBIGUOUS",),
            sources=sources,
        )
    if not sources or not observed or any(url not in observed for url in sources):
        return _inconclusive(
            server,
            now,
            confidence=confidence,
            reasons=("SOURCE_PROVENANCE_UNVERIFIED",),
            sources=sources,
        )
    if confidence not in _USABLE_CONFIDENCE:
        return _inconclusive(
            server,
            now,
            confidence=confidence,
            reasons=("CONFIDENCE_TOO_LOW",),
            sources=sources,
        )

    try:
        broker_label = _normalize_label(result.broker_label, "broker label")
        search_text = _normalize_label(result.search_text, "search text")
        aliases = _ordered_unique(
            tuple(_normalize_label(alias, "broker alias") for alias in result.aliases)
        )
    except BrokerIdentityError:
        return _inconclusive(
            server,
            now,
            confidence=confidence,
            reasons=("BROKER_IDENTITY_INVALID",),
            sources=sources,
        )

    return {
        "schema_version": IDENTITY_RESOLUTION_SCHEMA_VERSION,
        "server_identifier": server,
        "generated_at_unix_ms": now,
        "outcome": "SUGGESTION",
        "status": "SUGGESTION_ONLY",
        "promotion_allowed": False,
        "broker_label": broker_label,
        "search_text": search_text,
        "aliases": list(aliases),
        "confidence": confidence,
        "source_urls": list(sources),
        "reasons": [],
        "wizard_request": {
            "SearchText": search_text,
            "SuggestedBrokerLabel": broker_label,
            "ExpectedServerName": server,
        },
    }


def _collect_observed_source_urls(value: Any, *, in_source_container: bool = False) -> set[str]:
    urls: set[str] = set()
    if isinstance(value, Mapping):
        for key, child in value.items():
            scoped = in_source_container or key in {"sources", "annotations"}
            if scoped and key == "url" and isinstance(child, str):
                urls.add(child)
            else:
                urls.update(_collect_observed_source_urls(child, in_source_container=scoped))
    elif isinstance(value, (list, tuple)):
        for child in value:
            urls.update(_collect_observed_source_urls(child, in_source_container=in_source_container))
    return urls


class OpenAIServerBrokerProvider:
    """Optional OpenAI Responses API adapter.

    The API key is read by the SDK exclusively from ``OPENAI_API_KEY``.  Only
    the server identifier is sent to the model; accounts and credentials are
    not accepted by this interface.
    """

    def __init__(self, *, model: str = DEFAULT_OPENAI_MODEL) -> None:
        if not os.environ.get("OPENAI_API_KEY"):
            raise BrokerIdentityError("OPENAI_API_KEY is not configured")
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise BrokerIdentityError("OpenAI SDK is not installed") from exc
        self._client = OpenAI()
        self._model = model

    def resolve(self, server_identifier: str) -> ProviderIdentityResult:
        server = _normalize_server_identifier(server_identifier)
        response = self._client.responses.create(
            model=self._model,
            reasoning={"effort": "low"},
            tools=[{"type": "web_search", "search_context_size": "high"}],
            tool_choice="auto",
            include=["web_search_call.action.sources"],
            input=[
                {
                    "role": "system",
                    "content": (
                        "Identify the broker or prop-firm brand associated with the exact "
                        "MetaTrader server identifier supplied by the user. Search public "
                        "sources. Prefer official broker documentation, then MetaTrader "
                        "catalogs and independent references. Return a broker only when the "
                        "exact server identifier or an exact host component is supported by "
                        "the cited sources. Do not infer from a similar name. The broker_label "
                        "must be the broker name expected in MetaTrader's Find your broker "
                        "results; search_text is the safest text to type into that wizard. "
                        "Set ambiguous=true if more than one broker remains plausible."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {"mt_server_identifier": server},
                        ensure_ascii=True,
                        separators=(",", ":"),
                    ),
                },
            ],
            text={
                "format": {
                    "type": "json_schema",
                    "name": "mt_server_broker_identity",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "broker_label": {"type": ["string", "null"]},
                            "search_text": {"type": ["string", "null"]},
                            "aliases": {
                                "type": "array",
                                "items": {"type": "string"},
                                "maxItems": 5,
                            },
                            "confidence": {
                                "type": "string",
                                "enum": ["HIGH", "MEDIUM", "LOW", "INCONCLUSIVE"],
                            },
                            "source_urls": {
                                "type": "array",
                                "items": {"type": "string"},
                                "maxItems": 8,
                            },
                            "ambiguous": {"type": "boolean"},
                        },
                        "required": [
                            "broker_label",
                            "search_text",
                            "aliases",
                            "confidence",
                            "source_urls",
                            "ambiguous",
                        ],
                    },
                },
            },
        )
        try:
            payload = json.loads(response.output_text)
        except (TypeError, json.JSONDecodeError) as exc:
            raise BrokerIdentityError("OpenAI response is not valid structured JSON") from exc
        if not isinstance(payload, dict):
            raise BrokerIdentityError("OpenAI response is not an object")

        response_payload = response.model_dump() if hasattr(response, "model_dump") else {}
        observed_urls = _collect_observed_source_urls(response_payload)
        aliases = payload.get("aliases")
        source_urls = payload.get("source_urls")
        if not isinstance(aliases, list) or not all(isinstance(item, str) for item in aliases):
            raise BrokerIdentityError("OpenAI aliases are invalid")
        if not isinstance(source_urls, list) or not all(isinstance(item, str) for item in source_urls):
            raise BrokerIdentityError("OpenAI source URLs are invalid")
        if not isinstance(payload.get("ambiguous"), bool):
            raise BrokerIdentityError("OpenAI ambiguity flag is invalid")

        return ProviderIdentityResult(
            broker_label=payload.get("broker_label"),
            search_text=payload.get("search_text"),
            aliases=tuple(aliases),
            confidence=payload.get("confidence", ""),
            source_urls=tuple(source_urls),
            observed_source_urls=tuple(sorted(observed_urls)),
            ambiguous=payload["ambiguous"],
        )
