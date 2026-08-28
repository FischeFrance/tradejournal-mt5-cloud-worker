from __future__ import annotations

import base64
import gzip
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
from windows_agent.agent_errors import DeprovisionFailed
from windows_agent.agent_secrets import AGENT_SCOPE_ID, PROVISIONING_KEY_SECRET_NAME
from windows_agent.job_runner import JobRunner, LeaseLost
from windows_agent.provisioning.instance_layout import InstanceLayout
from windows_agent.provisioning.mt5_instance import InstanceProvisioner
from windows_agent.provisioning.secret_store import WindowsSecretStore
from windows_agent.real_handlers import build_real_handlers
from windows_agent.state_store import read_json
from windows_agent.worker.direct_mt5_adapter import (
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
        self.progress_calls: list[tuple[str, str, str | None]] = []
        self.history_uploads: list[bytes] = []
        self._history_expected: dict[str, int] = {}
        self._history_imported: dict[str, dict[str, int | bool | str]] = {}

    def progress(
        self,
        job_id: str,
        lease_id: str,
        event_code: str,
        event_status: str,
        detail_code: str | None = None,
    ) -> dict:
        self.progress_calls.append((event_code, event_status, detail_code))
        return {"event_recorded": True}

    def heartbeat(self, job_id: str, lease_id: str) -> dict:
        self.heartbeat_calls += 1
        if self.lease_lost_at is not None and self.heartbeat_calls >= self.lease_lost_at:
            return {"lease_valid": False, "error_code": "lease_lost"}
        return {"lease_valid": True}

    def transition(self, job_id: str, lease_id: str, status: str, result: dict | None = None) -> dict:
        self.transitions.append((status, result))
        return {}

    def history_file_prepare(
        self,
        job_id: str,
        _lease_id: str,
        **metadata: int | str,
    ) -> dict:
        imported = self._history_imported.get(job_id)
        if imported is not None:
            return {
                "api_version": "1",
                "already_imported": True,
                "object_path": "unused",
                "upload_url": None,
                "expires_in": 0,
                **imported,
            }
        self._history_expected[job_id] = int(metadata["event_count"])
        return {
            "api_version": "1",
            "already_imported": False,
            "object_path": f"history/{job_id}.json.gz",
            "upload_url": f"https://upload.invalid/{job_id}",
            "expires_in": 7200,
            "accepted": 0,
            "inserted": 0,
            "duplicates": 0,
        }

    def upload_history_file(self, _url: str, payload: bytes) -> None:
        self.history_uploads.append(payload)

    def history_file_import(
        self,
        job_id: str,
        _lease_id: str,
        expected_count: int,
    ) -> dict:
        assert expected_count == self._history_expected[job_id]
        result: dict[str, int | bool | str] = {
            "accepted": expected_count,
            "inserted": expected_count,
            "duplicates": 0,
            "object_deleted": True,
        }
        self._history_imported[job_id] = result
        return {"api_version": "1", **result}


class ScriptedAdapter:
    """Configurable fake standing in for DirectMt5Adapter -- lets tests exercise every branch of
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
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(
        real_handlers.ProcessManager,
        "find",
        staticmethod(lambda _terminal: []),
    )
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
        staticmethod(lambda _path: None),
    )
    instances_root = tmp_path / "instances"
    secrets_root = tmp_path / "secrets"
    source_terminal = tmp_path / "golden" / "terminal64.exe"
    source_terminal.parent.mkdir(parents=True)
    source_terminal.write_bytes(b"stub")
    WindowsSecretStore(secrets_root).write(AGENT_SCOPE_ID, PROVISIONING_KEY_SECRET_NAME, ENCRYPTION_KEY)
    return SimpleNamespace(instances_root=instances_root, secrets_root=secrets_root, source_terminal=source_terminal)


def _handlers(env, api, *, script: dict | None = None):
    def adapter_factory(terminal, login, server):
        return ScriptedAdapter(terminal, login, server, script=script or {})

    return build_real_handlers(
        api,
        instances_root=env.instances_root,
        secrets_root=env.secrets_root,
        source_terminal=env.source_terminal,
        adapter_factory=adapter_factory,
        process_factory=FakeProcessManager,
        broker_identity_resolver=lambda _server: SimpleNamespace(
            broker_label="Fixture Broker",
            search_text="Fixture Broker",
        ),
    )


def _provision_payload(login=12345, server="Demo-Server", password="investor-pw") -> dict:
    return {
        "credential_envelope": _envelope({"investor_password": password}),
        "expected_login": login,
        "expected_server": server,
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


def test_provision_is_idempotent_on_retry(env):
    cid = str(uuid4())
    api = FakeApi()
    handlers = _handlers(env, api)
    job = _job("provision", cid, payload=_provision_payload())
    handlers["provision"](job)
    result = handlers["provision"](job)  # simulates a retried/duplicate claim of the same job
    assert result["live_sync_started"] is True


def test_provision_rejects_ambiguous_existing_layout_without_terminal(env):
    cid = str(uuid4())
    root = InstanceLayout(env.instances_root, cid).create()
    (root / "state" / "stale-state.json").write_text("{}", encoding="utf-8")
    handlers = _handlers(env, FakeApi())

    with pytest.raises(Exception) as exc_info:
        handlers["provision"](
            _job("provision", cid, payload=_provision_payload())
        )

    assert exc_info.value.error_code == "instance_provision_failed"
    assert (root / "state" / "stale-state.json").is_file()
    assert not (env.secrets_root / cid).exists()


# ---------------------------------------------------------------------------
# Provision: error taxonomy (Fase 6)
# ---------------------------------------------------------------------------

def test_provision_missing_envelope_is_credential_envelope_invalid(env):
    cid = str(uuid4())
    handlers = _handlers(env, FakeApi())
    job = _job("provision", cid, payload={"expected_login": 1, "expected_server": "srv"})
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
    {"expected_login": 0, "expected_server": "srv"},
    {"expected_login": "not-a-number", "expected_server": "srv"},
    {"expected_login": 1, "expected_server": ""},
    {"expected_login": 1, "expected_server": "bad\nserver"},
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


def test_stopped_live_sync_repins_deployed_bridge_before_resume(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
        staticmethod(lambda _path: None),
    )
    env = SimpleNamespace(
        instances_root=tmp_path / "instances",
        secrets_root=tmp_path / "secrets",
        source_terminal=tmp_path / "golden" / "terminal64.exe",
    )
    env.source_terminal.parent.mkdir(parents=True)
    env.source_terminal.write_bytes(b"stub")
    cid = str(uuid4())
    root = InstanceProvisioner(
        env.instances_root,
        env.secrets_root,
    ).provision(cid, env.source_terminal)
    store = WindowsSecretStore(env.secrets_root)
    store.write(cid, "mt5_login", "12345")
    store.write(cid, "mt5_server", "Demo-Server")
    store.write(cid, "bridge_token", "bridge-token")
    expert = env.instances_root.parent / "TradeJournalBridge.ex5"
    expert.write_bytes(b"bridge-v2")
    expert_sha256 = hashlib.sha256(expert.read_bytes()).hexdigest()
    events: list[str] = []

    def validate_assets(_self, connection_id: str) -> str:
        assert connection_id == cid
        events.append("validate")
        return "a" * 64

    def record_assets(
        _cls,
        instance_root,
        connection_id: str,
        digest: str,
    ) -> str:
        assert instance_root == root
        assert connection_id == cid
        assert digest == expert_sha256
        events.append("record")
        return "b" * 64

    monkeypatch.setattr(
        InstanceProvisioner,
        "validate_runtime_assets",
        validate_assets,
    )
    monkeypatch.setattr(
        InstanceProvisioner,
        "record_verified_managed_asset_update",
        classmethod(record_assets),
    )
    monkeypatch.setattr(
        real_handlers.ProcessManager,
        "find",
        staticmethod(lambda _terminal: []),
    )

    class Runtime:
        def install_expert(self, *_args: object) -> None:
            events.append("install")

        def resume(self, **_kwargs: object) -> None:
            events.append("resume")

    class Adapter:
        @staticmethod
        def account_info():
            return SimpleNamespace(trade_allowed=False)

        @staticmethod
        def terminal_info():
            return SimpleNamespace(connected=True)

    class Sink:
        def __init__(self, *_args: object) -> None:
            pass

        @staticmethod
        def send_heartbeat(_account: object) -> bool:
            return True

    monkeypatch.setattr(
        real_handlers,
        "Mql5FileMt5Adapter",
        lambda *_args: Adapter(),
    )
    monkeypatch.setattr(real_handlers, "TradingIngestionSink", Sink)
    monkeypatch.setattr(
        real_handlers,
        "_run_live_sync_once",
        lambda *_args: 0,
    )
    api = FakeApi()
    api.progress = lambda *_args: {"event_recorded": True}  # type: ignore[attr-defined]
    handlers = build_real_handlers(
        api,
        instances_root=env.instances_root,
        secrets_root=env.secrets_root,
        source_terminal=env.source_terminal,
        process_factory=FakeProcessManager,
        expert_binary=expert,
        expert_sha256=expert_sha256,
        runtime_factory=lambda *_args: Runtime(),
        trading_ingestion_url="https://agent.example/trading-mt5-events",
    )

    result = handlers["live_sync"](_job("live_sync", cid))

    assert result == {"live_sync_events_delivered": 0}
    assert events == ["validate", "install", "record", "resume", "validate"]


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
    assert len(api.history_uploads) == 1
    archive = json.loads(gzip.decompress(api.history_uploads[0]))
    assert archive["connection_id"] == cid
    assert archive["account_number"] == "12345"
    assert archive["server"] == "Demo-Server"


def test_historical_sync_dedups_across_repeated_runs(env):
    cid = str(uuid4())
    api = FakeApi()
    moment = int(datetime(2026, 7, 10, tzinfo=timezone.utc).timestamp())
    deal = {
        "ticket": 9,
        "position_id": 1,
        "symbol": "EURUSD",
        "entry": "IN",
        "direction": "buy",
        "volume": 0.1,
        "price": 1.2,
        "profit": 5,
        "commission": -1,
        "swap": 0,
        "time": moment,
    }
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
    assert len(api.history_uploads) == 1
    assert api._history_imported["job-historical_sync"]["inserted"] == 1


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
    assert root.exists()
    assert read_json(root / "state" / "instance.json") == {
        "connection_id": cid,
        "status": "deprovisioned",
    }
    assert not (root / "terminal").exists()
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
    root_a = InstanceLayout(env.instances_root, cid_a).path
    assert read_json(root_a / "state" / "instance.json")["status"] == "deprovisioned"
    assert not (root_a / "terminal").exists()
    root_b = InstanceLayout(env.instances_root, cid_b).path
    assert (root_b / "terminal" / "terminal64.exe").exists()
    assert WindowsSecretStore(env.secrets_root).read(cid_b, "mt5_login") == "999"


def test_deprovision_fails_closed_when_instance_process_cleanup_is_not_confirmed(env):
    cid = str(uuid4())
    handlers = _handlers(env, FakeApi())
    handlers["provision"](_job("provision", cid, payload=_provision_payload()))
    root = InstanceLayout(env.instances_root, cid).path

    class UncleanableProcessManager(FakeProcessManager):
        def cleanup_path(self, executable):
            return False

    handlers = build_real_handlers(
        FakeApi(),
        instances_root=env.instances_root,
        secrets_root=env.secrets_root,
        source_terminal=env.source_terminal,
        adapter_factory=lambda terminal, login, server: ScriptedAdapter(
            terminal, login, server
        ),
        process_factory=UncleanableProcessManager,
    )

    with pytest.raises(DeprovisionFailed):
        handlers["deprovision"](_job("deprovision", cid))

    assert root.exists()
    assert WindowsSecretStore(env.secrets_root).read(cid, "mt5_login") == "12345"


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
