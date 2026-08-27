from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import call, patch

import pytest

from windows_agent.provisioning.secret_store import WindowsSecretStore
from windows_agent.worker.native_mt5_runtime import NativeMt5Error, NativeMt5Runtime


def _runtime(tmp_path: Path) -> NativeMt5Runtime:
    terminal = tmp_path / "terminal" / "terminal64.exe"
    terminal.parent.mkdir()
    terminal.write_bytes(b"terminal")
    return NativeMt5Runtime(tmp_path, "00000000-0000-4000-8000-000000000001")


def test_discovery_startup_candidates_prioritize_cache_then_common_suffixes() -> None:
    candidates = NativeMt5Runtime._discovery_startup_symbols(
        "EURUSD",
        "EURUSD.x",
        "EURUSD.raw",
    )

    assert candidates[:3] == ("EURUSD.x", "EURUSD.raw", "EURUSD")
    assert len(candidates) == len({value.casefold() for value in candidates})


def test_discovery_started_marker_is_bound_to_instance_and_chart(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    runtime.files.mkdir(parents=True)
    (runtime.files / "discovery-started.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "connection_id": runtime.connection_id,
                "chart_symbol": "EURUSD.x",
                "terminal_build": 6090,
            }
        ),
        encoding="utf-8",
    )

    assert runtime._wait_for_discovery_start("EURUSD.x", 0.1) is True


def test_discovery_started_marker_rejects_wrong_chart(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    runtime.files.mkdir(parents=True)
    (runtime.files / "discovery-started.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "connection_id": runtime.connection_id,
                "chart_symbol": "EURUSD.x",
                "terminal_build": 6090,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(NativeMt5Error, match="broker_symbol_start_invalid"):
        runtime._wait_for_discovery_start("EURUSD", 0.1)


def test_readiness_cleanup_removes_discovery_start_marker(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    runtime.files.mkdir(parents=True)
    marker = runtime.files / "discovery-started.json"
    marker.write_text("{}", encoding="utf-8")

    runtime._remove_readiness_files()

    assert not marker.exists()


def test_verified_broker_symbol_hint_is_reused_before_generic_symbol(tmp_path: Path) -> None:
    hints = tmp_path / "hints"
    runtime = NativeMt5Runtime(
        tmp_path / "one",
        "00000000-0000-4000-8000-000000000001",
        symbol_hint_root=hints,
    )
    runtime._publish_broker_symbol_hint("GoatFunded-Server3", "EURUSD.x", 6090)
    second = NativeMt5Runtime(
        tmp_path / "two",
        "00000000-0000-4000-8000-000000000002",
        symbol_hint_root=hints,
    )

    shared = second._broker_symbol_hint("goatfunded-server3")
    candidates = second._discovery_startup_symbols("EURUSD", None, shared)

    assert shared == "EURUSD.x"
    assert candidates[0] == "EURUSD.x"


def test_broker_symbol_hint_keeps_operator_and_service_acl(tmp_path: Path) -> None:
    hints = tmp_path / "hints"
    runtime = NativeMt5Runtime(
        tmp_path / "one",
        "00000000-0000-4000-8000-000000000001",
        symbol_hint_root=hints,
    )

    with patch.object(
        WindowsSecretStore, "restrict_shared_service_acl"
    ) as restrict_shared_acl:
        published = runtime._publish_broker_symbol_hint(
            "GoatFunded-Server3", "EURUSD.x", 6090
        )

    assert restrict_shared_acl.call_args_list == [call(hints), call(published)]


def test_managed_profile_has_a_valid_empty_order_file(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    profile = runtime.terminal_root / "Profiles" / "Charts" / "TradeJournal"
    profile.mkdir(parents=True)
    (profile / "chart01.chr").write_text("legacy", encoding="utf-8")

    assert runtime._reset_managed_chart_profile() == 1
    assert (profile / "order.wnd").read_bytes() == b"\xff\xfe"
    assert not (profile / "chart01.chr").exists()
