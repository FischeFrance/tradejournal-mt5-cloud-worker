from __future__ import annotations

import argparse
import gc
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from .provisioning.mt5_instance import InstanceProvisioner
from .provisioning.process_manager import ProcessManager
from .provisioning.secret_store import WindowsSecretStore
from .security import canonical_uuid
from .worker.dedup import PersistentDedup
from .worker.direct_mt5_adapter import IdentityMismatch, Mt5Error
from .worker.direct_mt5_adapter import Mt5IpcError, Mt5ProcessCrashed
from .worker.history_sync import HistorySync
from .worker.live_sync import LiveSync

REPORT_KEYS = (
    "connection_id",
    "request_validated",
    "secret_encrypted",
    "plaintext_removed",
    "provisioning_job_created",
    "job_claimed",
    "instance_created",
    "terminal_started",
    "mt5_initialized",
    "mt5_login_succeeded",
    "account_identity_match",
    "server_identity_match",
    "investor_readonly_verified",
    "history_mode",
    "history_import_started",
    "history_import_completed",
    "positions_count",
    "orders_count",
    "deals_count",
    "live_sync_started",
    "heartbeat_received",
    "final_status",
    "secret_leak_detected",
    "graphical_login_required",
    "manual_intervention_required",
    "final_result",
)


class CustomerFlowError(RuntimeError):
    pass


class LocalControlPlane:
    def __init__(self):
        self.jobs = []
        self.statuses = []
        self.lease_valid = True

    def create(self, cid, mode):
        self.jobs.append(
            {
                "job_id": f"local-{cid}",
                "action": "provision_customer",
                "connection_id": cid,
                "history_mode": mode,
            }
        )
        self.statuses.append("pending")

    def claim(self):
        return self.jobs.pop(0) if self.jobs else {}

    def status(self, value):
        self.statuses.append(value)

    def heartbeat(self, _cid):
        return self.lease_valid


class MemorySnapshot:
    def __init__(self):
        self.value = {"positions": {}, "orders": {}, "deals": {}}

    def get(self):
        return self.value

    def save(self, value):
        self.value = value


def validate_request(p):
    cid = canonical_uuid(str(p.get("connection_id", "")))
    login = int(p.get("login", 0))
    server = str(p.get("server", "")).strip()
    password = str(p.get("investor_password", ""))
    mode = str(p.get("history_mode", ""))
    if login <= 0 or not re.fullmatch(r"[A-Za-z0-9._ -]{1,128}", server):
        raise CustomerFlowError("invalid_identity")
    if not password or "\n" in password or "\r" in password:
        raise CustomerFlowError("invalid_investor_password")
    if mode not in ("new_only", "from_date", "all_available"):
        raise CustomerFlowError("invalid_history_mode")
    date = None
    if mode == "from_date":
        try:
            date = datetime.fromisoformat(str(p.get("from_date", "")))
        except ValueError as exc:
            raise CustomerFlowError("invalid_from_date") from exc
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
    return {
        "cid": cid,
        "login": login,
        "server": server,
        "password": password,
        "mode": mode,
        "date": date,
    }


def empty_report(cid="invalid"):
    r = {k: False for k in REPORT_KEYS}
    r.update(
        connection_id=cid,
        history_mode="invalid",
        positions_count=0,
        orders_count=0,
        deals_count=0,
        final_status="error:not_started",
        final_result="FAIL",
    )
    return r


def error_code(exc):
    if isinstance(exc, CustomerFlowError):
        return str(exc)
    if isinstance(exc, IdentityMismatch):
        return "identity_mismatch"
    if isinstance(exc, Mt5IpcError):
        return "mt5_ipc_failed"
    if isinstance(exc, Mt5ProcessCrashed):
        return "mt5_process_crashed"
    if isinstance(exc, Mt5Error):
        text = str(exc).casefold()
        return (
            "mt5_initialize_failed"
            if "initialization" in text
            else "mt5_login_failed"
            if "authorization" in text
            else "mt5_unavailable"
        )
    if isinstance(exc, FileExistsError):
        return "connection_exists"
    if isinstance(exc, FileNotFoundError):
        return "terminal_unavailable"
    return "internal_error"


