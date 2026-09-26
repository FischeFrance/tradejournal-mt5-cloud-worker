import gzip
import json
from datetime import datetime, timezone

import pytest

from windows_agent import real_handlers
from windows_agent.agent_errors import HistorySyncFailed, SourceRecoveryRequired
from windows_agent.worker.historical_trade_import import (
    build_historical_pending_order_events,
    build_historical_trade_events,
)
from windows_agent.worker.dedup import PersistentDedup
from windows_agent.worker.history_file_import import build_history_archive
from windows_agent.worker.live_sync import LiveSync
from worker.event_outbox import EventOutbox
from windows_agent.real_handlers import (
    _activate_v2_history_handoff,
    _prime_live_state_after_initial_history,
    _run_control_plane_history_import,
    _run_live_sync_once,
)


def test_groups_multiple_deals_into_one_open_and_one_aggregate_close():
    deals = [
        {
            "ticket": "10", "position_id": "7", "symbol": "EURUSD",
            "entry": "IN", "direction": "buy", "volume": 0.4, "price": 1.1,
            "profit": 0, "commission": -0.4, "swap": 0,
            "time": "2026-01-01T10:00:00Z",
        },
        {
            "ticket": "11", "position_id": "7", "symbol": "EURUSD",
            "entry": "IN", "direction": "buy", "volume": 0.6, "price": 1.2,
            "profit": 0, "commission": -0.6, "swap": 0,
            "time": "2026-01-01T10:01:00Z",
        },
        {
            "ticket": "12", "position_id": "7", "symbol": "EURUSD",
            "entry": "OUT", "direction": "sell", "volume": 0.5, "price": 1.3,
            "profit": 5, "commission": -0.5, "swap": -0.2,
            "time": "2026-01-02T10:00:00Z",
        },
        {
            "ticket": "13", "position_id": "7", "symbol": "EURUSD",
            "entry": "OUT", "direction": "sell", "volume": 0.5, "price": 1.4,
            "profit": 7, "commission": -0.5, "swap": -0.3,
            "time": "2026-01-02T11:00:00Z",
        },
    ]

    events, counts = build_historical_trade_events(deals, "42", "Demo")

    assert counts == {"positions": 1, "events": 2, "skipped_deals": 0}
    opened, closed = events
    assert opened["event_type"] == "trade_opened"
    assert opened["external_trade_id"] == "7"
    assert opened["volume"] == 1.0
    assert abs(opened["open_price"] - 1.16) < 1e-12
    assert closed["event_type"] == "trade_closed"
    assert abs(closed["close_price"] - 1.35) < 1e-12
    assert closed["profit"] == 12
    assert closed["commission"] == -2
    assert closed["swap"] == -0.5


def test_legacy_fallback_folds_native_fee_into_commission_exactly_once():
    events, _counts = build_historical_trade_events(
        [
            {
                "ticket": "10", "position_id": "7", "symbol": "EURUSD",
                "entry": "IN", "direction": "buy", "volume": 1.0, "price": 1.1,
                "profit": 0, "commission": -1.0, "fee": -0.25, "swap": 0,
                "time": "2026-01-01T10:00:00Z",
            },
            {
                "ticket": "11", "position_id": "7", "symbol": "EURUSD",
                "entry": "OUT", "direction": "sell", "volume": 1.0, "price": 1.2,
                "profit": 10, "commission": -2.0, "fee": -0.50, "swap": -0.1,
                "time": "2026-01-01T11:00:00Z",
            },
        ],
        "42",
        "Demo",
    )
    closed = events[-1]
    assert closed["commission"] == -3.75
    assert closed["fee"] == -0.75
    # Consumers use profit + commission + swap, never fee a second time.
    assert closed["profit"] + closed["commission"] + closed["swap"] == 6.15


