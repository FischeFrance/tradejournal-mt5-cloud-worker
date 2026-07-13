from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from provisioning.filesystem_queue import FilesystemQueue, QueueCollisionError
from provisioning.locks import LockUnavailableError
from provisioning.models import Action, ProvisioningJob
from provisioning.state_store import StateStore


def _job(*, job_id: str | None = None, server: str = "Broker-Demo") -> ProvisioningJob:
    return ProvisioningJob(
        version=1,
        job_id=job_id or str(uuid4()),
        action=Action.PROVISION,
        connection_id=str(uuid4()),
        account_number="12345",
        server=server,
        tradejournal_api_url="https://example.invalid/events",
        created_at=datetime.now(timezone.utc).isoformat(),
    )


class FakeEngine:
    def __init__(self, root: Path, shutdown: threading.Event | None = None) -> None:
        self.config = SimpleNamespace(allow_insecure_http=False)
        self.state_store = StateStore(root / "state")
        self.calls: list[str] = []
        self.shutdown = shutdown

    def execute_job(self, job: ProvisioningJob) -> dict:
        self.calls.append(job.job_id)
        self.state_store.begin_job(job)
        result = {"connection_id": job.connection_id, "status": "active"}
        self.state_store.complete_job(job, result)
        if self.shutdown:
            self.shutdown.set()
        return result


def test_enqueue_is_atomic_idempotent_and_never_overwrites(tmp_path):
    queue = FilesystemQueue(tmp_path / "queue", FakeEngine(tmp_path))
    job = _job()
    path = queue.enqueue(job)
    assert queue.enqueue(job) == path
    changed = ProvisioningJob(**{**job.__dict__, "server": "Other-Broker"})
    with pytest.raises(QueueCollisionError):
        queue.enqueue(changed)
    assert json.loads(path.read_text(encoding="utf-8"))["server"] == "Broker-Demo"


def test_recovery_recreates_result_sidecar_from_completed_ledger(tmp_path):
    engine = FakeEngine(tmp_path)
    queue = FilesystemQueue(tmp_path / "queue", engine)
    job = _job()
    inbox = queue.enqueue(job)
    processing = queue.paths["processing"] / "crashed.processing.json"
    os.replace(inbox, processing)
    result = engine.execute_job(job)

    assert queue.recover_processing() == 1
    completed = queue.paths["completed"] / f"{job.job_id}.json"
    sidecar = queue.paths["completed"] / f"{job.job_id}.result.json"
    assert completed.exists()
    assert json.loads(sidecar.read_text(encoding="utf-8")) == result
    assert not processing.exists()


def test_recovery_preserves_conflicting_omonimi_in_failed(tmp_path):
    engine = FakeEngine(tmp_path)
    queue = FilesystemQueue(tmp_path / "queue", engine)
    first = _job()
    conflict = ProvisioningJob(**{**first.__dict__, "server": "Other-Broker"})
    inbox = queue.enqueue(first)
    processing = queue.paths["processing"] / "conflict.processing.json"
    processing.write_text(json.dumps(conflict.to_dict()), encoding="utf-8")

    assert queue.recover_processing() == 1
    assert inbox.exists()
    failed_jobs = [
        path
        for path in queue.paths["failed"].glob("*.failed.json")
        if not path.name.endswith(".error.json")
    ]
    assert len(failed_jobs) == 1
    assert json.loads(failed_jobs[0].read_text(encoding="utf-8"))["server"] == "Other-Broker"
    assert list(queue.paths["failed"].glob("*.error.json"))


def test_shutdown_is_checked_between_jobs(tmp_path):
    shutdown = threading.Event()
    engine = FakeEngine(tmp_path, shutdown)
    queue = FilesystemQueue(tmp_path / "queue", engine)
    queue.enqueue(_job())
    queue.enqueue(_job())
    assert queue.run_once(shutdown) == 1
    assert len(engine.calls) == 1
    assert len(queue._job_files(queue.paths["inbox"])) == 1


def test_filesystem_agent_singleton_lock_is_nonblocking(tmp_path):
    engine = FakeEngine(tmp_path)
    first = FilesystemQueue(tmp_path / "queue", engine)
    second = FilesystemQueue(tmp_path / "queue", engine)
    with first.singleton_lock.acquire():
        with pytest.raises(LockUnavailableError):
            second.run_forever(threading.Event())
