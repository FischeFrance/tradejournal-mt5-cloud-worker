"""History must retain live-sync integrity while tolerating vendor tool updates."""
from __future__ import annotations

import hashlib
from types import SimpleNamespace
from uuid import uuid4

import pytest

from tests.windows.test_real_handlers import FakeApi, _handlers, _job, _provision_payload
from tests.windows.test_real_handlers import env  # noqa: F401 -- reuse the isolated DPAPI fixture
from windows_agent import real_handlers
from windows_agent.provisioning.instance_layout import InstanceLayout
from windows_agent.provisioning.mt5_instance import InstanceProvisioner
from windows_agent.provisioning.process_manager import ProcessManager


@pytest.fixture
def native_history(env, monkeypatch):
    cid = str(uuid4())
    assets = env.source_terminal.parent / "MQL5"
    for relative in (
        "Experts/TradeJournal/TradeJournalBridge.ex5",
        "Scripts/TradeJournal/TradeJournalDiscovery.ex5",
        "Scripts/TradeJournal/TradeJournalLoader.ex5",
    ):
        path = assets / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"managed-runtime")
    for name in ("metaeditor64.exe", "metatester64.exe"):
        (env.source_terminal.parent / name).write_bytes(b"original-vendor-tool")
    api = FakeApi()
    _handlers(env, api)["provision"](_job("provision", cid, payload=_provision_payload()))
    root = InstanceLayout(env.instances_root, cid).path
    provisioner = InstanceProvisioner(env.instances_root, env.secrets_root)
    provisioner.seal_runtime_assets(cid)
    files = root / "terminal/MQL5/Files/TradeJournal"
    files.mkdir(parents=True)
    (files / "history_mode").write_text("all_available")
    calls = []

    class Runtime:
        def __init__(self, *_args):
            pass

        def set_cancel_check(self, _check):
            pass

        def switch_to_history_mode(self, mode):
            calls.append(("switch", mode))
            (files / "history_mode").write_text(mode)

        def switch_to_new_only(self):
            calls.append("new_only")

    monkeypatch.setattr(ProcessManager, "find", staticmethod(lambda _path: [123]))
    monkeypatch.setattr(real_handlers, "_verify_investor_access", lambda _adapter: {"trade_allowed": False})
    monkeypatch.setattr(real_handlers, "_prepare_history_to_live_handoff", lambda *_args: 0)

    def import_history(*_args):
        calls.append("history")
        return {"deals": 44, "orders": 55}

    monkeypatch.setattr(real_handlers, "_run_history_sync", import_history)

    def run(*, template_digest=None, payload=None, mode="all_available", from_date=None):
        handlers = real_handlers.build_real_handlers(
            api,
            instances_root=env.instances_root,
            secrets_root=env.secrets_root,
            source_terminal=env.source_terminal,
            terminal_sha256=template_digest,
            runtime_factory=Runtime,
        )
        return handlers["historical_sync"](_job("historical_sync", cid, history_mode=mode, payload=payload, from_date=from_date))

    return SimpleNamespace(root=root, provisioner=provisioner, cid=cid, calls=calls, run=run, runtime=Runtime)


@pytest.mark.parametrize("fails", [False, True])
def test_ticket_recovery_clears_cursor_only_after_delivery_and_live_restore(native_history, monkeypatch, fails):
    from datetime import datetime, timezone
    from windows_agent.state_store import atomic_json
    from windows_agent.agent_errors import HistorySyncFailed
    case = native_history
    captured = datetime.now(timezone.utc).isoformat()
    sha = "b" * 64
    pointer = case.root / "state/maintenance-recovery.json"
    atomic_json(pointer, {"baseline_sha256": sha, "status": "queued"})
    monkeypatch.setattr("windows_agent.maintenance_ticket_recovery.load_baseline", lambda *_args: {"captured_at": captured})
    monkeypatch.setattr("windows_agent.maintenance_ticket_recovery.TicketDeltaAdapter", lambda adapter, _baseline: adapter)
    if fails:
        def unavailable(*_args):
            raise HistorySyncFailed("delivery not acknowledged")
        monkeypatch.setattr(real_handlers, "_run_history_sync", unavailable)
    request = dict(payload={"maintenance_recovery": True, "baseline_sha256": sha}, mode="from_date", from_date=captured)
    if fails:
        with pytest.raises(HistorySyncFailed):
            case.run(**request)
        assert pointer.exists()
    else:
        assert case.run(**request)["imported_deals"] == 44
        assert not pointer.exists()
    assert case.calls[-1] == "new_only"


