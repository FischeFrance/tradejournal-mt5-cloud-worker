from __future__ import annotations

import json
import stat
from pathlib import Path
from unittest.mock import ANY, Mock, call, patch

import pytest

from windows_agent.provisioning.secret_store import WindowsSecretStore
from windows_agent.worker.native_mt5_runtime import (
    NativeMt5Error,
    NativeMt5Runtime,
    NativeMt5Status,
)


@pytest.fixture(autouse=True)
def _no_machine_overrides(monkeypatch):
    # NativeMt5Runtime._setting() deliberately falls back to a machine-wide registry key (see its
    # own docstring/usage), not just the process environment, so it stays visible to a real
    # subprocess.Popen()-launched terminal on the same host. That means these tests are NOT
    # hermetic against whatever an operator has set on the actual VPS: TRADEJOURNAL_MT5_
    # INTERACTIVE_USER being set there (a legitimate real-flow requirement, see _start_process)
    # silently swaps the code path under test from a plain subprocess.Popen() call to a schtasks/
    # scheduled-task launch, which these tests never patch -- they fail with an unrelated
    # "Mock object does not support the context manager protocol" instead of testing what they
    # say they test. Force a clean, override-free environment so behavior here depends only on
    # what each test explicitly sets up, never on the host machine's operational configuration.
    monkeypatch.setattr(NativeMt5Runtime, "_setting", staticmethod(lambda name: ""))


def _runtime(
    tmp_path: Path,
    connection_id: str = "00000000-0000-4000-8000-000000000001",
) -> NativeMt5Runtime:
    terminal = tmp_path / "terminal" / "terminal64.exe"
    terminal.parent.mkdir(parents=True)
    terminal.write_bytes(b"terminal")
    scripts = terminal.parent / "MQL5" / "Scripts" / "TradeJournal"
    scripts.mkdir(parents=True)
    (scripts / "TradeJournalLoader.ex5").write_bytes(b"loader")
    (scripts / "TradeJournalDiscovery.ex5").write_bytes(b"discovery")
    templates = terminal.parent / "Profiles" / "Templates"
    templates.mkdir(parents=True)
    (templates / "ADX.tpl").write_text(
        "<chart>\nsymbol=GBPUSD\n<window>\n</window>\n</chart>\n",
        encoding="utf-16",
    )
    profile = terminal.parent / "Profiles" / "Charts" / "Default"
    profile.mkdir(parents=True)
    for name in ("chart01.chr", "chart02.chr", "chart03.chr", "chart04.chr"):
        (profile / name).write_text("generated chart", encoding="utf-8")
    (profile / "order.wnd").write_bytes(b"generated layout")
    managed_profile = terminal.parent / "Profiles" / "Charts" / "TradeJournal"
    managed_profile.mkdir()
    for name in ("chart01.chr", "chart02.chr", "chart03.chr", "chart04.chr"):
        (managed_profile / name).write_text("generated chart", encoding="utf-8")
    (managed_profile / "order.wnd").write_bytes(b"generated layout")
    return NativeMt5Runtime(tmp_path, connection_id)


def test_install_expert_replaces_read_only_target_from_read_only_release(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    expert = tmp_path / "TradeJournalBridge-release.ex5"
    expert.write_bytes(b"read-only-release")
    expert.chmod(stat.S_IREAD)
    destination = (
        runtime.terminal_root
        / "MQL5"
        / "Experts"
        / "TradeJournal"
        / "TradeJournalBridge.ex5"
    )
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"old-expert")
    destination.chmod(stat.S_IREAD)
    stale_temporary = destination.with_suffix(".ex5.tmp")
    stale_temporary.write_bytes(b"interrupted-copy")
    stale_temporary.chmod(stat.S_IREAD)

    try:
        installed = runtime.install_expert(expert, "all_available")

        assert installed == destination
        assert destination.read_bytes() == b"read-only-release"
        assert destination.stat().st_mode & stat.S_IWUSR
        assert not expert.stat().st_mode & stat.S_IWUSR
        assert not stale_temporary.exists()
        assert (runtime.files / "history_mode").read_text() == "all_available"
    finally:
        expert.chmod(stat.S_IREAD | stat.S_IWRITE)
        if destination.exists():
            destination.chmod(stat.S_IREAD | stat.S_IWRITE)
        if stale_temporary.exists():
            stale_temporary.chmod(stat.S_IREAD | stat.S_IWRITE)


def test_install_expert_cleans_read_only_temporary_after_integrity_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    expert = tmp_path / "TradeJournalBridge-release.ex5"
    expert.write_bytes(b"failed-release")
    expert.chmod(stat.S_IREAD)
    destination = (
        runtime.terminal_root
        / "MQL5"
        / "Experts"
        / "TradeJournal"
        / "TradeJournalBridge.ex5"
    )
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"known-good-expert")
    destination.chmod(stat.S_IREAD)
    temporary = destination.with_suffix(".ex5.tmp")
    original_sha256 = runtime._sha256

    def reject_temporary(path: Path) -> str:
        candidate = Path(path)
        digest = original_sha256(candidate)
        if candidate == temporary:
            candidate.chmod(stat.S_IREAD)
            return "0" * 64
        return digest

    monkeypatch.setattr(runtime, "_sha256", reject_temporary)

    try:
        with pytest.raises(NativeMt5Error, match="expert_copy_integrity_failed"):
            runtime.install_expert(expert)

        assert destination.read_bytes() == b"known-good-expert"
        assert destination.stat().st_mode & stat.S_IWUSR
        assert not expert.stat().st_mode & stat.S_IWUSR
        assert not temporary.exists()
    finally:
        expert.chmod(stat.S_IREAD | stat.S_IWRITE)
        if destination.exists():
            destination.chmod(stat.S_IREAD | stat.S_IWRITE)
        if temporary.exists():
            temporary.chmod(stat.S_IREAD | stat.S_IWRITE)


def _envelope(payload: dict[str, object]) -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "generated_at": "2026-07-17T00:00:00Z",
            "sequence": 1,
            "account_identity": {"login": "42", "server": "Demo"},
            "server_identity": "Demo",
            "payload": payload,
        }
    )


def _discovery_result(
    runtime: NativeMt5Runtime,
    *,
    symbol: str = "EURUSD.raw",
    login: int = 42,
    server: str = "Demo",
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "connection_id": runtime.connection_id,
        "login": login,
        "server": server,
        "requested_symbol": "EURUSD",
        "symbol": symbol,
        "resolution": "currency_pair",
        "catalog_total": 512,
        "synchronized": True,
        "terminal_build": 6036,
    }


