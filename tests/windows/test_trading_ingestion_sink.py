"""TradingIngestionSink: no real HTTP, requests.post is always patched (same convention as
tests/test_event_sender.py). Unlike test_real_handlers.py, this module has no pywin32 dependency
(EventSender/LocalEventSink are both plain-Python), so it runs on any platform."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from windows_agent.worker.trading_ingestion_sink import TradingIngestionSink


def _response(status_code: int) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    return response


def _sink(tmp_path) -> TradingIngestionSink:
    return TradingIngestionSink(tmp_path, "https://example.invalid/trading-mt5-events", "tjmt5_test-token")


def test_connection_transition_posts_only_when_explicitly_requested(tmp_path):
    with patch("requests.post", return_value=_response(200)) as mock_post:
        result = _sink(tmp_path).send_connection_transition(
            "11111111-1111-4111-8111-111111111111", 7, False
        )

    assert result.sent == 1 and result.pending == 0
    _, kwargs = mock_post.call_args
    assert kwargs["json"]["event_type"] == "heartbeat"
    assert kwargs["json"]["connected"] is False
    assert kwargs["json"]["event_id"].endswith(":7:0")
    assert kwargs["headers"]["Authorization"] == "Bearer tjmt5_test-token"


def test_transient_failure_remains_in_persistent_outbox(tmp_path):
    with patch("requests.post", return_value=_response(500)):
        result = _sink(tmp_path).send_connection_transition(
            "11111111-1111-4111-8111-111111111111", 8, True
        )

    assert result.pending == 1 and result.transient_failures == 1
    restarted = _sink(tmp_path)
    with patch("requests.post", return_value=_response(200)):
        assert restarted.flush_transitions().sent == 1


def test_send_heartbeat_posts_sanitized_account_snapshot(tmp_path):
    account = SimpleNamespace(
        balance=10_250.75,
        equity=10_310.25,
        currency="eur",
        leverage=100,
    )
    with patch("requests.post", return_value=_response(200)) as mock_post:
        assert _sink(tmp_path).send_heartbeat(account) is True

    _, kwargs = mock_post.call_args
    assert kwargs["json"] == {
        "event_type": "heartbeat",
        "balance": 10_250.75,
        "equity": 10_310.25,
        "currency": "EUR",
        "leverage": 100,
    }


def test_call_delivers_event_over_http(tmp_path):
    payload = {"event_id": "evt-1", "event_type": "trade_opened", "symbol": "EURUSD"}
    with patch("requests.post", return_value=_response(200)) as mock_post:
        _sink(tmp_path)(payload)

    _, kwargs = mock_post.call_args
    assert kwargs["json"] == payload


def test_call_writes_audit_before_failed_delivery(tmp_path):
    payload = {"event_id": "evt-1", "event_type": "trade_opened", "symbol": "EURUSD"}
    with patch("requests.post", return_value=_response(500)):
        with pytest.raises(RuntimeError, match="not acknowledged"):
            _sink(tmp_path)(payload)

    lines = (tmp_path / "data" / "live.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["event_id"] == "evt-1"


def test_call_raises_when_delivery_fails_permanently(tmp_path):
    payload = {"event_id": "evt-1", "event_type": "trade_opened", "symbol": "EURUSD"}
    with patch("requests.post", return_value=_response(422)):
        with pytest.raises(RuntimeError, match="not acknowledged"):
            _sink(tmp_path)(payload)
