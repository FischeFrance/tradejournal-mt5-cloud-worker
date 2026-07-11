import copy

from event_detector import detect_events


def _empty():
    return {"positions": {}, "orders": {}, "deals": {}}


def test_trade_opened_detected_for_new_position():
    previous = _empty()
    current = _empty()
    current["positions"]["1"] = {
        "ticket": "1",
        "symbol": "EURUSD",
        "direction": "buy",
        "volume": 0.1,
        "open_price": 1.1000,
        "stop_loss": 1.0950,
        "take_profit": 1.1100,
        "open_time": "2026-01-01T00:00:00+00:00",
    }

    events = detect_events(previous, current)

    assert len(events) == 1
    assert events[0]["event_type"] == "trade_opened"
    assert events[0]["ticket"] == "1"
    assert events[0]["symbol"] == "EURUSD"
    assert events[0]["stop_loss"] == 1.0950
    assert events[0]["take_profit"] == 1.1100


def test_trade_modified_detected_for_stop_loss_change():
    previous = _empty()
    previous["positions"]["1"] = {
        "ticket": "1", "symbol": "EURUSD", "direction": "buy", "volume": 0.1,
        "open_price": 1.1000, "stop_loss": 1.0950, "take_profit": 1.1100,
    }
    current = _empty()
    current["positions"]["1"] = {**previous["positions"]["1"], "stop_loss": 1.0975}

    events = detect_events(previous, current)

    assert len(events) == 1
    event = events[0]
    assert event["event_type"] == "trade_modified"
    assert event["stop_loss"] == 1.0975
    assert event["previous_stop_loss"] == 1.0950
    assert event["previous_take_profit"] == 1.1100


def test_trade_modified_detected_for_take_profit_change():
    previous = _empty()
    previous["positions"]["1"] = {
        "ticket": "1", "symbol": "EURUSD", "direction": "buy", "volume": 0.1,
        "open_price": 1.1000, "stop_loss": 1.0950, "take_profit": 1.1100,
    }
    current = _empty()
    current["positions"]["1"] = {**previous["positions"]["1"], "take_profit": 1.1150}

    events = detect_events(previous, current)

    assert len(events) == 1
    event = events[0]
    assert event["event_type"] == "trade_modified"
    assert event["take_profit"] == 1.1150
    assert event["previous_take_profit"] == 1.1100


def test_trade_modified_emits_two_events_when_both_sl_and_tp_change():
    previous = _empty()
    previous["positions"]["1"] = {
        "ticket": "1", "symbol": "EURUSD", "direction": "buy", "volume": 0.1,
        "open_price": 1.1000, "stop_loss": 1.0950, "take_profit": 1.1100,
    }
    current = _empty()
    current["positions"]["1"] = {**previous["positions"]["1"], "stop_loss": 1.0975, "take_profit": 1.1150}

    events = detect_events(previous, current)

    assert len(events) == 2
    assert all(e["event_type"] == "trade_modified" for e in events)


def test_trade_closed_detected_and_enriched_with_deal_data():
    previous = _empty()
    previous["positions"]["1"] = {
        "ticket": "1", "symbol": "EURUSD", "direction": "buy", "volume": 0.1,
        "open_price": 1.1000, "stop_loss": 1.0950, "take_profit": 1.1100,
    }
    current = _empty()
    current["deals"]["500"] = {
        "position_ticket": "1",
        "close_price": 1.1120,
        "profit": 12.0,
        "commission": -0.5,
        "swap": -0.1,
        "close_time": "2026-01-01T01:00:00+00:00",
    }

    events = detect_events(previous, current)

    assert len(events) == 1
    event = events[0]
    assert event["event_type"] == "trade_closed"
    assert event["ticket"] == "1"
    assert event["close_price"] == 1.1120
    assert event["profit"] == 12.0
    assert event["commission"] == -0.5
    assert event["swap"] == -0.1


def test_pending_order_created_detected():
    previous = _empty()
    current = _empty()
    current["orders"]["2"] = {
        "ticket": "2", "symbol": "EURUSD", "direction": "buy", "volume": 0.05,
        "price": 1.0900, "stop_loss": 1.0850, "take_profit": 1.1000,
    }

    events = detect_events(previous, current)

    assert len(events) == 1
    assert events[0]["event_type"] == "pending_order_created"
    assert events[0]["price"] == 1.0900