def test_start_uses_portable_config_and_removes_plaintext(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    expert = tmp_path / "bridge.ex5"
    expert.write_bytes(b"expert")
    bootstrap = runtime.state / "login-bootstrap.ini"
    startup = runtime.state / "startup.ini"
    runtime.state.mkdir()
    bootstrap.write_text("Password=not-a-real-secret")
    startup.write_text("KeepPrivate=1")
    expected = NativeMt5Status(
        pid=123,
        account={"login": "42", "server": "Demo", "trade_allowed": False},
        heartbeat={"terminal_connected": True},
        files_path=runtime.files,
    )
    with (
        patch.object(
            runtime,
            "_write_startup_config",
            side_effect=[bootstrap, startup],
        ) as write_config,
        patch.object(runtime, "_journal_checkpoint", return_value={}) as checkpoint,
        patch.object(runtime, "_start_process") as start_process,
        patch.object(runtime, "_wait_for_authorization") as wait_for_authorization,
        patch.object(runtime, "_wait_for_account_database") as wait_for_database,
        patch.object(
            runtime,
            "_cached_broker_symbol",
            return_value="EURUSD.raw",
        ) as cached_symbol,
        patch.object(runtime, "_wait_for_investor_sync") as wait_for_investor_sync,
        patch.object(
            runtime,
            "_probe_broker_symbol",
            return_value="EURUSD.raw",
        ) as probe_symbol,
        patch.object(runtime, "_wait_for_heartbeat", return_value=expected),
        patch.object(runtime, "stop", return_value=True) as stop,
        patch.object(runtime, "_remove_generated_example_code") as remove_examples,
    ):
        result = runtime.start(
            login=42,
            server="Demo",
            connection_endpoint="203.0.113.10:443",
            investor_password="not-a-real-secret",
            expert_binary=expert,
            history_mode="new_only",
        )
    assert result == expected
    assert write_config.call_args_list == [
        call(
            42,
            "203.0.113.10:443",
            "not-a-real-secret",
            "EURUSD",
            keep_private=True,
            start_expert=False,
            filename="login-bootstrap.ini",
        ),
        call(
            42,
            "Demo",
            "not-a-real-secret",
            "EURUSD.raw",
            keep_private=True,
            start_expert=False,
            script_name="TradeJournal\\TradeJournalDiscovery",
            filename="startup.ini",
        ),
    ]
    assert checkpoint.call_count == 2
    assert wait_for_authorization.call_args_list == [
        call({}, 42, "Demo", ANY, "203.0.113.10:443"),
        call({}, 42, "Demo", ANY),
    ]
    wait_for_database.assert_called_once_with(ANY)
    cached_symbol.assert_called_once_with(42, "Demo", "EURUSD")
    assert wait_for_investor_sync.call_args_list == [call({}, 42, ANY)]
    probe_symbol.assert_called_once_with("EURUSD", 42, "Demo", 120.0)
    assert start_process.call_args_list == [
        call(bootstrap),
        call(startup),
    ]
    assert stop.call_count == 1
    remove_examples.assert_called_once_with()
    assert not bootstrap.exists()
    assert not startup.exists()
    assert (runtime.files / "history_mode").read_text(encoding="utf-8") == "new_only"
    template_path = runtime.files / "TradeJournalBridge.tpl"
    assert template_path.read_bytes().startswith(b"\xff\xfe")
    template = template_path.read_text(encoding="utf-16")
    assert "symbol=EURUSD.raw" in template
    assert r"path=Experts\TradeJournal\TradeJournalBridge.ex5" in template
    assert "expertmode=0" in template
    assert "<inputs>\nInpTimerSeconds=2" in template
    assert "InpBackfillHours=168" in template
    assert "InpSnapshotHistoryHours=87600" in template
    assert "InpCandleBars=200\n</inputs>" in template


def _single_start_case(
    tmp_path, monkeypatch, *, build=6249, enabled=True, account=None, hint=True, cache=True,
):
    """Exercise real config, journal, discovery and producer guards without launching MT5."""
    base = _runtime(tmp_path)
    runtime = NativeMt5Runtime(
        tmp_path, base.connection_id, single_start_enabled=enabled,
        bootstrap_symbol_cache=tmp_path / "bootstrap-cache" if cache else None,
    )
    expert = tmp_path / "bridge.ex5"
    expert.write_bytes(b"expert")
    monkeypatch.setattr(WindowsSecretStore, "restrict_acl", staticmethod(lambda path: None))
    monkeypatch.setattr(WindowsSecretStore, "restrict_shared_service_acl", staticmethod(lambda path: None))
    if hint and runtime._bootstrap_symbol_cache is not None:
        assert runtime._bootstrap_symbol_cache.store_verified(
            canonical_server="Demo", terminal_build=build, preferred_base="EURUSD",
            verified_symbol="EURUSD.raw",
        )
    build_reader = Mock(return_value=build)
    monkeypatch.setattr("windows_agent.worker.native_mt5_runtime.read_windows_pe_build", build_reader)
    alive = []
    launches = []
    journal_lines = [
        "'42': authorized on Demo through access point\r\n",
        "'42': terminal synchronized with Demo Ltd.\r\n",
        "'42': trading has been disabled - investor mode\r\n",
    ]

    def launch(config):
        launches.append(config.read_text(encoding="utf-8"))
        alive[:] = [100 + len(launches)]
        accounts = runtime.terminal_root / "Config" / "accounts.dat"
        accounts.parent.mkdir(exist_ok=True)
        accounts.write_bytes(b"encrypted-fixture")
        logs = runtime.terminal_root / "logs"
        logs.mkdir(exist_ok=True)
        with (logs / "20261010.log").open("ab") as handle:
            handle.write("".join(journal_lines).encode("utf-16-le"))
        discovered = _discovery_result(runtime)
        discovered["terminal_build"] = build
        (runtime.files / "discovered-symbol.json").write_text(json.dumps(discovered), encoding="utf-8")

    def stop(**_kwargs):
        alive.clear()
        return True

    original_handoff = runtime._publish_bridge_handoff

    def handoff():
        marker = original_handoff()
        (runtime.files / "account.json").write_text(_envelope(account or {
            "login": "42", "server": "Demo", "trade_allowed": False,
        }), encoding="utf-8")
        (runtime.files / "heartbeat.json").write_text(
            _envelope({"terminal_connected": True}), encoding="utf-8"
        )
        return marker

    stop_mock = Mock(side_effect=stop)
    handoff_mock = Mock(side_effect=handoff)
    monkeypatch.setattr(runtime, "_start_process", launch)
    monkeypatch.setattr(runtime, "_running_terminal_pids", lambda: alive[:])
    monkeypatch.setattr(runtime, "stop", stop_mock)
    monkeypatch.setattr(runtime, "_publish_bridge_handoff", handoff_mock)
    monkeypatch.setattr(runtime, "set_terminal_window_visibility", lambda *_args, **_kwargs: {})
    return runtime, expert, launches, journal_lines, stop_mock, handoff_mock, build_reader


def test_verified_single_start_uses_one_process_and_removes_credentials(tmp_path, monkeypatch):
    runtime, expert, launches, _, stop, handoff, build_reader = _single_start_case(tmp_path, monkeypatch)
    result = runtime.start(login=42, server="Demo", investor_password="fixture-password", expert_binary=expert)
    assert result.pid == 101
    assert len(launches) == 1
    assert "Script=TradeJournal\\TradeJournalDiscovery" in launches[0]
    assert "Symbol=EURUSD.raw" in launches[0]
    assert (runtime.files / "symbol-preference.txt").read_text() == "EURUSD"
    assert "Password=fixture-password" in launches[0]
    assert "AllowLiveTrading=0" in launches[0]
    assert "AllowDllImport=0" in launches[0]
    assert not (runtime.state / "single-start.ini").exists()
    stop.assert_not_called()
    handoff.assert_called_once()
    build_reader.assert_called_once_with(runtime.terminal)
    assert runtime._bridge_template_symbol() == "EURUSD.raw"


@pytest.mark.parametrize("build,enabled", [(6032, True), (6250, True), (6249, False)])
def test_single_start_gate_retains_legacy_bootstrap_for_other_builds(tmp_path, monkeypatch, build, enabled):
    runtime, expert, launches, _, stop, _, build_reader = _single_start_case(
        tmp_path, monkeypatch, build=build, enabled=enabled
    )
    runtime.start(login=42, server="Demo", investor_password="fixture", expert_binary=expert)
    assert len(launches) == 2
    assert "Script=" not in launches[0]
    assert "Enabled=0" in launches[0]
    assert "Script=TradeJournal\\TradeJournalDiscovery" in launches[1]
    stop.assert_called_once()
    assert build_reader.call_count == 1  # Gate read, or post-success PE confirmation for learning.


@pytest.mark.parametrize("invalid", ["absent", "corrupt", "expired", "server", "build", "preferred"])
def test_single_start_invalid_hint_keeps_legacy_and_learns_only_after_success(tmp_path, monkeypatch, invalid):
    runtime, expert, launches, _, stop, _, _ = _single_start_case(tmp_path, monkeypatch, hint=False)
    cache = runtime._bootstrap_symbol_cache
    if invalid != "absent":
        assert cache.store_verified(
            canonical_server="Other" if invalid == "server" else "Demo",
            terminal_build=6032 if invalid == "build" else 6249,
            preferred_base="GBPUSD" if invalid == "preferred" else "EURUSD",
            verified_symbol="GBPUSD.other",
        )
        path = next(cache.root.glob("*.json"))
        if invalid == "corrupt":
            path.write_text("{invalid", encoding="utf-8")
        elif invalid == "expired":
            record = json.loads(path.read_text())
            record["verified_at"] -= cache.MAX_AGE_SECONDS + 1
            path.write_text(json.dumps(record))
    runtime.start(login=42, server="Demo", investor_password="fixture", expert_binary=expert)
    assert len(launches) == 2
    assert "Script=" not in launches[0]
    assert "Symbol=GBPUSD.other" not in "".join(launches)
    stop.assert_called_once()
    assert cache.lookup(canonical_server="Demo", terminal_build=6249, preferred_base="EURUSD") == "EURUSD.raw"


def test_single_start_flag_without_optional_cache_preserves_legacy(tmp_path, monkeypatch):
    runtime, expert, launches, _, stop, _, _ = _single_start_case(tmp_path, monkeypatch, cache=False)
    runtime.start(login=42, server="Demo", investor_password="fixture", expert_binary=expert)
    assert len(launches) == 2
    stop.assert_called_once()


def test_single_start_uninspectable_build_preserves_legacy_without_learning(tmp_path, monkeypatch):
    from windows_agent.provisioning.mt5_public_release import Mt5PublicReleaseError

    runtime, expert, launches, _, stop, _, reader = _single_start_case(tmp_path, monkeypatch, hint=False)
    reader.side_effect = Mt5PublicReleaseError("fixture invalid PE")
    runtime.start(login=42, server="Demo", investor_password="fixture", expert_binary=expert)
    assert len(launches) == 2
    stop.assert_called_once()
    assert not runtime._bootstrap_symbol_cache.root.exists()


@pytest.mark.parametrize("code", ["identity_mismatch", "investor_readonly_not_verified", "terminal_not_ready"])
def test_legacy_failed_producer_proof_never_learns_bootstrap_hint(tmp_path, monkeypatch, code):
    runtime, expert, _, _, _, _, _ = _single_start_case(tmp_path, monkeypatch, enabled=False, hint=False)
    monkeypatch.setattr(runtime, "_wait_for_heartbeat", Mock(side_effect=NativeMt5Error(code)))
    with pytest.raises(NativeMt5Error, match=code):
        runtime.start(login=42, server="Demo", investor_password="fixture", expert_binary=expert)
    assert not runtime._bootstrap_symbol_cache.root.exists()


def test_legacy_mismatched_discovery_and_pe_build_do_not_learn_hint(tmp_path, monkeypatch):
    runtime, expert, launches, _, _, _, reader = _single_start_case(
        tmp_path, monkeypatch, enabled=False, hint=False,
    )
    reader.return_value = 6250
    runtime.start(login=42, server="Demo", investor_password="fixture", expert_binary=expert)
    assert len(launches) == 2
    assert not runtime._bootstrap_symbol_cache.root.exists()


def test_verified_single_start_rechecks_wrong_hint_and_recovers_in_two_launches(tmp_path, monkeypatch):
    runtime, expert, launches, _, stop, _, _ = _single_start_case(tmp_path, monkeypatch)
    cache = runtime._bootstrap_symbol_cache
    assert cache.store_verified(
        canonical_server="Demo", terminal_build=6249, preferred_base="EURUSD", verified_symbol="EURUSD.old",
    )
    original = runtime._probe_broker_symbol
    calls = 0

    def probe(*args):
        nonlocal calls
        calls += 1
        if calls == 1:
            selected = runtime.terminal_root / "Bases" / "Demo" / "symbols" / "selected-42.dat"
            selected.parent.mkdir(parents=True)
            selected.write_bytes("EURUSD.raw\0".encode("utf-16-le"))
            raise NativeMt5Error("broker_symbol_probe_failed")
        return original(*args)

    monkeypatch.setattr(runtime, "_probe_broker_symbol", probe)
    runtime.start(login=42, server="Demo", investor_password="fixture", expert_binary=expert)
    assert len(launches) == 2
    assert "Symbol=EURUSD.old" in launches[0]
    assert "Symbol=EURUSD.raw" in launches[1]
    assert (runtime.files / "symbol-preference.txt").read_text() == "EURUSD"
    stop.assert_called_once()
    assert cache.lookup(canonical_server="Demo", terminal_build=6249, preferred_base="EURUSD") == "EURUSD.raw"


@pytest.mark.parametrize("code", sorted(NativeMt5Runtime._SINGLE_START_RETRYABLE))
def test_single_start_timeout_retries_only_once_on_warmed_instance(tmp_path, monkeypatch, code):
    runtime, expert, launches, _, stop, _, _ = _single_start_case(tmp_path, monkeypatch)
    original_probe = runtime._probe_broker_symbol
    calls = 0

    def probe(*args):
        nonlocal calls
        calls += 1
        if calls == 1:
            selected = runtime.terminal_root / "Bases" / "Demo" / "symbols" / "selected-42.dat"
            selected.parent.mkdir(parents=True)
            selected.write_bytes("EURUSD.raw\0".encode("utf-16-le"))
            raise NativeMt5Error(code)
        return original_probe(*args)

    monkeypatch.setattr(runtime, "_probe_broker_symbol", probe)
    result = runtime.start(login=42, server="Demo", investor_password="fixture", expert_binary=expert)
    assert result.pid == 102
    assert len(launches) == 2
    assert "Symbol=EURUSD.raw" in launches[1]
    stop.assert_called_once()
    assert not (runtime.state / "single-start.ini").exists()


@pytest.mark.parametrize("line,code", [
    ("'42': authorization failed (Invalid account)\r\n", "authorization_failed"),
    ("'42': trading has been enabled\r\n", "investor_readonly_not_verified"),
])
def test_single_start_credential_and_master_access_fail_without_retry(tmp_path, monkeypatch, line, code):
    runtime, expert, launches, lines, stop, handoff, _ = _single_start_case(tmp_path, monkeypatch)
    if code == "authorization_failed":
        lines[:] = [line]
    else:
        lines.append(line)
    with pytest.raises(NativeMt5Error, match=code):
        runtime.start(login=42, server="Demo", investor_password="fixture", expert_binary=expert)
    assert len(launches) == 1
    stop.assert_called_once()
    handoff.assert_not_called()
    assert not (runtime.state / "single-start.ini").exists()


@pytest.mark.parametrize("account,code", [
    ({"login": "99", "server": "Demo", "trade_allowed": False}, "identity_mismatch"),
    ({"login": "42", "server": "Other", "trade_allowed": False}, "server_identity_mismatch"),
    ({"login": "42", "server": "Demo", "trade_allowed": True}, "investor_readonly_not_verified"),
])
def test_single_start_producer_identity_failure_never_retries(tmp_path, monkeypatch, account, code):
    runtime, expert, launches, _, stop, _, _ = _single_start_case(tmp_path, monkeypatch, account=account)
    with pytest.raises(NativeMt5Error, match=code):
        runtime.start(login=42, server="Demo", investor_password="fixture", expert_binary=expert)
    assert len(launches) == 1
    stop.assert_called_once()


def test_single_start_second_timeout_does_not_start_a_third_process(tmp_path, monkeypatch):
    runtime, expert, launches, _, stop, handoff, _ = _single_start_case(tmp_path, monkeypatch)
    monkeypatch.setattr(runtime, "_probe_broker_symbol", Mock(side_effect=NativeMt5Error("broker_symbol_probe_failed")))
    with pytest.raises(NativeMt5Error, match="broker_symbol_probe_failed"):
        runtime.start(login=42, server="Demo", investor_password="fixture", expert_binary=expert)
    assert len(launches) == 2
    assert stop.call_count == 2
    handoff.assert_not_called()


def test_single_start_cancellation_prevents_warm_retry_and_cleans_config(tmp_path, monkeypatch):
    runtime, expert, launches, _, stop, handoff, _ = _single_start_case(tmp_path, monkeypatch)
    cancelled = False

    def guard():
        if cancelled:
            raise RuntimeError("fixture lease lost")

    def probe(*_args):
        nonlocal cancelled
        cancelled = True
        raise NativeMt5Error("broker_symbol_probe_failed")

    runtime.set_cancel_check(guard)
    monkeypatch.setattr(runtime, "_probe_broker_symbol", probe)
    with pytest.raises(RuntimeError, match="fixture lease lost"):
        runtime.start(login=42, server="Demo", investor_password="fixture", expert_binary=expert)
    assert len(launches) == 1
    stop.assert_called_once()
    handoff.assert_not_called()
    assert not (runtime.state / "single-start.ini").exists()


def test_single_start_exhausted_global_budget_prevents_another_launch(tmp_path, monkeypatch):
    runtime, expert, launches, _, stop, handoff, _ = _single_start_case(tmp_path, monkeypatch)
    clock = [0.0]
    monkeypatch.setattr("windows_agent.worker.native_mt5_runtime.time.monotonic", lambda: clock[0])

    def authorize(*_args):
        clock[0] = 10.0
        raise NativeMt5Error("authorization_timeout")

    monkeypatch.setattr(runtime, "_wait_for_authorization", authorize)
    with pytest.raises(NativeMt5Error, match="cold_start_timeout"):
        runtime.start(login=42, server="Demo", investor_password="fixture", expert_binary=expert, timeout=10)
    assert len(launches) == 1
    stop.assert_called_once()
    handoff.assert_not_called()


def test_single_start_warm_retry_uses_remaining_global_budget(tmp_path, monkeypatch):
    runtime, expert, launches, _, stop, _, _ = _single_start_case(tmp_path, monkeypatch)
    clock = [0.0]
    budgets = []
    original = runtime._wait_for_authorization
    monkeypatch.setattr("windows_agent.worker.native_mt5_runtime.time.monotonic", lambda: clock[0])

    def authorize(*args):
        budgets.append(args[3])
        if len(budgets) == 1:
            clock[0] = 7.0
            raise NativeMt5Error("authorization_timeout")
        return original(*args)

    monkeypatch.setattr(runtime, "_wait_for_authorization", authorize)
    runtime.start(login=42, server="Demo", investor_password="fixture", expert_binary=expert, timeout=10)
    assert budgets == [10.0, 3.0]
    assert len(launches) == 2
    stop.assert_called_once_with(timeout=3.0)


def test_single_start_failed_cleanup_prevents_another_launch(tmp_path, monkeypatch):
    runtime, expert, launches, _, stop, handoff, _ = _single_start_case(tmp_path, monkeypatch)
    monkeypatch.setattr(runtime, "_probe_broker_symbol", Mock(side_effect=NativeMt5Error("broker_symbol_probe_failed")))
    stop.side_effect = None
    stop.return_value = False
    with pytest.raises(NativeMt5Error, match="terminal_stop_failed"):
        runtime.start(login=42, server="Demo", investor_password="fixture", expert_binary=expert)
    assert len(launches) == 1
    assert stop.call_count == 2  # Retry cleanup fails; final failure cleanup still runs.
    handoff.assert_not_called()


def test_single_start_stale_handoff_marker_prevents_launch(tmp_path, monkeypatch):
    runtime, expert, launches, _, stop, handoff, _ = _single_start_case(tmp_path, monkeypatch)
    runtime.files.mkdir(parents=True)
    (runtime.files / "bridge-ready").write_text("old ready")
    monkeypatch.setattr(runtime, "_remove_readiness_files", lambda: None)
    with pytest.raises(NativeMt5Error, match="cold_start_cleanup_failed"):
        runtime.start(login=42, server="Demo", investor_password="fixture", expert_binary=expert)
    assert not launches
    stop.assert_called_once()
    handoff.assert_not_called()


@pytest.mark.parametrize("mutation", [{"terminal_build": 6250}, {"login": 99}])
def test_single_start_rejects_changed_build_or_uncorrelated_discovery_before_bridge(tmp_path, monkeypatch, mutation):
    runtime, expert, launches, _, stop, handoff, _ = _single_start_case(tmp_path, monkeypatch)
    original = runtime._probe_broker_symbol

    def probe(*args):
        path = runtime.files / "discovered-symbol.json"
        result = json.loads(path.read_text())
        result.update(mutation)
        path.write_text(json.dumps(result))
        return original(*args)

    monkeypatch.setattr(runtime, "_probe_broker_symbol", probe)
    code = "terminal_build_mismatch" if "terminal_build" in mutation else "broker_symbol_probe_invalid"
    with pytest.raises(NativeMt5Error, match=code):
        runtime.start(login=42, server="Demo", investor_password="fixture", expert_binary=expert)
    assert len(launches) == 1
    stop.assert_called_once()
    handoff.assert_not_called()


def test_single_start_endpoint_rejection_never_retries(tmp_path, monkeypatch):
    runtime, expert, launches, lines, stop, handoff, _ = _single_start_case(tmp_path, monkeypatch)
    lines[:] = ["'42': connection to 203.0.113.10:443 failed\r\n"]
    with pytest.raises(NativeMt5Error, match="endpoint_connection_failed"):
        runtime.start(login=42, server="Demo", connection_endpoint="203.0.113.10:443",
                      investor_password="fixture", expert_binary=expert)
    assert len(launches) == 1
    stop.assert_called_once()
    handoff.assert_not_called()


def test_remove_generated_example_code_removes_only_known_paths(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    generated = runtime.terminal_root / "MQL5" / "Scripts" / "Examples" / "Demo.mq5"
    generated.parent.mkdir(parents=True)
    generated.write_text("example", encoding="utf-8")
    retained = runtime.terminal_root / "MQL5" / "Scripts" / "TradeJournal" / "TradeJournalDiscovery.ex5"
    retained.parent.mkdir(parents=True, exist_ok=True)
    retained.write_bytes(b"loader")

    removed = runtime._remove_generated_example_code()

    assert removed == ("MQL5/Scripts/Examples",)
    assert not generated.exists()
    assert retained.is_file()


def test_reset_managed_chart_profile_removes_only_managed_generated_chart_state(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    profile = runtime.terminal_root / "Profiles" / "Charts" / "TradeJournal"
    retained = profile / "profile-note.txt"
    retained.write_text("keep", encoding="utf-8")

    assert runtime._reset_managed_chart_profile() == 5

    assert not tuple(profile.glob("*.chr"))
    assert not (profile / "order.wnd").exists()
    assert retained.read_text(encoding="utf-8") == "keep"
    default_profile = runtime.terminal_root / "Profiles" / "Charts" / "Default"
    assert len(tuple(default_profile.glob("*.chr"))) == 4
    assert (default_profile / "order.wnd").is_file()


def test_reset_managed_chart_profile_refuses_running_terminal(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)

    with (
        patch.object(runtime, "_running_terminal_pids", return_value=[123]),
        pytest.raises(NativeMt5Error, match="chart_profile_in_use"),
    ):
        runtime._reset_managed_chart_profile()


def test_resume_uses_cached_account_and_direct_readonly_expert(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    expert = tmp_path / "bridge.ex5"
    expert.write_bytes(b"expert")
    runtime.files.mkdir(parents=True)
    (runtime.files / "TradeJournalBridge.tpl").write_text(
        "<chart>\nsymbol=EURUSD.raw\n<window>\n</window>\n</chart>\n",
        encoding="utf-16",
    )
    runtime.state.mkdir()
    config = runtime.state / "resume.ini"
    config.write_text("temporary", encoding="utf-8")
    expected = NativeMt5Status(
        pid=123,
        account={"login": "42", "server": "Demo", "trade_allowed": False},
        heartbeat={"terminal_connected": True},
        files_path=runtime.files,
    )

    with (
        patch.object(
            runtime, "_write_startup_config", return_value=config
        ) as write_config,
        patch.object(runtime, "_journal_checkpoint", return_value={}),
        patch.object(runtime, "_start_process") as start_process,
        patch.object(runtime, "_wait_for_authorization"),
        patch.object(runtime, "_wait_for_investor_sync"),
        patch.object(runtime, "_wait_for_heartbeat", return_value=expected),
    ):
        result = runtime.resume(
            login=42,
            server="Demo",
            expert_binary=expert,
        )

    assert result == expected
    write_config.assert_called_once_with(
        42,
        "Demo",
        None,
        "EURUSD.raw",
        keep_private=True,
        start_expert=True,
        filename="resume.ini",
    )
    start_process.assert_called_once_with(config)
    assert not config.exists()


def test_startup_config_uses_expert_name_relative_to_mql5_experts(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(WindowsSecretStore, "restrict_acl", staticmethod(lambda path: None))
    runtime = _runtime(tmp_path)
    config = runtime._write_startup_config(None, None, None, "EURUSD")
    content = config.read_text(encoding="utf-8")
    assert "Expert=TradeJournal\\TradeJournalBridge" in content
    assert "ProfileLast=TradeJournal" in content
    assert "PreloadCharts=0" in content
    assert "ProfileLast=Default" not in content
    assert str(runtime.terminal_root) not in content


def test_start_falls_back_to_configured_chart_when_private_cache_is_opaque(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    expert = tmp_path / "bridge.ex5"
    expert.write_bytes(b"expert")
    bootstrap = runtime.state / "login-bootstrap.ini"
    startup = runtime.state / "startup.ini"
    runtime.state.mkdir()
    bootstrap.write_text("temporary")
    startup.write_text("temporary")
    expected = NativeMt5Status(
        pid=123,
        account={"login": "42", "server": "Demo", "trade_allowed": False},
        heartbeat={"terminal_connected": True},
        files_path=runtime.files,
    )
    with (
        patch.object(
            runtime,
            "_write_startup_config",
            side_effect=[bootstrap, startup],
        ) as write_config,
        patch.object(runtime, "_journal_checkpoint", return_value={}),
        patch.object(runtime, "_start_process") as start_process,
        patch.object(runtime, "_wait_for_authorization"),
        patch.object(runtime, "_wait_for_account_database"),
        patch.object(
            runtime,
            "_cached_broker_symbol",
            return_value=None,
        ) as cached_symbol,
        patch.object(runtime, "_wait_for_investor_sync"),
        patch.object(runtime, "_remove_readiness_files") as remove_readiness,
        patch.object(runtime, "_write_symbol_preference") as write_preference,
        patch.object(
            runtime,
            "_probe_broker_symbol",
            return_value="EURUSD",
        ) as probe_symbol,
        patch.object(runtime, "_install_bridge_template") as install_template,
        patch.object(runtime, "_publish_bridge_handoff") as publish_handoff,
        patch.object(
            runtime,
            "_wait_for_heartbeat",
            return_value=expected,
        ) as wait_for_heartbeat,
        patch.object(runtime, "stop", return_value=True) as stop,
    ):
        result = runtime.start(
            login=42,
            server="Demo",
            investor_password="placeholder",
            expert_binary=expert,
            history_mode="new_only",
            timeout=90,
        )

    assert result == expected
    assert start_process.call_args_list == [
        call(bootstrap),
        call(startup),
    ]
    assert write_config.call_args_list[1] == call(
        42,
        "Demo",
        "placeholder",
        "EURUSD",
        keep_private=True,
        start_expert=False,
        script_name="TradeJournal\\TradeJournalDiscovery",
        filename="startup.ini",
    )
    wait_for_heartbeat.assert_called_once_with(90.0, 42, "Demo")
    assert stop.call_count == 1
    assert remove_readiness.call_count == 1
    write_preference.assert_called_once_with("EURUSD")
    cached_symbol.assert_called_once_with(42, "Demo", "EURUSD")
    probe_symbol.assert_called_once_with("EURUSD", 42, "Demo", 90.0)
    install_template.assert_called_once_with("EURUSD")
    publish_handoff.assert_called_once_with()
    assert not bootstrap.exists()
    assert not startup.exists()


def test_symbol_handoff_files_are_validated_and_published_atomically(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)

    preference = runtime._write_symbol_preference("EURUSD")
    assert preference.read_text(encoding="utf-8") == "EURUSD"

    output = runtime.files / "discovered-symbol.json"
    output.write_text(json.dumps(_discovery_result(runtime)), encoding="utf-8")
    assert runtime._probe_broker_symbol("EURUSD", 42, "Demo", 1.0) == "EURUSD.raw"

    runtime._install_bridge_template("EURUSD.raw")
    handoff = runtime._publish_bridge_handoff()
    assert handoff.read_text(encoding="ascii") == "ready\n"
    assert not (runtime.files / "bridge-ready.tmp").exists()


def test_cached_broker_symbol_uses_exact_account_and_server_scope(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    selected = (
        runtime.terminal_root
        / "Bases"
        / "FPMTrading-Live"
        / "symbols"
        / "selected-12345678.dat"
    )
    selected.parent.mkdir(parents=True)
    selected.write_bytes(
        "description\0GBPUSD.raw\0EURUSD.raw\0USDJPY.raw\0".encode("utf-16-le")
    )

    assert (
        runtime._cached_broker_symbol(12345678, "fpmtrading-live", "EURUSD")
        == "EURUSD.raw"
    )
    assert runtime._cached_broker_symbol(12345679, "FPMTrading-Live", "EURUSD") is None
    assert runtime._cached_broker_symbol(12345678, "Other-Live", "EURUSD") is None


def test_cached_broker_symbol_rejects_symlinked_selected_file(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    symbols = runtime.terminal_root / "Bases" / "Demo" / "symbols"
    symbols.mkdir(parents=True)
    outside = tmp_path / "outside.dat"
    outside.write_bytes("EURUSD.raw".encode("utf-16-le"))
    selected = symbols / "selected-42.dat"
    try:
        selected.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable")

    with pytest.raises(NativeMt5Error, match="broker_symbol_cache_invalid"):
        runtime._cached_broker_symbol(42, "Demo", "EURUSD")


@pytest.mark.parametrize("symbol", ["", " EURUSD", "EURUSD\n", "X" * 65])
def test_symbol_handoff_rejects_invalid_values(tmp_path: Path, symbol: str) -> None:
    runtime = _runtime(tmp_path)
    with pytest.raises(NativeMt5Error, match="invalid_startup_symbol"):
        runtime._write_symbol_preference(symbol)


def test_symbol_probe_rejects_malformed_discovery_output(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    runtime.files.mkdir(parents=True)
    (runtime.files / "discovered-symbol.json").write_text(
        '{"symbol":"EURUSD\\n"}',
        encoding="utf-8",
    )
    with pytest.raises(NativeMt5Error, match="broker_symbol_probe_invalid"):
        runtime._probe_broker_symbol("EURUSD", 42, "Demo", 1.0)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("connection_id", "different-instance"),
        ("login", 99),
        ("server", "Other"),
        ("requested_symbol", "GBPUSD"),
        ("synchronized", False),
        ("catalog_total", 0),
    ],
)
def test_symbol_probe_rejects_uncorrelated_discovery_output(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    runtime = _runtime(tmp_path)
    runtime.files.mkdir(parents=True)
    record = _discovery_result(runtime)
    record[field] = value
    (runtime.files / "discovered-symbol.json").write_text(
        json.dumps(record),
        encoding="utf-8",
    )

    with pytest.raises(NativeMt5Error, match="broker_symbol_probe_invalid"):
        runtime._probe_broker_symbol("EURUSD", 42, "Demo", 1.0)


def test_start_process_requires_dedicated_interactive_user(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    config = runtime.state / "startup.ini"
    config.parent.mkdir()
    config.write_text("temporary")
    with pytest.raises(NativeMt5Error, match="dedicated_interactive_user_required"):
        runtime._start_process(config, 42)


def test_install_expert_rejects_unknown_history_mode(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    expert = tmp_path / "bridge.ex5"
    expert.write_bytes(b"expert")
    with pytest.raises(NativeMt5Error, match="invalid_history_mode"):
        runtime.install_expert(expert, "ten_year_snapshot")


def test_running_bridge_acknowledges_atomic_switch_to_new_only(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    runtime.files.mkdir(parents=True, exist_ok=True)
    historical = json.loads(
        _envelope(
            {
                "history_mode": "history",
                "terminal_connected": True,
            }
        )
    )
    live = {
        **historical,
        "sequence": 2,
        "payload": {
            "history_mode": "new_only",
            "terminal_connected": True,
        },
    }

    with (
        patch.object(runtime, "_read_json", side_effect=[historical, live]),
        patch("windows_agent.worker.native_mt5_runtime.time.sleep"),
    ):
        runtime.switch_to_new_only(timeout=1)

    assert (runtime.files / "history_mode").read_text(encoding="utf-8") == "new_only"


def test_login_bootstrap_and_noninteractive_startup_configs(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(WindowsSecretStore, "restrict_acl", staticmethod(lambda path: None))
    runtime = _runtime(tmp_path)
    bootstrap = runtime._write_startup_config(
        42,
        "Demo",
        "investor-secret",
        "EURUSD",
        keep_private=True,
        start_expert=False,
        filename="login-bootstrap.ini",
    )
    bootstrap_content = bootstrap.read_text(encoding="utf-8")
    assert "Login=42" in bootstrap_content
    assert "Server=Demo" in bootstrap_content
    assert "Password=investor-secret" in bootstrap_content
    assert "KeepPrivate=1" in bootstrap_content
    assert "[StartUp]" not in bootstrap_content
    assert "Enabled=0" in bootstrap_content

    startup = runtime._write_startup_config(
        42,
        "Demo",
        "investor-secret",
        "EURUSD",
        keep_private=True,
        start_expert=False,
        script_name="TradeJournal\\TradeJournalDiscovery",
        filename="startup.ini",
    )
    startup_content = startup.read_text(encoding="utf-8")
    assert "Login=42" in startup_content
    assert "Server=Demo" in startup_content
    assert "Password=investor-secret" in startup_content
    assert "KeepPrivate=1" in startup_content
    assert "Expert=" not in startup_content
    assert "Script=TradeJournal\\TradeJournalDiscovery" in startup_content


def test_startup_config_allows_configured_interactive_user_to_read(
    tmp_path: Path, monkeypatch
) -> None:
    runtime = _runtime(tmp_path)
    restricted: list[Path] = []
    monkeypatch.setattr(
        WindowsSecretStore,
        "restrict_acl",
        staticmethod(lambda path: restricted.append(Path(path))),
    )
    monkeypatch.setattr(
        NativeMt5Runtime,
        "_setting",
        staticmethod(
            lambda name: "TradeJournalMT5"
            if name == "TRADEJOURNAL_MT5_INTERACTIVE_USER"
            else ""
        ),
    )
    completed = Mock(returncode=0)
    with patch("subprocess.run", return_value=completed) as run:
        config = runtime._write_startup_config(
            42,
            "Demo",
            "investor-secret",
            "EURUSD",
            keep_private=True,
            start_expert=False,
            filename="login-bootstrap.ini",
        )

    assert restricted == [config]
    run.assert_called_once_with(
        ["icacls", str(config), "/grant", "TradeJournalMT5:(R)"],
        capture_output=True,
        text=True,
        check=False,
    )


def test_account_database_acl_allows_only_service_and_interactive_user(
    tmp_path: Path, monkeypatch
) -> None:
    runtime = _runtime(tmp_path)
    accounts = runtime.terminal_root / "Config" / "accounts.dat"
    accounts.parent.mkdir(exist_ok=True)
    accounts.write_bytes(b"encrypted-account-material")
    restricted: list[Path] = []
    monkeypatch.setattr(
        WindowsSecretStore,
        "restrict_acl",
        staticmethod(lambda path: restricted.append(Path(path))),
    )
    monkeypatch.setattr(
        NativeMt5Runtime,
        "_setting",
        staticmethod(
            lambda name: "TradeJournalMT5"
            if name == "TRADEJOURNAL_MT5_INTERACTIVE_USER"
            else ""
        ),
    )
    completed = Mock(returncode=0)
    with patch("subprocess.run", return_value=completed) as run:
        runtime._wait_for_account_database(1.0)

    assert restricted == [accounts]
    run.assert_called_once_with(
        ["icacls", str(accounts), "/grant", "TradeJournalMT5:(M)"],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    "account",
    ["Administrator", "ADMINISTRATOR", "SYSTEM", "LocalSystem", "LocalService", "NetworkService"],
)
def test_interactive_user_rejects_operator_and_service_identities(
    tmp_path: Path, monkeypatch, account: str
) -> None:
    runtime = _runtime(tmp_path)
    monkeypatch.setattr(
        NativeMt5Runtime,
        "_setting",
        staticmethod(
            lambda name: account
            if name == "TRADEJOURNAL_MT5_INTERACTIVE_USER"
            else ""
        ),
    )

    with pytest.raises(NativeMt5Error, match="interactive_user_not_dedicated"):
        runtime._interactive_user()


def test_start_process_uses_limited_dedicated_interactive_session(
    tmp_path: Path, monkeypatch
) -> None:
    runtime = _runtime(tmp_path)
    runtime.state.mkdir()
    config = runtime.state / "startup.ini"
    config.write_text("[Common]\n", encoding="utf-8")
    monkeypatch.setattr(
        NativeMt5Runtime,
        "_setting",
        staticmethod(
            lambda name: "TradeJournalMT5"
            if name == "TRADEJOURNAL_MT5_INTERACTIVE_USER"
            else ""
        ),
    )
    completed = Mock(returncode=0)
    monkeypatch.setattr(
        runtime,
        "_interactive_session_present",
        lambda _user: True,
    )

    with patch("subprocess.run", return_value=completed) as run:
        assert runtime._start_process(config, 42) is None

    calls = [entry.args[0] for entry in run.call_args_list]
    expected_acl_calls = [
        [
            "icacls",
            str(runtime.root),
            "/grant:r",
            "TradeJournalMT5:(RX)",
        ],
        [
            "icacls",
            str(runtime.state),
            "/grant:r",
            "TradeJournalMT5:(RX)",
        ],
        [
            "icacls",
            str(runtime.terminal_root),
            "/grant:r",
            "TradeJournalMT5:(OI)(CI)(M)",
        ],
        [
            "icacls",
            str(runtime.state / "launch-terminal.cmd"),
            "/grant:r",
            "TradeJournalMT5:(RX)",
        ],
    ]
    assert calls[:4] == expected_acl_calls
    assert not any("secrets" in argument for call_args in calls for argument in call_args)

    create = calls[4]
    assert create[:3] == ["schtasks", "/Create", "/TN"]
    assert create[create.index("/RU") + 1] == "TradeJournalMT5"
    assert "/IT" in create
    assert create[create.index("/RL") + 1] == "LIMITED"
    assert "HIGHEST" not in create
    run.assert_has_calls(
        [
            *[
                call(
                    acl_call,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                for acl_call in expected_acl_calls
            ],
            call(create, capture_output=True, text=True, check=False),
            call(
                ["schtasks", "/Run", "/TN", runtime._interactive_task],
                capture_output=True,
                text=True,
                check=False,
            ),
        ]
    )
    launcher = (runtime.state / "launch-terminal.cmd").read_text(encoding="utf-8")
    assert "/portable" in launcher
    assert "/login:42" in launcher
    assert str(config) in launcher


def test_reboot_recovery_waits_for_dedicated_interactive_session(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime = _runtime(tmp_path)
    probes = iter((False, False, True))
    sleeps: list[float] = []
    monkeypatch.setattr(
        runtime,
        "_interactive_session_present",
        lambda _user: next(probes),
    )
    monkeypatch.setattr(
        "windows_agent.worker.native_mt5_runtime.time.sleep",
        lambda seconds: sleeps.append(seconds),
    )

    runtime._wait_for_interactive_session("TradeJournalMT5", timeout=90)

    assert sleeps == [0.5, 0.5]


def test_reboot_recovery_fails_closed_without_interactive_session(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime = _runtime(tmp_path)
    clock = iter((0.0, 0.0, 1.0))
    monkeypatch.setattr(
        runtime,
        "_interactive_session_present",
        lambda _user: False,
    )
    monkeypatch.setattr(
        "windows_agent.worker.native_mt5_runtime.time.monotonic",
        lambda: next(clock),
    )
    monkeypatch.setattr(
        "windows_agent.worker.native_mt5_runtime.time.sleep",
        lambda _seconds: None,
    )

    with pytest.raises(
        NativeMt5Error,
        match="interactive_session_unavailable",
    ):
        runtime._wait_for_interactive_session(
            "TradeJournalMT5",
            timeout=0.5,
        )


def test_successful_interactive_launch_deletes_one_shot_task(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    runtime.state.mkdir()
    launcher = runtime.state / "launch-terminal.cmd"
    launcher.write_text("@echo off\r\n", encoding="utf-8")
    runtime._interactive_task = "TradeJournalMT5-fixture"

    with patch("subprocess.run", return_value=Mock(returncode=0)) as run:
        runtime._release_interactive_task()

    run.assert_called_once_with(
        [
            "schtasks",
            "/Delete",
            "/TN",
            "TradeJournalMT5-fixture",
            "/F",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert runtime._interactive_task is None
    assert not launcher.exists()


def test_failed_interactive_task_delete_remains_cleanup_eligible(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    runtime._interactive_task = "TradeJournalMT5-fixture"

    with (
        patch("subprocess.run", return_value=Mock(returncode=1)),
        pytest.raises(NativeMt5Error, match="interactive_task_cleanup_failed"),
    ):
        runtime._release_interactive_task()

    assert runtime._interactive_task == "TradeJournalMT5-fixture"


def test_wait_for_authorization_reads_only_new_journal_lines(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    logs = runtime.terminal_root / "logs"
    logs.mkdir()
    journal = logs / "20260718.log"
    old = "AA\t0\t10:00:00\tNetwork\t'42': Invalid account\r\n".encode("utf-16-le")
    journal.write_bytes(old)
    checkpoint = runtime._journal_checkpoint()
    with journal.open("ab") as handle:
        handle.write(
            "BB\t0\t10:00:01\tNetwork\t'42': authorized on Demo through Access Point\r\n".encode(
                "utf-16-le"
            )
        )
    with patch.object(runtime, "_running_terminal_pids", return_value=[123]):
        runtime._wait_for_authorization(checkpoint, 42, "Demo", 1.0)


def test_wait_for_authorization_returns_redirected_server_for_same_login(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    logs = runtime.terminal_root / "logs"
    logs.mkdir()
    (logs / "20260729.log").write_bytes(
        (
            "AA\t0\t08:14:26\tNetwork\t"
            "'42': authorized on PepperstoneEU-Live\r\n"
        ).encode("utf-16-le")
    )

    with patch.object(runtime, "_running_terminal_pids", return_value=[123]):
        effective_server = runtime._wait_for_authorization(
            {},
            42,
            "PepperstoneUK-Live",
            1.0,
        )

    assert effective_server == "PepperstoneEU-Live"


def test_start_uses_redirected_server_for_followup_identity_checks(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    expert = tmp_path / "bridge.ex5"
    expert.write_bytes(b"expert")
    runtime.state.mkdir()
    configs = [
        runtime.state / "login-bootstrap.ini",
        runtime.state / "startup.ini",
    ]
    for config in configs:
        config.write_text("temporary", encoding="utf-8")
    ready = NativeMt5Status(
        pid=123,
        account={
            "login": "42",
            "server": "PepperstoneEU-Live",
            "trade_allowed": False,
        },
        heartbeat={"terminal_connected": True},
        files_path=runtime.files,
    )

    with (
        patch.object(runtime, "install_expert"),
        patch.object(
            runtime,
            "_write_startup_config",
            side_effect=configs,
        ),
        patch.object(runtime, "_journal_checkpoint", return_value={}),
        patch.object(runtime, "_start_process"),
        patch.object(
            runtime,
            "_wait_for_authorization",
            side_effect=[
                "PepperstoneEU-Live",
                "PepperstoneEU-Live",
            ],
        ) as wait_for_authorization,
        patch.object(runtime, "_wait_for_account_database"),
        patch.object(
            runtime,
            "_cached_broker_symbol",
            return_value="EURUSD.raw",
        ),
        patch.object(runtime, "_wait_for_investor_sync"),
        patch.object(runtime, "_remove_readiness_files"),
        patch.object(runtime, "_write_symbol_preference"),
        patch.object(
            runtime,
            "_probe_broker_symbol",
            return_value="EURUSD.raw",
        ),
        patch.object(runtime, "_install_bridge_template") as install_template,
        patch.object(runtime, "_publish_bridge_handoff"),
        patch.object(
            runtime,
            "_wait_for_heartbeat",
            return_value=ready,
        ) as wait_for_heartbeat,
        patch.object(runtime, "stop", return_value=True),
        patch("gc.collect"),
    ):
        result = runtime.start(
            login=42,
            server="PepperstoneUK-Live",
            investor_password="placeholder",
            expert_binary=expert,
        )

    assert result.requested_server == "PepperstoneUK-Live"
    assert result.effective_server == "PepperstoneEU-Live"
    assert wait_for_authorization.call_args_list[1].args[2] == (
        "PepperstoneEU-Live"
    )
    install_template.assert_called_once_with("EURUSD.raw")
    wait_for_heartbeat.assert_called_once_with(
        90.0,
        42,
        "PepperstoneEU-Live",
    )


@pytest.mark.parametrize(
    ("message", "expected_code"),
    [
        (
            "'42': connection to 203.0.113.10:443 failed",
            "endpoint_connection_failed",
        ),
        (
            "'42': connection refused by 203.0.113.10:443",
            "endpoint_connection_refused",
        ),
        (
            "'42': protocol mismatch at 203.0.113.10:443",
            "endpoint_protocol_incompatible",
        ),
        (
            "'42': unknown server 203.0.113.10:443",
            "endpoint_server_unrecognized",
        ),
    ],
)
def test_wait_for_authorization_classifies_exact_endpoint_failure(
    tmp_path: Path,
    message: str,
    expected_code: str,
) -> None:
    runtime = _runtime(tmp_path)
    logs = runtime.terminal_root / "logs"
    logs.mkdir()
    (logs / "20260718.log").write_bytes(
        f"AA\t0\t10:00:00\tNetwork\t{message}\r\n".encode("utf-16-le")
    )

    with pytest.raises(NativeMt5Error, match=expected_code):
        runtime._wait_for_authorization(
            {},
            42,
            "Demo",
            1.0,
            "203.0.113.10:443",
        )


def test_invalid_account_remains_auth_failure_even_with_endpoint(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    logs = runtime.terminal_root / "logs"
    logs.mkdir()
    (logs / "20260718.log").write_bytes(
        (
            "AA\t0\t10:00:00\tNetwork\t"
            "'42': invalid account at 203.0.113.10:443\r\n"
        ).encode("utf-16-le")
    )

    with pytest.raises(NativeMt5Error, match="authorization_failed"):
        runtime._wait_for_authorization(
            {},
            42,
            "Demo",
            1.0,
            "203.0.113.10:443",
        )


def test_wait_for_heartbeat_binds_pid_after_interactive_task_release(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    runtime.files.mkdir(parents=True)
    (runtime.files / "account.json").write_text(
        _envelope({"login": "42", "server": "Demo", "trade_allowed": False})
    )
    (runtime.files / "heartbeat.json").write_text(
        _envelope({"terminal_connected": True})
    )
    runtime._interactive_task = None
    runtime._process = None

    with (
        patch.object(runtime, "_running_terminal_pids", return_value=[456]),
        patch.object(runtime, "set_terminal_window_visibility") as set_visibility,
    ):
        status = runtime._wait_for_heartbeat(1.0, 42, "Demo")

    assert status.pid == 456
    set_visibility.assert_called_once_with(456, visible=False)


def test_terminal_window_visibility_uses_pid_creation_time_and_interactive_task(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    runtime.state.mkdir()
    runtime.files.mkdir(parents=True)
    creation_time_unix_ms = 1_785_230_000_123
    commands: list[list[str]] = []

    def run_command(arguments, **kwargs):
        del kwargs
        command = [str(value) for value in arguments]
        commands.append(command)
        if "/Run" in command:
            (runtime.files / "window-visibility-result.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "success": True,
                        "action": "hide",
                        "process_id": 456,
                        "creation_time_unix_ms": creation_time_unix_ms,
                        "windows_matched": 1,
                        "visible_before": 1,
                        "visible_after": 0,
                    }
                ),
                encoding="utf-8",
            )
        return Mock(returncode=0)

    with (
        patch.object(runtime, "_interactive_user", return_value="TradeJournalMT5"),
        patch.object(
            runtime,
            "_terminal_process_identity",
            return_value=(runtime.terminal.resolve(), creation_time_unix_ms),
        ),
        patch.object(runtime, "_restrict_private_acl"),
        patch.object(runtime, "_grant_interactive_acl"),
        patch("subprocess.run", side_effect=run_command),
    ):
        result = runtime.set_terminal_window_visibility(456, visible=False)

    assert result["visible_after"] == 0
    create = next(command for command in commands if "/Create" in command)
    assert "/IT" in create
    assert "/RU" in create
    assert create[create.index("/RU") + 1] == "TradeJournalMT5"
    assert not (runtime.state / "window-visibility-request.json").exists()
    assert not (runtime.state / "set-window-visibility.cmd").exists()
    assert not (runtime.files / "window-visibility-result.json").exists()
    serialized_commands = " ".join(" ".join(command) for command in commands)
    assert "Password=" not in serialized_commands
    assert "Token=" not in serialized_commands


def test_wait_for_investor_sync_requires_sync_and_readonly_lines(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    logs = runtime.terminal_root / "logs"
    logs.mkdir()
    journal = logs / "20260718.log"
    checkpoint = runtime._journal_checkpoint()
    journal.write_bytes(
        (
            "AA\t0\t10:00:01\tNetwork\t'42': terminal synchronized with Demo Ltd.\r\n"
            "BB\t0\t10:00:02\tNetwork\t'42': trading has been disabled - investor mode\r\n"
        ).encode("utf-16-le")
    )
    with patch.object(runtime, "_running_terminal_pids", return_value=[123]):
        runtime._wait_for_investor_sync(checkpoint, 42, 1.0)


@pytest.mark.parametrize(
    ("account", "code"),
    [
        ({"login": "99", "server": "Demo", "trade_allowed": False}, "identity_mismatch"),
        ({"login": "42", "server": "Other", "trade_allowed": False}, "server_identity_mismatch"),
        ({"login": "42", "server": "Demo", "trade_allowed": True}, "investor_readonly_not_verified"),
    ],
)
def test_identity_and_readonly_guards(
    tmp_path: Path, account: dict[str, object], code: str
) -> None:
    runtime = _runtime(tmp_path)
    expert = tmp_path / "bridge.ex5"
    expert.write_bytes(b"expert")
    runtime.files.mkdir(parents=True)
    (runtime.files / "account.json").write_text(_envelope(account))
    (runtime.files / "heartbeat.json").write_text(
        _envelope({"terminal_connected": True})
    )
    bootstrap = runtime.state / "login-bootstrap.ini"
    startup = runtime.state / "startup.ini"
    runtime.state.mkdir()
    bootstrap.write_text("temporary")
    startup.write_text("temporary")
    with (
        patch.object(
            runtime,
            "_write_startup_config",
            side_effect=[bootstrap, startup],
        ),
        patch.object(runtime, "_journal_checkpoint", return_value={}),
        patch.object(runtime, "_start_process"),
        patch.object(runtime, "_wait_for_authorization"),
        patch.object(runtime, "_wait_for_account_database"),
        patch.object(
            runtime,
            "_cached_broker_symbol",
            return_value="EURUSD.raw",
        ),
        patch.object(runtime, "_wait_for_investor_sync"),
        patch.object(runtime, "_remove_readiness_files"),
        patch.object(runtime, "_write_symbol_preference"),
        patch.object(
            runtime,
            "_probe_broker_symbol",
            return_value="EURUSD.raw",
        ),
        patch.object(runtime, "_publish_bridge_handoff"),
        patch.object(runtime, "stop", return_value=True),
    ):
        with pytest.raises(NativeMt5Error, match=code):
            runtime.start(
                login=42,
                server="Demo",
                investor_password="placeholder",
                expert_binary=expert,
            )
    assert not bootstrap.exists()
    assert not startup.exists()


def test_crashed_process_is_reported(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    process = Mock(pid=123)
    process.poll.return_value = 1
    runtime._process = process
    with patch.object(runtime, "_running_terminal_pids", return_value=[]):
        with pytest.raises(NativeMt5Error, match="mt5_process_crashed"):
            runtime._wait_for_heartbeat(1.0, 42, "Demo")


def test_stop_treats_process_exit_during_cleanup_as_success(tmp_path: Path, monkeypatch) -> None:
    runtime = _runtime(tmp_path)

    class ProcessExited:
        def terminate(self) -> None:
            raise psutil.NoSuchProcess(123)

    import psutil

    pid_scans = iter(([123], []))
    monkeypatch.setattr(runtime, "_running_terminal_pids", lambda: next(pid_scans))
    monkeypatch.setattr(runtime, "_running_metaeditor_pids", lambda: [])
    monkeypatch.setattr(psutil, "Process", lambda pid: ProcessExited())
    monkeypatch.setattr(psutil, "wait_procs", lambda processes, timeout: ([], []))

    assert runtime.stop(timeout=0.1) is True


def test_stop_waits_after_forced_kill_before_final_rescan(tmp_path: Path, monkeypatch) -> None:
    runtime = _runtime(tmp_path)
    calls: list[list[object]] = []

    class ProcessStillAlive:
        def terminate(self) -> None:
            return None

        def kill(self) -> None:
            return None

    import psutil

    process = ProcessStillAlive()
    pid_scans = iter(([123], [], []))
    monkeypatch.setattr(runtime, "_running_terminal_pids", lambda: next(pid_scans))
    monkeypatch.setattr(runtime, "_running_metaeditor_pids", lambda: [])
    monkeypatch.setattr(psutil, "Process", lambda pid: process)

    def wait_procs(processes, timeout):
        calls.append(list(processes))
        return ([], [process]) if len(calls) == 1 else ([process], [])

    monkeypatch.setattr(psutil, "wait_procs", wait_procs)

    assert runtime.stop(timeout=0.1) is True
    assert calls == [[process], [process]]


def test_running_bridge_switches_to_history_without_restarting(tmp_path):
    runtime = _runtime(tmp_path)
    live = json.loads(_envelope({"history_mode": "new_only", "terminal_connected": True, "history_mode_switch_supported": True}))
    history = {**live, "sequence": live["sequence"] + 1, "payload": {"history_mode": "history", "terminal_connected": True}}
    with patch.object(runtime, "_read_json", side_effect=[live, history]), patch.object(runtime, "stop", side_effect=AssertionError("restart forbidden")):
        runtime.switch_to_history_mode("all_available", timeout=1)
    assert (runtime.files / "history_mode").read_text() == "all_available"


def test_legacy_live_bridge_requires_one_owned_process_before_publishing(tmp_path):
    runtime = _runtime(tmp_path)
    live = json.loads(_envelope({"history_mode": "new_only", "terminal_connected": True}))
    with patch.object(runtime, "_read_json", return_value=live), patch.object(runtime, "_running_terminal_pids", return_value=[]), patch.object(runtime, "_publish_history_mode") as publish:
        with pytest.raises(NativeMt5Error, match="history_mode_switch_process_identity_unavailable"):
            runtime.switch_to_history_mode("all_available")
    publish.assert_not_called()


def test_legacy_live_bridge_refreshes_chart_and_awaits_history_ack(tmp_path):
    runtime = _runtime(tmp_path)
    live = json.loads(_envelope({"history_mode": "new_only", "terminal_connected": True}))
    history = {**live, "sequence": live["sequence"] + 1, "payload": {"history_mode": "history", "terminal_connected": True}}
    with patch.object(runtime, "_read_json", side_effect=[live, history]), patch.object(runtime, "_running_terminal_pids", return_value=[123]), patch.object(runtime, "refresh_history_chart") as refresh, patch.object(runtime, "stop", side_effect=AssertionError("restart forbidden")):
        runtime.switch_to_history_mode("all_available", timeout=1)
    refresh.assert_called_once_with(123, timeout=1)
    assert (runtime.files / "history_mode").read_text() == "all_available"


def test_existing_frozen_history_does_not_require_sequence_advance(tmp_path):
    runtime = _runtime(tmp_path)
    history = json.loads(_envelope({"history_mode": "history", "terminal_connected": True}))
    with patch.object(runtime, "_read_json", return_value=history), patch.object(runtime, "refresh_history_chart", side_effect=AssertionError("already frozen")):
        runtime.switch_to_history_mode("all_available", timeout=1)
    assert (runtime.files / "history_mode").read_text() == "all_available"
