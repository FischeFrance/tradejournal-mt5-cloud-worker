"""One-shot, fail-closed recovery for one managed MT5 live-sync dead-letter.

This module is deliberately not part of the normal Agent loop.  It is invoked during a
maintenance window as LocalSystem, while the Windows service is stopped, and operates on one
record selected by digests rather than by putting sensitive MT5 identifiers on a command line.
"""

from __future__ import annotations

import copy
import ctypes
import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import urlparse

import requests

from worker.atomic_file import fsync_directory
from worker.event_outbox import EventOutbox, OutboxError, _restrict_file_access
from worker.event_sender import SendResult

from .event_supervisor import connection_sync_lock
from .provisioning.process_manager import ProcessManager
from .provisioning.secret_store import WindowsSecretStore
from .security import canonical_uuid
from .worker.history_balance import (
    CREDIT_DEAL_TYPE,
    KNOWN_BALANCE_DEAL_TYPES,
    TRADE_DEAL_TYPES,
    balance_ledger_snapshot_bytes,
    balance_ledger_snapshot_filename,
)
from .worker.mt5_broker_discovery import BrokerDiscoveryError, normalize_server_name


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
        "expected_ledger_snapshot_sha256",
    }
)
_TRADE_DEAL_TYPES = TRADE_DEAL_TYPES
_ACCOUNTING_DEAL_TYPES = frozenset(
    (KNOWN_BALANCE_DEAL_TYPES - TRADE_DEAL_TYPES) | {CREDIT_DEAL_TYPE}
)
_FILE_ATTRIBUTE_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_MOVEFILE_WRITE_THROUGH = 0x00000008
_MAX_JSON_BYTES = 512 * 1024 * 1024
_LEDGER_EVIDENCE_MAX_AGE = timedelta(minutes=30)
_LEDGER_EVIDENCE_MAX_FUTURE_SKEW = timedelta(minutes=5)


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
    expected_ledger_snapshot_sha256: str
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
        expected_ledger = document.get("expected_ledger_snapshot_sha256")
        count = document.get("expected_dead_letter_count")
        if (
            not isinstance(deployment_id, str)
            or not _DEPLOYMENT_ID.fullmatch(deployment_id)
            or not isinstance(expected_outbox, str)
            or not _SHA256.fullmatch(expected_outbox)
            or not isinstance(expected_record, str)
            or not _SHA256.fullmatch(expected_record)
            or not isinstance(expected_ledger, str)
            or not _SHA256.fullmatch(expected_ledger)
            or count != 1
            or isinstance(count, bool)
        ):
            raise DeadLetterRepairError("repair_request_invalid")
        return cls(
            connection_id=connection_id,
            deployment_id=deployment_id,
            expected_outbox_sha256=expected_outbox,
            expected_record_sha256=expected_record,
            expected_ledger_snapshot_sha256=expected_ledger,
        )


@dataclass(frozen=True)
class LedgerDecision:
    classification: str
    row: dict[str, Any]
    replacement_payload: dict[str, Any] | None
    ledger_snapshot_sha256: str


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
        _require_terminal_target_stopped(instance_root)
        completed = _completed_receipt_result(
            request,
            outbox_path=outbox_path,
            audit_path=audit_path,
            receipt_path=receipt_path,
            backup_path=backup_path,
        )
        if completed is not None:
            return completed
        existing_audit = _read_optional_json(audit_path)
        if existing_audit is None:
            outbox_bytes = _read_regular_bytes(outbox_path)
            if _sha256(outbox_bytes) != request.expected_outbox_sha256:
                raise DeadLetterRepairError("outbox_preimage_mismatch")
            outbox = EventOutbox(str(outbox_path))
            event_id, dead_record = _select_dead_letter(outbox, request)
            if outbox.pending_count() != 0:
                raise DeadLetterRepairError("outbox_not_quiescent")
            decision = _classify_from_coherent_ledger(
                instance_root,
                dead_record,
                request,
                require_fresh_evidence=True,
            )
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
            audited_decision = LedgerDecision(
                classification=str(audit_document["classification"]),
                row=copy.deepcopy(audit_document["ledger_record"]),
                replacement_payload=copy.deepcopy(audit_document.get("replacement_payload")),
                ledger_snapshot_sha256=str(
                    audit_document["ledger_snapshot_sha256"]
                ),
            )
            decision = _classify_from_coherent_ledger(
                instance_root,
                dead_record,
                request,
                require_fresh_evidence=False,
            )
            if _canonical_json_bytes(
                {
                    "classification": decision.classification,
                    "row": decision.row,
                    "replacement_payload": decision.replacement_payload,
                    "ledger_snapshot_sha256": decision.ledger_snapshot_sha256,
                }
            ) != _canonical_json_bytes(
                {
                    "classification": audited_decision.classification,
                    "row": audited_decision.row,
                    "replacement_payload": audited_decision.replacement_payload,
                    "ledger_snapshot_sha256": audited_decision.ledger_snapshot_sha256,
                }
            ):
                raise DeadLetterRepairError("resolution_audit_invalid")

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
                audit_path,
                audit_sha256,
            )
        else:  # pragma: no cover - validated classifiers cannot produce another value
            raise DeadLetterRepairError("ledger_classification_ambiguous")

        receipt_document = {
            "schema_version": 1,
            "classification": result["classification"],
            "record_sha256": request.expected_record_sha256,
            "ledger_snapshot_sha256": request.expected_ledger_snapshot_sha256,
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
            "ledger_snapshot_sha256": request.expected_ledger_snapshot_sha256,
            "audit_sha256": audit_sha256,
            "backup_sha256": request.expected_outbox_sha256,
            "outbox_after_sha256": _sha256(_read_regular_bytes(outbox_path)),
            "pending_count": refreshed.pending_count(),
            "dead_letter_count": refreshed.dead_letter_count(),
        }


