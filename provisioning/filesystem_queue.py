"""Coda filesystem crash-safe con recovery, no-clobber e agente singleton."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, Optional
from uuid import uuid4

from .engine import ProvisioningEngine
from .locks import SingletonLock
from .models import ProvisioningJob
from .state_store import atomic_create_json
from .validation import load_job


class QueueCollisionError(RuntimeError):
    """Una destinazione contiene gia' un artefatto diverso."""


class FilesystemQueue:
    QUEUE_NAMES = ("inbox", "processing", "completed", "failed")

    def __init__(self, root: Path, engine: ProvisioningEngine, poll_seconds: float = 2.0) -> None:
        self.root = Path(root)
        self.engine = engine
        self.poll_seconds = poll_seconds
        self.paths = {name: self.root / name for name in self.QUEUE_NAMES}
        for path in self.paths.values():
            if path.exists() and (path.is_symlink() or not path.is_dir()):
                raise ValueError(f"Directory queue non valida: {path}.")
            path.mkdir(parents=True, exist_ok=True, mode=0o750)
            path.chmod(0o750)
        self.singleton_lock = SingletonLock(self.root)

    @staticmethod
    def _job_files(directory: Path) -> list[Path]:
        return sorted(
            path
            for path in directory.glob("*.json")
            if path.is_file()
            and not path.is_symlink()
            and not path.name.endswith(".result.json")
            and not path.name.endswith(".error.json")
        )

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @classmethod
    def _remove(cls, path: Path) -> None:
        path.unlink()
        cls._fsync_directory(path.parent)

    @classmethod
    def _move_unique(cls, source: Path, directory: Path, suffix: str) -> Path:
        while True:
            destination = directory / f"{uuid4().hex}.{suffix}.json"
            if not os.path.lexists(destination):
                break
        os.replace(source, destination)
        cls._fsync_directory(source.parent)
        if destination.parent != source.parent:
            cls._fsync_directory(destination.parent)
        return destination

    @staticmethod
    def _read_sidecar(path: Path) -> Dict[str, Any]:
        if path.is_symlink() or not path.is_file():
            raise QueueCollisionError(f"Sidecar non regolare: {path.name}.")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise QueueCollisionError(f"Sidecar non leggibile: {path.name}.") from exc
        if not isinstance(payload, dict):
            raise QueueCollisionError(f"Sidecar non valido: {path.name}.")
        return payload

    def _ensure_sidecar(self, directory: str, stem: str, suffix: str, data: Dict[str, Any]) -> Path:
        target = self.paths[directory] / f"{stem}.{suffix}.json"
        if atomic_create_json(target, data, mode=0o640):
            return target
        if self._read_sidecar(target) != data:
            raise QueueCollisionError(f"Sidecar omonimo differente: {target.name}.")
        return target

    def _job_matches(self, path: Path, job: ProvisioningJob) -> bool:
        try:
            existing = load_job(
                path,
                allow_insecure_http=self.engine.config.allow_insecure_http,
            )
        except Exception:
            return False
        return existing.to_dict() == job.to_dict()

    def _publish_job(self, source: Path, queue: str, job: ProvisioningJob) -> Path:
        destination = self.paths[queue] / f"{job.job_id}.json"
        try:
            os.link(source, destination, follow_symlinks=False)
        except FileExistsError:
            if not self._job_matches(destination, job):
                raise QueueCollisionError(
                    f"Job omonimo differente gia' presente in {queue}: {destination.name}."
                )
        else:
            self._fsync_directory(destination.parent)
        return destination

    @staticmethod
    def _safe_error(exc: Exception) -> Dict[str, str]:
        message = "".join(char if char >= " " and char != "\x7f" else "?" for char in str(exc))
        return {"error_type": type(exc).__name__, "message": message[:1000]}

    def _fail_processing(self, processing: Path, exc: Exception) -> Path:
        failed = self._move_unique(processing, self.paths["failed"], "failed")
        self._ensure_sidecar("failed", failed.stem, "error", self._safe_error(exc))
        return failed

    def enqueue(self, job: ProvisioningJob) -> Path:
        """Pubblica atomicamente un job canonico, senza sostituire un omonimo."""

        destination = self.paths["inbox"] / f"{job.job_id}.json"
        if atomic_create_json(destination, job.to_dict(), mode=0o640):
            return destination
        if not self._job_matches(destination, job):
            raise QueueCollisionError(
                "job_id gia' presente nell'inbox con un contratto differente."
            )
        return destination

    def recover_processing(self, shutdown_event: Optional[threading.Event] = None) -> int:
        event = shutdown_event or threading.Event()
        recovered = 0
        for path in self._job_files(self.paths["processing"]):
            if event.is_set():
                break
            try:
                job = load_job(
                    path,
                    allow_insecure_http=self.engine.config.allow_insecure_http,
                )
                record = self.engine.state_store.load_job_record(job)
                if record and record.get("completed") is True:
                    result = record.get("result")
                    if not isinstance(result, dict):
                        raise ValueError("Ledger completato senza risultato JSON valido.")
                    self._ensure_sidecar("completed", job.job_id, "result", result)
                    self._publish_job(path, "completed", job)
                else:
                    self._publish_job(path, "inbox", job)
                self._remove(path)
            except Exception as exc:
                if path.exists():
                    self._fail_processing(path, exc)
            recovered += 1
        return recovered

    def process_one(self, shutdown_event: Optional[threading.Event] = None) -> bool:
        if shutdown_event is not None and shutdown_event.is_set():
            return False
        pending = self._job_files(self.paths["inbox"])
        if not pending:
            return False
        source = pending[0]
        try:
            processing = self._move_unique(source, self.paths["processing"], "processing")
        except FileNotFoundError:
            return False

        try:
            job = load_job(
                processing,
                allow_insecure_http=self.engine.config.allow_insecure_http,
            )
            result = self.engine.execute_job(job)
            self._ensure_sidecar("completed", job.job_id, "result", result)
            self._publish_job(processing, "completed", job)
            self._remove(processing)
        except Exception as exc:
            if processing.exists():
                self._fail_processing(processing, exc)
        return True

    def run_once(self, shutdown_event: Optional[threading.Event] = None) -> int:
        event = shutdown_event or threading.Event()
        processed = 0
        while not event.is_set() and self.process_one(event):
            processed += 1
        return processed

    def run_forever(self, shutdown_event: Optional[threading.Event] = None) -> None:
        event = shutdown_event or threading.Event()
        with self.singleton_lock.acquire():
            self.recover_processing(event)
            while not event.is_set():
                processed = self.run_once(event)
                if processed == 0:
                    event.wait(self.poll_seconds)
