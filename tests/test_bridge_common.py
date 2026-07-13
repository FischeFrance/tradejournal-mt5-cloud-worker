"""Test delle funzioni pure di bridge/common.py: nessun server HTTP coinvolto qui (vedi
tests/test_fake_bridge.py per il contratto end-to-end), solo parsing/validazione."""

from __future__ import annotations

import pytest
from common import (
    MAX_DEAL_LOOKBACK_HOURS,
    BaseBridgeHandler,
    BridgeConfig,
    BridgeError,
    format_iso_utc,
    parse_iso_utc,
)


def test_bridge_config_requires_token():
    with pytest.raises(ValueError):
        BridgeConfig(token="", broker_symbol="EURUSD")


def test_bridge_config_requires_broker_symbol():
    with pytest.raises(ValueError):
        BridgeConfig(token="tok", broker_symbol="")


def test_parse_iso_utc_accepts_z_suffix():
    parsed = parse_iso_utc("2026-01-01T00:05:00Z", "since")
    assert parsed.isoformat() == "2026-01-01T00:05:00+00:00"


def test_parse_iso_utc_accepts_explicit_utc_offset():
    parsed = parse_iso_utc("2026-01-01T00:05:00+00:00", "since")
    assert parsed.utcoffset().total_seconds() == 0


def test_parse_iso_utc_rejects_non_utc_offset():
    with pytest.raises(BridgeError) as exc_info:
        parse_iso_utc("2026-01-01T00:05:00+02:00", "since")
    assert exc_info.value.status == 422
    assert exc_info.value.code == "invalid_since"


def test_parse_iso_utc_rejects_naive_timestamp():
    with pytest.raises(BridgeError):
        parse_iso_utc("2026-01-01T00:05:00", "since")


def test_parse_iso_utc_rejects_non_string():
    with pytest.raises(BridgeError):
        parse_iso_utc(12345, "since")


def test_parse_iso_utc_rejects_garbage():
    with pytest.raises(BridgeError):
        parse_iso_utc("not-a-date", "since")


def test_format_iso_utc_round_trip():
    parsed = parse_iso_utc("2026-01-01T00:05:00Z", "since")
    assert format_iso_utc(parsed) == "2026-01-01T00:05:00Z"


def _parse_snapshot_request(request):
    handler = object.__new__(BaseBridgeHandler)
    return handler.parse_trading_snapshot_request(request)


def test_trading_snapshot_lookback_defaults_to_24_hours():
    assert _parse_snapshot_request({}) == 24


def test_trading_snapshot_lookback_is_clamped_to_safe_maximum():
    assert _parse_snapshot_request({"deal_lookback_hours": 9999}) == MAX_DEAL_LOOKBACK_HOURS == 168


@pytest.mark.parametrize("value", [0, -1, True, False, 1.5, "24", None])
def test_trading_snapshot_lookback_rejects_non_positive_or_non_integer_values(value):
    with pytest.raises(BridgeError) as exc_info:
        _parse_snapshot_request({"deal_lookback_hours": value})
    assert exc_info.value.status == 422
    assert exc_info.value.code == "invalid_deal_lookback_hours"


def test_trading_snapshot_request_rejects_unknown_fields():
    with pytest.raises(BridgeError) as exc_info:
        _parse_snapshot_request({"unexpected": "value"})
    assert exc_info.value.status == 422
    assert exc_info.value.code == "invalid_request"
