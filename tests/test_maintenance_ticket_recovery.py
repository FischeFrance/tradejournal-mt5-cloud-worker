from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from windows_agent.maintenance_ticket_recovery import TicketDeltaAdapter, load_baseline
from windows_agent.worker.history_sync import HistorySync


def adapter(rows):
    return SimpleNamespace(
        _historical_deal_snapshot=lambda: ({}, tuple(rows), tuple(rows)),
        history_orders=lambda *_args: ({"ticket": "1"}, {"ticket": "2"}),
        snapshot=lambda: {"deals": {r["ticket"]: r for r in rows}},
    )


def test_delta_recovers_cross_window_position_without_importing_unrelated_old_history(tmp_path):
    rows = [
        {"ticket": "10", "position_id": "100", "time": "2020-01-01T00:00:00Z"},
        {"ticket": "11", "position_id": "101", "time": "2020-01-01T00:00:00Z"},
        # Deliberately shifted broker clocks: native membership decides eligibility.
        {"ticket": "12", "position_id": "100", "time": "2099-01-01T00:00:00Z"},
        {"ticket": "13", "position_id": "0", "time": "2020-01-01T00:00:00Z"},
    ]
    delta = TicketDeltaAdapter(adapter(rows), {"deal_tickets": ["10", "11"], "order_tickets": ["1"]})
    delivered, accounting = [], []
    counts = HistorySync(delta, tmp_path / "history.json", delivered.append, accounting.append).run(
        "from_date", datetime.now(timezone.utc).replace(year=2025))
    assert counts == {"deals": 2, "orders": 1, "accounting_deals": 2}
    assert [e["record"]["ticket"] for e in delivered if e["kind"] == "deals"] == ["10", "12"]
    assert [e["record"]["ticket"] for e in accounting] == ["12", "13"]
    assert set(delta.snapshot()["deals"]) == {"10", "11", "12", "13"}


def test_unchanged_ledger_delivers_no_historical_trades():
    delta = TicketDeltaAdapter(adapter([{"ticket": "10", "position_id": "100"}]),
                              {"deal_tickets": ["10"], "order_tickets": ["1", "2"]})
    assert delta.history_deals() == ()
    assert delta.history_orders() == ()
    assert delta.history_accounting_deals() == ()


@pytest.mark.parametrize("baseline", [[], ["0"], ["10", "10"], ["10", "missing"]])
def test_invalid_or_incomplete_membership_cannot_authorize_full_history(baseline):
    value = {"deal_tickets": baseline, "order_tickets": []}
    if not baseline:
        # An empty baseline is valid only for a genuinely empty pre-stop ledger.
        value["deal_tickets"] = ["99"]
    with pytest.raises(ValueError):
        TicketDeltaAdapter(adapter([{"ticket": "10", "position_id": "100"}]), value)


def test_missing_baseline_never_falls_back_to_full_import(tmp_path):
    with pytest.raises(ValueError, match="maintenance_ticket_baseline_missing"):
        load_baseline(tmp_path, "connection", 42, "Demo")
