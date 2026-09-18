from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from windows_agent.worker.mql5_file_adapter import (
    Mql5FileAdapterError,
    Mql5FileMt5Adapter,
    Mql5FileStale,
    Mql5FileIdentityMismatch,
)


CONNECTION_ID = "00000000-0000-4000-8000-000000000001"


def _envelope(payload: object, *, generated_at: datetime | None = None, sequence: int = 1) -> dict:
    generated_at = generated_at or datetime.now(timezone.utc)
    return {
        "schema_version": 1,
        "generated_at": generated_at.isoformat().replace("+00:00", "Z"),
        "sequence": sequence,
        "account_identity": {"login": "42", "server": "Demo-Server"},
        "server_identity": "Demo-Server",
        "payload": payload,
    }


def _write(root: Path, name: str, payload: object, **kwargs: object) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_envelope(payload, **kwargs)), encoding="utf-8")


def _ready_adapter(tmp_path: Path) -> Mql5FileMt5Adapter:
    root = tmp_path / "TradeJournal"
    _write(root, "heartbeat.json", {"terminal_connected": True, "account_trade_allowed": False})
    _write(
        root,
        "account.json",
        {
            "login": "42",
            "server": "Demo-Server",
            "balance": 100.0,
            "equity": 101.0,
            "currency": "USD",
            "leverage": 100,
            "trade_allowed": False,
        },
    )
    _write(root, "positions.json", [{"ticket": "1", "symbol": "EURUSD", "direction": "buy"}])
    _write(root, "orders.json", [{"ticket": "2", "symbol": "EURUSD", "direction": "sell"}])
    _write(root, "history_orders.json", [])
    _write(
        root,
        "deals.json",
        [{"ticket": "3", "position_id": "1", "symbol": "EURUSD", "entry": "OUT", "time": "2026-07-17T10:00:00Z"}],
    )
    return Mql5FileMt5Adapter(root, CONNECTION_ID, 42, "Demo-Server", tmp_path / "state")


def test_reads_versioned_snapshots_and_preserves_sync_interface(tmp_path: Path) -> None:
    adapter = _ready_adapter(tmp_path)
    assert adapter.verify_identity() == {"login": "42", "server": "Demo-Server"}
    assert adapter.terminal_info().connected is True
    account = adapter.account_info()
    assert account.trade_allowed is False
    assert account.balance == 100.0
    assert account.equity == 101.0
    assert account.currency == "USD"
    assert account.leverage == 100
    snapshot = adapter.snapshot()
    assert set(snapshot) == {"positions", "orders", "deals"}
    assert list(snapshot["deals"]) == ["3"]
    assert len(adapter.history_deals(datetime(2026, 1, 1, tzinfo=timezone.utc), datetime(2027, 1, 1, tzinfo=timezone.utc))) == 1


def test_history_deals_ignore_non_position_and_unclassified_account_movements(
    tmp_path: Path,
) -> None:
    adapter = _ready_adapter(tmp_path)
    _write(
        adapter.files_dir,
        "deals.json",
        [
            {"ticket": "balance", "position_id": "0", "symbol": "", "entry": "IN", "time": "2026-07-17T09:00:00Z"},
            {"ticket": "legacy", "position_id": "8", "symbol": "EURUSD", "time": "2026-07-17T09:30:00Z"},
            {"ticket": "close", "position_id": "8", "symbol": "EURUSD", "entry": "OUT", "time": "2026-07-17T10:00:00Z"},
        ],
    )

    rows = adapter.history_deals(
        datetime(2026, 1, 1, tzinfo=timezone.utc),
        datetime(2027, 1, 1, tzinfo=timezone.utc),
    )

    assert [row["ticket"] for row in rows] == ["close"]


