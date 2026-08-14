"""No-login smoke test for the native, read-only MQL5 file bridge."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from uuid import uuid4

from .worker.native_mt5_runtime import NativeMt5Runtime


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-terminal", type=Path, required=True)
    parser.add_argument("--expert", type=Path, required=True)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args()
    root = args.work_root / f"heartbeat-{uuid4()}"
    result = {"terminal_started": False, "ea_loaded": False, "heartbeat_received": False, "json_valid": False, "credentials_used": False, "final_result": "FAIL"}
    runtime: NativeMt5Runtime | None = None
    try:
        if not args.source_terminal.is_file() or not args.expert.is_file():
            raise ValueError("source terminal or expert missing")
        shutil.copytree(args.source_terminal.parent, root / "terminal")
        runtime = NativeMt5Runtime(root, str(uuid4()))
        status = runtime.start_no_login(expert_binary=args.expert, timeout=args.timeout)
        result.update(terminal_started=True, ea_loaded=True, heartbeat_received=True)
        raw = json.loads((status.files_path / "heartbeat.json").read_text(encoding="utf-8-sig"))
        result["json_valid"] = bool(
            raw.get("schema_version") == 1
            and isinstance(raw.get("generated_at"), str)
            and isinstance(raw.get("sequence"), int)
            and isinstance(raw.get("account_identity"), dict)
            and isinstance(raw.get("server_identity"), str)
            and isinstance(raw.get("payload"), dict)
        )
        result["final_result"] = "PASS" if result["json_valid"] else "FAIL"
    except Exception as exc:
        result["error_code"] = str(exc)[:80]
    finally:
        if runtime is not None:
            runtime.stop()
        shutil.rmtree(root, ignore_errors=True)
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return 0 if result["final_result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
