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
from windows_agent.worker.live_sync import _merge_event_stream_with_snapshot


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


def _write_control(root: Path, name: str, payload: object) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_v2_heartbeat(
    adapter: Mql5FileMt5Adapter,
    *,
    request_generation: int,
    ack_generation: int,
    source_recovery_required: bool = False,
    sequence: int = 1,
) -> None:
    _write(
        adapter.files_dir,
        "heartbeat.json",
        {
            "terminal_connected": True,
            "account_trade_allowed": False,
            "source_recovery_required": source_recovery_required,
            "source_recovery_protocol_version": 2,
            "source_recovery_request_generation": request_generation,
            "source_recovery_ack_generation": ack_generation,
        },
        sequence=sequence,
    )


def _ready_adapter(tmp_path: Path) -> Mql5FileMt5Adapter:
    root = tmp_path / "TradeJournal"
    _write(
        root,
        "heartbeat.json",
        {
            "terminal_connected": True,
            "account_trade_allowed": False,
            "source_recovery_required": False,
            "source_recovery_protocol_version": 2,
            "source_recovery_request_generation": 0,
            "source_recovery_ack_generation": 0,
        },
    )
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
        [{"ticket": "3", "position_id": "1", "order_id": "2", "entry": "IN", "symbol": "EURUSD", "time": "2026-07-17T10:00:00Z"}],
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
    assert snapshot["deals"]["3"]["order_id"] == "2"
    assert len(adapter.history_deals(datetime(2026, 1, 1, tzinfo=timezone.utc), datetime(2027, 1, 1, tzinfo=timezone.utc))) == 1