def test_vendor_tool_updates_do_not_block_native_history(native_history):
    case = native_history
    for name in ("metaeditor64.exe", "metatester64.exe"):
        (case.root / "terminal" / name).write_bytes(b"newer-vendor-tool")
    # Reproduce the exact failing precondition; the strict publication gate still rejects it.
    with pytest.raises(ValueError, match="code manifest mismatch"):
        case.provisioner.validate(case.cid)
    result = case.run()
    assert result["imported_deals"] == 44
    assert result["imported_orders"] == 55
    assert case.calls == ["history", "new_only"]


def test_a_new_global_template_does_not_invalidate_an_existing_account(native_history):
    case = native_history
    newer_template_digest = hashlib.sha256(b"newer-global-terminal").hexdigest()
    assert case.run(template_digest=newer_template_digest)["imported_deals"] == 44


@pytest.mark.parametrize("relative, error_code", [
    ("terminal64.exe", "terminal_start_failed"),
    ("MQL5/Experts/TradeJournal/TradeJournalBridge.ex5", "instance_provision_failed"),
    ("MQL5/Scripts/TradeJournal/TradeJournalLoader.ex5", "instance_provision_failed"),
])
def test_terminal_and_managed_assets_still_fail_before_history_is_read(native_history, relative, error_code):
    case = native_history
    (case.root / "terminal" / relative).write_bytes(b"unexpected-change")
    with pytest.raises(Exception) as exc:
        case.run()
    assert exc.value.error_code == error_code
    assert case.calls == []


def test_running_history_mode_switch_never_stops_the_terminal(native_history, monkeypatch):
    case = native_history
    (case.root / "terminal/MQL5/Files/TradeJournal/history_mode").write_text("new_only")
    monkeypatch.setattr(ProcessManager, "stop", lambda *_a: pytest.fail("mode-only request stopped MT5"))
    assert case.run()["imported_deals"] == 44
    assert case.calls == [("switch", "all_available"), "history", "new_only"]


def test_failed_import_restores_connected_live_marker(native_history, monkeypatch):
    from windows_agent.state_store import read_json
    from windows_agent.agent_errors import HistorySyncFailed
    def fail(*_args):
        raise HistorySyncFailed("test history failure")
    monkeypatch.setattr(real_handlers, "_run_history_sync", fail)
    with pytest.raises(HistorySyncFailed):
        native_history.run()
    assert native_history.calls == ["new_only"]
    assert read_json(native_history.root / "state/job_progress.json")["status"] == "connected"


def test_failed_mode_switch_restores_verified_live_marker(native_history, monkeypatch):
    from windows_agent.state_store import read_json
    from windows_agent.worker.native_mt5_runtime import NativeMt5Error
    case = native_history
    (case.root / "terminal/MQL5/Files/TradeJournal/history_mode").write_text("new_only")
    def fail(*_args):
        raise NativeMt5Error("history_mode_change_requires_expert_update")
    monkeypatch.setattr(case.runtime, "switch_to_history_mode", fail)
    with pytest.raises(Exception) as exc:
        case.run()
    assert exc.value.error_code == "mt5_initialize_failed"
    assert case.calls == ["new_only"]
    assert read_json(case.root / "state/job_progress.json")["status"] == "connected"
