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
        [{"ticket": "3", "position_id": "1", "symbol": "EURUSD", "time": "2026-07-17T10:00:00Z"}],
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