def test_rejects_stale_heartbeat_and_corrupt_json_without_leaking_content(tmp_path: Path) -> None:
    adapter = _ready_adapter(tmp_path)
    old = datetime.now(timezone.utc) - timedelta(seconds=60)
    _write(
        adapter.files_dir,
        "heartbeat.json",
        {"terminal_connected": True, "source_recovery_required": False},
        generated_at=old,
    )
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
        sequence=44,
    )
    _write(
        adapter.files_dir,
        "heartbeat.json",
        {
            "terminal_connected": True,
            "account_trade_allowed": False,
            "source_recovery_required": False,
        },
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


def test_old_or_recovering_heartbeat_is_never_advertised_as_live(tmp_path: Path) -> None:
    adapter = _ready_adapter(tmp_path)
    _write(
        adapter.files_dir,
        "heartbeat.json",
        {"terminal_connected": True, "account_trade_allowed": False},
    )
    with pytest.raises(Mql5FileAdapterError, match="heartbeat_schema_invalid"):
        adapter.connection_state()

    _write(
        adapter.files_dir,
        "heartbeat.json",
        {
            "terminal_connected": True,
            "account_trade_allowed": False,
            "source_recovery_required": True,
        },
    )
    assert adapter.connection_state() == {
        "connected": False,
        "sequence": 1,
        "source_recovery_required": True,
        "source_recovery_protocol_version": 1,
        "source_recovery_request_generation": 0,
        "source_recovery_ack_generation": 0,
    }


def test_certified_history_recovery_request_is_a_durable_v2_full_ledger_request(
    tmp_path: Path,
) -> None:
    adapter = _ready_adapter(tmp_path)

    adapter.request_certified_history_recovery()

    assert json.loads(
        (adapter.files_dir / "source-recovery-request-v2.json").read_text(
            encoding="utf-8"
        )
    ) == {"protocol_version": 2, "generation": 1, "from_unix": 0}
    assert not (adapter.files_dir / "source-recovery-required.json").exists()
    assert json.loads(
        (tmp_path / "state" / "source-recovery-request-v2-state.json").read_text(
            encoding="utf-8"
        )
    ) == {
        "connection_id": CONNECTION_ID,
        "last_generation": 1,
        "schema_version": 1,
    }
    assert adapter.connection_state() == {
        "connected": False,
        "sequence": 1,
        "source_recovery_required": True,
        "source_recovery_protocol_version": 2,
        "source_recovery_request_generation": 0,
        "source_recovery_ack_generation": 0,
    }


def test_v2_request_generation_advances_for_each_recovery_signal(
    tmp_path: Path,
) -> None:
    adapter = _ready_adapter(tmp_path)

    adapter.request_certified_history_recovery()
    # A second detected reduction can arrive after the EA captured generation
    # 1 for replay.  It must advance the Windows-owned request to 2 so the EA
    # schedules a follow-up replay after it acknowledges 1.
    adapter.request_certified_history_recovery()
    assert json.loads(
        (adapter.files_dir / "source-recovery-request-v2.json").read_text(
            encoding="utf-8"
        )
    )["generation"] == 2

    _write_control(
        adapter.files_dir,
        "source-recovery-ack-v2.json",
        {"protocol_version": 2, "generation": 2, "from_unix": 0},
    )
    _write_v2_heartbeat(
        adapter,
        request_generation=2,
        ack_generation=2,
        sequence=2,
    )
    assert adapter.connection_state()["connected"] is True

    adapter.request_certified_history_recovery()
    assert json.loads(
        (adapter.files_dir / "source-recovery-request-v2.json").read_text(
            encoding="utf-8"
        )
    ) == {"protocol_version": 2, "generation": 3, "from_unix": 0}
    assert json.loads(
        (tmp_path / "state" / "source-recovery-request-v2-state.json").read_text(
            encoding="utf-8"
        )
    )["last_generation"] == 3


def test_direct_v2_request_after_clean_heartbeat_keeps_connection_gated(
    tmp_path: Path,
) -> None:
    adapter = _ready_adapter(tmp_path)
    # This models the critical interleaving: the EA has just emitted clean
    # generation 1, then Windows atomically writes a new request before the
    # watcher can publish that clean heartbeat remotely.
    _write_control(
        adapter.files_dir,
        "source-recovery-ack-v2.json",
        {"protocol_version": 2, "generation": 1, "from_unix": 0},
    )
    _write_v2_heartbeat(
        adapter,
        request_generation=1,
        ack_generation=1,
        sequence=11,
    )
    _write_control(
        adapter.files_dir,
        "source-recovery-request-v2.json",
        {"protocol_version": 2, "generation": 2, "from_unix": 0},
    )

    assert adapter.connection_state() == {
        "connected": False,
        "sequence": 11,
        "source_recovery_required": True,
        "source_recovery_protocol_version": 2,
        "source_recovery_request_generation": 1,
        "source_recovery_ack_generation": 1,
    }
    assert adapter.terminal_info().connected is False


@pytest.mark.parametrize(
    "name, contents, error",
    [
        ("source-recovery-request-v2.json", "{not json", "source_recovery_request_invalid"),
        (
            "source-recovery-request-v2.json",
            json.dumps({"protocol_version": 2, "generation": 1, "from_unix": 7}),
            "source_recovery_request_invalid",
        ),
        (
            "source-recovery-request-v2.json",
            json.dumps({"protocol_version": 2.0, "generation": 1, "from_unix": False}),
            "source_recovery_request_invalid",
        ),
        (
            "source-recovery-ack-v2.json",
            json.dumps({"protocol_version": 2, "generation": -1, "from_unix": 0}),
            "source_recovery_ack_invalid",
        ),
        (
            "source-recovery-ack-v2.json",
            json.dumps({"protocol_version": 2, "generation": 1}),
            "source_recovery_ack_invalid",
        ),
    ],
)
def test_invalid_v2_control_files_fail_closed(
    tmp_path: Path, name: str, contents: str, error: str
) -> None:
    adapter = _ready_adapter(tmp_path)
    (adapter.files_dir / name).write_text(contents, encoding="utf-8")

    with pytest.raises(Mql5FileAdapterError, match=error):
        adapter.connection_state()


def test_v2_control_file_symlink_fails_closed(tmp_path: Path) -> None:
    adapter = _ready_adapter(tmp_path)
    target = tmp_path / "outside-request.json"
    target.write_text(
        json.dumps({"protocol_version": 2, "generation": 1, "from_unix": 0}),
        encoding="utf-8",
    )
    (adapter.files_dir / "source-recovery-request-v2.json").symlink_to(target)

    with pytest.raises(Mql5FileAdapterError, match="source_recovery_request_invalid"):
        adapter.connection_state()


def test_legacy_heartbeat_never_receives_a_new_racy_v1_request(tmp_path: Path) -> None:
    adapter = _ready_adapter(tmp_path)
    _write(
        adapter.files_dir,
        "heartbeat.json",
        {
            "terminal_connected": True,
            "account_trade_allowed": False,
            "source_recovery_required": False,
        },
    )

    # v1 remains readable during a staged rollout, but Windows refuses to
    # write the old shared marker because that would reintroduce the loss race.
    assert adapter.connection_state()["connected"] is True
    with pytest.raises(Mql5FileAdapterError, match="source_recovery_protocol_unsupported"):
        adapter.request_certified_history_recovery()
    assert not (adapter.files_dir / "source-recovery-request-v2.json").exists()
    assert not (adapter.files_dir / "source-recovery-required.json").exists()


def test_existing_full_legacy_request_is_left_for_old_ea_to_finish(tmp_path: Path) -> None:
    adapter = _ready_adapter(tmp_path)
    _write(
        adapter.files_dir,
        "heartbeat.json",
        {
            "terminal_connected": True,
            "account_trade_allowed": False,
            "source_recovery_required": True,
        },
    )
    _write_control(
        adapter.files_dir,
        "source-recovery-required.json",
        {"continuity_version": 1, "from_unix": 0},
    )

    adapter.request_certified_history_recovery()
    assert json.loads(
        (adapter.files_dir / "source-recovery-required.json").read_text(
            encoding="utf-8"
        )
    ) == {"continuity_version": 1, "from_unix": 0}
    assert not (adapter.files_dir / "source-recovery-request-v2.json").exists()


def test_first_anchored_bundle_rejects_account_anchor_mismatch(tmp_path: Path) -> None:
    adapter = _ready_adapter(tmp_path)
    sequence = 17
    _write(
        adapter.files_dir,
        "heartbeat.json",
        {
            "terminal_connected": True,
            "account_trade_allowed": False,
            "source_recovery_required": False,
        },
        sequence=sequence,
    )
    _write(
        adapter.files_dir,
        "account.json",
        {
            "login": "42",
            "server": "Demo-Server",
            "balance": 100.0,
            "credit": 0.0,
            "equity": 100.0,
            "currency": "USD",
            "leverage": 100,
            "trade_allowed": False,
        },
        sequence=sequence,
    )
    _write(
        adapter.files_dir,
        "deals.json",
        {
            "anchor": {
                "balance": 99.0,
                "credit": 0.0,
                "coherent": True,
                "order_basis": "mt5_history_index_v1",
                "deal_count": 1,
                "last_deal_ticket": "3",
                "last_deal_time_msc": 1_000,
            },
            "deals": [
                {
                    "history_index": 0,
                    "ticket": "3",
                    "time_msc": 1_000,
                    "time": "2026-07-17T10:00:00Z",
                }
            ],
        },
        sequence=sequence,
    )
    with pytest.raises(Mql5FileAdapterError, match="history_anchor_account_mismatch"):
        adapter.history_ledger_bundle()


def test_delayed_archived_pending_order_callback_is_suppressed_after_handoff(tmp_path: Path) -> None:
    adapter = _ready_adapter(tmp_path)
    (tmp_path / "state").mkdir(exist_ok=True)
    (tmp_path / "state" / "history-live-handoff.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "job_id": "10000000-0000-4000-8000-000000000001",
                "connection_id": CONNECTION_ID,
                "archive_sha256": "a" * 64,
                "anchor_sequence": 7,
                "acknowledge_prefix": True,
                "archived_deal_tickets": ["10"],
                "archived_order_tickets": ["8001"],
            }
        ),
        encoding="utf-8",
    )
    _write(
        adapter.files_dir,
        "events/event-8.json",
        {
            "event_type": "HISTORY_FILLED",
            "ticket": "8001",
            "order_id": "8001",
            "symbol": "EURUSD",
            "order_type": 3,
            "order_state": 4,
        },
        sequence=8,
    )
    pending = adapter.pending_events()
    assert len(pending) == 1
    assert pending[0]["history_archived"] is True
    # The archive already contains its pending_order_filled lifecycle; this
    # delayed event is acked as source history but emits no second live event.
    assert _merge_event_stream_with_snapshot(pending, {}, {}) == []


