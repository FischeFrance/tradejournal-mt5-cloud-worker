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
from windows_agent.broker_endpoint_registry import (
    EndpointInvalidation,
    EndpointPromotion,
    ObservedProcessEndpoint,
)
from windows_agent.broker_identity import BrokerIdentitySuggestion
from windows_agent.broker_wizard import BrokerWizardError, BrokerWizardEvidence
from windows_agent.mtapi_search import (
    MtApiEndpointCandidate,
    MtApiSearchResult,
    MtApiSearchUnavailable,
)
from windows_agent.job_runner import JobRunner, LeaseLost
from windows_agent.provisioning.instance_layout import InstanceLayout
from windows_agent.provisioning.mt5_instance import InstanceProvisioner
from windows_agent.provisioning.secret_store import WindowsSecretStore
from windows_agent.real_handlers import (
    build_real_handlers,
    reconcile_startup_instances,
)
from windows_agent.state_store import atomic_json, read_json
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

    def progress(
        self,
        job_id: str,
        lease_id: str,
        event_code: str,
        event_status: str,
        detail_code: str | None = None,
    ) -> dict:
        if not hasattr(self, "progress_events"):
            self.progress_events = []
        self.progress_events.append((event_code, event_status, detail_code))
        return {"api_version": "1", "event_recorded": True}


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
    pid = 123

    def __init__(self, state_path) -> None:
        self.state_path = state_path

    def adopt(self, executable):
        raise RuntimeError("no real process to adopt in tests")

    def stop(self) -> bool:
        return True

    def cleanup_path(self, executable) -> bool:
        return True


