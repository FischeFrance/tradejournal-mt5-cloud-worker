"""Observe nightly maintenance independently from account/bridge liveness."""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping
from zoneinfo import ZoneInfo

from ..maintenance_capability import (
    MAINTENANCE_ENV,
    MaintenanceCapabilityError,
    verify_maintenance_capability,
)
from ..state_store import atomic_json, read_json


logger = logging.getLogger(__name__)
DEFAULT_STATE = Path(r"C:\TradeJournal\state\mt5-maintenance.json")


def evaluate_maintenance_health(
    release_root: Path,
    environment: Mapping[str, str],
    *,
    now: datetime | None = None,
) -> dict[str, object]:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("maintenance health clock must be timezone-aware")
    result: dict[str, object] = {
        "schema_version": 1,
        "observed_at_utc": now.astimezone(timezone.utc).isoformat(),
        "status": "disabled",
    }
    enabled = environment.get(MAINTENANCE_ENV, "").strip()
    if enabled not in ("", "0", "1"):
        return {**result, "status": "invalid_config", "error_code": "mt5_maintenance_config_invalid"}
    if enabled != "1":
        return result
    try:
        verify_maintenance_capability(release_root)
    except MaintenanceCapabilityError as exc:
        return {**result, "status": "unsupported", "error_code": str(exc)}
    try:
        zone_name = environment.get("TRADEJOURNAL_MT5_MAINTENANCE_TIMEZONE", "Europe/Rome")
        zone = ZoneInfo(zone_name)
        clock = environment.get("TRADEJOURNAL_MT5_MAINTENANCE_LOCAL_TIME", "23:30")
        parsed = datetime.strptime(clock, "%H:%M").time()
        if parsed.strftime("%H:%M") != clock:
            raise ValueError("invalid scheduled time")
        grace = int(environment.get("TRADEJOURNAL_MT5_MAINTENANCE_GRACE_MINUTES", "120"))
        if not 5 <= grace <= 720:
            raise ValueError("invalid grace window")
        local_now = now.astimezone(zone)
        expected_date = local_now.date()
        while datetime.combine(expected_date, parsed, zone) + timedelta(minutes=grace) > local_now:
            expected_date -= timedelta(days=1)
        result.update({"timezone": zone_name, "expected_completed_local_date": expected_date.isoformat()})
        state_path = Path(environment.get("TRADEJOURNAL_MT5_MAINTENANCE_STATE_PATH", str(DEFAULT_STATE)))
        if state_path.is_symlink():
            raise ValueError("invalid state path")
        state = read_json(state_path, {})
        if not state:
            return {**result, "status": "overdue", "error_code": "mt5_maintenance_never_completed"}
        if state.get("schema_version") != 1 or state.get("timezone") != zone_name:
            raise ValueError("invalid maintenance state")
        run_date = datetime.strptime(state["scheduled_local_date"], "%Y-%m-%d").date()
        if datetime.combine(run_date, parsed, zone) > local_now:
            raise ValueError("future maintenance state")
        result.update({"last_run_local_date": run_date.isoformat(), "last_run_status": state.get("status")})
        if run_date < expected_date:
            return {**result, "status": "overdue", "error_code": "mt5_maintenance_overdue"}
        status = state.get("status")
        if status == "completed":
            finished = datetime.fromisoformat(state["finished_at_utc"].replace("Z", "+00:00"))
            if finished.tzinfo is None or not datetime.combine(run_date, parsed, zone) <= finished <= now:
                raise ValueError("invalid completion timestamp")
            return {**result, "status": "healthy", "last_completed_at_utc": state["finished_at_utc"]}
        if status == "running" and run_date > expected_date:
            return {**result, "status": "running"}
        if status == "failed":
            code = "native_maintenance_recovery_unavailable" if state.get("error_code") == "native_maintenance_recovery_unavailable" else "mt5_maintenance_failed"
            return {**result, "status": "failed", "error_code": code}
        if status == "running":
            return {**result, "status": "overdue", "error_code": "mt5_maintenance_stuck"}
        raise ValueError("invalid maintenance status")
    except (OSError, ValueError, TypeError, KeyError):
        return {**result, "status": "invalid_state", "error_code": "mt5_maintenance_health_unverifiable"}


class Mt5MaintenanceHealthMonitor:
    """Publish local evidence and logs even when the scheduler has disappeared."""

    def __init__(self, release_root: Path, environment: Mapping[str, str] | None = None) -> None:
        source = os.environ if environment is None else environment
        self.environment = {
            name: value for name, value in source.items()
            if name.startswith("TRADEJOURNAL_MT5_MAINTENANCE_")
        }
        self.release_root = release_root
        state = Path(self.environment.get("TRADEJOURNAL_MT5_MAINTENANCE_STATE_PATH", str(DEFAULT_STATE)))
        self.health_path = state.with_name("mt5-maintenance-health.json")

    def check_once(self) -> dict[str, object]:
        health = evaluate_maintenance_health(self.release_root, self.environment)
        try:
            atomic_json(self.health_path, health)
        except OSError:
            logger.error("MT5 maintenance health evidence could not be persisted")
        report = json.dumps(health, sort_keys=True)
        if health["status"] in ("disabled", "healthy", "running"):
            logger.info("MT5 maintenance health %s", report)
        else:
            logger.error("MT5 maintenance health %s", report)
        return health

    def run(self, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            try:
                self.check_once()
            except Exception:
                # Observability must never stop account processing. Emit failure rather than
                # letting a dead monitor look healthy in Grafana's no-data checks.
                logger.exception("MT5 maintenance health monitor failed")
            stop_event.wait(60)
