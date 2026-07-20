from __future__ import annotations

import logging
import random
import threading
from pathlib import Path
from typing import Callable

from .job_runner import JobRunner
from .real_handlers import build_real_handlers, sweep_stale_instances
from .runtime_config import AgentRuntimeConfig, build_api_client, load_runtime_config
from .security import RedactionFilter

logger = logging.getLogger(__name__)

DEFAULT_STATE_PATH = Path(r"C:\TradeJournal\state\agent-job.json")

JobHandler = Callable[[dict], dict]


def _not_implemented(job_type: str) -> JobHandler:
    def handler(job: dict) -> dict:
        raise NotImplementedError(
            f"{job_type} handler not yet wired to the real MT5 pipeline (see CONTROL-PLANE-NEXT-STEPS.txt)"
        )

    return handler


def default_handlers() -> dict[str, JobHandler]:
    """Explicit no-op handlers for isolated unit tests only.

    ``build_runner()`` never calls this helper: its normal path wires the real native MQL5 file
    bridge handlers. Keeping this function avoids accidentally using a production API in a test.
    """
    return {
        "provision": _not_implemented("provision"),
        "deprovision": _not_implemented("deprovision"),
        "historical_sync": _not_implemented("historical_sync"),
        "live_sync": _not_implemented("live_sync"),
    }


def build_runner(
    config: AgentRuntimeConfig,
    state_path: Path = DEFAULT_STATE_PATH,
    handlers: dict[str, JobHandler] | None = None,
) -> JobRunner:
    api = build_api_client(config)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    if handlers is None:
        swept = sweep_stale_instances(config.instances_root)
        if swept:
            logger.warning("terminated orphaned MT5 processes at startup: %s", swept)
    real_handlers = handlers or build_real_handlers(
        api,
        instances_root=config.instances_root,
        secrets_root=config.secrets_root,
        source_terminal=config.source_terminal,
        expert_binary=config.expert_binary,
        trading_ingestion_url=config.trading_ingestion_url,
    )
    return JobRunner(state_path, api, real_handlers)


def run_forever(runner: JobRunner, poll_seconds: float, stop_event: threading.Event) -> None:
    """Polls run_once() until stop_event is set. A crash-restart of the whole process leaves the
    previous job's lease to expire and be reclaimed server-side (see claim_mt5_provisioning_job's
    reclaim-on-expiry logic) -- recover() only logs the leftover state for operator visibility,
    it does not attempt to resume the job locally (the contract has no "get job status" route to
    safely reconcile against)."""
    leftover = runner.recover()
    if leftover.get("status") in ("running", "claimed"):
        logger.warning(
            "leftover job state from a previous run: job_id=%s status=%s -- its lease will "
            "expire and be reclaimed automatically, no local resume is attempted",
            leftover.get("job_id"),
            leftover.get("status"),
        )
    while not stop_event.is_set():
        try:
            claimed = runner.run_once()
        except Exception:
            logger.exception("run_once failed unexpectedly")
            claimed = False
        if not claimed:
            jitter = poll_seconds + random.uniform(0, poll_seconds * 0.25)
            stop_event.wait(jitter)


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    logging.getLogger().addFilter(RedactionFilter())
    config = load_runtime_config()
    runner = build_runner(config)
    stop_event = threading.Event()
    run_forever(runner, config.poll_seconds, stop_event)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
