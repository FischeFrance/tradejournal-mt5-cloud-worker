from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from windows_agent.mt5_maintenance_scheduler import (
    Mt5MaintenanceScheduleError,
    Mt5MaintenanceScheduler,
)
from windows_agent.state_store import atomic_json, read_json


class Clock:
    def __init__(self, current: datetime) -> None:
        self.current = current

    def __call__(self) -> datetime:
        return self.current


class Coordinator:
    def __init__(self, failures: int = 0) -> None:
        self.failures = failures
        self.calls: list[threading.Event] = []

    def run_once(self, stop_event: threading.Event) -> None:
        self.calls.append(stop_event)
        if len(self.calls) <= self.failures:
            raise RuntimeError("fixture maintenance failure")


class InterruptingCoordinator(Coordinator):
    def run_once(self, stop_event: threading.Event) -> None:
        self.calls.append(stop_event)
        stop_event.set()
        raise RuntimeError("fixture service stop")


def _scheduler(
    tmp_path: Path,
    clock: Clock,
    coordinator: Coordinator | None = None,
    **kwargs: object,
) -> tuple[Mt5MaintenanceScheduler, Coordinator]:
    coordinator = coordinator or Coordinator()
    return (
        Mt5MaintenanceScheduler(
            coordinator,
            tmp_path / "maintenance.json",
            clock=clock,
            **kwargs,
        ),
        coordinator,
    )


def test_runs_at_2330_europe_rome_and_only_once_after_restart(
    tmp_path: Path,
) -> None:
    # 2026-08-27 is in CEST: 23:30 Europe/Rome == 21:30 UTC.
    clock = Clock(datetime(2026, 8, 27, 21, 29, 59, tzinfo=timezone.utc))
    scheduler, coordinator = _scheduler(tmp_path, clock)
    stop_event = threading.Event()

    assert scheduler.is_due() is False
    assert scheduler.run_if_due(stop_event) is False

    clock.current = datetime(2026, 8, 27, 21, 30, tzinfo=timezone.utc)
    assert scheduler.is_due() is True
    assert scheduler.run_if_due(stop_event) is True
    assert coordinator.calls == [stop_event]

    state = read_json(tmp_path / "maintenance.json")
    assert state["scheduled_local_date"] == "2026-08-27"
    assert state["scheduled_at_local"].startswith("2026-08-27T23:30:00+02:00")
    assert state["status"] == "completed"
    assert state["attempts"] == 1

    restarted, _ = _scheduler(tmp_path, clock, coordinator)
    assert restarted.is_due() is False
    assert restarted.run_if_due(stop_event) is False
    assert len(coordinator.calls) == 1


def test_failed_attempt_retries_once_after_delay_within_grace(
    tmp_path: Path,
) -> None:
    clock = Clock(datetime(2026, 8, 27, 21, 30, tzinfo=timezone.utc))
    coordinator = Coordinator(failures=1)
    scheduler, _ = _scheduler(
        tmp_path,
        clock,
        coordinator,
        grace_window=timedelta(minutes=45),
        retry_delay=timedelta(minutes=10),
        max_attempts_per_local_date=2,
    )

    assert scheduler.run_if_due(threading.Event()) is True
    failed = read_json(tmp_path / "maintenance.json")
    assert failed["status"] == "failed"
    assert failed["attempts"] == 1

    clock.current = datetime(2026, 8, 27, 21, 39, 59, tzinfo=timezone.utc)
    assert scheduler.is_due() is False
    clock.current = datetime(2026, 8, 27, 21, 40, tzinfo=timezone.utc)
    assert scheduler.is_due() is True
    assert scheduler.run_if_due(threading.Event()) is True

    completed = read_json(tmp_path / "maintenance.json")
    assert completed["status"] == "completed"
    assert completed["attempts"] == 2
    assert len(coordinator.calls) == 2


def test_failed_attempts_are_bounded_per_local_date(tmp_path: Path) -> None:
    clock = Clock(datetime(2026, 8, 27, 21, 30, tzinfo=timezone.utc))
    coordinator = Coordinator(failures=5)
    scheduler, _ = _scheduler(
        tmp_path,
        clock,
        coordinator,
        retry_delay=timedelta(minutes=5),
        max_attempts_per_local_date=2,
    )

    assert scheduler.run_if_due(threading.Event()) is True
    clock.current += timedelta(minutes=5)
    assert scheduler.run_if_due(threading.Event()) is True
    clock.current += timedelta(minutes=5)
    assert scheduler.is_due() is False
    assert scheduler.run_if_due(threading.Event()) is False
    assert len(coordinator.calls) == 2


