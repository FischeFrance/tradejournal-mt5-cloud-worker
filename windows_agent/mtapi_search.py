"""Untrusted, credential-free MTAPI ``/Search`` client.

This module deliberately supports discovery only.  It never calls MTAPI ``/Connect``
and therefore never receives an account number, password, token, or encrypted
credential envelope.  Its results are candidates used to prioritise MT5's official
broker wizard; they are not endpoint verification or authority to log in directly.
"""
from __future__ import annotations

import ipaddress
import json
import re
import time
from dataclasses import dataclass
from typing import Callable, Literal

import httpx

MTAPI_SEARCH_URL = "https://mt5.mtapi.io/Search"
_SERVER_PATTERN = re.compile(r"[A-Za-z0-9._ -]{1,128}")
_HOSTNAME_PATTERN = re.compile(
    r"(?=.{1,253}\Z)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}\Z"
)
_MAX_RESPONSE_BYTES = 256 * 1024
_RETRYABLE_STATUS_CODES = frozenset((408, 429, 502, 503, 504))


class MtApiSearchError(ValueError):
    """Base class for safe, credential-free MTAPI Search failures."""


class MtApiSearchUnavailable(MtApiSearchError):
    """The public Search service is temporarily unavailable."""


class MtApiSearchContractError(MtApiSearchError):
    """Search returned data that is malformed or unsafe to use."""


@dataclass(frozen=True)
class MtApiEndpointCandidate:
    company_name: str
    server_name: str
    host: str
    port: int
    protocol: str = "TCP/TLS"
    status: str = "CANDIDATE"
    # A single IP:port can legitimately occur under more than one MTAPI
    # company record. Preserve that ambiguity rather than silently retaining
    # whichever row happened to be parsed last.
    company_names: tuple[str, ...] = ()

    @property
    def server_address(self) -> str:
        try:
            address = ipaddress.ip_address(self.host)
        except ValueError:
            return f"{self.host}:{self.port}"
        return f"[{address.compressed}]:{self.port}" if address.version == 6 else f"{address.compressed}:{self.port}"

    @property
    def is_ip_address(self) -> bool:
        try:
            ipaddress.ip_address(self.host)
        except ValueError:
            return False
        return True

    def to_audit_document(self) -> dict[str, object]:
        return {
            "company_name": self.company_name,
            "company_names": list(self.company_names or (self.company_name,)),
            "server_name": self.server_name,
            "host": self.host,
            "port": self.port,
            "protocol": self.protocol,
            "status": self.status,
        }


@dataclass(frozen=True)
class MtApiSearchResult:
    outcome: Literal["EXACT_MATCH", "NO_MATCH", "AMBIGUOUS"]
    candidates: tuple[MtApiEndpointCandidate, ...]
    company_names: tuple[str, ...]
    fetched_at_unix_ms: int

    def to_audit_document(self, expected_server_name: str) -> dict[str, object]:
        return {
            "schema_version": 1,
            "source": "MTAPI_SEARCH",
            "source_url": MTAPI_SEARCH_URL,
            "expected_server_name": _server_identifier(expected_server_name),
            "outcome": self.outcome,
            "company_names": list(self.company_names),
            "candidates": [item.to_audit_document() for item in self.candidates],
            "fetched_at_unix_ms": self.fetched_at_unix_ms,
        }


def _server_identifier(value: object) -> str:
    if not isinstance(value, str):
        raise MtApiSearchContractError("server identifier is invalid")
    normalized = value.strip()
    if not _SERVER_PATTERN.fullmatch(normalized):
        raise MtApiSearchContractError("server identifier is invalid")
    return normalized


def _company_name(value: object) -> str:
    if not isinstance(value, str):
        raise MtApiSearchContractError("MTAPI Search company is invalid")
    normalized = value.strip()
    if not normalized or len(normalized) > 256 or any(ord(character) < 32 for character in normalized):
        raise MtApiSearchContractError("MTAPI Search company is invalid")
    return normalized


def _endpoint(value: object, *, company_name: str, server_name: str) -> MtApiEndpointCandidate:
    if not isinstance(value, str) or len(value) > 128:
        raise MtApiSearchContractError("MTAPI Search endpoint is invalid")
    raw = value.strip()
    host: str
    port_text: str
    if raw.startswith("["):
        closing = raw.find("]")
        if closing <= 1 or raw[closing + 1 : closing + 2] != ":":
            raise MtApiSearchContractError("MTAPI Search endpoint is invalid")
        host, port_text = raw[1:closing], raw[closing + 2 :]
    else:
        if raw.count(":") != 1:
            raise MtApiSearchContractError("MTAPI Search endpoint is invalid")
        host, port_text = raw.rsplit(":", 1)
    try:
        port = int(port_text, 10)
    except ValueError as exc:
        raise MtApiSearchContractError("MTAPI Search endpoint is invalid") from exc
    if not 1 <= port <= 65535:
        raise MtApiSearchContractError("MTAPI Search endpoint is unsafe")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        normalized_host = host.casefold()
        if not _HOSTNAME_PATTERN.fullmatch(normalized_host):
            raise MtApiSearchContractError("MTAPI Search endpoint is invalid")
    else:
        if (
            not address.is_global
            or (
                isinstance(address, ipaddress.IPv6Address)
                and address.ipv4_mapped is not None
            )
        ):
            raise MtApiSearchContractError("MTAPI Search endpoint is unsafe")
        normalized_host = address.compressed
    return MtApiEndpointCandidate(
        company_name=company_name,
        server_name=server_name,
        host=normalized_host,
        port=port,
    )


