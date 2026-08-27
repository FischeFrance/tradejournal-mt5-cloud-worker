"""Build and deliver one compressed JSON document for an MT5 history job."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import tempfile
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from worker.atomic_file import durable_replace


MAX_COMPRESSED_BYTES = 6 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 48 * 1024 * 1024
MAX_EVENTS = 50_000


@dataclass(frozen=True)
class HistoryArchive:
    path: Path
    compressed: bytes
    compressed_sha256: str
    uncompressed_bytes: int
    event_count: int


def _archive_path(root: Path, job_id: str) -> Path:
    return root / "data" / "history-imports" / f"{job_id}.json.gz"


def _decode_archive(path: Path, job_id: str, connection_id: str) -> tuple[bytes, dict]:
    compressed = path.read_bytes()
    if len(compressed) > MAX_COMPRESSED_BYTES:
        raise ValueError("history archive exceeds compressed limit")
    raw = gzip.decompress(compressed)
    if len(raw) > MAX_UNCOMPRESSED_BYTES:
        raise ValueError("history archive exceeds uncompressed limit")
    document = json.loads(raw)
    if (
        not isinstance(document, dict)
        or document.get("job_id") != job_id
        or document.get("connection_id") != connection_id
        or not isinstance(document.get("trades"), list)
    ):
        raise ValueError("persisted history archive identity is invalid")
    return compressed, document


def _atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        durable_replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def build_history_archive(
    root: Path,
    *,
    job_id: str,
    connection_id: str,
    account_number: str,
    server: str,
    history_mode: str,
    from_date: datetime | None,
    events: list[dict],
) -> HistoryArchive:
    """Create once per job so a retry reuses byte-identical upload metadata."""
    path = _archive_path(root, job_id)
    if path.exists():
        compressed, document = _decode_archive(path, job_id, connection_id)
        event_count = sum(
            len(trade.get("events", []))
            for trade in document["trades"]
            if isinstance(trade, dict) and isinstance(trade.get("events"), list)
        )
        raw = gzip.decompress(compressed)
        return HistoryArchive(
            path=path,
            compressed=compressed,
            compressed_sha256=hashlib.sha256(compressed).hexdigest(),
            uncompressed_bytes=len(raw),
            event_count=event_count,
        )

    if history_mode not in ("all_available", "from_date"):
        raise ValueError("history archive requires a history window")
    if len(events) > MAX_EVENTS:
        raise ValueError("history archive event limit exceeded")

    grouped: OrderedDict[str, list[dict]] = OrderedDict()
    for event in events:
        external_id = str(event.get("external_trade_id") or "")
        if not external_id:
            raise ValueError("history event has no external trade id")
        grouped.setdefault(external_id, []).append(event)
    document = {
        "schema_version": 1,
        "job_id": job_id,
        "connection_id": connection_id,
        "account_number": account_number,
        "server": server,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "history_mode": history_mode,
        "from_date": from_date.astimezone(timezone.utc).isoformat() if from_date else None,
        "trades": [
            {"external_trade_id": external_id, "events": trade_events}
            for external_id, trade_events in grouped.items()
        ],
    }
    raw = json.dumps(
        document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    if len(raw) > MAX_UNCOMPRESSED_BYTES:
        raise ValueError("history archive exceeds uncompressed limit")
    compressed = gzip.compress(raw, compresslevel=6, mtime=0)
    if len(compressed) > MAX_COMPRESSED_BYTES:
        raise ValueError("history archive exceeds compressed limit")
    _atomic_bytes(path, compressed)
    return HistoryArchive(
        path=path,
        compressed=compressed,
        compressed_sha256=hashlib.sha256(compressed).hexdigest(),
        uncompressed_bytes=len(raw),
        event_count=len(events),
    )


def deliver_history_archive(
    api: Any,
    *,
    job_id: str,
    lease_id: str,
    archive: HistoryArchive,
    require_lease: Callable[[], None],
) -> dict:
    require_lease()
    prepared = api.history_file_prepare(
        job_id,
        lease_id,
        compressed_sha256=archive.compressed_sha256,
        compressed_bytes=len(archive.compressed),
        uncompressed_bytes=archive.uncompressed_bytes,
        event_count=archive.event_count,
    )
    if prepared.get("error_code") == "lease_lost":
        raise RuntimeError("lease_lost")
    if prepared["already_imported"]:
        try:
            archive.path.unlink()
        except FileNotFoundError:
            pass
        return {
            "accepted": prepared["accepted"],
            "inserted": prepared["inserted"],
            "duplicates": prepared["duplicates"],
            "object_deleted": True,
        }
    api.upload_history_file(prepared["upload_url"], archive.compressed)
    require_lease()
    imported = api.history_file_import(job_id, lease_id, archive.event_count)
    if imported.get("error_code") == "lease_lost":
        raise RuntimeError("lease_lost")
    try:
        archive.path.unlink()
    except FileNotFoundError:
        pass
    return imported
