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
    assert adapter.account_info().trade_allowed is False
    snapshot = adapter.snapshot()
    assert set(snapshot) == {"positions", "orders", "deals"}
    assert list(snapshot["deals"]) == ["3"]
    assert len(adapter.history_deals(datetime(2026, 1, 1, tzinfo=timezone.utc), datetime(2027, 1, 1, tzinfo=timezone.utc))) == 1


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
        {"login": "42", "server": "Demo-Server", "trade_allowed": False},
    )
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
