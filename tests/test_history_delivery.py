import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from windows_agent import real_handlers
from windows_agent.real_handlers import _run_history_sync
from windows_agent.worker.history_balance import reconstruct_trade_deals
from windows_agent.worker.mql5_file_adapter import Mql5FileMt5Adapter
from windows_agent.worker.history_sync import HistorySync


def _deal(ticket, moment, deal_type, position, entry, **values):
    return {
        "ticket": str(ticket),
        "order_id": str(ticket + 100),
        "position_id": position,
        "symbol": "EURUSD" if position != "0" else "",
        "direction": "buy" if deal_type == 0 else "sell",
        "deal_type": deal_type,
        "entry": entry,
        "volume": values.get("volume", 0.1),
        "price": values.get("price", 1.1),
        "profit": values.get("profit", 0),
        "commission": values.get("commission", 0),
        "swap": values.get("swap", 0),
        "fee": values.get("fee", 0),
        "time_msc": moment,
        "time": datetime.fromtimestamp(moment / 1000, timezone.utc).isoformat(),
    }


def test_history_sync_delivers_only_safe_projection_through_lease_archive(
    tmp_path, monkeypatch
):
    base = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
    ledger = [
        _deal(1, base + 1_000, 2, "0", "IN", profit=1_000, volume=0),
        _deal(2, base + 2_000, 0, "A", "IN", commission=-1, volume=0.1, price=1.1),
        _deal(3, base + 3_000, 0, "A", "IN", commission=-1, volume=0.2, price=1.2),
        _deal(4, base + 4_000, 1, "A", "OUT", profit=12, commission=-1, volume=0.3),
        _deal(5, base + 5_000, 0, "B", "IN", commission=-1),
        _deal(6, base + 6_000, 1, "B", "INOUT", profit=5, commission=-1),
    ]
    anchor = {
        "balance": 1_012,
        "credit": 0,
        "coherent": True,
        "deal_count": len(ledger),
        "last_deal_ticket": "6",
        "last_deal_time_msc": base + 6_000,
    }
    enriched = reconstruct_trade_deals(ledger, anchor)

    class Adapter:
        history_snapshot_atomic = True

        @staticmethod
        def history_orders(_start, _end):
            return (
                {
                    "ticket": "order-1",
                    "symbol": "EURUSD",
                    "type": 0,
                    "volume_current": 0.3,
                    "price_open": 1.1,
                    "time": datetime.fromtimestamp(
                        (base + 1_500) / 1000, timezone.utc
                    ).isoformat(),
                },
            )

        @staticmethod
        def history_deals(start, end):
            return tuple(
                row
                for row in enriched
                if row["project_as_trade"]
                and start <= datetime.fromisoformat(row["time"]) < end
            )

        @staticmethod
        def history_accounting_deals(start, end):
            return tuple(
                row
                for row in ledger
                if start <= datetime.fromisoformat(row["time"]) < end
            )

        @staticmethod
        def history_anchor():
            return anchor

        @staticmethod
        def history_balance_rows():
            return enriched

    class Api:
        document = None

        def import_history_file(self, job_id, lease_id, document):
            self.document = document
            accepted = sum(len(group["events"]) for group in document["trades"])
            return {
                "api_version": "1",
                "accepted": accepted,
                "inserted": accepted,
                "duplicates": 0,
                "object_deleted": True,
            }

    # A regression to the normal live sink would fail this test immediately.
    monkeypatch.setattr(
        real_handlers,
        "TradingIngestionSink",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("live sink used")),
    )
    api = Api()
    job = {
        "job_id": "11111111-1111-4111-8111-111111111111",
        "lease_id": "22222222-2222-4222-8222-222222222222",
    }
    counts = _run_history_sync(
        Adapter(),
        tmp_path,
        "all_available",
        None,
        api,
        job,
        "33333333-3333-4333-8333-333333333333",
        "42",
        "Demo",
    )

    events = [event for group in api.document["trades"] for event in group["events"]]
    assert counts == {"orders": 1, "deals": 3, "accounting_deals": 6}
    # Orders are retained in the local audit only.  The lease archive contains
    # precisely projector-approved trade rows.
    assert all(event["event_type"] != "pending_order_created" for event in events)
    assert [event["event_type"] for event in events] == [
        "trade_opened",
        "trade_volume_changed",
        "trade_closed",
    ]
    assert {event["external_trade_id"] for event in events} == {"A"}
    assert events[0]["volume"] == 0.1
    assert events[1]["previous_volume"] == 0.1
    assert events[1]["volume"] == pytest.approx(0.3)
    accounting = (tmp_path / "data" / "history-accounting.jsonl").read_text().splitlines()
    assert len(accounting) == 6
    report = json.loads((tmp_path / "data" / "history-balance-backfill.json").read_text())
    assert report["entries"]["A"]["source"] == "mt5_historical_ledger"
    assert report["entries"]["B"]["source"] == "not_available"


