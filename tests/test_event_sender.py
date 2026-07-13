"""Test dell'invio HTTP: nessuna rete reale, requests.post e' sempre patchato."""

from unittest.mock import MagicMock, patch

import pytest
import requests

from event_sender import EventSender, mask_value, sanitize_payload_for_log

PAYLOAD = {
    "event_id": "mt5-77002-trade_opened-1-9f2c7ae5b6d3f40a",
    "event_type": "trade_opened",
    "platform": "mt5",
    "account_number": "12345678",
    "server": "MockServer-Demo",
    "external_trade_id": "1",
    "symbol": "EURUSD",
}


def _response(status_code: int) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    return response


def _sender(**overrides) -> EventSender:
    defaults = dict(
        api_url="https://example.invalid/api/mt5-events",
        bridge_token="super-secret-token",
        dry_run=False,
        max_retries=3,
        backoff_base_seconds=0.01,
        max_backoff_seconds=0.05,
        sleep_fn=lambda _seconds: None,
    )
    defaults.update(overrides)
    return EventSender(**defaults)


def test_mask_value_short_string_fully_masked():
    assert mask_value("abcd") == "****"


def test_mask_value_long_string_keeps_first_and_last_two_chars():
    assert mask_value("123456789") == "12*****89"


def test_mask_value_none_is_readable_placeholder():
    assert mask_value(None) == "<vuoto>"


def test_sanitize_payload_masks_account_and_server_without_mutating_original():
    sanitized = sanitize_payload_for_log(PAYLOAD)

    assert sanitized["account_number"] != PAYLOAD["account_number"]
    assert sanitized["server"] != PAYLOAD["server"]
    assert PAYLOAD["account_number"] not in sanitized["event_id"]
    assert PAYLOAD["account_number"] == "12345678"  # originale intatto
    assert sanitized["symbol"] == "EURUSD"


def test_sanitize_payload_redacts_any_secret_looking_key():
    tainted = {**PAYLOAD, "mt5_password": "hunter2", "bridge_token": "abc123"}

    sanitized = sanitize_payload_for_log(tainted)

    assert sanitized["mt5_password"] == "<redacted>"
    assert sanitized["bridge_token"] == "<redacted>"


@patch("event_sender.requests.post")
def test_dry_run_never_calls_requests_post(mock_post):
    sender = _sender(dry_run=True)

    result = sender.send(PAYLOAD)

    mock_post.assert_not_called()
    assert result.status == "dry_run"
    assert result.attempts == 0


@patch("event_sender.requests.post")
def test_missing_api_target_fails_without_network_call(mock_post):
    sender = _sender(api_url=None, bridge_token=None)

    result = sender.send(PAYLOAD)

    mock_post.assert_not_called()
    assert result.status == "failed"
    assert result.error == "missing_api_target"
    assert result.failure_type == "transient"
    assert result.retryable is True


@patch("event_sender.requests.post")
def test_successful_send_on_first_attempt(mock_post):
    mock_post.return_value = _response(200)
    sender = _sender()

    result = sender.send(PAYLOAD)

    assert result.status == "sent"
    assert result.attempts == 1
    mock_post.assert_called_once()
    _, kwargs = mock_post.call_args
    assert kwargs["headers"]["Authorization"] == "Bearer super-secret-token"
    assert kwargs["json"] == PAYLOAD


@patch("event_sender.requests.post")
def test_retries_on_transient_server_error_then_succeeds(mock_post):
    mock_post.side_effect = [_response(500), _response(200)]
    sender = _sender()

    result = sender.send(PAYLOAD)

    assert result.status == "sent"
    assert result.attempts == 2
    assert mock_post.call_count == 2


@patch("event_sender.requests.post")
def test_exhausts_retries_on_persistent_server_error(mock_post):
    mock_post.side_effect = [_response(503), _response(503), _response(503)]
    sender = _sender(max_retries=3)

    result = sender.send(PAYLOAD)

    assert result.status == "failed"
    assert result.failure_type == "transient"
    assert result.attempts == 3
    assert mock_post.call_count == 3


