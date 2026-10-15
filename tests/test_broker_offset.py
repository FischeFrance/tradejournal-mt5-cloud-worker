"""Broker clock offset measured from the Expert's heartbeat; TradeMove converts with it."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from windows_agent.real_handlers import _history_event
from windows_agent.worker.broker_offset import (
    MAX_MEASUREMENT_AGE,
    BrokerOffset,
    BrokerOffsetStore,
    measure_broker_offset,
)
from windows_agent.worker.mql5_file_adapter import Mql5FileMt5Adapter

UTC = timezone.utc
NOW = datetime(2026, 10, 15, 9, 0, 0, tzinfo=UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def test_a_fresh_server_clock_gives_whole_quarter_hours():
    assert measure_broker_offset(
        NOW, NOW + timedelta(hours=3, seconds=-40), terminal_connected=True
    ) == BrokerOffset(10800, NOW)
    assert measure_broker_offset(
        NOW, NOW - timedelta(hours=5), terminal_connected=True
    ) == BrokerOffset(-18000, NOW)


def test_a_stopped_or_disconnected_clock_is_not_trusted():
    # TimeCurrent stops at the last quote: ten minutes late, or Friday's close on a Sunday.
    assert measure_broker_offset(NOW, NOW + timedelta(hours=3, minutes=-10), terminal_connected=True) is None
    assert measure_broker_offset(NOW, NOW - timedelta(days=2), terminal_connected=True) is None
    assert measure_broker_offset(NOW, NOW + timedelta(hours=3), terminal_connected=False) is None


def test_event_fields_match_the_contract():
    assert BrokerOffset(7200, NOW).event_fields() == {
        "broker_utc_offset_seconds": 7200,
        "broker_offset_observed_at": "2026-10-15T09:00:00Z",
    }


def test_the_store_reuses_only_a_recent_valid_measurement(tmp_path: Path):
    store = BrokerOffsetStore(tmp_path / "broker-utc-offset.json")
    assert store.recent(NOW) is None
    store.save(BrokerOffset(10800, NOW))
    assert store.recent(NOW + timedelta(days=2)) == BrokerOffset(10800, NOW)
    assert store.recent(NOW + MAX_MEASUREMENT_AGE + timedelta(seconds=1)) is None
    store.path.write_text("{not json", encoding="utf-8")
    assert store.recent(NOW) is None


def _adapter(tmp_path: Path) -> Mql5FileMt5Adapter:
    return Mql5FileMt5Adapter(
        files_dir=tmp_path / "files",
        connection_id="connection-1",
        expected_login=12345,
        expected_server="Broker-Live",
        state_dir=tmp_path / "state",
    )


def _write_heartbeat(
    adapter: Mql5FileMt5Adapter, *, server_clock: datetime, server_time: Optional[datetime] = None
) -> None:
    payload = {
        "generated_at": _iso(server_clock),
        "sequence": 1,
        "terminal_connected": True,
        "account_trade_allowed": False,
    }
    if server_time is not None:
        payload["server_time"] = _iso(server_time)
    envelope = {
        "schema_version": 1,
        "generated_at": _iso(datetime.now(UTC)),
        "sequence": 1,
        "account_identity": {"login": "12345", "server": "Broker-Live"},
        "server_identity": "Broker-Live",
        "payload": payload,
    }
    adapter.files_dir.mkdir(parents=True, exist_ok=True)
    (adapter.files_dir / "heartbeat.json").write_text(json.dumps(envelope), encoding="utf-8")


def test_the_adapter_measures_on_the_heartbeat_and_remembers_it(tmp_path: Path):
    adapter = _adapter(tmp_path)
    now = datetime.now(UTC)
    _write_heartbeat(adapter, server_clock=now + timedelta(hours=3, seconds=-5))
    measured = adapter.broker_utc_offset()
    assert measured is not None and measured.seconds == 10800
    # Weekend: the last quote is Friday's and the stored measurement still answers.
    _write_heartbeat(adapter, server_clock=now - timedelta(days=2))
    assert adapter.broker_utc_offset() == measured


def test_the_adapter_prefers_the_trade_server_clock_when_present(tmp_path: Path):
    adapter = _adapter(tmp_path)
    now = datetime.now(UTC)
    _write_heartbeat(adapter, server_clock=now - timedelta(days=2), server_time=now + timedelta(hours=2))
    measured = adapter.broker_utc_offset()
    assert measured is not None and measured.seconds == 7200


def test_history_events_carry_the_measured_offset():
    row = {
        "ticket": "1",
        "order_id": "101",
        "position_id": "position-1",
        "symbol": "EURUSD",
        "direction": "buy",
        "deal_type": 0,
        "entry": "IN",
        "volume": 0.1,
        "price": 1.1,
        "profit": 0.0,
        "commission": -1.0,
        "swap": 0.0,
        "fee": 0.0,
        "time_msc": 1_000,
        "time": "2026-01-01T00:00:01Z",
    }
    without = _history_event({"kind": "deals", "record": row}, "42", "Demo")
    event = _history_event({"kind": "deals", "record": row}, "42", "Demo", BrokerOffset(10800, NOW))
    assert event["broker_utc_offset_seconds"] == 10800
    assert event["broker_offset_observed_at"] == "2026-10-15T09:00:00Z"
    assert event["event_id"] == without["event_id"]
    assert "broker_utc_offset_seconds" not in without
