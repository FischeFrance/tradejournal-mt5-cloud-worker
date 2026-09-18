"""Build and persist the immutable V1 archive consumed by trading-agent /history."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from ..state_store import atomic_json, read_json

MAX_EVENTS = 50_000
MAX_EVENTS_PER_GROUP = 4


def history_document_bytes(document: dict[str, Any]) -> bytes:
    """Return the exact UTF-8 representation written by ``atomic_json``."""

    return (
        json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def history_document_sha256(document: dict[str, Any]) -> str:
    return hashlib.sha256(history_document_bytes(document)).hexdigest()


def _groups(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    count = 0
    for raw in events:
        event = dict(raw)
        external_id = str(event.get("external_trade_id", "")).strip()
        if not external_id:
            raise ValueError("history event external_trade_id is missing")
        if external_id not in grouped:
            grouped[external_id] = []
            order.append(external_id)
        grouped[external_id].append(event)
        count += 1
        if count > MAX_EVENTS:
            raise ValueError("history event limit exceeded")
    result: list[dict[str, Any]] = []
    for external_id in order:
        values = grouped[external_id]
        for offset in range(0, len(values), MAX_EVENTS_PER_GROUP):
            result.append(
                {
                    "external_trade_id": external_id,
                    "events": values[offset : offset + MAX_EVENTS_PER_GROUP],
                }
            )
    return result


def load_or_create_history_document(
    path: Path,
    *,
    job_id: str,
    connection_id: str,
    account_number: str,
    server: str,
    history_mode: str,
    from_date: datetime | None,
    events: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Reuse the exact same document on retry so upload metadata cannot drift."""
    if path.exists():
        existing = read_json(path, {})
        validate_history_document(
            existing,
            job_id=job_id,
            connection_id=connection_id,
            account_number=account_number,
            server=server,
            history_mode=history_mode,
            from_date=from_date,
        )
        return existing
    document = build_history_document(
        job_id=job_id,
        connection_id=connection_id,
        account_number=account_number,
        server=server,
        history_mode=history_mode,
        from_date=from_date,
        events=events,
    )
    atomic_json(path, document)
    return document


def validate_history_document(
    document: dict[str, Any],
    *,
    job_id: str,
    connection_id: str,
    account_number: str,
    server: str,
    history_mode: str,
    from_date: datetime | None,
) -> None:
    normalized_from_date = (
        from_date.astimezone(timezone.utc).isoformat()
        if from_date is not None
        else None
    )
    if (
        document.get("schema_version") != 1
        or document.get("job_id") != job_id
        or document.get("connection_id") != connection_id
        or document.get("account_number") != account_number
        or document.get("server") != server
        or document.get("history_mode") != history_mode
        or document.get("from_date") != normalized_from_date
        or not isinstance(document.get("generated_at"), str)
        or not isinstance(document.get("trades"), list)
    ):
        raise ValueError("history archive identity mismatch")


def build_history_document(
    *,
    job_id: str,
    connection_id: str,
    account_number: str,
    server: str,
    history_mode: str,
    from_date: datetime | None,
    events: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Build a fresh document without publishing it as the immutable archive.

    History/live handoff uses this to write a staging file first.  Its frozen
    snapshot artifact is made durable before that file is atomically promoted
    to the immutable archive name, eliminating an archive-without-baseline
    crash window.
    """

    if history_mode not in ("all_available", "from_date"):
        raise ValueError("history archive mode is invalid")
    normalized_from_date = (
        from_date.astimezone(timezone.utc).isoformat()
        if from_date is not None
        else None
    )
    return {
        "schema_version": 1,
        "job_id": job_id,
        "connection_id": connection_id,
        "account_number": account_number,
        "server": server,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "history_mode": history_mode,
        "from_date": normalized_from_date,
        "trades": _groups(events),
    }
