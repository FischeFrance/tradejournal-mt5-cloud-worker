from __future__ import annotations

import base64
import hashlib
import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from windows_agent import real_handlers
from windows_agent.agent_secrets import AGENT_SCOPE_ID, PROVISIONING_KEY_SECRET_NAME
from windows_agent.broker_endpoint_resolver import (
    BrokerEndpointResolutionError,
    VerifiedBrokerEndpoint,
)
from windows_agent.broker_identity import BrokerIdentitySuggestion
from windows_agent.broker_wizard import BrokerWizardError, BrokerWizardEvidence
from windows_agent.job_runner import JobRunner, LeaseLost
from windows_agent.provisioning.instance_layout import InstanceLayout
from windows_agent.provisioning.secret_store import WindowsSecretStore
from windows_agent.real_handlers import build_real_handlers, sweep_stale_instances
from windows_agent.state_store import read_json
from windows_agent.worker.adapter_errors import (
    IdentityMismatch,
    Mt5Error,
    Mt5IpcError,
)

ENCRYPTION_KEY = base64.b64encode(b"0" * 32).decode("ascii")


def _envelope(payload: dict, key_b64: str = ENCRYPTION_KEY) -> dict:
    key = base64.b64decode(key_b64)
    iv = os.urandom(12)
    ciphertext = AESGCM(key).encrypt(iv, json.dumps(payload).encode("utf-8"), None)
    return {
        "alg": "aes-256-gcm-v1",
        "iv": base64.b64encode(iv).decode("ascii"),
        "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
    }


def _job(job_type: str, cid: str, *, payload=None, history_mode="new_only", from_date=None, lease_id="lease-1") -> dict:
    return {
        "job_id": f"job-{job_type}",
        "job_type": job_type,
        "connection_id": cid,
        "lease_id": lease_id,
        "history_mode": history_mode,
        "from_date": from_date,
        "payload": payload or {},
    }


class FakeApi:
    """Minimal control-plane double: heartbeat can be scripted to fail lease checks at a chosen
    call index, matching the "verifica periodica della lease" requirement in real_handlers."""

    def __init__(self, lease_lost_at: int | None = None) -> None:
        self.lease_lost_at = lease_lost_at
        self.heartbeat_calls = 0
        self.transitions: list[tuple[str, dict | None]] = []

    def heartbeat(self, job_id: str, lease_id: str) -> dict:
        self.heartbeat_calls += 1
        if self.lease_lost_at is not None and self.heartbeat_calls >= self.lease_lost_at:
            return {"lease_valid": False, "error_code": "lease_lost"}
        return {"lease_valid": True}

    def transition(self, job_id: str, lease_id: str, status: str, result: dict | None = None) -> dict:
        self.transitions.append((status, result))
        return {"status": "failed" if status == "fail" else status}


class ScriptedAdapter:
    """Configurable adapter double that lets tests exercise every branch of
    real_handlers.py (identity mismatch, investor-not-verified, IPC failure, history records)
    without a real MetaTrader5 terminal."""

    def __init__(self, terminal, login, server, *, script: dict | None = None) -> None:
        self.terminal, self.login, self.server = terminal, login, server
        self._script = script or {}
        self._idx = 0

    @contextmanager
    def session(self, password: str):
        error = self._script.get("session_error")
        if error is not None:
            raise error
        yield self

    def account_info(self) -> Any:
        return SimpleNamespace(trade_allowed=self._script.get("trade_allowed", False))

    def terminal_info(self) -> Any:
        return SimpleNamespace(connected=self._script.get("connected", True))

    def verify_identity(self) -> dict:
        return {"login": str(self.login), "server": self.server}

    @staticmethod
    def _in_window(record: dict, start, end) -> bool:
        # Real MT5 filters history_orders_get/history_deals_get by [start, end); HistorySync
        # chunks the whole requested window into several calls, so a faithful fake must only
        # return a fixture record for the one chunk that actually contains its timestamp,
        # exactly like a real terminal would.
        moment = datetime.fromtimestamp(record.get("time", 0), timezone.utc)
        return start <= moment < end

    def history_orders(self, start, end):
        return tuple(r for r in self._script.get("history_orders", ()) if self._in_window(r, start, end))

    def history_deals(self, start, end):
        return tuple(r for r in self._script.get("history_deals", ()) if self._in_window(r, start, end))

    def snapshot(self, lookback_hours: int = 72) -> dict:
        snapshots = self._script.get("snapshots", [{"positions": {}, "orders": {}, "deals": {}}])
        value = snapshots[min(self._idx, len(snapshots) - 1)]
        self._idx += 1
        return value


