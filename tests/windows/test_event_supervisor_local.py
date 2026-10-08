from __future__ import annotations

import logging
import threading
import time
from unittest.mock import Mock, patch
from uuid import uuid4

import pytest

from windows_agent.event_supervisor import Mt5EventSupervisor
from windows_agent.state_store import atomic_json


@pytest.mark.parametrize("mode", ["history", None])
def test_live_consumer_refuses_frozen_history_even_with_connected_marker(tmp_path, mode):
    connection_id = str(uuid4())
    supervisor = Mt5EventSupervisor(tmp_path / "instances", tmp_path / "secrets", "https://example.invalid")
    adapter = Mock()
    adapter._heartbeat.return_value = {"history_mode": mode}
    with patch.object(supervisor.secrets, "read", side_effect=["42", "Demo", "token"]), patch("windows_agent.event_supervisor.Mql5FileMt5Adapter", return_value=adapter), patch("windows_agent.event_supervisor.TradingIngestionSink") as sink:
        with pytest.raises(RuntimeError, match="live_history_handoff_pending"):
            supervisor._process_connection(connection_id)
    sink.assert_not_called()


def _managed_instance(instances_root, secrets_root, connection_id, *, active=True):
    root = instances_root / connection_id
    (root / "state").mkdir(parents=True)
    if active:
        atomic_json(
            root / "state" / "job_progress.json",
            {"connection_id": connection_id, "status": "connected"},
        )
    secret_root = secrets_root / connection_id
    secret_root.mkdir(parents=True)
    for name in ("mt5_login", "mt5_server", "bridge_token"):
        (secret_root / f"{name}.dpapi").write_bytes(b"fixture")


def test_poll_once_processes_only_managed_connection_directories(tmp_path):
    instances_root = tmp_path / "instances"
    secrets_root = tmp_path / "secrets"
    first = str(uuid4())
    second = str(uuid4())
    _managed_instance(instances_root, secrets_root, first)
    _managed_instance(instances_root, secrets_root, second)
    (instances_root / "not-a-connection").mkdir()
    processed = []

    supervisor = Mt5EventSupervisor(
        instances_root,
        secrets_root,
        "https://example.invalid",
        processor=lambda connection_id: processed.append(connection_id) or 1,
    )

    assert supervisor.poll_once() == 2
    assert processed == sorted((first, second))


def test_poll_once_ignores_connection_until_history_handoff_is_active(tmp_path):
    instances_root = tmp_path / "instances"
    secrets_root = tmp_path / "secrets"
    pending = str(uuid4())
    active = str(uuid4())
    _managed_instance(instances_root, secrets_root, pending, active=False)
    _managed_instance(instances_root, secrets_root, active)
    processed = []
    supervisor = Mt5EventSupervisor(
        instances_root,
        secrets_root,
        "https://example.invalid",
        processor=lambda connection_id: processed.append(connection_id) or 1,
    )

    assert supervisor.poll_once() == 1
    assert processed == [active]


def test_run_stops_without_network_or_recurring_remote_heartbeat(tmp_path):
    connection_id = str(uuid4())
    instances_root = tmp_path / "instances"
    secrets_root = tmp_path / "secrets"
    _managed_instance(instances_root, secrets_root, connection_id)
    calls = []
    supervisor = Mt5EventSupervisor(
        instances_root,
        secrets_root,
        "https://example.invalid",
        poll_seconds=0.01,
        processor=lambda value: calls.append(value) or 0,
    )
    stop_event = threading.Event()
    thread = threading.Thread(target=supervisor.run, args=(stop_event,))

    thread.start()
    time.sleep(0.04)
    stop_event.set()
    thread.join(timeout=1)

    assert not thread.is_alive()
    assert len(calls) >= 2
    assert set(calls) == {connection_id}


def test_poll_once_logs_a_heartbeat_on_success_tagged_with_connection_id(tmp_path, caplog):
    instances_root = tmp_path / "instances"
    secrets_root = tmp_path / "secrets"
    connection_id = str(uuid4())
    _managed_instance(instances_root, secrets_root, connection_id)
    supervisor = Mt5EventSupervisor(
        instances_root,
        secrets_root,
        "https://example.invalid",
        processor=lambda value: 0,
    )

    with caplog.at_level(logging.INFO, logger="windows_agent.event_supervisor"):
        supervisor.poll_once()

    heartbeats = [r for r in caplog.records if "healthy" in r.message]
    assert len(heartbeats) == 1
    assert heartbeats[0].connection_id == connection_id


def test_poll_once_throttles_repeated_heartbeats_within_the_interval(tmp_path, caplog):
    instances_root = tmp_path / "instances"
    secrets_root = tmp_path / "secrets"
    connection_id = str(uuid4())
    _managed_instance(instances_root, secrets_root, connection_id)
    supervisor = Mt5EventSupervisor(
        instances_root,
        secrets_root,
        "https://example.invalid",
        processor=lambda value: 0,
    )

    with caplog.at_level(logging.INFO, logger="windows_agent.event_supervisor"):
        supervisor.poll_once()
        supervisor.poll_once()
        supervisor.poll_once()

    heartbeats = [r for r in caplog.records if "healthy" in r.message]
    assert len(heartbeats) == 1


def test_poll_once_does_not_log_a_heartbeat_when_processing_fails(tmp_path, caplog):
    instances_root = tmp_path / "instances"
    secrets_root = tmp_path / "secrets"
    connection_id = str(uuid4())
    _managed_instance(instances_root, secrets_root, connection_id)

    def failing_processor(value):
        raise RuntimeError("boom")

    supervisor = Mt5EventSupervisor(
        instances_root,
        secrets_root,
        "https://example.invalid",
        processor=failing_processor,
    )

    with caplog.at_level(logging.INFO, logger="windows_agent.event_supervisor"):
        supervisor.poll_once()

    heartbeats = [r for r in caplog.records if "healthy" in r.message]
    assert heartbeats == []


def test_run_keeps_polling_after_a_connection_processing_error(tmp_path):
    connection_id = str(uuid4())
    instances_root = tmp_path / "instances"
    secrets_root = tmp_path / "secrets"
    _managed_instance(instances_root, secrets_root, connection_id)
    calls = []

    def failing_processor(value):
        calls.append(value)
        raise RuntimeError("persistent local barrier")

    supervisor = Mt5EventSupervisor(
        instances_root,
        secrets_root,
        "https://example.invalid",
        poll_seconds=0.01,
        processor=failing_processor,
    )
    stop_event = threading.Event()
    thread = threading.Thread(target=supervisor.run, args=(stop_event,))

    thread.start()
    time.sleep(0.04)
    stop_event.set()
    thread.join(timeout=1)

    assert not thread.is_alive()
    assert len(calls) >= 2
    assert set(calls) == {connection_id}
