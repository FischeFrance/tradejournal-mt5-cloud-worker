from windows_agent.worker.history_balance import (
    build_balance_backfill_report,
    reconstruct_trade_deals,
)


def _deal(ticket, moment, deal_type, *, position="0", entry="IN", **economics):
    return {
        "ticket": str(ticket),
        "order_id": str(ticket + 100),
        "position_id": str(position),
        "symbol": "EURUSD" if position != "0" else "",
        "deal_type": deal_type,
        "entry": entry,
        "direction": "buy",
        "volume": economics.get("volume", 0.1),
        "price": economics.get("price", 1.1),
        "profit": economics.get("profit", 0),
        "commission": economics.get("commission", 0),
        "swap": economics.get("swap", 0),
        "fee": economics.get("fee", 0),
        "time_msc": moment,
        "time": "2026-01-01T00:00:00Z",
    }


def test_reconstructs_overlapping_positions_cash_flow_credit_and_full_costs():
    deals = [
        _deal(1, 1_000, 2, profit=1_000),
        _deal(2, 2_000, 0, position="A", commission=-2, fee=-0.5),
        _deal(3, 2_500, 0, position="A", commission=-1),  # scale-in
        _deal(4, 3_000, 1, position="B", commission=-1),
        _deal(5, 3_500, 3, profit=50),
        _deal(6, 4_000, 1, position="A", entry="OUT", profit=100, commission=-2, swap=-1, fee=-0.5),
        _deal(7, 5_000, 2, profit=-100),
        _deal(8, 6_000, 0, position="B", entry="OUT", profit=30, commission=-1),
        _deal(9, 6_500, 12, profit=0),
    ]
    projected = reconstruct_trade_deals(
        deals,
        {"balance": 1_021, "credit": 50, "coherent": True, "as_of": "2026-01-02T00:00:00Z"},
    )

    assert all(row["deal_type"] in (0, 1) for row in projected)
    openings = [
        row
        for row in projected
        if row["entry"] == "IN" and row["project_as_trade"]
    ]
    first_a = next(row for row in openings if row["ticket"] == "2")
    scale_a = next(row for row in openings if row["ticket"] == "3")
    first_b = next(row for row in openings if row["ticket"] == "4")
    assert first_a["balance_before_open"] == 1_000
    assert first_a["commission"] == -2.5
    assert first_a["volume"] == 0.1
    assert first_a["price"] == 1.1
    assert scale_a["project_as_trade"] is True
    assert scale_a["history_event_type"] == "trade_volume_changed"
    assert scale_a["previous_volume"] == 0.1
    assert scale_a["volume"] == 0.2
    assert scale_a["open_price"] == 1.1
    assert "balance_before_open" not in scale_a
    assert first_b["balance_before_open"] == 996.5
    closes = [row for row in projected if row["entry"] == "OUT"]
    assert closes[0]["history_event_type"] == "trade_partial_closed"
    assert "total_commission" not in closes[0]
    assert closes[1]["history_event_type"] == "trade_closed"
    assert closes[1]["total_commission"] == -2

    report = build_balance_backfill_report(
        projected,
        connection_id="connection",
        account_number="42",
        server="Demo",
        anchor={"balance": 1_021, "credit": 50, "coherent": True},
    )
    assert report["entries"]["A"]["balance_before_open"] == 1_000
    assert report["entries"]["A"]["source"] == "mt5_historical_ledger"


def test_marks_opening_unavailable_when_anchor_or_economics_are_not_provable():
    opening = _deal(2, 2_000, 0, position="A", commission=-2)
    incoherent = reconstruct_trade_deals(
        [opening], {"balance": 998, "credit": 0, "coherent": False}
    )
    assert incoherent[0]["balance_before_open"] is None
    assert incoherent[0]["balance_before_open_reason"] == "anchor_unavailable"

    ambiguous_cash_flow = _deal(3, 3_000, 2, profit=10)
    ambiguous_cash_flow.pop("fee")
    ambiguous = reconstruct_trade_deals(
        [opening, ambiguous_cash_flow],
        {"balance": 1_008, "credit": 0, "coherent": True},
    )
    assert ambiguous[0]["balance_before_open"] is None
    assert ambiguous[0]["balance_before_open_source"] == "not_available"


def test_cancelled_mt5_deal_invalidates_earlier_balance_reconstruction():
    rows = [
        _deal(1, 1_000, 2, profit=1_000),
        _deal(2, 2_000, 0, position="A", commission=-1),
        # MT5 has mutated the original execution into BUY_CANCELED with zero economics.
        _deal(3, 3_000, 13, position="cancelled", profit=0),
        # The separate compensation is visible, but the original effect that it reverses is not.
        _deal(4, 4_000, 2, profit=-25),
        _deal(5, 5_000, 0, position="B", commission=-1),
    ]
    projected = reconstruct_trade_deals(
        rows,
        {"balance": 973, "credit": 0, "coherent": True},
    )

    opening_a = next(row for row in projected if row["position_id"] == "A")
    opening_b = next(row for row in projected if row["position_id"] == "B")
    assert opening_a["balance_before_open"] is None
    assert opening_a["balance_before_open_reason"] == (
        "ledger_order_or_economics_incomplete"
    )
    assert opening_b["balance_before_open"] == 974


def test_does_not_project_inout_reversal_as_a_simple_close():
    rows = [
        _deal(1, 1_000, 0, position="A"),
        _deal(2, 2_000, 1, position="A", entry="INOUT", profit=5),
    ]
    projected = reconstruct_trade_deals(
        rows, {"balance": 1_005, "credit": 0, "coherent": True}
    )

    assert all(row["project_as_trade"] is False for row in projected)
    assert projected[0]["balance_before_open"] is None
    assert projected[0]["balance_before_open_reason"] == "inout_reversal_not_reconstructible"