def _parse_document(payload: object, expected_server_name: str, fetched_at_unix_ms: int) -> MtApiSearchResult:
    if not isinstance(payload, list):
        raise MtApiSearchContractError("MTAPI Search response is invalid")
    exact_rows: list[tuple[str, str, list[MtApiEndpointCandidate]]] = []
    for company in payload:
        if not isinstance(company, dict) or set(company) != {"companyName", "results"}:
            raise MtApiSearchContractError("MTAPI Search response is invalid")
        company_name = _company_name(company["companyName"])
        results = company["results"]
        if not isinstance(results, list):
            raise MtApiSearchContractError("MTAPI Search response is invalid")
        for result in results:
            if not isinstance(result, dict) or set(result) != {"name", "access"}:
                raise MtApiSearchContractError("MTAPI Search response is invalid")
            name = _server_identifier(result["name"])
            access = result["access"]
            if not isinstance(access, list) or not all(isinstance(item, str) for item in access):
                raise MtApiSearchContractError("MTAPI Search response is invalid")
            if name.casefold() == expected_server_name.casefold():
                # MTAPI may legitimately return a mixed list containing a
                # stale private address beside usable public routes.  A bad
                # individual candidate must never make us discard the whole
                # exact-server response (including its authoritative hostname).
                candidates: list[MtApiEndpointCandidate] = []
                for item in access:
                    try:
                        candidates.append(
                            _endpoint(
                                item,
                                company_name=company_name,
                                server_name=name,
                            )
                        )
                    except MtApiSearchContractError:
                        continue
                exact_rows.append((company_name, name, candidates))
    if not exact_rows:
        return MtApiSearchResult("NO_MATCH", (), (), fetched_at_unix_ms)
    company_names = tuple(sorted({row[0] for row in exact_rows}, key=str.casefold))
    by_address: dict[str, list[MtApiEndpointCandidate]] = {}
    for _company, _name, row in exact_rows:
        for item in row:
            by_address.setdefault(item.server_address, []).append(item)
    if not by_address:
        raise MtApiSearchContractError("MTAPI Search exact match has no endpoints")
    candidates: list[MtApiEndpointCandidate] = []
    for address, items in by_address.items():
        companies = tuple(sorted({item.company_name for item in items}, key=str.casefold))
        first = items[0]
        candidates.append(
            MtApiEndpointCandidate(
                company_name=companies[0],
                company_names=companies,
                server_name=first.server_name,
                host=first.host,
                port=first.port,
            )
        )
    def candidate_order(item: MtApiEndpointCandidate) -> tuple[int, object, int]:
        # A broker hostname is the broker's own routing authority.  Try it before
        # point-in-time IP addresses; only fall back to those addresses if MT5
        # cannot authenticate through the hostname.
        if not item.is_ip_address:
            return (0, item.host.casefold(), item.port)
        address = ipaddress.ip_address(item.host)
        return (1, (address.version, address.packed), item.port)

    ordered = tuple(sorted(candidates, key=candidate_order))
    outcome: Literal["EXACT_MATCH", "AMBIGUOUS"] = "EXACT_MATCH" if len(company_names) == 1 else "AMBIGUOUS"
    return MtApiSearchResult(outcome, ordered, company_names, fetched_at_unix_ms)


class MtApiSearchClient:
    def __init__(
        self,
        client: httpx.Client | None = None,
        *,
        now_unix_ms: Callable[[], int] | None = None,
    ) -> None:
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(connect=3.0, read=7.0, write=3.0, pool=3.0),
            follow_redirects=False,
            trust_env=False,
        )
        self._now = now_unix_ms or (lambda: int(time.time() * 1000))

    def search_exact(self, server_identifier: str) -> MtApiSearchResult:
        server = _server_identifier(server_identifier)
        last_unavailable: Exception | None = None
        for attempt in range(2):
            try:
                with self._client.stream("GET", MTAPI_SEARCH_URL, params={"company": server}) as response:
                    if response.is_redirect:
                        raise MtApiSearchContractError("MTAPI Search redirect is forbidden")
                    if response.status_code != 200:
                        if response.status_code in _RETRYABLE_STATUS_CODES and attempt == 0:
                            continue
                        raise MtApiSearchUnavailable("MTAPI Search is unavailable")
                    content_type = response.headers.get("content-type", "").lower()
                    if "application/json" not in content_type:
                        raise MtApiSearchContractError("MTAPI Search response is invalid")
                    chunks: list[bytes] = []
                    total = 0
                    for chunk in response.iter_bytes():
                        total += len(chunk)
                        if total > _MAX_RESPONSE_BYTES:
                            raise MtApiSearchContractError("MTAPI Search response is too large")
                        chunks.append(chunk)
                try:
                    document = json.loads(b"".join(chunks).decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise MtApiSearchContractError("MTAPI Search response is invalid") from exc
                now = self._now()
                if not isinstance(now, int) or isinstance(now, bool) or now < 0:
                    raise MtApiSearchContractError("MTAPI Search timestamp is invalid")
                return _parse_document(document, server, now)
            except MtApiSearchError:
                raise
            except httpx.HTTPError as exc:
                last_unavailable = exc
                if attempt == 0:
                    continue
        raise MtApiSearchUnavailable("MTAPI Search is unavailable") from last_unavailable