def test_pending_order_modified_detected():
    previous = _empty()
    previous["orders"]["2"] = {
        "ticket": "2", "symbol": "EURUSD", "direction": "buy", "volume": 0.05,
        "price": 1.0900, "stop_loss": 1.0850, "take_profit": 1.1000,
    }
    current = _empty()
    current["orders"]["2"] = {**previous["orders"]["2"], "stop_loss": 1.0870}

    events = detect_events(previous, current)

    assert len(events) == 1
    event = events[0]
    assert event["event_type"] == "pending_order_modified"
    assert event["stop_loss"] == 1.0870
    assert event["previous_stop_loss"] == 1.0850


def test_pending_order_cancelled_detected():
    previous = _empty()
    previous["orders"]["2"] = {
        "ticket": "2", "symbol": "EURUSD", "direction": "buy", "volume": 0.05,
        "price": 1.0900, "stop_loss": 1.0850, "take_profit": 1.1000,
    }
    current = _empty()

    events = detect_events(previous, current)

    assert len(events) == 1
    assert events[0]["event_type"] == "pending_order_cancelled"


def test_pending_order_disappearing_into_a_position_is_not_a_cancellation():
    previous = _empty()
    previous["orders"]["2"] = {
        "ticket": "2", "symbol": "EURUSD", "direction": "buy", "volume": 0.05,
        "price": 1.0900, "stop_loss": 1.0850, "take_profit": 1.1000,
    }
    current = _empty()
    current["positions"]["2"] = {
        "ticket": "2", "symbol": "EURUSD", "direction": "buy", "volume": 0.05,
        "open_price": 1.0900, "stop_loss": 1.0850, "take_profit": 1.1000,
    }

    events = detect_events(previous, current)

    order_events = [e for e in events if e["event_type"] == "pending_order_cancelled"]
    assert order_events == []


def test_no_events_when_snapshot_unchanged():
    snapshot = _empty()
    snapshot["positions"]["1"] = {
        "ticket": "1", "symbol": "EURUSD", "direction": "buy", "volume": 0.1,
        "open_price": 1.1000, "stop_loss": 1.0950, "take_profit": 1.1100,
    }

    events = detect_events(snapshot, snapshot)

    assert events == []


def test_full_scenario_sequence_produces_all_seven_event_types():
    """Riproduce in miniatura l'intera sequenza richiesta dal mock, passo per passo."""
    state = _empty()
    all_events = []

    def step(mutate):
        nonlocal state
        previous = state
        current = {
            "positions": copy.deepcopy(previous["positions"]),
            "orders": copy.deepcopy(previous["orders"]),
            "deals": {},
        }
        mutate(current)
        events = detect_events(previous, current)
        all_events.extend(events)
        state = current

    step(lambda s: s["positions"].update({"1": {
        "ticket": "1", "symbol": "EURUSD", "direction": "buy", "volume": 0.1,
        "open_price": 1.1000, "stop_loss": 1.0950, "take_profit": 1.1100,
    }}))
    step(lambda s: s["positions"]["1"].update({"stop_loss": 1.0975}))
    step(lambda s: s["positions"]["1"].update({"take_profit": 1.1150}))
    step(lambda s: (s["positions"].pop("1"), s["deals"].update({"500": {
        "position_ticket": "1", "close_price": 1.1150, "profit": 15.0, "commission": -0.5,
        "swap": -0.1, "close_time": "2026-01-01T01:00:00+00:00",
    }})))
    step(lambda s: s["orders"].update({"2": {
        "ticket": "2", "symbol": "EURUSD", "direction": "buy", "volume": 0.05,
        "price": 1.0900, "stop_loss": 1.0850, "take_profit": 1.1000,
    }}))
    step(lambda s: s["orders"]["2"].update({"stop_loss": 1.0870}))
    step(lambda s: s["orders"].pop("2"))

    event_types = [e["event_type"] for e in all_events]
    assert event_types == [
        "trade_opened",
        "trade_modified",
        "trade_modified",
        "trade_closed",
        "pending_order_created",
        "pending_order_modified",
        "pending_order_cancelled",
    ]
