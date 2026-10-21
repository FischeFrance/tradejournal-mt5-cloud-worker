"""Tell the control plane how each nightly MT5 maintenance attempt ended.

The server turns a final failure, an overdue night or accounts left out of the
night into an alert for the administrators. Delivery is best effort: a report
that cannot be sent is logged and never affects maintenance or account sync.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Mapping

logger = logging.getLogger(__name__)

# The maintenance endpoint rejects bodies above 2 KiB; twelve exclusions stay well below it.
MAX_REPORTED_EXCLUSIONS = 12
EXCLUSION_REASONS = frozenset({"ticket_baseline_unavailable", "release_state_unverifiable"})
_ERROR_CODE = re.compile(r"^[a-z0-9_]{1,64}$")
_LOCAL_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class MaintenanceReporter:
    def __init__(self, api: Any, recovery: Any | None = None) -> None:
        self.api = api
        self.recovery = recovery
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
        self._send({
            "api_version": "1",
            "operation": "report",
            "local_date": local_date,
            "status": status,
            "attempt": max(1, min(attempts, 10)),
            "final": bool(final),
            "error_code": error_code,
            "excluded": self._excluded(),
        })

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