def test_from_date_fails_closed_without_verified_broker_timezone(tmp_path):
    class Adapter:
        history_time_basis = "broker_server_unresolved"

    sync = HistorySync(Adapter(), tmp_path / "history.json", lambda _value: None)

    with pytest.raises(ValueError, match="historical_timezone_unavailable"):
        sync.run("from_date", datetime(2026, 1, 1, tzinfo=timezone.utc))


def test_file_snapshot_to_history_archive_end_to_end(tmp_path):
    files = tmp_path / "bridge"
    files.mkdir()
    generated_at = datetime.now(timezone.utc).isoformat()

    def write(name, payload):
        (files / name).write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "generated_at": generated_at,
                    "sequence": 7,
                    "account_identity": {"login": "42", "server": "Demo"},
                    "server_identity": "Demo",
                    "payload": payload,
                }
            ),
            encoding="utf-8",
        )

    base = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
    ledger = [
        _deal(1, base, 2, "0", "IN", profit=1_000, volume=0),
        _deal(2, base + 1_000, 0, "A", "IN", commission=-1),
        _deal(3, base + 2_000, 1, "A", "OUT", profit=10, commission=-1),
        # Non-trade accounting rows stay in the accounting ledger only.
        _deal(4, base + 3_000, 12, "A", "OUT", profit=0),
    ]
    for history_index, row in enumerate(ledger):
        row["history_index"] = history_index
    write("heartbeat.json", {"terminal_connected": True})
    write(
        "account.json",
        {
            "login": "42",
            "server": "Demo",
            "balance": 1_008,
            "equity": 1_008,
            "credit": 0,
            "currency": "USD",
            "leverage": 100,
            "trade_allowed": False,
        },
    )
    write(
        "history_orders.json",
        [
            {
                "ticket": "order-1",
                "symbol": "EURUSD",
                "type": 0,
                "volume_current": 0.1,
                "price_open": 1.1,
                "time": ledger[1]["time"],
            }
        ],
    )
    write("positions.json", [])
    write("orders.json", [])
    write(
        "deals.json",
        {
            "anchor": {
                "balance": 1_008,
                "credit": 0,
                "as_of": datetime.fromtimestamp(
                    (base + 4_000) / 1000, timezone.utc
                ).isoformat(),
                "coherent": True,
                "order_basis": "mt5_history_index_v1",
                "deal_count": len(ledger),
                "last_deal_ticket": "4",
                "last_deal_time_msc": base + 3_000,
            },
            "deals": ledger,
        },
    )
    adapter = Mql5FileMt5Adapter(
        files,
        "33333333-3333-4333-8333-333333333333",
        42,
        "Demo",
        tmp_path / "adapter-state",
    )

    class Api:
        document = None

        def import_history_file(self, _job_id, _lease_id, document):
            self.document = document
            accepted = sum(len(group["events"]) for group in document["trades"])
            return {
                "api_version": "1",
                "accepted": accepted,
                "inserted": accepted,
                "duplicates": 0,
                "object_deleted": True,
            }

    api = Api()
    _run_history_sync(
        adapter,
        tmp_path / "instance",
        "all_available",
        None,
        api,
        {
            "job_id": "11111111-1111-4111-8111-111111111111",
            "lease_id": "22222222-2222-4222-8222-222222222222",
        },
        "33333333-3333-4333-8333-333333333333",
        "42",
        "Demo",
    )

    events = [event for group in api.document["trades"] for event in group["events"]]
    assert [event["event_type"] for event in events] == ["trade_opened", "trade_closed"]
    assert events[0]["balance_before_open"] == 1_000
    assert {event["external_trade_id"] for event in events} == {"A"}
    accounting = (
        tmp_path / "instance" / "data" / "history-accounting.jsonl"
    ).read_text(encoding="utf-8")
    assert len(accounting.splitlines()) == 4
    handoff = json.loads(
        (tmp_path / "instance" / "state" / "history-handoff-pending.json").read_text(
            encoding="utf-8"
        )
    )
    assert handoff["archived_deal_tickets"] == ["1", "2", "3", "4"]
    assert handoff["imported_deal_tickets"] == ["2", "3"]
    assert handoff["anchor_sequence"] == 7


