from __future__ import annotations

import base64
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from windows_agent.agent_secrets import AGENT_SCOPE_ID, PROVISIONING_KEY_SECRET_NAME
from windows_agent.job_runner import JobRunner
from windows_agent.provisioning.secret_store import WindowsSecretStore
from windows_agent.real_handlers import build_real_handlers
from windows_agent.worker.native_mt5_runtime import NativeMt5Status


KEY = base64.b64encode(b"8" * 32).decode("ascii")


def _envelope(payload: dict[str, str]) -> dict[str, str]:
    nonce = os.urandom(12)
    encrypted = AESGCM(base64.b64decode(KEY)).encrypt(nonce, json.dumps(payload).encode(), None)
    return {"alg": "aes-256-gcm-v1", "iv": base64.b64encode(nonce).decode(), "ciphertext": base64.b64encode(encrypted).decode()}


def _bridge_envelope(payload: object, sequence: int = 1) -> dict[str, object]:
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "sequence": sequence,
        "account_identity": {"login": "42", "server": "Demo"},
        "server_identity": "Demo",
        "payload": payload,
    }


class FakeNativeRuntime:
    def __init__(self, root: Path, connection_id: str) -> None:
        self.root = root
        self.connection_id = connection_id

    def start(self, **kwargs: Any) -> NativeMt5Status:
        assert kwargs["expert_binary"].name == "TradeJournalBridge.ex5"
        files = self.root / "terminal" / "MQL5" / "Files" / "TradeJournal"
        files.mkdir(parents=True, exist_ok=True)
        records = {
            "heartbeat.json": {"terminal_connected": True, "account_trade_allowed": False},
            "account.json": {"login": "42", "server": "Demo", "trade_allowed": False},
            "positions.json": [],
            "orders.json": [],
            "history_orders.json": [{"ticket": "1", "time": "2026-07-01T00:00:00Z"}],
            "deals.json": [{"ticket": "2", "position_id": "1", "time": "2026-07-01T00:00:00Z"}],
        }
        for name, payload in records.items():
            (files / name).write_text(json.dumps(_bridge_envelope(payload)), encoding="utf-8")
        return NativeMt5Status(999, records["account.json"], records["heartbeat.json"], files)


class FakeProcessManager:
    def __init__(self, state_path: Path) -> None:
        self.state_path = state_path

    def adopt(self, executable: Path) -> int:
        return 999

    def stop(self) -> bool:
        return True

    def cleanup_path(self, executable: Path) -> bool:
        return True


class QueueApi:
    def __init__(self, jobs: list[dict[str, object]]) -> None:
        self.jobs = list(jobs)
        self.transitions: list[tuple[str, str, dict[str, object] | None]] = []

    def claim(self) -> dict[str, object]:
        return self.jobs.pop(0) if self.jobs else {}

    def heartbeat(self, job_id: str, lease_id: str) -> dict[str, bool]:
        return {"lease_valid": True}

    def transition(self, job_id: str, lease_id: str, status: str, result: dict[str, object] | None = None) -> dict[str, str]:
        self.transitions.append((job_id, status, result))
        return {"status": status}


def test_daemon_uses_native_file_bridge_by_default_and_deprovisions(tmp_path: Path, monkeypatch) -> None:
    # DPAPI itself is Windows-only. This mock preserves the secret-store interface so the E2E
    # verifies the file bridge on every OS; production still exercises real DPAPI on Windows.
    monkeypatch.setattr(WindowsSecretStore, "_crypt_protect", staticmethod(lambda value: value))
    monkeypatch.setattr(WindowsSecretStore, "_crypt_unprotect", staticmethod(lambda value: value))
    monkeypatch.setattr(WindowsSecretStore, "restrict_acl", staticmethod(lambda path: None))
    cid = str(uuid4())
    instances, secrets = tmp_path / "instances", tmp_path / "secrets"
    terminal = tmp_path / "template" / "terminal64.exe"
    expert = tmp_path / "template" / "TradeJournalBridge.ex5"
    terminal.parent.mkdir(parents=True)
    terminal.write_bytes(b"terminal")
    expert.write_bytes(b"expert")
    WindowsSecretStore(secrets).write(AGENT_SCOPE_ID, PROVISIONING_KEY_SECRET_NAME, KEY)
    jobs = [
        {"job_id": "provision", "job_type": "provision", "connection_id": cid, "lease_id": "1", "history_mode": "all_available", "payload": {"credential_envelope": _envelope({"investor_password": "read-only"}), "expected_login": 42, "expected_server": "Demo"}},
        {"job_id": "deprovision", "job_type": "deprovision", "connection_id": cid, "lease_id": "2", "history_mode": None, "payload": {}},
    ]
    api = QueueApi(jobs)
    handlers = build_real_handlers(
        api,
        instances_root=instances,
        secrets_root=secrets,
        source_terminal=terminal,
        expert_binary=expert,
        process_factory=FakeProcessManager,
        runtime_factory=FakeNativeRuntime,
    )
    runner = JobRunner(tmp_path / "agent-state.json", api, handlers)
    while runner.run_once():
        pass
    completed = [job_id for job_id, status, _ in api.transitions if status == "complete"]
    assert completed == ["provision", "deprovision"]
    result = next(result for job_id, status, result in api.transitions if job_id == "provision" and status == "complete")
    assert result is not None and result["result"]["file_bridge"] == "mql5-local-json"
    assert not (instances / cid / "terminal").exists()
    assert not (secrets / cid).exists()
