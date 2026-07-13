"""CLI locale del provisioning agent; nessun secret viene accettato come argomento."""

from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

from .config import load_config
from .engine import ProvisioningEngine
from .filesystem_queue import FilesystemQueue
from .models import Action
from .validation import ValidationError, load_job, validate_uuid


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m provisioning.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("validate-job", "provision", "enqueue"):
        child = sub.add_parser(command)
        child.add_argument("job", type=Path)
    for command in ("start", "stop", "restart", "status", "deprovision"):
        child = sub.add_parser(command)
        child.add_argument("connection_id")
    sub.add_parser("run-filesystem-agent")
    return parser


def _print_json(payload: Dict[str, Any]) -> None:
    print(json.dumps(payload, sort_keys=True))


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    config = load_config()
    try:
        if args.command == "validate-job":
            job = load_job(args.job, allow_insecure_http=config.allow_insecure_http)
            _print_json({"valid": True, "job": job.to_dict()})
            return 0

        engine = ProvisioningEngine(config)
        if args.command == "provision":
            job = load_job(args.job, allow_insecure_http=config.allow_insecure_http)
            if job.action is not Action.PROVISION:
                raise ValidationError("Il comando provision richiede un job con action=provision.")
            _print_json(engine.provision(job))
            return 0
        if args.command == "enqueue":
            job = load_job(args.job, allow_insecure_http=config.allow_insecure_http)
            queue = FilesystemQueue(
                config.queue_root,
                engine,
                poll_seconds=config.filesystem_poll_seconds,
            )
            queue.enqueue(job)
            _print_json({"enqueued": True, "job_id": job.job_id})
            return 0
        if args.command in {"start", "stop", "restart", "status", "deprovision"}:
            connection_id = validate_uuid(args.connection_id, "connection_id")
            operation = getattr(engine, args.command)
            _print_json(operation(connection_id))
            return 0
        if args.command == "run-filesystem-agent":
            shutdown = threading.Event()

            def _request_shutdown(_signum, _frame) -> None:
                shutdown.set()

            signal.signal(signal.SIGINT, _request_shutdown)
            signal.signal(signal.SIGTERM, _request_shutdown)
            queue = FilesystemQueue(
                config.queue_root,
                engine,
                poll_seconds=config.filesystem_poll_seconds,
            )
            queue.run_forever(shutdown)
            return 0
        raise AssertionError(f"Comando non gestito: {args.command}")
    except Exception as exc:
        # I secret non entrano nei job/argomenti e le implementazioni sottostanti non includono
        # contenuti secret nei messaggi. Niente traceback nella CLI di produzione.
        print(f"provisioning error ({type(exc).__name__}): {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
