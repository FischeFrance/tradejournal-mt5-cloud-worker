from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import timedelta
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
from .mt5_lifecycle import Mt5LifecycleCoordinator
from .mt5_maintenance import Mt5MaintenanceCoordinator
from .mt5_maintenance_scheduler import Mt5MaintenanceScheduler
from .provisioning.mt5_instance import InstanceProvisioner
from .provisioning.mt5_instance_pool import Mt5InstancePool
from .provisioning.mt5_instance_rotation import (
    Mt5InstanceRotationError,
    Mt5InstanceRotator,
)
from .provisioning.mt5_public_release import (
    Mt5ProvisionedReleaseInventory,
    Mt5PublicReleaseProbe,
)
from .provisioning.mt5_template import Mt5TemplateManager
from .provisioning.mt5_update_store import Mt5PendingUpdateStore
from .real_handlers import (
    build_real_handlers,
    reconcile_startup_instances,
    recover_startup_instances,
)
from .realtime_wake import RealtimeWakeListener
from .runtime_config import AgentRuntimeConfig, build_api_client, load_runtime_config
from .security import RedactionFilter, canonical_uuid
from .worker.native_mt5_runtime import (
    NativeMt5Error,
    NativeMt5Runtime,
    NativeMt5UpdateRecovery,
)

logger = logging.getLogger(__name__)

DEFAULT_STATE_PATH = Path(r"C:\TradeJournal\state\agent-job.json")
COMPONENT_STARTUP_TIMEOUT_SECONDS = 30.0

JobHandler = Callable[[dict], dict]


@dataclass(frozen=True)
class StartupLiveUpdateRecovery:
    """Credential-free startup preflight report containing only connection IDs."""

    discarded_staged: tuple[str, ...] = ()
    sealed_applied: tuple[str, ...] = ()
    restart_required: tuple[str, ...] = ()


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


def recover_startup_live_updates(
    instances_root: Path,
    *,
    runtime_factory: Callable[[Path, str], NativeMt5Runtime] = NativeMt5Runtime,
) -> StartupLiveUpdateRecovery:
    """Resolve staged LiveUpdate archives before process adoption or restart."""

    if not instances_root.exists():
        return StartupLiveUpdateRecovery()
    if (
        InstanceProvisioner._is_reparse_point(instances_root)
        or not instances_root.is_dir()
    ):
        raise NativeMt5Error("mt5_update_startup_preflight_failed")

    discarded: list[str] = []
    sealed: list[str] = []
    restart_required: list[str] = []
    failed: list[str] = []
    for entry in sorted(instances_root.iterdir(), key=lambda path: path.name):
        try:
            connection_id = canonical_uuid(entry.name)
        except ValueError:
            continue
        if (
            InstanceProvisioner._is_reparse_point(entry)
            or not entry.is_dir()
        ):
            failed.append(connection_id)
            continue
        rotation_journal = entry / "state" / "mt5-rotation.json"
        terminal = entry / "terminal" / "terminal64.exe"
        if rotation_journal.is_file() and not terminal.is_file():
            # ``old_moved`` is a valid crash point: the rotator's durable
            # backup exists while ``terminal`` is intentionally absent. The
            # first LiveUpdate scan must defer this instance so rotation
            # recovery can restore it; build_runner performs a second scan
            # immediately afterwards.
            logger.warning(
                "deferred MT5 LiveUpdate preflight until rotation recovery: %s",
                connection_id,
            )
            continue
        try:
            runtime = runtime_factory(
                entry,
                connection_id,
            )
            result: NativeMt5UpdateRecovery = (
                runtime.recover_interrupted_live_updates()
            )
            if result.pending_health:
                if not runtime.stop():
                    raise NativeMt5Error(
                        "mt5_update_startup_process_stop_failed"
                    )
                restart_required.append(connection_id)
        except Exception:
            # Never include an exception string here: PowerShell, filesystem
            # and vendor failures can carry account paths or process arguments.
            failed.append(connection_id)
            continue
        if result.discarded_staged:
            discarded.append(connection_id)
        if result.sealed_applied:
            sealed.append(connection_id)
    if failed:
        logger.error(
            "MT5 LiveUpdate startup preflight failed for: %s",
            tuple(failed),
        )
        raise NativeMt5Error("mt5_update_startup_preflight_failed")
    return StartupLiveUpdateRecovery(
        discarded_staged=tuple(discarded),
        sealed_applied=tuple(sealed),
        restart_required=tuple(restart_required),
    )


