"""Assigned-account maintenance and lease-bound restart-gap recovery."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .security import canonical_uuid
from .state_store import atomic_json, read_json
from .mt5_recovery_window import new_only_recovery_from
from .worker.maintenance_mt5_runtime import NativeMt5Runtime


class MaintenanceRecovery:
    def __init__(self, api: Any, instances_root: Path) -> None:
        self.api, self.instances_root = api, instances_root.resolve()
        self.connection_ids: frozenset[str] = frozenset()

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

    def runtime(self, root: Path, connection_id: str) -> NativeMt5Runtime:
        owner = self

        class RecoveringRuntime(NativeMt5Runtime):
            def resume(self, **kwargs):
                cutoff = kwargs.get("history_from") or new_only_recovery_from(self.root)
                status = super().resume(**kwargs)
                # Candidate copies must never create application history jobs.
                if self.root.parent == owner.instances_root and owner.allows(connection_id):
                    state = read_json(self.state / "instance.json")
                    response = owner.api.request("POST", "../trading-agent-maintenance", {
                        "api_version": "1", "operation": "recover",
                        "connection_id": connection_id,
                        "from_date": cutoff.isoformat(),
                        "release_id": state["template_code_manifest_sha256"],
                    })
                    if response.get("api_version") != "1":
                        raise RuntimeError("maintenance recovery response invalid")
                    canonical_uuid(response.get("job_id"))
                    progress_path = self.state / "job_progress.json"
                    progress = read_json(progress_path, {})
                    atomic_json(progress_path, {
                        **progress, "connection_id": connection_id, "status": "connected",
                        "maintenance_recovery_job_id": response["job_id"],
                    })
                return status

        return RecoveringRuntime(root, connection_id)
