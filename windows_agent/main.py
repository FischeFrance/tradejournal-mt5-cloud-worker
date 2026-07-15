from __future__ import annotations

import argparse
from pathlib import Path

from .poc import run, write_report
from .provisioning.mt5_instance import InstanceProvisioner


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("provision", "deprovision", "poc"):
        command = sub.add_parser(name)
        command.add_argument("--connection-id", required=True)
    sub.choices["provision"].add_argument("--terminal", type=Path)
    sub.choices["poc"].add_argument(
        "--report",
        type=Path,
        default=Path(r"C:\TradeJournal\logs\demo-poc-result.json"),
    )
    args = parser.parse_args()
    provisioner = InstanceProvisioner(
        Path(r"C:\TradeJournal\instances"), Path(r"C:\TradeJournal\secrets")
    )
    if args.command == "provision":
        provisioner.provision(args.connection_id, args.terminal)
    elif args.command == "deprovision":
        provisioner.deprovision(args.connection_id)
    else:
        write_report(
            run(
                args.connection_id,
                Path(r"C:\TradeJournal\instances"),
                Path(r"C:\TradeJournal\secrets"),
            ),
            args.report,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
