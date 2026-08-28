"""LocalSystem-only entrypoint for one isolated MT5 LiveUpdate probe.

The operator wrapper stops the Agent service before invoking this module, so
the normal in-process lifecycle lock has no cross-process competitor.  This
entrypoint deliberately builds no instance pool and calls only the targeted
canary path: a verified vendor update may be captured as pending evidence, but
the golden template and the rest of the fleet are never promoted or rotated.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from .deploy_guard import (
    RELEASE_ROOT,
    _assert_local_system,
    _effective_environment,
    _environment_map,
    _get_service_environment,
    _service_status,
)
from .interactive_identity import verify_interactive_task_identity
from .mt5_lifecycle import Mt5LifecycleCoordinator
from .mt5_maintenance import Mt5MaintenanceCoordinator
from .provisioning.mt5_instance_rotation import Mt5InstanceRotator
from .provisioning.mt5_public_release import (
    Mt5ProvisionedReleaseInventory,
    Mt5PublicReleaseProbe,
)
from .provisioning.mt5_template import Mt5TemplateManager
from .provisioning.mt5_update_store import Mt5PendingUpdateStore
from .provisioning.secret_store import WindowsSecretStore
from .release_manifest import verify_release
from .runtime_config import AgentRuntimeConfig, load_runtime_config
from .security import canonical_uuid
from .state_store import atomic_json


logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
RESULT_ROOT = Path(r"C:\TradeJournal\state\mt5-adhoc-results")
FPM_TEST_CONNECTION_ID = "2f1647b4-035e-41be-b634-0cf785a70b07"
FPM_TEST_SERVER = "FPMTrading-Live"
_REVISION = re.compile(r"[0-9a-f]{40}")
_SERVER = re.compile(r"[A-Za-z0-9._ -]{1,128}")


class Mt5AdHocProbeError(RuntimeError):
    """A stable operator-facing failure code without credentials or paths."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _release_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _validate_release(revision: str) -> Path:
    if not isinstance(revision, str) or _REVISION.fullmatch(revision) is None:
        raise Mt5AdHocProbeError("source_revision_invalid")
    root = _release_root()
    expected = RELEASE_ROOT / f"agent-{revision[:12]}"
    try:
        if root != expected.resolve(strict=True):
            raise Mt5AdHocProbeError("release_path_invalid")
        manifest = verify_release(root)
    except Mt5AdHocProbeError:
        raise
    except Exception as exc:
        raise Mt5AdHocProbeError("release_verification_failed") from exc
    if manifest.get("source_revision") != revision:
        raise Mt5AdHocProbeError("release_revision_mismatch")
    return root


def _load_service_config() -> AgentRuntimeConfig:
    try:
        _entries, overrides = _environment_map(_get_service_environment())
        return load_runtime_config(_effective_environment(overrides))
    except Exception as exc:
        raise Mt5AdHocProbeError("service_configuration_invalid") from exc


def _assert_service_stopped() -> None:
    try:
        status, process_id = _service_status()
    except Exception as exc:
        raise Mt5AdHocProbeError("service_status_unavailable") from exc
    if status != "stopped" or process_id != 0:
        raise Mt5AdHocProbeError("agent_service_must_be_stopped")


def _assert_outside_maintenance_window(
    config: AgentRuntimeConfig,
    *,
    clock: Callable[[], datetime] | None = None,
) -> None:
    """Reject an ad-hoc start during the configured nightly maintenance slot."""

    try:
        observed = (clock or (lambda: datetime.now(timezone.utc)))()
        scheduled_time = config.mt5_maintenance_local_time
        timezone_name = config.mt5_maintenance_timezone
        grace_minutes = config.mt5_maintenance_grace_minutes
        if (
            not isinstance(observed, datetime)
            or observed.tzinfo is None
            or not isinstance(timezone_name, str)
            or not timezone_name.strip()
            or scheduled_time.tzinfo is not None
            or type(grace_minutes) is not int
            or not 5 <= grace_minutes <= 12 * 60
        ):
            raise ValueError("invalid maintenance window")
        local_timezone = ZoneInfo(timezone_name.strip())
        local_now = observed.astimezone(local_timezone)
        scheduled = datetime.combine(
            local_now.date(),
            scheduled_time,
            tzinfo=local_timezone,
        )
        if local_now < scheduled:
            scheduled = datetime.combine(
                local_now.date() - timedelta(days=1),
                scheduled_time,
                tzinfo=local_timezone,
            )
    except Exception as exc:
        raise Mt5AdHocProbeError("maintenance_window_invalid") from exc
    if scheduled <= local_now <= scheduled + timedelta(minutes=grace_minutes):
        raise Mt5AdHocProbeError("maintenance_window_active")