def build_runner(
    config: AgentRuntimeConfig,
    state_path: Path = DEFAULT_STATE_PATH,
    handlers: dict[str, JobHandler] | None = None,
) -> JobRunner:
    api = build_api_client(config)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    instance_pool: Mt5InstancePool | None = None
    template_manager: Mt5TemplateManager | None = None
    pending_update_store: Mt5PendingUpdateStore | None = None
    scheduled_maintenance: Mt5MaintenanceScheduler | None = None
    lifecycle_coordinator = Mt5LifecycleCoordinator()
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
        # Capture every verified LiveUpdate durably even when the scheduler is
        # administratively disabled. Disabling the 23:30 job must never fall
        # back to an immediate daytime mutation of the shared golden template.
        pending_update_store = Mt5PendingUpdateStore(
            config.mt5_maintenance_state_path.parent
            / "mt5-update-pending"
        )

        def capture_verified_update(
            bundle_root: Path,
            updater: Path,
            updater_config: Path,
            signer_subject: str,
        ) -> str:
            return pending_update_store.capture(
                bundle_root,
                updater,
                updater_config,
                signer_subject,
            ).receipt_id

        verified_update_callback = capture_verified_update
        rotator = Mt5InstanceRotator(
            instances_root=config.instances_root,
            secrets_root=config.secrets_root,
            source_terminal=config.source_terminal,
            expert_binary=config.expert_binary,
            expert_sha256=config.expert_sha256,
            lifecycle=lifecycle_coordinator,
            template_lock=template_lock,
        )
        # First reconcile the terminal bytes to which a staged LiveUpdate
        # receipt refers. A rotation rollback may otherwise replace those
        # bytes before their signer/release can be established.
        pre_rotation_live_update_recovery = recover_startup_live_updates(
            config.instances_root
        )
        rotation_recovery = rotator.recover_incomplete(
            verified_update_callback=verified_update_callback,
            verified_update_required=pending_update_store is not None,
        )
        if rotation_recovery.recovered:
            logger.warning(
                "recovered interrupted MT5 rotations at startup: %s",
                rotation_recovery.recovered,
            )
        if rotation_recovery.failed:
            raise Mt5InstanceRotationError(
                "MT5 rotation recovery requires operator attention"
            )
        # Rotation recovery may itself resume an account and either capture an
        # orphan or create a new applied receipt. Re-scan so only still-pending
        # health gates are forced through generic startup recovery below.
        live_update_recovery = recover_startup_live_updates(
            config.instances_root
        )
        discarded_staged = tuple(
            dict.fromkeys(
                pre_rotation_live_update_recovery.discarded_staged
                + live_update_recovery.discarded_staged
            )
        )
        sealed_applied = tuple(
            dict.fromkeys(
                pre_rotation_live_update_recovery.sealed_applied
                + live_update_recovery.sealed_applied
            )
        )
        if discarded_staged:
            logger.info(
                "discarded unapplied MT5 LiveUpdate archives at startup: %s",
                discarded_staged,
            )
        if sealed_applied:
            logger.warning(
                "sealed interrupted MT5 LiveUpdates at startup: %s",
                sealed_applied,
            )
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
                verified_update_callback=verified_update_callback,
                verified_update_required=pending_update_store is not None,
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
        else:
            recovery = None
        if live_update_recovery.restart_required:
            recovered = set(recovery.recovered if recovery is not None else ())
            if not set(live_update_recovery.restart_required).issubset(recovered):
                raise NativeMt5Error(
                    "mt5_update_startup_health_recovery_failed"
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
        if config.mt5_maintenance_enabled:
            public_release_probe = Mt5PublicReleaseProbe(
                config.mt5_maintenance_state_path.parent
                / "mt5-public-releases"
            )
            maintenance_coordinator = Mt5MaintenanceCoordinator(
                instances_root=config.instances_root,
                expert_binary=config.expert_binary,
                template_manager=template_manager,
                rotator=rotator,
                lifecycle=lifecycle_coordinator,
                template_lock=template_lock,
                instance_pool=instance_pool,
                pending_update_store=pending_update_store,
                public_release_probe=public_release_probe,
                public_release_inventory=(
                    Mt5ProvisionedReleaseInventory(config.instances_root)
                ),
            )
            config.mt5_maintenance_state_path.parent.mkdir(
                parents=True,
                exist_ok=True,
            )
            scheduled_maintenance = Mt5MaintenanceScheduler(
                maintenance_coordinator,
                config.mt5_maintenance_state_path,
                timezone_name=config.mt5_maintenance_timezone,
                scheduled_time=config.mt5_maintenance_local_time,
                grace_window=timedelta(
                    minutes=config.mt5_maintenance_grace_minutes
                ),
            )
            if scheduled_maintenance.recover_interrupted():
                logger.warning(
                    "recovered an interrupted MT5 maintenance schedule claim"
                )
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
        pending_update_store=pending_update_store,
        lifecycle_coordinator=lifecycle_coordinator,
    )
    return JobRunner(
        state_path,
        api,
        real_handlers,
        background_workers=background_workers,
        scheduled_maintenance=scheduled_maintenance,
        lifecycle_coordinator=lifecycle_coordinator,
    )