class FakeProcessManager:
    def __init__(self, state_path) -> None:
        self.state_path = state_path

    def adopt(self, executable):
        raise RuntimeError("no real process to adopt in tests")

    def stop(self) -> bool:
        return True

    def cleanup_path(self, executable) -> bool:
        return True


@pytest.fixture()
def env(tmp_path):
    instances_root = tmp_path / "instances"
    secrets_root = tmp_path / "secrets"
    source_terminal = tmp_path / "golden" / "terminal64.exe"
    source_terminal.parent.mkdir(parents=True)
    source_terminal.write_bytes(b"stub")
    WindowsSecretStore(secrets_root).write(AGENT_SCOPE_ID, PROVISIONING_KEY_SECRET_NAME, ENCRYPTION_KEY)
    return SimpleNamespace(instances_root=instances_root, secrets_root=secrets_root, source_terminal=source_terminal)


def _handlers(
    env,
    api,
    *,
    script: dict | None = None,
    endpoint_resolver=None,
    broker_identity_resolver=None,
    broker_wizard=None,
):
    def adapter_factory(terminal, login, server):
        return ScriptedAdapter(terminal, login, server, script=script or {})

    return build_real_handlers(
        api,
        instances_root=env.instances_root,
        secrets_root=env.secrets_root,
        source_terminal=env.source_terminal,
        adapter_factory=adapter_factory,
        process_factory=FakeProcessManager,
        endpoint_resolver=endpoint_resolver,
        broker_identity_resolver=broker_identity_resolver,
        broker_wizard=broker_wizard,
    )


def _provision_payload(
    login=12345,
    server="Demo-Server",
    password="investor-pw",
    broker_label="Demo Broker",
) -> dict:
    return {
        "credential_envelope": _envelope({"investor_password": password}),
        "expected_login": login,
        "expected_server": server,
        "broker_label": broker_label,
    }


# ---------------------------------------------------------------------------
# Provision: happy path
# ---------------------------------------------------------------------------

def test_provision_full_success_persists_secrets_and_progress(env):
    cid = str(uuid4())
    api = FakeApi()
    handlers = _handlers(env, api, script={"trade_allowed": False, "connected": True})
    job = _job("provision", cid, payload=_provision_payload())

    result = handlers["provision"](job)

    assert result["live_sync_started"] is True
    assert result["imported_deals"] == 0
    store = WindowsSecretStore(env.secrets_root)
    assert store.read(cid, "mt5_login") == "12345"
    assert store.read(cid, "mt5_server") == "Demo-Server"
    assert store.read(cid, "mt5_investor_password") == "investor-pw"
    root = InstanceLayout(env.instances_root, cid).path
    progress = read_json(root / "state" / "job_progress.json")
    assert progress["status"] == "connected"
    # the plaintext ciphertext/password must never appear in any local state file
    dump = json.dumps(read_json(root / "state" / "job_progress.json"))
    assert "investor-pw" not in dump


def test_provision_resolves_and_persists_verified_connection_endpoint(env):
    cid = str(uuid4())
    observed_labels: list[str] = []

    def resolve(label: str) -> VerifiedBrokerEndpoint:
        observed_labels.append(label)
        return VerifiedBrokerEndpoint(
            broker_label="Demo Broker",
            host="203.0.113.10",
            port=443,
            protocol="TCP/TLS",
            observed_at_unix_ms=1,
            discovery_method="MT5_LOGIN_DIALOG_IP",
            verification_pid=123,
            verification_session_id="12345678-1234-4234-8234-123456789abc",
            confidence="MEDIUM",
            artifact_relative_path="events.jsonl",
            artifact_sha256="1" * 64,
        )

    handlers = _handlers(env, FakeApi(), endpoint_resolver=resolve)
    result = handlers["provision"](
        _job("provision", cid, payload=_provision_payload())
    )

    store = WindowsSecretStore(env.secrets_root)
    assert observed_labels == ["Demo Broker"]
    assert store.read(cid, "mt5_server") == "Demo-Server"
    assert store.read(cid, "mt5_broker_label") == "Demo Broker"
    assert store.read(cid, "mt5_endpoint") == "203.0.113.10:443"
    assert result["verified_server_name"] == "Demo-Server"
    assert result["verified_broker_label"] == "Demo Broker"
    assert result["verification_method"] == "managed_investor_login"
    assert result["endpoint_protocol"] == "TCP/TLS"
    assert result["endpoint_artifact_sha256"] == "1" * 64
    assert (
        result["endpoint_verification_session_id"]
        == "12345678-1234-4234-8234-123456789abc"
    )