def test_history_handoff_retry_reuses_archive_bound_baseline_without_recapture(
    tmp_path,
):
    connection_id = "33333333-3333-4333-8333-333333333333"
    root = tmp_path / connection_id
    archive_path = root / "state" / "history-import-0123456789abcdef.json"
    archive_path.parent.mkdir(parents=True)
    document = {
        "schema_version": 1,
        "trades": [
            {
                "external_trade_id": "A",
                "events": [{"native_deal_ticket": "10"}],
            }
        ],
    }
    archive_path.write_text(json.dumps(document), encoding="utf-8")

    class InitialAdapter:
        @staticmethod
        def snapshot():
            return {
                "positions": {"A": {"volume": 0.2}},
                "orders": {},
                "deals": {"10": {"ticket": "10"}},
            }

        @staticmethod
        def checkpoint():
            return {"sequence": 7}

        @staticmethod
        def acknowledge_events(_sequence):
            pass

    real_handlers._persist_history_handoff_artifact(
        InitialAdapter(),
        root,
        job_id="11111111-1111-4111-8111-111111111111",
        connection_id=connection_id,
        archive_path=archive_path,
        archive_preexisting=False,
        document=document,
    )
    pending_path = root / "state" / "history-handoff-pending.json"
    original = pending_path.read_bytes()

    class RetryAdapter:
        @staticmethod
        def snapshot():
            raise AssertionError("a retry must not recapture current MT5 state")

        @staticmethod
        def checkpoint():
            raise AssertionError("a retry must reuse the archived anchor")

        @staticmethod
        def acknowledge_events(_sequence):
            pass

    real_handlers._persist_history_handoff_artifact(
        RetryAdapter(),
        root,
        job_id="11111111-1111-4111-8111-111111111111",
        connection_id=connection_id,
        archive_path=archive_path,
        archive_preexisting=True,
        document=document,
    )

    assert pending_path.read_bytes() == original


def test_history_handoff_retry_fails_closed_when_original_artifact_is_missing(
    tmp_path,
):
    connection_id = "33333333-3333-4333-8333-333333333333"
    root = tmp_path / connection_id
    archive_path = root / "state" / "history-import-0123456789abcdef.json"
    archive_path.parent.mkdir(parents=True)
    document = {"schema_version": 1, "trades": []}
    archive_path.write_text(json.dumps(document), encoding="utf-8")

    class Adapter:
        snapshot = staticmethod(
            lambda: (_ for _ in ()).throw(
                AssertionError("a committed archive must never recapture current state")
            )
        )
        checkpoint = staticmethod(
            lambda: (_ for _ in ()).throw(
                AssertionError("a committed archive must never move its boundary")
            )
        )
        acknowledge_events = staticmethod(lambda _sequence: None)

    with pytest.raises(
        real_handlers.HistorySyncFailed,
        match="history handoff artifact missing or mismatched",
    ):
        real_handlers._persist_history_handoff_artifact(
            Adapter(),
            root,
            job_id="11111111-1111-4111-8111-111111111111",
            connection_id=connection_id,
            archive_path=archive_path,
            archive_preexisting=True,
            document=document,
        )