def build_event_supervisor(
    config: AgentRuntimeConfig,
    lifecycle_coordinator: Mt5LifecycleCoordinator | None = None,
) -> Mt5EventSupervisor:
    return Mt5EventSupervisor(
        config.instances_root,
        config.secrets_root,
        config.trading_ingestion_url,
        lifecycle_coordinator,
    )


def _drain_available_jobs(runner: JobRunner, stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        maintenance = runner.scheduled_maintenance
        if maintenance is not None and maintenance.is_due():
            return
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
    *,
    ready_event: threading.Event | None = None,
    component_startup_timeout_seconds: float = (
        COMPONENT_STARTUP_TIMEOUT_SECONDS
    ),
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
    if component_startup_timeout_seconds <= 0:
        raise ValueError("agent component startup timeout is invalid")
    component_failures: list[tuple[str, str]] = []
    component_failure_lock = threading.Lock()
    component_failed = threading.Event()
    wake_event = threading.Event()

    def run_component(
        name: str,
        target: Callable[..., None],
        args: tuple[object, ...],
    ) -> None:
        failure_type: str | None = None
        try:
            target(*args)
        except BaseException as exc:
            if not stop_event.is_set():
                failure_type = type(exc).__name__
        else:
            if not stop_event.is_set():
                failure_type = "UnexpectedReturn"
        if failure_type is not None:
            with component_failure_lock:
                component_failures.append((name, failure_type))
            component_failed.set()
            stop_event.set()
            wake_event.set()

    background_threads = tuple(
        threading.Thread(
            target=run_component,
            args=(
                f"background-{index}",
                worker,
                (stop_event,),
            ),
            daemon=True,
            name=f"agent-background-{index}",
        )
        for index, worker in enumerate(runner.background_workers, start=1)
    )
    for thread in background_threads:
        thread.start()
    listener = wake_listener or RealtimeWakeListener(runner.api)
    listener_thread = threading.Thread(
        target=run_component,
        args=(
            "realtime-wake",
            listener.run,
            (wake_event, stop_event),
        ),
        daemon=True,
        name="agent-realtime-wake",
    )
    listener_thread.start()
    supervisor_thread = None
    supervisor_ready = threading.Event()
    if event_supervisor is not None:
        supervisor_thread = threading.Thread(
            target=run_component,
            args=(
                "event-supervisor",
                event_supervisor.run,
                (stop_event, supervisor_ready),
            ),
            daemon=True,
            name="agent-mt5-event-supervisor",
        )
        supervisor_thread.start()
        deadline = time.monotonic() + component_startup_timeout_seconds
        while not supervisor_ready.wait(0.05):
            if component_failed.is_set() or stop_event.is_set():
                break
            if time.monotonic() >= deadline:
                with component_failure_lock:
                    component_failures.append(
                        ("event-supervisor", "StartupTimeout")
                    )
                component_failed.set()
                stop_event.set()
                wake_event.set()
                break
    if not component_failed.is_set() and not stop_event.is_set():
        if ready_event is not None:
            ready_event.set()
    wake_event.set()
    try:
        while not stop_event.is_set():
            maintenance = runner.scheduled_maintenance
            if maintenance is not None and maintenance.run_if_due(stop_event):
                # ``wake_event`` may have been consumed just before the daily
                # slot became due.  Keep it pending so durable jobs that were
                # already queued are drained immediately after maintenance,
                # without waiting for another Realtime broadcast.
                wake_event.set()
                continue
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
    if component_failures:
        name, failure_type = component_failures[0]
        raise RuntimeError(
            f"critical agent component failed ({name}, {failure_type})"
        ) from None


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    logging.getLogger().addFilter(RedactionFilter())
    config = load_runtime_config()
    runner = build_runner(config)
    event_supervisor = build_event_supervisor(
        config,
        runner.lifecycle_coordinator,
    )
    stop_event = threading.Event()
    run_forever(runner, stop_event, event_supervisor=event_supervisor)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