def test_projected_ledger_forwards_only_certified_opening_balance_provenance():
    events, _counts = build_historical_trade_events(
        [
            {
                "history_event_type": "trade_opened",
                "ticket": "10", "position_id": "7", "order_id": "8",
                "symbol": "EURUSD", "direction": "buy", "volume": 1.0,
                "price": 1.1, "open_price": 1.1, "profit": 0,
                "commission": -1.25, "fee": -0.25, "swap": 0,
                "balance_before_open": 1_000.0,
                "balance_before_open_source": "mt5_historical_ledger",
                "time_msc": 1_000, "history_index": 0,
                "time": "2026-01-01T10:00:00Z",
            }
        ],
        "42",
        "Demo",
    )
    assert events[0]["balance_before_open"] == 1_000.0
    assert events[0]["balance_before_open_source"] == "mt5_historical_ledger"


def test_skips_non_trade_balance_rows_and_close_without_predecessor():
    events, counts = build_historical_trade_events(
        [
            {"ticket": "1", "position_id": "0", "symbol": "", "entry": "IN"},
            {
                "ticket": "2", "position_id": "9", "symbol": "EURUSD",
                "entry": "OUT", "direction": "sell", "volume": 0.1,
                "price": 1.2, "time": "2026-01-02T11:00:00Z",
            },
        ],
        "42",
        "Demo",
    )

    assert events == []
    assert counts == {"positions": 0, "events": 0, "skipped_deals": 2}


def test_history_preserves_only_pending_orders_with_terminal_state():
    events, counts = build_historical_pending_order_events(
        [
            {
                "ticket": "8001", "symbol": "USDCAD", "type": 3, "state": 4,
                "volume_initial": 2.79, "price_open": 1.3782, "sl": 1.3787,
                "tp": 1.3767, "time_setup": "2026-09-09T12:39:00Z",
                "time_done": "2026-09-09T12:42:00Z",
            },
            {
                "ticket": "9001", "symbol": "USDCAD", "type": 0, "state": 4,
                "volume_initial": 2.79, "price_open": 1.3782,
                "time_setup": "2026-09-09T12:58:00Z",
                "time_done": "2026-09-09T12:58:00Z",
            },
        ],
        "42",
        "Demo",
    )

    assert counts == {"pending_orders": 1, "pending_events": 2, "skipped_orders": 1}
    assert [event["event_type"] for event in events] == [
        "pending_order_created", "pending_order_filled",
    ]
    assert all(event["external_trade_id"] == "8001" for event in events)
    assert all(event["order_type"] == "3" for event in events)


def test_control_plane_import_uploads_one_archive_and_resumes_idempotently(tmp_path):
    class Adapter:
        def history_orders(self, _start, _end):
            return ()

        def history_deals(self, _start, _end):
            return ({
                "ticket": "10", "position_id": "7", "symbol": "EURUSD",
                "entry": "IN", "direction": "buy", "volume": 0.1, "price": 1.1,
                "profit": 0, "commission": -0.1, "swap": 0,
                "time": "2026-01-01T10:00:00Z",
            },)

    class Api:
        def __init__(self):
            self.uploads = []
            self.imported = False

        def heartbeat(self, _job_id, _lease_id):
            return {"lease_valid": True}

        def history_file_prepare(self, _job_id, _lease_id, **metadata):
            self.metadata = metadata
            if self.imported:
                return {
                    "api_version": "1", "already_imported": True,
                    "object_path": "unused", "upload_url": None, "expires_in": 0,
                    "accepted": 1, "inserted": 1, "duplicates": 0,
                }
            return {
                "api_version": "1", "already_imported": False,
                "object_path": "unused", "upload_url": "https://upload.test/signed",
                "expires_in": 7200, "accepted": 0, "inserted": 0,
                "duplicates": 0,
            }

        def upload_history_file(self, _url, payload):
            self.uploads.append(payload)

        def history_file_import(self, _job_id, _lease_id, expected_count):
            self.imported = True
            return {
                "api_version": "1", "accepted": expected_count,
                "inserted": expected_count, "duplicates": 0,
                "object_deleted": True,
            }

    api = Api()
    job = {
        "job_id": "10000000-0000-4000-8000-000000000001",
        "connection_id": "20000000-0000-4000-8000-000000000002",
        "lease_id": "30000000-0000-4000-8000-000000000003",
    }
    first = _run_control_plane_history_import(
        Adapter(), tmp_path, api, job, "all_available", None, "42", "Demo"
    )
    second = _run_control_plane_history_import(
        Adapter(), tmp_path, api, job, "all_available", None, "42", "Demo"
    )

    assert first["delivered"] == 1
    assert second["delivered"] == 1
    assert len(api.uploads) == 1


