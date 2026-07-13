"""Configurazione/factory del client trade-sync e avanzamento sicuro del checkpoint."""

from __future__ import annotations

import pytest

from bridge_mt5_client import BridgeMt5Client
from config import ConfigError, load_config
from event_outbox import EventOutbox
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


def test_invalid_dry_run_boolean_fails_closed():
    with pytest.raises(ConfigError, match="DRY_RUN"):
        load_config({"DRY_RUN": "flase"})


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
    def __init__(self, results):
        self.results = list(results)
        self.payloads = []

    def send(self, payload):
        self.payloads.append(payload)
        result = self.results.pop(0)
        return result if isinstance(result, SendResult) else SendResult(status=result)


def test_transient_delivery_advances_snapshot_only_after_persistent_enqueue_and_survives_restart(
    tmp_path,
):
    snapshot_path = tmp_path / "snapshot.json"
    outbox_path = tmp_path / "outbox.json"
    store = SnapshotStore(str(snapshot_path))
    outbox = EventOutbox(str(outbox_path))
    current = _position_snapshot()
    account = {"login": "123456", "server": "Broker-Demo"}

    failed_sender = _Sender([
        SendResult(status="failed", error="http_503", failure_type="transient", attempts=3)
    ])
    assert _process_snapshot(current, account, store, failed_sender, outbox) is False
    assert store.get() == current
    assert outbox.pending_count() == 1

    # Riavvio simulato: snapshot e outbox sono istanze nuove lette dai file persistenti. La
    # transizione non viene rigenerata, ma l'evento pendente viene comunque drenato.
    restarted_store = SnapshotStore(str(snapshot_path))
    restarted_outbox = EventOutbox(str(outbox_path))
    successful_sender = _Sender(["sent"])
    assert _process_snapshot(
        current, account, restarted_store, successful_sender, restarted_outbox
    ) is True
    assert restarted_outbox.pending_count() == 0
    assert failed_sender.payloads[0]["event_id"] == successful_sender.payloads[0]["event_id"]


def test_dry_run_event_is_delivered_after_restart_with_dry_run_disabled(tmp_path):
    snapshot_path = tmp_path / "snapshot.json"
    outbox_path = tmp_path / "outbox.json"
    current = _position_snapshot()
    account = {"login": "123456", "server": "Broker-Demo"}
    dry_sender = _Sender([SendResult(status="dry_run")])

    assert _process_snapshot(
        current,
        account,
        SnapshotStore(str(snapshot_path)),
        dry_sender,
        EventOutbox(str(outbox_path)),
    ) is False
    assert EventOutbox(str(outbox_path)).pending_count() == 1

    real_sender = _Sender([SendResult(status="sent")])
    assert _process_snapshot(
        current,
        account,
        SnapshotStore(str(snapshot_path)),
        real_sender,
        EventOutbox(str(outbox_path)),
    ) is True
    assert dry_sender.payloads[0]["event_id"] == real_sender.payloads[0]["event_id"]


def test_partial_success_leaves_only_transient_event_pending(tmp_path):
    previous = {"positions": {}, "orders": {}, "deals": {}}
    current = _position_snapshot()
    second = dict(current["positions"]["1"])
    second["ticket"] = "2"
    current["positions"]["2"] = second
    store = SnapshotStore(str(tmp_path / "snapshot.json"))
    store.update(previous)
    outbox = EventOutbox(str(tmp_path / "outbox.json"))
    sender = _Sender([
        SendResult(status="sent"),
        SendResult(status="failed", error="network", failure_type="transient"),
    ])

    assert _process_snapshot(
        current,
        {"login": "123456", "server": "Broker-Demo"},
        store,
        sender,
        outbox,
    ) is False
    assert store.get() == current
    assert outbox.pending_count() == 1
    assert len(sender.payloads) == 2


def test_no_events_still_advances_snapshot():
    store = SnapshotStore()
    empty = {"positions": {}, "orders": {}, "deals": {}}
    sender = _Sender([])
    assert _process_snapshot(empty, {"login": "1", "server": "S"}, store, sender) is True
    assert store.get() == empty


def test_permanent_rejection_is_dead_lettered_and_does_not_block_snapshot(tmp_path):
    store = SnapshotStore(str(tmp_path / "snapshot.json"))
    outbox = EventOutbox(str(tmp_path / "outbox.json"))
    current = _position_snapshot()
    sender = _Sender([
        SendResult(
            status="failed",
            http_status=422,
            error="rejected_by_api",
            failure_type="permanent",
            attempts=1,
        )
    ])

    assert _process_snapshot(
        current,
        {"login": "123456", "server": "Broker-Demo"},
        store,
        sender,
        outbox,
    ) is True
    assert store.get() == current
    assert outbox.pending_count() == 0
    assert outbox.dead_letter_count() == 1

    # Un poll uguale e un riavvio non ritentano il rifiuto permanente all'infinito.
    restarted_sender = _Sender([])
    assert _process_snapshot(
        current,
        {"login": "123456", "server": "Broker-Demo"},
        SnapshotStore(str(tmp_path / "snapshot.json")),
        restarted_sender,
        EventOutbox(str(tmp_path / "outbox.json")),
    ) is True
    assert restarted_sender.payloads == []


def test_snapshot_is_not_advanced_if_outbox_enqueue_cannot_be_persisted(tmp_path):
    store = SnapshotStore(str(tmp_path / "snapshot.json"))
    outbox = EventOutbox(str(tmp_path / "outbox.json"))
    current = _position_snapshot()
    sender = _Sender([])

    def fail_persist(_state):
        raise OSError("disk full")

    outbox._persist = fail_persist

    with pytest.raises(OSError, match="disk full"):
        _process_snapshot(
            current,
            {"login": "123456", "server": "Broker-Demo"},
            store,
            sender,
            outbox,
        )

    assert store.get() == {"positions": {}, "orders": {}, "deals": {}}
    assert sender.payloads == []
