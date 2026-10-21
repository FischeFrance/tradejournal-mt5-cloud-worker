from uuid import uuid4

import pytest

from windows_agent.maintenance_recovery import MaintenanceRecovery, MaintenanceRecoveryUnavailable
from windows_agent.state_store import atomic_json
from windows_agent.worker.native_mt5_runtime import NativeMt5Runtime as CurrentNativeMt5Runtime


class Api:
    def __init__(self, connection_ids):
        self.connection_ids = list(connection_ids)

    def request(self, method, path, payload):
        assert payload["operation"] == "inventory"
        return {"api_version": "1", "connection_ids": self.connection_ids}


def _recovery(tmp_path, monkeypatch, connection_ids, failing):
    recovery = MaintenanceRecovery(Api(connection_ids), tmp_path / "instances")
    monkeypatch.setattr(CurrentNativeMt5Runtime, "_setting", staticmethod(lambda _name: "1"))
    monkeypatch.setattr("windows_agent.maintenance_recovery.verify_ticket_reader", lambda: None)
    monkeypatch.setattr(recovery, "identity", lambda _cid: (42, "Demo"))
    captured = []

    def capture(root, cid, *_args):
        if cid in failing:
            raise ValueError("maintenance_ticket_process_ambiguous")
        captured.append(cid)

    monkeypatch.setattr("windows_agent.maintenance_recovery.capture_baseline", capture)
    return recovery, captured


def test_an_account_the_broker_disabled_is_left_out_and_the_rest_is_maintained(tmp_path, monkeypatch, caplog):
    healthy, disabled = sorted(str(uuid4()) for _ in range(2))
    recovery, captured = _recovery(tmp_path, monkeypatch, [healthy, disabled], failing={disabled})

    recovery.refresh_for_rotation()

    assert captured == [healthy]
    assert recovery.allows(healthy)
    assert not recovery.allows(disabled)
    assert recovery.skipped_connection_ids == frozenset({disabled})
    assert recovery.skip_reasons == {disabled: "ticket_baseline_unavailable"}
    assert disabled in caplog.text


def test_a_failure_before_the_last_account_does_not_stop_the_capture_of_the_others(tmp_path, monkeypatch):
    first, second, third = sorted(str(uuid4()) for _ in range(3))
    recovery, captured = _recovery(tmp_path, monkeypatch, [first, second, third], failing={first})

    recovery.refresh_for_rotation()

    assert captured == [second, third]
    assert not recovery.allows(first)


def test_the_run_stops_when_no_account_can_produce_a_baseline(tmp_path, monkeypatch):
    first, second = sorted(str(uuid4()) for _ in range(2))
    recovery, captured = _recovery(tmp_path, monkeypatch, [first, second], failing={first, second})

    with pytest.raises(MaintenanceRecoveryUnavailable, match="native_maintenance_recovery_unavailable"):
        recovery.refresh_for_rotation()
    assert captured == []


def test_an_unfinished_recovery_keeps_only_that_account_out(tmp_path, monkeypatch):
    pending, healthy = sorted(str(uuid4()) for _ in range(2))
    state = tmp_path / "instances" / pending / "state"
    state.mkdir(parents=True)
    atomic_json(state / "maintenance-recovery.json", {"status": "queued"})
    recovery, captured = _recovery(tmp_path, monkeypatch, [pending, healthy], failing=set())

    recovery.refresh_for_rotation()

    assert captured == [healthy]
    assert not recovery.allows(pending)


def test_a_missing_ticket_reader_still_stops_the_whole_run(tmp_path, monkeypatch):
    recovery, captured = _recovery(tmp_path, monkeypatch, [str(uuid4())], failing=set())

    def unavailable():
        raise RuntimeError("history reader manifest mismatch")

    monkeypatch.setattr("windows_agent.maintenance_recovery.verify_ticket_reader", unavailable)
    with pytest.raises(MaintenanceRecoveryUnavailable):
        recovery.refresh_for_rotation()
    assert captured == []


def test_a_later_preflight_check_can_remove_one_account_from_tonight(tmp_path, monkeypatch):
    first, second = sorted(str(uuid4()) for _ in range(2))
    recovery, _captured = _recovery(tmp_path, monkeypatch, [first, second], failing=set())
    recovery.refresh_for_rotation()

    recovery.exclude(frozenset({second, str(uuid4())}))

    assert recovery.allows(first)
    assert not recovery.allows(second)
    assert recovery.skipped_connection_ids == frozenset({second})
    assert recovery.skip_reasons == {second: "release_state_unverifiable"}


def test_each_night_starts_without_the_previous_reasons(tmp_path, monkeypatch):
    first, second = sorted(str(uuid4()) for _ in range(2))
    recovery, _captured = _recovery(tmp_path, monkeypatch, [first, second], failing={second})
    recovery.refresh_for_rotation()
    recovery.exclude(frozenset({first}))
    assert recovery.skip_reasons == {
        first: "release_state_unverifiable", second: "ticket_baseline_unavailable",
    }

    monkeypatch.setattr("windows_agent.maintenance_recovery.capture_baseline", lambda *_args: None)
    recovery.refresh_for_rotation()

    assert recovery.skip_reasons == {}
    assert recovery.skipped_connection_ids == frozenset()