def test_single_archive_contains_more_than_500_complete_trades(tmp_path):
    deals = [
        {
            "ticket": str(index), "position_id": str(index), "symbol": "EURUSD",
            "entry": "IN", "direction": "buy", "volume": 0.1, "price": 1.1,
            "time": "2026-01-01T10:00:00Z",
        }
        for index in range(1, 751)
    ]
    events, counts = build_historical_trade_events(deals, "42", "Demo")
    archive = build_history_archive(
        tmp_path,
        job_id="10000000-0000-4000-8000-000000000001",
        connection_id="20000000-0000-4000-8000-000000000002",
        account_number="42",
        server="Demo",
        history_mode="all_available",
        from_date=None,
        events=events,
    )
    document = json.loads(gzip.decompress(archive.compressed))

    assert counts["positions"] == 750
    assert archive.event_count == 750
    assert len(document["trades"]) == 750
    assert archive.path.name.endswith(".json.gz")


def test_initial_history_snapshot_is_the_first_live_sync_baseline(tmp_path):
    class Adapter:
        def __init__(self):
            self.events = ({"sequence": 7, "event_type": "DEAL_ADD"},)
            self.acknowledged_sequence = None
            self.current = {
                "positions": {},
                "orders": {},
                "deals": {
                    "10": {
                        "symbol": "EURUSD",
                        "direction": "buy",
                        "volume": 0.1,
                        "price": 1.1,
                        "profit": 3.0,
                        "commission": -0.1,
                        "swap": 0.0,
                        "time": "2026-01-01T10:00:00Z",
                    }
                },
            }

        def pending_events(self):
            return self.events

        def acknowledge_events(self, sequence):
            self.acknowledged_sequence = sequence
            self.events = ()

        def snapshot(self):
            return self.current

        def verify_identity(self):
            return {"login": 42, "server": "Demo"}

    adapter = Adapter()
    delivered = []

    _prime_live_state_after_initial_history(adapter, tmp_path)
    sent = _run_live_sync_once(adapter, tmp_path, delivered.append)

    assert adapter.acknowledged_sequence == 7
    assert (tmp_path / "state" / "live_snapshot.json").is_file()
    assert not (tmp_path / "state" / "live-snapshot.json").exists()
    assert sent == 0
    assert delivered == []


def test_history_archive_splits_five_native_lifecycle_events_and_reuses_exact_bytes(tmp_path):
    events = [
        {
            "event_id": f"native-{index}",
            "event_type": "trade_volume_changed" if index else "trade_opened",
            "external_trade_id": "77",
            "native_deal_ticket": str(100 + index),
        }
        for index in range(5)
    ]
    first = build_history_archive(
        tmp_path,
        job_id="10000000-0000-4000-8000-000000000011",
        connection_id="20000000-0000-4000-8000-000000000012",
        account_number="42",
        server="Demo",
        history_mode="all_available",
        from_date=None,
        events=events,
    )
    document = json.loads(gzip.decompress(first.compressed))
    assert [len(group["events"]) for group in document["trades"]] == [4, 1]
    assert [
        event["event_id"]
        for group in document["trades"]
        for event in group["events"]
    ] == [f"native-{index}" for index in range(5)]

    retry = build_history_archive(
        tmp_path,
        job_id="10000000-0000-4000-8000-000000000011",
        connection_id="20000000-0000-4000-8000-000000000012",
        account_number="42",
        server="Demo",
        history_mode="all_available",
        from_date=None,
        events=[],
    )
    assert retry.compressed == first.compressed
    assert retry.compressed_sha256 == first.compressed_sha256


