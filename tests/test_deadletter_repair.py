from __future__ import annotations

import hashlib
import json
import os
import stat
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


CID = "0973451f-9b9c-4da7-ac53-8de1bc5b5949"


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
        "history_index": 9,
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


def _fixture(tmp_path: Path, *, row=None, payload=None):
    instances = tmp_path / "instances"
    instance = instances / CID
    state = instance / "state"
    data = instance / "data"
    state.mkdir(parents=True)
    data.mkdir(parents=True)
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
    _write_json(
        data / "history-balance-backfill.json",
        {
            "schema_version": 1,
            "connection_id": CID,
            "account_number": "sensitive-account",
            "server": "Sensitive Broker",
            "source": "mt5_historical_ledger",
            "anchor": {
                "coherent": True,
                "deal_count": 1,
                "balance": 10000.0,
                "credit": 0.0,
                "as_of": "2026-09-18T11:59:00+00:00",
            },
            "entries": {},
        },
    )
    (data / "history-accounting.jsonl").write_text(
        json.dumps({"kind": "accounting_deals", "record": row}, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    request = RepairRequest(
        connection_id=CID,
        deployment_id="test-deployment",
        expected_outbox_sha256=outbox_digest,
        expected_record_sha256=record_digest,
    )
    return {
        "instances": instances,
        "instance": instance,
        "rollback": tmp_path / "rollback",
        "request": request,
        "payload": payload,
        "record": record,
        "outbox_path": outbox_path,
    }


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
    report_path = env["instance"] / "data" / "history-balance-backfill.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["account_number"] = "  sensitive-account  "
    report["server"] = "  SENSITIVE   broker  "
    _write_json(report_path, report)

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
        ("incomplete", "ledger_not_complete"),
        ("ambiguous", "ledger_match_ambiguous"),
    ],
)
def test_ambiguous_or_unverified_ledger_never_mutates_outbox(
    tmp_path, mutation, expected_error
):
    env = _fixture(tmp_path)
    original = env["outbox_path"].read_bytes()
    report_path = env["instance"] / "data" / "history-balance-backfill.json"
    audit_path = env["instance"] / "data" / "history-accounting.jsonl"
    if mutation == "incoherent":
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report["anchor"]["coherent"] = False
        _write_json(report_path, report)
    elif mutation == "incomplete":
        audit_path.write_text("", encoding="utf-8")
    else:
        first = _ledger_row()
        second = {**first, "ticket": "9002", "order_id": "7001"}
        audit_path.write_text(
            "\n".join(
                json.dumps({"kind": "accounting_deals", "record": row}, sort_keys=True)
                for row in (first, second)
            )
            + "\n",
            encoding="utf-8",
        )
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report["anchor"]["deal_count"] = 2
        _write_json(report_path, report)
        checkpoint = json.loads(
            (env["instance"] / "state" / "history.json").read_text(encoding="utf-8")
        )
        checkpoint["accounting_deals"] = 2
        _write_json(env["instance"] / "state" / "history.json", checkpoint)

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


def test_transient_delivery_stays_pending_and_rerun_can_confirm_duplicate(tmp_path):
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
    assert persisted.pending_count() == 1
    assert persisted.dead_letter_count() == 0

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