def test_provision_resolves_missing_broker_once_before_verified_endpoint_lookup(env):
    cid = str(uuid4())
    identity_calls: list[str] = []
    endpoint_calls: list[str] = []

    def resolve_identity(server: str) -> BrokerIdentitySuggestion:
        identity_calls.append(server)
        return BrokerIdentitySuggestion(
            broker_label="Demo Broker",
            search_text="Demo Broker",
            confidence="HIGH",
            source_urls=("https://broker.example/servers",),
            generated_at_unix_ms=1_000,
        )

    def resolve_endpoint(label: str) -> VerifiedBrokerEndpoint:
        endpoint_calls.append(label)
        return VerifiedBrokerEndpoint(
            broker_label="Demo Broker",
            host="203.0.113.10",
            port=443,
            protocol="TCP/TLS",
            observed_at_unix_ms=1,
            discovery_method="MT5_LOGIN_DIALOG_IP",
            verification_pid=123,
            verification_session_id="12345678-1234-4234-8234-123456789abc",
            confidence="MEDIUM",
            artifact_relative_path="events.jsonl",
            artifact_sha256="1" * 64,
        )

    handlers = _handlers(
        env,
        FakeApi(),
        endpoint_resolver=resolve_endpoint,
        broker_identity_resolver=resolve_identity,
    )
    payload = _provision_payload(broker_label=None)

    result = handlers["provision"](_job("provision", cid, payload=payload))

    assert identity_calls == ["Demo-Server"]
    assert endpoint_calls == ["Demo Broker"]
    assert result["verified_broker_label"] == "Demo Broker"
    assert WindowsSecretStore(env.secrets_root).read(cid, "mt5_broker_label") == "Demo Broker"


def test_provision_missing_broker_fails_closed_without_identity_resolver(env):
    cid = str(uuid4())
    handlers = _handlers(env, FakeApi(), endpoint_resolver=lambda _label: None)

    with pytest.raises(Exception) as exc_info:
        handlers["provision"](
            _job(
                "provision",
                cid,
                payload=_provision_payload(broker_label=None),
            )
        )

    assert exc_info.value.error_code == "broker_identity_unavailable"
    assert not (env.secrets_root / cid).exists()


def test_provision_fails_before_secret_persistence_when_endpoint_is_unavailable(env):
    cid = str(uuid4())

    def reject(_label: str):
        raise BrokerEndpointResolutionError("fixture unavailable")

    handlers = _handlers(env, FakeApi(), endpoint_resolver=reject)
    with pytest.raises(Exception) as exc_info:
        handlers["provision"](_job("provision", cid, payload=_provision_payload()))

    assert exc_info.value.error_code == "broker_endpoint_unavailable"
    assert not (env.secrets_root / cid).exists()