class _V2HandoffAdapter:
    """Small V2-capable bridge fake: ledger/snapshot share one sequence."""

    def __init__(self) -> None:
        self.sequence = 7
        self.rows = [self._deal("10", "77", 0)]
        self.acknowledged: list[int] = []

    @staticmethod
    def _deal(ticket: str, position_id: str, index: int) -> dict:
        return {
            "ticket": ticket,
            "position_id": position_id,
            "order_id": ticket,
            "symbol": "EURUSD",
            "deal_type": 0,
            "entry": "IN",
            "direction": "buy",
            "volume": 0.1,
            "price": 1.1,
            "profit": 0.0,
            "commission": 0.0,
            "fee": 0.0,
            "swap": 0.0,
            "history_index": index,
            "time_msc": 1_000 + index,
            "time": "2026-01-01T10:00:00Z",
        }

    def history_ledger_bundle(self):
        return (
            {"balance": 1_000.0, "credit": 0.0, "coherent": True},
            tuple(dict(row) for row in self.rows),
            self.sequence,
        )

    def history_orders_bundle(self, _start, _end):
        return (), self.sequence

    def snapshot(self):
        return {
            "positions": {},
            "orders": {},
            "deals": {str(row["ticket"]): {} for row in self.rows},
        }

    def checkpoint(self):
        return {"sequence": self.sequence}

    def acknowledge_events(self, sequence):
        self.acknowledged.append(sequence)

    def add_second_frozen_deal(self) -> None:
        self.sequence = 8
        self.rows.append(self._deal("11", "88", 1))


class _V2BoundaryLiveAdapter(_V2HandoffAdapter):
    """Frozen N archive followed by one N+1 native open before activation."""

    def __init__(self) -> None:
        super().__init__()
        self.live_records: tuple[dict, ...] = ()
        self.live_snapshot = False

    def snapshot(self):
        if not self.live_snapshot:
            return super().snapshot()
        return {
            "positions": {
                "88": {
                    "ticket": "501",
                    "position_id": "88",
                    "symbol": "EURUSD",
                    "direction": "buy",
                    "volume": 0.2,
                    "open_price": 1.2,
                }
            },
            "orders": {},
            "deals": {str(row["ticket"]): {} for row in self.rows},
        }

    def publish_post_boundary_open(self) -> None:
        self.live_snapshot = True
        self.live_records = (
            {
                # Historical member at/below the frozen N. It must disappear
                # on activation rather than being mapped through live sync.
                "sequence": 7,
                "event_id": (
                    "00000000-0000-4000-8000-000000000001|42|Demo|"
                    "DEAL_ADD|10|7000|EURUSD|buy|0.10000000|1.10000000"
                ),
                "event_type": "DEAL_ADD",
                "ticket": "10",
                "deal_id": "10",
                "position_id": "77",
                "order_id": "10",
                "symbol": "EURUSD",
                "direction": "buy",
                "volume": 0.1,
                "price": 1.1,
                "entry": "IN",
                "time": "2026-09-24T10:03:00Z",
            },
            {
                "sequence": 8,
                "event_id": (
                    "00000000-0000-4000-8000-000000000001|42|Demo|"
                    "DEAL_ADD|11|8000|EURUSD|buy|0.20000000|1.20000000"
                ),
                "event_type": "DEAL_ADD",
                "ticket": "11",
                "deal_id": "11",
                "position_id": "88",
                "order_id": "11",
                "symbol": "EURUSD",
                "direction": "buy",
                "volume": 0.2,
                "price": 1.2,
                "entry": "IN",
                "profit": 0.0,
                "commission": -0.1,
                "fee": -0.05,
                "swap": 0.0,
                "time": "2026-09-24T10:04:00Z",
            },
        )

    def pending_events(self):
        through = max(self.acknowledged, default=0)
        return tuple(record for record in self.live_records if record["sequence"] > through)

    def verify_identity(self):
        return {"login": "42", "server": "Demo"}

    def connection_state(self):
        return {
            "connected": True,
            "sequence": 8 if self.live_snapshot else self.sequence,
            "source_recovery_required": False,
        }


