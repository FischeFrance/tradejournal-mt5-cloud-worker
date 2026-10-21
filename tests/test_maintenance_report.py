import json
from types import SimpleNamespace
from uuid import uuid4

from windows_agent.maintenance_report import (
    MAX_REPORTED_EXCLUSIONS,
    MAX_REPORTED_ISSUES,
    MaintenanceReporter,
)


class Api:
    def __init__(self, fail=False):
        self.fail = fail
        self.bodies = []

    def request(self, method, path, payload):
        assert method == "POST"
        assert path == "../trading-agent-maintenance"
        self.bodies.append(payload)
        if self.fail:
            raise RuntimeError("control plane down")
        return {"api_version": "1", "status": "recorded"}


def state(status="completed", attempts=1, **extra):
    return {"status": status, "scheduled_local_date": "2026-10-21", "attempts": attempts, **extra}


def test_a_completed_night_reports_the_accounts_left_out_and_why():
    first, second = sorted(str(uuid4()) for _ in range(2))
    recovery = SimpleNamespace(skip_reasons={
        second: "release_state_unverifiable", first: "ticket_baseline_unavailable",
    })
    api = Api()

    MaintenanceReporter(api, recovery).attempt_finished(state(), True)

    assert api.bodies == [{
        "api_version": "1", "operation": "report", "local_date": "2026-10-21",
        "status": "completed", "attempt": 1, "final": True, "error_code": None,
        "excluded": [
            {"connection_id": first, "reason": "ticket_baseline_unavailable"},
            {"connection_id": second, "reason": "release_state_unverifiable"},
        ],
        "trigger": "scheduled", "phases": {},
    }]


def test_the_report_carries_what_each_phase_did():
    account = str(uuid4())
    run_id = str(uuid4())
    phases = {
        "expert": {"status": "updated", "template": "updated", "updated": 1, "failed": 0},
        "mt5": {"status": "updated", "build": 6274, "migrated": 5},
        "verify": {
            "status": "issues", "checked": 5, "healthy": 4,
            "issues": [{"connection_id": account, "problem": "disconnected"}],
        },
    }
    api = Api()

    MaintenanceReporter(api, SimpleNamespace(skip_reasons={}), phases=lambda: phases).attempt_finished(
        state(trigger="manual", run_id=run_id), True,
    )

    body = api.bodies[0]
    assert body["trigger"] == "manual"
    assert body["run_id"] == run_id
    assert body["phases"] == phases


def test_phase_values_are_reduced_to_codes_and_counts():
    phases = {
        "expert": {"status": "failed", "reason": "MT5 template expert update failed", "updated": True},
        "mt5": {"status": "skipped", "build": -1, "secret": "value"},
        "verify": {"status": "issues", "issues": [
            {"connection_id": "not-a-uuid", "problem": "disconnected"},
            {"connection_id": str(uuid4()), "problem": "Not A Code"},
        ]},
        "other": {"status": "ok"},
    }
    api = Api()

    MaintenanceReporter(api, phases=lambda: phases).attempt_finished(state("failed", 2, run_id="x"), True)

    body = api.bodies[0]
    assert "run_id" not in body
    assert body["phases"] == {
        "expert": {"status": "failed"},
        "mt5": {"status": "skipped"},
        "verify": {"status": "issues", "issues": []},
    }


def test_unavailable_phases_still_send_the_report():
    def broken():
        raise RuntimeError("coordinator state unavailable")

    api = Api()

    MaintenanceReporter(api, phases=broken).attempt_finished(state(), True)

    assert api.bodies[0]["phases"] == {}


def test_a_failure_carries_its_error_code_and_whether_a_retry_follows():
    api = Api()
    reporter = MaintenanceReporter(api, SimpleNamespace(skip_reasons={}))

    reporter.attempt_finished(state("failed", 1, error_code="native_maintenance_recovery_unavailable"), False)
    reporter.attempt_finished(state("failed", 2, window_deadline_reached=True), True)
    reporter.attempt_finished(state("failed", 2), True)

    assert [(body["error_code"], body["final"]) for body in api.bodies] == [
        ("native_maintenance_recovery_unavailable", False),
        ("maintenance_window_ended", True),
        ("mt5_maintenance_failed", True),
    ]


def test_running_or_malformed_states_are_not_reported():
    api = Api()
    reporter = MaintenanceReporter(api)

    reporter.attempt_finished(state("running"), False)
    reporter.attempt_finished({"status": "failed", "scheduled_local_date": "21/10/2026", "attempts": 1}, True)
    reporter.attempt_finished(state("failed", True), True)

    assert api.bodies == []


def test_an_unreachable_control_plane_never_raises():
    api = Api(fail=True)

    MaintenanceReporter(api).attempt_finished(state("failed", 2), True)

    assert len(api.bodies) == 1


def test_an_overdue_night_is_reported_once_and_retried_only_if_undelivered():
    api = Api(fail=True)
    reporter = MaintenanceReporter(api)

    reporter.overdue("2026-10-20", "mt5_maintenance_stuck")
    api.fail = False
    reporter.overdue("2026-10-20", "mt5_maintenance_stuck")
    reporter.overdue("2026-10-20", "mt5_maintenance_stuck")
    reporter.overdue("not-a-date", None)

    assert len(api.bodies) == 2
    assert api.bodies[-1] == {
        "api_version": "1", "operation": "report", "local_date": "2026-10-20",
        "status": "overdue", "attempt": 0, "final": True,
        "error_code": "mt5_maintenance_stuck", "excluded": [],
    }


def test_the_report_stays_under_the_endpoint_size_limit():
    reasons = {str(uuid4()): "ticket_baseline_unavailable" for _ in range(MAX_REPORTED_EXCLUSIONS + 5)}
    phases = {
        "expert": {"status": "partial", "template": "updated", "updated": 3, "failed": 2},
        "mt5": {"status": "failed", "reason": "r" * 64},
        "verify": {
            "status": "issues", "checked": 40, "healthy": 20,
            "issues": [
                {"connection_id": str(uuid4()), "problem": "process_ambiguous"}
                for _ in range(MAX_REPORTED_ISSUES + 5)
            ],
        },
    }
    api = Api()

    MaintenanceReporter(api, SimpleNamespace(skip_reasons=reasons), phases=lambda: phases).attempt_finished(
        state("failed", 2, error_code="native_maintenance_recovery_unavailable", run_id=str(uuid4())), True,
    )

    body = api.bodies[0]
    assert len(body["excluded"]) == MAX_REPORTED_EXCLUSIONS
    assert len(body["phases"]["verify"]["issues"]) == MAX_REPORTED_ISSUES
    assert len(json.dumps(body).encode("utf-8")) < 4096
