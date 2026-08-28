"""Durable, timezone-aware scheduling for local MT5 maintenance.

The scheduler deliberately owns only *when* maintenance may run.  The injected
coordinator owns the MT5/template/pool operation itself.  Persisting a RUNNING
claim before calling the coordinator prevents a service restart from silently
executing the same local-date maintenance twice after an uncertain crash.
"""

from __future__ import annotations

import logging
import re
import threading
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Callable, Protocol
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .state_store import atomic_json, read_json


logger = logging.getLogger(__name__)

STATE_SCHEMA_VERSION = 1
DEFAULT_STATE_PATH = Path(r"C:\TradeJournal\state\mt5-maintenance.json")
DEFAULT_TIMEZONE_NAME = "Europe/Rome"
DEFAULT_SCHEDULED_TIME = time(23, 30)
DEFAULT_GRACE_WINDOW = timedelta(minutes=60)
DEFAULT_RETRY_DELAY = timedelta(minutes=10)
DEFAULT_MAX_ATTEMPTS_PER_LOCAL_DATE = 2

_TIME_PATTERN = re.compile(r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")
_STATUSES = frozenset(("running", "completed", "failed"))


class Mt5MaintenanceCoordinator(Protocol):
    """Operation dependency used by :class:`Mt5MaintenanceScheduler`."""

    def run_once(self, stop_event: threading.Event) -> object:
        """Run one idempotent MT5 maintenance pass."""


class Mt5MaintenanceScheduleError(RuntimeError):
    """The persisted maintenance schedule cannot be trusted."""


def _parse_scheduled_time(value: time | str) -> time:
    if isinstance(value, str):
        if not _TIME_PATTERN.fullmatch(value):
            raise ValueError("MT5 maintenance time must use HH:MM")
        hour, minute = (int(part) for part in value.split(":"))
        return time(hour, minute)
    if not isinstance(value, time) or value.tzinfo is not None:
        raise ValueError("MT5 maintenance time must be a naive local time")
    return value


def _utc_text(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _parse_utc_text(value: object) -> datetime:
    if not isinstance(value, str):
        raise Mt5MaintenanceScheduleError("MT5 maintenance state is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise Mt5MaintenanceScheduleError("MT5 maintenance state is invalid") from exc
    if parsed.tzinfo is None:
        raise Mt5MaintenanceScheduleError("MT5 maintenance state is invalid")
    return parsed.astimezone(timezone.utc)


class Mt5MaintenanceScheduler:
    """Run maintenance in one bounded daily ``Europe/Rome`` window.

    A completed or still-running local date is never executed again. A known
    failure may be retried after a bounded delay, within the same grace window,
    up to ``max_attempts_per_local_date``. Startup can reconcile the filesystem
    journal of an interrupted attempt, but it never makes fleet rotation due
    outside the configured market-closed window.
    """

    def __init__(
        self,
        coordinator: Mt5MaintenanceCoordinator,
        state_path: Path = DEFAULT_STATE_PATH,
        *,
        timezone_name: str = DEFAULT_TIMEZONE_NAME,
        scheduled_time: time | str = DEFAULT_SCHEDULED_TIME,
        grace_window: timedelta = DEFAULT_GRACE_WINDOW,
        retry_delay: timedelta = DEFAULT_RETRY_DELAY,
        max_attempts_per_local_date: int = (DEFAULT_MAX_ATTEMPTS_PER_LOCAL_DATE),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(timezone_name, str) or not timezone_name.strip():
            raise ValueError("MT5 maintenance timezone is invalid")
        try:
            local_timezone = ZoneInfo(timezone_name.strip())
        except ZoneInfoNotFoundError as exc:
            raise ValueError("MT5 maintenance timezone is invalid") from exc
        if (
            not isinstance(grace_window, timedelta)
            or grace_window <= timedelta(0)
            or grace_window >= timedelta(days=1)
        ):
            raise ValueError("MT5 maintenance grace window is invalid")
        if (
            not isinstance(retry_delay, timedelta)
            or retry_delay <= timedelta(0)
            or retry_delay >= grace_window
        ):
            raise ValueError("MT5 maintenance retry delay is invalid")
        if (
            not isinstance(max_attempts_per_local_date, int)
            or isinstance(max_attempts_per_local_date, bool)
            or not 1 <= max_attempts_per_local_date <= 5
        ):
            raise ValueError("MT5 maintenance retry limit is invalid")

        self.coordinator = coordinator
        self.state_path = Path(state_path)
        self.timezone_name = timezone_name.strip()
        self.local_timezone = local_timezone
        self.scheduled_time = _parse_scheduled_time(scheduled_time)
        self.grace_window = grace_window
        self.retry_delay = retry_delay
        self.max_attempts_per_local_date = max_attempts_per_local_date
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()

    def _now(self) -> datetime:
        observed = self._clock()
        if not isinstance(observed, datetime) or observed.tzinfo is None:
            raise ValueError("MT5 maintenance clock must return an aware datetime")
        return observed.astimezone(timezone.utc)

    def _eligible_slot(self, now_utc: datetime) -> tuple[date, datetime] | None:
        local_now = now_utc.astimezone(self.local_timezone)
        scheduled = datetime.combine(
            local_now.date(),
            self.scheduled_time,
            tzinfo=self.local_timezone,
        )
        if local_now < scheduled:
            scheduled = datetime.combine(
                local_now.date() - timedelta(days=1),
                self.scheduled_time,
                tzinfo=self.local_timezone,
            )
        if scheduled <= local_now <= scheduled + self.grace_window:
            return scheduled.date(), scheduled
        return None

    def _read_state(self) -> dict:
        try:
            state = read_json(self.state_path, {})
        except (OSError, ValueError) as exc:
            raise Mt5MaintenanceScheduleError(
                "MT5 maintenance state is unavailable"
            ) from exc
        if not state:
            return {}
        status = state.get("status")
        attempts = state.get("attempts")
        local_date = state.get("scheduled_local_date")
        if (
            state.get("schema_version") != STATE_SCHEMA_VERSION
            or status not in _STATUSES
            or not isinstance(local_date, str)
            or not isinstance(attempts, int)
            or isinstance(attempts, bool)
            or attempts < 1
        ):
            raise Mt5MaintenanceScheduleError("MT5 maintenance state is invalid")
        try:
            date.fromisoformat(local_date)
        except ValueError as exc:
            raise Mt5MaintenanceScheduleError(
                "MT5 maintenance state is invalid"
            ) from exc
        if status == "failed":
            _parse_utc_text(state.get("next_retry_at_utc"))
        if (
            "recovered_interrupted_run" in state
            and state.get("recovered_interrupted_run") is not True
        ):
            raise Mt5MaintenanceScheduleError(
                "MT5 maintenance state is invalid"
            )
        return state

    def _state_allows_attempt(
        self,
        state: dict,
        scheduled_local_date: date,
        now_utc: datetime,
    ) -> bool:
        if (
            not state
            or state.get("scheduled_local_date") != scheduled_local_date.isoformat()
        ):
            return True
        status = state["status"]
        if status in ("running", "completed"):
            return False
        attempts = state["attempts"]
        if attempts >= self.max_attempts_per_local_date:
            return False
        return now_utc >= _parse_utc_text(state["next_retry_at_utc"])

    def is_due(self) -> bool:
        """Return whether one maintenance attempt may start at the current time."""

        with self._lock:
            now_utc = self._now()
            state = self._read_state()
            slot = self._eligible_slot(now_utc)
            if slot is None:
                return False
            scheduled_local_date, _ = slot
            return self._state_allows_attempt(
                state,
                scheduled_local_date,
                now_utc,
            )

    def recover_interrupted(self) -> bool:
        """Make a crash-interrupted attempt eligible for an idempotent retry.

        The MT5 coordinator performs its filesystem journal recovery before
        this method is called.  Once that recovery succeeds, retrying the whole
        pool/fleet pass is safe, but it remains eligible only inside a later
        23:30 grace window. Recovery must never create daytime MT5 restarts.
        """

        with self._lock:
            state = self._read_state()
            if not state or state.get("status") != "running":
                return False
            recovered_at = self._now()
            atomic_json(
                self.state_path,
                {
                    **state,
                    "status": "failed",
                    "finished_at_utc": _utc_text(recovered_at),
                    "next_retry_at_utc": _utc_text(
                        recovered_at + self.retry_delay
                    ),
                    "recovered_interrupted_run": True,
                },
            )
            return True

    def run_if_due(self, stop_event: threading.Event) -> bool:
        """Run one due attempt and return whether an attempt was started.

        Coordinator failures are recorded and logged, but deliberately do not
        escape: the long-running Windows Agent must remain alive so the bounded
        retry can occur.  State I/O failures still propagate because proceeding
        without a durable claim would make duplicate execution possible.
        """

        if stop_event.is_set():
            return False
        with self._lock:
            if stop_event.is_set():
                return False
            now_utc = self._now()
            previous = self._read_state()
            slot = self._eligible_slot(now_utc)
            if slot is None:
                return False
            scheduled_local_date, scheduled_local = slot
            if not self._state_allows_attempt(
                previous,
                scheduled_local_date,
                now_utc,
            ):
                return False
            attempts = (
                int(previous["attempts"]) + 1
                if previous.get("scheduled_local_date")
                == scheduled_local_date.isoformat()
                else 1
            )
            run_id = str(uuid4())
            running = {
                "schema_version": STATE_SCHEMA_VERSION,
                "scheduled_local_date": scheduled_local_date.isoformat(),
                "scheduled_at_local": scheduled_local.isoformat(),
                "timezone": self.timezone_name,
                "status": "running",
                "attempts": attempts,
                "run_id": run_id,
                "started_at_utc": _utc_text(now_utc),
            }
            atomic_json(self.state_path, running)

        try:
            self.coordinator.run_once(stop_event)
        except Exception:
            failed_at = self._now()
            interrupted = stop_event.is_set()
            failed = {
                **running,
                "status": "failed",
                "finished_at_utc": _utc_text(failed_at),
                "next_retry_at_utc": _utc_text(
                    failed_at + self.retry_delay
                ),
            }
            if interrupted:
                # A graceful service stop can happen after part of the pool or
                # fleet has already moved to the new release. Record it for
                # idempotent recovery, but never bypass the next market-closed
                # scheduling window.
                failed["recovered_interrupted_run"] = True
            atomic_json(self.state_path, failed)
            logger.exception(
                "scheduled MT5 maintenance failed (local_date=%s, attempt=%s)",
                scheduled_local_date.isoformat(),
                attempts,
            )
            return True

        completed_at = self._now()
        completed = {
            **running,
            "status": "completed",
            "finished_at_utc": _utc_text(completed_at),
        }
        atomic_json(self.state_path, completed)
        logger.info(
            "scheduled MT5 maintenance completed (local_date=%s, attempt=%s)",
            scheduled_local_date.isoformat(),
            attempts,
        )
        return True
