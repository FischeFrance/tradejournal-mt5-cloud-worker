from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from lab_model import LabValidationError
from network_summary import load_and_summarize


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Offline MT5 lab network-summary v2 with exclusive TCP flow "
            "accounting for sanitized JSONL."
        )
    )
    parser.add_argument("--events", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--candidate", help="JSON file containing one candidate endpoint object")
    parser.add_argument("--output", default="-")
    args = parser.parse_args(argv)
    try:
        candidate = None
        if args.candidate:
            candidate = json.loads(Path(args.candidate).read_text(encoding="utf-8"))
        summary = load_and_summarize(
            Path(args.events), run_id=args.run_id, candidate=candidate
        )
        rendered = json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if args.output == "-":
            sys.stdout.write(rendered)
        else:
            destination = Path(args.output)
            if destination.exists():
                raise LabValidationError("output already exists; overwrite refused")
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(rendered, encoding="utf-8")
        return 0
    except (OSError, json.JSONDecodeError, LabValidationError) as exc:
        sys.stderr.write(f"ERROR: {exc}\n")
        return 64


if __name__ == "__main__":
    raise SystemExit(main())
