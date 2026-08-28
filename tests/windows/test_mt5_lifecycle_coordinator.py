from __future__ import annotations

import threading
from pathlib import Path
from uuid import uuid4

from windows_agent.event_supervisor import Mt5EventSupervisor
from windows_agent.mt5_lifecycle import Mt5LifecycleCoordinator


def test_event_processing_waits_for_the_same_connection_lifecycle_lock(
    tmp_path: Path,
) -> None:
    connection_id = str(uuid4())
    lifecycle = Mt5LifecycleCoordinator()
    supervisor = Mt5EventSupervisor(
        tmp_path / "instances",
        tmp_path / "secrets",
        "https://example.invalid",
        lifecycle,
    )
    entered = threading.Event()
    finished = threading.Event()
    supervisor._process_locked = lambda _cid: (entered.set(), finished.set())  # type: ignore[method-assign]

    with lifecycle.connection(connection_id):
        worker = threading.Thread(target=supervisor._process, args=(connection_id,))
        worker.start()
        assert entered.wait(0.05) is False
        assert finished.is_set() is False

    worker.join(timeout=1)
    assert not worker.is_alive()
    assert entered.is_set()
    assert finished.is_set()


def test_different_connections_do_not_share_a_global_lock() -> None:
    lifecycle = Mt5LifecycleCoordinator()
    first = str(uuid4())
    second = str(uuid4())
    entered = threading.Event()

    def enter_second() -> None:
        with lifecycle.connection(second):
            entered.set()

    with lifecycle.connection(first):
        worker = threading.Thread(target=enter_second)
        worker.start()
        assert entered.wait(1)
    worker.join(timeout=1)
