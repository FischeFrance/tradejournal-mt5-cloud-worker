"""Test delle funzioni pure di bridge/common.py: nessun server HTTP coinvolto qui (vedi
tests/test_fake_bridge.py per il contratto end-to-end), solo parsing/validazione."""

from __future__ import annotations

import pytest
from common import BridgeConfig, BridgeError, format_iso_utc, parse_iso_utc


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
