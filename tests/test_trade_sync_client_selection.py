"""Configurazione/factory del client trade-sync e avanzamento sicuro del checkpoint."""

from __future__ import annotations

import pytest

from bridge_mt5_client import BridgeMt5Client
from config import ConfigError, load_config
from event_sender import SendResult
from main import _build_client, _process_snapshot
from mock_mt5_client import MockMt5Client
from mt5_client import RealMt5Client
from snapshot_store import SnapshotStore


def _bridge_env(**overrides):
    env = {
        "MOCK_MODE": "false",
        "MT5_BRIDGE_URL": "http://mt5-bridge:8080",
        "MT5_BRIDGE_TOKEN": "private-token",
    }
    env.update(overrides)
    return env


def test_legacy_mock_mode_true_selects_mock_by_default():
    config = load_config({})
    assert config.mt5_client_source == "mock"
    assert isinstance(_build_client(config), MockMt5Client)


def test_legacy_mock_mode_false_selects_bridge_by_default():
    config = load_config(_bridge_env())
    assert config.mt5_client_source == "bridge"
    assert isinstance(_build_client(config), BridgeMt5Client)


def test_explicit_direct_keeps_real_client_compatibility():
    config = load_config({
        "MOCK_MODE": "false",
        "MT5_CLIENT_SOURCE": "direct",
        "MT5_LOGIN": "123456",
        "MT5_PASSWORD": "investor-password",
        "MT5_SERVER": "Broker-Demo",
    })
    assert isinstance(_build_client(config), RealMt5Client)


def test_explicit_source_is_authoritative_over_legacy_mock_mode():
    bridge = load_config(_bridge_env(MOCK_MODE="true", MT5_CLIENT_SOURCE="bridge"))
    direct = load_config({"MOCK_MODE": "true", "MT5_CLIENT_SOURCE": "direct"})
    mock = load_config({"MOCK_MODE": "false", "MT5_CLIENT_SOURCE": "mock"})
    assert bridge.mt5_client_source == "bridge"
    assert direct.mt5_client_source == "direct"
    assert mock.mt5_client_source == "mock"


def test_invalid_source_and_missing_bridge_settings_fail_fast():
    with pytest.raises(ConfigError, match="MT5_CLIENT_SOURCE"):
        load_config({"MT5_CLIENT_SOURCE": "wine"})
    with pytest.raises(ConfigError, match="MT5_BRIDGE_URL"):
        load_config({"MOCK_MODE": "false", "MT5_BRIDGE_TOKEN": "tok"})
    with pytest.raises(ConfigError, match="MT5_BRIDGE_TOKEN"):
        load_config({"MOCK_MODE": "false", "MT5_BRIDGE_URL": "http://bridge"})


def test_bridge_token_must_be_distinct_from_ingestion_token_when_both_are_set():
    with pytest.raises(ConfigError, match="devono essere distinti"):
        load_config(_bridge_env(TRADEJOURNAL_BRIDGE_TOKEN="private-token"))


@pytest.mark.parametrize("lookback", ["0", "169", "not-an-int"])
def test_bridge_lookback_must_stay_in_safe_range(lookback):
    with pytest.raises(ConfigError, match="MT5_DEAL_LOOKBACK_HOURS"):
        load_config(_bridge_env(MT5_DEAL_LOOKBACK_HOURS=lookback))


def _position_snapshot(stop_loss=1.095):
    return {
        "positions": {"1": {
            "ticket": "1",
            "symbol": "EURUSD",
            "direction": "buy",
            "volume": 0.1,
            "open_price": 1.1,
            "stop_loss": stop_loss,
            "take_profit": 1.11,
            "open_time": "2026-01-01T00:00:00Z",
        }},
        "orders": {},
        "deals": {},
    }


class _Sender:
    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.payloads = []

    def send(self, payload):
        self.payloads.append(payload)
        return SendResult(status=self.statuses.pop(0))


def test_failed_delivery_does_not_advance_snapshot_and_retry_keeps_event_id():
    store = SnapshotStore()
    current = _position_snapshot()
    account = {"login": "123456", "server": "Broker-Demo"}

    failed_sender = _Sender(["failed"])
    assert _process_snapshot(current, account, store, failed_sender) is False
    assert store.get() == {"positions": {}, "orders": {}, "deals": {}}

    successful_sender = _Sender(["sent"])
    assert _process_snapshot(current, account, store, successful_sender) is True
    assert store.get() == current
    assert failed_sender.payloads[0]["event_id"] == successful_sender.payloads[0]["event_id"]


def test_partial_success_does_not_advance_snapshot():
    previous = _position_snapshot(stop_loss=1.095)
    current = _position_snapshot(stop_loss=1.097)
    current["positions"]["1"]["take_profit"] = 1.115
    store = SnapshotStore()
    store.update(previous)
    sender = _Sender(["sent", "failed"])

    assert _process_snapshot(
        current,
        {"login": "123456", "server": "Broker-Demo"},
        store,
        sender,
    ) is False
    assert store.get() == previous
    assert len(sender.payloads) == 2


def test_no_events_still_advances_snapshot():
    store = SnapshotStore()
    empty = {"positions": {}, "orders": {}, "deals": {}}
    sender = _Sender([])
    assert _process_snapshot(empty, {"login": "1", "server": "S"}, store, sender) is True
    assert store.get() == empty
