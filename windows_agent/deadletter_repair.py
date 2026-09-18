"""One-shot, fail-closed recovery for one managed MT5 live-sync dead-letter.

This module is deliberately not part of the normal Agent loop.  It is invoked during a
maintenance window as LocalSystem, while the Windows service is stopped, and operates on one
record selected by digests rather than by putting sensitive MT5 identifiers on a command line.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import urlparse

import requests

from worker.atomic_file import fsync_directory
from worker.event_outbox import EventOutbox, OutboxError, _restrict_file_access
from worker.event_sender import SendResult

from .event_supervisor import connection_sync_lock
from .provisioning.secret_store import WindowsSecretStore
from .security import canonical_uuid, safe_child


DEFAULT_INSTANCES_ROOT = Path(r"C:\TradeJournal\instances")
DEFAULT_SECRETS_ROOT = Path(r"C:\TradeJournal\secrets")
DEFAULT_ROLLBACK_ROOT = Path(r"C:\TradeJournal\rollback")
SERVICE_NAME = "TradeJournalMT5Agent"
_SERVICE_KEY = rf"SYSTEM\CurrentControlSet\Services\{SERVICE_NAME}"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DEPLOYMENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_REQUEST_KEYS = frozenset(
    {
        "schema_version",
        "connection_id",
        "deployment_id",
        "expected_outbox_sha256",
        "expected_dead_letter_count",
        "expected_record_sha256",
    }
)
_TRADE_DEAL_TYPES = frozenset((0, 1))
_MAX_JSON_BYTES = 512 * 1024 * 1024


class DeadLetterRepairError(RuntimeError):
    """A sanitized, operator-actionable maintenance failure."""


class RepairSender(Protocol):
    http_status: int | None
    outcome: str | None

    def send(self, payload: dict[str, Any]) -> SendResult: ...


@dataclass(frozen=True)
class RepairRequest:
    connection_id: str
    deployment_id: str
    expected_outbox_sha256: str
    expected_record_sha256: str
    expected_dead_letter_count: int = 1

    @classmethod
    def from_document(cls, document: Any) -> "RepairRequest":
        if not isinstance(document, dict) or set(document) != _REQUEST_KEYS:
            raise DeadLetterRepairError("repair_request_invalid")
        if document.get("schema_version") != 1:
            raise DeadLetterRepairError("repair_request_invalid")
        try:
            connection_id = canonical_uuid(str(document.get("connection_id", "")))
        except (TypeError, ValueError) as exc:
            raise DeadLetterRepairError("repair_request_invalid") from exc
        deployment_id = document.get("deployment_id")
        expected_outbox = document.get("expected_outbox_sha256")
        expected_record = document.get("expected_record_sha256")
        count = document.get("expected_dead_letter_count")
        if (
            not isinstance(deployment_id, str)
            or not _DEPLOYMENT_ID.fullmatch(deployment_id)
            or not isinstance(expected_outbox, str)
            or not _SHA256.fullmatch(expected_outbox)
            or not isinstance(expected_record, str)
            or not _SHA256.fullmatch(expected_record)
            or count != 1
            or isinstance(count, bool)
        ):
            raise DeadLetterRepairError("repair_request_invalid")
        return cls(
            connection_id=connection_id,
            deployment_id=deployment_id,
            expected_outbox_sha256=expected_outbox,
            expected_record_sha256=expected_record,
        )


@dataclass(frozen=True)
class LedgerDecision:
    classification: str
    row: dict[str, Any]
    replacement_payload: dict[str, Any] | None


class RepairHttpSender:
    """Deliver one corrected event while retaining only the allowlisted acknowledgement."""

    def __init__(self, url: str, bridge_token: str, *, timeout_seconds: float = 10.0) -> None:
        self.url = _validated_ingestion_url(url)
        if not isinstance(bridge_token, str) or not bridge_token or "\n" in bridge_token or "\r" in bridge_token:
            raise DeadLetterRepairError("bridge_token_unavailable")
        self._bridge_token = bridge_token
        self.timeout_seconds = timeout_seconds
        self.http_status: int | None = None
        self.outcome: str | None = None

    def send(self, payload: dict[str, Any]) -> SendResult:
        try:
            response = requests.post(
                self.url,
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self._bridge_token}",
                },
                timeout=self.timeout_seconds,
            )
        except requests.RequestException as exc:
            return SendResult(
                status="failed",
                error=type(exc).__name__,
                attempts=1,
                failure_type="transient",
            )
        self.http_status = response.status_code
        if response.status_code == 200:
            try:
                body = response.json()
            except (ValueError, json.JSONDecodeError):
                body = None
            outcome = body.get("status") if isinstance(body, dict) and set(body) == {"status"} else None
            if outcome in ("ok", "duplicate"):
                self.outcome = outcome
                return SendResult(status="sent", http_status=200, attempts=1)
            return SendResult(
                status="failed",
                http_status=200,
                error="invalid_acknowledgement",
                attempts=1,
                failure_type="transient",
            )
        if response.status_code in (401, 403, 408, 425, 429) or 500 <= response.status_code <= 599:
            return SendResult(
                status="failed",
                http_status=response.status_code,
                error="delivery_unavailable",
                attempts=1,
                failure_type="transient",
            )
        return SendResult(
            status="failed",
            http_status=response.status_code,
            error="rejected_by_api",
            attempts=1,
            failure_type="permanent",
        )


def read_repair_request(path: Path) -> RepairRequest:
    return RepairRequest.from_document(_read_json(path, max_bytes=64 * 1024))


def load_service_environment() -> dict[str, str]:
    if os.name != "nt":
        raise DeadLetterRepairError("windows_required")
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _SERVICE_KEY) as key:
            raw, _kind = winreg.QueryValueEx(key, "Environment")
    except OSError as exc:
        raise DeadLetterRepairError("service_environment_unavailable") from exc
    if not isinstance(raw, (list, tuple)):
        raise DeadLetterRepairError("service_environment_invalid")
    result: dict[str, str] = {}
    for item in raw:
        if not isinstance(item, str) or "=" not in item:
            raise DeadLetterRepairError("service_environment_invalid")
        name, value = item.split("=", 1)
        if not name or name in result or "\r" in value or "\n" in value:
            raise DeadLetterRepairError("service_environment_invalid")
        result[name] = value
    return result


def require_local_system_and_stopped_service() -> None:
    if os.name != "nt":
        raise DeadLetterRepairError("windows_required")
    import win32api
    import win32con
    import win32security
    import win32service
    import win32serviceutil

    token = win32security.OpenProcessToken(
        win32api.GetCurrentProcess(), win32con.TOKEN_QUERY
    )
    try:
        sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    finally:
        token.Close()
    if win32security.ConvertSidToStringSid(sid) != "S-1-5-18":
        raise DeadLetterRepairError("localsystem_required")
    try:
        status = win32serviceutil.QueryServiceStatus(SERVICE_NAME)[1]
    except Exception as exc:
        raise DeadLetterRepairError("service_status_unavailable") from exc
    if status != win32service.SERVICE_STOPPED:
        raise DeadLetterRepairError("service_must_be_stopped")


def repair_live_dead_letter(
    request: RepairRequest,
    *,
    instances_root: Path = DEFAULT_INSTANCES_ROOT,
    secrets_root: Path = DEFAULT_SECRETS_ROOT,
    rollback_root: Path = DEFAULT_ROLLBACK_ROOT,
    ingestion_url: str,
    sender: RepairSender | None = None,
    token_reader: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    """Classify and resolve one digest-selected dead-letter without losing evidence."""

    instance_root = _validated_instance_root(instances_root, request.connection_id)
    outbox_path = instance_root / "state" / "live-outbox.json"
    audit_root = instance_root / "state" / "dead-letter-resolutions"
    audit_path = audit_root / f"{request.expected_record_sha256}.json"
    receipt_path = audit_root / f"{request.expected_record_sha256}.receipt.json"
    backup_path = _backup_path(rollback_root, request)

    with connection_sync_lock(request.connection_id):
        existing_audit = _read_optional_json(audit_path)
        if existing_audit is None:
            outbox_bytes = _read_regular_bytes(outbox_path)
            if _sha256(outbox_bytes) != request.expected_outbox_sha256:
                raise DeadLetterRepairError("outbox_preimage_mismatch")
            outbox = EventOutbox(str(outbox_path))
            event_id, dead_record = _select_dead_letter(outbox, request)
            if outbox.pending_count() != 0:
                raise DeadLetterRepairError("outbox_not_quiescent")
            decision = _classify_from_coherent_ledger(instance_root, dead_record)
            audit_document = _audit_document(request, dead_record, decision)
            audit_bytes = _canonical_json_bytes(audit_document)
            _write_private_once(backup_path, outbox_bytes)
            if _sha256(_read_regular_bytes(backup_path)) != request.expected_outbox_sha256:
                raise DeadLetterRepairError("outbox_backup_mismatch")
            _write_private_once(audit_path, audit_bytes)
            audit_sha256 = _sha256(audit_bytes)
        else:
            audit_document = _validate_existing_audit(existing_audit, request)
            audit_bytes = _read_regular_bytes(audit_path)
            if _canonical_json_bytes(existing_audit) != audit_bytes:
                raise DeadLetterRepairError("resolution_audit_noncanonical")
            audit_sha256 = _sha256(audit_bytes)
            dead_record = copy.deepcopy(audit_document["dead_letter_record"])
            event_id = str(dead_record["payload"]["event_id"])
            decision = LedgerDecision(
                classification=str(audit_document["classification"]),
                row=copy.deepcopy(audit_document["ledger_record"]),
                replacement_payload=copy.deepcopy(audit_document.get("replacement_payload")),
            )

        if decision.classification == "non_trading_accounting":
            result = _resolve_non_trading(
                request,
                outbox_path,
                event_id,
                audit_path,
                audit_sha256,
            )
        elif decision.classification == "real_trade" and decision.replacement_payload is not None:
            effective_sender = sender
            if effective_sender is None:
                reader = token_reader or (
                    lambda connection_id: WindowsSecretStore(secrets_root).read(
                        connection_id, "bridge_token"
                    )
                )
                effective_sender = RepairHttpSender(
                    ingestion_url,
                    reader(request.connection_id),
                )
            result = _resolve_real_trade(
                request,
                outbox_path,
                event_id,
                dead_record,
                decision.replacement_payload,
                effective_sender,
            )
        else:  # pragma: no cover - validated classifiers cannot produce another value
            raise DeadLetterRepairError("ledger_classification_ambiguous")

        receipt_document = {
            "schema_version": 1,
            "classification": result["classification"],
            "record_sha256": request.expected_record_sha256,
            "audit_sha256": audit_sha256,
            "action": result["action"],
            "http_status": result.get("http_status"),
            "outcome": result.get("outcome"),
        }
        _write_private_once(receipt_path, _canonical_json_bytes(receipt_document))
        refreshed = EventOutbox(str(outbox_path))
        return {
            "schema_version": 1,
            "status": "resolved",
            "classification": result["classification"],
            "action": result["action"],
            "http_status": result.get("http_status"),
            "outcome": result.get("outcome"),
            "record_sha256": request.expected_record_sha256,
            "audit_sha256": audit_sha256,
            "backup_sha256": request.expected_outbox_sha256,
            "outbox_after_sha256": _sha256(_read_regular_bytes(outbox_path)),
            "pending_count": refreshed.pending_count(),
            "dead_letter_count": refreshed.dead_letter_count(),
        }


def _resolve_non_trading(
    request: RepairRequest,
    outbox_path: Path,
    event_id: str,
    audit_path: Path,
    audit_sha256: str,
) -> dict[str, Any]:
    outbox = EventOutbox(str(outbox_path))
    record = outbox.dead_letters().get(event_id)
    if record is not None:
        if EventOutbox.record_sha256(record) != request.expected_record_sha256:
            raise DeadLetterRepairError("dead_letter_changed")
        try:
            outbox.resolve_non_trading_dead_letter(
                event_id,
                expected_record_sha256=request.expected_record_sha256,
                audit_path=str(audit_path),
                expected_audit_sha256=audit_sha256,
            )
        except OutboxError as exc:
            raise DeadLetterRepairError("dead_letter_resolution_failed") from exc
    elif event_id in outbox.pending_payloads():
        raise DeadLetterRepairError("dead_letter_state_conflict")
    return {
        "classification": "non_trading_accounting",
        "action": "audit_archived_barrier_removed",
    }


def _resolve_real_trade(
    request: RepairRequest,
    outbox_path: Path,
    event_id: str,
    original_record: dict[str, Any],
    replacement_payload: dict[str, Any],
    sender: RepairSender,
) -> dict[str, Any]:
    outbox = EventOutbox(str(outbox_path))
    pending = outbox.pending_payloads()
    dead = outbox.dead_letters()
    if event_id in dead:
        if EventOutbox.record_sha256(dead[event_id]) != request.expected_record_sha256:
            raise DeadLetterRepairError("dead_letter_changed")
        if outbox.pending_count() != 0:
            raise DeadLetterRepairError("outbox_not_quiescent")
        try:
            outbox.requeue_dead_letter_first(
                event_id,
                replacement_payload,
                expected_record_sha256=request.expected_record_sha256,
            )
        except OutboxError as exc:
            raise DeadLetterRepairError("dead_letter_requeue_failed") from exc
    elif event_id in pending:
        if _canonical_payload_sha256(pending[event_id]) != _canonical_payload_sha256(replacement_payload):
            raise DeadLetterRepairError("pending_payload_changed")
        if outbox.pending_count() != 1 or outbox.dead_letter_count() != 0:
            raise DeadLetterRepairError("outbox_not_quiescent")
    else:
        # A crash can occur after the idempotent API accepted the event but before the receipt
        # was written. Re-sending the same event_id can only return ok/duplicate.
        send_result = sender.send(copy.deepcopy(replacement_payload))
        _require_acknowledged(send_result, sender)
        return {
            "classification": "real_trade",
            "action": "delivery_confirmed_after_recovery",
            "http_status": sender.http_status,
            "outcome": sender.outcome,
        }

    drain = EventOutbox(str(outbox_path)).drain(sender)  # type: ignore[arg-type]
    if drain.sent != 1 or drain.pending != 0:
        raise DeadLetterRepairError("corrected_delivery_incomplete")
    _require_acknowledged(SendResult(status="sent", http_status=sender.http_status), sender)
    final = EventOutbox(str(outbox_path))
    if final.pending_count() != 0 or final.dead_letter_count() != 0:
        raise DeadLetterRepairError("corrected_delivery_incomplete")
    if replacement_payload["event_id"] != original_record["payload"]["event_id"]:
        raise DeadLetterRepairError("event_identity_changed")
    return {
        "classification": "real_trade",
        "action": "corrected_event_delivered",
        "http_status": sender.http_status,
        "outcome": sender.outcome,
    }


def _require_acknowledged(result: SendResult, sender: RepairSender) -> None:
    if (
        result.status != "sent"
        or sender.http_status != 200
        or sender.outcome not in ("ok", "duplicate")
    ):
        raise DeadLetterRepairError("corrected_delivery_incomplete")


def _select_dead_letter(
    outbox: EventOutbox,
    request: RepairRequest,
) -> tuple[str, dict[str, Any]]:
    dead = outbox.dead_letters()
    if len(dead) != request.expected_dead_letter_count:
        raise DeadLetterRepairError("dead_letter_count_mismatch")
    matches = [
        (event_id, record)
        for event_id, record in dead.items()
        if EventOutbox.record_sha256(record) == request.expected_record_sha256
    ]
    if len(matches) != 1:
        raise DeadLetterRepairError("dead_letter_record_mismatch")
    event_id, record = matches[0]
    payload = record.get("payload")
    symbol = payload.get("symbol") if isinstance(payload, dict) else None
    if (
        record.get("failure_type") != "permanent"
        or record.get("http_status") != 422
        or record.get("error") != "rejected_by_api"
        or not isinstance(payload, dict)
        or payload.get("event_id") != event_id
        or payload.get("event_type") != "trade_opened"
        or not isinstance(payload.get("event_time"), str)
        or not isinstance(payload.get("external_trade_id"), str)
        or (isinstance(symbol, str) and symbol.strip())
        or symbol is not None and not isinstance(symbol, str)
    ):
        raise DeadLetterRepairError("dead_letter_not_expected_symbol_rejection")
    return event_id, record


def _classify_from_coherent_ledger(
    instance_root: Path,
    dead_record: dict[str, Any],
) -> LedgerDecision:
    checkpoint = _read_json(instance_root / "state" / "history.json")
    report = _read_json(instance_root / "data" / "history-balance-backfill.json")
    anchor = report.get("anchor") if isinstance(report, dict) else None
    if (
        not isinstance(checkpoint, dict)
        or not isinstance(report, dict)
        or report.get("schema_version") != 1
        or report.get("source") != "mt5_historical_ledger"
        or not isinstance(anchor, dict)
        or anchor.get("coherent") is not True
    ):
        raise DeadLetterRepairError("ledger_not_coherent")
    deal_count = anchor.get("deal_count")
    accounting_count = checkpoint.get("accounting_deals")
    if (
        not isinstance(deal_count, int)
        or isinstance(deal_count, bool)
        or deal_count <= 0
        or accounting_count != deal_count
        or isinstance(accounting_count, bool)
    ):
        raise DeadLetterRepairError("ledger_not_coherent")
    payload = dead_record["payload"]
    event_time = _parse_aware_datetime(payload.get("event_time"))
    through = _parse_aware_datetime(checkpoint.get("through"))
    anchor_as_of = _parse_aware_datetime(anchor.get("as_of"))
    if through < event_time or anchor_as_of < event_time:
        raise DeadLetterRepairError("ledger_not_fresh")

    rows = _read_accounting_rows(instance_root / "data" / "history-accounting.jsonl")
    if len(rows) != deal_count:
        raise DeadLetterRepairError("ledger_not_complete")
    row = _match_ledger_row(payload, tuple(rows.values()))
    deal_type = row.get("deal_type")
    position_id = str(row.get("position_id", "")).strip()
    entry = str(row.get("entry", "")).strip().upper()
    symbol = row.get("symbol")
    if isinstance(deal_type, int) and not isinstance(deal_type, bool):
        if deal_type not in _TRADE_DEAL_TYPES or position_id == "0":
            return LedgerDecision("non_trading_accounting", row, None)
        if (
            deal_type in _TRADE_DEAL_TYPES
            and position_id
            and position_id != "0"
            and entry == "IN"
            and isinstance(symbol, str)
            and symbol.strip()
        ):
            replacement = copy.deepcopy(payload)
            replacement.update(
                {
                    "symbol": symbol.strip(),
                    "direction": "buy" if deal_type == 0 else "sell",
                    "external_trade_id": position_id,
                    "native_deal_ticket": str(row.get("ticket")),
                    "time_basis": "broker_server_unresolved",
                }
            )
            if row.get("time_msc") is not None:
                replacement["time_msc"] = row["time_msc"]
            if replacement.get("event_id") != payload.get("event_id"):
                raise DeadLetterRepairError("event_identity_changed")
            return LedgerDecision("real_trade", row, replacement)
    raise DeadLetterRepairError("ledger_classification_ambiguous")


def _read_accounting_rows(path: Path) -> dict[str, dict[str, Any]]:
    raw = _read_regular_bytes(path, max_bytes=_MAX_JSON_BYTES)
    rows: dict[str, dict[str, Any]] = {}
    for encoded_line in raw.splitlines():
        if not encoded_line.strip():
            continue
        try:
            document = json.loads(encoded_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DeadLetterRepairError("ledger_audit_invalid") from exc
        record = document.get("record") if isinstance(document, dict) else None
        if document.get("kind") != "accounting_deals" or not isinstance(record, dict):
            raise DeadLetterRepairError("ledger_audit_invalid")
        ticket = str(record.get("ticket", "")).strip()
        if not ticket:
            raise DeadLetterRepairError("ledger_audit_invalid")
        previous = rows.get(ticket)
        if previous is not None and _canonical_json_bytes(previous) != _canonical_json_bytes(record):
            raise DeadLetterRepairError("ledger_audit_conflict")
        rows[ticket] = record
    return rows


def _match_ledger_row(
    payload: dict[str, Any],
    rows: tuple[dict[str, Any], ...],
) -> dict[str, Any]:
    native_ticket = str(payload.get("native_deal_ticket") or "").strip()
    external_id = str(payload.get("external_trade_id") or "").strip()
    if native_ticket:
        candidates = [row for row in rows if str(row.get("ticket", "")) == native_ticket]
    elif external_id:
        candidates = [
            row
            for row in rows
            if external_id
            in {
                str(row.get("ticket", "")),
                str(row.get("order_id", "")),
                str(row.get("position_id", "")),
            }
        ]
    else:
        raise DeadLetterRepairError("ledger_match_ambiguous")
    event_time = _parse_aware_datetime(payload.get("event_time"))
    candidates = [
        row
        for row in candidates
        if abs((_parse_aware_datetime(row.get("time")) - event_time).total_seconds()) <= 1.0
    ]
    time_msc = payload.get("time_msc")
    if time_msc is not None:
        candidates = [row for row in candidates if row.get("time_msc") == time_msc]
    comparisons = (
        ("volume", "volume"),
        ("open_price", "price"),
        ("profit", "profit"),
        ("swap", "swap"),
    )
    for payload_field, row_field in comparisons:
        if payload.get(payload_field) is not None:
            candidates = [
                row
                for row in candidates
                if _numbers_equal(payload[payload_field], row.get(row_field))
            ]
    if payload.get("commission") is not None:
        candidates = [
            row
            for row in candidates
            if _numbers_equal(
                payload["commission"],
                _decimal_sum(row.get("commission"), row.get("fee")),
            )
        ]
    if len(candidates) != 1:
        raise DeadLetterRepairError("ledger_match_ambiguous")
    return copy.deepcopy(candidates[0])


def _audit_document(
    request: RepairRequest,
    dead_record: dict[str, Any],
    decision: LedgerDecision,
) -> dict[str, Any]:
    payload = dead_record["payload"]
    return {
        "schema_version": 1,
        "connection_id": request.connection_id,
        "deployment_id": request.deployment_id,
        "classification": decision.classification,
        "record_sha256": request.expected_record_sha256,
        "event_id_sha256": hashlib.sha256(payload["event_id"].encode("utf-8")).hexdigest(),
        "dead_letter_record": copy.deepcopy(dead_record),
        "ledger_record": copy.deepcopy(decision.row),
        "replacement_payload": copy.deepcopy(decision.replacement_payload),
    }


def _validate_existing_audit(document: Any, request: RepairRequest) -> dict[str, Any]:
    if (
        not isinstance(document, dict)
        or document.get("schema_version") != 1
        or document.get("connection_id") != request.connection_id
        or document.get("deployment_id") != request.deployment_id
        or document.get("record_sha256") != request.expected_record_sha256
        or document.get("classification") not in ("real_trade", "non_trading_accounting")
        or not isinstance(document.get("dead_letter_record"), dict)
        or not isinstance(document.get("ledger_record"), dict)
    ):
        raise DeadLetterRepairError("resolution_audit_invalid")
    record = document["dead_letter_record"]
    if EventOutbox.record_sha256(record) != request.expected_record_sha256:
        raise DeadLetterRepairError("resolution_audit_invalid")
    payload = record.get("payload")
    if not isinstance(payload, dict) or not isinstance(payload.get("event_id"), str):
        raise DeadLetterRepairError("resolution_audit_invalid")
    expected_event_digest = hashlib.sha256(payload["event_id"].encode("utf-8")).hexdigest()
    if document.get("event_id_sha256") != expected_event_digest:
        raise DeadLetterRepairError("resolution_audit_invalid")
    replacement = document.get("replacement_payload")
    if document["classification"] == "real_trade":
        if not isinstance(replacement, dict) or replacement.get("event_id") != payload["event_id"]:
            raise DeadLetterRepairError("resolution_audit_invalid")
    elif replacement is not None:
        raise DeadLetterRepairError("resolution_audit_invalid")
    return document


def _backup_path(root: Path, request: RepairRequest) -> Path:
    base = Path(root).resolve()
    target = (base / request.deployment_id / request.connection_id).resolve()
    try:
        target.relative_to(base)
    except ValueError as exc:
        raise DeadLetterRepairError("rollback_path_invalid") from exc
    _reject_existing_reparse_ancestors(base, target)
    return target / "live-outbox.before.json"


def _validated_instance_root(root: Path, connection_id: str) -> Path:
    try:
        instance = safe_child(Path(root), connection_id)
    except ValueError as exc:
        raise DeadLetterRepairError("instance_path_invalid") from exc
    if _is_reparse_point(instance) or not instance.is_dir():
        raise DeadLetterRepairError("instance_path_invalid")
    return instance


def _reject_existing_reparse_ancestors(base: Path, target: Path) -> None:
    current = base
    if current.exists() and _is_reparse_point(current):
        raise DeadLetterRepairError("rollback_path_invalid")
    relative = target.relative_to(base)
    for part in relative.parts:
        current = current / part
        if current.exists() and _is_reparse_point(current):
            raise DeadLetterRepairError("rollback_path_invalid")


def _is_reparse_point(path: Path) -> bool:
    try:
        value = path.lstat()
    except OSError:
        return True
    return path.is_symlink() or bool(getattr(value, "st_file_attributes", 0) & 0x400)


def _write_private_once(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if _is_reparse_point(path.parent):
        raise DeadLetterRepairError("maintenance_path_invalid")
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        _restrict_file_access(str(path), descriptor)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        fsync_directory(path.parent)
    except FileExistsError:
        if _read_regular_bytes(path) != payload:
            raise DeadLetterRepairError("immutable_maintenance_artifact_conflict")
    except DeadLetterRepairError:
        raise
    except OSError as exc:
        raise DeadLetterRepairError("maintenance_artifact_write_failed") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def write_private_result(path: Path, result: Mapping[str, Any]) -> None:
    payload = _canonical_json_bytes(dict(result))
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise DeadLetterRepairError("result_path_exists")
    _write_private_once(path, payload)


def _read_optional_json(path: Path) -> Any | None:
    if not path.exists():
        return None
    return _read_json(path)


def _read_json(path: Path, *, max_bytes: int = _MAX_JSON_BYTES) -> Any:
    try:
        return json.loads(_read_regular_bytes(path, max_bytes=max_bytes).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DeadLetterRepairError("json_artifact_invalid") from exc


def _read_regular_bytes(path: Path, *, max_bytes: int = _MAX_JSON_BYTES) -> bytes:
    descriptor = -1
    try:
        path_stat = path.lstat()
        if path.is_symlink() or not stat.S_ISREG(path_stat.st_mode) or path_stat.st_size > max_bytes:
            raise DeadLetterRepairError("maintenance_file_invalid")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (path_stat.st_dev, path_stat.st_ino)
            or opened.st_size > max_bytes
        ):
            raise DeadLetterRepairError("maintenance_file_invalid")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        value = b"".join(chunks)
        if len(value) > max_bytes:
            raise DeadLetterRepairError("maintenance_file_invalid")
        return value
    except DeadLetterRepairError:
        raise
    except OSError as exc:
        raise DeadLetterRepairError("maintenance_file_unavailable") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise DeadLetterRepairError("maintenance_json_invalid") from exc


def _canonical_payload_sha256(payload: dict[str, Any]) -> str:
    return _sha256(_canonical_json_bytes(payload))


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _parse_aware_datetime(value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        raise DeadLetterRepairError("ledger_time_invalid")
    normalized = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise DeadLetterRepairError("ledger_time_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DeadLetterRepairError("ledger_time_invalid")
    return parsed


def _to_decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


def _decimal_sum(first: Any, second: Any) -> Decimal | None:
    left, right = _to_decimal(first), _to_decimal(second)
    if left is None or right is None:
        return None
    return left + right


def _numbers_equal(first: Any, second: Any) -> bool:
    left, right = _to_decimal(first), _to_decimal(second)
    return left is not None and right is not None and abs(left - right) <= Decimal("0.00000001")


def _validated_ingestion_url(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DeadLetterRepairError("ingestion_url_invalid")
    try:
        parsed = urlparse(value.strip())
        port = parsed.port
    except ValueError as exc:
        raise DeadLetterRepairError("ingestion_url_invalid") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port is not None and not 1 <= port <= 65535
    ):
        raise DeadLetterRepairError("ingestion_url_invalid")
    return value.strip().rstrip("/")


def runtime_paths_from_environment(environment: Mapping[str, str]) -> tuple[Path, Path, Path, str]:
    instances = Path(environment.get("TRADEJOURNAL_INSTANCES_ROOT", "").strip() or DEFAULT_INSTANCES_ROOT)
    secrets = Path(environment.get("TRADEJOURNAL_SECRETS_ROOT", "").strip() or DEFAULT_SECRETS_ROOT)
    rollback = Path(environment.get("TRADEJOURNAL_ROLLBACK_ROOT", "").strip() or DEFAULT_ROLLBACK_ROOT)
    ingestion = _validated_ingestion_url(
        environment.get("TRADEJOURNAL_TRADING_INGESTION_URL", "")
    )
    return instances, secrets, rollback, ingestion
