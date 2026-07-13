"""Test unitari del client HTTP trade-sync verso mt5-bridge."""

from __future__ import annotations

import copy
import logging

import pytest
import requests

from bridge_mt5_client import BridgeMt5Client
from event_detector import detect_events
from mt5_client import Mt5ConnectionError


HEALTH = {
    "status": "ok",
    "terminal_connected": True,
    "account_connected": True,
    "server": "Br****mo",
    "version": "fake-1.0",
}

SNAPSHOT = {
    "account": {
        "login": "123456",
        "server": "Broker-Demo",
        "balance": 10000.0,
        "equity": 10010.0,
        "currency": "EUR",
        "leverage": 100,
    },
    "positions": [{
        "ticket": "10001",
        "symbol": "EURUSD",
        "direction": "buy",
        "volume": 0.10,
        "open_price": 1.17000,
        "stop_loss": 1.16800,
        "take_profit": 1.17400,
        "open_time": "2026-07-13T10:00:00Z",
    }],
    "orders": [{
        "ticket": "20001",
        "symbol": "EURUSD",
        "direction": "buy",
        "volume": 0.10,
        "price": 1.16800,
        "stop_loss": 1.16600,
        "take_profit": 1.17200,
        "order_type": 2,
    }],
    "deals": [{
        "deal_ticket": "30001",
        "position_ticket": "10001",
        "close_price": 1.17200,
        "profit": 20.0,
        "commission": -0.5,
        "swap": 0.0,
        "close_time": "2026-07-13T10:15:00+00:00",
    }],
    "generated_at": "2026-07-13T10:15:01Z",
}


class _Response:
    def __init__(self, status_code, payload=None, json_error=None):
        self.status_code = status_code
        self._payload = payload
        self._json_error = json_error

    def json(self):
        if self._json_error:
            raise self._json_error
        return copy.deepcopy(self._payload)


def _client(**overrides):
    values = {
        "bridge_url": "http://mt5-bridge:8080/",
        "bridge_token": "private-bridge-token",
        "timeout_seconds": 2.0,
        "deal_lookback_hours": 24,
        "max_retries": 3,
        "backoff_base_seconds": 0.0,
        "sleep_fn": lambda _seconds: None,
    }
    values.update(overrides)
    return BridgeMt5Client(**values)


def _queue_requests(monkeypatch, responses):
    calls = []
    queue = list(responses)

    def fake_request(method, url, **kwargs):
        calls.append({"method": method, "url": url, **kwargs})
        response = queue.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    monkeypatch.setattr("bridge_mt5_client.requests.request", fake_request)
    return calls


def test_connect_health_then_snapshot_uses_one_post_and_caches_account(monkeypatch):
    calls = _queue_requests(monkeypatch, [_Response(200, HEALTH), _Response(200, SNAPSHOT)])
    client = _client(deal_lookback_hours=48)

    client.connect()
    snapshot = client.snapshot()
    account = client.account_info()
    positions = client.get_open_positions()
    orders = client.get_pending_orders()
    deals = client.get_recent_deals()

    assert len(calls) == 2
    assert calls[0]["method"] == "GET"
    assert calls[0]["url"] == "http://mt5-bridge:8080/health"
    assert calls[1]["method"] == "POST"
    assert calls[1]["url"] == "http://mt5-bridge:8080/v1/trading/snapshot"
    assert calls[1]["json"] == {"deal_lookback_hours": 48}
    assert all(call["headers"]["Authorization"] == "Bearer private-bridge-token" for call in calls)
    assert calls[1]["headers"]["Content-Type"] == "application/json"
    assert snapshot["positions"]["10001"]["ticket"] == "10001"
    assert positions == snapshot["positions"]
    assert orders["20001"]["order_type"] == 2
    assert deals["30001"]["position_ticket"] == "10001"
    assert account == SNAPSHOT["account"]


def test_empty_arrays_become_empty_dicts(monkeypatch):
    payload = copy.deepcopy(SNAPSHOT)
    payload.update({"positions": [], "orders": [], "deals": []})
    _queue_requests(monkeypatch, [_Response(200, HEALTH), _Response(200, payload)])
    client = _client()
    client.connect()
    assert client.snapshot() == {"positions": {}, "orders": {}, "deals": {}}


def test_cache_is_not_mutable_by_callers(monkeypatch):
    _queue_requests(monkeypatch, [_Response(200, HEALTH), _Response(200, SNAPSHOT)])
    client = _client()
    client.connect()
    snapshot = client.snapshot()
    snapshot["positions"]["10001"]["symbol"] = "MUTATED"
    account = client.account_info()
    account["server"] = "MUTATED"
    assert client.get_open_positions()["10001"]["symbol"] == "EURUSD"
    assert client.account_info()["server"] == "Broker-Demo"