def test_provision_censuses_unknown_server_before_login_and_promotes_after_success(env):
    cid = str(uuid4())
    wizard_calls: list[tuple[str, str, str]] = []

    def reject_endpoint(_label: str):
        raise BrokerEndpointResolutionError("fixture unavailable")

    def resolve_identity(server: str) -> BrokerIdentitySuggestion:
        return BrokerIdentitySuggestion(
            broker_label="Goat Funded Trader",
            search_text="Goat Funded Trader",
            confidence="HIGH",
            source_urls=("https://broker.example/servers",),
            generated_at_unix_ms=1_000,
        )

    def run_wizard(
        root,
        search_text,
        suggested_broker_label,
        expected_server,
        cancel_check,
    ):
        if cancel_check is not None:
            cancel_check()
        wizard_calls.append((search_text, suggested_broker_label, expected_server))
        assert not (env.secrets_root / cid).exists()
        artifact = root / "state" / "broker-wizard-result.json"
        artifact.write_text('{"status":"SUCCESS"}', encoding="utf-8")
        return BrokerWizardEvidence(
            run_id="12345678-1234-4234-8234-123456789abc",
            expected_server_name=expected_server,
            selected_broker_label="Goat Funded Trader",
            censused_server_names=(expected_server,),
            terminal_pid=4321,
            completed_at_unix_ms=1_785_190_000_000,
            artifact_path=artifact,
            artifact_sha256=hashlib.sha256(artifact.read_bytes()).hexdigest(),
        )

    handlers = _handlers(
        env,
        FakeApi(),
        endpoint_resolver=reject_endpoint,
        broker_identity_resolver=resolve_identity,
        broker_wizard=run_wizard,
    )
    result = handlers["provision"](
        _job(
            "provision",
            cid,
            payload=_provision_payload(
                server="GoatFunded-Server3",
                broker_label=None,
            ),
        )
    )

    store = WindowsSecretStore(env.secrets_root)
    assert wizard_calls == [
        ("Goat Funded Trader", "Goat Funded Trader", "GoatFunded-Server3")
    ]
    assert store.read(cid, "mt5_endpoint") == "GoatFunded-Server3"
    assert store.read(cid, "mt5_broker_label") == "Goat Funded Trader"
    assert result["verified_server_name"] == "GoatFunded-Server3"
    assert result["verified_broker_label"] == "Goat Funded Trader"
    assert result["verification_method"] == "managed_investor_login"
    assert result["endpoint_protocol"] == "TCP/TLS"
    assert len(result["endpoint_artifact_sha256"]) == 64
    assert (
        result["endpoint_verification_session_id"]
        == "12345678-1234-4234-8234-123456789abc"
    )
    assert "_verification_pid" not in result
    verification_artifacts = list(
        (env.instances_root / cid / "state").glob("endpoint-verification-*.json")
    )
    assert len(verification_artifacts) == 1
    assert "investor-pw" not in verification_artifacts[0].read_text(encoding="utf-8")


def test_provision_wizard_failure_keeps_credential_envelope_unopened(env):
    cid = str(uuid4())

    def reject_endpoint(_label: str):
        raise BrokerEndpointResolutionError("fixture unavailable")

    def fail_wizard(*_args):
        assert not (env.secrets_root / cid).exists()
        raise BrokerWizardError("sanitized fixture failure")

    handlers = _handlers(
        env,
        FakeApi(),
        endpoint_resolver=reject_endpoint,
        broker_wizard=fail_wizard,
    )

    with pytest.raises(Exception) as exc_info:
        handlers["provision"](
            _job("provision", cid, payload=_provision_payload())
        )

    assert exc_info.value.error_code == "broker_discovery_failed"
    assert not (env.secrets_root / cid).exists()


def test_provision_is_idempotent_on_retry(env):
    cid = str(uuid4())
    api = FakeApi()
    handlers = _handlers(env, api)
    job = _job("provision", cid, payload=_provision_payload())
    handlers["provision"](job)
    result = handlers["provision"](job)  # simulates a retried/duplicate claim of the same job
    assert result["live_sync_started"] is True


# ---------------------------------------------------------------------------
# Provision: error taxonomy (Fase 6)
# ---------------------------------------------------------------------------

def test_provision_missing_envelope_is_credential_envelope_invalid(env):
    cid = str(uuid4())
    handlers = _handlers(env, FakeApi())
    job = _job(
        "provision",
        cid,
        payload={
            "expected_login": 1,
            "expected_server": "srv",
            "broker_label": "Demo Broker",
        },
    )
    with pytest.raises(Exception) as exc_info:
        handlers["provision"](job)
    assert exc_info.value.error_code == "credential_envelope_invalid"


def test_provision_wrong_key_is_credential_decryption_failed(env):
    cid = str(uuid4())
    bad_key = base64.b64encode(b"1" * 32).decode("ascii")
    handlers = _handlers(env, FakeApi())
    job = _job(
        "provision",
        cid,
        payload={
            "credential_envelope": _envelope({"investor_password": "x"}, key_b64=bad_key),
            "expected_login": 1,
            "expected_server": "srv",
            "broker_label": "Demo Broker",
        },
    )
    with pytest.raises(Exception) as exc_info:
        handlers["provision"](job)
    assert exc_info.value.error_code == "credential_decryption_failed"


