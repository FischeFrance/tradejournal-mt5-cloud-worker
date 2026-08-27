import gzip
import json

from windows_agent.worker.historical_trade_import import build_historical_trade_events
from windows_agent.worker.history_file_import import build_history_archive
from windows_agent.real_handlers import (
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