def _build_coordinator(config: AgentRuntimeConfig) -> Mt5MaintenanceCoordinator:
    lock = threading.RLock()
    lifecycle = Mt5LifecycleCoordinator()
    manager = Mt5TemplateManager(
        config.source_terminal,
        config.terminal_sha256,
        lock=lock,
    )
    try:
        manager.validate_current_quiesced()
        verify_interactive_task_identity(config.mt5_interactive_user)
    except Exception as exc:
        raise Mt5AdHocProbeError("mt5_adhoc_preflight_failed") from exc
    pending = Mt5PendingUpdateStore(
        config.mt5_maintenance_state_path.parent / "mt5-update-pending"
    )
    public_probe = Mt5PublicReleaseProbe(
        config.mt5_maintenance_state_path.parent / "mt5-public-releases"
    )
    public_inventory = Mt5ProvisionedReleaseInventory(
        config.instances_root
    )
    rotator = Mt5InstanceRotator(
        instances_root=config.instances_root,
        secrets_root=config.secrets_root,
        source_terminal=config.source_terminal,
        expert_binary=config.expert_binary,
        expert_sha256=config.expert_sha256,
        lifecycle=lifecycle,
        template_lock=lock,
    )
    return Mt5MaintenanceCoordinator(
        instances_root=config.instances_root,
        expert_binary=config.expert_binary,
        template_manager=manager,
        rotator=rotator,
        lifecycle=lifecycle,
        template_lock=lock,
        instance_pool=None,
        pending_update_store=pending,
        public_release_probe=public_probe,
        public_release_inventory=public_inventory,
    )


def _result_path(nonce: str) -> Path:
    try:
        nonce = canonical_uuid(nonce)
    except (TypeError, ValueError) as exc:
        raise Mt5AdHocProbeError("request_nonce_invalid") from exc
    return RESULT_ROOT / f"{nonce}.json"


def _write_result(path: Path, document: dict[str, object]) -> None:
    if path.exists():
        raise Mt5AdHocProbeError("result_already_exists")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        WindowsSecretStore.restrict_shared_service_acl(path.parent)
        atomic_json(path, document)
        WindowsSecretStore.restrict_shared_service_acl(path)
    except Mt5AdHocProbeError:
        raise
    except Exception as exc:
        raise Mt5AdHocProbeError("result_write_failed") from exc


def run_probe(
    *,
    revision: str,
    nonce: str,
    connection_id: str,
    expected_server: str,
) -> dict[str, object]:
    """Run one exact canary and persist no shared promotion side effects."""

    _assert_local_system()
    _assert_service_stopped()
    _validate_release(revision)
    try:
        connection_id = canonical_uuid(connection_id)
    except (TypeError, ValueError) as exc:
        raise Mt5AdHocProbeError("connection_id_invalid") from exc
    if (
        not isinstance(expected_server, str)
        or _SERVER.fullmatch(expected_server) is None
        or expected_server != expected_server.strip()
    ):
        raise Mt5AdHocProbeError("expected_server_invalid")
    # Temporary rollout safety rail: while this implementation is under test,
    # the privileged one-shot entrypoint is physically unable to touch another
    # account.  The ordinary nightly coordinator remains fleet-wide.
    if (
        connection_id != FPM_TEST_CONNECTION_ID
        or expected_server != FPM_TEST_SERVER
    ):
        raise Mt5AdHocProbeError("canary_scope_restricted")

    config = _load_service_config()
    _assert_outside_maintenance_window(config)
    coordinator = _build_coordinator(config)
    report = coordinator.run_public_canary_only(
        connection_id,
        expected_server,
        threading.Event(),
    )
    return {
        "connection_id": report.connection_id,
        "server": report.server,
        "update_captured": report.update_captured,
        "pending_update_receipt_ids": list(
            report.pending_update_receipt_ids
        ),
        "public_build": report.public_build,
        "observed_build_before": report.observed_build_before,
        "observed_build_after": report.observed_build_after,
        "classification_before": report.classification_before,
        "classification_after": report.classification_after,
        "updated": report.updated,
        "inventory_counts": {
            name: count for name, count in report.inventory_counts
        },
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Probe exactly one MT5 account for a verified vendor update."
    )
    parser.add_argument("--revision", required=True)
    parser.add_argument("--nonce", required=True)
    parser.add_argument("--connection-id", required=True)
    parser.add_argument("--expected-server", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result_path = _result_path(args.nonce)
    except Mt5AdHocProbeError:
        return 2
    base: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "nonce": args.nonce,
        "source_revision": args.revision,
        "mode": "canary_only",
        "connection_id": args.connection_id,
        "expected_server": args.expected_server,
        "finished_at_unix_ms": 0,
    }
    exit_code = 1
    try:
        details = run_probe(
            revision=args.revision,
            nonce=args.nonce,
            connection_id=args.connection_id,
            expected_server=args.expected_server,
        )
        document = {
            **base,
            "success": True,
            "code": "ok",
            "details": details,
        }
        exit_code = 0
    except Exception as exc:
        logger.error(
            "isolated MT5 ad-hoc probe failed "
            "(connection=%s, failure_type=%s)",
            args.connection_id,
            type(exc).__name__,
        )
        document = {
            **base,
            "success": False,
            "code": "mt5_adhoc_probe_failed",
            "details": {},
        }
    document["finished_at_unix_ms"] = int(time.time() * 1000)
    try:
        _write_result(result_path, document)
    except Exception as exc:
        logger.error(
            "isolated MT5 ad-hoc result publication failed "
            "(failure_type=%s)",
            type(exc).__name__,
        )
        return 2
    return exit_code


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(main(sys.argv[1:]))
