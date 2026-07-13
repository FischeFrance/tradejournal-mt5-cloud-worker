"""Contratto HTTP e scenario trading del fake bridge, esercitati su socket reali."""

from __future__ import annotations

import json

import pytest
import requests
from event_detector import detect_events


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _post(base_url: str, token: str, body=None):
    return requests.post(
        f"{base_url}/v1/trading/snapshot",
        json={} if body is None else body,
        headers=_headers(token),
        timeout=5,
    )


def _for_detector(payload: dict) -> dict:
    return {
        "positions": {item["ticket"]: item for item in payload["positions"]},
        "orders": {item["ticket"]: item for item in payload["orders"]},
        "deals": {item["deal_ticket"]: item for item in payload["deals"]},
    }


def test_trading_snapshot_requires_bearer_auth(fake_bridge_server):
    base_url, _token = fake_bridge_server
    response = requests.post(f"{base_url}/v1/trading/snapshot", json={}, timeout=5)
    assert response.status_code == 401
    assert response.json() == {
        "error": {
            "code": "unauthorized",
            "message": "Token di autenticazione mancante o non valido.",
        }
    }


def test_trading_snapshot_initial_payload_has_complete_account_and_empty_arrays(fake_bridge_server):
    base_url, token = fake_bridge_server
    response = _post(base_url, token)
    assert response.status_code == 200
    payload = response.json()
    assert payload["account"] == {
        "login": "123456",
        "server": "FakeBridge-Demo",
        "balance": 10000.0,
        "equity": 10010.0,
        "currency": "EUR",
        "leverage": 100,
    }
    assert payload["positions"] == []
    assert payload["orders"] == []
    assert payload["deals"] == []
    assert payload["generated_at"].endswith("Z")
    serialized = json.dumps(payload).lower()
    assert "password" not in serialized
    assert "token" not in serialized


@pytest.mark.parametrize("value", [0, -2, True, 1.5, "24", None])
def test_trading_snapshot_invalid_lookback_returns_structured_422(fake_bridge_server, value):
    base_url, token = fake_bridge_server
    response = _post(base_url, token, {"deal_lookback_hours": value})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_deal_lookback_hours"


def test_trading_snapshot_rejects_non_object_json_without_advancing_scenario(fake_bridge_server):
    base_url, token = fake_bridge_server
    response = requests.post(
        f"{base_url}/v1/trading/snapshot",
        data=b"[]",
        headers=_headers(token),
        timeout=5,
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
    assert _post(base_url, token).json()["positions"] == []


def test_trading_snapshot_invalid_json_returns_structured_400(fake_bridge_server):
    base_url, token = fake_bridge_server
    response = requests.post(
        f"{base_url}/v1/trading/snapshot",
        data=b"{not-json",
        headers=_headers(token),
        timeout=5,
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_json"


def test_fake_trading_scenario_produces_exact_event_sequence(fake_bridge_server):
    base_url, token = fake_bridge_server
    payloads = [_post(base_url, token, {"deal_lookback_hours": 9999}).json() for _ in range(8)]

    # Forma wire: array sempre presenti, ticket sempre stringa, timestamp sempre UTC/Z.
    for payload in payloads:
        assert isinstance(payload["positions"], list)
        assert isinstance(payload["orders"], list)
        assert isinstance(payload["deals"], list)
        assert payload["generated_at"].endswith("Z")
        for item in payload["positions"] + payload["orders"]:
            assert isinstance(item["ticket"], str)
        for deal in payload["deals"]:
            assert isinstance(deal["deal_ticket"], str)
            assert isinstance(deal["position_ticket"], str)
            assert deal["close_time"].endswith("Z")

    snapshots = [_for_detector(payload) for payload in payloads]
    events = []
    previous = {"positions": {}, "orders": {}, "deals": {}}
    for current in snapshots:
        events.extend(detect_events(previous, current))
        previous = current

    assert [event["event_type"] for event in events] == [
        "trade_opened",
        "trade_modified",
        "trade_modified",
        "trade_closed",
        "pending_order_created",
        "pending_order_modified",
        "pending_order_cancelled",
    ]
    assert events[1]["previous_stop_loss"] == 1.168
    assert events[1]["stop_loss"] == 1.169
    assert events[2]["previous_take_profit"] == 1.174
    assert events[2]["take_profit"] == 1.175
    assert events[3]["close_price"] == 1.172
    assert events[3]["profit"] == 20.0

    # Lo stato finale resta stabile: nessun nuovo evento e nessun reset implicito.
    final_payload = _post(base_url, token).json()
    assert detect_events(previous, _for_detector(final_payload)) == []


def test_trading_snapshot_does_not_create_trading_endpoints(fake_bridge_server):
    base_url, token = fake_bridge_server
    headers = _headers(token)
    for path in ("/v1/order_send", "/v1/order_check", "/v1/trading/order", "/v1/trading/close"):
        response = requests.post(f"{base_url}{path}", json={}, headers=headers, timeout=5)
        assert response.status_code == 404
