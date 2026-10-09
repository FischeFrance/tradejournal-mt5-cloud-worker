"""Read ticket membership through IPC to an already-running investor terminal.

Executed only as the verified non-administrator MT5 actor. Never logs account
data, calls login/order APIs, stops a terminal, or writes its trading profile.
"""
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import MetaTrader5 as mt5
import psutil


def capture(request):
    executable = Path(request["expected_executable"]).resolve()
    pid = request["process_id"]
    created = request["creation_time_unix_ms"]

    def process_identity():
        process = psutil.Process(pid)
        if Path(process.exe()).resolve() != executable or int(process.create_time() * 1000) != created:
            raise ValueError("history_ticket_process_changed")
        matches = [p.pid for p in psutil.process_iter(["exe"])
                   if p.info.get("exe") and Path(p.info["exe"]).resolve() == executable]
        if matches != [pid]:
            raise ValueError("history_ticket_process_ambiguous")

    process_identity()
    heartbeat = json.loads((executable.parent / "MQL5/Files/TradeJournal/heartbeat.json").read_text(encoding="utf-8-sig"))
    generated = datetime.fromisoformat(heartbeat["generated_at"].replace("Z", "+00:00"))
    if not -2 <= (datetime.now(timezone.utc) - generated).total_seconds() <= 30:
        raise ValueError("history_ticket_heartbeat_stale")
    identity = heartbeat["account_identity"]

    def account_identity():
        account, terminal = mt5.account_info(), mt5.terminal_info()
        if (account is None or terminal is None or account.trade_allowed
                or not terminal.connected or str(account.login) != identity["login"]
                or account.server.casefold() != identity["server"].casefold()
                or Path(terminal.path).resolve() != executable.parent
                or Path(terminal.data_path).resolve() != executable.parent):
            raise ValueError("history_ticket_investor_identity_invalid")
        return account

    if not mt5.initialize(str(executable), timeout=5000, portable=True):
        raise ValueError("history_ticket_ipc_unavailable")
    try:
        for _ in range(3):
            before = account_identity()
            # Membership only: no conversion of broker wall-clock timestamps to UTC.
            deals = mt5.history_deals_get(0, 2147483647)
            orders = mt5.history_orders_get(0, 2147483647)
            again = mt5.history_deals_get(0, 2147483647)
            orders_again = mt5.history_orders_get(0, 2147483647)
            if any(value is None for value in (deals, orders, again, orders_again)):
                raise ValueError("history_ticket_history_unavailable")
            after = account_identity()
            process_identity()
            deal_ids, order_ids = sorted(str(row.ticket) for row in deals), sorted(str(row.ticket) for row in orders)
            if (deal_ids != sorted(str(row.ticket) for row in again)
                    or order_ids != sorted(str(row.ticket) for row in orders_again)
                    or before.balance != after.balance or before.credit != after.credit):
                time.sleep(0.1)
                continue
            if len(deal_ids) != len(set(deal_ids)) or len(order_ids) != len(set(order_ids)):
                raise ValueError("history_ticket_membership_invalid")
            return {**request, "success": True, "account_identity": identity,
                    "captured_at": datetime.now(timezone.utc).isoformat(),
                    "deal_tickets": deal_ids, "order_tickets": order_ids}
        raise ValueError("history_ticket_snapshot_unstable")
    finally:
        mt5.shutdown()


def main():
    request_path, result_path = map(Path, sys.argv[1:])
    result = {"schema_version": 1, "success": False, "error_code": "history_ticket_capture_failed"}
    try:
        if request_path.is_symlink() or not request_path.is_file():
            raise ValueError("history_ticket_request_invalid")
        request = json.loads(request_path.read_text(encoding="utf-8-sig"))
        if request.get("schema_version") != 1 or request.get("action") != "capture_tickets":
            raise ValueError("history_ticket_request_invalid")
        result = capture(request)
    except Exception:
        pass
    temporary = result_path.with_suffix(".json.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(result, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, result_path)
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
