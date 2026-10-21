"""Assigned-account maintenance and lease-bound restart-gap recovery."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .security import canonical_uuid
from .state_store import atomic_json, read_json
from .worker.maintenance_mt5_runtime import NativeMt5Runtime
from .provisioning.secret_store import WindowsSecretStore
from .maintenance_ticket_recovery import POINTER, capture_baseline, load_baseline, verify_ticket_reader
from .worker.native_mt5_runtime import NativeMt5Runtime as CurrentNativeMt5Runtime
from .worker.mql5_file_adapter import Mql5FileMt5Adapter


logger = logging.getLogger(__name__)


class MaintenanceRecoveryUnavailable(RuntimeError):
    error_code = "native_maintenance_recovery_unavailable"


class MaintenanceRecovery:
    def __init__(self, api: Any, instances_root: Path, *, secrets_root: Path | None = None) -> None:
        self.api, self.instances_root = api, instances_root.resolve()
        self.connection_ids: frozenset[str] = frozenset()
        self.skipped_connection_ids: frozenset[str] = frozenset()
        # Why each account was left out of tonight's scope, reported with the night's outcome.
        self.skip_reasons: dict[str, str] = {}
        self.secrets = WindowsSecretStore(secrets_root or self.instances_root.parent / "secrets")

    def refresh(self) -> None:
        result = self.api.request("POST", "../trading-agent-maintenance", {
            "api_version": "1", "operation": "inventory",
        })
        values = result.get("connection_ids")
        if result.get("api_version") != "1" or not isinstance(values, list) or len(values) > 512:
            raise RuntimeError("maintenance inventory response invalid")
        self.connection_ids = frozenset(canonical_uuid(value) for value in values)

    def allows(self, connection_id: str) -> bool:
        return connection_id in self.connection_ids

    def refresh_for_rotation(self) -> None:
        self.refresh()
        self.skipped_connection_ids = frozenset()
        self.skip_reasons = {}
        if not self.connection_ids:
            return
        if CurrentNativeMt5Runtime._setting("TRADEJOURNAL_MT5_TICKET_RECOVERY_ENABLED") != "1":
            raise MaintenanceRecoveryUnavailable("native_maintenance_recovery_unavailable")
        try:
            verify_ticket_reader()
        except Exception as exc:
            raise MaintenanceRecoveryUnavailable("native_maintenance_recovery_unavailable") from exc
        # Every terminal that takes part in tonight's rotation must first produce a baseline, so
        # none is ever stopped without one. A prop firm can disable an account during the day
        # (daily loss limit, expired challenge): its terminal can no longer log in or capture.
        # That account alone stays out of tonight's scope, which every maintenance step reads
        # through allows(), and the rest of the fleet is still maintained. When no account can
        # capture, the cause is shared (session, reader, control plane) and the run stops.
        ready: set[str] = set()
        failures: dict[str, Exception] = {}
        for cid in sorted(self.connection_ids):
            try:
                root = self.instances_root / cid
                pointer = read_json(root / "state" / POINTER, {})
                if pointer and pointer.get("status") != "prepared":
                    raise ValueError("maintenance_ticket_recovery_pending")
                login, server = self.identity(cid)
                capture_baseline(root, cid, login, server)
            except Exception as exc:
                failures[cid] = exc
                logger.warning(
                    "MT5 maintenance excludes %s from tonight's rotation: ticket baseline unavailable (%s: %s)",
                    cid,
                    type(exc).__name__,
                    str(exc)[:200],
                )
            else:
                ready.add(cid)
        if not ready:
            raise MaintenanceRecoveryUnavailable(
                "native_maintenance_recovery_unavailable"
            ) from next(iter(failures.values()))
        self.connection_ids = frozenset(ready)
        self.skipped_connection_ids = frozenset(failures)
        self.skip_reasons = {cid: "ticket_baseline_unavailable" for cid in failures}

    def exclude(
        self,
        connection_ids: frozenset[str],
        reason: str = "release_state_unverifiable",
    ) -> None:
        """Remove accounts from tonight's scope after a later preflight check."""
        excluded = frozenset(connection_ids) & self.connection_ids
        self.connection_ids = self.connection_ids - excluded
        self.skipped_connection_ids = self.skipped_connection_ids | excluded
        self.skip_reasons.update({cid: reason for cid in excluded})

    def identity(self, connection_id: str) -> tuple[int, str]:
        return int(self.secrets.read(connection_id, "mt5_login")), self.secrets.read(connection_id, "mt5_server")

    def runtime(self, root: Path, connection_id: str) -> NativeMt5Runtime:
        owner = self

        class RecoveringRuntime(NativeMt5Runtime):
            def published(self):
                return self.root.parent == owner.instances_root and owner.allows(connection_id)

            def stop(self, timeout=15.0):
                if self.published():
                    login, server = owner.identity(connection_id)
                    load_baseline(self.root, connection_id, login, server)
                    progress_path = self.state / "job_progress.json"
                    atomic_json(progress_path, {**read_json(progress_path, {}),
                        "connection_id": connection_id, "status": "maintenance_restarting"})
                return super().stop(timeout)

            def resume(self, **kwargs):
                pointer = None
                if self.published():
                    login, server = owner.identity(connection_id)
                    baseline = load_baseline(self.root, connection_id, login, server)
                    pointer = read_json(self.state / POINTER)
                    # The frozen complete ledger is consumed only by the leased
                    # delta job. The live supervisor refuses historical producers.
                    kwargs = {**kwargs, "history_mode": "all_available", "history_from": None}
                status = super().resume(**kwargs)
                # Candidate copies must never create application history jobs.
                if pointer is not None:
                    state = read_json(self.state / "instance.json")
                    atomic_json(self.state / POINTER, {**pointer, "status": "queue_unconfirmed"})
                    WindowsSecretStore.restrict_acl(self.state / POINTER)
                    try:
                        response = owner.api.request("POST", "../trading-agent-maintenance", {
                            "api_version": "1", "operation": "recover",
                            "connection_id": connection_id,
                            "from_date": baseline["captured_at"],
                            "release_id": state["template_code_manifest_sha256"],
                            "baseline_sha256": pointer["baseline_sha256"],
                        })
                        if response.get("api_version") != "1" or response.get("status") not in ("pending", "running"):
                            raise RuntimeError("maintenance recovery response invalid")
                        canonical_uuid(response.get("job_id"))
                    except Exception:
                        # Preserve the unrecovered cursor, but keep the healthy
                        # investor producer live if the control plane is unavailable.
                        CurrentNativeMt5Runtime(self.root, connection_id).switch_to_new_only()
                        live = Mql5FileMt5Adapter(self.files, connection_id, login, server, self.state)
                        live.verify_identity()
                        if live.account_info().trade_allowed:
                            raise RuntimeError("maintenance investor verification failed")
                        progress_path = self.state / "job_progress.json"
                        atomic_json(progress_path, {**read_json(progress_path, {}),
                            "connection_id": connection_id, "status": "connected"})
                        raise
                    progress_path = self.state / "job_progress.json"
                    progress = read_json(progress_path, {})
                    atomic_json(progress_path, {
                        **progress, "connection_id": connection_id, "status": "recovering_history",
                        "maintenance_recovery_job_id": response["job_id"],
                    })
                    atomic_json(self.state / POINTER, {**pointer, "status": "queued", "job_id": response["job_id"]})
                    WindowsSecretStore.restrict_acl(self.state / POINTER)
                return status

        return RecoveringRuntime(root, connection_id)