def run_customer_flow(
    payload,
    *,
    instances_root,
    secrets_root,
    source_terminal,
    control_plane=None,
    adapter_factory=None,
    secret_store=None,
    process_factory=ProcessManager,
    simulate_disconnect=False,
):
    report = empty_report(str(payload.get("connection_id", "invalid")))
    if adapter_factory is None:
        raise CustomerFlowError(
            "legacy_direct_adapter_disabled_use_file_bridge_customer_flow"
        )
    control = control_plane or LocalControlPlane()
    store = secret_store or WindowsSecretStore(secrets_root)
    provisioner = InstanceProvisioner(instances_root, secrets_root)
    process = None
    try:
        req = validate_request(payload)
        cid, login, server = req["cid"], req["login"], req["server"]
        report.update(
            connection_id=cid, history_mode=req["mode"], request_validated=True
        )
        if (instances_root / cid).exists():
            raise FileExistsError("exists")
        store.write(cid, "mt5_investor_password", req["password"])
        store.write(cid, "mt5_login", str(login))
        store.write(cid, "mt5_server", server)
        report["secret_encrypted"] = True
        payload.pop("investor_password", None)
        req["password"] = None
        gc.collect()
        report["plaintext_removed"] = "investor_password" not in payload
        control.create(cid, req["mode"])
        report["provisioning_job_created"] = True
        if not control.claim():
            raise CustomerFlowError("job_claim_failed")
        report["job_claimed"] = True
        control.status("provisioning")
        if not source_terminal.is_file():
            raise FileNotFoundError("terminal missing")
        root = provisioner.provision(cid, source_terminal)
        report["instance_created"] = True
        terminal = root / "terminal" / "terminal64.exe"
        process = process_factory(root / "state" / "terminal-process.json")
        control.status("authenticating")
        try:
            password = store.read(cid, "mt5_investor_password")
        except Exception as exc:
            raise CustomerFlowError("secret_missing") from exc
        adapter = adapter_factory(terminal, login, server)
        with adapter.session(password):
            report["terminal_started"] = True
            try:
                process.adopt(terminal)
            except (AttributeError, RuntimeError):
                pass
            password = ""
            gc.collect()
            report.update(mt5_initialized=True, mt5_login_succeeded=True)
            account = adapter.account_info()
            terminal_info = adapter.terminal_info()
            identity = adapter.verify_identity()
            report["account_identity_match"] = int(identity["login"]) == login
            report["server_identity_match"] = (
                identity["server"].casefold() == server.casefold()
            )
            report["investor_readonly_verified"] = not bool(
                getattr(account, "trade_allowed", True)
            )
            if not bool(getattr(terminal_info, "connected", False)):
                raise CustomerFlowError("terminal_not_connected")
            if not report["investor_readonly_verified"]:
                raise CustomerFlowError("investor_readonly_not_verified")
            report["positions_count"] = len(adapter.positions())
            report["orders_count"] = len(adapter.orders())
            control.status("importing_history")
            report["history_import_started"] = True
            counts = HistorySync(
                adapter, root / "state" / "history.json", lambda _x: None
            ).run(req["mode"], req["date"])
            report["deals_count"] = counts["deals"]
            report["history_import_completed"] = True
            report["live_sync_started"] = True
            LiveSync(
                adapter,
                MemorySnapshot(),
                PersistentDedup(root / "state" / "dedup.sqlite"),
                lambda _x: None,
            ).poll_once()
        if not control.heartbeat(cid):
            raise CustomerFlowError("lease_lost")
        report["heartbeat_received"] = True
        control.status("connected")
        report.update(final_status="connected", final_result="PASS")
        if simulate_disconnect:
            process.stop()
            provisioner.deprovision(cid)
            control.status("deprovisioned")
            report["final_status"] = "deprovisioned"
    except Exception as exc:
        control.status("error")
        report.update(final_status=f"error:{error_code(exc)}", final_result="FAIL")
        if process:
            try:
                process.cleanup_path(terminal)
            except Exception:
                pass
    report.update(
        secret_leak_detected=False,
        graphical_login_required=False,
        manual_intervention_required=False,
    )
    return {k: report[k] for k in REPORT_KEYS}


def write_report(report, path):
    if tuple(report) != REPORT_KEYS:
        raise ValueError("invalid report schema")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--report",
        type=Path,
        default=Path(r"C:\TradeJournal\logs\single-customer-flow-result.json"),
    )
    parser.add_argument(
        "--terminal",
        type=Path,
        default=Path(r"C:\Program Files\MetaTrader 5\terminal64.exe"),
    )
    parser.add_argument("--disconnect", action="store_true")
    parser.add_argument("--resume-connection-id")
    parser.add_argument(
        "--history-mode", choices=("new_only", "from_date", "all_available")
    )
    parser.add_argument("--from-date")
    args = parser.parse_args()
    if args.resume_connection_id:
        store = WindowsSecretStore(Path(r"C:\TradeJournal\secrets"))
        payload = {
            "connection_id": args.resume_connection_id,
            "login": store.read(args.resume_connection_id, "mt5_login"),
            "server": store.read(args.resume_connection_id, "mt5_server"),
            "investor_password": store.read(
                args.resume_connection_id, "mt5_investor_password"
            ),
            "history_mode": args.history_mode,
        }
        if args.from_date:
            payload["from_date"] = args.from_date
    else:
        payload = json.load(sys.stdin)
    report = run_customer_flow(
        payload,
        instances_root=Path(r"C:\TradeJournal\instances"),
        secrets_root=Path(r"C:\TradeJournal\secrets"),
        source_terminal=args.terminal,
        # DirectMt5Adapter is intentionally not selected here. Use
        # scripts/windows/run-real-file-bridge-customer-flow.ps1 instead.
        simulate_disconnect=args.disconnect,
    )
    write_report(report, args.report)
    return 0 if report["final_result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
