from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from windows_agent.api_client import AgentApiClient
from windows_agent.job_runner import JobRunner
from windows_agent.provisioning.instance_layout import InstanceLayout, SUBDIRS
from windows_agent.provisioning.mt5_instance import InstanceProvisioner
from windows_agent.provisioning.secret_store import WindowsSecretStore
from windows_agent.security import RedactionFilter, canonical_uuid, safe_child
from windows_agent.state_store import atomic_json, read_json
from windows_agent.worker.dedup import PersistentDedup
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
        violations += [
            (path, item)
            for item in forbidden
            if re.search(rf"\b{re.escape(item)}\s*\(", text)
        ]
    assert violations == []


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
        return {"status": "failed" if status == "fail" else status}

    def heartbeat(self, job, lease):
        return {"lease_valid": self.lease}


def test_lease_lost_never_completes(tmp_path):
    api = FakeApi(False)
    runner = JobRunner(
        tmp_path / "job.json", api, {"provision": lambda job: {"ok": True}}
    )
    assert runner.run_once() is False and "complete" not in api.transitions


def test_unacknowledged_running_transition_never_executes_handler(tmp_path):
    api = FakeApi()
    called = []

    def transition(job, lease, status, result=None):
        api.transitions.append(status)
        return {"error_code": "lease_lost"}

    api.transition = transition
    runner = JobRunner(
        tmp_path / "job.json", api, {"provision": lambda job: called.append(job)}
    )

    assert runner.run_once() is False
    assert called == []
    assert read_json(tmp_path / "job.json")["status"] == "lease_lost"


def test_unacknowledged_complete_transition_is_not_reported_complete(tmp_path):
    api = FakeApi()

    def transition(job, lease, status, result=None):
        api.transitions.append(status)
        if status == "complete":
            return {"error_code": "lease_lost"}
        return {"status": status}

    api.transition = transition
    runner = JobRunner(
        tmp_path / "job.json", api, {"provision": lambda job: {"ok": True}}
    )

    assert runner.run_once() is False
    assert read_json(tmp_path / "job.json")["status"] == "lease_lost"


def test_fake_provision_deprovision_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(WindowsSecretStore, "delete_connection", lambda *args: None)
    provisioner = InstanceProvisioner(tmp_path / "instances", tmp_path / "secrets")
    cid = str(uuid4())
    root = provisioner.provision(cid)
    provisioner.deprovision(cid)
    provisioner.deprovision(cid)
    assert read_json(root / "state" / "instance.json")["status"] == "deprovisioned"


def test_instance_provision_rejects_symlinked_template_content(tmp_path):
    source = tmp_path / "template"
    source.mkdir()
    (source / "terminal64.exe").write_bytes(b"terminal")
    outside = tmp_path / "outside.dat"
    outside.write_bytes(b"outside")
    try:
        (source / "linked.dat").symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    provisioner = InstanceProvisioner(
        tmp_path / "instances", tmp_path / "secrets"
    )
    with pytest.raises(ValueError, match="reparse"):
        provisioner.provision(str(uuid4()), source / "terminal64.exe")


def test_instance_provision_pins_and_records_terminal_digest(tmp_path):
    source = tmp_path / "template"
    source.mkdir()
    terminal = source / "terminal64.exe"
    terminal.write_bytes(b"terminal")
    expected = hashlib.sha256(b"terminal").hexdigest()
    connection_id = str(uuid4())
    provisioner = InstanceProvisioner(
        tmp_path / "instances", tmp_path / "secrets"
    )

    root = provisioner.provision(
        connection_id, terminal, expected_terminal_sha256=expected
    )

    state = read_json(root / "state" / "instance.json")
    assert state["terminal_sha256"] == expected
    with pytest.raises(ValueError, match="digest mismatch"):
        provisioner.provision(
            str(uuid4()), terminal, expected_terminal_sha256="0" * 64
        )


def test_instance_removes_only_known_mt5_generated_example_code(tmp_path):
    source = tmp_path / "template"
    source.mkdir()
    terminal = source / "terminal64.exe"
    terminal.write_bytes(b"terminal")
    connection_id = str(uuid4())
    provisioner = InstanceProvisioner(
        tmp_path / "instances", tmp_path / "secrets"
    )
    root = provisioner.provision(connection_id, terminal)
    generated = (
        root
        / "terminal"
        / "MQL5"
        / "Experts"
        / "Advisors"
        / "ExpertMACD.ex5"
    )
    generated.parent.mkdir(parents=True)
    generated.write_bytes(b"mt5 default example")

    removed = provisioner.remove_generated_example_code(connection_id)

    assert removed == ("MQL5/Experts/Advisors",)
    assert not generated.exists()
    assert provisioner.validate(connection_id) == root


def test_instance_keeps_unknown_executable_for_fail_closed_validation(tmp_path):
    source = tmp_path / "template"
    source.mkdir()
    terminal = source / "terminal64.exe"
    terminal.write_bytes(b"terminal")
    connection_id = str(uuid4())
    provisioner = InstanceProvisioner(
        tmp_path / "instances", tmp_path / "secrets"
    )
    root = provisioner.provision(connection_id, terminal)
    unknown = root / "terminal" / "MQL5" / "Experts" / "Unknown" / "foreign.ex5"
    unknown.parent.mkdir(parents=True)
    unknown.write_bytes(b"unknown executable")

    assert provisioner.remove_generated_example_code(connection_id) == ()
    assert unknown.exists()
    with pytest.raises(ValueError, match="code manifest"):
        provisioner.validate(connection_id)


def test_instance_provision_failure_leaves_no_partial_publication(
    tmp_path, monkeypatch
):
    source = tmp_path / "template"
    source.mkdir()
    terminal = source / "terminal64.exe"
    terminal.write_bytes(b"terminal")
    connection_id = str(uuid4())
    provisioner = InstanceProvisioner(
        tmp_path / "instances", tmp_path / "secrets"
    )

    def fail_copy(*args, **kwargs):
        raise OSError("fixture copy failure")

    monkeypatch.setattr(
        "windows_agent.provisioning.mt5_instance.shutil.copy2", fail_copy
    )
    with pytest.raises(OSError, match="fixture copy failure"):
        provisioner.provision(connection_id, terminal)

    assert not (tmp_path / "instances" / connection_id).exists()
    assert not list((tmp_path / "instances").glob(".*.staging"))
