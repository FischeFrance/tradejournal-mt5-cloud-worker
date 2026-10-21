"""Tell the control plane how each MT5 maintenance pass ended.

Every final attempt, nightly or asked for by an administrator, becomes one notification
for the administrators with what each phase did: Expert, MT5 and the final verification.
A night that never ran raises its own alert. Delivery is best effort: a report that
cannot be sent is logged and never affects maintenance or account sync.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Callable, Mapping

logger = logging.getLogger(__name__)

# The maintenance endpoint rejects bodies above 4 KiB; twelve exclusions and twelve
# verification problems stay well below it.
MAX_REPORTED_EXCLUSIONS = 12
MAX_REPORTED_ISSUES = 12
EXCLUSION_REASONS = frozenset({
    "ticket_baseline_unavailable",
    "release_state_unverifiable",
    "expert_update_failed",
})
_ERROR_CODE = re.compile(r"^[a-z0-9_]{1,64}$")
_LOCAL_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_PHASES = ("expert", "mt5", "verify")
_PHASE_CODES = ("status", "template", "reason")
_PHASE_COUNTS = ("updated", "failed", "migrated", "build", "previous_build", "checked", "healthy")


class MaintenanceReporter:
    def __init__(
        self,
        api: Any,
        recovery: Any | None = None,
        phases: Callable[[], Mapping[str, Any]] | None = None,
    ) -> None:
        self.api = api
        self.recovery = recovery
        self.phases = phases
        self._overdue_reported: set[str] = set()

    def attempt_finished(self, state: Mapping[str, Any], final: bool) -> None:
        """Report one finished attempt; ``final`` means no retry follows tonight."""
        status = state.get("status")
        local_date = state.get("scheduled_local_date")
        attempts = state.get("attempts")
        if (
            status not in ("completed", "failed")
            or not isinstance(local_date, str)
            or not _LOCAL_DATE.fullmatch(local_date)
            or not isinstance(attempts, int)
            or isinstance(attempts, bool)
        ):
            return
        error_code = None
        if status == "failed":
            raw = state.get("error_code")
            if isinstance(raw, str) and _ERROR_CODE.fullmatch(raw):
                error_code = raw
            elif state.get("window_deadline_reached") is True:
                error_code = "maintenance_window_ended"
            else:
                error_code = "mt5_maintenance_failed"
        body = {
            "api_version": "1",
            "operation": "report",
            "local_date": local_date,
            "status": status,
            "attempt": max(1, min(attempts, 10)),
            "final": bool(final),
            "error_code": error_code,
            "excluded": self._excluded(),
            "trigger": "manual" if state.get("trigger") == "manual" else "scheduled",
            "phases": self._phases(),
        }
        run_id = state.get("run_id")
        if isinstance(run_id, str) and _UUID.fullmatch(run_id):
            body["run_id"] = run_id
        self._send(body)

    def overdue(self, local_date: str, error_code: str | None) -> None:
        """Report, once per local date, a night whose maintenance never finished."""
        if not isinstance(local_date, str) or not _LOCAL_DATE.fullmatch(local_date):
            return
        if local_date in self._overdue_reported:
            return
        code = error_code if isinstance(error_code, str) and _ERROR_CODE.fullmatch(error_code) else "mt5_maintenance_overdue"
        if self._send({
            "api_version": "1",
            "operation": "report",
            "local_date": local_date,
            "status": "overdue",
            "attempt": 0,
            "final": True,
            "error_code": code,
            "excluded": [],
        }):
            self._overdue_reported.add(local_date)

    def _excluded(self) -> list[dict[str, str]]:
        reasons = getattr(self.recovery, "skip_reasons", None)
        if not isinstance(reasons, Mapping):
            return []
        items = [
            {"connection_id": connection_id, "reason": reason}
            for connection_id, reason in sorted(reasons.items())
            if isinstance(connection_id, str) and reason in EXCLUSION_REASONS
        ]
        return items[:MAX_REPORTED_EXCLUSIONS]

    def _phases(self) -> dict[str, dict[str, Any]]:
        """What each phase did, reduced to short codes and counts."""
        try:
            source = self.phases() if self.phases is not None else None
        except Exception:
            logger.exception("MT5 maintenance phases unavailable for the report")
            return {}
        if not isinstance(source, Mapping):
            return {}
        result: dict[str, dict[str, Any]] = {}
        for name in _PHASES:
            phase = source.get(name)
            if not isinstance(phase, Mapping):
                continue
            item: dict[str, Any] = {}
            for field in _PHASE_CODES:
                value = phase.get(field)
                if isinstance(value, str) and _ERROR_CODE.fullmatch(value):
                    item[field] = value
            if "status" not in item:
                continue
            for field in _PHASE_COUNTS:
                value = phase.get(field)
                if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 1_000_000:
                    item[field] = value
            issues = phase.get("issues")
            if isinstance(issues, list):
                item["issues"] = [
                    {"connection_id": issue["connection_id"], "problem": issue["problem"]}
                    for issue in issues
                    if isinstance(issue, Mapping)
                    and isinstance(issue.get("connection_id"), str)
                    and _UUID.fullmatch(issue["connection_id"])
                    and isinstance(issue.get("problem"), str)
                    and _ERROR_CODE.fullmatch(issue["problem"])
                ][:MAX_REPORTED_ISSUES]
            result[name] = item
        return result

    def _send(self, body: dict[str, Any]) -> bool:
        try:
            response = self.api.request("POST", "../trading-agent-maintenance", body)
        except Exception as exc:
            logger.warning("MT5 maintenance report not delivered (%s)", type(exc).__name__)
            return False
        if not isinstance(response, Mapping) or response.get("api_version") != "1":
            logger.warning("MT5 maintenance report was not accepted")
            return False
        return True
