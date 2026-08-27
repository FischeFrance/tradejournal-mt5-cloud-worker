from __future__ import annotations

import json
from pathlib import Path

import pytest

from windows_agent.release_manifest import RELEASE_CONTENTS
from windows_agent.worker.native_mt5_runtime import NativeMt5Error, NativeMt5Runtime


CONNECTION_ID = "00000000-0000-4000-8000-000000000001"


def _runtime(tmp_path: Path) -> NativeMt5Runtime:
    runtime = NativeMt5Runtime(tmp_path, CONNECTION_ID)
    runtime.files.mkdir(parents=True)
    return runtime


def _record() -> dict[str, object]:
    return {
        "schema_version": 1,
        "connection_id": CONNECTION_ID,
        "login": 42,
        "server": "Demo",
        "requested_symbol": "EURUSD",
        "resolution": "currency_pair",
        "catalog_total": 60,
        "synchronized": True,
        "terminal_connected": True,
        "account_trade_allowed": False,
        "terminal_build": 6090,
        "symbol": "EURUSD.x",
    }


def test_runtime_accepts_the_versioned_discovery_contract(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    (runtime.files / "discovered-symbol.json").write_text(
        json.dumps(_record()),
        encoding="utf-8",
    )

    assert runtime._probe_broker_symbol("EURUSD", 42, "Demo", 0.1) == "EURUSD.x"


def test_runtime_rejects_the_legacy_symbol_only_contract(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    (runtime.files / "discovered-symbol.json").write_text(
        '{"symbol":"EURUSD.x"}',
        encoding="utf-8",
    )

    with pytest.raises(NativeMt5Error, match="broker_symbol_probe_invalid"):
        runtime._probe_broker_symbol("EURUSD", 42, "Demo", 0.1)


def test_runtime_rejects_trade_enabled_discovery_evidence(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    record = _record()
    record["account_trade_allowed"] = True
    (runtime.files / "discovered-symbol.json").write_text(
        json.dumps(record),
        encoding="utf-8",
    )

    with pytest.raises(NativeMt5Error, match="investor_readonly_not_verified"):
        runtime._probe_broker_symbol("EURUSD", 42, "Demo", 0.1)


def test_release_contains_the_discovery_source_used_for_compilation() -> None:
    assert "mt5/experts" in RELEASE_CONTENTS


def test_release_contains_the_terminal_window_visibility_helper() -> None:
    helper = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "windows"
        / "Set-Mt5WindowVisibility.ps1"
    )
    assert "scripts/windows" in RELEASE_CONTENTS
    assert helper.is_file()
