"""Discover a broker-backed MT5 symbol through the terminal's local Python IPC."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import MetaTrader5 as mt5


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--terminal", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--preference", type=Path, required=True)
    args = parser.parse_args()

    preferred = args.preference.read_text(encoding="utf-8").strip()
    if not preferred or any(character in preferred for character in "\r\n"):
        return 2
    if not mt5.initialize(path=str(args.terminal), timeout=10_000):
        return 3
    selected = ""
    try:
        symbols = mt5.symbols_get() or ()
        folded = preferred.casefold()
        exact = [item for item in symbols if item.name.casefold() == folded]
        related = [item for item in symbols if folded in item.name.casefold()]
        candidates = (*exact, *related, *symbols)
        selected = next(
            (item.name for item in candidates if mt5.symbol_select(item.name, True)),
            "",
        )
    finally:
        mt5.shutdown()
    if not selected:
        return 4
    # Publish only after mt5.shutdown(): the parent treats this file as proof that the IPC helper
    # has fully exited, so it never needs to force-end a still-disconnecting scheduled task.
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(".tmp")
    temporary.write_text(json.dumps({"symbol": selected}) + "\n", encoding="utf-8")
    temporary.replace(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