def test_all_available_uses_the_complete_atomic_snapshot_not_a_utc_cutoff(
    tmp_path: Path,
) -> None:
    adapter = _ready_adapter(tmp_path)
    _write(
        adapter.files_dir,
        "deals.json",
        [{
            "ticket": "broker-future",
            "position_id": "8",
            "symbol": "EURUSD",
            "entry": "OUT",
            "time": "2030-01-01T02:00:00Z",
        }],
    )

    rows = adapter.history_deals(
        datetime(1970, 1, 1, tzinfo=timezone.utc),
        datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    assert [row["ticket"] for row in rows] == ["broker-future"]


def test_anchored_history_keeps_accounting_ledger_separate_and_reconstructs_balance(
    tmp_path: Path,
) -> None:
    adapter = _ready_adapter(tmp_path)
    deals = [
        {
            "history_index": 0, "ticket": "1", "order_id": "0", "position_id": "0", "symbol": "",
            "deal_type": 2, "entry": "IN", "volume": 0, "price": 0,
            "profit": 1000, "commission": 0, "swap": 0, "fee": 0,
            "time_msc": 1_767_225_600_000, "time": "2026-01-01T00:00:00Z",
        },
        {
            "history_index": 1, "ticket": "2", "order_id": "12", "position_id": "A", "symbol": "EURUSD",
            "deal_type": 0, "entry": "IN", "direction": "buy", "volume": 0.1,
            "price": 1.1, "profit": 0, "commission": -0.8, "swap": 0, "fee": -0.2,
            "time_msc": 1_767_225_601_000, "time": "2026-01-01T00:00:01Z",
        },
        {
            "history_index": 2, "ticket": "3", "order_id": "13", "position_id": "A", "symbol": "EURUSD",
            "deal_type": 1, "entry": "OUT", "direction": "sell", "volume": 0.1,
            "price": 1.2, "profit": 10, "commission": -1, "swap": 0, "fee": 0,
            "time_msc": 1_767_225_602_000, "time": "2026-01-01T00:00:02Z",
        },
    ]
    _write(
        adapter.files_dir,
        "deals.json",
        {
            "anchor": {
                "balance": 1008,
                "credit": 0,
                "as_of": "2026-01-01T00:00:03Z",
                "coherent": True,
                "order_basis": "mt5_history_index_v1",
                "deal_count": 3,
                "last_deal_ticket": "3",
                "last_deal_time_msc": 1_767_225_602_000,
            },
            "deals": deals,
        },
    )

    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = datetime(2026, 1, 2, tzinfo=timezone.utc)
    ledger = adapter.history_accounting_deals(start, end)
    projected = adapter.history_deals(start, end)

    assert [row["ticket"] for row in ledger] == ["1", "2", "3"]
    assert [row["ticket"] for row in projected] == ["2", "3"]
    assert projected[0]["balance_before_open"] == 1000
    assert projected[0]["commission"] == -1
    assert projected[1]["total_commission"] == -2
    # from_date is based on the canonical opening, never a close without its predecessor.
    assert adapter.history_deals(
        datetime(2026, 1, 1, 0, 0, 2, tzinfo=timezone.utc), end
    ) == ()


def test_anchored_history_fails_closed_without_contiguous_mt5_history_indices(
    tmp_path: Path,
) -> None:
    adapter = _ready_adapter(tmp_path)
    row = {
        "history_index": 1,
        "ticket": "2",
        "order_id": "12",
        "position_id": "A",
        "symbol": "EURUSD",
        "deal_type": 0,
        "entry": "IN",
        "direction": "buy",
        "volume": 0.1,
        "price": 1.1,
        "profit": 0,
        "commission": 0,
        "swap": 0,
        "fee": 0,
        "time_msc": 1_767_225_601_000,
        "time": "2026-01-01T00:00:01Z",
    }
    _write(
        adapter.files_dir,
        "deals.json",
        {
            "anchor": {
                "balance": 1_000,
                "credit": 0,
                "as_of": "2026-01-01T00:00:03Z",
                "coherent": True,
                "order_basis": "mt5_history_index_v1",
                "deal_count": 1,
                "last_deal_ticket": "2",
                "last_deal_time_msc": 1_767_225_601_000,
            },
            "deals": [row],
        },
    )

    assert adapter.history_anchor()["coherent"] is False
    assert adapter.history_balance_rows()[0]["balance_before_open"] is None


def test_snapshot_keys_positions_by_stable_identifier_with_ticket_fallback(tmp_path: Path) -> None:
    adapter = _ready_adapter(tmp_path)
    _write(
        adapter.files_dir,
        "positions.json",
        [
            {"ticket": "mutable-1", "position_id": "stable-1", "symbol": "EURUSD"},
            {"ticket": "legacy-2", "symbol": "GBPUSD"},
        ],
    )

    snapshot = adapter.snapshot()

    assert set(snapshot["positions"]) == {"stable-1", "legacy-2"}
    assert snapshot["positions"]["stable-1"]["ticket"] == "mutable-1"


def test_rejects_snapshot_composed_from_different_publish_sequences(tmp_path: Path) -> None:
    adapter = _ready_adapter(tmp_path)
    _write(
        adapter.files_dir,
        "positions.json",
        [{"ticket": "1", "symbol": "EURUSD", "direction": "buy"}],
        sequence=2,
    )

    with pytest.raises(Mql5FileAdapterError, match="snapshot_sequence_mismatch"):
        adapter.snapshot()


@pytest.mark.parametrize("filename", ["history_orders.json", "deals.json"])
def test_rejects_historical_snapshot_with_stale_file_sequence(
    tmp_path: Path, filename: str
) -> None:
    adapter = _ready_adapter(tmp_path)
    _write(adapter.files_dir, filename, [], sequence=0)
    start = datetime(1970, 1, 1, tzinfo=timezone.utc)
    end = datetime(2027, 1, 1, tzinfo=timezone.utc)

    with pytest.raises(Mql5FileAdapterError, match="history_snapshot_sequence_mismatch"):
        if filename == "history_orders.json":
            adapter.history_orders(start, end)
        else:
            adapter.history_deals(start, end)


def test_retries_one_snapshot_publish_boundary_before_accepting_a_consistent_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _ready_adapter(tmp_path)
    original_rows = adapter._rows
    reads = 0

    def rows_during_publish(name: str):
        nonlocal reads
        result = original_rows(name)
        if name == "positions.json" and reads == 0:
            reads += 1
            _write(adapter.files_dir, "heartbeat.json", {"terminal_connected": True}, sequence=2)
            _write(
                adapter.files_dir,
                "account.json",
                {
                    "login": "42",
                    "server": "Demo-Server",
                    "balance": 100.0,
                    "equity": 101.0,
                    "currency": "USD",
                    "leverage": 100,
                    "trade_allowed": False,
                },
                sequence=2,
            )
            _write(adapter.files_dir, "positions.json", [], sequence=2)
            _write(adapter.files_dir, "orders.json", [], sequence=2)
            _write(adapter.files_dir, "deals.json", [], sequence=2)
        return result

    monkeypatch.setattr(adapter, "_rows", rows_during_publish)
    monkeypatch.setattr("windows_agent.worker.mql5_file_adapter.time.sleep", lambda _: None)

    assert adapter.snapshot() == {"positions": {}, "orders": {}, "deals": {}}
    assert reads == 1


def test_retries_a_transient_windows_sharing_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _ready_adapter(tmp_path)
    original_read_text = Path.read_text
    blocked_path = adapter.files_dir / "heartbeat.json"
    attempts = 0

    def read_text_with_one_sharing_failure(path: Path, *args: object, **kwargs: object) -> str:
        nonlocal attempts
        if path == blocked_path and attempts == 0:
            attempts += 1
            raise PermissionError("sharing violation")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text_with_one_sharing_failure)
    monkeypatch.setattr("windows_agent.worker.mql5_file_adapter.time.sleep", lambda _: None)

    assert adapter.verify_identity() == {"login": "42", "server": "Demo-Server"}
    assert attempts == 1


