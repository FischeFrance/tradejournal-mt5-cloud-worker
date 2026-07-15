from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .provisioning.process_manager import ProcessManager

REPORT_KEYS = (
    "terminal_path",
    "terminal_started",
    "initialize_succeeded",
    "terminal_info_available",
    "last_error_code",
    "last_error_message",
    "process_cleanup_succeeded",
    "secret_leak_detected",
    "final_result",
)


def child_probe(terminal: Path, timeout_ms: int) -> int:
    import MetaTrader5 as mt5  # type: ignore[import-untyped]

    started = time.monotonic()
    ok = bool(
        mt5.initialize(str(terminal.resolve()), timeout=timeout_ms, portable=True)
    )
    error = mt5.last_error()
    info = mt5.terminal_info() if ok else None
    print(
        json.dumps(
            {
                "initialize_succeeded": ok,
                "terminal_info_available": info is not None,
                "last_error_code": int(error[0]),
                "last_error_message": str(error[1])[:160]
                .replace("\r", " ")
                .replace("\n", " "),
                "duration_seconds": round(time.monotonic() - started, 3),
            }
        ),
        flush=True,
    )
    mt5.shutdown()
    return 0 if ok else 1


def run_probe(terminal: Path, hard_timeout: int = 75) -> dict[str, Any]:
    terminal = terminal.resolve()
    before = set(ProcessManager.find(terminal))
    command = [
        sys.executable,
        "-m",
        "windows_agent.initialize_probe",
        "--child",
        "--terminal",
        str(terminal),
    ]
    data: dict[str, Any]
    child_pid: int | None = None
    try:
        child = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            cwd=Path(__file__).parents[1],
        )
        child_pid = child.pid
        stdout, _ = child.communicate(timeout=hard_timeout)
        data = json.loads(stdout.strip())
    except subprocess.TimeoutExpired:
        if child_pid is not None:
            try:
                import psutil

                parent = psutil.Process(child_pid)
                for descendant in parent.children(recursive=True):
                    descendant.kill()
                parent.kill()
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                pass
        data = {
            "initialize_succeeded": False,
            "terminal_info_available": False,
            "last_error_code": -10005,
            "last_error_message": "IPC supervisor timeout",
        }
    except (json.JSONDecodeError, OSError, ValueError):
        data = {
            "initialize_succeeded": False,
            "terminal_info_available": False,
            "last_error_code": -1,
            "last_error_message": "probe process failed",
        }
    after = set(ProcessManager.find(terminal))
    created = after - before
    cleanup = True
    if created:
        import psutil

        for pid in created:
            try:
                process = psutil.Process(pid)
                process.terminate()
                process.wait(15)
            except psutil.TimeoutExpired:
                process.kill()
                process.wait(5)
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                cleanup = False
        cleanup = cleanup and not (set(ProcessManager.find(terminal)) - before)
    report = {
        "terminal_path": str(terminal),
        "terminal_started": bool(created) or bool(after),
        "initialize_succeeded": bool(data["initialize_succeeded"]),
        "terminal_info_available": bool(data["terminal_info_available"]),
        "last_error_code": int(data["last_error_code"]),
        "last_error_message": str(data["last_error_message"]),
        "process_cleanup_succeeded": cleanup,
        "secret_leak_detected": False,
        "final_result": "PASS" if data["initialize_succeeded"] and cleanup else "FAIL",
    }
    return {key: report[key] for key in REPORT_KEYS}


def write_report(report: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--terminal", type=Path, required=True)
    parser.add_argument("--child", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if args.child:
        return child_probe(args.terminal, 60_000)
    report = run_probe(args.terminal)
    if args.report:
        write_report(report, args.report)
    else:
        print(json.dumps(report, indent=2))
    return 0 if report["final_result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