@pytest.fixture()
def env(tmp_path, monkeypatch):
    # These are offline handler tests, not a DPAPI integration test.  A real Windows session
    # reached through SSH may have no loadable user master key, so keep the fixture deterministic
    # exactly like the daemon E2E tests do.
    monkeypatch.setattr(
        WindowsSecretStore,
        "_crypt_protect",
        staticmethod(lambda value: value),
    )
    monkeypatch.setattr(
        WindowsSecretStore,
        "_crypt_unprotect",
        staticmethod(lambda value: value),
    )
    monkeypatch.setattr(
        WindowsSecretStore,
        "restrict_acl",
        staticmethod(lambda path: None),
    )
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
    native_runtime=False,
    script: dict | None = None,
    process_factory=FakeProcessManager,
    endpoint_resolver=None,
    endpoint_observer=None,
    endpoint_publisher=None,
    endpoint_invalidator=None,
    broker_identity_resolver=None,
    broker_wizard=None,
    mtapi_search=None,
    instance_pool=None,
    terminal_sha256=None,
):
    def adapter_factory(terminal, login, server):
        return ScriptedAdapter(terminal, login, server, script=script or {})

    return build_real_handlers(
        api,
        instances_root=env.instances_root,
        secrets_root=env.secrets_root,
        source_terminal=env.source_terminal,
        adapter_factory=None if native_runtime else adapter_factory,
        process_factory=process_factory,
        endpoint_resolver=endpoint_resolver,
        endpoint_observer=endpoint_observer,
        endpoint_publisher=endpoint_publisher,
        endpoint_invalidator=endpoint_invalidator,
        broker_identity_resolver=broker_identity_resolver,
        broker_wizard=broker_wizard,
        mtapi_search=mtapi_search,
        instance_pool=instance_pool,
        terminal_sha256=terminal_sha256,
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


def _verified_endpoint() -> VerifiedBrokerEndpoint:
    return VerifiedBrokerEndpoint(
        broker_label="Demo Broker",
        server_name="Demo-Server",
        host="203.0.113.10",
        port=443,
        protocol="TCP/TLS",
        observed_at_unix_ms=1,
        discovery_method="MT5_MANAGED_INVESTOR_LOGIN",
        verification_pid=123,
        process_creation_time_unix_ms=999,
        verification_session_id=(
            "12345678-1234-4234-8234-123456789abc"
        ),
        confidence="HIGH",
        artifact_relative_path="events.jsonl",
        artifact_sha256="1" * 64,
    )


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
    assert api.progress_events == [
        ("broker_identity", "completed", "request_identity"),
        ("endpoint_resolution", "started", None),
        ("endpoint_resolution", "skipped", "resolver_not_configured"),
        ("instance_preparation", "started", None),
        ("instance_preparation", "completed", None),
        ("terminal_start", "started", None),
        ("terminal_start", "completed", None),
        ("investor_verification", "started", None),
        ("investor_verification", "completed", None),
        ("history_sync", "started", "new_only"),
        ("history_sync", "completed", "new_only"),
        ("sync_activation", "started", None),
        ("sync_activation", "completed", None),
    ]


def test_provision_claims_prebuilt_instance_before_direct_copy(
    env,
    monkeypatch,
):
    cid = str(uuid4())
    original_provision = InstanceProvisioner.provision
    pooled_root = original_provision(
        InstanceProvisioner(env.instances_root, env.secrets_root),
        cid,
        env.source_terminal,
    )

    class FakePool:
        def __init__(self):
            self.calls = []

        def claim(self, connection_id):
            self.calls.append(connection_id)
            return pooled_root

    pool = FakePool()

    def unexpected_direct_copy(*args, **kwargs):
        raise AssertionError("direct-copy fallback must not run")

    monkeypatch.setattr(
        InstanceProvisioner,
        "provision",
        unexpected_direct_copy,
    )
    result = _handlers(
        env,
        FakeApi(),
        instance_pool=pool,
    )["provision"](
        _job("provision", cid, payload=_provision_payload())
    )

    assert result["live_sync_started"] is True
    assert pool.calls == [cid]


def test_provision_uses_direct_copy_only_when_pool_is_empty(env):
    cid = str(uuid4())

    class EmptyPool:
        def __init__(self):
            self.calls = []

        def claim(self, connection_id):
            self.calls.append(connection_id)
            return None

    pool = EmptyPool()
    result = _handlers(
        env,
        FakeApi(),
        instance_pool=pool,
    )["provision"](
        _job("provision", cid, payload=_provision_payload())
    )

    assert result["live_sync_started"] is True
    assert pool.calls == [cid]
    assert (env.instances_root / cid / "terminal" / "terminal64.exe").is_file()


def test_provision_resolves_and_persists_verified_connection_endpoint(env):
    cid = str(uuid4())
    observed_labels: list[str] = []

    def resolve(label: str, server: str) -> VerifiedBrokerEndpoint:
        observed_labels.append(label)
        assert server == "Demo-Server"
        return VerifiedBrokerEndpoint(
            broker_label="Demo Broker",
            server_name="Demo-Server",
            host="203.0.113.10",
            port=443,
            protocol="TCP/TLS",
            observed_at_unix_ms=1,
            discovery_method="MT5_LOGIN_DIALOG_IP",
            verification_pid=123,
            process_creation_time_unix_ms=999,
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
    endpoint_calls: list[str | None] = []

    def resolve_identity(server: str) -> BrokerIdentitySuggestion:
        identity_calls.append(server)
        return BrokerIdentitySuggestion(
            broker_label="Demo Broker",
            search_text="Demo Broker",
            confidence="HIGH",
            source_urls=("https://broker.example/servers",),
            generated_at_unix_ms=1_000,
        )

    def resolve_endpoint(
        label: str | None,
        server: str,
    ) -> VerifiedBrokerEndpoint:
        endpoint_calls.append(label)
        assert server == "Demo-Server"
        if label is None:
            raise BrokerEndpointResolutionError(
                "fixture has no server-only match"
            )
        return VerifiedBrokerEndpoint(
            broker_label="Demo Broker",
            server_name="Demo-Server",
            host="203.0.113.10",
            port=443,
            protocol="TCP/TLS",
            observed_at_unix_ms=1,
            discovery_method="MT5_LOGIN_DIALOG_IP",
            verification_pid=123,
            process_creation_time_unix_ms=999,
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
    assert endpoint_calls == [None, "Demo Broker"]
    assert result["verified_broker_label"] == "Demo Broker"
    assert WindowsSecretStore(env.secrets_root).read(cid, "mt5_broker_label") == "Demo Broker"


def test_provision_exact_server_registry_hit_skips_ai_identity_lookup(env):
    cid = str(uuid4())
    endpoint_calls: list[tuple[str | None, str]] = []

    def resolve_endpoint(
        label: str | None,
        server: str,
    ) -> VerifiedBrokerEndpoint:
        endpoint_calls.append((label, server))
        return VerifiedBrokerEndpoint(
            broker_label="Demo Broker",
            server_name=server,
            host="203.0.113.10",
            port=443,
            protocol="TCP/TLS",
            observed_at_unix_ms=1,
            discovery_method="MT5_MANAGED_INVESTOR_LOGIN",
            verification_pid=123,
            process_creation_time_unix_ms=999,
            verification_session_id=(
                "12345678-1234-4234-8234-123456789abc"
            ),
            confidence="HIGH",
            artifact_relative_path="events.jsonl",
            artifact_sha256="1" * 64,
        )

    def unexpected_identity(_server: str):
        raise AssertionError("AI identity resolver must not be called")

    handlers = _handlers(
        env,
        FakeApi(),
        endpoint_resolver=resolve_endpoint,
        broker_identity_resolver=unexpected_identity,
    )
    result = handlers["provision"](
        _job(
            "provision",
            cid,
            payload=_provision_payload(broker_label=None),
        )
    )

    assert endpoint_calls == [(None, "Demo-Server")]
    assert result["verified_broker_label"] == "Demo Broker"


def test_provision_missing_broker_fails_closed_without_identity_resolver(env):
    cid = str(uuid4())

    def reject_endpoint(_label, _server):
        raise BrokerEndpointResolutionError("fixture unavailable")

    handlers = _handlers(
        env,
        FakeApi(),
        endpoint_resolver=reject_endpoint,
    )

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

    def reject(_label: str, _server: str):
        raise BrokerEndpointResolutionError("fixture unavailable")

    handlers = _handlers(env, FakeApi(), endpoint_resolver=reject)
    with pytest.raises(Exception) as exc_info:
        handlers["provision"](_job("provision", cid, payload=_provision_payload()))

    assert exc_info.value.error_code == "broker_endpoint_unavailable"
    assert not (env.secrets_root / cid).exists()


def test_provision_censuses_unknown_server_before_login_and_promotes_after_success(env):
    cid = str(uuid4())
    wizard_calls: list[tuple[str, str, str]] = []
    promotions: list[EndpointPromotion] = []
    generated_example = (
        env.instances_root
        / cid
        / "terminal"
        / "MQL5"
        / "Experts"
        / "Advisors"
        / "ExpertMACD.ex5"
    )

    def reject_endpoint(_label: str, _server: str):
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
        generated_example.parent.mkdir(parents=True)
        generated_example.write_bytes(b"mt5 default example")
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

    def observe_endpoint(pid: int) -> ObservedProcessEndpoint:
        assert pid == 123
        return ObservedProcessEndpoint(
            host="203.0.113.10",
            port=443,
            pid=pid,
            process_creation_time_unix_ms=1_785_190_001_000,
            observed_at_unix_ms=1_785_190_002_000,
        )

    def publish_endpoint(
        promotion: EndpointPromotion,
    ) -> VerifiedBrokerEndpoint:
        promotions.append(promotion)
        evidence = json.loads(
            promotion.verification_artifact.read_text(encoding="utf-8")
        )
        assert evidence["schema_version"] == 3
        assert evidence["remote_host"] == "203.0.113.10"
        assert evidence["remote_port"] == 443
        assert evidence["process_creation_time_unix_ms"] == 1_785_190_001_000
        assert evidence["provenance_kind"] == "BROKER_WIZARD"
        assert promotion.provenance_artifact == artifact
        return VerifiedBrokerEndpoint(
            broker_label=promotion.broker_label,
            server_name=promotion.server_name,
            host=promotion.observation.host,
            port=promotion.observation.port,
            protocol="TCP/TLS",
            observed_at_unix_ms=promotion.observation.observed_at_unix_ms,
            discovery_method="MT5_MANAGED_INVESTOR_LOGIN",
            verification_pid=promotion.observation.pid,
            process_creation_time_unix_ms=(
                promotion.observation.process_creation_time_unix_ms
            ),
            verification_session_id=promotion.verification_session_id,
            confidence="HIGH",
            artifact_relative_path="artifacts/fixture/verification.json",
            artifact_sha256=promotion.verification_artifact_sha256,
        )

    handlers = _handlers(
        env,
        FakeApi(),
        endpoint_resolver=reject_endpoint,
        endpoint_observer=observe_endpoint,
        endpoint_publisher=publish_endpoint,
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
    assert not generated_example.exists()
    assert store.read(cid, "mt5_endpoint") == "203.0.113.10:443"
    assert store.read(cid, "mt5_broker_label") == "Goat Funded Trader"
    assert result["verified_server_name"] == "GoatFunded-Server3"
    assert result["verified_broker_label"] == "Goat Funded Trader"
    assert result["verification_method"] == "managed_investor_login"
    assert result["endpoint_protocol"] == "TCP/TLS"
    assert result["endpoint_registry_status"] == "PROMOTED"
    assert result["promoted_endpoint"] == "203.0.113.10:443"
    assert len(promotions) == 1
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
    assert (
        json.loads(verification_artifacts[0].read_text(encoding="utf-8"))[
            "verification_pid"
        ]
        == 123
    )
    assert "investor-pw" not in verification_artifacts[0].read_text(encoding="utf-8")


def test_provision_wizard_failure_keeps_credential_envelope_unopened(env):
    cid = str(uuid4())

    def reject_endpoint(_label: str, _server: str):
        raise BrokerEndpointResolutionError("fixture unavailable")

    def fail_wizard(*_args):
        assert not (env.secrets_root / cid).exists()
        raise BrokerWizardError(
            "sanitized fixture failure",
            failure_reason="timeout",
        )

    api = FakeApi()
    handlers = _handlers(
        env,
        api,
        endpoint_resolver=reject_endpoint,
        broker_wizard=fail_wizard,
    )

    with pytest.raises(Exception) as exc_info:
        handlers["provision"](
            _job("provision", cid, payload=_provision_payload())
        )

    assert exc_info.value.error_code == "broker_discovery_failed"
    assert (
        "broker_discovery",
        "failed",
        "wizard_timeout",
    ) in api.progress_events
    assert not (env.secrets_root / cid).exists()
    assert not (env.instances_root / cid).exists()


def test_provision_mtapi_exact_match_skips_wizard_and_persists_sanitized_audit(env):
    cid = str(uuid4())
    wizard_calls: list[tuple[str, str, str]] = []

    def reject_endpoint(_label: str, _server: str):
        raise BrokerEndpointResolutionError("fixture unavailable")

    def search(server: str) -> MtApiSearchResult:
        assert server == "PepperstoneUK-Live"
        return MtApiSearchResult(
            outcome="EXACT_MATCH",
            candidates=(
                MtApiEndpointCandidate(
                    company_name="Pepperstone Limited",
                    server_name="PepperstoneUK-Live",
                    host="13.134.102.187",
                    port=443,
                ),
            ),
            company_names=("Pepperstone Limited",),
            fetched_at_unix_ms=1_785_190_000_000,
        )

    def unexpected_identity(_server: str):
        raise AssertionError("AI must not run after an exact MTAPI Search match")

    def run_wizard(root, search_text, suggested_broker_label, expected_server, _guard):
        wizard_calls.append((search_text, suggested_broker_label, expected_server))
        assert not (env.secrets_root / cid).exists()
        artifact = root / "state" / "broker-wizard-result.json"
        artifact.write_text('{"status":"SUCCESS"}', encoding="utf-8")
        return BrokerWizardEvidence(
            run_id="12345678-1234-4234-8234-123456789abc",
            expected_server_name=expected_server,
            selected_broker_label="Pepperstone Limited",
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
        broker_identity_resolver=unexpected_identity,
        broker_wizard=run_wizard,
        mtapi_search=search,
    )
    handlers["provision"](
        _job(
            "provision",
            cid,
            payload=_provision_payload(
                server="PepperstoneUK-Live",
                broker_label=None,
            ),
        )
    )

    assert wizard_calls == []
    audit = (env.instances_root / cid / "state" / "mtapi-search.json").read_text(
        encoding="utf-8"
    )
    assert "13.134.102.187" in audit
    assert "investor-pw" not in audit
    assert "password" not in audit.lower()


def test_provision_mtapi_candidates_are_tried_before_wizard(env, monkeypatch):
    cid = str(uuid4())
    attempted: list[str] = []
    wizard_calls: list[str] = []

    def reject_endpoint(_label: str, _server: str):
        raise BrokerEndpointResolutionError("fixture unavailable")

    def search(_server: str) -> MtApiSearchResult:
        return MtApiSearchResult(
            outcome="EXACT_MATCH",
            candidates=(
                MtApiEndpointCandidate(
                    company_name="Fortune Prime Global Capital Pty Ltd",
                    server_name="FortunePrimeGlobal-Live",
                    host="192.81.110.54",
                    port=443,
                ),
                MtApiEndpointCandidate(
                    company_name="Fortune Prime Global Capital Pty Ltd",
                    server_name="FortunePrimeGlobal-Live",
                    host="192.229.23.143",
                    port=443,
                ),
            ),
            company_names=("Fortune Prime Global Capital Pty Ltd",),
            fetched_at_unix_ms=1_785_190_000_000,
        )

    def run_native(*args, **kwargs):
        endpoint = kwargs.get("connection_endpoint") or args[6]
        attempted.append(endpoint)
        if len(attempted) == 1:
            raise real_handlers.Mt5InitializeFailed("endpoint_connection_refused")
        return {"effective_server_name": "FortunePrimeGlobal-Live", "_verification_pid": 42}

    def unexpected_wizard(*_args):
        wizard_calls.append("called")
        raise AssertionError("wizard must not run after a successful MTAPI candidate")

    monkeypatch.setattr(real_handlers, "_start_file_bridge_and_sync", run_native)
    handlers = _handlers(
        env,
        FakeApi(),
        native_runtime=True,
        endpoint_resolver=reject_endpoint,
        broker_wizard=unexpected_wizard,
        mtapi_search=search,
    )

    result = handlers["provision"](
        _job(
            "provision",
            cid,
            payload=_provision_payload(
                server="FortunePrimeGlobal-Live",
                broker_label=None,
            ),
        )
    )

    assert attempted == ["192.81.110.54:443", "192.229.23.143:443"]
    assert wizard_calls == []
    assert result["effective_server_name"] == "FortunePrimeGlobal-Live"


def test_provision_mtapi_hostname_is_tried_before_ip_candidates(env, monkeypatch):
    cid = str(uuid4())
    attempted: list[str] = []

    def reject_endpoint(_label: str, _server: str):
        raise BrokerEndpointResolutionError("fixture unavailable")

    def search(_server: str) -> MtApiSearchResult:
        return MtApiSearchResult(
            outcome="EXACT_MATCH",
            candidates=(
                MtApiEndpointCandidate(
                    company_name="Fortune Prime Limited",
                    server_name="FortunePrime-Live2",
                    host="ga-bp14h62c0bwpfhso8fwxp.aliyunga0017.com",
                    port=443,
                ),
                MtApiEndpointCandidate(
                    company_name="Fortune Prime Limited",
                    server_name="FortunePrime-Live2",
                    host="38.76.16.208",
                    port=443,
                ),
            ),
            company_names=("Fortune Prime Limited",),
            fetched_at_unix_ms=1_785_190_000_000,
        )

    def run_native(*args, **kwargs):
        endpoint = kwargs.get("connection_endpoint") or args[6]
        attempted.append(endpoint)
        if endpoint.startswith("ga-bp"):
            return {"effective_server_name": "FortunePrime-Live2", "_verification_pid": 42}
        raise AssertionError("IP fallback must not run after hostname authentication")

    monkeypatch.setattr(real_handlers, "_start_file_bridge_and_sync", run_native)
    handlers = _handlers(
        env,
        FakeApi(),
        native_runtime=True,
        endpoint_resolver=reject_endpoint,
        mtapi_search=search,
    )

    handlers["provision"](
        _job("provision", cid, payload=_provision_payload(server="FortunePrime-Live2", broker_label=None))
    )

    assert attempted == ["ga-bp14h62c0bwpfhso8fwxp.aliyunga0017.com:443"]


def test_direct_mtapi_login_binds_promoted_endpoint_to_exact_candidate(env, monkeypatch):
    cid = str(uuid4())
    observed_bindings: list[tuple[int, str | None, int | None]] = []
    promotions: list[EndpointPromotion] = []

    def reject_endpoint(_label: str, _server: str):
        raise BrokerEndpointResolutionError("fixture unavailable")

    def search(_server: str) -> MtApiSearchResult:
        return MtApiSearchResult(
            outcome="EXACT_MATCH",
            candidates=(
                MtApiEndpointCandidate(
                    company_name="Fortune Prime Global",
                    server_name="FortunePrimeGlobal-Live",
                    host="192.81.110.54",
                    port=443,
                ),
            ),
            company_names=("Fortune Prime Global",),
            fetched_at_unix_ms=1_785_190_000_000,
        )

    def observe(pid: int, *, expected_host=None, expected_port=None):
        observed_bindings.append((pid, expected_host, expected_port))
        return ObservedProcessEndpoint(
            host="192.81.110.54",
            port=443,
            pid=pid,
            process_creation_time_unix_ms=1_785_190_001_000,
            observed_at_unix_ms=1_785_190_002_000,
        )

    def start_native(*args, **kwargs):
        observer = kwargs["endpoint_observer"]
        return {
            "effective_server_name": "FortunePrimeGlobal-Live",
            "_verification_pid": 42,
            "_verification_observation": observer(42),
        }

    def publish(promotion: EndpointPromotion) -> VerifiedBrokerEndpoint:
        promotions.append(promotion)
        document = json.loads(promotion.verification_artifact.read_text(encoding="utf-8"))
        assert document["provenance_kind"] == "MTAPI_SEARCH"
        assert promotion.provenance_artifact.name == "mtapi-search.json"
        return VerifiedBrokerEndpoint(
            broker_label=promotion.broker_label,
            server_name=promotion.server_name,
            host=promotion.observation.host,
            port=promotion.observation.port,
            protocol="TCP/TLS",
            observed_at_unix_ms=promotion.observation.observed_at_unix_ms,
            discovery_method="MT5_MANAGED_INVESTOR_LOGIN",
            verification_pid=promotion.observation.pid,
            process_creation_time_unix_ms=promotion.observation.process_creation_time_unix_ms,
            verification_session_id=promotion.verification_session_id,
            confidence="HIGH",
            artifact_relative_path="artifacts/fixture/verification.json",
            artifact_sha256=promotion.verification_artifact_sha256,
        )

    monkeypatch.setattr(real_handlers, "_start_file_bridge_and_sync", start_native)
    handlers = _handlers(
        env,
        FakeApi(),
        native_runtime=True,
        endpoint_resolver=reject_endpoint,
        endpoint_observer=observe,
        endpoint_publisher=publish,
        mtapi_search=search,
    )

    result = handlers["provision"](
        _job(
            "provision",
            cid,
            payload=_provision_payload(
                server="FortunePrimeGlobal-Live",
                broker_label=None,
            ),
        )
    )

    assert observed_bindings == [(42, "192.81.110.54", 443)]
    assert len(promotions) == 1
    assert result["endpoint_registry_status"] == "PROMOTED"


def test_provision_mtapi_unavailable_falls_back_to_existing_wizard_search(env):
    cid = str(uuid4())
    wizard_calls: list[str] = []

    def reject_endpoint(_label: str, _server: str):
        raise BrokerEndpointResolutionError("fixture unavailable")

    def unavailable(_server: str) -> MtApiSearchResult:
        raise MtApiSearchUnavailable("MTAPI Search is unavailable")

    def run_wizard(root, search_text, _suggested_broker_label, expected_server, _guard):
        wizard_calls.append(search_text)
        artifact = root / "state" / "broker-wizard-result.json"
        artifact.write_text('{"status":"SUCCESS"}', encoding="utf-8")
        return BrokerWizardEvidence(
            run_id="12345678-1234-4234-8234-123456789abc",
            expected_server_name=expected_server,
            selected_broker_label="Demo Broker",
            censused_server_names=(expected_server,),
            terminal_pid=4321,
            completed_at_unix_ms=1_785_190_000_000,
            artifact_path=artifact,
            artifact_sha256=hashlib.sha256(artifact.read_bytes()).hexdigest(),
        )

    api = FakeApi()
    handlers = _handlers(
        env,
        api,
        endpoint_resolver=reject_endpoint,
        broker_wizard=run_wizard,
        mtapi_search=unavailable,
    )
    handlers["provision"](_job("provision", cid, payload=_provision_payload()))

    assert wizard_calls == ["Demo Broker"]
    assert ("broker_discovery", "info", "mtapi_search_unavailable") in api.progress_events
    assert not (env.instances_root / cid / "state" / "mtapi-search.json").exists()


def test_failed_provision_removes_instance_and_persisted_secrets(env):
    cid = str(uuid4())
    handlers = _handlers(
        env,
        FakeApi(),
        script={"session_error": Mt5IpcError("fixture failure")},
    )

    with pytest.raises(Exception) as exc_info:
        handlers["provision"](
            _job("provision", cid, payload=_provision_payload())
        )

    assert exc_info.value.error_code == "mt5_initialize_failed"
    assert not (env.instances_root / cid).exists()
    assert not (env.secrets_root / cid).exists()


def test_retry_discards_stale_failed_instance_before_fresh_provision(env):
    cid = str(uuid4())
    stale_root = InstanceLayout(env.instances_root, cid).create()
    stale_config = stale_root / "terminal" / "Config" / "accounts.dat"
    stale_config.parent.mkdir(parents=True)
    stale_config.write_bytes(b"stale-account-cache")
    atomic_json(
        stale_root / "state" / "job_progress.json",
        {
            "connection_id": cid,
            "status": "censusing_broker",
        },
    )
    WindowsSecretStore(env.secrets_root).write(
        cid,
        "mt5_server",
        "Stale-Server",
    )

    result = _handlers(env, FakeApi())["provision"](
        _job("provision", cid, payload=_provision_payload())
    )

    assert result["live_sync_started"] is True
    assert not stale_config.exists()
    assert read_json(
        env.instances_root / cid / "state" / "job_progress.json"
    )["status"] == "connected"
    assert (
        WindowsSecretStore(env.secrets_root).read(cid, "mt5_server")
        == "Demo-Server"
    )


def test_retry_discards_partial_instance_with_only_compiler_process_path(
    env,
):
    cid = str(uuid4())
    stale_root = InstanceLayout(env.instances_root, cid).create()
    terminal = stale_root / "terminal" / "terminal64.exe"
    compiler = stale_root / "terminal" / "metaeditor64.exe"
    compiler.write_bytes(b"stale-compiler")
    atomic_json(
        stale_root / "state" / "job_progress.json",
        {
            "connection_id": cid,
            "status": "censusing_broker",
        },
    )
    cleanup_calls = []

    class RecordingProcessManager(FakeProcessManager):
        def cleanup_path(self, executable) -> bool:
            cleanup_calls.append(executable)
            return True

    result = _handlers(
        env,
        FakeApi(),
        process_factory=RecordingProcessManager,
    )["provision"](
        _job("provision", cid, payload=_provision_payload())
    )

    assert result["live_sync_started"] is True
    assert cleanup_calls == [terminal]
    assert not compiler.exists()


def test_failed_duplicate_provision_preserves_connected_instance(env):
    cid = str(uuid4())
    job = _job("provision", cid, payload=_provision_payload())
    _handlers(env, FakeApi())["provision"](job)
    root = env.instances_root / cid
    secret = env.secrets_root / cid / "mt5_investor_password.dpapi"

    with pytest.raises(Exception) as exc_info:
        _handlers(
            env,
            FakeApi(),
            script={"session_error": Mt5IpcError("fixture failure")},
        )["provision"](job)

    assert exc_info.value.error_code == "mt5_initialize_failed"
    assert root.is_dir()
    assert secret.is_file()


def test_failed_provision_blocks_retry_when_process_cleanup_is_unverified(env):
    cid = str(uuid4())

    class UnverifiedCleanupProcessManager(FakeProcessManager):
        def cleanup_path(self, executable) -> bool:
            return False

    handlers = _handlers(
        env,
        FakeApi(),
        script={"session_error": Mt5IpcError("fixture failure")},
        process_factory=UnverifiedCleanupProcessManager,
    )

    with pytest.raises(Exception) as exc_info:
        handlers["provision"](
            _job("provision", cid, payload=_provision_payload())
        )

    assert exc_info.value.error_code == "instance_cleanup_failed"
    assert (env.instances_root / cid).is_dir()


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


@pytest.mark.parametrize(
    ("failure_code", "expected_reason", "expected_error_code"),
    [
        (
            "server_identity_mismatch",
            "SERVER_IDENTITY_MISMATCH",
            "server_identity_mismatch",
        ),
        (
            "endpoint_connection_failed",
            "ENDPOINT_CONNECTION_FAILED",
            "mt5_initialize_failed",
        ),
        (
            "endpoint_connection_refused",
            "ENDPOINT_CONNECTION_REFUSED",
            "mt5_initialize_failed",
        ),
        (
            "endpoint_protocol_incompatible",
            "ENDPOINT_PROTOCOL_INCOMPATIBLE",
            "mt5_initialize_failed",
        ),
        (
            "endpoint_server_unrecognized",
            "ENDPOINT_SERVER_UNRECOGNIZED",
            "mt5_initialize_failed",
        ),
    ],
)
def test_endpoint_specific_failure_invalidates_exact_record(
    env,
    failure_code,
    expected_reason,
    expected_error_code,
):
    cid = str(uuid4())
    invalidations: list[EndpointInvalidation] = []
    endpoint = _verified_endpoint()

    handlers = _handlers(
        env,
        FakeApi(),
        script={"session_error": Mt5Error(failure_code)},
        endpoint_resolver=lambda _label, _server: endpoint,
        endpoint_invalidator=invalidations.append,
    )

    with pytest.raises(Exception) as exc_info:
        handlers["provision"](
            _job("provision", cid, payload=_provision_payload())
        )

    assert exc_info.value.error_code == expected_error_code
    assert len(invalidations) == 1
    assert invalidations[0].endpoint is endpoint
    assert invalidations[0].reason == expected_reason


@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [
        (Mt5Error("authorization_failed"), "mt5_authorization_failed"),
        (Mt5Error("authorization_timeout"), "mt5_initialize_failed"),
        (Mt5IpcError("IPC timeout"), "mt5_initialize_failed"),
    ],
)
def test_auth_or_environment_failure_preserves_verified_endpoint(
    env,
    failure,
    expected_code,
):
    cid = str(uuid4())
    invalidations: list[EndpointInvalidation] = []

    handlers = _handlers(
        env,
        FakeApi(),
        script={"session_error": failure},
        endpoint_resolver=lambda _label, _server: _verified_endpoint(),
        endpoint_invalidator=invalidations.append,
    )

    with pytest.raises(Exception) as exc_info:
        handlers["provision"](
            _job("provision", cid, payload=_provision_payload())
        )

    assert exc_info.value.error_code == expected_code
    assert invalidations == []


def test_provision_trade_allowed_account_is_investor_access_not_verified(env):
    cid = str(uuid4())
    handlers = _handlers(env, FakeApi(), script={"trade_allowed": True})
    job = _job("provision", cid, payload=_provision_payload())
    with pytest.raises(Exception) as exc_info:
        handlers["provision"](job)
    assert exc_info.value.error_code == "investor_access_not_verified"


def test_native_investor_sync_timeout_has_a_recoverable_error_code(env):
    """A missing journal proof is not an MT5 initialization failure or proof of master access."""
    cid = str(uuid4())
    root = InstanceLayout(env.instances_root, cid).path
    terminal = root / "terminal"
    terminal.mkdir(parents=True)
    (terminal / "terminal64.exe").write_bytes(b"stub")
    expert = env.instances_root / "bridge.ex5"
    expert.write_bytes(b"stub")
    store = WindowsSecretStore(env.secrets_root)
    store.write(cid, "mt5_investor_password", "fixture-password")

    class TimeoutRuntime:
        def __init__(self, _root, _connection_id):
            pass

        def set_cancel_check(self, _check):
            pass

        def start(self, **_kwargs):
            raise real_handlers.NativeMt5Error("investor_sync_timeout")

    with pytest.raises(Exception) as exc_info:
        real_handlers._start_file_bridge_and_sync(
            _job("provision", cid),
            FakeApi(),
            root,
            cid,
            42,
            "Demo-Server",
            "203.0.113.10:443",
            "new_only",
            None,
            store,
            FakeProcessManager,
            expert,
            TimeoutRuntime,
            "https://example.invalid/events",
        )

    assert exc_info.value.error_code == "investor_verification_timeout"


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


def test_live_sync_accepts_existing_instance_after_template_pin_rotation(env):
    """A new global template pin must not invalidate an already-live instance."""
    cid = str(uuid4())
    original_digest = hashlib.sha256(b"stub").hexdigest()
    rotated_template_digest = "a" * 64
    InstanceProvisioner(env.instances_root, env.secrets_root).provision(
        cid,
        env.source_terminal,
        expected_terminal_sha256=original_digest,
    )

    handlers = _handlers(
        env,
        FakeApi(),
        terminal_sha256=rotated_template_digest,
    )

    # Reaching secret lookup proves live_sync accepted the instance's recorded
    # digest rather than rejecting it against the rotated global template pin.
    with pytest.raises(Exception) as exc_info:
        handlers["live_sync"](_job("live_sync", cid))
    assert exc_info.value.error_code == "secret_store_failed"


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
    assert not root.exists()
    assert not (env.secrets_root / cid).exists()
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
    assert not InstanceLayout(env.instances_root, cid_a).path.exists()
    root_b = InstanceLayout(env.instances_root, cid_b).path
    assert (root_b / "terminal" / "terminal64.exe").exists()
    assert WindowsSecretStore(env.secrets_root).read(cid_b, "mt5_login") == "999"


# ---------------------------------------------------------------------------
# Recovery: preserve valid terminals and clean only unsafe/ambiguous processes
# ---------------------------------------------------------------------------

def test_startup_reconciliation_adopts_valid_running_instance(env, monkeypatch):
    cid = str(uuid4())
    handlers = _handlers(env, FakeApi())
    handlers["provision"](_job("provision", cid, payload=_provision_payload()))
    root = InstanceLayout(env.instances_root, cid).path

    adopted = []
    cleaned = []
    monkeypatch.setattr(real_handlers.ProcessManager, "find", staticmethod(lambda executable: [4242]))

    class RecordingProcessManager(FakeProcessManager):
        def adopt(self, executable):
            adopted.append((self.state_path, executable))
            return 4242

        def cleanup_path(self, executable):
            cleaned.append((self.state_path, executable))
            return True

    result = reconcile_startup_instances(
        env.instances_root,
        env.secrets_root,
        process_factory=RecordingProcessManager,
    )

    assert result.adopted == (cid,)
    assert result.missing == ()
    assert result.terminated == ()
    assert result.blocked == ()
    assert adopted == [
        (
            root / "state" / "terminal-process.json",
            root / "terminal" / "terminal64.exe",
        )
    ]
    assert cleaned == []


def test_startup_reconciliation_leaves_missing_instance_for_live_sync(
    env,
    monkeypatch,
):
    cid = str(uuid4())
    handlers = _handlers(env, FakeApi())
    handlers["provision"](_job("provision", cid, payload=_provision_payload()))
    monkeypatch.setattr(
        real_handlers.ProcessManager,
        "find",
        staticmethod(lambda executable: []),
    )

    result = reconcile_startup_instances(
        env.instances_root,
        env.secrets_root,
        process_factory=FakeProcessManager,
    )

    assert result.missing == (cid,)
    assert result.adopted == ()
    assert result.terminated == ()


def test_startup_reconciliation_terminates_duplicate_exact_path_processes(
    env,
    monkeypatch,
):
    cid = str(uuid4())
    handlers = _handlers(env, FakeApi())
    handlers["provision"](_job("provision", cid, payload=_provision_payload()))
    cleaned = []
    monkeypatch.setattr(
        real_handlers.ProcessManager,
        "find",
        staticmethod(lambda executable: [4242, 4343]),
    )

    class RecordingProcessManager(FakeProcessManager):
        def cleanup_path(self, executable):
            cleaned.append(executable)
            return True

    result = reconcile_startup_instances(
        env.instances_root,
        env.secrets_root,
        process_factory=RecordingProcessManager,
    )

    assert result.terminated == (cid,)
    assert result.adopted == ()
    assert cleaned == [
        InstanceLayout(env.instances_root, cid).path
        / "terminal"
        / "terminal64.exe"
    ]


def test_startup_reconciliation_adopt_failure_preserves_valid_terminal(
    env,
    monkeypatch,
):
    cid = str(uuid4())
    handlers = _handlers(env, FakeApi())
    handlers["provision"](_job("provision", cid, payload=_provision_payload()))
    cleaned = []
    monkeypatch.setattr(
        real_handlers.ProcessManager,
        "find",
        staticmethod(lambda executable: [4242]),
    )

    class FailingAdoptionProcessManager(FakeProcessManager):
        def adopt(self, executable):
            raise OSError("sanitized fixture write failure")

        def cleanup_path(self, executable):
            cleaned.append(executable)
            return True

    result = reconcile_startup_instances(
        env.instances_root,
        env.secrets_root,
        process_factory=FailingAdoptionProcessManager,
    )

    assert result.blocked == (cid,)
    assert result.adopted == ()
    assert result.terminated == ()
    assert cleaned == []


def test_startup_reconciliation_terminates_invalid_publication(
    env,
    monkeypatch,
):
    cid = str(uuid4())
    handlers = _handlers(env, FakeApi())
    handlers["provision"](_job("provision", cid, payload=_provision_payload()))
    root = InstanceLayout(env.instances_root, cid).path
    atomic_json(
        root / "state" / "instance.json",
        {"connection_id": cid, "status": "deprovisioned"},
    )
    monkeypatch.setattr(
        real_handlers.ProcessManager,
        "find",
        staticmethod(lambda executable: [4242]),
    )

    result = reconcile_startup_instances(
        env.instances_root,
        env.secrets_root,
        process_factory=FakeProcessManager,
    )

    assert result.terminated == (cid,)
    assert result.adopted == ()


def test_startup_reconciliation_empty_root_is_safe(tmp_path):
    assert reconcile_startup_instances(
        tmp_path / "does-not-exist",
        tmp_path / "secrets",
    ) == real_handlers.StartupReconciliation()


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
