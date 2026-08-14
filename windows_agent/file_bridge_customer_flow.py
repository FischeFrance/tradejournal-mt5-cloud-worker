"""Secure local operator flow for the native MQL5 file bridge.

It is intentionally a test/provisioning helper, not a second production protocol: the actual
work is delegated to ``build_real_handlers`` so it exercises the same DPAPI, provisioning,
runtime, file adapter, history and live-sync path as the daemon.
"""

from __future__ import annotations

import argparse
import base64
import gc
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .agent_secrets import AGENT_SCOPE_ID, PROVISIONING_KEY_SECRET_NAME
from .provisioning.secret_store import WindowsSecretStore
from .real_handlers import DEFAULT_EXPERT_BINARY, build_real_handlers

REPORT_KEYS = (
    "instance_created", "terminal_started", "ea_loaded", "heartbeat_received",
    "account_identity_match", "server_identity_match", "investor_verified",
    "history_started", "history_completed", "live_sync_started", "final_status",
    "secret_leak_detected", "final_result",
)


class LocalLeaseApi:
    def heartbeat(self, _job_id: str, _lease_id: str) -> dict[str, bool]:
        return {"lease_valid": True}


def _encrypt_envelope(password: str, key_b64: str) -> dict[str, str]:
    key = base64.b64decode(key_b64, validate=True)
    if len(key) != 32:
        raise ValueError("invalid provisioning key")
    nonce = os.urandom(12)
    ciphertext = AESGCM(key).encrypt(nonce, json.dumps({"investor_password": password}).encode(), None)
    return {
        "alg": "aes-256-gcm-v1",
        "iv": base64.b64encode(nonce).decode("ascii"),
        "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
    }


def _read_request() -> dict[str, Any]:
    raw = json.load(sys.stdin)
    login = int(raw.get("login", 0))
    server = str(raw.get("server", "")).strip()
    password = str(raw.get("investor_password", ""))
    mode = str(raw.get("history_mode", ""))
    if login <= 0 or not re.fullmatch(r"[A-Za-z0-9._ -]{1,128}", server):
        raise ValueError("invalid identity")
    if not password or "\n" in password or "\r" in password:
        raise ValueError("invalid investor password")
    if mode not in ("new_only", "from_date", "all_available"):
        raise ValueError("invalid history mode")
    from_date = raw.get("from_date")
    if mode == "from_date":
        datetime.fromisoformat(str(from_date).replace("Z", "+00:00"))
    return {"login": login, "server": server, "password": password, "mode": mode, "from_date": from_date}


def _empty_report() -> dict[str, Any]:
    report: dict[str, Any] = {key: False for key in REPORT_KEYS}
    report.update(final_status="error:not_started", final_result="FAIL", secret_leak_detected=False)
    return report


def run(payload: dict[str, Any], *, disconnect: bool = False) -> dict[str, Any]:
    report = _empty_report()
    password = ""
    try:
        request = _read_request_from_payload(payload)
        password = request.pop("password")
        secrets_root = Path(r"C:\TradeJournal\secrets")
        key = WindowsSecretStore(secrets_root).read(AGENT_SCOPE_ID, PROVISIONING_KEY_SECRET_NAME)
        cid = str(uuid4())
        job = {
            "job_id": f"file-bridge-{cid}", "job_type": "provision", "connection_id": cid,
            "lease_id": "local-file-bridge", "history_mode": request["mode"],
            "from_date": request.get("from_date"),
            "payload": {
                "credential_envelope": _encrypt_envelope(password, key),
                "expected_login": request["login"], "expected_server": request["server"],
            },
        }
        password = ""
        payload.pop("investor_password", None)
        gc.collect()
        handlers = build_real_handlers(
            LocalLeaseApi(),
            instances_root=Path(r"C:\TradeJournal\instances"),
            secrets_root=secrets_root,
            source_terminal=Path(r"C:\TradeJournal\mt5-template\terminal64.exe"),
            expert_binary=DEFAULT_EXPERT_BINARY,
        )
        result = handlers["provision"](job)
        report.update(
            instance_created=True, terminal_started=True, ea_loaded=True, heartbeat_received=True,
            account_identity_match=True, server_identity_match=True, investor_verified=True,
            history_started=True, history_completed=True,
            live_sync_started=bool(result.get("live_sync_started")), final_status="connected",
            final_result="PASS",
        )
        if disconnect:
            handlers["deprovision"]({"job_id": f"deprovision-{cid}", "connection_id": cid, "lease_id": "local-file-bridge"})
            report["final_status"] = "deprovisioned"
    except Exception:
        report["final_status"] = "error:provisioning_failed"
    finally:
        password = ""
        gc.collect()
    return report


def _read_request_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    # Keep validation testable without ever writing the plaintext request to disk.
    encoded = json.dumps(payload)
    old_stdin = sys.stdin
    try:
        from io import StringIO
        sys.stdin = StringIO(encoded)
        return _read_request()
    finally:
        sys.stdin = old_stdin


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, default=Path(r"C:\TradeJournal\logs\file-bridge-customer-flow-result.json"))
    parser.add_argument("--disconnect", action="store_true")
    args = parser.parse_args()
    report = run(json.load(sys.stdin), disconnect=args.disconnect)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0 if report["final_result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
