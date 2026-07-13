"""Test del contratto HTTP di bridge/fake/fake_bridge.py: un vero server HTTP in ascolto su
127.0.0.1 (fixture fake_bridge_server, vedi tests/conftest.py), interrogato con richieste HTTP
reali (requests), non un mock. Copre sia il comportamento "normale" sia gli scenari di errore che
il fake e' in grado di simulare (401, timeout, errore MT5, payload malformato, candela
duplicata), oltre alla garanzia che nessun endpoint di trading esista."""

from __future__ import annotations

import time

import requests


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def test_health_requires_auth(fake_bridge_server):
    base_url, _token = fake_bridge_server
    response = requests.get(f"{base_url}/health")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_health_with_valid_token(fake_bridge_server):
    base_url, token = fake_bridge_server
    response = requests.get(f"{base_url}/health", headers=_headers(token))
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["terminal_connected"] is True
    assert body["account_connected"] is True
    assert "server" in body and "version" in body


def test_candles_wrong_token_is_401(fake_bridge_server):
    base_url, _token = fake_bridge_server
    response = requests.post(
        f"{base_url}/v1/candles",
        json={"symbol": "EURUSD", "timeframe": "M1", "since": None, "limit": 5},
        headers=_headers("wrong-token"),
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_candles_happy_path_shape(fake_bridge_server):
    base_url, token = fake_bridge_server
    response = requests.post(
        f"{base_url}/v1/candles",
        json={"symbol": "EURUSD", "timeframe": "M5", "since": None, "limit": 5},
        headers=_headers(token),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["symbol"] == "EURUSD"
    assert body["timeframe"] == "M5"
    assert len(body["candles"]) == 5
    candle = body["candles"][0]
    for field in ("open_time", "open", "high", "low", "close", "tick_volume", "spread", "source"):
        assert field in candle
    assert isinstance(candle["open"], str)  # prezzi come stringhe decimali, mai numeri JSON
    assert candle["open_time"].endswith("Z")


def test_candles_are_chronologically_ordered(fake_bridge_server):
    base_url, token = fake_bridge_server
    response = requests.post(
        f"{base_url}/v1/candles",
        json={"symbol": "EURUSD", "timeframe": "M1", "since": None, "limit": 20},
        headers=_headers(token),
    )
    open_times = [c["open_time"] for c in response.json()["candles"]]
    assert open_times == sorted(open_times)


def test_candles_since_is_exclusive(fake_bridge_server):
    base_url, token = fake_bridge_server
    first = requests.post(
        f"{base_url}/v1/candles",
        json={"symbol": "EURUSD", "timeframe": "M1", "since": None, "limit": 3},
        headers=_headers(token),
    ).json()["candles"]
    checkpoint = first[-1]["open_time"]

    second = requests.post(
        f"{base_url}/v1/candles",
        json={"symbol": "EURUSD", "timeframe": "M1", "since": checkpoint, "limit": 3},
        headers=_headers(token),
    ).json()["candles"]

    assert all(c["open_time"] > checkpoint for c in second)
    assert checkpoint not in {c["open_time"] for c in second}


def test_candles_limit_is_respected_and_clamped(fake_bridge_server):
    base_url, token = fake_bridge_server
    response = requests.post(
        f"{base_url}/v1/candles",
        json={"symbol": "EURUSD", "timeframe": "M1", "since": None, "limit": 3},
        headers=_headers(token),
    )
    assert len(response.json()["candles"]) == 3

    response = requests.post(
        f"{base_url}/v1/candles",
        json={"symbol": "EURUSD", "timeframe": "M1", "since": None, "limit": 999999},
        headers=_headers(token),
    )
    assert len(response.json()["candles"]) <= 1000  # MAX_LIMIT, mai sforato


def test_candles_wrong_symbol_is_422(fake_bridge_server):
    base_url, token = fake_bridge_server
    response = requests.post(
        f"{base_url}/v1/candles",
        json={"symbol": "GBPUSD", "timeframe": "M1", "since": None, "limit": 5},
        headers=_headers(token),
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "unsupported_symbol"


def test_candles_wrong_timeframe_is_422(fake_bridge_server):
    base_url, token = fake_bridge_server
    response = requests.post(
        f"{base_url}/v1/candles",
        json={"symbol": "EURUSD", "timeframe": "M2", "since": None, "limit": 5},
        headers=_headers(token),
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "unsupported_timeframe"


def test_candles_invalid_json_body_is_400(fake_bridge_server):
    base_url, token = fake_bridge_server
    response = requests.post(f"{base_url}/v1/candles", data=b"{not-json", headers=_headers(token))
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_json"


def test_scenario_mt5_error_returns_structured_502(fake_bridge_server):
    base_url, token = fake_bridge_server
    headers = _headers(token)
    headers["X-Mt5-Fake-Scenario"] = "mt5_error"
    response = requests.post(
        f"{base_url}/v1/candles",
        json={"symbol": "EURUSD", "timeframe": "M1", "since": None, "limit": 5},
        headers=headers,
    )
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "mt5_error"


def test_scenario_malformed_payload_is_not_contract_compliant(fake_bridge_server):
    base_url, token = fake_bridge_server
    headers = _headers(token)
    headers["X-Mt5-Fake-Scenario"] = "malformed_payload"
    response = requests.post(
        f"{base_url}/v1/candles",
        json={"symbol": "EURUSD", "timeframe": "M1", "since": None, "limit": 5},
        headers=headers,
    )
    assert response.status_code == 200
    candle = response.json()["candles"][0]
    assert candle["open_time"] == "not-a-date"  # non un timestamp valido, deliberatamente
    assert isinstance(candle["open"], float)  # numero JSON, non stringa: viola il contratto


def test_scenario_duplicate_candle(fake_bridge_server):
    base_url, token = fake_bridge_server
    headers = _headers(token)
    headers["X-Mt5-Fake-Scenario"] = "duplicate_candle"
    response = requests.post(
        f"{base_url}/v1/candles",
        json={"symbol": "EURUSD", "timeframe": "M1", "since": None, "limit": 3},
        headers=headers,
    )
    candles = response.json()["candles"]
    assert candles[-1]["open_time"] == candles[-2]["open_time"]
    assert candles[-1] == candles[-2]


def test_scenario_timeout_blocks_for_at_least_the_configured_sleep(fake_bridge_server):
    base_url, token = fake_bridge_server
    headers = _headers(token)
    headers["X-Mt5-Fake-Scenario"] = "timeout"
    start = time.monotonic()
    response = requests.post(
        f"{base_url}/v1/candles",
        json={"symbol": "EURUSD", "timeframe": "M1", "since": None, "limit": 1},
        headers=headers,
        timeout=10,
    )
    elapsed = time.monotonic() - start
    assert elapsed >= 1.0
    assert response.status_code == 200  # risponde comunque, solo in ritardo


def test_current_incomplete_candle_is_excluded_via_now_override(fake_bridge_server):
    base_url, token = fake_bridge_server
    headers = _headers(token)
    # 'now' fissato a meta' della seconda candela M1 (indice 1: [60s, 120s) dall'epoca): la
    # candela in formazione (indice 1) non deve mai comparire nella risposta, solo l'indice 0.
    headers["X-Mt5-Bridge-Now-Override"] = "2026-01-01T00:01:30Z"
    response = requests.post(
        f"{base_url}/v1/candles",
        json={"symbol": "EURUSD", "timeframe": "M1", "since": None, "limit": 10},
        headers=headers,
    )
    candles = response.json()["candles"]
    assert len(candles) == 1
    assert candles[0]["open_time"] == "2026-01-01T00:00:00Z"


def test_no_trading_endpoints_exposed(fake_bridge_server):
    base_url, token = fake_bridge_server
    headers = _headers(token)
    for path in ("/v1/order_send", "/v1/order", "/v1/trade", "/v1/positions/close"):
        response = requests.post(f"{base_url}{path}", json={}, headers=headers)
        assert response.status_code == 404, f"{path} non dovrebbe esistere"
    # /v1/candles esiste solo in POST, non in GET.
    assert requests.get(f"{base_url}/v1/candles", headers=headers).status_code == 404
