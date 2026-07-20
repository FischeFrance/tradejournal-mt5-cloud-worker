"""TradingIngestionSink: no real HTTP, requests.post is always patched (same convention as
tests/test_event_sender.py). Unlike test_real_handlers.py, this module has no pywin32 dependency
(EventSender/LocalEventSink are both plain-Python), so it runs on any platform."""

import json
from unittest.mock import MagicMock, patch

from windows_agent.worker.trading_ingestion_sink import TradingIngestionSink


def _response(status_code: int) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    return response


def _sink(tmp_path) -> TradingIngestionSink:
    return TradingIngestionSink(tmp_path, "https://example.invalid/trading-mt5-events", "tjmt5_test-token")


def test_send_heartbeat_posts_heartbeat_event_type(tmp_path):
    with patch("requests.post", return_value=_response(200)) as mock_post:
        ok = _sink(tmp_path).send_heartbeat()

    assert ok is True
    _, kwargs = mock_post.call_args
    assert kwargs["json"] == {"event_type": "heartbeat"}
    assert kwargs["headers"]["Authorization"] == "Bearer tjmt5_test-token"


def test_send_heartbeat_returns_false_on_permanent_failure(tmp_path):
    with patch("requests.post", return_value=_response(422)):
        ok = _sink(tmp_path).send_heartbeat()

    assert ok is False


def test_call_delivers_event_over_http(tmp_path):
    payload = {"event_id": "evt-1", "event_type": "trade_opened", "symbol": "EURUSD"}
    with patch("requests.post", return_value=_response(200)) as mock_post:
        _sink(tmp_path)(payload)

    _, kwargs = mock_post.call_args
    assert kwargs["json"] == payload


def test_call_always_writes_to_local_audit_log_first_even_if_delivery_fails(tmp_path):
    payload = {"event_id": "evt-1", "event_type": "trade_opened", "symbol": "EURUSD"}
    with patch("requests.post", return_value=_response(500)):
        _sink(tmp_path)(payload)

    lines = (tmp_path / "data" / "live.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["event_id"] == "evt-1"


def test_call_does_not_raise_when_delivery_fails_permanently(tmp_path):
    payload = {"event_id": "evt-1", "event_type": "trade_opened", "symbol": "EURUSD"}
    with patch("requests.post", return_value=_response(422)):
        _sink(tmp_path)(payload)  # must not raise
