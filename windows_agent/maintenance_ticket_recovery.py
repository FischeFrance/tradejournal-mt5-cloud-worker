"""Restart recovery by native ticket membership, without broker timezone guesses."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .provisioning.secret_store import WindowsSecretStore
from .state_store import atomic_json, read_json
from .worker.mql5_file_adapter import Mql5FileMt5Adapter
from .worker.native_mt5_runtime import NativeMt5Runtime

SHA = re.compile(r"^[0-9a-f]{64}$")
TICKET = re.compile(r"^[1-9][0-9]*$")
POINTER = "maintenance-recovery.json"


def _digest(path: Path) -> str:
    return NativeMt5Runtime._sha256(path)


def verify_ticket_reader() -> None:
    setting = NativeMt5Runtime._setting
    python = Path(setting("TRADEJOURNAL_MT5_HISTORY_READER_PYTHON"))
    manifest = Path(setting("TRADEJOURNAL_MT5_HISTORY_READER_MANIFEST"))
    expected = setting("TRADEJOURNAL_MT5_HISTORY_READER_MANIFEST_SHA256")
    if (not SHA.fullmatch(expected) or not python.is_file() or not manifest.is_file()
            or python.is_symlink() or manifest.is_symlink() or _digest(manifest) != expected):
        raise ValueError("history_ticket_reader_unavailable")
    root = python.parent.parent.resolve()
    files = read_json(manifest).get("files")
    if not isinstance(files, dict) or not files or len(files) > 10000:
        raise ValueError("history_ticket_reader_manifest_invalid")
    actual = {p.relative_to(root).as_posix() for p in root.rglob("*")
              if p.is_file() and p.suffix.casefold() in (".py", ".pyd", ".dll", ".exe")}
    if actual != set(files):
        raise ValueError("history_ticket_reader_code_changed")
    for name, digest in files.items():
        path = root / name
        if (not isinstance(digest, str) or not SHA.fullmatch(digest)
                or path.is_symlink() or root not in path.resolve().parents
                or _digest(path) != digest):
            raise ValueError("history_ticket_reader_code_changed")


def _tickets(value: Any) -> frozenset[str]:
    if (not isinstance(value, list) or len(value) > 1000000
            or any(not isinstance(ticket, str) or not TICKET.fullmatch(ticket) for ticket in value)
            or len(value) != len(set(value))):
        raise ValueError("maintenance_ticket_membership_invalid")
    return frozenset(value)


def load_baseline(root: Path, connection_id: str, login: int, server: str,
                  expected_sha256: str | None = None) -> dict:
    pointer = read_json(root / "state" / POINTER, {})
    digest = expected_sha256 or pointer.get("baseline_sha256")
    if not isinstance(digest, str) or not SHA.fullmatch(digest):
        raise ValueError("maintenance_ticket_baseline_missing")
    path = root / "state" / f"maintenance-baseline-{digest}.json"
    if path.is_symlink() or not path.is_file() or _digest(path) != digest:
        raise ValueError("maintenance_ticket_baseline_changed")
    value = read_json(path)
    identity = value.get("account_identity", {})
    captured = datetime.fromisoformat(str(value.get("captured_at")))
    now = datetime.now(timezone.utc)
    if (value.get("schema_version") != 1 or value.get("connection_id") != connection_id
            or str(identity.get("login")) != str(login)
            or str(identity.get("server")).casefold() != server.casefold()
            or captured.tzinfo is None or captured > now
            or captured < now - timedelta(hours=6)):
        raise ValueError("maintenance_ticket_baseline_invalid")
    _tickets(value.get("deal_tickets"))
    _tickets(value.get("order_tickets"))
    return value


def capture_baseline(root: Path, connection_id: str, login: int, server: str) -> dict:
    verify_ticket_reader()
    runtime = NativeMt5Runtime(root, connection_id)
    adapter = Mql5FileMt5Adapter(runtime.files, connection_id, login, server, runtime.state)
    adapter.verify_identity()
    if adapter.account_info().trade_allowed:
        raise ValueError("maintenance_ticket_investor_required")
    pids = runtime._running_terminal_pids()
    if len(pids) != 1:
        raise ValueError("maintenance_ticket_process_ambiguous")
    result = runtime.capture_history_tickets(pids[0])
    adapter.verify_identity()
    value = {key: result[key] for key in ("account_identity", "captured_at", "deal_tickets", "order_tickets")}
    value.update(schema_version=1, connection_id=connection_id)
    _tickets(value["deal_tickets"])
    _tickets(value["order_tickets"])
    content = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(content).hexdigest()
    path = runtime.state / f"maintenance-baseline-{digest}.json"
    temporary = path.with_suffix(".json.tmp")
    NativeMt5Runtime._write_text_durable(temporary, content.decode(), "utf-8")
    WindowsSecretStore.restrict_acl(temporary)
    from worker.atomic_file import durable_replace
    durable_replace(temporary, path)
    pointer = {"connection_id": connection_id, "baseline_sha256": digest,
               "from_date": value["captured_at"], "status": "prepared"}
    atomic_json(runtime.state / POINTER, pointer)
    WindowsSecretStore.restrict_acl(runtime.state / POINTER)
    load_baseline(root, connection_id, login, server, digest)
    return pointer


class TicketDeltaAdapter:
    """Retain the complete snapshot for handoff; publish only affected lifecycles."""
    history_time_basis = "native_ticket_membership_v1"
    history_snapshot_atomic = True

    def __init__(self, adapter: Mql5FileMt5Adapter, baseline: dict):
        self.adapter = adapter
        self.deal_tickets = _tickets(baseline["deal_tickets"])
        self.order_tickets = _tickets(baseline["order_tickets"])
        _, projected, ledger = adapter._historical_deal_snapshot()
        current = {str(row["ticket"]) for row in ledger}
        if not self.deal_tickets <= current:
            raise ValueError("maintenance_ticket_history_regressed")
        self.new_tickets = current - self.deal_tickets
        positions = {str(row.get("position_id")) for row in ledger
                     if str(row["ticket"]) in self.new_tickets
                     and str(row.get("position_id", "0")) not in ("", "0")}
        self.projected = tuple(row for row in projected if str(row.get("position_id")) in positions)
        self.accounting = tuple(row for row in ledger if str(row["ticket"]) in self.new_tickets)

    def __getattr__(self, name):
        return getattr(self.adapter, name)

    def history_deals(self, *_args):
        return tuple(row for row in self.projected if row.get("project_as_trade", True) is True)

    def history_orders(self, *_args):
        epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
        rows = self.adapter.history_orders(epoch, datetime.now(timezone.utc))
        current = {str(row["ticket"]) for row in rows}
        if not self.order_tickets <= current:
            raise ValueError("maintenance_ticket_orders_regressed")
        return tuple(row for row in rows if str(row["ticket"]) not in self.order_tickets)

    def history_accounting_deals(self, *_args):
        return self.accounting

    def history_balance_rows(self):
        return self.projected
