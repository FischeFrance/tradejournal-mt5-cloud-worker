from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Callable

from .broker_endpoint_resolver import resolve_verified_broker_endpoint
from .broker_endpoint_registry import (
    BrokerEndpointRegistryPublisher,
    observe_process_endpoint,
)
from .broker_identity import (
    CachedBrokerIdentityResolver,
    OpenAIBrokerIdentityProvider,
)
from .broker_wizard import HiddenSessionBrokerWizard
from .mtapi_search import MtApiSearchClient
from .job_runner import JobRunner
from .event_supervisor import Mt5EventSupervisor
from .provisioning.mt5_instance_pool import Mt5InstancePool
from .provisioning.mt5_template import Mt5TemplateManager
from .real_handlers import (
    build_real_handlers,
    reconcile_startup_instances,
    recover_startup_instances,
)
from .realtime_wake import RealtimeWakeListener
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
    instance_pool: Mt5InstancePool | None = None
    template_manager: Mt5TemplateManager | None = None
    background_workers: tuple[
        Callable[[threading.Event], None], ...
    ] = ()
    if handlers is None:
        template_lock = threading.RLock()
        template_manager = Mt5TemplateManager(
            config.source_terminal,
            config.terminal_sha256,
            lock=template_lock,
        )
        effective_terminal_sha256 = template_manager.current_sha256
        reconciliation = reconcile_startup_instances(
            config.instances_root,
            config.secrets_root,
        )
        if reconciliation.adopted:
            logger.info(
                "adopted healthy MT5 processes at startup: %s",
                reconciliation.adopted,
            )
        if reconciliation.missing:
            logger.info(
                "MT5 processes selected for one-time local reboot recovery: %s",
                reconciliation.missing,
            )
            recovery = recover_startup_instances(
                config.instances_root,
                config.secrets_root,
                reconciliation.missing,
                config.expert_binary,
                config.expert_sha256,
            )
            if recovery.recovered:
                logger.info(
                    "recovered MT5 processes locally at startup: %s",
                    recovery.recovered,
                )
            if recovery.failed:
                logger.error(
                    "MT5 local startup recovery requires operator attention: %s",
                    recovery.failed,
                )
        if reconciliation.terminated:
            logger.warning(
                "terminated invalid or ambiguous MT5 processes at startup: %s",
                reconciliation.terminated,
            )
        if reconciliation.blocked:
            logger.error(
                "MT5 startup reconciliation requires operator attention: %s",
                reconciliation.blocked,
            )
        if config.instance_pool_target_size > 0:
            instance_pool = Mt5InstancePool(
                pool_root=config.instance_pool_root,
                instances_root=config.instances_root,
                secrets_root=config.secrets_root,
                source_terminal=config.source_terminal,
                expected_terminal_sha256=effective_terminal_sha256,
                target_size=config.instance_pool_target_size,
                max_size=config.instance_pool_max_size,
                template_lock=template_lock,
            )
            instance_pool.recover_incomplete()
            background_workers = (instance_pool.replenish_forever,)
    identity_resolver: CachedBrokerIdentityResolver | None = None
    endpoint_publisher = BrokerEndpointRegistryPublisher(
        config.broker_registry_path,
        artifact_root=config.broker_artifact_root,
        artifact_manifest=config.broker_artifact_manifest,
    )
    broker_wizard = (
        HiddenSessionBrokerWizard(
            interactive_user=config.mt5_interactive_user,
        )
        if config.broker_wizard_enabled
        else None
    )
    mtapi_search = MtApiSearchClient().search_exact if config.mtapi_search_enabled else None

    def resolve_broker_identity(server_identifier: str):
        nonlocal identity_resolver
        if identity_resolver is None:
            identity_resolver = CachedBrokerIdentityResolver(
                config.broker_identity_cache,
                OpenAIBrokerIdentityProvider(model=config.broker_identity_model),
                ttl_seconds=config.broker_identity_cache_ttl_seconds,
            )
        return identity_resolver.resolve(server_identifier)

    real_handlers = handlers or build_real_handlers(
        api,
        instances_root=config.instances_root,
        secrets_root=config.secrets_root,
        source_terminal=config.source_terminal,
        expert_binary=config.expert_binary,
        terminal_sha256=(
            template_manager.current_sha256
            if template_manager is not None
            else config.terminal_sha256
        ),
        expert_sha256=config.expert_sha256,
        trading_ingestion_url=config.trading_ingestion_url,
        endpoint_resolver=lambda broker_label, server_name: resolve_verified_broker_endpoint(
            config.broker_registry_path,
            broker_label=broker_label,
            server_name=server_name,
            artifact_root=config.broker_artifact_root,
            artifact_manifest=config.broker_artifact_manifest,
        ),
        endpoint_observer=observe_process_endpoint,
        endpoint_publisher=endpoint_publisher.publish,
        endpoint_invalidator=endpoint_publisher.invalidate,
        broker_identity_resolver=resolve_broker_identity,
        broker_wizard=broker_wizard,
        mtapi_search=mtapi_search,
        instance_pool=instance_pool,
        template_manager=template_manager,
    )
    return JobRunner(
        state_path,
        api,
        real_handlers,
        background_workers=background_workers,
    )


def build_event_supervisor(config: AgentRuntimeConfig) -> Mt5EventSupervisor:
    return Mt5EventSupervisor(
        config.instances_root,
        config.secrets_root,
        config.trading_ingestion_url,
    )


def _drain_available_jobs(runner: JobRunner, stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        try:
            if not runner.run_once():
                return
        except Exception:
            logger.exception("command drain failed unexpectedly")
            return


def run_forever(
    runner: JobRunner,
    stop_event: threading.Event,
    wake_listener: RealtimeWakeListener | None = None,
    event_supervisor: Mt5EventSupervisor | None = None,
) -> None:
    """Drain durable jobs at startup/reconnect and after a private Realtime wake-up.

    A crash-restart of the whole process leaves the
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
    background_threads = tuple(
        threading.Thread(
            target=worker,
            args=(stop_event,),
            daemon=True,
            name=f"agent-background-{index}",
        )
        for index, worker in enumerate(runner.background_workers, start=1)
    )
    for thread in background_threads:
        thread.start()
    wake_event = threading.Event()
    listener = wake_listener or RealtimeWakeListener(runner.api)
    listener_thread = threading.Thread(
        target=listener.run,
        args=(wake_event, stop_event),
        daemon=True,
        name="agent-realtime-wake",
    )
    listener_thread.start()
    supervisor_thread = None
    if event_supervisor is not None:
        supervisor_thread = threading.Thread(
            target=event_supervisor.run,
            args=(stop_event,),
            daemon=True,
            name="agent-mt5-event-supervisor",
        )
        supervisor_thread.start()
    wake_event.set()
    try:
        while not stop_event.is_set():
            if not wake_event.wait(1.0):
                continue
            wake_event.clear()
            _drain_available_jobs(runner, stop_event)
    finally:
        stop_event.set()
        listener_thread.join(timeout=5.0)
        if supervisor_thread is not None:
            supervisor_thread.join(timeout=5.0)
        for thread in background_threads:
            thread.join(timeout=1.0)


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    logging.getLogger().addFilter(RedactionFilter())
    config = load_runtime_config()
    runner = build_runner(config)
    event_supervisor = build_event_supervisor(config)
    stop_event = threading.Event()
    run_forever(runner, stop_event, event_supervisor=event_supervisor)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
