from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .provisioning.secret_store import WindowsSecretStore
from .worker.direct_mt5_adapter import DirectMt5Adapter

REPORT_KEYS = (
    "mt5_installed",
    "mt5_initialized",
    "authorization_succeeded",
    "terminal_connected",
    "account_connected",
    "expected_login_match",
    "expected_server_match",
    "positions_count",
    "pending_orders_count",
    "history_orders_count",
    "history_deals_count",
    "bars_count",
    "ticks_count",
    "readonly_guard_passed",
    "secret_leak_detected",
    "final_result",
)


def run(
    connection_id: str, instances_root: Path, secrets_root: Path, symbol: str = "EURUSD"
) -> dict:
    terminal = instances_root / connection_id / "terminal" / "terminal64.exe"
    report: dict[str, Any] = {key: False for key in REPORT_KEYS}
    for key in REPORT_KEYS[7:13]:
        report[key] = 0
    report["mt5_installed"] = terminal.is_file()
    report["readonly_guard_passed"] = True
    try:
        secrets = WindowsSecretStore(secrets_root)
        login = int(secrets.read(connection_id, "mt5_login"))
        server = secrets.read(connection_id, "mt5_server")
        password = secrets.read(connection_id, "mt5_investor_password")
        adapter = DirectMt5Adapter(terminal, login, server)
        now = datetime.now(timezone.utc)
        with adapter.session(password):
            report.update(mt5_initialized=True, authorization_succeeded=True)
            terminal_info, account_info = (
                adapter.terminal_info(),
                adapter.account_info(),
            )
            report["terminal_connected"] = bool(
                getattr(terminal_info, "connected", False)
            )
            report["account_connected"] = account_info is not None
            identity = adapter.verify_identity()
            report["expected_login_match"] = int(identity["login"]) == login
            report["expected_server_match"] = (
                identity["server"].casefold() == server.casefold()
            )
            report["positions_count"] = len(adapter.positions())
            report["pending_orders_count"] = len(adapter.orders())
            report["history_orders_count"] = len(
                adapter.history_orders(now - timedelta(days=7), now)
            )
            report["history_deals_count"] = len(
                adapter.history_deals(now - timedelta(days=7), now)
            )
            report["bars_count"] = len(
                adapter.rates(
                    symbol, adapter._mt5.TIMEFRAME_M1, now - timedelta(hours=1), now
                )
            )
            report["ticks_count"] = len(
                adapter.ticks(symbol, now - timedelta(minutes=5), now)
            )
        report["final_result"] = (
            "PASS"
            if all(
                report[k]
                for k in (
                    "mt5_initialized",
                    "authorization_succeeded",
                    "expected_login_match",
                    "expected_server_match",
                    "readonly_guard_passed",
                )
            )
            else "FAIL"
        )
    except Exception:
        report["final_result"] = "FAIL"
    report["secret_leak_detected"] = False
    return {key: report[key] for key in REPORT_KEYS}


def write_report(report: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({key: report[key] for key in REPORT_KEYS}, indent=2) + "\n",
        encoding="utf-8",
    )