def test_account_and_compatibility_methods_require_a_snapshot(monkeypatch):
    _queue_requests(monkeypatch, [_Response(200, HEALTH)])
    client = _client()
    client.connect()
    with pytest.raises(Mt5ConnectionError, match="snapshot"):
        client.account_info()
    with pytest.raises(Mt5ConnectionError, match="snapshot"):
        client.get_open_positions()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.pop("orders"),
        lambda p: p.update({"positions": {}}),
        lambda p: p["positions"][0].update({"ticket": 10001}),
        lambda p: p["positions"][0].update({"open_time": "2026-07-13T12:00:00+02:00"}),
        lambda p: p["account"].update({"password": "must-not-be-accepted"}),
        lambda p: p.update({"generated_at": "not-a-time"}),
        lambda p: p.update({"positions": [p["positions"][0], copy.deepcopy(p["positions"][0])]}),
    ],
    ids=["missing-field", "array-required", "ticket-string", "position-utc", "extra-secret", "generated-utc", "duplicate"],
)
def test_snapshot_schema_is_fully_validated(monkeypatch, mutate):
    payload = copy.deepcopy(SNAPSHOT)
    mutate(payload)
    _queue_requests(monkeypatch, [_Response(200, HEALTH), _Response(200, payload)])
    client = _client()
    client.connect()
    with pytest.raises(Mt5ConnectionError, match="Payload mt5-bridge non valido"):
        client.snapshot()


def test_malformed_json_is_rejected_without_retry(monkeypatch):
    calls = _queue_requests(
        monkeypatch,
        [_Response(200, HEALTH), _Response(200, json_error=ValueError("malformed"))],
    )
    client = _client()
    client.connect()
    with pytest.raises(Mt5ConnectionError, match="JSON"):
        client.snapshot()
    assert len(calls) == 2


def test_timeout_is_retried_up_to_the_limit(monkeypatch):
    calls = _queue_requests(
        monkeypatch,
        [requests.Timeout(), requests.Timeout(), _Response(200, HEALTH)],
    )
    client = _client(max_retries=3)
    client.connect()
    assert len(calls) == 3


def test_invalid_bridge_url_error_is_not_retried(monkeypatch):
    calls = _queue_requests(
        monkeypatch,
        [requests.exceptions.InvalidURL("bad url"), _Response(200, HEALTH)],
    )
    with pytest.raises(Mt5ConnectionError, match="nessun retry"):
        _client(max_retries=3).connect()
    assert len(calls) == 1


def test_generic_requests_transport_error_is_retried(monkeypatch):
    calls = _queue_requests(
        monkeypatch,
        [requests.RequestException("broken transport"), _Response(200, HEALTH)],
    )
    _client(max_retries=2).connect()
    assert len(calls) == 2


def test_5xx_is_retried_but_401_is_not(monkeypatch):
    calls = _queue_requests(monkeypatch, [_Response(503), _Response(200, HEALTH)])
    _client(max_retries=3).connect()
    assert len(calls) == 2

    calls = _queue_requests(monkeypatch, [_Response(401), _Response(200, HEALTH)])
    with pytest.raises(Mt5ConnectionError, match="non ritentabile"):
        _client(max_retries=3).connect()
    assert len(calls) == 1


def test_snapshot_5xx_retry_returns_one_state_and_cannot_duplicate_an_event(monkeypatch):
    position_only = copy.deepcopy(SNAPSHOT)
    position_only["orders"] = []
    position_only["deals"] = []
    calls = _queue_requests(
        monkeypatch,
        [_Response(200, HEALTH), _Response(503), _Response(200, position_only)],
    )
    client = _client(max_retries=3)
    client.connect()

    snapshot = client.snapshot()
    events = detect_events({"positions": {}, "orders": {}, "deals": {}}, snapshot)

    assert [call["method"] for call in calls] == ["GET", "POST", "POST"]
    assert [(event["event_type"], event["ticket"]) for event in events] == [
        ("trade_opened", "10001")
    ]


@pytest.mark.parametrize("status", [403, 404, 422])
def test_other_contract_4xx_are_not_retried(monkeypatch, status):
    calls = _queue_requests(monkeypatch, [_Response(status), _Response(200, HEALTH)])
    with pytest.raises(Mt5ConnectionError, match="non ritentabile"):
        _client(max_retries=3).connect()
    assert len(calls) == 1


def test_token_never_appears_in_retry_or_auth_logs(monkeypatch, caplog):
    secret = "a-very-private-bridge-token"
    _queue_requests(monkeypatch, [requests.Timeout(), _Response(401)])
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(Mt5ConnectionError):
            _client(bridge_token=secret, max_retries=2).connect()
    assert secret not in caplog.text


def test_constructor_rejects_unsafe_lookback():
    with pytest.raises(ValueError, match="1 e 168"):
        _client(deal_lookback_hours=0)
    with pytest.raises(ValueError, match="1 e 168"):
        _client(deal_lookback_hours=169)