def test_retries_windows_sharing_errors_across_a_complete_publish_cycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _ready_adapter(tmp_path)
    original_read_text = Path.read_text
    blocked_path = adapter.files_dir / "heartbeat.json"
    attempts = 0

    def read_text_during_publish(path: Path, *args: object, **kwargs: object) -> str:
        nonlocal attempts
        if path == blocked_path and attempts < 20:
            attempts += 1
            raise PermissionError("sharing violation")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text_during_publish)
    monkeypatch.setattr("windows_agent.worker.mql5_file_adapter.time.sleep", lambda _: None)

    assert adapter.verify_identity() == {"login": "42", "server": "Demo-Server"}
    assert attempts == 20


def test_persistent_windows_sharing_error_remains_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _ready_adapter(tmp_path)
    blocked_path = adapter.files_dir / "heartbeat.json"
    original_read_text = Path.read_text

    def read_text_always_blocked(path: Path, *args: object, **kwargs: object) -> str:
        if path == blocked_path:
            raise PermissionError("sharing violation")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text_always_blocked)
    monkeypatch.setattr("windows_agent.worker.mql5_file_adapter.time.sleep", lambda _: None)

    with pytest.raises(Mql5FileAdapterError, match="heartbeat_unavailable"):
        adapter.verify_identity()


def test_retries_snapshot_boundaries_across_a_complete_publish_cycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _ready_adapter(tmp_path)
    original_rows = adapter._rows
    position_reads = 0

    def rows_during_repeated_publish_boundaries(name: str):
        nonlocal position_reads
        result = original_rows(name)
        if name == "positions.json" and position_reads < 20:
            position_reads += 1
            _write(
                adapter.files_dir,
                "heartbeat.json",
                {"terminal_connected": True, "account_trade_allowed": False},
                sequence=position_reads + 1,
            )
        elif name == "positions.json":
            _write(
                adapter.files_dir,
                "account.json",
                {
                    "login": "42",
                    "server": "Demo-Server",
                    "balance": 100.0,
                    "equity": 101.0,
                    "currency": "USD",
                    "leverage": 100,
                    "trade_allowed": False,
                },
                sequence=position_reads + 1,
            )
            _write(adapter.files_dir, "positions.json", [], sequence=position_reads + 1)
            _write(adapter.files_dir, "orders.json", [], sequence=position_reads + 1)
            _write(adapter.files_dir, "deals.json", [], sequence=position_reads + 1)
        return result

    monkeypatch.setattr(adapter, "_rows", rows_during_repeated_publish_boundaries)
    monkeypatch.setattr("windows_agent.worker.mql5_file_adapter.time.sleep", lambda _: None)

    assert adapter.snapshot() == {"positions": {}, "orders": {}, "deals": {}}
    assert position_reads == 20


