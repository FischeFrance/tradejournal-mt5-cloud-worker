from __future__ import annotations

import base64
import json
import os
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from windows_agent.agent_daemon import run_forever
from windows_agent.job_runner import JobRunner
from windows_agent.provisioning.secret_store import WindowsSecretStore
from windows_agent.real_handlers import build_real_handlers

"""Fase 8 end-to-end: mock control plane -> agent_daemon.run_forever()/JobRunner -> the real
build_real_handlers() factory -> a fake MT5 adapter, covering the full provision -> heartbeat ->
historical_sync -> deprovision lifecycle. This goes through the exact same JobRunner and
build_real_handlers() that agent_daemon.build_runner()/windows_service.py wire together in
production -- unlike customer_flow.py's CLI path, no LocalControlPlane is involved anywhere here."""

ENCRYPTION_KEY = base64.b64encode(b"7" * 32).decode("ascii")


def _envelope(payload: dict) -> dict:
    key = base64.b64decode(ENCRYPTION_KEY)
    iv = os.urandom(12)
    ciphertext = AESGCM(key).encrypt(iv, json.dumps(payload).encode("utf-8"), None)
    return {
        "alg": "aes-256-gcm-v1",
        "iv": base64.b64encode(iv).decode("ascii"),
        "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
    }


class ScriptedAdapter:
    def __init__(self, terminal, login, server) -> None:
        self.login, self.server = login, server

    @contextmanager
    def session(self, password: str):
        yield self

    def account_info(self) -> Any:
        return SimpleNamespace(trade_allowed=False)

    def terminal_info(self) -> Any:
        return SimpleNamespace(connected=True)

    def verify_identity(self) -> dict:
        return {"login": str(self.login), "server": self.server}

    def history_orders(self, start, end):
        return ()

    def history_deals(self, start, end):
        return ()

    def snapshot(self, lookback_hours: int = 72) -> dict:
        return {"positions": {}, "orders": {}, "deals": {}}


class FakeProcessManager:
    def __init__(self, state_path) -> None:
        self.state_path = state_path

    def adopt(self, executable):
        raise RuntimeError("no real OS process in this mock")

    def stop(self) -> bool:
        return True

    def cleanup_path(self, executable) -> bool:
        return True


class MockControlPlane:
    """Fake trading-agent Edge Function: a fixed queue of jobs for one connection_id, served in
    order exactly like the real claim_mt5_provisioning_job RPC (one job claimed at a time)."""

    def __init__(self, jobs: list[dict]) -> None:
        self._queue = list(jobs)
        self.transitions: list[tuple[str, str, dict | None]] = []

    def claim(self) -> dict:
        return self._queue.pop(0) if self._queue else {}

    def heartbeat(self, job_id: str, lease_id: str) -> dict:
        return {"api_version": "1", "lease_valid": True}

    def transition(self, job_id: str, lease_id: str, status: str, result: dict | None = None) -> dict:
        self.transitions.append((job_id, status, result))
        return {"api_version": "1", "status": status}


def test_full_provision_historical_sync_deprovision_lifecycle_through_daemon(tmp_path):
    cid = str(uuid4())
    instances_root = tmp_path / "instances"
    secrets_root = tmp_path / "secrets"
    source_terminal = tmp_path / "golden" / "terminal64.exe"
    source_terminal.parent.mkdir(parents=True)
    source_terminal.write_bytes(b"stub")

    from windows_agent.agent_secrets import AGENT_SCOPE_ID, PROVISIONING_KEY_SECRET_NAME

    WindowsSecretStore(secrets_root).write(AGENT_SCOPE_ID, PROVISIONING_KEY_SECRET_NAME, ENCRYPTION_KEY)

    jobs = [
        {
            "job_id": "job-provision",
            "job_type": "provision",
            "connection_id": cid,
            "lease_id": "lease-1",
            "history_mode": "new_only",
            "from_date": None,
            "payload": {
                "credential_envelope": _envelope({"investor_password": "investor-pw"}),
                "expected_login": 555,
                "expected_server": "Demo-Broker",
            },
        },
        {
            "job_id": "job-historical-sync",
            "job_type": "historical_sync",
            "connection_id": cid,
            "lease_id": "lease-2",
            "history_mode": "from_date",
            "from_date": "2026-07-10T00:00:00Z",
            "payload": {},
        },
        {
            "job_id": "job-deprovision",
            "job_type": "deprovision",
            "connection_id": cid,
            "lease_id": "lease-3",
            "history_mode": None,
            "from_date": None,
            "payload": {},
        },
    ]
    control_plane = MockControlPlane(jobs)

    def adapter_factory(terminal, login, server):
        return ScriptedAdapter(terminal, login, server)

    handlers = build_real_handlers(
        control_plane,
        instances_root=instances_root,
        secrets_root=secrets_root,
        source_terminal=source_terminal,
        adapter_factory=adapter_factory,
        process_factory=FakeProcessManager,
    )
    runner = JobRunner(tmp_path / "agent-job.json", control_plane, handlers)

    stop_event = threading.Event()

    def stop_once_drained():
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if len(control_plane.transitions) >= len(jobs) * 2:  # running + complete per job
                break
            time.sleep(0.01)
        stop_event.set()

    watcher = threading.Thread(target=stop_once_drained)
    watcher.start()
    run_forever(runner, poll_seconds=0.01, stop_event=stop_event)
    watcher.join(timeout=2)

    completed = [job_id for job_id, status, _ in control_plane.transitions if status == "complete"]
    failed = [job_id for job_id, status, _ in control_plane.transitions if status == "fail"]
    assert failed == []
    assert completed == ["job-provision", "job-historical-sync", "job-deprovision"]

    store = WindowsSecretStore(secrets_root)
    # Provisioned secrets must be gone after deprovision -- proves deprovision actually ran
    # its DPAPI cleanup, not just returned a fake success.
    try:
        store.read(cid, "mt5_investor_password")
        assert False, "secret should have been deleted by deprovision"
    except Exception:
        pass

    provision_result = next(r for job_id, status, r in control_plane.transitions if job_id == "job-provision" and status == "complete")
    assert provision_result["result"]["live_sync_started"] is True