def test_snapshot_filters_archived_deals_but_keeps_post_handoff_native_deal(tmp_path: Path) -> None:
    adapter = _ready_adapter(tmp_path)
    (tmp_path / "state").mkdir(exist_ok=True)
    (tmp_path / "state" / "history-live-handoff.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "job_id": "10000000-0000-4000-8000-000000000001",
                "connection_id": CONNECTION_ID,
                "archive_sha256": "a" * 64,
                "anchor_sequence": 7,
                "acknowledge_prefix": True,
                "archived_deal_tickets": ["3"],
                "archived_order_tickets": [],
            }
        ),
        encoding="utf-8",
    )
    _write(
        adapter.files_dir,
        "deals.json",
        [
            {
                "ticket": "3",
                "position_id": "1",
                "order_id": "2",
                "entry": "IN",
                "symbol": "EURUSD",
                "time": "2026-07-17T10:00:00Z",
            },
            {
                "ticket": "4",
                "position_id": "4",
                "order_id": "4",
                "entry": "IN",
                "symbol": "EURUSD",
                "time": "2026-07-17T10:01:00Z",
            },
        ],
    )

    snapshot = adapter.snapshot()
    # If the persisted baseline is lost after a restart, archived membership
    # still prevents deal #3 being rediscovered as a live deal_recorded. A
    # genuine N+1 deal is not hidden by that filter.
    assert set(snapshot["deals"]) == {"4"}
