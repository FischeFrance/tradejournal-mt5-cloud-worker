from __future__ import annotations

import gzip
import hashlib
import json
import random
import re
import time
from datetime import datetime
from typing import Callable
from urllib.parse import parse_qs, unquote, urljoin, urlparse
from uuid import UUID

import httpx

MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 0.5
BACKOFF_MAX_SECONDS = 5.0
JOB_TYPES = frozenset(("provision", "deprovision", "historical_sync", "live_sync"))
HISTORY_MODES = frozenset(("new_only", "from_date", "all_available"))
PROGRESS_EVENT_CODES = frozenset(
    (
        "endpoint_resolution",
        "broker_identity",
        "broker_discovery",
        "instance_preparation",
        "terminal_start",
        "investor_verification",
        "bridge_activation",
        "history_sync",
        "sync_activation",
        "deprovision",
        "live_sync",
    )
)
PROGRESS_EVENT_STATUSES = frozenset(
    ("started", "completed", "failed", "skipped", "info")
)
PROGRESS_DETAIL_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
MAX_HISTORY_COMPRESSED_BYTES = 6 * 1024 * 1024
MAX_HISTORY_UNCOMPRESSED_BYTES = 48 * 1024 * 1024
MAX_HISTORY_EVENTS = 50_000
HISTORY_BUCKET = "mt5-history-imports"
HISTORY_OBJECT_PATTERN = re.compile(
    r"^[0-9a-f-]{36}/[0-9a-f-]{36}/[0-9a-f-]{36}/history\.json\.gz$"
)


class AgentContractError(RuntimeError):
    """The control plane returned a response outside the pinned V1 contract."""


def _normalized_origin(parsed) -> tuple[str, str | None, int | None]:
    port = parsed.port
    if (parsed.scheme == "https" and port == 443) or (
        parsed.scheme == "http" and port == 80
    ):
        port = None
    return parsed.scheme, parsed.hostname, port


def _history_upload_origins(parsed) -> frozenset[tuple[str, str | None, int | None]]:
    """Return the exact origins allowed for a signed historical archive upload.

    Supabase Edge Functions and Storage intentionally use two different hosts for
    the same project.  A control plane at ``<ref>.functions.supabase.co`` may
    therefore issue a signed upload URL at ``<ref>.supabase.co``.  No other
    cross-origin upload is accepted.
    """
    origins = {_normalized_origin(parsed)}
    hostname = parsed.hostname or ""
    functions_suffix = ".functions.supabase.co"
    if parsed.scheme == "https" and hostname.endswith(functions_suffix):
        project_ref = hostname[: -len(functions_suffix)]
        if project_ref and "." not in project_ref:
            origins.add(("https", f"{project_ref}.supabase.co", None))
    return frozenset(origins)


