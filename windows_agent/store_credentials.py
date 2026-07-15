from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .provisioning.secret_store import WindowsSecretStore


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--connection-id", required=True)
    args = parser.parse_args()
    payload = json.load(sys.stdin)
    if set(payload) != {"login", "server", "investor_password"}:
        raise ValueError("invalid credential input")
    store = WindowsSecretStore(Path(r"C:\TradeJournal\secrets"))
    store.write(args.connection_id, "mt5_login", str(int(payload["login"])))
    store.write(args.connection_id, "mt5_server", str(payload["server"]))
    store.write(
        args.connection_id, "mt5_investor_password", str(payload["investor_password"])
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
