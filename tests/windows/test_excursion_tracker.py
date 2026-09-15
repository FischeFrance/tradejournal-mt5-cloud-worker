from __future__ import annotations

from copy import deepcopy

from windows_agent.worker.excursion_tracker import PositionExcursionTracker


class MemoryStore:
    def __init__(self) -> None:
        self.value: dict = {}

    def get(self) -> dict:
        return deepcopy(self.value)

    def save(self, value: dict) -> None:
        self.value = deepcopy(value)


def _position(price: float, profit: float) -> dict:
    return {
        "symbol": "EURUSD",
        "direction": "buy",
        "open_price": 1.1000,
        "current_price": price,
        "floating_profit": profit,
        "point": 0.00001,
        "digits": 5,
    }


def test_tracker_persists_extrema_and_enriches_only_the_close_event() -> None:
    store = MemoryStore()
    tracker = PositionExcursionTracker(store, sample_ms=2_000)
    tracker.observe_open_events([
        {
            "event_type": "trade_opened",
            "ticket": "10",
            "symbol": "EURUSD",
            "direction": "buy",
            "open_price": 1.1000,
            "balance_before_open": 10_000,
        }
    ])
    tracker.observe_snapshot({"positions": {"10": _position(1.0980, -200)}})

    # Recreate the tracker to prove the extrema survive an Agent restart.
    tracker = PositionExcursionTracker(store, sample_ms=2_000)
    tracker.observe_snapshot({"positions": {"10": _position(1.1030, 300)}})
    close = {
        "event_type": "trade_closed",
        "ticket": "10",
        "direction": "buy",
        "close_price": 1.1010,
        "profit": 100,
        "close_time": "2026-09-16T12:00:00Z",
    }

    assert tracker.enrich_close_events([close]) == ["10"]
    assert round(close["mae_points"]) == 200
    assert round(close["mfe_points"]) == 300
    assert close["mae_money"] == 200
    assert close["mfe_money"] == 300
    assert close["mae_pct"] == 2
    assert close["mfe_pct"] == 3
    assert close["excursion_samples"] == 3
    assert close["excursion_sample_ms"] == 2_000
    assert close["excursion_source"] == "mt5_snapshot"

    tracker.discard(["10"])
    assert store.value["positions"] == {}


def test_short_position_inverts_price_excursion_direction() -> None:
    tracker = PositionExcursionTracker()
    tracker.observe_snapshot({
        "positions": {
            "11": {
                **_position(1.0975, 250),
                "direction": "sell",
            }
        }
    })
    state = tracker.positions["11"]
    assert round(state["mfe_points"]) == 250
    assert state["mae_points"] == 0


def test_missing_balance_keeps_percentages_unknown_instead_of_inventing_them() -> None:
    tracker = PositionExcursionTracker()
    tracker.observe_snapshot({"positions": {"12": _position(1.1010, 100)}})
    close = {
        "event_type": "trade_closed",
        "ticket": "12",
        "direction": "buy",
        "close_price": 1.1010,
        "profit": 100,
    }
    tracker.enrich_close_events([close])
    assert close["mae_pct"] is None
    assert close["mfe_pct"] is None


def test_snapshot_uses_stable_position_identifier_instead_of_mutable_ticket() -> None:
    tracker = PositionExcursionTracker()
    tracker.observe_snapshot({
        "positions": {
            "mutable-ticket": {
                **_position(1.1010, 100),
                "position_id": "stable-position-id",
            }
        }
    })
    close = {
        "event_type": "trade_closed",
        "ticket": "stable-position-id",
        "direction": "buy",
        "close_price": 1.1010,
        "profit": 100,
    }
    assert tracker.enrich_close_events([close]) == ["stable-position-id"]
    assert close["mfe_points"] > 0
