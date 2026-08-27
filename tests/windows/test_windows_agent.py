from __future__ import annotations

import logging
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from windows_agent.api_client import AgentApiClient
from windows_agent.config import AgentConfig
from windows_agent.job_runner import JobRunner
from windows_agent.provisioning.instance_layout import InstanceLayout, SUBDIRS
from windows_agent.provisioning.mt5_instance import InstanceProvisioner
from windows_agent.provisioning.secret_store import WindowsSecretStore
from windows_agent.research import ResearchCollector
from windows_agent.security import RedactionFilter, canonical_uuid, safe_child
from windows_agent.state_store import atomic_json, read_json
from windows_agent.worker.dedup import PersistentDedup
from windows_agent.worker.direct_mt5_adapter import DirectMt5Adapter, IdentityMismatch
from windows_agent.worker.live_sync import detect_windows_events


def test_uuid_and_path_traversal(tmp_path):
    valid = str(uuid4())
    assert canonical_uuid(valid) == valid
    assert safe_child(tmp_path, valid).parent == tmp_path.resolve()
    for bad in ("../bad", "ABC", valid.upper()):
        with pytest.raises(ValueError):
            canonical_uuid(bad)


def test_instance_isolation_and_state_without_secret(tmp_path):
    a, b = str(uuid4()), str(uuid4())
    pa, pb = InstanceLayout(tmp_path, a).create(), InstanceLayout(tmp_path, b).create()
    assert pa != pb and all((pa / item).is_dir() for item in SUBDIRS)
    with pytest.raises(ValueError):
        atomic_json(pa / "state" / "bad.json", {"password": "fixture"})


def test_atomic_state(tmp_path):
    path = tmp_path / "s.json"
    atomic_json(path, {"status": "ok"})
    assert read_json(path) == {"status": "ok"}
    assert not list(tmp_path.glob(".s.json.*"))


def test_redaction_filter():
    record = logging.LogRecord(
        "x", 20, "", 1, "Authorization: Bearer fake-value", (), None
    )
    assert RedactionFilter().filter(record)
    assert "fake-value" not in str(record.msg)


@pytest.mark.skipif(__import__("sys").platform != "win32", reason="Windows DPAPI only")
def test_dpapi_round_trip_and_acl(tmp_path):
    store = WindowsSecretStore(tmp_path)
    cid = str(uuid4())
    path = store.write(cid, "worker_token", "fixture-secret-value")
    assert b"fixture-secret-value" not in path.read_bytes()
    assert store.read(cid, "worker_token") == "fixture-secret-value"
    store.delete_connection(cid)
    assert not path.exists()


class Module:
    COPY_TICKS_ALL = 0

    def __init__(self, login=42, server="Demo"):
        self.account = type("A", (), {"login": login, "server": server})()

    def initialize(self, *args, **kwargs):
        return True

    def login(self, *args, **kwargs):
        return True

    def account_info(self):
        return self.account

    def shutdown(self):
        self.closed = True

    def positions_get(self):
        return ()

    def orders_get(self):
        return ()

    def history_deals_get(self, *args):
        return ()


def test_adapter_identity_and_final_shutdown(tmp_path):
    terminal = tmp_path / "terminal64.exe"
    terminal.touch()
    module = Module()
    adapter = DirectMt5Adapter(terminal, 42, "Demo", module)
    with adapter.session("fixture-investor"):
        assert adapter.verify_identity()["login"] == "42"
    assert module.closed
    bad = DirectMt5Adapter(terminal, 43, "Demo", Module())
    with pytest.raises(IdentityMismatch):
        with bad.session("fixture-investor"):
            pass
    assert bad._mt5.closed


def test_runtime_has_no_trading_calls():
    root = Path(__file__).parents[2] / "windows_agent"
    forbidden = (
        "order" + "_send",
        "order" + "_check",
        "position" + "_close",
        "position" + "_modify",
        "order" + "_delete",
        "order" + "_remove",
        "trade" + "_action",
    )
    violations = []
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8").casefold()
        violations += [(path, item) for item in forbidden if item in text]
    assert violations == []


def test_adapter_exposes_only_read_operations():
    public = {
        name
        for name in vars(DirectMt5Adapter)
        if not name.startswith("_") and name not in {"session", "verify_identity"}
    }
    assert public == {
        "terminal_info",
        "account_info",
        "positions",
        "orders",
        "history_orders",
        "history_deals",
        "rates",
        "ticks",
        "symbol_info",
        "symbol_tick",
        "snapshot",
        "initialize",
        "last_error",
        "verify_ipc_compatibility",
    }


def test_partial_close_and_new_deal():
    old = {
        "positions": {"1": {"volume": 1.0, "symbol": "EURUSD", "direction": "buy"}},
        "orders": {},
        "deals": {},
    }
    new = {
        "positions": {"1": {"volume": 0.4, "symbol": "EURUSD", "direction": "buy"}},
        "orders": {},
        "deals": {"9": {"position_ticket": "1", "commission": -1, "swap": -0.2}},
    }
    events = detect_windows_events(old, new)
    assert any(
        x["event_type"] == "trade_volume_changed" and x["partial_close"] for x in events
    )
    assert any(x["event_type"] == "deal_recorded" for x in events)


def test_dedup_survives_restart(tmp_path):
    path = tmp_path / "d.sqlite"
    assert PersistentDedup(path).add("event-1")
    assert PersistentDedup(path).contains("event-1")


def test_cross_host_redirect_refused():
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            302, headers={"location": "https://evil.invalid/x"}
        )
    )
    client = AgentApiClient("https://agent.example/", "fixture", transport)
    with pytest.raises(RuntimeError, match="cross-host"):
        client.claim()


class FakeApi:
    def __init__(self, lease=True):
        self.lease, self.transitions = lease, []

    def claim(self):
        return {"job_id": "j", "job_type": "provision", "connection_id": "c", "lease_id": "l"}

    def transition(self, job, lease, status, result=None):
        self.transitions.append(status)
        return {}

    def heartbeat(self, job, lease):
        return {"lease_valid": self.lease}


def test_lease_lost_never_completes(tmp_path):
    api = FakeApi(False)
    runner = JobRunner(
        tmp_path / "job.json", api, {"provision": lambda job: {"ok": True}}
    )
    assert runner.run_once() is False and "complete" not in api.transitions


def test_fake_provision_deprovision_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(WindowsSecretStore, "delete_connection", lambda *args: None)
    provisioner = InstanceProvisioner(tmp_path / "instances", tmp_path / "secrets")
    cid = str(uuid4())
    root = provisioner.provision(cid)
    provisioner.deprovision(cid)
    provisioner.deprovision(cid)
    assert read_json(root / "state" / "instance.json")["status"] == "deprovisioned"


def test_research_requires_server_allowlist(tmp_path):
    with pytest.raises(PermissionError):
        ResearchCollector(tmp_path / "r.db", False, True)
    disabled = ResearchCollector(tmp_path / "r2.db", False)
    disabled.add({"symbol": "EURUSD"})
    assert (
        disabled.connection.execute("select count(*) from market_data").fetchone()[0]
        == 0
    )


def test_config_defaults_closed():
    cfg = AgentConfig(str(uuid4()))
    assert not cfg.research_enabled and cfg.poll_seconds >= 0.25
    with pytest.raises(ValueError):
        AgentConfig(str(uuid4()), research_enabled=True)