def test_running_state_is_never_retried_automatically(tmp_path: Path) -> None:
    state_path = tmp_path / "maintenance.json"
    atomic_json(
        state_path,
        {
            "schema_version": 1,
            "scheduled_local_date": "2026-08-27",
            "scheduled_at_local": "2026-08-27T23:30:00+02:00",
            "timezone": "Europe/Rome",
            "status": "running",
            "attempts": 1,
            "run_id": "00000000-0000-4000-8000-000000000001",
            "started_at_utc": "2026-08-27T21:30:00.000Z",
        },
    )
    clock = Clock(datetime(2026, 8, 27, 21, 50, tzinfo=timezone.utc))
    scheduler, coordinator = _scheduler(tmp_path, clock)

    assert scheduler.is_due() is False
    assert scheduler.run_if_due(threading.Event()) is False
    assert coordinator.calls == []


def test_recovered_interrupted_claim_retries_only_after_delay_inside_window(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "maintenance.json"
    atomic_json(
        state_path,
        {
            "schema_version": 1,
            "scheduled_local_date": "2026-08-27",
            "scheduled_at_local": "2026-08-27T23:30:00+02:00",
            "timezone": "Europe/Rome",
            "status": "running",
            "attempts": 1,
            "run_id": "00000000-0000-4000-8000-000000000001",
            "started_at_utc": "2026-08-27T21:30:00.000Z",
        },
    )
    clock = Clock(datetime(2026, 8, 27, 21, 50, tzinfo=timezone.utc))
    scheduler, coordinator = _scheduler(tmp_path, clock)

    assert scheduler.recover_interrupted() is True
    assert scheduler.is_due() is False
    assert scheduler.run_if_due(threading.Event()) is False
    clock.current = datetime(2026, 8, 27, 22, 0, tzinfo=timezone.utc)
    assert scheduler.is_due() is True
    assert scheduler.run_if_due(threading.Event()) is True
    assert len(coordinator.calls) == 1
    state = read_json(state_path)
    assert state["status"] == "completed"
    assert state["attempts"] == 2


def test_recovered_interrupted_claim_waits_for_next_daily_window(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "maintenance.json"
    atomic_json(
        state_path,
        {
            "schema_version": 1,
            "scheduled_local_date": "2026-08-27",
            "scheduled_at_local": "2026-08-27T23:30:00+02:00",
            "timezone": "Europe/Rome",
            "status": "running",
            "attempts": 2,
            "run_id": "00000000-0000-4000-8000-000000000001",
            "started_at_utc": "2026-08-27T21:40:00.000Z",
        },
    )
    # 06:00 Europe/Rome: far outside the ordinary grace window.
    clock = Clock(datetime(2026, 8, 28, 4, 0, tzinfo=timezone.utc))
    scheduler, coordinator = _scheduler(
        tmp_path,
        clock,
        max_attempts_per_local_date=2,
    )

    assert scheduler.recover_interrupted() is True
    assert scheduler.is_due() is False
    assert scheduler.run_if_due(threading.Event()) is False
    assert coordinator.calls == []

    clock.current = datetime(2026, 8, 28, 21, 30, tzinfo=timezone.utc)
    assert scheduler.is_due() is True
    assert scheduler.run_if_due(threading.Event()) is True
    assert len(coordinator.calls) == 1
    completed = read_json(state_path)
    assert completed["status"] == "completed"
    assert completed["scheduled_local_date"] == "2026-08-28"
    assert completed["attempts"] == 1


def test_graceful_stop_during_maintenance_waits_for_next_window(
    tmp_path: Path,
) -> None:
    clock = Clock(datetime(2026, 8, 27, 21, 30, tzinfo=timezone.utc))
    interrupted = InterruptingCoordinator()
    scheduler, _ = _scheduler(tmp_path, clock, interrupted)

    assert scheduler.run_if_due(threading.Event()) is True
    failed = read_json(tmp_path / "maintenance.json")
    assert failed["status"] == "failed"
    assert failed["recovered_interrupted_run"] is True
    assert failed["next_retry_at_utc"] > failed["finished_at_utc"]

    # Restart well outside the normal 23:30 grace window.
    clock.current = datetime(2026, 8, 28, 4, 0, tzinfo=timezone.utc)
    restarted, coordinator = _scheduler(tmp_path, clock)

    assert restarted.is_due() is False
    assert restarted.run_if_due(threading.Event()) is False
    assert coordinator.calls == []

    clock.current = datetime(2026, 8, 28, 21, 30, tzinfo=timezone.utc)
    assert restarted.is_due() is True
    assert restarted.run_if_due(threading.Event()) is True
    assert len(coordinator.calls) == 1
    completed = read_json(tmp_path / "maintenance.json")
    assert completed["status"] == "completed"
    assert completed["scheduled_local_date"] == "2026-08-28"
    assert completed["attempts"] == 1


def test_grace_window_can_cross_local_midnight(tmp_path: Path) -> None:
    # 00:10 CEST on the 28th is still inside the 27th's 23:30-00:30 window.
    clock = Clock(datetime(2026, 8, 27, 22, 10, tzinfo=timezone.utc))
    scheduler, coordinator = _scheduler(tmp_path, clock)

    assert scheduler.run_if_due(threading.Event()) is True
    assert len(coordinator.calls) == 1
    assert (
        read_json(tmp_path / "maintenance.json")["scheduled_local_date"] == "2026-08-27"
    )


def test_does_not_catch_up_outside_grace_window(tmp_path: Path) -> None:
    # 00:31 CEST is one minute outside the default one-hour grace window.
    clock = Clock(datetime(2026, 8, 27, 22, 31, tzinfo=timezone.utc))
    scheduler, coordinator = _scheduler(tmp_path, clock)

    assert scheduler.is_due() is False
    assert scheduler.run_if_due(threading.Event()) is False
    assert coordinator.calls == []
    assert not (tmp_path / "maintenance.json").exists()


@pytest.mark.parametrize(
    ("observed_utc", "expected_offset"),
    (
        # DST starts on 2026-03-29: 23:30 Rome is 21:30 UTC (UTC+02).
        (datetime(2026, 3, 29, 21, 30, tzinfo=timezone.utc), "+02:00"),
        # DST ends on 2026-10-25: 23:30 Rome is 22:30 UTC (UTC+01).
        (datetime(2026, 10, 25, 22, 30, tzinfo=timezone.utc), "+01:00"),
    ),
)
def test_europe_rome_dst_uses_the_correct_utc_offset(
    tmp_path: Path,
    observed_utc: datetime,
    expected_offset: str,
) -> None:
    clock = Clock(observed_utc)
    scheduler, _ = _scheduler(tmp_path, clock)

    assert scheduler.run_if_due(threading.Event()) is True
    state = read_json(tmp_path / "maintenance.json")
    assert state["scheduled_at_local"].endswith(expected_offset)


def test_stop_requested_does_not_claim_the_daily_run(tmp_path: Path) -> None:
    clock = Clock(datetime(2026, 8, 27, 21, 30, tzinfo=timezone.utc))
    scheduler, coordinator = _scheduler(tmp_path, clock)
    stop_event = threading.Event()
    stop_event.set()

    assert scheduler.run_if_due(stop_event) is False
    assert coordinator.calls == []
    assert not (tmp_path / "maintenance.json").exists()


def test_invalid_or_untrusted_state_fails_closed(tmp_path: Path) -> None:
    state_path = tmp_path / "maintenance.json"
    state_path.write_text("not-json", encoding="utf-8")
    clock = Clock(datetime(2026, 8, 27, 21, 30, tzinfo=timezone.utc))
    scheduler, coordinator = _scheduler(tmp_path, clock)

    with pytest.raises(Mt5MaintenanceScheduleError):
        scheduler.is_due()
    assert coordinator.calls == []


@pytest.mark.parametrize(
    "kwargs",
    (
        {"scheduled_time": "24:00"},
        {"timezone_name": "Invalid/Timezone"},
        {"grace_window": timedelta(0)},
        {
            "grace_window": timedelta(minutes=10),
            "retry_delay": timedelta(minutes=10),
        },
        {"max_attempts_per_local_date": 0},
    ),
)
def test_invalid_schedule_configuration_is_rejected(
    tmp_path: Path,
    kwargs: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        _scheduler(
            tmp_path,
            Clock(datetime(2026, 8, 27, 21, 30, tzinfo=timezone.utc)),
            **kwargs,
        )