@pytest.mark.parametrize(
    ("failure_point", "error_pattern"),
    [
        ("after_stage_write", "history import failed"),
        ("before_archive_promotion", "history archive publication failed"),
    ],
)
def test_history_publication_retry_reuses_prepared_boundary_without_recapture(
    tmp_path,
    monkeypatch,
    failure_point,
    error_pattern,
):
    connection_id = "33333333-3333-4333-8333-333333333333"
    root = tmp_path / connection_id
    job = {
        "job_id": "11111111-1111-4111-8111-111111111111",
        "lease_id": "22222222-2222-4222-8222-222222222222",
    }
    captures = {"snapshot": 0, "checkpoint": 0}
    history_reads = {
        "orders": 0,
        "deals": 0,
        "accounting": 0,
        "anchor": 0,
        "balance_rows": 0,
    }

    class Adapter:
        history_snapshot_atomic = True

        @staticmethod
        def history_orders(_start, _end):
            history_reads["orders"] += 1
            return ()

        @staticmethod
        def history_deals(_start, _end):
            history_reads["deals"] += 1
            return ()

        @staticmethod
        def history_accounting_deals(_start, _end):
            history_reads["accounting"] += 1
            return ()

        @staticmethod
        def history_anchor():
            history_reads["anchor"] += 1
            return None

        @staticmethod
        def history_balance_rows():
            history_reads["balance_rows"] += 1
            return ()

        @staticmethod
        def snapshot():
            captures["snapshot"] += 1
            if captures["snapshot"] != 1:
                raise AssertionError("promotion retry recaptured current MT5 state")
            return {"positions": {}, "orders": {}, "deals": {}}

        @staticmethod
        def checkpoint():
            captures["checkpoint"] += 1
            if captures["checkpoint"] != 1:
                raise AssertionError("promotion retry moved the frozen event boundary")
            return {"sequence": 7}

        acknowledge_events = staticmethod(lambda _sequence: None)

    class Api:
        calls = 0

        def import_history_file(self, _job_id, _lease_id, document):
            self.calls += 1
            accepted = sum(len(group["events"]) for group in document["trades"])
            return {
                "api_version": "1",
                "accepted": accepted,
                "inserted": accepted,
                "duplicates": 0,
                "object_deleted": True,
            }

    real_atomic_json = real_handlers.atomic_json
    real_replace = real_handlers.durable_replace

    def interrupt_after_stage_write(path, payload):
        real_atomic_json(path, payload)
        if Path(path).name.endswith(".json.staged"):
            pending_path = root / "state" / "history-handoff-pending.json"
            assert pending_path.is_file()
            raise OSError("simulated crash after staged archive write")

    def interrupt_promotion(source, destination):
        source_path = Path(source)
        destination_path = Path(destination)
        pending = json.loads(
            (root / "state" / "history-handoff-pending.json").read_text(
                encoding="utf-8"
            )
        )
        assert source_path.is_file()
        assert not destination_path.exists()
        assert pending["history_document"] == destination_path.name
        assert pending["history_document_sha256"] == hashlib.sha256(
            source_path.read_bytes()
        ).hexdigest()
        raise OSError("simulated crash before archive promotion")

    if failure_point == "after_stage_write":
        monkeypatch.setattr(real_handlers, "atomic_json", interrupt_after_stage_write)
    else:
        monkeypatch.setattr(real_handlers, "durable_replace", interrupt_promotion)
    api = Api()
    with pytest.raises(
        real_handlers.HistorySyncFailed,
        match=error_pattern,
    ):
        _run_history_sync(
            Adapter(),
            root,
            "all_available",
            None,
            api,
            job,
            connection_id,
            "42",
            "Demo",
        )

    staged = list((root / "state").glob(".history-import-*.json.staged"))
    assert len(staged) == 1
    assert not list((root / "state").glob("history-import-*.json"))
    pending_path = root / "state" / "history-handoff-pending.json"
    pending_before_retry = pending_path.read_bytes()
    staged_before_retry = staged[0].read_bytes()
    assert api.calls == 0

    monkeypatch.setattr(real_handlers, "atomic_json", real_atomic_json)
    monkeypatch.setattr(real_handlers, "durable_replace", real_replace)
    _run_history_sync(
        Adapter(),
        root,
        "all_available",
        None,
        api,
        job,
        connection_id,
        "42",
        "Demo",
    )

    archives = list((root / "state").glob("history-import-*.json"))
    assert len(archives) == 1
    assert archives[0].read_bytes() == staged_before_retry
    assert pending_path.read_bytes() == pending_before_retry
    assert captures == {"snapshot": 1, "checkpoint": 1}
    assert history_reads == {
        "orders": 1,
        "deals": 1,
        "accounting": 1,
        "anchor": 1,
        "balance_rows": 1,
    }
    assert api.calls == 1


