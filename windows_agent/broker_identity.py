"""Suggestion-only broker identity resolution for previously unseen MT5 servers.

The model receives only the server identifier. Its answer cannot verify an endpoint or promote
anything: ``real_handlers`` must still resolve a separately VERIFIED endpoint and complete an
investor login before the control plane accepts the broker/server pair.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import stat
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import urlsplit, urlunsplit

from .state_store import atomic_json

DEFAULT_OPENAI_MODEL = "gpt-5.6"
_CACHE_SCHEMA_VERSION = 1
_SERVER_PATTERN = re.compile(r"[A-Za-z0-9._ -]{1,128}")
_BROKER_PATTERN = re.compile(r"[A-Za-z0-9&'()._ /+-]{1,128}")
_USABLE_CONFIDENCE = frozenset(("HIGH", "MEDIUM"))


class BrokerIdentityError(ValueError):
    """The identity suggestion is unavailable or not trustworthy enough to use."""


@dataclass(frozen=True)
class BrokerIdentitySuggestion:
    broker_label: str
    search_text: str
    confidence: str
    source_urls: tuple[str, ...]
    generated_at_unix_ms: int


class BrokerIdentityProvider(Protocol):
    def resolve(self, server_identifier: str) -> BrokerIdentitySuggestion: ...


class FakeBrokerIdentityProvider:
    def __init__(self, suggestion: BrokerIdentitySuggestion) -> None:
        self.suggestion = suggestion
        self.calls: list[str] = []

    def resolve(self, server_identifier: str) -> BrokerIdentitySuggestion:
        self.calls.append(server_identifier)
        return self.suggestion


def _server(value: object) -> str:
    if not isinstance(value, str):
        raise BrokerIdentityError("server identifier is invalid")
    normalized = value.strip()
    if not _SERVER_PATTERN.fullmatch(normalized):
        raise BrokerIdentityError("server identifier is invalid")
    return normalized


def _label(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise BrokerIdentityError(f"{name} is invalid")
    normalized = value.strip()
    if not _BROKER_PATTERN.fullmatch(normalized):
        raise BrokerIdentityError(f"{name} is invalid")
    return normalized


def _source_url(value: object) -> str:
    if not isinstance(value, str) or len(value) > 2048:
        raise BrokerIdentityError("source URL is invalid")
    parsed = urlsplit(value.strip())
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise BrokerIdentityError("source URL is invalid")
    host = parsed.hostname.rstrip(".").lower()
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if host == "localhost" or host.endswith(".localhost") or "." not in host:
            raise BrokerIdentityError("source URL is invalid")
    else:
        if not address.is_global:
            raise BrokerIdentityError("source URL is invalid")
    return urlunsplit(("https", parsed.netloc.lower(), parsed.path or "/", "", ""))


def _validated_suggestion(
    value: BrokerIdentitySuggestion,
    *,
    now_unix_ms: int | None = None,
) -> BrokerIdentitySuggestion:
    if not isinstance(value, BrokerIdentitySuggestion):
        raise BrokerIdentityError("provider response is invalid")
    confidence = value.confidence.strip().upper()
    if confidence not in _USABLE_CONFIDENCE:
        raise BrokerIdentityError("broker identity confidence is insufficient")
    generated = value.generated_at_unix_ms if now_unix_ms is None else now_unix_ms
    if not isinstance(generated, int) or isinstance(generated, bool) or generated < 0:
        raise BrokerIdentityError("broker identity timestamp is invalid")
    sources = tuple(dict.fromkeys(_source_url(url) for url in value.source_urls))
    if not sources:
        raise BrokerIdentityError("broker identity provenance is missing")
    return BrokerIdentitySuggestion(
        broker_label=_label(value.broker_label, "broker label"),
        search_text=_label(value.search_text, "search text"),
        confidence=confidence,
        source_urls=sources,
        generated_at_unix_ms=generated,
    )


def _is_reparse_point(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return path.is_symlink() or bool(attributes & reparse_flag)


class CachedBrokerIdentityResolver:
    """Atomic local cache that prevents a second model call within the configured TTL."""

    def __init__(
        self,
        path: Path,
        provider: BrokerIdentityProvider,
        *,
        ttl_seconds: int,
        now_unix_ms: Callable[[], int] | None = None,
    ) -> None:
        if (
            not isinstance(ttl_seconds, int)
            or isinstance(ttl_seconds, bool)
            or not 60 <= ttl_seconds <= 30 * 24 * 60 * 60
        ):
            raise BrokerIdentityError("broker identity cache TTL is invalid")
        self._path = path
        self._provider = provider
        self._ttl_ms = ttl_seconds * 1000
        self._now = now_unix_ms or (lambda: int(time.time() * 1000))

    def _read(self) -> dict[str, dict[str, object]]:
        if not self._path.exists():
            return {}
        if _is_reparse_point(self._path) or not self._path.is_file():
            raise BrokerIdentityError("broker identity cache is unsafe")
        try:
            if self._path.stat().st_size <= 0 or self._path.stat().st_size > 1024 * 1024:
                raise BrokerIdentityError("broker identity cache size is invalid")
            document = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BrokerIdentityError("broker identity cache cannot be read") from exc
        if (
            not isinstance(document, dict)
            or set(document) != {"schema_version", "entries"}
            or document["schema_version"] != _CACHE_SCHEMA_VERSION
            or not isinstance(document["entries"], dict)
        ):
            raise BrokerIdentityError("broker identity cache fields are invalid")
        entries: dict[str, dict[str, object]] = {}
        for key, raw in document["entries"].items():
            if not isinstance(key, str) or not isinstance(raw, dict):
                raise BrokerIdentityError("broker identity cache entry is invalid")
            expected = {
                "server_identifier",
                "broker_label",
                "search_text",
                "confidence",
                "source_urls",
                "generated_at_unix_ms",
            }
            if set(raw) != expected:
                raise BrokerIdentityError("broker identity cache entry is invalid")
            suggestion = _validated_suggestion(
                BrokerIdentitySuggestion(
                    broker_label=raw["broker_label"],
                    search_text=raw["search_text"],
                    confidence=raw["confidence"],
                    source_urls=tuple(raw["source_urls"])
                    if isinstance(raw["source_urls"], list)
                    else (),
                    generated_at_unix_ms=raw["generated_at_unix_ms"],
                )
            )
            server_identifier = _server(raw["server_identifier"])
            if server_identifier.casefold() != key:
                raise BrokerIdentityError("broker identity cache key is invalid")
            entries[key] = {
                "server_identifier": server_identifier,
                **asdict(suggestion),
                "source_urls": list(suggestion.source_urls),
            }
        return entries

    def resolve(self, server_identifier: str) -> BrokerIdentitySuggestion:
        server = _server(server_identifier)
        now = self._now()
        if not isinstance(now, int) or isinstance(now, bool) or now < 0:
            raise BrokerIdentityError("current timestamp is invalid")
        entries = self._read()
        raw = entries.get(server.casefold())
        if raw is not None:
            cached = _validated_suggestion(
                BrokerIdentitySuggestion(
                    broker_label=raw["broker_label"],  # type: ignore[arg-type]
                    search_text=raw["search_text"],  # type: ignore[arg-type]
                    confidence=raw["confidence"],  # type: ignore[arg-type]
                    source_urls=tuple(raw["source_urls"]),  # type: ignore[arg-type]
                    generated_at_unix_ms=raw["generated_at_unix_ms"],  # type: ignore[arg-type]
                )
            )
            if cached.generated_at_unix_ms + self._ttl_ms >= now:
                return cached

        resolved = _validated_suggestion(
            self._provider.resolve(server),
            now_unix_ms=now,
        )
        entries[server.casefold()] = {
            "server_identifier": server,
            **asdict(resolved),
            "source_urls": list(resolved.source_urls),
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(
            self._path,
            {"schema_version": _CACHE_SCHEMA_VERSION, "entries": entries},
        )
        return resolved


def _observed_urls(value: Any, *, inside_sources: bool = False) -> set[str]:
    urls: set[str] = set()
    if isinstance(value, Mapping):
        for key, child in value.items():
            scoped = inside_sources or key in {"sources", "annotations"}
            if scoped and key == "url" and isinstance(child, str):
                urls.add(child)
            else:
                urls.update(_observed_urls(child, inside_sources=scoped))
    elif isinstance(value, (list, tuple)):
        for child in value:
            urls.update(_observed_urls(child, inside_sources=inside_sources))
    return urls


class OpenAIBrokerIdentityProvider:
    """Responses API adapter; the SDK reads ``OPENAI_API_KEY`` only from the environment."""

    def __init__(self, *, model: str = DEFAULT_OPENAI_MODEL) -> None:
        if not os.environ.get("OPENAI_API_KEY"):
            raise BrokerIdentityError("OPENAI_API_KEY is not configured")
        try:
            from openai import OpenAI  # type: ignore[import-not-found]
        except ImportError as exc:
            raise BrokerIdentityError("OpenAI SDK is not installed") from exc
        self._client = OpenAI()
        self._model = model

    def resolve(self, server_identifier: str) -> BrokerIdentitySuggestion:
        server = _server(server_identifier)
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
                        "Identify the exact broker or prop-firm brand for the supplied MetaTrader "
                        "server identifier. Search public sources and prefer official evidence. "
                        "Do not infer from a similar name. Return HIGH or MEDIUM confidence only "
                        "when the exact server is supported by the cited sources."
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
                            "confidence",
                            "source_urls",
                            "ambiguous",
                        ],
                    },
                }
            },
        )
        try:
            payload = json.loads(response.output_text)
            dumped = response.model_dump()
        except (AttributeError, TypeError, json.JSONDecodeError) as exc:
            raise BrokerIdentityError("OpenAI broker identity response is invalid") from exc
        if not isinstance(payload, dict) or payload.get("ambiguous") is not False:
            raise BrokerIdentityError("broker identity is ambiguous")
        claimed = tuple(_source_url(url) for url in payload.get("source_urls", ()))
        observed = {_source_url(url) for url in _observed_urls(dumped)}
        if not claimed or any(url not in observed for url in claimed):
            raise BrokerIdentityError("broker identity provenance is unverified")
        return _validated_suggestion(
            BrokerIdentitySuggestion(
                broker_label=_label(payload.get("broker_label"), "broker label"),
                search_text=_label(payload.get("search_text"), "search text"),
                confidence=str(payload.get("confidence") or ""),
                source_urls=claimed,
                generated_at_unix_ms=int(time.time() * 1000),
            )
        )