class _BoundedHistoryAdapter(_V2HandoffAdapter):
    """One old native callback intentionally excluded from a from_date archive."""

    def __init__(self) -> None:
        super().__init__()
        self.records = (
            {
                "sequence": 7,
                "event_id": (
                    "00000000-0000-4000-8000-000000000001|42|Demo|"
                    "DEAL_ADD|10|7000|EURUSD|buy|0.10000000|1.10000000"
                ),
                "event_type": "DEAL_ADD",
                "ticket": "10",
                "deal_id": "10",
                "position_id": "77",
                "order_id": "10",
                "symbol": "EURUSD",
                "direction": "buy",
                "volume": 0.1,
                "price": 1.1,
                "entry": "IN",
                "profit": 0.0,
                "commission": -0.1,
                "fee": 0.0,
                "swap": 0.0,
                "time": "2026-01-01T10:00:00Z",
            },
        )

    def snapshot(self):
        return {
            "positions": {
                "77": {
                    "ticket": "77",
                    "position_id": "77",
                    "symbol": "EURUSD",
                    "direction": "buy",
                    "volume": 0.1,
                    "open_price": 1.1,
                }
            },
            "orders": {},
            "deals": {"10": {}},
        }

    def pending_events(self):
        through = max(self.acknowledged, default=0)
        return tuple(record for record in self.records if record["sequence"] > through)

    @staticmethod
    def verify_identity():
        return {"login": "42", "server": "Demo"}


class _V2SkippedNativeDealAdapter(_V2HandoffAdapter):
    """An INOUT deal is unsafe for archive projection but remains live source."""

    def __init__(self) -> None:
        super().__init__()
        reversal = self._deal("99", "99", 1)
        reversal["entry"] = "INOUT"
        self.rows.append(reversal)
        self.records = (
            {
                "sequence": 7,
                "event_id": (
                    "00000000-0000-4000-8000-000000000001|42|Demo|"
                    "DEAL_ADD|99|7001|EURUSD|buy|0.10000000|1.10000000"
                ),
                "event_type": "DEAL_ADD",
                "ticket": "99",
                "deal_id": "99",
                "position_id": "99",
                "order_id": "99",
                "symbol": "EURUSD",
                "direction": "buy",
                "volume": 0.1,
                "price": 1.1,
                "entry": "INOUT",
                "profit": 0.0,
                "commission": 0.0,
                "fee": 0.0,
                "swap": 0.0,
                "time": "2026-01-01T10:01:00Z",
            },
        )

    def pending_events(self):
        through = max(self.acknowledged, default=0)
        return tuple(record for record in self.records if record["sequence"] > through)

    @staticmethod
    def verify_identity():
        return {"login": "42", "server": "Demo"}


class _V2ArchiveApi:
    def __init__(self) -> None:
        self.imported: set[str] = set()
        self.uploads: list[bytes] = []

    def heartbeat(self, _job_id, _lease_id):
        return {"lease_valid": True}

    def history_file_prepare(self, job_id, _lease_id, **_metadata):
        if job_id in self.imported:
            return {
                "api_version": "1", "already_imported": True,
                "object_path": "unused", "upload_url": None, "expires_in": 0,
                "accepted": 1, "inserted": 1, "duplicates": 0,
            }
        return {
            "api_version": "1", "already_imported": False,
            "object_path": "unused", "upload_url": "https://upload.test/signed",
            "expires_in": 7200, "accepted": 0, "inserted": 0, "duplicates": 0,
        }

    def upload_history_file(self, _url, payload):
        self.uploads.append(payload)

    def history_file_import(self, job_id, _lease_id, expected_count):
        self.imported.add(job_id)
        return {
            "api_version": "1", "accepted": expected_count,
            "inserted": expected_count, "duplicates": 0,
            "object_deleted": True,
        }