def test_history_publication_retry_materializes_missing_stage_from_pending_only(
    tmp_path, monkeypatch
):
    connection_id = "33333333-3333-4333-8333-333333333333"
    root = tmp_path / connection_id
    job = {
        "job_id": "11111111-1111-4111-8111-111111111111",
        "lease_id": "22222222-2222-4222-8222-222222222222",
    }
    reads = {"history": 0, "snapshot": 0, "checkpoint": 0}

    class Adapter:
        history_snapshot_atomic = True

        @staticmethod
        def history_orders(_start, _end):
            reads["history"] += 1
            return ()

        @staticmethod
        def history_deals(_start, _end):
            reads["history"] += 1
            return ()

        @staticmethod
        def history_accounting_deals(_start, _end):
            reads["history"] += 1
            return ()

        @staticmethod
        def history_anchor():
            reads["history"] += 1
            return None

        @staticmethod
        def history_balance_rows():
            reads["history"] += 1
            return ()

        @staticmethod
        def snapshot():
            reads["snapshot"] += 1
            if reads["snapshot"] != 1:
                raise AssertionError("pending retry recaptured current MT5 state")
            return {"positions": {}, "orders": {}, "deals": {}}

        @staticmethod
        def checkpoint():
            reads["checkpoint"] += 1
            if reads["checkpoint"] != 1:
                raise AssertionError("pending retry moved the frozen event boundary")
            return {"sequence": 7}

        acknowledge_events = staticmethod(lambda _sequence: None)

    class Api:
        calls = 0

        def import_history_file(self, _job_id, _lease_id, document):
            self.calls += 1
            accepted = sum(len(group["events"]) for group in document["trades"])
            return {
                "api_version": "1",
                "accepted": accepted,
                "inserted": accepted,
                "duplicates": 0,
                "object_deleted": True,
            }

    real_atomic_json = real_handlers.atomic_json

    def interrupt_before_stage_write(path, payload):
        if Path(path).name.endswith(".json.staged"):
            assert (root / "state" / "history-handoff-pending.json").is_file()
            raise OSError("simulated crash before staged archive write")
        real_atomic_json(path, payload)

    monkeypatch.setattr(real_handlers, "atomic_json", interrupt_before_stage_write)
    api = Api()
    with pytest.raises(real_handlers.HistorySyncFailed, match="history import failed"):
        _run_history_sync(
            Adapter(),
            root,
            "all_available",
            None,
            api,
            job,
            connection_id,
            "42",
            "Demo",
        )

    pending_path = root / "state" / "history-handoff-pending.json"
    pending_before_retry = pending_path.read_bytes()
    assert not list((root / "state").glob(".history-import-*.json.staged"))
    assert not list((root / "state").glob("history-import-*.json"))

    monkeypatch.setattr(real_handlers, "atomic_json", real_atomic_json)
    _run_history_sync(
        Adapter(),
        root,
        "all_available",
        None,
        api,
        job,
        connection_id,
        "42",
        "Demo",
    )

    archives = list((root / "state").glob("history-import-*.json"))
    assert len(archives) == 1
    pending = json.loads(pending_before_retry)
    assert hashlib.sha256(archives[0].read_bytes()).hexdigest() == pending[
        "history_document_sha256"
    ]
    assert pending_path.read_bytes() == pending_before_retry
    assert reads == {"history": 5, "snapshot": 1, "checkpoint": 1}
    assert api.calls == 1


