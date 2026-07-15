from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .agent_secrets import AGENT_SCOPE_ID, AGENT_TOKEN_SECRET_NAME, PROVISIONING_KEY_SECRET_NAME
from .provisioning.secret_store import WindowsSecretStore

ALLOWED_NAMES = (AGENT_TOKEN_SECRET_NAME, PROVISIONING_KEY_SECRET_NAME)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True, choices=ALLOWED_NAMES)
    parser.add_argument(
        "--secrets-root", type=Path, default=Path(r"C:\TradeJournal\secrets")
    )
    args = parser.parse_args()

    value = sys.stdin.readline().rstrip("\r\n")
    if not value:
        raise ValueError("empty secret value")

    store = WindowsSecretStore(args.secrets_root)
    store.write(AGENT_SCOPE_ID, args.name, value)

    # Verify the value round-trips through DPAPI without ever printing it.
    readback = store.read(AGENT_SCOPE_ID, args.name)
    if readback != value:
        raise RuntimeError("stored secret failed verification read-back")
    print(f"{args.name} stored and verified ({len(readback)} chars). Value was not displayed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