@patch("event_sender.requests.post")
def test_client_error_is_not_retried(mock_post):
    mock_post.return_value = _response(422)
    sender = _sender(max_retries=3)

    result = sender.send(PAYLOAD)

    assert result.status == "failed"
    assert result.error == "rejected_by_api"
    assert result.failure_type == "permanent"
    assert result.retryable is False
    assert result.attempts == 1
    mock_post.assert_called_once()


@pytest.mark.parametrize("status_code", [408, 425, 429])
@patch("event_sender.requests.post")
def test_retryable_client_statuses_are_transient(mock_post, status_code):
    mock_post.return_value = _response(status_code)
    sender = _sender(max_retries=2)

    result = sender.send(PAYLOAD)

    assert result.status == "failed"
    assert result.failure_type == "transient"
    assert result.http_status == status_code
    assert result.error == f"http_{status_code}"
    assert result.attempts == 2
    assert mock_post.call_count == 2


@pytest.mark.parametrize("status_code", [400, 404, 409, 422])
@patch("event_sender.requests.post")
def test_other_client_statuses_are_permanent(mock_post, status_code):
    mock_post.return_value = _response(status_code)
    sender = _sender(max_retries=3)

    result = sender.send(PAYLOAD)

    assert result.failure_type == "permanent"
    assert result.http_status == status_code
    assert result.attempts == 1
    mock_post.assert_called_once()


@pytest.mark.parametrize("status_code", [401, 403])
@patch("event_sender.requests.post")
def test_auth_errors_remain_pending_for_token_rotation(mock_post, status_code):
    mock_post.return_value = _response(status_code)
    sender = _sender(max_retries=3)

    result = sender.send(PAYLOAD)

    assert result.failure_type == "transient"
    assert result.error == "authentication_failed"
    assert result.http_status == status_code
    assert result.attempts == 1
    mock_post.assert_called_once()


@patch("event_sender.requests.post")
def test_network_exception_is_retried_then_fails(mock_post):
    mock_post.side_effect = requests.exceptions.ConnectionError("connessione rifiutata")
    sender = _sender(max_retries=2)

    result = sender.send(PAYLOAD)

    assert result.status == "failed"
    assert result.failure_type == "transient"
    assert mock_post.call_count == 2


@patch("event_sender.requests.post")
def test_request_exception_cannot_echo_authorization_header(mock_post, caplog):
    token = "never-log-this-token"
    mock_post.side_effect = requests.exceptions.InvalidHeader(
        f"Invalid header value: Bearer {token}"
    )
    sender = _sender(bridge_token=token, max_retries=1)

    with caplog.at_level("WARNING"):
        result = sender.send(PAYLOAD)

    assert result.failure_type == "transient"
    assert token not in caplog.text


@patch("event_sender.requests.post")
def test_bridge_token_never_appears_in_logs(mock_post, caplog):
    mock_post.return_value = _response(200)
    sender = _sender(bridge_token="hyper-secret-value")

    with caplog.at_level("DEBUG"):
        sender.send(PAYLOAD)

    assert "hyper-secret-value" not in caplog.text


@patch("event_sender.requests.post")
def test_account_number_and_server_are_masked_in_logs(mock_post, caplog):
    mock_post.return_value = _response(200)
    sender = _sender()

    with caplog.at_level("DEBUG"):
        sender.send(PAYLOAD)

    assert PAYLOAD["account_number"] not in caplog.text
    assert PAYLOAD["server"] not in caplog.text


@patch("event_sender.requests.post")
def test_account_number_is_masked_in_retry_event_id_logs(mock_post, caplog):
    mock_post.return_value = _response(503)
    sender = _sender(max_retries=1)

    with caplog.at_level("DEBUG"):
        sender.send(PAYLOAD)

    assert PAYLOAD["account_number"] not in caplog.text