def test_classifies_partial_fills_and_only_final_close_gets_total_costs():
    rows = [
        _deal(10, 1_000, 0, position="A", volume=1, commission=-2),
        _deal(11, 2_000, 1, position="A", entry="OUT", volume=0.4, profit=40, commission=-1),
        _deal(
            12,
            3_000,
            1,
            position="A",
            entry="OUT",
            volume=0.6,
            profit=60,
            commission=-1,
            fee=-0.5,
        ),
    ]

    projected = reconstruct_trade_deals(
        rows, {"balance": 1_095.5, "credit": 0, "coherent": True}
    )
    partial, final = projected[1], projected[2]

    assert partial["history_event_type"] == "trade_partial_closed"
    assert "total_commission" not in partial
    assert final["history_event_type"] == "trade_closed"
    assert final["total_commission"] == -4.5
    assert final["commission_complete"] is True


def test_keeps_only_exit_partial_while_position_remains_open():
    projected = reconstruct_trade_deals(
        [
            _deal(20, 1_000, 0, position="A", volume=1),
            _deal(21, 2_000, 1, position="A", entry="OUT", volume=0.4, profit=10),
        ],
        {"balance": 1_010, "credit": 0, "coherent": True},
    )

    assert projected[1]["history_event_type"] == "trade_partial_closed"


def test_reconstructs_scale_in_after_partial_close_from_sequential_ledger_state():
    projected = reconstruct_trade_deals(
        [
            _deal(25, 1_000, 0, position="A", volume=1, price=100, commission=-1),
            _deal(
                26,
                2_000,
                1,
                position="A",
                entry="OUT",
                volume=0.4,
                price=105,
                profit=10,
                commission=-0.4,
            ),
            _deal(27, 3_000, 0, position="A", volume=0.4, price=110, commission=-0.5),
            _deal(
                28,
                4_000,
                1,
                position="A",
                entry="OUT",
                volume=1,
                price=120,
                profit=20,
                commission=-0.6,
            ),
        ],
        {"balance": 1_027.5, "credit": 0, "coherent": True},
    )

    opened, partial, scale_in, final = projected
    assert all(row["project_as_trade"] is True for row in projected)
    assert opened["history_event_type"] == "trade_opened"
    assert opened["volume"] == 1
    assert opened["open_price"] == 100
    assert partial["history_event_type"] == "trade_partial_closed"
    assert scale_in["history_event_type"] == "trade_volume_changed"
    assert scale_in["partial_close"] is False
    assert scale_in["previous_volume"] == 0.6
    assert scale_in["volume"] == 1
    assert scale_in["open_price"] == 104
    assert final["history_event_type"] == "trade_closed"
    assert final["commission_complete"] is True
    assert final["total_commission"] == -2.5


def test_fails_closed_when_same_position_identifier_reopens_after_full_close():
    projected = reconstruct_trade_deals(
        [
            _deal(29, 1_000, 0, position="A", volume=1),
            _deal(30, 2_000, 1, position="A", entry="OUT", volume=1, profit=10),
            _deal(31, 3_000, 0, position="A", volume=0.5),
        ],
        {"balance": 1_010, "credit": 0, "coherent": True},
    )

    assert all(row["project_as_trade"] is False for row in projected)
    assert projected[0]["balance_before_open_reason"] == (
        "position_reopened_not_reconstructible"
    )


def test_fails_closed_when_exit_volume_exceeds_opening_volume():
    projected = reconstruct_trade_deals(
        [
            _deal(30, 1_000, 0, position="A", volume=1),
            _deal(31, 2_000, 1, position="A", entry="OUT", volume=1.1, profit=10),
        ],
        {"balance": 1_010, "credit": 0, "coherent": True},
    )

    assert all(row["project_as_trade"] is False for row in projected)
    assert projected[0]["balance_before_open_reason"] == "exit_volume_incoherent"


def test_ledger_sequence_remains_authoritative_when_broker_clock_moves_backwards():
    projected = reconstruct_trade_deals(
        [
            _deal(40, 2_000, 0, position="A", volume=1),
            _deal(41, 1_000, 1, position="A", entry="OUT", volume=1, profit=10),
        ],
        {"balance": 1_010, "credit": 0, "coherent": True},
    )

    assert projected[0]["balance_before_open"] == 1_000
    assert projected[1]["history_event_type"] == "trade_closed"


def test_attributes_separate_commission_when_position_link_is_exact():
    rows = [
        _deal(50, 1_000, 0, position="A", volume=1, commission=0),
        _deal(51, 1_500, 7, position="A", profit=-2.5, volume=0),
        _deal(52, 2_000, 1, position="A", entry="OUT", volume=1, profit=10),
    ]
    projected = reconstruct_trade_deals(
        rows, {"balance": 1_007.5, "credit": 0, "coherent": True}
    )
    final = next(row for row in projected if row.get("history_event_type") == "trade_closed")

    assert final["commission_complete"] is True
    assert final["total_commission"] == -2.5


def test_unlinked_separate_commission_marks_per_trade_economics_incomplete():
    rows = [
        _deal(60, 1_000, 0, position="A", volume=1, commission=0),
        _deal(61, 1_500, 8, position="0", profit=-2.5, volume=0),
        _deal(62, 2_000, 1, position="A", entry="OUT", volume=1, profit=10),
    ]
    projected = reconstruct_trade_deals(
        rows, {"balance": 1_007.5, "credit": 0, "coherent": True}
    )
    final = next(row for row in projected if row.get("history_event_type") == "trade_closed")

    assert final["commission_complete"] is False
    assert "total_commission" not in final