def test_event_files_are_read_in_sequence_and_acknowledged_separately(tmp_path: Path) -> None:
    adapter = _ready_adapter(tmp_path)
    _write(
        adapter.files_dir,
        "events/event-12.json",
        {
            "event_type": "DEAL_ADD",
            "position_id": "100",
            "ticket": "200",
            "entry": "OUT",
            "time": "2026-07-17T10:00:00Z",
        },
        sequence=12,
    )
    _write(
        adapter.files_dir,
        "events/event-10.json",
        {
            "event_type": "DEAL_ADD",
            "position_id": "100",
            "ticket": "199",
            "entry": "IN",
            "time": "2026-07-17T09:00:00Z",
        },
        sequence=10,
    )

    assert [event["sequence"] for event in adapter.pending_events()] == [10, 12]
    adapter.acknowledge_events(12)
    assert adapter.pending_events() == ()
    assert not (adapter.files_dir / "events" / "event-10.json").exists()
    assert not (adapter.files_dir / "events" / "event-12.json").exists()


def test_pending_event_marks_native_deal_already_committed_in_history(tmp_path: Path) -> None:
    adapter = _ready_adapter(tmp_path)
    adapter.history_handoff_path.parent.mkdir(parents=True, exist_ok=True)
    adapter.history_handoff_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "job_id": "11111111-1111-4111-8111-111111111111",
                "connection_id": CONNECTION_ID,
                "history_document_sha256": "a" * 64,
                "anchor_sequence": 9,
                "archived_deal_tickets": ["199"],
                # Ticket 199 was deliberately not projected by history (for example an
                # ambiguous reversal), but it still belongs to the frozen ledger boundary.
                "imported_deal_tickets": [],
            }
        ),
        encoding="utf-8",
    )
    _write(
        adapter.files_dir,
        "events/event-10.json",
        {
            "event_type": "DEAL_ADD",
            "deal_id": "199",
            "position_id": "100",
            "ticket": "199",
            "entry": "IN",
            "time": "2026-07-17T09:00:00Z",
        },
        sequence=10,
    )
    _write(
        adapter.files_dir,
        "events/event-11.json",
        {
            "event_type": "DEAL_ADD",
            "deal_id": "200",
            "position_id": "100",
            "ticket": "200",
            "entry": "OUT",
            "time": "2026-07-17T10:00:00Z",
        },
        sequence=11,
    )

    archived, fresh = adapter.pending_events()
    assert archived["history_archived"] is True
    assert "history_archived" not in fresh
    assert adapter.history_handoff_path.exists()


def test_rejects_stale_heartbeat_and_corrupt_json_without_leaking_content(tmp_path: Path) -> None:
    adapter = _ready_adapter(tmp_path)
    old = datetime.now(timezone.utc) - timedelta(seconds=60)
    _write(adapter.files_dir, "heartbeat.json", {"terminal_connected": True}, generated_at=old)
    with pytest.raises(Mql5FileStale, match="heartbeat_stale"):
        adapter.verify_identity()
    (adapter.files_dir / "heartbeat.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(Mql5FileAdapterError, match="heartbeat_unavailable"):
        adapter.verify_identity()


def test_rejects_identity_mismatch_and_keeps_checkpoint_bounded(tmp_path: Path) -> None:
    adapter = _ready_adapter(tmp_path)
    _write(
        adapter.files_dir,
        "account.json",
        {"login": "99", "server": "Demo-Server", "trade_allowed": False},
    )
    with pytest.raises(Mql5FileIdentityMismatch, match="account_identity_mismatch"):
        adapter.verify_identity()

    _write(
        adapter.files_dir,
        "account.json",
        {
            "login": "42",
            "server": "Demo-Server",
            "balance": 100.0,
            "equity": 101.0,
            "currency": "USD",
            "leverage": 100,
            "trade_allowed": False,
        },
        sequence=44,
    )
    _write(
        adapter.files_dir,
        "heartbeat.json",
        {"terminal_connected": True, "account_trade_allowed": False},
        sequence=44,
    )
    _write(adapter.files_dir, "positions.json", [], sequence=44)
    _write(adapter.files_dir, "orders.json", [], sequence=44)
    _write(
        adapter.files_dir,
        "deals.json",
        [{"ticket": str(index), "time": "2026-07-17T10:00:00Z"} for index in range(800)],
        sequence=44,
    )
    assert len(adapter.snapshot()["deals"]) == 800
    checkpoint = json.loads((tmp_path / "state" / "file-adapter-checkpoint.json").read_text())
    assert checkpoint["sequence"] == 44
    assert len(checkpoint["recent_deal_keys"]) <= 512