def _uuid(value: object, field: str) -> None:
    try:
        UUID(str(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise AgentContractError(f"{field} is not a UUID") from exc


def _timestamp(value: object, field: str) -> None:
    if not isinstance(value, str):
        raise AgentContractError(f"{field} is not a timestamp")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AgentContractError(f"{field} is not a timestamp") from exc


def _non_negative_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _validate_history_prepare(
    prepared: dict, *, job_id: str, connection_id: str
) -> tuple[str, str | None]:
    required = {
        "api_version", "already_imported", "object_path", "upload_url",
        "expires_in", "accepted", "inserted", "duplicates",
    }
    object_path = prepared.get("object_path")
    counts = tuple(prepared.get(field) for field in ("accepted", "inserted", "duplicates"))
    if (
        set(prepared) != required
        or prepared.get("api_version") != "1"
        or not isinstance(prepared.get("already_imported"), bool)
        or not isinstance(object_path, str)
        or HISTORY_OBJECT_PATTERN.fullmatch(object_path) is None
        or not all(_non_negative_integer(value) for value in counts)
        or counts[1] + counts[2] != counts[0]
        or not _non_negative_integer(prepared.get("expires_in"))
        or prepared["expires_in"] > 7200
    ):
        raise AgentContractError("history upload response does not match contract V1")
    path_parts = object_path.split("/")
    if path_parts[1] != connection_id or path_parts[2] != job_id:
        raise AgentContractError("history upload object identity mismatch")
    for value in path_parts[:3]:
        try:
            if str(UUID(value)) != value:
                raise ValueError
        except (ValueError, AttributeError):
            raise AgentContractError("history upload object identity mismatch") from None
    upload_url = prepared.get("upload_url")
    if prepared["already_imported"]:
        if upload_url is not None or prepared["expires_in"] != 0:
            raise AgentContractError("history upload response does not match contract V1")
        return object_path, None
    if not isinstance(upload_url, str) or prepared["expires_in"] <= 0 or counts != (0, 0, 0):
        raise AgentContractError("history upload response does not match contract V1")
    return object_path, upload_url


def _validate_claim(job: dict) -> dict:
    required = {
        "api_version", "job_id", "connection_id", "lease_id", "lease_expires_at",
        "job_type", "attempt", "created_at", "history_mode", "from_date", "payload",
    }
    if set(job) != required or job.get("api_version") != "1":
        raise AgentContractError("claim response fields do not match contract V1")
    for field in ("job_id", "connection_id", "lease_id"):
        _uuid(job.get(field), field)
    for field in ("lease_expires_at", "created_at"):
        _timestamp(job.get(field), field)
    if job.get("job_type") not in JOB_TYPES:
        raise AgentContractError("unsupported job_type")
    attempt = job.get("attempt")
    if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
        raise AgentContractError("attempt must be a positive integer")
    if job.get("history_mode") is not None and job.get("history_mode") not in HISTORY_MODES:
        raise AgentContractError("unsupported history_mode")
    if job.get("from_date") is not None:
        _timestamp(job.get("from_date"), "from_date")
    payload = job.get("payload")
    if not isinstance(payload, dict):
        raise AgentContractError("payload must be an object")
    if job["job_type"] == "provision":
        if set(payload) != {
            "credential_envelope",
            "expected_login",
            "expected_server",
            "broker_label",
            "bridge_token",
        }:
            raise AgentContractError("provision payload fields do not match contract V1")
        envelope = payload.get("credential_envelope")
        if (
            not isinstance(envelope, dict)
            or set(envelope) != {"alg", "iv", "ciphertext"}
            or envelope.get("alg") != "aes-256-gcm-v1"
            or not all(isinstance(envelope.get(key), str) and envelope[key] for key in ("iv", "ciphertext"))
        ):
            raise AgentContractError("credential envelope does not match contract V1")
        if (
            not isinstance(payload.get("expected_login"), str)
            or not payload["expected_login"].isdigit()
            or not isinstance(payload.get("expected_server"), str)
            or not payload["expected_server"]
            or (
                payload.get("broker_label") is not None
                and (
                    not isinstance(payload.get("broker_label"), str)
                    or not payload["broker_label"]
                )
            )
            or not isinstance(payload.get("bridge_token"), str)
            or not payload["bridge_token"].startswith("tjmt5_")
        ):
            raise AgentContractError("provision identity/token fields are invalid")
    return job


def _validate_heartbeat(response: dict) -> dict:
    if response.get("api_version") != "1" or not isinstance(response.get("lease_valid"), bool):
        raise AgentContractError("heartbeat response does not match contract V1")
    if response["lease_valid"]:
        if set(response) != {"api_version", "lease_valid"}:
            raise AgentContractError("heartbeat success fields do not match contract V1")
    elif set(response) != {"api_version", "lease_valid", "error_code"} or response.get("error_code") != "lease_lost":
        raise AgentContractError("heartbeat lease-loss fields do not match contract V1")
    return response


def _validate_transition(response: dict, requested: str) -> dict:
    if response.get("api_version") != "1":
        raise AgentContractError("transition response does not match contract V1")
    if response.get("error_code") == "lease_lost":
        if set(response) != {"api_version", "error_code"}:
            raise AgentContractError("transition lease-loss fields do not match contract V1")
        return response
    expected = "failed" if requested == "fail" else requested
    if set(response) != {"api_version", "status"} or response.get("status") != expected:
        raise AgentContractError("transition acknowledgement does not match request")
    return response


def _validate_progress(response: dict) -> dict:
    if response.get("error_code") == "lease_lost":
        if set(response) != {"api_version", "error_code"}:
            raise AgentContractError(
                "progress lease-loss response does not match contract V1"
            )
        return response
    if (
        set(response) != {"api_version", "event_recorded"}
        or response.get("api_version") != "1"
        or response.get("event_recorded") is not True
    ):
        raise AgentContractError("progress response does not match contract V1")
    return response


class AgentApiClient:
    API_VERSION = "1"
    def __init__(
        self,
        base_url: str,
        token: str,
        transport: httpx.BaseTransport | None = None,
        max_retries: int = MAX_RETRIES,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        parsed = urlparse(base_url)
        if parsed.scheme != "https" and parsed.hostname not in (
            "localhost",
            "127.0.0.1",
            "::1",
        ):
            raise ValueError("HTTPS required except loopback tests")
        self.base_url, self._origin = (
            base_url.rstrip("/") + "/",
            _normalized_origin(parsed),
        )
        self._history_upload_origins = _history_upload_origins(parsed)
        self.client = httpx.Client(
            transport=transport,
            timeout=10,
            follow_redirects=False,
            headers={"Authorization": f"Bearer {token}"},
        )
        # Signed Storage uploads must not inherit the control-plane bearer token.
        self._upload_client = httpx.Client(
            transport=transport, timeout=30, follow_redirects=False
        )
        self._max_retries = max_retries
        self._sleep = sleep_fn

    def request(self, method: str, path: str, payload: dict | None = None) -> dict:
        url = urljoin(self.base_url, path.lstrip("/"))
        attempt = 0
        while True:
            attempt += 1
            try:
                response = self.client.request(method, url, json=payload)
            except httpx.TransportError:
                if attempt > self._max_retries:
                    raise
                self._sleep(self._backoff_delay(attempt))
                continue
            if response.is_redirect:
                location = response.headers.get("location", "")
                target = urlparse(urljoin(url, location))
                if (target.scheme, target.hostname, target.port) != self._origin:
                    raise RuntimeError("cross-host redirect refused")
                raise RuntimeError("redirect refused")
            if response.status_code == 204:
                return {}
            if response.status_code == 409:
                # lease_lost is a structured, expected response for heartbeat/transition
                # per contracts/mt5-agent-v1/schema.json, not a transport-level error.
                return response.json()
            if response.status_code >= 500 and attempt <= self._max_retries:
                self._sleep(self._backoff_delay(attempt))
                continue
            response.raise_for_status()
            return response.json()

    def _backoff_delay(self, attempt: int) -> float:
        capped = min(BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)), BACKOFF_MAX_SECONDS)
        return capped + random.uniform(0, capped * 0.25)

    def claim(self) -> dict:
        response = self.request("POST", "claim", {"api_version": self.API_VERSION})
        return _validate_claim(response) if response else {}

    def heartbeat(self, job_id: str, lease_id: str) -> dict:
        return _validate_heartbeat(
            self.request(
                "POST",
                f"jobs/{job_id}/heartbeat",
                {"api_version": self.API_VERSION, "lease_id": lease_id},
            )
        )

    def transition(self, job_id: str, lease_id: str, status: str, result: dict | None = None) -> dict:
        if status not in ("running", "complete", "fail"):
            raise ValueError("invalid transition")
        payload = {"api_version": self.API_VERSION, "lease_id": lease_id, **(result or {})}
        return _validate_transition(
            self.request("POST", f"jobs/{job_id}/{status}", payload), status
        )

    def progress(
        self,
        job_id: str,
        lease_id: str,
        event_code: str,
        event_status: str,
        detail_code: str | None = None,
    ) -> dict:
        if event_code not in PROGRESS_EVENT_CODES:
            raise ValueError("invalid progress event code")
        if event_status not in PROGRESS_EVENT_STATUSES:
            raise ValueError("invalid progress event status")
        if detail_code is not None and not PROGRESS_DETAIL_PATTERN.fullmatch(detail_code):
            raise ValueError("invalid progress detail code")
        return _validate_progress(
            self.request(
                "POST",
                f"jobs/{job_id}/progress",
                {
                    "api_version": self.API_VERSION,
                    "lease_id": lease_id,
                    "event_code": event_code,
                    "event_status": event_status,
                    "detail_code": detail_code,
                },
            )
        )

    def import_history_file(
        self, job_id: str, lease_id: str, document: dict
    ) -> dict:
        """Import one immutable, lease-bound historical archive.

        The server-derived Storage path and the content hash make retries
        idempotent.  History never traverses the live/rate-limited endpoint.
        """
        trades = document.get("trades") if isinstance(document, dict) else None
        connection_id = document.get("connection_id") if isinstance(document, dict) else None
        if (
            not isinstance(trades, list)
            or document.get("job_id") != job_id
            or not isinstance(connection_id, str)
        ):
            raise ValueError("history document is invalid")
        _uuid(job_id, "job_id")
        _uuid(connection_id, "connection_id")
        event_count = sum(
            len(group.get("events", ()))
            for group in trades
            if isinstance(group, dict) and isinstance(group.get("events"), list)
        )
        if event_count > MAX_HISTORY_EVENTS:
            raise ValueError("history event limit exceeded")
        raw = json.dumps(
            document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        compressed = gzip.compress(raw, compresslevel=9, mtime=0)
        if len(raw) > MAX_HISTORY_UNCOMPRESSED_BYTES or len(compressed) > MAX_HISTORY_COMPRESSED_BYTES:
            raise ValueError("history archive limit exceeded")
        metadata = {
            "api_version": self.API_VERSION,
            "lease_id": lease_id,
            "compressed_sha256": hashlib.sha256(compressed).hexdigest(),
            "compressed_bytes": len(compressed),
            "uncompressed_bytes": len(raw),
            "event_count": event_count,
        }
        prepared = self.request("POST", f"jobs/{job_id}/history-upload", metadata)
        if prepared.get("error_code") == "lease_lost":
            return prepared
        object_path, upload_url = _validate_history_prepare(
            prepared, job_id=job_id, connection_id=connection_id
        )
        if upload_url is not None:
            parsed = urlparse(upload_url) if isinstance(upload_url, str) else None
            query = parse_qs(parsed.query, keep_blank_values=True) if parsed is not None else {}
            expected_path = (
                f"/storage/v1/object/upload/sign/{HISTORY_BUCKET}/{object_path}"
            )
            if (
                parsed is None
                or parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.fragment
                or _normalized_origin(parsed) not in self._history_upload_origins
                or unquote(parsed.path) != expected_path
                or set(query) != {"token"}
                or len(query["token"]) != 1
                or not query["token"][0]
            ):
                raise AgentContractError("history upload URL is invalid")
            try:
                response = self._upload_client.put(
                    upload_url,
                    content=compressed,
                    headers={
                        "Cache-Control": "max-age=3600",
                        "Content-Type": "application/gzip",
                        "x-upsert": "true",
                    },
                )
            except httpx.HTTPError:
                raise AgentContractError("history upload failed") from None
            if response.is_redirect:
                raise AgentContractError("history upload redirect refused")
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError:
                raise AgentContractError("history upload failed") from None
        imported = self.request(
            "POST",
            f"jobs/{job_id}/history-import",
            {"api_version": self.API_VERSION, "lease_id": lease_id},
        )
        if imported.get("error_code") == "lease_lost":
            return imported
        required = {"api_version", "accepted", "inserted", "duplicates", "object_deleted"}
        if (
            set(imported) != required
            or imported.get("api_version") != self.API_VERSION
            or not all(
                isinstance(imported.get(field), int)
                and not isinstance(imported.get(field), bool)
                and imported[field] >= 0
                for field in ("accepted", "inserted", "duplicates")
            )
            or imported["inserted"] + imported["duplicates"] != imported["accepted"]
            or imported.get("object_deleted") is not True
        ):
            raise AgentContractError("history import response does not match contract V1")
        return imported
