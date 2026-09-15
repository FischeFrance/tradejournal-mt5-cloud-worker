from __future__ import annotations

import threading
import time
from uuid import uuid4

from windows_agent.event_supervisor import Mt5EventSupervisor


def _managed_instance(instances_root, secrets_root, connection_id):
    (instances_root / connection_id).mkdir(parents=True)
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