def test_stage_only_crash_is_rebuilt_with_one_new_atomic_boundary(tmp_path):
    connection_id = "33333333-3333-4333-8333-333333333333"
    root = tmp_path / connection_id
    job = {
        "job_id": "11111111-1111-4111-8111-111111111111",
        "lease_id": "22222222-2222-4222-8222-222222222222",
    }
    archive_key = hashlib.sha256(job["job_id"].encode("utf-8")).hexdigest()[:16]
    staged = root / "state" / f".history-import-{archive_key}.json.staged"
    staged.parent.mkdir(parents=True)
    # Simulate the superseded stage-first protocol crashing before it captured an artifact.
    staged.write_bytes(b"{}\n")
    captures = {"snapshot": 0, "checkpoint": 0}

    class Adapter:
        history_snapshot_atomic = True
        history_orders = staticmethod(lambda _start, _end: ())
        history_deals = staticmethod(lambda _start, _end: ())
        history_accounting_deals = staticmethod(lambda _start, _end: ())
        history_anchor = staticmethod(lambda: None)
        history_balance_rows = staticmethod(lambda: ())

        @staticmethod
        def snapshot():
            captures["snapshot"] += 1
            return {"positions": {}, "orders": {}, "deals": {}}

        @staticmethod
        def checkpoint():
            captures["checkpoint"] += 1
            return {"sequence": 7}

        acknowledge_events = staticmethod(lambda _sequence: None)

    class Api:
        @staticmethod
        def import_history_file(_job_id, _lease_id, document):
            accepted = sum(len(group["events"]) for group in document["trades"])
            return {
                "api_version": "1",
                "accepted": accepted,
                "inserted": accepted,
                "duplicates": 0,
                "object_deleted": True,
            }

    _run_history_sync(
        Adapter(),
        root,
        "all_available",
        None,
        Api(),
        job,
        connection_id,
        "42",
        "Demo",
    )

    archive = root / "state" / f"history-import-{archive_key}.json"
    pending = json.loads(
        (root / "state" / "history-handoff-pending.json").read_text(
            encoding="utf-8"
        )
    )
    assert archive.is_file()
    assert not staged.exists()
    assert archive.read_bytes() != b"{}\n"
    assert pending["history_document_payload"] == json.loads(
        archive.read_text(encoding="utf-8")
    )
    assert captures == {"snapshot": 1, "checkpoint": 1}


def test_activated_history_handoff_retry_never_rewinds_advanced_live_state(
    tmp_path,
):
    connection_id = "33333333-3333-4333-8333-333333333333"
    root = tmp_path / connection_id
    archive_path = root / "state" / "history-import-0123456789abcdef.json"
    archive_path.parent.mkdir(parents=True)
    document = {
        "schema_version": 1,
        "trades": [
            {
                "external_trade_id": "A",
                "events": [{"native_deal_ticket": "10"}],
            }
        ],
    }
    archive_path.write_text(json.dumps(document), encoding="utf-8")
    acknowledgements: list[int] = []

    class Adapter:
        @staticmethod
        def snapshot():
            return {
                "positions": {"A": {"volume": 0.2}},
                "orders": {},
                "deals": {"10": {"ticket": "10"}},
            }

        @staticmethod
        def checkpoint():
            return {"sequence": 7}

        @staticmethod
        def acknowledge_events(sequence):
            if acknowledgements:
                raise AssertionError("activated retry must not regress the event checkpoint")
            acknowledgements.append(sequence)

    adapter = Adapter()
    real_handlers._persist_history_handoff_artifact(
        adapter,
        root,
        job_id="11111111-1111-4111-8111-111111111111",
        connection_id=connection_id,
        archive_path=archive_path,
        archive_preexisting=False,
        document=document,
    )
    assert real_handlers._prepare_history_to_live_handoff(adapter, root) == 7

    advanced = {
        "positions": {"A": {"volume": 0.5}},
        "orders": {"later": {"ticket": "later"}},
        "deals": {},
    }
    real_handlers.PersistentSnapshot(root / "state" / "live_snapshot.json").save(advanced)
    advanced_bytes = (root / "state" / "live_snapshot.json").read_bytes()

    assert real_handlers._prepare_history_to_live_handoff(adapter, root) == 7
    assert acknowledgements == [7]
    assert (root / "state" / "live_snapshot.json").read_bytes() == advanced_bytes
