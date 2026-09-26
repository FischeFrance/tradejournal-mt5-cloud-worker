from windows_agent.worker.history_balance import reconstruct_trade_deals


def _deal(ticket, deal_type, *, position="0", entry="IN", **values):
    return {
        "history_index": ticket - 1,
        "ticket": str(ticket),
        "order_id": str(ticket + 100),
        "position_id": str(position),
        "symbol": "XAUUSD" if position != "0" else "",
        "deal_type": deal_type,
        "entry": entry,
        "direction": values.pop("direction", "buy"),
        "volume": values.pop("volume", 1.0),
        "price": values.pop("price", 2_500.0),
        "profit": values.pop("profit", 0.0),
        "commission": values.pop("commission", 0.0),
        "swap": values.pop("swap", 0.0),
        "fee": values.pop("fee", 0.0),
        "time_msc": values.pop("time_msc", ticket * 1_000),
        "time": values.pop("time", f"2026-09-23T10:00:0{ticket}Z"),
        **values,
    }


def test_reconstructs_opening_balance_from_full_ledger_and_keeps_fee_audit_only():
    # Deposit is outside the trade's display period but is indispensable for
    # proving its denominator.  The current balance is the end anchor.
    projected = reconstruct_trade_deals(
        [
            _deal(1, 2, profit=1_000.0),
            _deal(2, 0, position="42", commission=-1.0, fee=-0.25),
            _deal(
                3,
                1,
                position="42",
                entry="OUT",
                direction="sell",
                profit=20.0,
                commission=-0.50,
                fee=-0.25,
            ),
        ],
        {"balance": 1_018.0, "credit": 0.0, "coherent": True},
    )

    opened, closed = projected
    assert opened["balance_before_open"] == 1_000.0
    # effective commission is native commission + DEAL_FEE exactly once;
    # fee remains present only for audit and must not be summed by clients.
    assert opened["commission"] == -1.25
    assert opened["fee"] == -0.25
    assert closed["commission"] == -0.75
    assert closed["fee"] == -0.25
    assert closed["total_commission"] == -2.0
    assert closed["commission_complete"] is True


def test_incoherent_anchor_never_invents_an_opening_balance():
    projected = reconstruct_trade_deals(
        [_deal(1, 0, position="42", commission=-1.0, fee=-0.25)],
        {"balance": 998.75, "credit": 0.0, "coherent": False},
    )

    assert projected[0]["balance_before_open"] is None
    assert projected[0]["balance_before_open_source"] == "not_available"
    assert projected[0]["balance_before_open_reason"] == "anchor_unavailable"


def test_partial_and_final_native_fills_remain_distinct_lifecycle_events():
    projected = reconstruct_trade_deals(
        [
            _deal(1, 0, position="42", volume=1.0, commission=-1.0),
            _deal(
                2,
                1,
                position="42",
                entry="OUT",
                direction="sell",
                volume=0.4,
                profit=10.0,
                commission=-0.4,
            ),
            _deal(
                3,
                1,
                position="42",
                entry="OUT",
                direction="sell",
                volume=0.6,
                profit=15.0,
                commission=-0.6,
            ),
        ],
        {"balance": 1_023.0, "credit": 0.0, "coherent": True},
    )

    assert [row["history_event_type"] for row in projected] == [
        "trade_opened",
        "trade_partial_closed",
        "trade_closed",
    ]
    assert "total_commission" not in projected[1]
    assert projected[2]["total_commission"] == -2.0
