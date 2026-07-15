from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest

from windows_agent.customer_flow import (
    LocalControlPlane,
    REPORT_KEYS,
    run_customer_flow,
)
from windows_agent.worker.direct_mt5_adapter import IdentityMismatch, Mt5Error


class Secrets:
    def __init__(self, missing=False):
        self.values = {}
        self.missing = missing

    def write(self, cid, name, value):
        self.values[(cid, name)] = f"encrypted:{len(value)}"

    def read(self, cid, name):
        if self.missing:
            raise FileNotFoundError()
        return "fixture-investor"


class Process:
    def __init__(self, _path, fail=False):
        self.fail = fail

    def start(self, _terminal):
        if self.fail:
            raise OSError()
        return 123

    def stop(self):
        return True


class Adapter:
    def __init__(self, _terminal, login, server, scenario="ok"):
        self.login, self.server, self.scenario = login, server, scenario

    @contextmanager
    def session(self, _password):
        if self.scenario == "login":
            raise Mt5Error("MT5 authorization failed")
        if self.scenario == "initialize":
            raise Mt5Error("MT5 initialization failed")
        if self.scenario == "crash":
            from windows_agent.worker.direct_mt5_adapter import Mt5ProcessCrashed

            raise Mt5ProcessCrashed("terminal exited")
        yield self

    def account_info(self):
        return SimpleNamespace(trade_allowed=self.scenario == "master")

    def terminal_info(self):
        return SimpleNamespace(connected=True)

    def verify_identity(self):
        if self.scenario == "identity":
            raise IdentityMismatch("mismatch")
        return {"login": str(self.login), "server": self.server}

    def positions(self):
        return ()

    def orders(self):
        return ()

    def history_orders(self, *_args):
        return ()

    def history_deals(self, *_args):
        return ()

    def snapshot(self):
        return {"positions": {}, "orders": {}, "deals": {}}


def request():
    return {
        "connection_id": str(uuid4()),
        "login": 42,
        "server": "Broker-Demo",
        "investor_password": "fixture-investor",
        "history_mode": "all_available",
    }


def run(
    tmp_path,
    payload=None,
    scenario="ok",
    control=None,
    secrets=None,
    process_fail=False,
    disconnect=False,
):
    terminal = tmp_path / "source" / "terminal64.exe"
    terminal.parent.mkdir(parents=True, exist_ok=True)
    terminal.touch()
    return run_customer_flow(
        payload or request(),
        instances_root=tmp_path / "instances",
        secrets_root=tmp_path / "secrets",
        source_terminal=terminal,
        control_plane=control,
        secret_store=secrets or Secrets(),
        adapter_factory=lambda terminal, login, server: Adapter(
            terminal, login, server, scenario
        ),
        process_factory=lambda p: Process(p, process_fail),
        simulate_disconnect=disconnect,
    )


def test_single_customer_mock_e2e(tmp_path):
    payload = request()
    control = LocalControlPlane()
    report = run(tmp_path, payload, control=control, disconnect=True)
    assert (
        report["final_result"] == "PASS" and report["final_status"] == "deprovisioned"
    )
    assert tuple(report) == REPORT_KEYS and "investor_password" not in payload
    assert control.statuses == [
        "pending",
        "provisioning",
        "authenticating",
        "importing_history",
        "connected",
        "deprovisioned",
    ]
    assert all(
        report[k]
        for k in (
            "secret_encrypted",
            "job_claimed",
            "mt5_login_succeeded",
            "investor_readonly_verified",
            "history_import_completed",
            "live_sync_started",
            "heartbeat_received",
        )
    )


@pytest.mark.parametrize(
    ("scenario", "code"),
    [
        ("login", "mt5_login_failed"),
        ("initialize", "mt5_initialize_failed"),
        ("identity", "identity_mismatch"),
        ("master", "investor_readonly_not_verified"),
    ],
)
def test_mt5_failures_are_sanitized(tmp_path, scenario, code):
    report = run(tmp_path, scenario=scenario)
    assert (
        report["final_status"] == f"error:{code}" and report["final_result"] == "FAIL"
    )


def test_empty_history_is_success(tmp_path):
    for case in ("wrong_password", "wrong_server", "wrong_login"):
        report = run(tmp_path / case, scenario="login")
        assert report["final_status"] == "error:mt5_login_failed"


def test_empty_history_is_success_original(tmp_path):
    report = run(tmp_path)
    assert report["final_result"] == "PASS" and report["deals_count"] == 0


def test_lease_lost(tmp_path):
    control = LocalControlPlane()
    control.lease_valid = False
    assert run(tmp_path, control=control)["final_status"] == "error:lease_lost"


def test_secret_missing(tmp_path):
    assert (
        run(tmp_path, secrets=Secrets(True))["final_status"] == "error:secret_missing"
    )


def test_terminal_not_startable(tmp_path):
    assert (
        run(tmp_path, scenario="crash")["final_status"] == "error:mt5_process_crashed"
    )


def test_existing_connection(tmp_path):
    payload = request()
    (tmp_path / "instances" / payload["connection_id"]).mkdir(parents=True)
    assert run(tmp_path, payload)["final_status"] == "error:connection_exists"