def test_provision_missing_provisioning_key_is_secret_store_failed(env):
    cid = str(uuid4())
    WindowsSecretStore(env.secrets_root)._path(AGENT_SCOPE_ID, PROVISIONING_KEY_SECRET_NAME).unlink()
    handlers = _handlers(env, FakeApi())
    job = _job("provision", cid, payload=_provision_payload())
    with pytest.raises(Exception) as exc_info:
        handlers["provision"](job)
    assert exc_info.value.error_code == "secret_store_failed"


def test_provision_identity_mismatch_maps_to_account_identity_mismatch(env):
    cid = str(uuid4())
    handlers = _handlers(
        env, FakeApi(), script={"session_error": IdentityMismatch("connected account does not match")}
    )
    job = _job("provision", cid, payload=_provision_payload())
    with pytest.raises(Exception) as exc_info:
        handlers["provision"](job)
    assert exc_info.value.error_code == "account_identity_mismatch"


def test_provision_ipc_error_maps_to_mt5_initialize_failed(env):
    cid = str(uuid4())
    handlers = _handlers(env, FakeApi(), script={"session_error": Mt5IpcError("IPC timeout")})
    job = _job("provision", cid, payload=_provision_payload())
    with pytest.raises(Exception) as exc_info:
        handlers["provision"](job)
    assert exc_info.value.error_code == "mt5_initialize_failed"


def test_provision_authorization_error_maps_to_mt5_authorization_failed(env):
    cid = str(uuid4())
    handlers = _handlers(env, FakeApi(), script={"session_error": Mt5Error("MT5 authorization failed")})
    job = _job("provision", cid, payload=_provision_payload())
    with pytest.raises(Exception) as exc_info:
        handlers["provision"](job)
    assert exc_info.value.error_code == "mt5_authorization_failed"


def test_provision_trade_allowed_account_is_investor_access_not_verified(env):
    cid = str(uuid4())
    handlers = _handlers(env, FakeApi(), script={"trade_allowed": True})
    job = _job("provision", cid, payload=_provision_payload())
    with pytest.raises(Exception) as exc_info:
        handlers["provision"](job)
    assert exc_info.value.error_code == "investor_access_not_verified"


def test_provision_disconnected_terminal_is_mt5_initialize_failed(env):
    cid = str(uuid4())
    handlers = _handlers(env, FakeApi(), script={"connected": False})
    job = _job("provision", cid, payload=_provision_payload())
    with pytest.raises(Exception) as exc_info:
        handlers["provision"](job)
    assert exc_info.value.error_code == "mt5_initialize_failed"


def test_provision_missing_terminal_template_is_terminal_start_failed(env):
    cid = str(uuid4())
    env.source_terminal.unlink()
    handlers = _handlers(env, FakeApi())
    job = _job("provision", cid, payload=_provision_payload())
    with pytest.raises(Exception) as exc_info:
        handlers["provision"](job)
    assert exc_info.value.error_code == "terminal_start_failed"


@pytest.mark.parametrize("bad_payload", [
    {"expected_login": 0, "expected_server": "srv", "broker_label": "Demo Broker"},
    {
        "expected_login": "not-a-number",
        "expected_server": "srv",
        "broker_label": "Demo Broker",
    },
    {"expected_login": 1, "expected_server": "", "broker_label": "Demo Broker"},
    {
        "expected_login": 1,
        "expected_server": "bad\nserver",
        "broker_label": "Demo Broker",
    },
    {"expected_login": 1, "expected_server": "srv", "broker_label": ""},
])
def test_provision_invalid_identity_is_credential_envelope_invalid(env, bad_payload):
    cid = str(uuid4())
    handlers = _handlers(env, FakeApi())
    payload = {"credential_envelope": _envelope({"investor_password": "x"}), **bad_payload}
    job = _job("provision", cid, payload=payload)
    with pytest.raises(Exception) as exc_info:
        handlers["provision"](job)
    assert exc_info.value.error_code == "credential_envelope_invalid"


def test_provision_invalid_history_mode_is_credential_envelope_invalid(env):
    cid = str(uuid4())
    handlers = _handlers(env, FakeApi())
    job = _job("provision", cid, payload=_provision_payload(), history_mode="bogus")
    with pytest.raises(Exception) as exc_info:
        handlers["provision"](job)
    assert exc_info.value.error_code == "credential_envelope_invalid"


