"""Per-account, isolated worker directory and persistent worker state.

One ``AccountWorker`` corresponds to exactly one account: it owns a single
isolated directory tree, one persistent ``worker.json`` state file, and one
``OnboardingStateMachine`` instance. Nothing here starts a process or opens a
Job Object -- that is ``worker_supervisor.py``'s job, driven through the
existing Coordinator, never reimplemented here.

The state file never stores credentials: only the resolved ``host:port`` (a
public, non-secret value already read from the endpoint registry) is kept.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from enum import Enum, auto
from typing import Any
from uuid import uuid4

from .account_onboarding import FailureReason, OnboardingState, OnboardingStateMachine, OnboardingTrigger
from .credential_provider import CredentialProvider, json_dumps_safe
from .endpoint_registry import RegistryError, resolve_verified
from .mt5_dry_run import mt5_config_dry_run


_ACCOUNT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_SUBDIRS = ("config", "state", "logs", "session")
_STATE_SCHEMA_VERSION = 1


class WorkerError(ValueError):
    """Raised for isolation/state-file safety violations."""


class WorkerState(Enum):
    RUNNING = auto()
    STARTING = auto()
    LOGIN_REQUIRED = auto()
    HEALTHY = auto()
    DEGRADED = auto()
    RESTARTING = auto()
    FAILED_CLOSED = auto()
    STOPPED = auto()


def _validate_account_id(account_id: str) -> str:
    if not isinstance(account_id, str) or not _ACCOUNT_ID.fullmatch(account_id):
        raise WorkerError("account_id must match ^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
    return account_id


@dataclass(frozen=True)
class WorkerDirectory:
    """An isolated, single-account directory tree. Never shared between accounts."""

    account_id: str
    root: Path
    config: Path
    state: Path
    logs: Path
    session: Path

    @staticmethod
    def create(base_dir: str | Path, account_id: str) -> "WorkerDirectory":
        account_id = _validate_account_id(account_id)
        root = Path(base_dir) / account_id
        if root.exists():
            raise WorkerError(f"worker directory already exists, refusing to reuse or overwrite: {root}")
        root.mkdir(parents=True)
        subdirs = {name: root / name for name in _SUBDIRS}
        for path in subdirs.values():
            path.mkdir()
        return WorkerDirectory(account_id=account_id, root=root, **subdirs)

    @staticmethod
    def resume(base_dir: str | Path, account_id: str) -> "WorkerDirectory":
        account_id = _validate_account_id(account_id)
        root = Path(base_dir) / account_id
        subdirs = {name: root / name for name in _SUBDIRS}
        missing = [str(path) for path in (root, *subdirs.values()) if not path.is_dir()]
        if missing:
            raise WorkerError(f"worker directory is incomplete, cannot resume: {missing}")
        return WorkerDirectory(account_id=account_id, root=root, **subdirs)

    @property
    def worker_state_path(self) -> Path:
        return self.state / "worker.json"


@dataclass
class WorkerStateFile:
    worker_id: str
    account_id: str
    state: WorkerState
    registry_path: str
    broker_label: str
    restart_count: int = 0
    last_heartbeat_unix_ms: int | None = None
    onboarding_state: OnboardingState = OnboardingState.NEW
    failure_reason: FailureReason | None = None
    endpoint: str | None = None
    launcher_pid: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": _STATE_SCHEMA_VERSION,
            "worker_id": self.worker_id,
            "account_id": self.account_id,
            "state": self.state.name,
            "registry_path": self.registry_path,
            "broker_label": self.broker_label,
            "restart_count": self.restart_count,
            "last_heartbeat_unix_ms": self.last_heartbeat_unix_ms,
            "onboarding_state": self.onboarding_state.name,
            "failure_reason": self.failure_reason.name if self.failure_reason is not None else None,
            "endpoint": self.endpoint,
            "launcher_pid": self.launcher_pid,
        }

    @staticmethod
    def from_dict(payload: dict[str, Any]) -> "WorkerStateFile":
        if payload.get("schema_version") != _STATE_SCHEMA_VERSION:
            raise WorkerError("unsupported worker state schema")
        return WorkerStateFile(
            worker_id=payload["worker_id"],
            account_id=payload["account_id"],
            state=WorkerState[payload["state"]],
            registry_path=payload["registry_path"],
            broker_label=payload["broker_label"],
            restart_count=payload["restart_count"],
            last_heartbeat_unix_ms=payload["last_heartbeat_unix_ms"],
            onboarding_state=OnboardingState[payload["onboarding_state"]],
            failure_reason=FailureReason[payload["failure_reason"]] if payload.get("failure_reason") else None,
            endpoint=payload.get("endpoint"),
            launcher_pid=payload.get("launcher_pid"),
        )

    def save_atomic(self, path: str | Path) -> None:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json_dumps_safe(self.to_dict(), sort_keys=True, separators=(",", ":")))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        except Exception:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            raise

    @staticmethod
    def load(path: str | Path) -> "WorkerStateFile":
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorkerError("worker state file cannot be read") from exc
        return WorkerStateFile.from_dict(payload)


class AccountWorker:
    """Composes an isolated directory, persistent state, and onboarding FSM for one account."""

    def __init__(
        self,
        directory: WorkerDirectory,
        state_file: WorkerStateFile,
        onboarding: OnboardingStateMachine,
        credential_provider: CredentialProvider,
        registry_path: str | Path,
        broker_label: str,
    ) -> None:
        self.directory = directory
        self.state_file = state_file
        self.onboarding = onboarding
        self.credential_provider = credential_provider
        self.registry_path = registry_path
        self.broker_label = broker_label

    @classmethod
    def create(
        cls,
        base_dir: str | Path,
        account_id: str,
        *,
        credential_provider: CredentialProvider,
        registry_path: str | Path,
        broker_label: str,
    ) -> "AccountWorker":
        directory = WorkerDirectory.create(base_dir, account_id)
        state_file = WorkerStateFile(
            worker_id=str(uuid4()),
            account_id=account_id,
            state=WorkerState.STARTING,
            registry_path=str(registry_path),
            broker_label=broker_label,
        )
        worker = cls(directory, state_file, OnboardingStateMachine(), credential_provider, registry_path, broker_label)
        worker._persist()
        return worker

    @classmethod
    def resume(
        cls,
        base_dir: str | Path,
        account_id: str,
        *,
        credential_provider: CredentialProvider,
    ) -> "AccountWorker":
        """Reconstruct a previously created worker from its persisted state file.

        ``registry_path``/``broker_label`` are read back from the state file
        rather than re-supplied by the caller -- a worker always resolves
        against the same registry/broker it was created with.
        """
        directory = WorkerDirectory.resume(base_dir, account_id)
        state_file = WorkerStateFile.load(directory.worker_state_path)
        onboarding = OnboardingStateMachine()
        onboarding.current_state = state_file.onboarding_state
        onboarding.last_failure_reason = state_file.failure_reason
        return cls(directory, state_file, onboarding, credential_provider, state_file.registry_path, state_file.broker_label)

    @property
    def session_dir(self) -> Path:
        """One C012 session directory per worker, never shared with another account."""
        return self.directory.session

    def _persist(self) -> None:
        self.state_file.onboarding_state = self.onboarding.current_state
        self.state_file.failure_reason = self.onboarding.last_failure_reason
        self.state_file.save_atomic(self.directory.worker_state_path)

    def set_state(self, state: WorkerState) -> None:
        self.state_file.state = state
        self._persist()

    def set_launcher_pid(self, pid: int | None) -> None:
        """Record the OS PID of the currently-supervised launcher process (or ``None`` once
        stopped/never started). Not a credential -- a PID is public, non-secret, operational
        information -- persisted purely so a separate CLI process can verify no residual
        process is left behind after stop, without needing platform-specific process trees."""
        self.state_file.launcher_pid = pid
        self._persist()

    def heartbeat(self, now_unix_ms: int | None = None) -> None:
        self.state_file.last_heartbeat_unix_ms = int(time.time() * 1000) if now_unix_ms is None else now_unix_ms
        self._persist()

    def is_stale(self, max_age_seconds: float, now_unix_ms: int | None = None) -> bool:
        if self.state_file.last_heartbeat_unix_ms is None:
            return True
        now = int(time.time() * 1000) if now_unix_ms is None else now_unix_ms
        return (now - self.state_file.last_heartbeat_unix_ms) > max_age_seconds * 1000

    def apply_onboarding_trigger(self, trigger: OnboardingTrigger):
        result = self.onboarding.apply(trigger)
        self._persist()
        return result

    def resolve_endpoint(self, *, now_unix_ms: int | None = None):
        """Resolve via the real registry and drive the onboarding FSM accordingly.

        Never re-filters CANDIDATE/METAQUOTES_CDN/EXPIRED itself -- that
        filtering already happened inside ``resolve_verified``. No registry
        file at all is a missing endpoint; a registry that exists but fails
        to load/validate (corrupt JSON, tampered digest, schema mismatch) is
        an invalid configuration, not a missing one -- these are kept
        distinguishable per the FailureReason taxonomy.
        """
        if not Path(self.registry_path).exists():
            result = self.onboarding.apply(OnboardingTrigger.MISSING_ENDPOINT)
            self._persist()
            return result

        try:
            records = resolve_verified(self.registry_path, broker_label=self.broker_label, now_unix_ms=now_unix_ms)
        except RegistryError:
            result = self.onboarding.apply(OnboardingTrigger.INVALID_CONFIG)
            self._persist()
            return result

        result = self.onboarding.resolve_endpoint(records)
        if result.accepted and self.onboarding.current_state == OnboardingState.BROKER_ENDPOINT_VERIFIED:
            record = records[0]
            self.state_file.endpoint = f"{record['host']}:{record['port']}"
        self._persist()
        return result

    def generate_config_dry_run(self, *, now_unix_ms: int | None = None):
        """Context manager yielding a ``DryRunPlan``; delegates entirely to ``mt5_dry_run``.

        Never launches, never accepts or writes credentials.
        """
        return mt5_config_dry_run(
            self.registry_path,
            self.broker_label,
            directory=self.directory.session / "config-dry-run",
            now_unix_ms=now_unix_ms,
        )