def _require_terminal_target_stopped(instance_root: Path) -> None:
    try:
        pids = ProcessManager.find_under(instance_root / "terminal")
    except (OSError, RuntimeError, ValueError) as exc:
        raise DeadLetterRepairError("terminal_state_unavailable") from exc
    if pids:
        raise DeadLetterRepairError("terminal_must_be_stopped")


def _completed_receipt_result(
    request: RepairRequest,
    *,
    outbox_path: Path,
    audit_path: Path,
    receipt_path: Path,
    backup_path: Path,
) -> dict[str, Any] | None:
    receipt = _read_optional_json(receipt_path)
    if receipt is None:
        return None
    receipt_bytes = _read_regular_bytes(receipt_path)
    if _canonical_json_bytes(receipt) != receipt_bytes:
        raise DeadLetterRepairError("resolution_receipt_invalid")
    expected_keys = {
        "schema_version",
        "classification",
        "record_sha256",
        "ledger_snapshot_sha256",
        "audit_sha256",
        "action",
        "http_status",
        "outcome",
    }
    if (
        not isinstance(receipt, dict)
        or set(receipt) != expected_keys
        or receipt.get("schema_version") != 1
        or receipt.get("record_sha256") != request.expected_record_sha256
        or receipt.get("ledger_snapshot_sha256")
        != request.expected_ledger_snapshot_sha256
        or not isinstance(receipt.get("audit_sha256"), str)
        or not _SHA256.fullmatch(receipt["audit_sha256"])
        or receipt.get("classification")
        not in ("real_trade", "non_trading_accounting")
        or not isinstance(receipt.get("action"), str)
    ):
        raise DeadLetterRepairError("resolution_receipt_invalid")
    audit = _validate_existing_audit(_read_json(audit_path), request)
    audit_bytes = _read_regular_bytes(audit_path)
    if (
        _canonical_json_bytes(audit) != audit_bytes
        or _sha256(audit_bytes) != receipt["audit_sha256"]
    ):
        raise DeadLetterRepairError("resolution_receipt_invalid")
    if _sha256(_read_regular_bytes(backup_path)) != request.expected_outbox_sha256:
        raise DeadLetterRepairError("resolution_receipt_invalid")
    if audit.get("classification") != receipt["classification"]:
        raise DeadLetterRepairError("resolution_receipt_invalid")
    payload = audit["dead_letter_record"].get("payload")
    event_id = payload.get("event_id") if isinstance(payload, dict) else None
    if not isinstance(event_id, str):
        raise DeadLetterRepairError("resolution_receipt_invalid")
    outbox = EventOutbox(str(outbox_path))
    if event_id in outbox.pending_payloads() or event_id in outbox.dead_letters():
        raise DeadLetterRepairError("resolution_receipt_invalid")
    if receipt["classification"] == "real_trade":
        if (
            receipt["action"]
            not in ("corrected_event_delivered", "delivery_confirmed_after_recovery")
            or receipt.get("http_status") != 200
            or receipt.get("outcome") not in ("ok", "duplicate")
        ):
            raise DeadLetterRepairError("resolution_receipt_invalid")
    elif (
        receipt["action"] != "audit_archived_barrier_removed"
        or receipt.get("http_status") is not None
        or receipt.get("outcome") is not None
    ):
        raise DeadLetterRepairError("resolution_receipt_invalid")
    return {
        "schema_version": 1,
        "status": "resolved",
        "classification": receipt["classification"],
        "action": receipt["action"],
        "http_status": receipt.get("http_status"),
        "outcome": receipt.get("outcome"),
        "record_sha256": request.expected_record_sha256,
        "ledger_snapshot_sha256": request.expected_ledger_snapshot_sha256,
        "audit_sha256": receipt["audit_sha256"],
        "backup_sha256": request.expected_outbox_sha256,
        "outbox_after_sha256": _sha256(_read_regular_bytes(outbox_path)),
        "pending_count": outbox.pending_count(),
        "dead_letter_count": outbox.dead_letter_count(),
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
            outbox.resolve_audited_dead_letter(
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
    audit_path: Path,
    audit_sha256: str,
) -> dict[str, Any]:
    outbox = EventOutbox(str(outbox_path))
    pending = outbox.pending_payloads()
    dead = outbox.dead_letters()
    if replacement_payload["event_id"] != original_record["payload"]["event_id"]:
        raise DeadLetterRepairError("event_identity_changed")
    if event_id in dead:
        if EventOutbox.record_sha256(dead[event_id]) != request.expected_record_sha256:
            raise DeadLetterRepairError("dead_letter_changed")
        send_result = sender.send(copy.deepcopy(replacement_payload))
        _require_acknowledged(send_result, sender)
        try:
            EventOutbox(str(outbox_path)).resolve_audited_dead_letter(
                event_id,
                expected_record_sha256=request.expected_record_sha256,
                audit_path=str(audit_path),
                expected_audit_sha256=audit_sha256,
            )
        except OutboxError as exc:
            raise DeadLetterRepairError("dead_letter_resolution_failed") from exc
    elif event_id in pending:
        # This repair never stages corrected payloads in the general live queue. A matching
        # pending event therefore belongs to another writer or an older repair implementation.
        raise DeadLetterRepairError("dead_letter_state_conflict")
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

    final = EventOutbox(str(outbox_path))
    if event_id in final.pending_payloads() or event_id in final.dead_letters():
        raise DeadLetterRepairError("corrected_delivery_incomplete")
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


def _is_canonical_ticket_list(value: Any) -> bool:
    if (
        not isinstance(value, list)
        or any(
            not isinstance(ticket, str)
            or not re.fullmatch(r"[0-9]{1,32}", ticket)
            for ticket in value
        )
        or len(set(value)) != len(value)
    ):
        return False
    return value == sorted(value, key=lambda ticket: (len(ticket), ticket))


def _history_archive_tickets(document: dict[str, Any]) -> list[str]:
    trades = document.get("trades")
    if not isinstance(trades, list):
        raise DeadLetterRepairError("ledger_handoff_not_active")
    tickets: set[str] = set()
    for group in trades:
        events = group.get("events") if isinstance(group, dict) else None
        if not isinstance(events, list):
            raise DeadLetterRepairError("ledger_handoff_not_active")
        for event in events:
            ticket = event.get("native_deal_ticket") if isinstance(event, dict) else None
            ticket_text = str(ticket) if ticket is not None else ""
            if not re.fullmatch(r"[0-9]{1,32}", ticket_text):
                raise DeadLetterRepairError("ledger_handoff_not_active")
            tickets.add(ticket_text)
    return sorted(tickets, key=lambda ticket: (len(ticket), ticket))


def _classify_from_coherent_ledger(
    instance_root: Path,
    dead_record: dict[str, Any],
    request: RepairRequest,
    *,
    require_fresh_evidence: bool,
) -> LedgerDecision:
    report = _read_json(instance_root / "data" / "history-balance-backfill.json")
    anchor = report.get("anchor") if isinstance(report, dict) else None
    binding = report.get("ledger_snapshot") if isinstance(report, dict) else None
    if (
        not isinstance(report, dict)
        or report.get("schema_version") != 1
        or report.get("source") != "mt5_historical_ledger"
        or not isinstance(anchor, dict)
        or anchor.get("coherent") is not True
    ):
        raise DeadLetterRepairError("ledger_not_coherent")
    if (
        not isinstance(binding, dict)
        or set(binding)
        != {
            "job_id",
            "filename",
            "sha256",
            "deal_count",
            "history_mode",
            "through",
            "captured_at_utc",
        }
        or not isinstance(binding.get("job_id"), str)
        or binding.get("filename")
        != balance_ledger_snapshot_filename(
            binding.get("job_id", ""), str(binding.get("sha256", ""))
        )
        or not isinstance(binding.get("sha256"), str)
        or not _SHA256.fullmatch(binding["sha256"])
        or binding.get("history_mode") != "all_available"
        or not isinstance(binding.get("deal_count"), int)
        or isinstance(binding.get("deal_count"), bool)
        or binding["deal_count"] <= 0
        or not isinstance(binding.get("through"), str)
        or not isinstance(binding.get("captured_at_utc"), str)
    ):
        raise DeadLetterRepairError("ledger_snapshot_binding_invalid")
    if binding["sha256"] != request.expected_ledger_snapshot_sha256:
        raise DeadLetterRepairError("ledger_snapshot_preimage_mismatch")

    active = _read_json(instance_root / "state" / "history-live-handoff.json")
    pending = _read_json(instance_root / "state" / "history-handoff-pending.json")
    active_archive_sha256 = (
        active.get("history_document_sha256") if isinstance(active, dict) else None
    )
    active_sequence = active.get("anchor_sequence") if isinstance(active, dict) else None
    pending_sequence = pending.get("anchor_sequence") if isinstance(pending, dict) else None
    active_archived = active.get("archived_deal_tickets") if isinstance(active, dict) else None
    pending_archived = pending.get("archived_deal_tickets") if isinstance(pending, dict) else None
    active_imported = active.get("imported_deal_tickets") if isinstance(active, dict) else None
    pending_imported = pending.get("imported_deal_tickets") if isinstance(pending, dict) else None
    expected_archive_name = (
        "history-import-"
        + hashlib.sha256(binding["job_id"].encode("utf-8")).hexdigest()[:16]
        + ".json"
    )
    if (
        not isinstance(active, dict)
        or active.get("schema_version") != 2
        or active.get("connection_id") != request.connection_id
        or active.get("job_id") != binding["job_id"]
        or active.get("history_mode") != "all_available"
        or active.get("ledger_evidence") != binding
        or not isinstance(active_archive_sha256, str)
        or not _SHA256.fullmatch(active_archive_sha256)
        or not isinstance(pending, dict)
        or pending.get("schema_version") != 1
        or pending.get("connection_id") != request.connection_id
        or pending.get("job_id") != binding["job_id"]
        or pending.get("history_mode") != "all_available"
        or pending.get("ledger_evidence") != binding
        or pending.get("history_document_sha256") != active_archive_sha256
        or pending.get("history_document") != expected_archive_name
        or not isinstance(active_sequence, int)
        or isinstance(active_sequence, bool)
        or active_sequence < 0
        or pending_sequence != active_sequence
        or not _is_canonical_ticket_list(active_archived)
        or pending_archived != active_archived
        or not _is_canonical_ticket_list(active_imported)
        or pending_imported != active_imported
        or not set(active_imported).issubset(set(active_archived))
        or not isinstance(pending.get("history_counts"), dict)
        or pending["history_counts"].get("accounting_deals")
        != binding["deal_count"]
    ):
        raise DeadLetterRepairError("ledger_handoff_not_active")

    archive_path = instance_root / "state" / expected_archive_name
    try:
        archive_bytes = _read_regular_bytes(archive_path)
    except DeadLetterRepairError as exc:
        raise DeadLetterRepairError("ledger_handoff_not_active") from exc
    if _sha256(archive_bytes) != active_archive_sha256:
        raise DeadLetterRepairError("ledger_handoff_not_active")
    try:
        archive = json.loads(archive_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DeadLetterRepairError("ledger_handoff_not_active") from exc
    if (
        not isinstance(archive, dict)
        or archive.get("schema_version") != 1
        or archive.get("job_id") != binding["job_id"]
        or archive.get("connection_id") != request.connection_id
        or archive.get("history_mode") != "all_available"
        or _history_archive_tickets(archive) != active_imported
        or pending.get("history_document_payload") != archive
    ):
        raise DeadLetterRepairError("ledger_handoff_not_active")

    snapshot_path = instance_root / "data" / binding["filename"]
    try:
        snapshot_bytes = _read_regular_bytes(snapshot_path)
    except DeadLetterRepairError as exc:
        raise DeadLetterRepairError("ledger_snapshot_unavailable") from exc
    if _sha256(snapshot_bytes) != binding["sha256"]:
        raise DeadLetterRepairError("ledger_snapshot_digest_mismatch")
    try:
        snapshot = json.loads(snapshot_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DeadLetterRepairError("ledger_snapshot_invalid") from exc
    if not isinstance(snapshot, dict):
        raise DeadLetterRepairError("ledger_snapshot_invalid")
    try:
        canonical_snapshot = balance_ledger_snapshot_bytes(snapshot)
    except (TypeError, ValueError) as exc:
        raise DeadLetterRepairError("ledger_snapshot_invalid") from exc
    if canonical_snapshot != snapshot_bytes:
        raise DeadLetterRepairError("ledger_snapshot_noncanonical")
    if (
        set(snapshot)
        != {
            "schema_version",
            "job_id",
            "connection_id",
            "account_number",
            "server",
            "history_mode",
            "through",
            "captured_at_utc",
            "deal_count",
            "anchor",
            "rows",
        }
        or snapshot.get("schema_version") != 1
        or snapshot.get("job_id") != binding["job_id"]
        or snapshot.get("history_mode") != "all_available"
        or snapshot.get("through") != binding["through"]
        or snapshot.get("captured_at_utc") != binding["captured_at_utc"]
        or snapshot.get("deal_count") != binding["deal_count"]
        or snapshot.get("anchor") != anchor
        or not isinstance(snapshot.get("rows"), list)
        or any(not isinstance(row, dict) for row in snapshot["rows"])
        or len(snapshot["rows"]) != binding["deal_count"]
    ):
        raise DeadLetterRepairError("ledger_snapshot_invalid")

    payload = dead_record["payload"]
    _require_ledger_identity(request, report, snapshot, payload)
    deal_count = anchor.get("deal_count")
    if (
        not isinstance(deal_count, int)
        or isinstance(deal_count, bool)
        or deal_count <= 0
        or deal_count != binding["deal_count"]
        or anchor.get("order_basis") != "mt5_history_index_v1"
    ):
        raise DeadLetterRepairError("ledger_not_coherent")
    event_time = _parse_aware_datetime(payload.get("event_time"))
    # ``through`` is worker UTC acquisition metadata, while MT5 DEAL_TIME and anchor.as_of use
    # unresolved broker-server time despite their legacy Z suffix. Never compare those clocks.
    _parse_aware_datetime(snapshot.get("through"))
    captured_at_utc = _parse_aware_datetime(snapshot.get("captured_at_utc"))
    if captured_at_utc.utcoffset() != timedelta(0):
        raise DeadLetterRepairError("ledger_time_invalid")
    if require_fresh_evidence:
        now_utc = datetime.now(timezone.utc)
        if (
            captured_at_utc > now_utc + _LEDGER_EVIDENCE_MAX_FUTURE_SKEW
            or now_utc - captured_at_utc > _LEDGER_EVIDENCE_MAX_AGE
        ):
            raise DeadLetterRepairError("ledger_snapshot_stale")
    anchor_as_of = _parse_aware_datetime(anchor.get("as_of"))
    if anchor_as_of < event_time:
        raise DeadLetterRepairError("ledger_not_fresh")

    rows = _validated_snapshot_rows(snapshot["rows"], anchor)
    snapshot_tickets = sorted(
        (str(row["ticket"]) for row in rows),
        key=lambda value: (len(value), value),
    )
    if snapshot_tickets != active_archived:
        raise DeadLetterRepairError("ledger_handoff_not_active")
    row = _match_ledger_row(payload, rows)
    deal_type = row.get("deal_type")
    position_id = row.get("position_id")
    order_id = row.get("order_id")
    entry = row.get("entry")
    symbol = row.get("symbol")
    if not isinstance(deal_type, int) or isinstance(deal_type, bool):
        raise DeadLetterRepairError("ledger_classification_ambiguous")
    if deal_type in _ACCOUNTING_DEAL_TYPES:
        if _has_strict_accounting_shape(row):
            return LedgerDecision(
                "non_trading_accounting", row, None, binding["sha256"]
            )
        raise DeadLetterRepairError("ledger_classification_ambiguous")
    if deal_type not in _TRADE_DEAL_TYPES:
        raise DeadLetterRepairError("ledger_classification_ambiguous")
    expected_direction = "buy" if deal_type == 0 else "sell"
    if (
        not isinstance(position_id, str)
        or not position_id.isdecimal()
        or int(position_id) <= 0
        or not isinstance(order_id, str)
        or not order_id.isdecimal()
        or int(order_id) <= 0
        or not isinstance(entry, str)
        or entry.strip().upper() != "IN"
        or not isinstance(symbol, str)
        or not symbol.strip()
        or str(row.get("direction", "")).strip().lower() != expected_direction
        or not _number_is_positive(row.get("volume"))
        or not _number_is_positive(row.get("price"))
    ):
        # BUY/SELL with position zero is not silently treated as accounting.  Any malformed
        # trade shape (and every unknown future MT5 enum) remains a hard, non-mutating stop.
        raise DeadLetterRepairError("ledger_classification_ambiguous")
    replacement = copy.deepcopy(payload)
    replacement.update(
        {
            "symbol": symbol.strip(),
            "direction": expected_direction,
            "external_trade_id": position_id,
            "native_deal_ticket": str(row.get("ticket")),
            "time_basis": "broker_server_unresolved",
        }
    )
    if row.get("time_msc") is not None:
        replacement["time_msc"] = row["time_msc"]
    if replacement.get("event_id") != payload.get("event_id"):
        raise DeadLetterRepairError("event_identity_changed")
    return LedgerDecision("real_trade", row, replacement, binding["sha256"])


def _require_ledger_identity(
    request: RepairRequest,
    report: dict[str, Any],
    snapshot: dict[str, Any],
    payload: dict[str, Any],
) -> None:
    try:
        report_connection_id = canonical_uuid(report.get("connection_id"))
        snapshot_connection_id = canonical_uuid(snapshot.get("connection_id"))
        report_account = _normalize_account_number(report.get("account_number"))
        snapshot_account = _normalize_account_number(snapshot.get("account_number"))
        payload_account = _normalize_account_number(payload.get("account_number"))
        report_server = _normalize_server(report.get("server"))
        snapshot_server = _normalize_server(snapshot.get("server"))
        payload_server = _normalize_server(payload.get("server"))
    except (TypeError, ValueError, BrokerDiscoveryError) as exc:
        raise DeadLetterRepairError("ledger_identity_mismatch") from exc
    if (
        report_connection_id != request.connection_id
        or snapshot_connection_id != request.connection_id
        or snapshot_connection_id != report_connection_id
        or report_account != payload_account
        or snapshot_account != payload_account
        or snapshot_account != report_account
        or report_server != payload_server
        or snapshot_server != payload_server
        or snapshot_server != report_server
    ):
        raise DeadLetterRepairError("ledger_identity_mismatch")


def _normalize_account_number(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("invalid account number")
    normalized = value.strip()
    if not normalized or any(character.isspace() for character in normalized):
        raise ValueError("invalid account number")
    return normalized


def _normalize_server(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("invalid server")
    normalized = normalize_server_name(value)
    if not normalized:
        raise ValueError("invalid server")
    return normalized


def _has_strict_accounting_shape(row: dict[str, Any]) -> bool:
    return (
        row.get("position_id") == "0"
        and row.get("order_id") == "0"
        and isinstance(row.get("symbol"), str)
        and not row["symbol"].strip()
        and isinstance(row.get("entry"), str)
        and row["entry"].strip().upper() == "IN"
        and _number_is_zero(row.get("volume"))
        and _number_is_zero(row.get("price"))
    )


def _number_is_zero(value: Any) -> bool:
    parsed = _to_decimal(value)
    return parsed is not None and parsed == 0


def _number_is_positive(value: Any) -> bool:
    parsed = _to_decimal(value)
    return parsed is not None and parsed > 0


def _validated_snapshot_rows(
    raw_rows: list[dict[str, Any]], anchor: dict[str, Any]
) -> tuple[dict[str, Any], ...]:
    rows = tuple(copy.deepcopy(row) for row in raw_rows)
    tickets: set[str] = set()
    for expected_index, row in enumerate(rows):
        ticket = str(row.get("ticket", "")).strip()
        if (
            not ticket.isdecimal()
            or int(ticket) <= 0
            or ticket in tickets
            or row.get("history_index") != expected_index
        ):
            raise DeadLetterRepairError("ledger_snapshot_invalid")
        tickets.add(ticket)
    last = rows[-1] if rows else {}
    if (
        str(anchor.get("last_deal_ticket", "0"))
        != str(last.get("ticket", "0"))
        or anchor.get("last_deal_time_msc", 0) != last.get("time_msc", 0)
    ):
        raise DeadLetterRepairError("ledger_not_coherent")
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
        "ledger_snapshot_sha256": decision.ledger_snapshot_sha256,
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
        or document.get("ledger_snapshot_sha256")
        != request.expected_ledger_snapshot_sha256
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
    if not _DEPLOYMENT_ID.fullmatch(request.deployment_id):
        raise DeadLetterRepairError("rollback_path_invalid")
    try:
        connection_id = canonical_uuid(request.connection_id)
    except (TypeError, ValueError) as exc:
        raise DeadLetterRepairError("rollback_path_invalid") from exc
    base = _absolute_path(root)
    target = base / request.deployment_id / connection_id
    try:
        target.relative_to(base)
    except ValueError as exc:
        raise DeadLetterRepairError("rollback_path_invalid") from exc
    _reject_reparse_chain(target, "rollback_path_invalid")
    return target / "live-outbox.before.json"


def _validated_instance_root(root: Path, connection_id: str) -> Path:
    try:
        canonical = canonical_uuid(connection_id)
    except (TypeError, ValueError) as exc:
        raise DeadLetterRepairError("instance_path_invalid") from exc
    base = _absolute_path(root)
    instance = base / canonical
    try:
        instance.relative_to(base)
    except ValueError as exc:  # pragma: no cover - canonical UUID has no separators
        raise DeadLetterRepairError("instance_path_invalid") from exc
    _reject_reparse_chain(instance, "instance_path_invalid")
    try:
        metadata = instance.lstat()
    except OSError as exc:
        raise DeadLetterRepairError("instance_path_invalid") from exc
    if _stat_is_reparse(instance, metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise DeadLetterRepairError("instance_path_invalid")
    return instance


def _absolute_path(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _reject_reparse_chain(path: Path, error_code: str) -> None:
    current = _absolute_path(path)
    chain: list[Path] = []
    while True:
        chain.append(current)
        if current.parent == current:
            break
        current = current.parent
    for candidate in reversed(chain):
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise DeadLetterRepairError(error_code) from exc
        if _stat_is_reparse(candidate, metadata):
            raise DeadLetterRepairError(error_code)


def _stat_is_reparse(path: Path, metadata: os.stat_result) -> bool:
    return stat.S_ISLNK(metadata.st_mode) or bool(
        _windows_file_attributes(path, metadata) & _FILE_ATTRIBUTE_REPARSE_POINT
    )


def _windows_file_attributes(path: Path, metadata: os.stat_result) -> int:
    del path  # Kept as an argument so tests can model a Windows reparse point by path.
    return int(getattr(metadata, "st_file_attributes", 0))


def _path_exists_without_follow(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise DeadLetterRepairError("maintenance_path_invalid") from exc
    return True


def _write_private_once(path: Path, payload: bytes) -> None:
    path = _absolute_path(path)
    _prepare_private_parent(path.parent)
    temporary = _private_temporary_path(path, payload)
    if _path_exists_without_follow(path):
        if _read_regular_bytes(path) != payload:
            raise DeadLetterRepairError("immutable_maintenance_artifact_conflict")
        _remove_orphan_temporary(temporary)
        return
    _stage_private_temporary(temporary, payload)
    try:
        _publish_no_replace(temporary, path)
    except FileExistsError:
        if _read_regular_bytes(path) != payload:
            raise DeadLetterRepairError("immutable_maintenance_artifact_conflict")
        _remove_orphan_temporary(temporary)
    except DeadLetterRepairError:
        raise
    except OSError as exc:
        # MoveFileExW reports ERROR_ALREADY_EXISTS as an OSError on some Python builds.
        if _path_exists_without_follow(path):
            if _read_regular_bytes(path) != payload:
                raise DeadLetterRepairError("immutable_maintenance_artifact_conflict") from exc
            _remove_orphan_temporary(temporary)
            return
        raise DeadLetterRepairError("maintenance_artifact_write_failed") from exc
    if _read_regular_bytes(path) != payload:
        raise DeadLetterRepairError("maintenance_artifact_write_failed")


def _prepare_private_parent(parent: Path) -> None:
    _reject_reparse_chain(parent, "maintenance_path_invalid")
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise DeadLetterRepairError("maintenance_path_invalid") from exc
    _reject_reparse_chain(parent, "maintenance_path_invalid")
    try:
        metadata = parent.lstat()
    except OSError as exc:
        raise DeadLetterRepairError("maintenance_path_invalid") from exc
    if _stat_is_reparse(parent, metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise DeadLetterRepairError("maintenance_path_invalid")


def _private_temporary_path(path: Path, payload: bytes) -> Path:
    return path.with_name(f".{path.name}.{_sha256(payload)}.tmp")


def _stage_private_temporary(path: Path, payload: bytes) -> None:
    _reject_reparse_chain(path, "maintenance_path_invalid")
    existed = _path_exists_without_follow(path)
    previous: os.stat_result | None = None
    if existed:
        try:
            previous = path.lstat()
        except OSError as exc:
            raise DeadLetterRepairError("maintenance_path_invalid") from exc
        if _stat_is_reparse(path, previous) or not stat.S_ISREG(previous.st_mode):
            raise DeadLetterRepairError("maintenance_path_invalid")
        if os.name != "nt" and (
            stat.S_IMODE(previous.st_mode) != 0o600
            or hasattr(os, "geteuid") and previous.st_uid != os.geteuid()
        ):
            raise DeadLetterRepairError("maintenance_path_invalid")
    flags = (
        os.O_WRONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0)
    )
    if not existed:
        flags |= os.O_CREAT | os.O_EXCL
    descriptor = -1
    try:
        descriptor = os.open(path, flags, 0o600)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or _windows_file_attributes(path, opened) & _FILE_ATTRIBUTE_REPARSE_POINT
            or previous is not None
            and (opened.st_dev, opened.st_ino) != (previous.st_dev, previous.st_ino)
        ):
            raise DeadLetterRepairError("maintenance_path_invalid")
        _restrict_file_access(str(path), descriptor)
        os.ftruncate(descriptor, 0)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:  # pragma: no cover - os.write raises instead in normal operation
                raise OSError("short write")
            view = view[written:]
        os.fsync(descriptor)
    except DeadLetterRepairError:
        raise
    except (OSError, OutboxError) as exc:
        raise DeadLetterRepairError("maintenance_artifact_write_failed") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    fsync_directory(path.parent)


def _publish_no_replace(source: Path, destination: Path) -> None:
    if os.name == "nt":
        from ctypes import wintypes

        move_file_ex = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
        move_file_ex.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD)
        move_file_ex.restype = wintypes.BOOL
        if not move_file_ex(
            os.path.abspath(os.fspath(source)),
            os.path.abspath(os.fspath(destination)),
            _MOVEFILE_WRITE_THROUGH,
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return
    os.link(source, destination, follow_symlinks=False)
    fsync_directory(destination.parent)
    source.unlink()
    fsync_directory(destination.parent)


def _remove_orphan_temporary(path: Path) -> None:
    if not _path_exists_without_follow(path):
        return
    _reject_reparse_chain(path, "maintenance_path_invalid")
    try:
        metadata = path.lstat()
        if _stat_is_reparse(path, metadata) or not stat.S_ISREG(metadata.st_mode):
            raise DeadLetterRepairError("maintenance_path_invalid")
        path.unlink()
        fsync_directory(path.parent)
    except DeadLetterRepairError:
        raise
    except OSError as exc:
        raise DeadLetterRepairError("maintenance_artifact_write_failed") from exc


def write_private_result(path: Path, result: Mapping[str, Any]) -> None:
    payload = _canonical_json_bytes(dict(result))
    if _path_exists_without_follow(_absolute_path(path)):
        raise DeadLetterRepairError("result_path_exists")
    _write_private_once(path, payload)


def _read_optional_json(path: Path) -> Any | None:
    path = _absolute_path(path)
    _reject_reparse_chain(path, "maintenance_path_invalid")
    if not _path_exists_without_follow(path):
        return None
    return _read_json(path)


def _read_json(path: Path, *, max_bytes: int = _MAX_JSON_BYTES) -> Any:
    try:
        return json.loads(_read_regular_bytes(path, max_bytes=max_bytes).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DeadLetterRepairError("json_artifact_invalid") from exc


def _read_regular_bytes(path: Path, *, max_bytes: int = _MAX_JSON_BYTES) -> bytes:
    path = _absolute_path(path)
    descriptor = -1
    try:
        _reject_reparse_chain(path, "maintenance_file_invalid")
        path_stat = path.lstat()
        if (
            _stat_is_reparse(path, path_stat)
            or not stat.S_ISREG(path_stat.st_mode)
            or path_stat.st_size > max_bytes
        ):
            raise DeadLetterRepairError("maintenance_file_invalid")
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_BINARY", 0)
        )
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or _windows_file_attributes(path, opened) & _FILE_ATTRIBUTE_REPARSE_POINT
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
