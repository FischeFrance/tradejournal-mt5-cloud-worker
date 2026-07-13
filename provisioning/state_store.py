"""Persistenza atomica di stati istanza e ledger dei job completati."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Union

from .models import InstanceState, ProvisioningJob
from .naming import connection_slug
from .validation import validate_uuid


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_write_json(path: Path, payload: Dict[str, Any], mode: int = 0o640) -> None:
    """Scrive JSON e directory entry in modo crash-safe, senza seguire symlink."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    if path.exists() and path.is_symlink():
        raise ValueError(f"Rifiutato state path symlink: {path}.")
    descriptor, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def atomic_create_json(path: Path, payload: Dict[str, Any], mode: int = 0o640) -> bool:
    """Pubblica JSON completo senza sovrascrivere una destinazione esistente.

    Il link finale e' atomico e fallisce con ``False`` in caso di collisione. Scrivere prima un
    file temporaneo evita che il filesystem agent osservi un JSON parziale.
    """

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    descriptor, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(tmp_path, path, follow_symlinks=False)
        except FileExistsError:
            return False
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return True
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


class JobLedgerConflictError(ValueError):
    """Un job_id e' gia' legato a un contratto o risultato differente."""


class StateStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.instances = self.root / "instances"
        self.jobs = self.root / "jobs"
        self.instances.mkdir(parents=True, exist_ok=True, mode=0o750)
        self.jobs.mkdir(parents=True, exist_ok=True, mode=0o750)

    def instance_path(self, connection_id: str) -> Path:
        return self.instances / f"{connection_slug(connection_id)}.json"

    def job_path(self, job_id: str) -> Path:
        canonical = validate_uuid(job_id, "job_id")
        return self.jobs / f"{canonical}.json"

    def load_instance(self, connection_id: str) -> Optional[InstanceState]:
        path = self.instance_path(connection_id)
        if not path.exists():
            return None
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"State istanza non regolare: {path}.")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"State istanza non leggibile: {path}.") from exc
        return InstanceState.from_dict(data)

    def save_instance(self, state: InstanceState) -> None:
        atomic_write_json(self.instance_path(state.connection_id), state.to_dict())

    def load_job_result(self, job_id: str) -> Optional[Dict[str, Any]]:
        path = self.job_path(job_id)
        if not path.exists():
            return None
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Job ledger non regolare: {path}.")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Job ledger non leggibile: {path}.") from exc
        if not isinstance(data, dict):
            raise ValueError(f"Job ledger non valido: {path}.")
        return data

    @staticmethod
    def _contract(job: ProvisioningJob) -> Dict[str, Any]:
        return job.to_dict()

    def _assert_job_contract(
        self, job: ProvisioningJob, record: Dict[str, Any]
    ) -> Dict[str, Any]:
        expected = self._contract(job)
        if record.get("job_id") != job.job_id or record.get("contract") != expected:
            raise JobLedgerConflictError(
                "job_id gia' associato a un contratto differente; usare un nuovo UUID."
            )
        return record

    def load_job_record(self, job: ProvisioningJob) -> Optional[Dict[str, Any]]:
        record = self.load_job_result(job.job_id)
        return None if record is None else self._assert_job_contract(job, record)

    def begin_job(self, job: ProvisioningJob) -> Dict[str, Any]:
        """Lega durevolmente il job_id al contratto prima di qualsiasi side effect."""

        existing = self.load_job_record(job)
        if existing is not None:
            return existing
        payload = {
            "job_id": job.job_id,
            "contract": self._contract(job),
            "completed": False,
            "started_at": utc_now(),
        }
        if atomic_create_json(self.job_path(job.job_id), payload):
            return payload
        # Un altro processo puo' aver vinto la pubblicazione anche senza il lock dell'engine.
        raced = self.load_job_record(job)
        if raced is None:  # pragma: no cover - impossibile salvo filesystem non conforme
            raise ValueError("Job ledger scomparso durante la creazione atomica.")
        return raced

    def job_is_completed(self, job: Union[ProvisioningJob, str]) -> bool:
        if isinstance(job, ProvisioningJob):
            result = self.load_job_record(job)
        else:
            result = self.load_job_result(validate_uuid(job, "job_id"))
        return bool(result and result.get("completed") is True)

    def complete_job(self, job: ProvisioningJob, result: Dict[str, Any]) -> Dict[str, Any]:
        record = self.begin_job(job)
        if record.get("completed") is True:
            if record.get("result") != result:
                raise JobLedgerConflictError(
                    "job_id gia' completato con un risultato differente."
                )
            return record
        payload = dict(record)
        payload.update(
            {
                "completed": True,
                "completed_at": utc_now(),
                "result": result,
            }
        )
        atomic_write_json(self.job_path(job.job_id), payload)
        return payload