# ---------------------------------------------------------------------------
# Lease safety (Fase 2/5): never completes and never sends `fail` after lease loss
# ---------------------------------------------------------------------------

def test_lease_lost_before_login_aborts_without_fail_transition(env):
    cid = str(uuid4())

    class QueueApi(FakeApi):
        def __init__(self):
            super().__init__(lease_lost_at=2)  # 1st heartbeat (top of provision) ok, 2nd (pre-auth) fails
            self._job = _job("provision", cid, payload=_provision_payload())
            self._claimed = False

        def claim(self):
            if self._claimed:
                return {}
            self._claimed = True
            return self._job

    queue_api = QueueApi()
    handlers = _handlers(env, queue_api)
    runner = JobRunner(env.instances_root.parent / "state.json", queue_api, handlers)
    claimed = runner.run_once()
    assert claimed is False
    assert not any(status == "fail" for status, _ in queue_api.transitions)
    assert not any(status == "complete" for status, _ in queue_api.transitions)


def test_lease_lost_during_history_sync_raises_lease_lost(env):
    cid = str(uuid4())
    api = FakeApi(lease_lost_at=3)  # ok, ok, fails right before history import
    handlers = _handlers(env, api)
    job = _job("provision", cid, payload=_provision_payload())
    with pytest.raises(LeaseLost):
        handlers["provision"](job)


def test_lease_lost_before_starting_live_sync_raises_lease_lost(env):
    cid = str(uuid4())
    api = FakeApi(lease_lost_at=4)
    handlers = _handlers(env, api)
    job = _job("provision", cid, payload=_provision_payload())
    with pytest.raises(LeaseLost):
        handlers["provision"](job)


def test_live_sync_start_failure_is_live_sync_failed(env, monkeypatch):
    cid = str(uuid4())
    handlers = _handlers(env, FakeApi())

    def boom(*args, **kwargs):
        raise RuntimeError("snapshot failed")

    monkeypatch.setattr(real_handlers, "_run_live_sync_once", boom)
    job = _job("provision", cid, payload=_provision_payload())
    with pytest.raises(Exception) as exc_info:
        handlers["provision"](job)
    assert exc_info.value.error_code == "live_sync_failed"


# ---------------------------------------------------------------------------
# Historical sync
# ---------------------------------------------------------------------------

def test_historical_sync_requires_prior_provision(env):
    cid = str(uuid4())
    handlers = _handlers(env, FakeApi())
    job = _job("historical_sync", cid, history_mode="all_available")
    with pytest.raises(Exception) as exc_info:
        handlers["historical_sync"](job)
    assert exc_info.value.error_code == "instance_provision_failed"