def _v2_job(suffix: str) -> dict:
    return {
        "job_id": f"10000000-0000-4000-8000-0000000000{suffix}",
        "connection_id": "20000000-0000-4000-8000-000000000002",
        "lease_id": f"30000000-0000-4000-8000-0000000000{suffix}",
    }


def test_v2_orphan_archive_after_precommit_is_rebuilt_then_activated(tmp_path, monkeypatch):
    adapter = _V2HandoffAdapter()
    api = _V2ArchiveApi()
    job = _v2_job("21")
    original = real_handlers._persist_v2_history_handoff
    attempts = 0

    def crash_after_archive(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("simulated crash after archive write")
        return original(*args, **kwargs)

    monkeypatch.setattr(real_handlers, "_persist_v2_history_handoff", crash_after_archive)
    with pytest.raises(HistorySyncFailed):
        _run_control_plane_history_import(
            adapter, tmp_path, api, job, "all_available", None, "42", "Demo"
        )

    archive_path = tmp_path / "data" / "history-imports" / f"{job['job_id']}.json.gz"
    precommit_path = tmp_path / "state" / "history-archive-precommit.json"
    pending_path = tmp_path / "state" / "history-handoff-pending.json"
    assert archive_path.is_file()
    assert precommit_path.is_file()
    assert not pending_path.exists()

    result = _run_control_plane_history_import(
        adapter, tmp_path, api, job, "all_available", None, "42", "Demo"
    )
    assert result["delivered"] == 1
    assert attempts == 2
    assert not precommit_path.exists()
    assert pending_path.is_file()
    # The activation acknowledges only the frozen boundary. A later native
    # callback would remain outside this acknowledgement and live-deliver.
    assert _activate_v2_history_handoff(adapter, tmp_path, job) == 7
    assert adapter.acknowledged == [7]


def test_v2_handoff_rotates_completed_job_without_membership_rewind(tmp_path):
    adapter = _V2HandoffAdapter()
    api = _V2ArchiveApi()
    first_job = _v2_job("31")
    second_job = _v2_job("32")

    _run_control_plane_history_import(
        adapter, tmp_path, api, first_job, "all_available", None, "42", "Demo"
    )
    assert _activate_v2_history_handoff(adapter, tmp_path, first_job) == 7

    adapter.add_second_frozen_deal()
    _run_control_plane_history_import(
        adapter, tmp_path, api, second_job, "all_available", None, "42", "Demo"
    )
    assert _activate_v2_history_handoff(adapter, tmp_path, second_job) == 8
    active = json.loads(
        (tmp_path / "state" / "history-live-handoff.json").read_text()
    )
    assert active["job_id"] == second_job["job_id"]
    assert active["archived_deal_tickets"] == ["10", "11"]
    assert active["archived_order_tickets"] == []
    assert adapter.acknowledged == [7, 8]


def test_v2_active_handoff_rejects_a_pending_artifact_from_another_job_without_mutation(tmp_path):
    """A newer active boundary must never bless a stale pending archive.

    This is deliberately built from a real pending artifact A and a simulated
    later active commit B.  The rejection has to happen before precommit
    retirement, archive rotation, MT5 reads, or the caller's new_only switch.
    """
    adapter = _V2HandoffAdapter()
    api = _V2ArchiveApi()
    stale_job = _v2_job("51")
    active_job = _v2_job("52")

    _run_control_plane_history_import(
        adapter, tmp_path, api, stale_job, "all_available", None, "42", "Demo"
    )
    pending_path = tmp_path / "state" / "history-handoff-pending.json"
    active_path = tmp_path / "state" / "history-live-handoff.json"
    precommit_path = tmp_path / "state" / "history-archive-precommit.json"
    active_path.write_text(json.dumps({
        "schema_version": 1,
        "job_id": active_job["job_id"],
        "connection_id": active_job["connection_id"],
        "archive_sha256": "b" * 64,
        "anchor_sequence": 7,
        "archived_deal_tickets": ["10"],
        "archived_order_tickets": [],
        "acknowledge_prefix": True,
    }), encoding="utf-8")
    precommit_path.write_text(json.dumps({
        "schema_version": 1,
        "job_id": active_job["job_id"],
        "connection_id": active_job["connection_id"],
        "anchor_sequence": 7,
    }), encoding="utf-8")

    before = {
        "pending": pending_path.read_bytes(),
        "active": active_path.read_bytes(),
        "precommit": precommit_path.read_bytes(),
        "uploads": list(api.uploads),
    }
    with pytest.raises(HistorySyncFailed, match="mismatched pending"):
        _run_control_plane_history_import(
            adapter, tmp_path, api, active_job, "all_available", None, "42", "Demo"
        )

    assert pending_path.read_bytes() == before["pending"]
    assert active_path.read_bytes() == before["active"]
    assert precommit_path.read_bytes() == before["precommit"]
    assert api.uploads == before["uploads"]
    assert adapter.acknowledged == []


def test_v2_frozen_anchor_acks_only_n_and_delivers_post_boundary_n_plus_one_once(tmp_path):
    adapter = _V2BoundaryLiveAdapter()
    api = _V2ArchiveApi()
    job = _v2_job("35")
    _run_control_plane_history_import(
        adapter, tmp_path, api, job, "all_available", None, "42", "Demo"
    )
    pending = json.loads(
        (tmp_path / "state" / "history-handoff-pending.json").read_text()
    )
    assert pending["anchor_sequence"] == 7
    assert pending["archived_deal_tickets"] == ["10"]

    # This callback appears after the frozen archive read but before the
    # in-place new_only activation. It is deliberately N+1, therefore outside
    # the exact acknowledgement and must be delivered by normal live sync.
    adapter.publish_post_boundary_open()
    assert [record["sequence"] for record in adapter.pending_events()] == [7, 8]
    assert _activate_v2_history_handoff(adapter, tmp_path, job) == 7
    assert adapter.acknowledged == [7]
    assert [record["sequence"] for record in adapter.pending_events()] == [8]

    received: list[dict] = []
    dedup = PersistentDedup(tmp_path / "state" / "live-dedup.sqlite")
    try:
        sync = LiveSync(
            adapter,
            real_handlers.PersistentSnapshot(tmp_path / "state" / "live_snapshot.json"),
            dedup,
            received.append,
            outbox=EventOutbox(str(tmp_path / "state" / "live-outbox.json")),
        )
        assert sync.poll_once() == 1
        assert sync.poll_once() == 0
    finally:
        dedup.close()

    assert adapter.acknowledged == [7, 8]
    assert len(received) == 1
    assert received[0]["event_type"] == "trade_opened"
    assert received[0]["external_trade_id"] == "88"
    assert received[0]["commission"] == pytest.approx(-0.15)
    assert received[0]["fee"] == -0.05


def test_bounded_history_never_prefix_acks_callback_excluded_from_archive(tmp_path):
    adapter = _BoundedHistoryAdapter()
    api = _V2ArchiveApi()
    job = _v2_job("37")
    from_date = datetime(2026, 9, 1, tzinfo=timezone.utc)

    result = _run_control_plane_history_import(
        adapter, tmp_path, api, job, "from_date", from_date, "42", "Demo"
    )

    # The January opening is deliberately outside the requested archive
    # window. A V2 prefix ack at N=7 would delete this file; bounded imports
    # instead leave it live and create no archive membership/activation state.
    assert result["deals"] == 0
    assert adapter.acknowledged == []
    assert not (tmp_path / "state" / "history-handoff-pending.json").exists()
    assert not (tmp_path / "state" / "history-live-handoff.json").exists()
    assert [record["sequence"] for record in adapter.pending_events()] == [7]

    received: list[dict] = []
    dedup = PersistentDedup(tmp_path / "state" / "live-dedup.sqlite")
    try:
        sync = LiveSync(
            adapter,
            real_handlers.PersistentSnapshot(tmp_path / "state" / "live_snapshot.json"),
            dedup,
            received.append,
            outbox=EventOutbox(str(tmp_path / "state" / "live-outbox.json")),
        )
        assert sync.poll_once() == 1
        assert sync.poll_once() == 0
    finally:
        dedup.close()

    assert adapter.acknowledged == [7]
    assert len(received) == 1
    assert received[0]["event_type"] == "trade_opened"
    assert received[0]["external_trade_id"] == "77"


def test_v2_retains_unprojectable_native_deal_instead_of_prefix_acking_it(tmp_path):
    adapter = _V2SkippedNativeDealAdapter()
    api = _V2ArchiveApi()
    job = _v2_job("38")

    _run_control_plane_history_import(
        adapter, tmp_path, api, job, "all_available", None, "42", "Demo"
    )
    pending = json.loads(
        (tmp_path / "state" / "history-handoff-pending.json").read_text()
    )
    # #99 is an INOUT reversal: projection intentionally skips it, so it is
    # neither archive membership nor eligible for a destructive prefix ack.
    assert pending["archived_deal_tickets"] == ["10"]
    assert pending["acknowledge_prefix"] is False

    assert _activate_v2_history_handoff(adapter, tmp_path, job) == 7
    assert adapter.acknowledged == []
    assert [record["ticket"] for record in adapter.pending_events()] == ["99"]

    received: list[dict] = []
    dedup = PersistentDedup(tmp_path / "state" / "live-dedup.sqlite")
    try:
        sync = LiveSync(
            adapter,
            real_handlers.PersistentSnapshot(tmp_path / "state" / "live_snapshot.json"),
            dedup,
            received.append,
            outbox=EventOutbox(str(tmp_path / "state" / "live-outbox.json")),
        )
        assert sync.poll_once() == 1
    finally:
        dedup.close()

    assert adapter.acknowledged == [7]
    assert len(received) == 1
    assert received[0]["event_type"] == "deal_recorded"
    assert received[0]["external_trade_id"] == "99"


def test_v2_refuses_an_unstable_ledger_orders_snapshot_boundary(tmp_path):
    class UnstableAdapter(_V2HandoffAdapter):
        def history_orders_bundle(self, _start, _end):
            # Simulate an order mutation after the ledger's frozen sequence.
            return (), self.sequence + 1

    with pytest.raises(HistorySyncFailed, match="boundary unstable"):
        _run_control_plane_history_import(
            UnstableAdapter(),
            tmp_path,
            _V2ArchiveApi(),
            _v2_job("36"),
            "all_available",
            None,
            "42",
            "Demo",
        )
    assert not (tmp_path / "state" / "history-handoff-pending.json").exists()
    assert not (tmp_path / "data" / "history-imports").exists()


@pytest.mark.parametrize(
    ("state", "expected_error"),
    [
        (
            {"connected": False, "source_recovery_required": True},
            SourceRecoveryRequired,
        ),
        ({"connected": True}, HistorySyncFailed),
        ({"connected": True, "source_recovery_required": 0}, HistorySyncFailed),
    ],
)
def test_v2_activation_never_acks_or_retires_archive_without_clean_source_continuity(
    tmp_path, state, expected_error
):
    adapter = _V2HandoffAdapter()
    api = _V2ArchiveApi()
    job = _v2_job("41")
    _run_control_plane_history_import(
        adapter, tmp_path, api, job, "all_available", None, "42", "Demo"
    )
    adapter.connection_state = lambda: state

    with pytest.raises(expected_error) as exc_info:
        _activate_v2_history_handoff(adapter, tmp_path, job)

    if expected_error is SourceRecoveryRequired:
        assert exc_info.value.error_code == "source_recovery_required"

    assert adapter.acknowledged == []
    assert not (tmp_path / "state" / "history-live-handoff.json").exists()
    assert (tmp_path / "data" / "history-imports" / f"{job['job_id']}.json.gz").is_file()
