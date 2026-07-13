"""Validazione fail-fast del contratto V1 e degli input usati dal provisioner."""

from __future__ import annotations

import json
import ipaddress
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping
from urllib.parse import unquote, urlsplit
from uuid import UUID

from .models import Action, ProvisioningJob

JOB_VERSION = 1
_JOB_FIELDS = {
    "version",
    "job_id",
    "action",
    "connection_id",
    "account_number",
    "server",
    "tradejournal_api_url",
    "created_at",
}
_SECRET_MARKERS = ("password", "token", "secret")
_ACCOUNT_RE = re.compile(r"^[1-9][0-9]{4,19}$")
_SERVER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,127}$")
_MALFORMED_PERCENT_RE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_MAX_JOB_BYTES = 64 * 1024
_MAX_URL_LENGTH = 2048


class ValidationError(ValueError):
    """Input non valido o potenzialmente pericoloso."""


def validate_uuid(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValidationError(f"{field_name} deve essere un UUID canonico.")
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValidationError(f"{field_name} deve essere un UUID valido.") from exc
    canonical = str(parsed)
    if value.lower() != canonical:
        raise ValidationError(f"{field_name} deve usare il formato UUID canonico '{canonical}'.")
    return canonical


def validate_account_number(value: object) -> str:
    if not isinstance(value, str) or not _ACCOUNT_RE.fullmatch(value):
        raise ValidationError("account_number deve contenere da 5 a 20 cifre e non iniziare con zero.")
    return value


def _reject_control_or_traversal(value: str, field_name: str) -> None:
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValidationError(f"{field_name} contiene caratteri di controllo non ammessi.")
    if ".." in value or "/" in value or "\\" in value:
        raise ValidationError(f"{field_name} contiene una sequenza di path traversal non ammessa.")


def _reject_control_characters(value: str, field_name: str) -> None:
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValidationError(f"{field_name} contiene caratteri di controllo non ammessi.")


def validate_server(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValidationError("server deve essere una stringa non vuota senza spazi esterni.")
    _reject_control_or_traversal(value, "server")
    if not _SERVER_RE.fullmatch(value):
        raise ValidationError(
            "server deve essere lungo al massimo 128 caratteri e contenere solo lettere, "
            "numeri, spazio, punto, underscore o trattino."
        )
    return value


def validate_tradejournal_url(value: object, allow_insecure_http: bool = False) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValidationError("tradejournal_api_url deve essere una URL non vuota.")
    if len(value) > _MAX_URL_LENGTH:
        raise ValidationError(f"tradejournal_api_url supera {_MAX_URL_LENGTH} caratteri.")
    _reject_control_characters(value, "tradejournal_api_url")
    if any(char.isspace() for char in value) or "\\" in value:
        raise ValidationError("tradejournal_api_url contiene spazi o backslash non ammessi.")
    if _MALFORMED_PERCENT_RE.search(value):
        raise ValidationError("tradejournal_api_url contiene percent-encoding non valido.")
    decoded = unquote(value)
    _reject_control_characters(decoded, "tradejournal_api_url decodificata")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
    except ValueError as exc:
        raise ValidationError("tradejournal_api_url non e' sintatticamente valida.") from exc
    allowed_schemes = {"https"} | ({"http"} if allow_insecure_http else set())
    if parsed.scheme not in allowed_schemes:
        requirement = "HTTPS (HTTP e' ammesso solo in modalita' test locale)"
        raise ValidationError(f"tradejournal_api_url deve usare {requirement}.")
    if not hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValidationError(
            "tradejournal_api_url deve avere un host e non puo' contenere credenziali o fragment."
        )
    decoded_path = unquote(parsed.path)
    if any(segment in {".", ".."} for segment in decoded_path.split("/")):
        raise ValidationError("tradejournal_api_url contiene path traversal non ammesso.")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValidationError("tradejournal_api_url contiene una porta non valida.") from exc
    if port == 0:
        raise ValidationError("tradejournal_api_url contiene una porta non valida.")
    if parsed.scheme == "http":
        local = hostname.lower() == "localhost"
        try:
            local = local or ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            pass
        if not local:
            raise ValidationError(
                "HTTP e' ammesso in modalita' test soltanto verso localhost/loopback."
            )
    return value


def validate_created_at(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValidationError("created_at deve essere un timestamp ISO8601 UTC.")
    _reject_control_characters(value, "created_at")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValidationError("created_at deve essere un timestamp ISO8601 valido.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValidationError("created_at deve essere in UTC.")
    return value


def validate_job_data(
    raw: Mapping[str, Any], *, allow_insecure_http: bool = False
) -> ProvisioningJob:
    if not isinstance(raw, Mapping):
        raise ValidationError("Il job deve essere un oggetto JSON.")
    for key in raw:
        if not isinstance(key, str):
            raise ValidationError("I nomi dei campi job devono essere stringhe.")
        lowered = str(key).lower()
        if any(marker in lowered for marker in _SECRET_MARKERS):
            raise ValidationError("Il JSON del job non deve contenere password, token o secret.")
    unexpected = set(raw) - _JOB_FIELDS
    if unexpected:
        raise ValidationError(f"Campi job non supportati: {', '.join(sorted(unexpected))}.")
    missing = {"version", "job_id", "action", "connection_id", "created_at"} - set(raw)
    if missing:
        raise ValidationError(f"Campi job obbligatori mancanti: {', '.join(sorted(missing))}.")
    if (
        isinstance(raw["version"], bool)
        or not isinstance(raw["version"], int)
        or raw["version"] != JOB_VERSION
    ):
        raise ValidationError(f"version deve essere {JOB_VERSION}.")
    try:
        action = Action(str(raw["action"]))
    except ValueError as exc:
        allowed = ", ".join(action.value for action in Action)
        raise ValidationError(f"action non valida; valori ammessi: {allowed}.") from exc

    account = raw.get("account_number")
    server = raw.get("server")
    api_url = raw.get("tradejournal_api_url")
    if action is Action.PROVISION:
        if account is None or server is None or api_url is None:
            raise ValidationError(
                "provision richiede account_number, server e tradejournal_api_url."
            )
        account = validate_account_number(account)
        server = validate_server(server)
        api_url = validate_tradejournal_url(api_url, allow_insecure_http)
    else:
        if account is not None:
            account = validate_account_number(account)
        if server is not None:
            server = validate_server(server)
        if api_url is not None:
            api_url = validate_tradejournal_url(api_url, allow_insecure_http)

    return ProvisioningJob(
        version=JOB_VERSION,
        job_id=validate_uuid(raw["job_id"], "job_id"),
        action=action,
        connection_id=validate_uuid(raw["connection_id"], "connection_id"),
        account_number=account,
        server=server,
        tradejournal_api_url=api_url,
        created_at=validate_created_at(raw["created_at"]),
    )


def load_job(path: Path, *, allow_insecure_http: bool = False) -> ProvisioningJob:
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValidationError(f"Job file non valido o non regolare: {path}.")
    if path.stat().st_size > _MAX_JOB_BYTES:
        raise ValidationError(f"Job file troppo grande (massimo {_MAX_JOB_BYTES} byte).")
    try:
        raw: Dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"Impossibile leggere il job JSON: {path}.") from exc
    return validate_job_data(raw, allow_insecure_http=allow_insecure_http)
