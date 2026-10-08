import json
import logging
from datetime import datetime, timezone

import pytest

from tests.test_maintenance_capability import capable_release
from windows_agent.maintenance_capability import MAINTENANCE_ENV
from windows_agent.observability.maintenance_health import (
    Mt5MaintenanceHealthMonitor,
    evaluate_maintenance_health,
)


def environment(tmp_path):
    return {
        MAINTENANCE_ENV: "1",
        "TRADEJOURNAL_MT5_MAINTENANCE_STATE_PATH": str(tmp_path / "mt5-maintenance.json"),
    }


def state(tmp_path, date="2026-10-07", status="completed", finished="2026-10-07T21:50:00Z"):
    (tmp_path / "mt5-maintenance.json").write_text(json.dumps({
        "schema_version": 1, "timezone": "Europe/Rome", "scheduled_local_date": date,
        "status": status, "finished_at_utc": finished,
    }))


def test_distinguishes_missing_code_from_a_healthy_live_connection(tmp_path):
    state(tmp_path)
    assert evaluate_maintenance_health(tmp_path, environment(tmp_path))["status"] == "unsupported"


def test_the_real_september_journal_is_overdue_and_is_never_reset(tmp_path):
    root = capable_release(tmp_path / "release")
    state(tmp_path, "2026-09-23", finished="2026-09-23T21:50:32.100Z")
    before = (tmp_path / "mt5-maintenance.json").read_bytes()
    health = evaluate_maintenance_health(root, environment(tmp_path), now=datetime(2026, 10, 8, 21, 50, tzinfo=timezone.utc))
    assert health["status"] == "overdue"
    assert health["expected_completed_local_date"] == "2026-10-07"
    assert (tmp_path / "mt5-maintenance.json").read_bytes() == before


@pytest.mark.parametrize("utc, expected", [
    ("2026-10-08T21:29:00+00:00", "healthy"),
    ("2026-10-08T21:31:00+00:00", "healthy"),
    ("2026-10-08T23:29:00+00:00", "healthy"),
    ("2026-10-08T23:30:00+00:00", "overdue"),
])
def test_respects_the_2330_to_0130_rome_grace_window(tmp_path, utc, expected):
    root = capable_release(tmp_path / "release")
    state(tmp_path)
    assert evaluate_maintenance_health(root, environment(tmp_path), now=datetime.fromisoformat(utc))["status"] == expected


def test_timezone_conversion_handles_the_end_of_daylight_saving(tmp_path):
    root = capable_release(tmp_path / "release")
    state(tmp_path, "2026-10-25", finished="2026-10-25T23:00:00Z")
    now = datetime(2026, 10, 26, 0, 30, tzinfo=timezone.utc)
    health = evaluate_maintenance_health(root, environment(tmp_path), now=now)
    assert health["expected_completed_local_date"] == "2026-10-25"
    assert health["status"] == "healthy"


def test_monitor_persists_a_separate_health_record_and_logs_for_loki(tmp_path, caplog):
    state(tmp_path, "2026-09-23", finished="2026-09-23T21:50:32Z")
    before = (tmp_path / "mt5-maintenance.json").read_bytes()
    monitor = Mt5MaintenanceHealthMonitor(tmp_path / "missing-release", environment(tmp_path))
    with caplog.at_level(logging.ERROR):
        result = monitor.check_once()
    assert json.loads((tmp_path / "mt5-maintenance-health.json").read_text())["status"] == "unsupported"
    assert result["status"] == "unsupported"
    assert "mt5_maintenance_module_missing" in caplog.text
    assert (tmp_path / "mt5-maintenance.json").read_bytes() == before


def test_future_or_corrupt_completion_cannot_look_healthy(tmp_path):
    root = capable_release(tmp_path / "release")
    state(tmp_path, "2099-10-07", finished="2099-10-07T21:50:00Z")
    assert evaluate_maintenance_health(root, environment(tmp_path))["status"] == "invalid_state"


def test_native_gap_delivery_failure_is_visible_in_health(tmp_path):
    root = capable_release(tmp_path / "release")
    state(tmp_path, status="failed")
    path = tmp_path / "mt5-maintenance.json"
    journal = json.loads(path.read_text())
    journal["error_code"] = "native_maintenance_recovery_unavailable"
    path.write_text(json.dumps(journal))
    health = evaluate_maintenance_health(root, environment(tmp_path), now=datetime(2026, 10, 8, 21, 50, tzinfo=timezone.utc))
    assert health["status"] == "failed"
    assert health["error_code"] == "native_maintenance_recovery_unavailable"
