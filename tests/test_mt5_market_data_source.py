"""Test di Mt5MarketDataSource (worker/market_data_source.py): client HTTP verso mt5-bridge.

Usa server HTTP reali (socket veri, nessun mock di `requests`): la fixture fake_bridge_server
(vedi tests/conftest.py) per il percorso felice/autenticazione, e un piccolo server ad-hoc
(_AdHocServer, sotto) per gli scenari che il client di produzione non puo' pilotare tramite il
fake bridge (che si comporta diversamente solo dietro header di test mai inviati dal client
reale): retry su 5xx, timeout, payload malformato a livello di trasporto."""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from market_data_source import (
    Mt5BridgeAuthError,
    Mt5BridgeError,
    Mt5MarketDataSource,
    build_market_data_source,
)


def _json_bytes(obj) -> bytes:
    return json.dumps(obj).encode("utf-8")


def _valid_candle(open_time="2026-01-01T00:05:00Z", open_="1.17001", high="1.17045", low="1.16990", close="1.17030"):
    return {
        "open_time": open_time, "open": open_, "high": high, "low": low, "close": close,
        "tick_volume": 122, "spread": 8, "source": "mt5",
    }


class _AdHocServer:
    """Server HTTP minimale con una coda di risposte (status, delay_seconds, raw_body_bytes)
    servite in ordine, una per richiesta ricevuta: permette di simulare sequenze come "due 500
    poi un 200" o "una risposta lentissima" senza scrivere un handler dedicato per ogni test."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                content_length = int(self.headers.get("Content-Length", "0") or "0")
                raw_body = self.rfile.read(content_length) if content_length else b""
                outer.requests.append({
                    "path": self.path,
                    "headers": dict(self.headers.items()),
                    "body": json.loads(raw_body) if raw_body else None,
                })
                if outer._responses:
                    status, delay, body = outer._responses.pop(0)
                else:
                    status, delay, body = 500, 0, _json_bytes({"error": {"code": "exhausted", "message": "no more"}})
                if delay:
                    time.sleep(delay)
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def __enter__(self) -> "_AdHocServer":
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self._server.shutdown()
        self._server.server_close()


def _client(base_url: str, token: str = "test-token", **kwargs) -> Mt5MarketDataSource:
    kwargs.setdefault("sleep_fn", lambda _seconds: None)  # niente attese reali nei test di retry
    kwargs.setdefault("timeout_seconds", 5.0)
    return Mt5MarketDataSource(bridge_url=base_url, bridge_token=token, **kwargs)


# --- percorso felice / autenticazione, contro il fake bridge reale -------------------------

def test_get_candles_against_real_fake_bridge(fake_bridge_server):
    base_url, token = fake_bridge_server
    client = _client(base_url, token=token)
    candles = client.get_candles("EURUSD", "M1", since=None, limit=5)
    assert len(candles) == 5
    assert all(isinstance(c.open, Decimal) for c in candles)
    assert candles == sorted(candles, key=lambda c: c.open_time)


def test_wrong_token_raises_auth_error_without_retry(fake_bridge_server):
    base_url, _token = fake_bridge_server
    client = _client(base_url, token="wrong-token", max_retries=3)
    with pytest.raises(Mt5BridgeAuthError):
        client.get_candles("EURUSD", "M1", since=None, limit=5)


def test_wrong_broker_symbol_raises_without_retry(fake_bridge_server):
    base_url, token = fake_bridge_server
    client = _client(base_url, token=token, max_retries=3)
    with pytest.raises(Mt5BridgeError):
        client.get_candles("GBPUSD", "M1", since=None, limit=5)  # fake accetta solo EURUSD


def test_invalid_timeframe_raises_without_network_call(fake_bridge_server):
    base_url, token = fake_bridge_server
    client = _client(base_url, token=token)
    with pytest.raises(ValueError):
        client.get_candles("EURUSD", "M2", since=None, limit=5)


def test_limit_zero_returns_empty_without_network_call(fake_bridge_server):
    base_url, token = fake_bridge_server
    client = _client(base_url, token=token)
    assert client.get_candles("EURUSD", "M1", since=None, limit=0) == []


# --- request wire format ---------------------------------------------------------------------

def test_sends_bearer_token_and_broker_symbol_in_request():
    with _AdHocServer([(200, 0, _json_bytes({"symbol": "EURUSD", "timeframe": "M1", "candles": [_valid_candle()]}))]) as server:
        client = _client(server.url, token="my-secret-token")
        client.get_candles("EURUSD", "M1", since=None, limit=10)
        assert server.requests[0]["headers"]["Authorization"] == "Bearer my-secret-token"
        assert server.requests[0]["body"] == {"symbol": "EURUSD", "timeframe": "M1", "since": None, "limit": 10}


def test_sends_since_as_utc_z_suffix_string():
    with _AdHocServer([(200, 0, _json_bytes({"symbol": "EURUSD", "timeframe": "M1", "candles": []}))]) as server:
        client = _client(server.url)
        since = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)
        client.get_candles("EURUSD", "M1", since=since, limit=10)
        assert server.requests[0]["body"]["since"] == "2026-01-01T10:00:00Z"


# --- parsing / Decimal / UTC / OHLC -----------------------------------------------------------

def test_parses_prices_as_decimal_from_strings_exactly():
    body = {"symbol": "EURUSD", "timeframe": "M1", "candles": [_valid_candle(open_="1.17001")]}
    with _AdHocServer([(200, 0, _json_bytes(body))]) as server:
        client = _client(server.url)
        candles = client.get_candles("EURUSD", "M1", since=None, limit=10)
        assert candles[0].open == Decimal("1.17001")
        assert str(candles[0].open) == "1.17001"  # nessun artefatto binario (vedi anche sotto)


def test_price_as_json_number_is_rejected_not_silently_accepted():
    # Un prezzo inviato come numero JSON invece che stringa violerebbe "Decimal senza float
    # intermedi": deve essere un errore esplicito, non una conversione silenziosa via float.
    candle = _valid_candle()
    candle["open"] = 1.17001  # numero JSON, non stringa
    body = {"symbol": "EURUSD", "timeframe": "M1", "candles": [candle]}
    with _AdHocServer([(200, 0, _json_bytes(body))]) as server:
        client = _client(server.url)
        with pytest.raises(Mt5BridgeError, match="open"):
            client.get_candles("EURUSD", "M1", since=None, limit=10)


def test_open_time_with_non_utc_offset_is_rejected():
    candle = _valid_candle(open_time="2026-01-01T10:00:00+02:00")
    body = {"symbol": "EURUSD", "timeframe": "M1", "candles": [candle]}
    with _AdHocServer([(200, 0, _json_bytes(body))]) as server:
        client = _client(server.url)
        with pytest.raises(Mt5BridgeError, match="UTC"):
            client.get_candles("EURUSD", "M1", since=None, limit=10)


def test_open_time_naive_is_rejected():
    candle = _valid_candle(open_time="2026-01-01T10:00:00")
    body = {"symbol": "EURUSD", "timeframe": "M1", "candles": [candle]}
    with _AdHocServer([(200, 0, _json_bytes(body))]) as server:
        client = _client(server.url)
        with pytest.raises(Mt5BridgeError):
            client.get_candles("EURUSD", "M1", since=None, limit=10)


def test_invalid_ohlc_is_rejected():
    candle = _valid_candle(high="1.0", low="2.0")  # high < low: incoerente
    body = {"symbol": "EURUSD", "timeframe": "M1", "candles": [candle]}
    with _AdHocServer([(200, 0, _json_bytes(body))]) as server:
        client = _client(server.url)
        with pytest.raises(Mt5BridgeError, match="OHLC"):
            client.get_candles("EURUSD", "M1", since=None, limit=10)


def test_missing_candles_key_is_rejected():
    body = {"symbol": "EURUSD", "timeframe": "M1"}  # senza 'candles'
    with _AdHocServer([(200, 0, _json_bytes(body))]) as server:
        client = _client(server.url)
        with pytest.raises(Mt5BridgeError):
            client.get_candles("EURUSD", "M1", since=None, limit=10)


def test_malformed_json_body_is_rejected():
    with _AdHocServer([(200, 0, b"{not-valid-json")]) as server:
        client = _client(server.url)
        with pytest.raises(Mt5BridgeError):
            client.get_candles("EURUSD", "M1", since=None, limit=10)


# --- difesa in profondita' lato client: ordine, since esclusivo, limit ------------------------

def test_client_sorts_unsorted_response_defensively():
    candles = [_valid_candle(open_time="2026-01-01T00:10:00Z"), _valid_candle(open_time="2026-01-01T00:00:00Z")]
    body = {"symbol": "EURUSD", "timeframe": "M1", "candles": candles}
    with _AdHocServer([(200, 0, _json_bytes(body))]) as server:
        client = _client(server.url)
        result = client.get_candles("EURUSD", "M1", since=None, limit=10)
        assert [c.open_time for c in result] == sorted(c.open_time for c in result)


def test_client_filters_candles_at_or_before_since_defensively():
    since = datetime(2026, 1, 1, 0, 5, tzinfo=timezone.utc)
    candles = [_valid_candle(open_time="2026-01-01T00:05:00Z"), _valid_candle(open_time="2026-01-01T00:10:00Z")]
    body = {"symbol": "EURUSD", "timeframe": "M1", "candles": candles}
    with _AdHocServer([(200, 0, _json_bytes(body))]) as server:
        client = _client(server.url)
        result = client.get_candles("EURUSD", "M1", since=since, limit=10)
        assert len(result) == 1
        assert result[0].open_time > since


def test_client_truncates_to_limit_defensively():
    candles = [_valid_candle(open_time=f"2026-01-01T00:{i:02d}:00Z") for i in range(10)]
    body = {"symbol": "EURUSD", "timeframe": "M1", "candles": candles}
    with _AdHocServer([(200, 0, _json_bytes(body))]) as server:
        client = _client(server.url)
        result = client.get_candles("EURUSD", "M1", since=None, limit=3)
        assert len(result) == 3


# --- retry / timeout / errori non ritentabili --------------------------------------------------

def test_retries_on_5xx_then_succeeds():
    ok_body = _json_bytes({"symbol": "EURUSD", "timeframe": "M1", "candles": [_valid_candle()]})
    responses = [
        (500, 0, _json_bytes({"error": {"code": "internal", "message": "boom"}})),
        (200, 0, ok_body),
    ]
    with _AdHocServer(responses) as server:
        client = _client(server.url, max_retries=3)
        result = client.get_candles("EURUSD", "M1", since=None, limit=10)
        assert len(result) == 1
        assert len(server.requests) == 2


def test_exhausts_retries_and_raises_after_repeated_5xx():
    responses = [(503, 0, _json_bytes({"error": {"code": "unavailable", "message": "down"}}))] * 3
    with _AdHocServer(responses) as server:
        client = _client(server.url, max_retries=3)
        with pytest.raises(Mt5BridgeError):
            client.get_candles("EURUSD", "M1", since=None, limit=10)
        assert len(server.requests) == 3


def test_no_retry_on_401():
    responses = [(401, 0, _json_bytes({"error": {"code": "unauthorized", "message": "nope"}}))]
    with _AdHocServer(responses) as server:
        client = _client(server.url, max_retries=3)
        with pytest.raises(Mt5BridgeAuthError):
            client.get_candles("EURUSD", "M1", since=None, limit=10)
        assert len(server.requests) == 1


def test_no_retry_on_400():
    responses = [(400, 0, _json_bytes({"error": {"code": "bad_request", "message": "nope"}}))]
    with _AdHocServer(responses) as server:
        client = _client(server.url, max_retries=3)
        with pytest.raises(Mt5BridgeError):
            client.get_candles("EURUSD", "M1", since=None, limit=10)
        assert len(server.requests) == 1


def test_timeout_raises_mt5_bridge_error_without_hanging():
    with _AdHocServer([(200, 2.0, _json_bytes({"symbol": "EURUSD", "timeframe": "M1", "candles": []}))]) as server:
        client = _client(server.url, timeout_seconds=0.3, max_retries=1)
        start = time.monotonic()
        with pytest.raises(Mt5BridgeError):
            client.get_candles("EURUSD", "M1", since=None, limit=10)
        assert time.monotonic() - start < 2.0  # non ha aspettato la risposta lenta


# --- sicurezza: nessun segreto nei log ---------------------------------------------------------

def test_no_token_in_logs_on_auth_error(caplog):
    responses = [(401, 0, _json_bytes({"error": {"code": "unauthorized", "message": "nope"}}))]
    with _AdHocServer(responses) as server:
        client = _client(server.url, token="super-secret-bridge-token", max_retries=1)
        with caplog.at_level(logging.DEBUG):
            with pytest.raises(Mt5BridgeAuthError):
                client.get_candles("EURUSD", "M1", since=None, limit=10)
    assert "super-secret-bridge-token" not in caplog.text


def test_no_token_in_logs_on_retry_warning(caplog):
    responses = [(500, 0, _json_bytes({"error": {"code": "x", "message": "x"}}))] * 3
    with _AdHocServer(responses) as server:
        client = _client(server.url, token="super-secret-bridge-token", max_retries=3)
        with caplog.at_level(logging.DEBUG):
            with pytest.raises(Mt5BridgeError):
                client.get_candles("EURUSD", "M1", since=None, limit=10)
    assert "super-secret-bridge-token" not in caplog.text


# --- factory -------------------------------------------------------------------------------

def test_build_market_data_source_mt5_wires_bridge_config():
    source = build_market_data_source(
        "mt5", mt5_bridge_url="http://localhost:9999", mt5_bridge_token="tok", mt5_bridge_timeout_seconds=3.0
    )
    assert isinstance(source, Mt5MarketDataSource)
    assert source._bridge_url == "http://localhost:9999"
    assert source._timeout_seconds == 3.0


def test_build_market_data_source_mt5_without_url_raises():
    with pytest.raises(ValueError):
        build_market_data_source("mt5", mt5_bridge_token="tok")


def test_build_market_data_source_mt5_without_token_raises():
    with pytest.raises(ValueError):
        build_market_data_source("mt5", mt5_bridge_url="http://localhost:9999")
