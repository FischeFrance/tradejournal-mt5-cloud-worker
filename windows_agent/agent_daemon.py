from __future__ import annotations

import logging
import os
import random
import threading
from datetime import timedelta
from pathlib import Path
from typing import Callable

from .broker_endpoint_registry import (
    BrokerEndpointRegistryPublisher,
    observe_process_endpoint,
)
from .broker_endpoint_resolver import resolve_verified_broker_endpoint
from .broker_identity import (
    CachedBrokerIdentityResolver,
    OpenAIBrokerIdentityProvider,
)
from .broker_wizard import HiddenSessionBrokerWizard
from .event_supervisor import Mt5EventSupervisor
from .job_runner import JobRunner
from .maintenance_recovery import MaintenanceRecovery
from .mt5_lifecycle import Mt5LifecycleCoordinator
from .mt5_maintenance import Mt5MaintenanceCoordinator
from .mt5_maintenance_scheduler import Mt5MaintenanceScheduler
from .mtapi_search import MtApiSearchClient
from .observability.maintenance_health import Mt5MaintenanceHealthMonitor
from .provisioning.mt5_instance_pool import Mt5InstancePool
from .provisioning.mt5_instance_rotation import Mt5InstanceRotator
from .provisioning.mt5_public_release import Mt5ProvisionedReleaseInventory, Mt5PublicReleaseProbe
from .provisioning.mt5_template import Mt5TemplateManager
from .provisioning.mt5_update_store import Mt5PendingUpdateStore
from .real_handlers import build_real_handlers, reconcile_startup_instances
from .runtime_config import AgentRuntimeConfig, build_api_client, load_runtime_config
from .security import RedactionFilter
from .worker.native_mt5_runtime import NativeMt5Runtime

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
    scheduled_maintenance: Mt5MaintenanceScheduler | None = None
    template_lock = threading.RLock()
    background_workers: tuple[
        Callable[[threading.Event], None], ...
    ] = ()
    if handlers is None:
        if config.mt5_maintenance_enabled:
            template_manager = Mt5TemplateManager(
                config.source_terminal, config.terminal_sha256, lock=template_lock,
            )
            template_manager.recover_interrupted_rotation()
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
                "MT5 processes awaiting live-sync reboot recovery: %s",
                reconciliation.missing,
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
                expected_terminal_sha256=(template_manager.current_sha256 if template_manager else config.terminal_sha256),
                target_size=config.instance_pool_target_size,
                max_size=config.instance_pool_max_size,
                template_lock=template_lock,
            )
            instance_pool.recover_incomplete()
            background_workers = (instance_pool.replenish_forever,)
        if config.mt5_maintenance_enabled:
            recovery = MaintenanceRecovery(api, config.instances_root, secrets_root=config.secrets_root)
            pending_update_store = Mt5PendingUpdateStore(
                config.mt5_maintenance_state_path.parent / "mt5-update-pending",
            )
            rotator = Mt5InstanceRotator(
                instances_root=config.instances_root,
                secrets_root=config.secrets_root,
                source_terminal=config.source_terminal,
                expert_binary=config.expert_binary,
                expert_sha256=config.expert_sha256,
                lifecycle=Mt5LifecycleCoordinator(),
                template_lock=template_lock,
                runtime_factory=recovery.runtime,
                connection_allowed=recovery.allows,
            )
            maintenance_coordinator = Mt5MaintenanceCoordinator(
                instances_root=config.instances_root,
                expert_binary=config.expert_binary,
                template_manager=template_manager,
                rotator=rotator,
                lifecycle=rotator.lifecycle,
                template_lock=template_lock,
                instance_pool=instance_pool,
                runtime_factory=recovery.runtime,
                scope_refresh=recovery.refresh_for_rotation,
                connection_allowed=recovery.allows,
                pending_update_store=pending_update_store,
                public_release_probe=Mt5PublicReleaseProbe(
                    config.mt5_maintenance_state_path.parent / "mt5-public-releases",
                ),
                public_release_inventory=Mt5ProvisionedReleaseInventory(
                    config.instances_root, trusted_template_root=config.source_terminal.parent,
                    connection_allowed=recovery.allows,
                ),
            )
            scheduled_maintenance = Mt5MaintenanceScheduler(
                maintenance_coordinator,
                config.mt5_maintenance_state_path,
                timezone_name=config.mt5_maintenance_timezone,
                scheduled_time=config.mt5_maintenance_local_time,
                grace_window=timedelta(minutes=config.mt5_maintenance_grace_minutes),
            )
            scheduled_maintenance.recover_interrupted()
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
        runtime_factory=lambda root, connection_id: NativeMt5Runtime(
            root, connection_id, single_start_enabled=config.mt5_single_start_enabled,
            bootstrap_symbol_cache=config.instances_root.parent / "state" / "broker-bootstrap-symbols",
        ),
        terminal_sha256=config.terminal_sha256,
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
    if handlers is None:
        event_supervisor = Mt5EventSupervisor(
            config.instances_root,
            config.secrets_root,
            config.trading_ingestion_url,
        )
        background_workers = (*background_workers, event_supervisor.run)
        if os.environ.get("TRADEJOURNAL_MT5_MAINTENANCE_ENABLED"):
            maintenance_health = Mt5MaintenanceHealthMonitor(Path(__file__).resolve().parent.parent)
            background_workers = (*background_workers, maintenance_health.run)
    return JobRunner(
        state_path,
        api,
        real_handlers,
        background_workers=background_workers,
        scheduled_maintenance=scheduled_maintenance,
    )


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
    workers = tuple(
        threading.Thread(
            target=worker,
            args=(stop_event,),
            daemon=True,
            name=f"agent-background-{index}",
        )
        for index, worker in enumerate(runner.background_workers, start=1)
    )
    for thread in workers:
        thread.start()
    try:
        while not stop_event.is_set():
            try:
                if runner.scheduled_maintenance is not None and runner.scheduled_maintenance.run_if_due(stop_event):
                    continue
                claimed = runner.run_once()
            except Exception:
                logger.exception("run_once failed unexpectedly")
                claimed = False
            if not claimed:
                jitter = poll_seconds + random.uniform(0, poll_seconds * 0.25)
                stop_event.wait(jitter)
    finally:
        stop_event.set()
        for thread in workers:
            thread.join(timeout=1.0)


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    root_logger = logging.getLogger()
    root_logger.addFilter(RedactionFilter())
    # Logger filters are not applied to records propagated by children such as httpx; handler
    # filters are. Protect console execution with the same boundary as the Windows service.
    for handler in root_logger.handlers:
        handler.addFilter(RedactionFilter())
    config = load_runtime_config()
    runner = build_runner(config)
    stop_event = threading.Event()
    run_forever(runner, config.poll_seconds, stop_event)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