def test_historical_sync_reuses_dpapi_credentials_and_imports_records(env):
    cid = str(uuid4())
    api = FakeApi()
    moment = int(datetime(2026, 3, 15, tzinfo=timezone.utc).timestamp())
    order = {"ticket": 1, "symbol": "EURUSD", "volume_current": 0.1, "type": 0, "price_open": 1.1, "sl": 0, "tp": 0, "time": moment}
    deal = {"ticket": 2, "position_id": 1, "symbol": "EURUSD", "volume": 0.1, "price": 1.2, "profit": 5, "commission": -1, "swap": 0, "time": moment}
    handlers = _handlers(env, api, script={"history_orders": (order,), "history_deals": (deal,)})
    handlers["provision"](_job("provision", cid, payload=_provision_payload()))

    result = handlers["historical_sync"](
        _job("historical_sync", cid, history_mode="from_date", from_date="2026-01-01T00:00:00Z")
    )
    assert result["imported_orders"] == 1
    assert result["imported_deals"] == 1
    root = InstanceLayout(env.instances_root, cid).path
    lines = (root / "data" / "history.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2


def test_historical_sync_dedups_across_repeated_runs(env):
    cid = str(uuid4())
    api = FakeApi()
    moment = int(datetime(2026, 7, 10, tzinfo=timezone.utc).timestamp())
    deal = {"ticket": 9, "position_id": 1, "symbol": "EURUSD", "volume": 0.1, "price": 1.2, "profit": 5, "commission": -1, "swap": 0, "time": moment}
    handlers = _handlers(env, api, script={"history_deals": (deal,)})
    handlers["provision"](_job("provision", cid, payload=_provision_payload()))
    root = InstanceLayout(env.instances_root, cid).path
    # Force a re-scan of the same window by resetting the checkpoint, simulating a resumed job
    # that re-reads a chunk it already (partially) delivered -- the sink-level dedup must still
    # prevent a duplicate line, independent of the checkpoint.
    (root / "state" / "history.json").unlink(missing_ok=True)
    handlers["historical_sync"](
        _job("historical_sync", cid, history_mode="from_date", from_date="2026-07-01T00:00:00Z")
    )
    lines = (root / "data" / "history.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1


def test_historical_sync_invalid_history_mode_rejected(env):
    cid = str(uuid4())
    handlers = _handlers(env, FakeApi())
    handlers["provision"](_job("provision", cid, payload=_provision_payload()))
    job = _job("historical_sync", cid, history_mode="from_date", from_date="not-a-date")
    with pytest.raises(Exception) as exc_info:
        handlers["historical_sync"](job)
    assert exc_info.value.error_code == "credential_envelope_invalid"


# ---------------------------------------------------------------------------
# Deprovision: idempotent, only touches its own instance
# ---------------------------------------------------------------------------

def test_deprovision_is_idempotent(env):
    cid = str(uuid4())
    handlers = _handlers(env, FakeApi())
    handlers["provision"](_job("provision", cid, payload=_provision_payload()))
    root = InstanceLayout(env.instances_root, cid).path
    assert root.exists()

    result_1 = handlers["deprovision"](_job("deprovision", cid))
    result_2 = handlers["deprovision"](_job("deprovision", cid))
    assert result_1 == {"deprovisioned": True}
    assert result_2 == {"deprovisioned": True}
    store = WindowsSecretStore(env.secrets_root)
    with pytest.raises(Exception):
        store.read(cid, "mt5_investor_password")


def test_deprovision_never_provisioned_connection_is_safe(env):
    cid = str(uuid4())
    handlers = _handlers(env, FakeApi())
    result = handlers["deprovision"](_job("deprovision", cid))
    assert result == {"deprovisioned": True}


def test_deprovision_only_touches_its_own_connection(env):
    cid_a, cid_b = str(uuid4()), str(uuid4())
    handlers = _handlers(env, FakeApi())
    handlers["provision"](_job("provision", cid_a, payload=_provision_payload()))
    handlers["provision"](_job("provision", cid_b, payload=_provision_payload(login=999, server="Other")))
    handlers["deprovision"](_job("deprovision", cid_a))
    assert not (InstanceLayout(env.instances_root, cid_a).path / "terminal").exists()
    root_b = InstanceLayout(env.instances_root, cid_b).path
    assert (root_b / "terminal" / "terminal64.exe").exists()
    assert WindowsSecretStore(env.secrets_root).read(cid_b, "mt5_login") == "999"


# ---------------------------------------------------------------------------
# Recovery: stale/orphaned processes at daemon startup
# ---------------------------------------------------------------------------

def test_sweep_stale_instances_terminates_orphans(env, monkeypatch):
    cid = str(uuid4())
    handlers = _handlers(env, FakeApi())
    handlers["provision"](_job("provision", cid, payload=_provision_payload()))
    root = InstanceLayout(env.instances_root, cid).path

    cleaned = []
    monkeypatch.setattr(real_handlers.ProcessManager, "find", staticmethod(lambda executable: [4242]))

    class RecordingProcessManager(FakeProcessManager):
        def cleanup_path(self, executable):
            cleaned.append((self.state_path, executable))
            return True

    swept = sweep_stale_instances(env.instances_root, process_factory=RecordingProcessManager)
    assert swept == [cid]
    assert cleaned and cleaned[0][1] == root / "terminal" / "terminal64.exe"


def test_sweep_stale_instances_empty_root_is_safe(tmp_path):
    assert sweep_stale_instances(tmp_path / "does-not-exist") == []


# ---------------------------------------------------------------------------
# Secrets never leak into logs/state
# ---------------------------------------------------------------------------

def test_no_secret_markers_in_any_local_state_file(env):
    cid = str(uuid4())
    handlers = _handlers(env, FakeApi())
    handlers["provision"](_job("provision", cid, payload=_provision_payload(password="super-secret-pw")))
    root = InstanceLayout(env.instances_root, cid).path
    for path in (root / "state").rglob("*.json"):
        assert "super-secret-pw" not in path.read_text(encoding="utf-8")
