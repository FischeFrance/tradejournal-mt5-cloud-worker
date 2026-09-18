from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from worker.event_outbox import EventOutbox
from worker.event_sender import SendResult
from windows_agent import deadletter_repair as repair_module
from windows_agent.deadletter_repair import (
    DeadLetterRepairError,
    RepairRequest,
    repair_live_dead_letter,
)
from windows_agent.worker.history_balance import (
    balance_ledger_snapshot_bytes,
    balance_ledger_snapshot_filename,
)


CID = "0973451f-9b9c-4da7-ac53-8de1bc5b5949"
JOB_ID = "11111111-1111-4111-8111-111111111111"


@pytest.fixture(autouse=True)
def _model_stopped_terminal(monkeypatch):
    monkeypatch.setattr(
        repair_module.ProcessManager,
        "find_under",
        staticmethod(lambda _root: []),
    )


class _PermanentReject:
    def send(self, _payload):
        return SendResult(
            status="failed",
            http_status=422,
            error="rejected_by_api",
            attempts=1,
            failure_type="permanent",
        )


class _RepairSender:
    def __init__(self, outcome="ok", result=None):
        self.http_status = 200 if result is None else result.http_status
        self.outcome = outcome
        self.result = result or SendResult(status="sent", http_status=200, attempts=1)
        self.payloads = []

    def send(self, payload):
        self.payloads.append(payload)
        return self.result


class _EnqueueSuccessorOnSend(_RepairSender):
    def __init__(self, outbox_path, successor):
        super().__init__(outcome="ok")
        self.outbox_path = outbox_path
        self.successor = successor

    def send(self, payload):
        result = super().send(payload)
        EventOutbox(str(self.outbox_path)).enqueue_many([self.successor])
        return result


def _payload(**overrides):
    value = {
        "event_id": "mt5-sensitive-account-trade_opened-sensitive-digest",
        "event_type": "trade_opened",
        "platform": "mt5",
        "account_number": "sensitive-account",
        "server": "Sensitive Broker",
        "external_trade_id": "7001",
        "symbol": "",
        "direction": "buy",
        "volume": 0.3,
        "open_price": 1.2345,
        "profit": 0.0,
        "commission": -1.36,
        "swap": 0.0,
        "event_time": "2026-09-17T15:05:15+00:00",
        "open_time": "2026-09-17T15:05:15+00:00",
    }
    value.update(overrides)
    return value


def _ledger_row(**overrides):
    value = {
        "history_index": 0,
        "ticket": "9001",
        "position_id": "7001",
        "order_id": "8001",
        "symbol": "EURUSD",
        "deal_type": 0,
        "direction": "buy",
        "entry": "IN",
        "volume": 0.3,
        "price": 1.2345,
        "profit": 0.0,
        "commission": -1.0,
        "fee": -0.36,
        "swap": 0.0,
        "time_msc": 1789657515000,
        "time": "2026-09-17T15:05:15+00:00",
    }
    value.update(overrides)
    return value


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


def _write_ledger_snapshot(path: Path, value) -> str:
    payload = balance_ledger_snapshot_bytes(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return hashlib.sha256(payload).hexdigest()


def _fixture(tmp_path: Path, *, row=None, payload=None):
    instances = tmp_path / "instances"
    instance = instances / CID
    state = instance / "state"
    data = instance / "data"
    state.mkdir(parents=True)
    data.mkdir(parents=True)
    (instance / "terminal").mkdir()
    payload = payload or _payload()
    row = row or _ledger_row()
    outbox_path = state / "live-outbox.json"
    outbox = EventOutbox(str(outbox_path))
    outbox.enqueue_many([payload])
    result = outbox.drain(_PermanentReject())
    assert result.dead_lettered == 1
    record = outbox.dead_letters()[payload["event_id"]]
    record_digest = EventOutbox.record_sha256(record)
    outbox_digest = hashlib.sha256(outbox_path.read_bytes()).hexdigest()
    _write_json(
        state / "history.json",
        {
            "through": "2026-09-18T12:00:00+00:00",
            "orders": 0,
            "deals": 1,
            "accounting_deals": 1,
        },
    )
    captured_at_utc = datetime.now(timezone.utc).isoformat()
    through = captured_at_utc
    anchor = {
        "coherent": True,
        "order_basis": "mt5_history_index_v1",
        "deal_count": 1,
        "last_deal_ticket": str(row["ticket"]),
        "last_deal_time_msc": row["time_msc"],
        "balance": 10000.0,
        "credit": 0.0,
        "as_of": "2026-09-18T15:06:00+00:00",
    }
    snapshot = {
        "schema_version": 1,
        "job_id": JOB_ID,
        "connection_id": CID,
        "account_number": "sensitive-account",
        "server": "Sensitive Broker",
        "history_mode": "all_available",
        "through": through,
        "captured_at_utc": captured_at_utc,
        "deal_count": 1,
        "anchor": anchor,
        "rows": [row],
    }
    snapshot_digest = hashlib.sha256(balance_ledger_snapshot_bytes(snapshot)).hexdigest()
    snapshot_filename = balance_ledger_snapshot_filename(JOB_ID, snapshot_digest)
    snapshot_path = data / snapshot_filename
    assert _write_ledger_snapshot(snapshot_path, snapshot) == snapshot_digest
    binding = {
        "job_id": JOB_ID,
        "filename": snapshot_filename,
        "sha256": snapshot_digest,
        "deal_count": 1,
        "history_mode": "all_available",
        "through": through,
        "captured_at_utc": captured_at_utc,
    }
    report = {
        "schema_version": 1,
        "connection_id": CID,
        "account_number": "sensitive-account",
        "server": "Sensitive Broker",
        "source": "mt5_historical_ledger",
        "anchor": anchor,
        "ledger_snapshot": binding,
        "entries": {},
    }
    _write_json(data / "history-balance-backfill.json", report)
    imported_tickets = (
        [str(row["ticket"])]
        if row.get("deal_type") in (0, 1)
        and str(row.get("position_id", "0")) != "0"
        and str(row.get("symbol", "")).strip()
        else []
    )
    archive = {
        "schema_version": 1,
        "job_id": JOB_ID,
        "connection_id": CID,
        "account_number": "sensitive-account",
        "server": "Sensitive Broker",
        "history_mode": "all_available",
        "from_date": None,
        "trades": (
            [
                {
                    "external_trade_id": str(row.get("position_id", "0")),
                    "events": [
                        {
                            "event_id": "fixture-history-event",
                            "native_deal_ticket": str(row["ticket"]),
                        }
                    ],
                }
            ]
            if imported_tickets
            else []
        ),
    }
    archive_key = hashlib.sha256(JOB_ID.encode("utf-8")).hexdigest()[:16]
    archive_path = state / f"history-import-{archive_key}.json"
    _write_json(archive_path, archive)
    archive_sha256 = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    handoff_common = {
        "schema_version": 1,
        "job_id": JOB_ID,
        "connection_id": CID,
        "history_mode": "all_available",
        "history_document_sha256": archive_sha256,
        "ledger_evidence": binding,
        "anchor_sequence": 7,
        "archived_deal_tickets": [str(row["ticket"])],
        "imported_deal_tickets": imported_tickets,
    }
    _write_json(
        state / "history-handoff-pending.json",
        {
            **handoff_common,
            "history_document": archive_path.name,
            "history_document_payload": archive,
            "history_counts": {
                "orders": 0,
                "deals": len(imported_tickets),
                "accounting_deals": 1,
            },
        },
    )
    _write_json(
        state / "history-live-handoff.json",
        {**handoff_common, "schema_version": 2},
    )
    # Deliberately stale diagnostics must never participate in repair classification.
    (data / "history-accounting.jsonl").write_text(
        json.dumps(
            {
                "kind": "accounting_deals",
                "record": {**row, "deal_type": 13, "symbol": ""},
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    request = RepairRequest(
        connection_id=CID,
        deployment_id="test-deployment",
        expected_outbox_sha256=outbox_digest,
        expected_record_sha256=record_digest,
        expected_ledger_snapshot_sha256=snapshot_digest,
    )
    return {
        "instances": instances,
        "instance": instance,
        "rollback": tmp_path / "rollback",
        "request": request,
        "payload": payload,
        "record": record,
        "outbox_path": outbox_path,
        "snapshot_path": snapshot_path,
        "report_path": data / "history-balance-backfill.json",
        "archive_path": archive_path,
    }


def _replace_snapshot(env, rows) -> None:
    snapshot = json.loads(env["snapshot_path"].read_text(encoding="utf-8"))
    snapshot["rows"] = rows
    snapshot["deal_count"] = len(rows)
    snapshot["anchor"]["deal_count"] = len(rows)
    last = rows[-1] if rows else {}
    snapshot["anchor"]["last_deal_ticket"] = str(last.get("ticket", "0"))
    snapshot["anchor"]["last_deal_time_msc"] = last.get("time_msc", 0)
    _bind_snapshot(env, snapshot)


def _bind_snapshot(env, snapshot, *, update_boundary=True) -> None:
    """Install a new intentional evidence preimage without mutating the old bundle."""

    snapshot_bytes = balance_ledger_snapshot_bytes(snapshot)
    digest = hashlib.sha256(snapshot_bytes).hexdigest()
    filename = balance_ledger_snapshot_filename(snapshot["job_id"], digest)
    snapshot_path = env["instance"] / "data" / filename
    assert _write_ledger_snapshot(snapshot_path, snapshot) == digest
    binding = {
        "job_id": snapshot["job_id"],
        "filename": filename,
        "sha256": digest,
        "deal_count": snapshot["deal_count"],
        "history_mode": snapshot["history_mode"],
        "through": snapshot["through"],
        "captured_at_utc": snapshot["captured_at_utc"],
    }
    report = json.loads(env["report_path"].read_text(encoding="utf-8"))
    report["anchor"] = snapshot["anchor"]
    report["ledger_snapshot"] = binding
    report["account_number"] = snapshot["account_number"]
    report["server"] = snapshot["server"]
    _write_json(env["report_path"], report)
    for name in ("history-handoff-pending.json", "history-live-handoff.json"):
        path = env["instance"] / "state" / name
        handoff = json.loads(path.read_text(encoding="utf-8"))
        handoff["ledger_evidence"] = binding
        if update_boundary:
            tickets = sorted(
                (str(row["ticket"]) for row in snapshot["rows"]),
                key=lambda value: (len(value), value),
            )
            handoff["archived_deal_tickets"] = tickets
            if name == "history-handoff-pending.json":
                handoff["history_counts"]["accounting_deals"] = len(tickets)
        _write_json(path, handoff)
    env["request"] = replace(
        env["request"], expected_ledger_snapshot_sha256=digest
    )
    env["snapshot_path"] = snapshot_path


def _assert_private(path: Path) -> None:
    assert path.is_file()
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_real_trade_is_corrected_with_same_event_id_and_acknowledged(tmp_path):
    env = _fixture(tmp_path)
    sender = _RepairSender(outcome="ok")

    result = repair_live_dead_letter(
        env["request"],
        instances_root=env["instances"],
        secrets_root=tmp_path / "secrets",
        rollback_root=env["rollback"],
        ingestion_url="https://example.supabase.co/trading-mt5-events",
        sender=sender,
    )

    assert result["status"] == "resolved"
    assert result["classification"] == "real_trade"
    assert result["http_status"] == 200
    assert result["outcome"] == "ok"
    assert result["pending_count"] == result["dead_letter_count"] == 0
    assert len(sender.payloads) == 1
    corrected = sender.payloads[0]
    assert corrected["event_id"] == env["payload"]["event_id"]
    assert corrected["symbol"] == "EURUSD"
    assert corrected["external_trade_id"] == "7001"
    assert corrected["native_deal_ticket"] == "9001"
    assert corrected["time_msc"] == 1789657515000
    assert EventOutbox(str(env["outbox_path"])).dead_letter_count() == 0

    backup = env["rollback"] / "test-deployment" / CID / "live-outbox.before.json"
    audit = env["instance"] / "state" / "dead-letter-resolutions" / (
        env["request"].expected_record_sha256 + ".json"
    )
    receipt = audit.with_name(env["request"].expected_record_sha256 + ".receipt.json")
    for path in (backup, audit, receipt):
        _assert_private(path)
    assert hashlib.sha256(backup.read_bytes()).hexdigest() == env["request"].expected_outbox_sha256

    # The operator-facing result contains digests and the allowlisted acknowledgement only.
    visible = json.dumps(result)
    assert "sensitive-account" not in visible
    assert "Sensitive Broker" not in visible
    assert "EURUSD" not in visible
    assert "9001" not in visible


def test_duplicate_acknowledgement_is_success(tmp_path):
    env = _fixture(tmp_path)
    sender = _RepairSender(outcome="duplicate")

    result = repair_live_dead_letter(
        env["request"],
        instances_root=env["instances"],
        rollback_root=env["rollback"],
        ingestion_url="https://example.supabase.co/trading-mt5-events",
        sender=sender,
    )

    assert result["outcome"] == "duplicate"
    assert result["dead_letter_count"] == result["pending_count"] == 0


def test_completed_real_trade_receipt_short_circuits_without_resend(tmp_path):
    env = _fixture(tmp_path)
    sender = _RepairSender(outcome="ok")

    first = repair_live_dead_letter(
        env["request"],
        instances_root=env["instances"],
        rollback_root=env["rollback"],
        ingestion_url="https://example.supabase.co/trading-mt5-events",
        sender=sender,
    )
    second = repair_live_dead_letter(
        env["request"],
        instances_root=env["instances"],
        rollback_root=env["rollback"],
        ingestion_url="https://example.supabase.co/trading-mt5-events",
        sender=sender,
    )

    assert second == first
    assert len(sender.payloads) == 1


def test_running_terminal_target_blocks_repair_before_mutation(tmp_path, monkeypatch):
    env = _fixture(tmp_path)
    original = env["outbox_path"].read_bytes()
    monkeypatch.setattr(
        repair_module.ProcessManager,
        "find_under",
        staticmethod(lambda _root: [4242]),
    )

    with pytest.raises(DeadLetterRepairError, match="terminal_must_be_stopped"):
        repair_live_dead_letter(
            env["request"],
            instances_root=env["instances"],
            rollback_root=env["rollback"],
            ingestion_url="https://example.supabase.co/trading-mt5-events",
            sender=_RepairSender(),
        )

    assert env["outbox_path"].read_bytes() == original
    assert not env["rollback"].exists()


def test_stale_full_ledger_evidence_blocks_first_attempt(tmp_path):
    env = _fixture(tmp_path)
    original = env["outbox_path"].read_bytes()
    snapshot = json.loads(env["snapshot_path"].read_text(encoding="utf-8"))
    snapshot["captured_at_utc"] = (
        datetime.now(timezone.utc) - timedelta(hours=1)
    ).isoformat()
    _bind_snapshot(env, snapshot)

    with pytest.raises(DeadLetterRepairError, match="ledger_snapshot_stale"):
        repair_live_dead_letter(
            env["request"],
            instances_root=env["instances"],
            rollback_root=env["rollback"],
            ingestion_url="https://example.supabase.co/trading-mt5-events",
            sender=_RepairSender(),
        )

    assert env["outbox_path"].read_bytes() == original
    assert not env["rollback"].exists()


def test_worker_utc_through_is_not_compared_to_broker_clock(tmp_path):
    env = _fixture(tmp_path)
    snapshot = json.loads(env["snapshot_path"].read_text(encoding="utf-8"))
    # The worker acquisition cursor can be behind a broker-server timestamp by hours.
    snapshot["through"] = "2026-09-17T12:00:00+00:00"
    _bind_snapshot(env, snapshot)

    result = repair_live_dead_letter(
        env["request"],
        instances_root=env["instances"],
        rollback_root=env["rollback"],
        ingestion_url="https://example.supabase.co/trading-mt5-events",
        sender=_RepairSender(),
    )

    assert result["classification"] == "real_trade"


def test_stale_append_only_ledger_is_ignored_in_favor_of_bound_snapshot(tmp_path):
    env = _fixture(tmp_path)
    # The fixture deliberately stores BUY_CANCELED in the legacy JSONL while the current,
    # digest-bound full snapshot contains the exact BUY opening.
    sender = _RepairSender()

    result = repair_live_dead_letter(
        env["request"],
        instances_root=env["instances"],
        rollback_root=env["rollback"],
        ingestion_url="https://example.supabase.co/trading-mt5-events",
        sender=sender,
    )

    assert result["classification"] == "real_trade"
    assert sender.payloads[0]["symbol"] == "EURUSD"


@pytest.mark.parametrize("mutation", ["missing", "digest-mismatch"])
def test_missing_or_mutated_bound_snapshot_never_mutates(tmp_path, mutation):
    env = _fixture(tmp_path)
    original = env["outbox_path"].read_bytes()
    if mutation == "missing":
        env["snapshot_path"].unlink()
        expected = "ledger_snapshot_unavailable"
    else:
        env["snapshot_path"].write_bytes(env["snapshot_path"].read_bytes() + b" ")
        expected = "ledger_snapshot_digest_mismatch"

    with pytest.raises(DeadLetterRepairError, match=expected):
        repair_live_dead_letter(
            env["request"],
            instances_root=env["instances"],
            rollback_root=env["rollback"],
            ingestion_url="https://example.supabase.co/trading-mt5-events",
            sender=_RepairSender(),
        )

    assert env["outbox_path"].read_bytes() == original
    assert not env["rollback"].exists()
    assert not (env["instance"] / "state" / "dead-letter-resolutions").exists()


def test_current_cancelled_snapshot_wins_over_stale_buy_jsonl(tmp_path):
    cancelled = _ledger_row(
        deal_type=13,
        position_id="0",
        order_id="0",
        symbol="",
        volume=0,
        price=0,
    )
    env = _fixture(
        tmp_path,
        row=cancelled,
        payload=_payload(external_trade_id="9001", volume=0, open_price=0),
    )
    legacy = env["instance"] / "data" / "history-accounting.jsonl"
    legacy.write_text(
        json.dumps(
            {"kind": "accounting_deals", "record": _ledger_row()}, sort_keys=True
        )
        + "\n",
        encoding="utf-8",
    )
    original = env["outbox_path"].read_bytes()
    sender = _RepairSender()

    with pytest.raises(DeadLetterRepairError, match="ledger_classification_ambiguous"):
        repair_live_dead_letter(
            env["request"],
            instances_root=env["instances"],
            rollback_root=env["rollback"],
            ingestion_url="https://example.supabase.co/trading-mt5-events",
            sender=sender,
        )

    assert sender.payloads == []
    assert env["outbox_path"].read_bytes() == original


def test_real_trade_delivery_never_drains_successor_enqueued_during_send(tmp_path):
    env = _fixture(tmp_path)
    successor = {
        **_payload(external_trade_id="7001"),
        "event_id": "mt5-sensitive-account-trade_closed-sensitive-digest",
        "event_type": "trade_closed",
        "event_time": "2026-09-17T16:05:15+00:00",
    }
    sender = _EnqueueSuccessorOnSend(env["outbox_path"], successor)

    result = repair_live_dead_letter(
        env["request"],
        instances_root=env["instances"],
        rollback_root=env["rollback"],
        ingestion_url="https://example.supabase.co/trading-mt5-events",
        sender=sender,
    )

    persisted = EventOutbox(str(env["outbox_path"]))
    assert result["status"] == "resolved"
    assert result["pending_count"] == 1
    assert len(sender.payloads) == 1
    assert sender.payloads[0]["event_id"] == env["payload"]["event_id"]
    assert persisted.dead_letter_count() == 0
    assert persisted.pending_payloads() == {successor["event_id"]: successor}


def test_non_trading_accounting_is_audited_without_delivery(tmp_path):
    row = _ledger_row(
        deal_type=2,
        position_id="0",
        order_id="0",
        symbol="",
        volume=0,
        price=0,
    )
    env = _fixture(
        tmp_path,
        row=row,
        payload=_payload(external_trade_id="9001", volume=0, open_price=0),
    )
    sender = _RepairSender()

    result = repair_live_dead_letter(
        env["request"],
        instances_root=env["instances"],
        rollback_root=env["rollback"],
        ingestion_url="https://example.supabase.co/trading-mt5-events",
        sender=sender,
    )

    assert result["classification"] == "non_trading_accounting"
    assert result["action"] == "audit_archived_barrier_removed"
    assert result["http_status"] is None
    assert sender.payloads == []
    assert EventOutbox(str(env["outbox_path"])).dead_letter_count() == 0

    # Re-running the same digest-addressed request is idempotent and performs no delivery.
    repeated = repair_live_dead_letter(
        env["request"],
        instances_root=env["instances"],
        rollback_root=env["rollback"],
        ingestion_url="https://example.supabase.co/trading-mt5-events",
        sender=sender,
    )
    assert repeated["classification"] == "non_trading_accounting"
    assert sender.payloads == []


@pytest.mark.parametrize(
    "deal_type",
    [2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 15, 16, 17],
)
def test_only_explicit_mt5_accounting_types_with_accounting_shape_are_resolved(
    tmp_path, deal_type
):
    row = _ledger_row(
        deal_type=deal_type,
        position_id="0",
        order_id="0",
        symbol="",
        volume=0,
        price=0,
    )
    env = _fixture(
        tmp_path,
        row=row,
        payload=_payload(external_trade_id="9001", volume=0, open_price=0),
    )

    result = repair_live_dead_letter(
        env["request"],
        instances_root=env["instances"],
        rollback_root=env["rollback"],
        ingestion_url="https://example.supabase.co/trading-mt5-events",
        sender=_RepairSender(),
    )

    assert result["classification"] == "non_trading_accounting"


@pytest.mark.parametrize(
    "row,payload",
    [
        (
            _ledger_row(
                deal_type=99,
                position_id="0",
                order_id="0",
                symbol="",
                volume=0,
                price=0,
            ),
            _payload(external_trade_id="9001", volume=0, open_price=0),
        ),
        (
            _ledger_row(
                deal_type=0,
                position_id="0",
                order_id="0",
                symbol="",
                volume=0,
                price=0,
            ),
            _payload(external_trade_id="9001", volume=0, open_price=0),
        ),
        (
            _ledger_row(
                deal_type=1,
                direction="sell",
                position_id="0",
                order_id="0",
                symbol="",
                volume=0,
                price=0,
            ),
            _payload(external_trade_id="9001", volume=0, open_price=0),
        ),
        (_ledger_row(deal_type=2), _payload()),
        (
            _ledger_row(
                deal_type=13,
                position_id="0",
                order_id="0",
                symbol="",
                volume=0,
                price=0,
            ),
            _payload(external_trade_id="9001", volume=0, open_price=0),
        ),
        (
            _ledger_row(
                deal_type=14,
                position_id="0",
                order_id="0",
                symbol="",
                volume=0,
                price=0,
            ),
            _payload(external_trade_id="9001", volume=0, open_price=0),
        ),
    ],
    ids=[
        "unknown-accounting-shape",
        "buy-position-zero",
        "sell-position-zero",
        "accounting-type-trade-shape",
        "buy-cancelled",
        "sell-cancelled",
    ],
)
def test_ambiguous_deal_type_or_shape_never_mutates(tmp_path, row, payload):
    env = _fixture(tmp_path, row=row, payload=payload)
    original = env["outbox_path"].read_bytes()
    sender = _RepairSender()

    with pytest.raises(DeadLetterRepairError, match="ledger_classification_ambiguous"):
        repair_live_dead_letter(
            env["request"],
            instances_root=env["instances"],
            rollback_root=env["rollback"],
            ingestion_url="https://example.supabase.co/trading-mt5-events",
            sender=sender,
        )

    assert env["outbox_path"].read_bytes() == original
    assert EventOutbox(str(env["outbox_path"])).dead_letter_count() == 1
    assert sender.payloads == []
    assert not env["rollback"].exists()
    assert not (env["instance"] / "state" / "dead-letter-resolutions").exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("connection_id", "d8c7ea21-512f-4629-a065-d5d6f85c0f0e"),
        ("account_number", "different-account"),
        ("server", "Different Broker"),
    ],
)
def test_ledger_report_identity_mismatch_never_mutates(tmp_path, field, value):
    env = _fixture(tmp_path)
    original = env["outbox_path"].read_bytes()
    report_path = env["instance"] / "data" / "history-balance-backfill.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report[field] = value
    _write_json(report_path, report)

    with pytest.raises(DeadLetterRepairError, match="ledger_identity_mismatch"):
        repair_live_dead_letter(
            env["request"],
            instances_root=env["instances"],
            rollback_root=env["rollback"],
            ingestion_url="https://example.supabase.co/trading-mt5-events",
            sender=_RepairSender(),
        )

    assert env["outbox_path"].read_bytes() == original
    assert not env["rollback"].exists()
    assert not (env["instance"] / "state" / "dead-letter-resolutions").exists()


def test_ledger_report_identity_uses_existing_exact_normalizers(tmp_path):
    env = _fixture(tmp_path)
    snapshot = json.loads(env["snapshot_path"].read_text(encoding="utf-8"))
    snapshot["account_number"] = "  sensitive-account  "
    snapshot["server"] = "  SENSITIVE   broker  "
    _bind_snapshot(env, snapshot)

    result = repair_live_dead_letter(
        env["request"],
        instances_root=env["instances"],
        rollback_root=env["rollback"],
        ingestion_url="https://example.supabase.co/trading-mt5-events",
        sender=_RepairSender(),
    )

    assert result["classification"] == "real_trade"


@pytest.mark.parametrize(
    "mutation,expected_error",
    [
        ("incoherent", "ledger_not_coherent"),
        ("incomplete", "ledger_snapshot_invalid"),
        ("ambiguous", "ledger_match_ambiguous"),
    ],
)
def test_ambiguous_or_unverified_ledger_never_mutates_outbox(
    tmp_path, mutation, expected_error
):
    env = _fixture(tmp_path)
    original = env["outbox_path"].read_bytes()
    report_path = env["instance"] / "data" / "history-balance-backfill.json"
    if mutation == "incoherent":
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report["anchor"]["coherent"] = False
        _write_json(report_path, report)
    elif mutation == "incomplete":
        snapshot = json.loads(env["snapshot_path"].read_text(encoding="utf-8"))
        snapshot["rows"] = []
        # Keep the bound count unchanged to model a truncated but freshly hashed snapshot.
        _bind_snapshot(env, snapshot, update_boundary=False)
    else:
        first = _ledger_row()
        second = {
            **first,
            "history_index": 1,
            "ticket": "9002",
            "order_id": "7001",
        }
        _replace_snapshot(env, [first, second])

    with pytest.raises(DeadLetterRepairError, match=expected_error):
        repair_live_dead_letter(
            env["request"],
            instances_root=env["instances"],
            rollback_root=env["rollback"],
            ingestion_url="https://example.supabase.co/trading-mt5-events",
            sender=_RepairSender(),
        )

    assert env["outbox_path"].read_bytes() == original
    assert not env["rollback"].exists()
    assert not (env["instance"] / "state" / "dead-letter-resolutions").exists()


def test_outbox_preimage_mismatch_fails_before_backup_or_audit(tmp_path):
    env = _fixture(tmp_path)
    wrong = RepairRequest(
        connection_id=CID,
        deployment_id="test-deployment",
        expected_outbox_sha256="0" * 64,
        expected_record_sha256=env["request"].expected_record_sha256,
        expected_ledger_snapshot_sha256=(
            env["request"].expected_ledger_snapshot_sha256
        ),
    )

    with pytest.raises(DeadLetterRepairError, match="outbox_preimage_mismatch"):
        repair_live_dead_letter(
            wrong,
            instances_root=env["instances"],
            rollback_root=env["rollback"],
            ingestion_url="https://example.supabase.co/trading-mt5-events",
            sender=_RepairSender(),
        )

    assert not env["rollback"].exists()
    assert EventOutbox(str(env["outbox_path"])).dead_letter_count() == 1


def test_transient_delivery_stays_dead_lettered_and_rerun_can_confirm_duplicate(tmp_path):
    env = _fixture(tmp_path)
    transient = _RepairSender(
        outcome=None,
        result=SendResult(
            status="failed",
            http_status=503,
            error="delivery_unavailable",
            attempts=1,
            failure_type="transient",
        ),
    )
    transient.http_status = 503

    with pytest.raises(DeadLetterRepairError, match="corrected_delivery_incomplete"):
        repair_live_dead_letter(
            env["request"],
            instances_root=env["instances"],
            rollback_root=env["rollback"],
            ingestion_url="https://example.supabase.co/trading-mt5-events",
            sender=transient,
        )
    persisted = EventOutbox(str(env["outbox_path"]))
    assert persisted.pending_count() == 0
    assert persisted.dead_letter_count() == 1

    duplicate = _RepairSender(outcome="duplicate")
    result = repair_live_dead_letter(
        env["request"],
        instances_root=env["instances"],
        rollback_root=env["rollback"],
        ingestion_url="https://example.supabase.co/trading-mt5-events",
        sender=duplicate,
    )
    assert result["outcome"] == "duplicate"
    assert EventOutbox(str(env["outbox_path"])).pending_count() == 0


def test_retry_is_pinned_to_audited_ledger_preimage_after_new_full_scan(tmp_path):
    env = _fixture(tmp_path)
    audited_request = env["request"]
    transient = _RepairSender(
        result=SendResult(
            status="failed",
            http_status=503,
            error="delivery_unavailable",
            attempts=1,
            failure_type="transient",
        )
    )
    with pytest.raises(DeadLetterRepairError, match="corrected_delivery_incomplete"):
        repair_live_dead_letter(
            audited_request,
            instances_root=env["instances"],
            rollback_root=env["rollback"],
            ingestion_url="https://example.supabase.co/trading-mt5-events",
            sender=transient,
        )

    # A newer all-available job can publish a different immutable bundle, but the existing
    # audit/request must never silently follow the moving report pointer.
    cancelled = _ledger_row(
        deal_type=13,
        position_id="0",
        order_id="0",
        symbol="",
        volume=0,
        price=0,
    )
    _replace_snapshot(env, [cancelled])
    original = env["outbox_path"].read_bytes()
    retry_sender = _RepairSender(outcome="duplicate")

    with pytest.raises(
        DeadLetterRepairError, match="ledger_snapshot_preimage_mismatch"
    ):
        repair_live_dead_letter(
            audited_request,
            instances_root=env["instances"],
            rollback_root=env["rollback"],
            ingestion_url="https://example.supabase.co/trading-mt5-events",
            sender=retry_sender,
        )

    assert retry_sender.payloads == []
    assert env["outbox_path"].read_bytes() == original
    assert EventOutbox(str(env["outbox_path"])).dead_letter_count() == 1


@pytest.mark.parametrize(
    "mutation",
    ["active-sequence", "archived-membership", "archive-digest", "partial-active"],
)
def test_incomplete_or_superseded_handoff_never_mutates(tmp_path, mutation):
    env = _fixture(tmp_path)
    original = env["outbox_path"].read_bytes()
    active_path = env["instance"] / "state" / "history-live-handoff.json"
    active = json.loads(active_path.read_text(encoding="utf-8"))
    if mutation == "active-sequence":
        active["anchor_sequence"] += 1
        _write_json(active_path, active)
    elif mutation == "archived-membership":
        active["archived_deal_tickets"] = []
        _write_json(active_path, active)
    elif mutation == "archive-digest":
        env["archive_path"].write_bytes(env["archive_path"].read_bytes() + b" ")
    else:
        active["history_mode"] = "from_date"
        active["ledger_evidence"] = None
        _write_json(active_path, active)

    with pytest.raises(DeadLetterRepairError, match="ledger_handoff_not_active"):
        repair_live_dead_letter(
            env["request"],
            instances_root=env["instances"],
            rollback_root=env["rollback"],
            ingestion_url="https://example.supabase.co/trading-mt5-events",
            sender=_RepairSender(),
        )

    assert env["outbox_path"].read_bytes() == original
    assert not env["rollback"].exists()


def test_private_write_recovers_partial_orphan_and_rerun_cleans_orphan(tmp_path):
    destination = tmp_path / "audit" / "record.json"
    payload = b'{"classification":"real_trade"}'
    absolute_destination = repair_module._absolute_path(destination)
    temporary = repair_module._private_temporary_path(absolute_destination, payload)
    temporary.parent.mkdir(parents=True)
    temporary.write_bytes(b"partial")
    temporary.chmod(0o600)

    repair_module._write_private_once(destination, payload)

    assert destination.read_bytes() == payload
    assert not temporary.exists()
    _assert_private(destination)

    # Models POSIX crashing after the no-replace link and before unlinking the temp name.
    temporary.write_bytes(b"orphan-after-publish")
    temporary.chmod(0o600)
    repair_module._write_private_once(destination, payload)
    assert destination.read_bytes() == payload
    assert not temporary.exists()


def test_private_write_never_overwrites_existing_artifact(tmp_path):
    destination = tmp_path / "audit" / "record.json"
    repair_module._write_private_once(destination, b"first")

    with pytest.raises(
        DeadLetterRepairError, match="immutable_maintenance_artifact_conflict"
    ):
        repair_module._write_private_once(destination, b"second")

    assert destination.read_bytes() == b"first"


def test_private_write_losing_publish_race_does_not_replace_winner(
    tmp_path, monkeypatch
):
    destination = tmp_path / "audit" / "record.json"

    def competing_publish(_source, target):
        target.write_bytes(b"winner")
        target.chmod(0o600)
        raise FileExistsError

    monkeypatch.setattr(repair_module, "_publish_no_replace", competing_publish)

    with pytest.raises(
        DeadLetterRepairError, match="immutable_maintenance_artifact_conflict"
    ):
        repair_module._write_private_once(destination, b"candidate")

    assert destination.read_bytes() == b"winner"


@pytest.mark.parametrize("target_kind", ["state", "data", "audit", "rollback"])
def test_symlink_in_sensitive_path_chain_is_rejected_without_mutation(
    tmp_path, target_kind
):
    env = _fixture(tmp_path)
    original = env["outbox_path"].read_bytes()
    if target_kind in ("state", "data"):
        target = env["instance"] / target_kind
        real = tmp_path / f"real-{target_kind}"
        target.rename(real)
    elif target_kind == "audit":
        target = env["instance"] / "state" / "dead-letter-resolutions"
        real = tmp_path / "real-audit"
        real.mkdir()
    else:
        target = env["rollback"]
        real = tmp_path / "real-rollback"
        real.mkdir()
    try:
        target.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable on this platform")

    with pytest.raises(
        DeadLetterRepairError,
        match="(?:rollback|maintenance)_(?:path|file)_invalid",
    ):
        repair_live_dead_letter(
            env["request"],
            instances_root=env["instances"],
            rollback_root=env["rollback"],
            ingestion_url="https://example.supabase.co/trading-mt5-events",
            sender=_RepairSender(),
        )

    assert env["outbox_path"].read_bytes() == original
    assert EventOutbox(str(env["outbox_path"])).dead_letter_count() == 1


@pytest.mark.parametrize(
    "target_kind", ["state", "data", "ledger-file", "audit", "rollback"]
)
def test_windows_reparse_attribute_on_file_or_ancestor_is_rejected(
    tmp_path, monkeypatch, target_kind
):
    env = _fixture(tmp_path)
    original = env["outbox_path"].read_bytes()
    if target_kind == "state":
        target = env["instance"] / "state"
    elif target_kind == "data":
        target = env["instance"] / "data"
    elif target_kind == "ledger-file":
        target = env["instance"] / "data" / "history-balance-backfill.json"
    elif target_kind == "audit":
        target = env["instance"] / "state" / "dead-letter-resolutions"
        target.mkdir()
    else:
        target = env["rollback"]
        target.mkdir()
    target = repair_module._absolute_path(target)
    actual_attributes = repair_module._windows_file_attributes

    def modeled_attributes(path, metadata):
        if repair_module._absolute_path(path) == target:
            return repair_module._FILE_ATTRIBUTE_REPARSE_POINT
        return actual_attributes(path, metadata)

    monkeypatch.setattr(
        repair_module, "_windows_file_attributes", modeled_attributes
    )

    with pytest.raises(
        DeadLetterRepairError,
        match="(?:rollback|maintenance)_(?:path|file)_invalid",
    ):
        repair_live_dead_letter(
            env["request"],
            instances_root=env["instances"],
            rollback_root=env["rollback"],
            ingestion_url="https://example.supabase.co/trading-mt5-events",
            sender=_RepairSender(),
        )

    assert env["outbox_path"].read_bytes() == original
    assert EventOutbox(str(env["outbox_path"])).dead_letter_count() == 1
