"""Orchestratore idempotente del lifecycle per una singola connection_id."""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import Any, Dict, Optional

from .compose_renderer import ComposeRenderer, RenderedCompose
from .config import ProvisioningConfig
from .docker_runner import DockerRunner
from .locks import ConnectionLockManager, JobLockManager
from .models import Action, InstanceState, InstanceStatus, ProvisioningJob
from .naming import project_name
from .secret_store import SecretStore
from .state_store import StateStore, utc_now
from .validation import validate_uuid


class ProvisioningError(RuntimeError):
    pass


class ProvisioningEngine:
    def __init__(
        self,
        config: ProvisioningConfig,
        *,
        state_store: Optional[StateStore] = None,
        secret_store: Optional[SecretStore] = None,
        lock_manager: Optional[ConnectionLockManager] = None,
        job_lock_manager: Optional[JobLockManager] = None,
        renderer: Optional[ComposeRenderer] = None,
        docker: Optional[DockerRunner] = None,
    ) -> None:
        self.config = config
        self.config.ensure_host_directories()
        self.state_store = state_store or StateStore(config.state_root)
        self.secret_store = secret_store or SecretStore(
            config.secrets_root, expected_owner_uid=config.secret_owner_uid
        )
        self.lock_manager = lock_manager or ConnectionLockManager(config.locks_root)
        self.job_lock_manager = job_lock_manager or JobLockManager(config.locks_root / "jobs")
        self.renderer = renderer or ComposeRenderer(config)
        self.docker = docker or DockerRunner(timeout_seconds=config.docker_timeout_seconds)

    def _rendered_for(self, connection_id: str) -> RenderedCompose:
        project = project_name(connection_id)
        directory = self.config.instances_root / project
        return RenderedCompose(
            project_name=project,
            instance_dir=directory,
            compose_file=directory / "compose.yaml",
            env_file=directory / "instance.env",
        )

    @staticmethod
    def _configuration_state(rendered: RenderedCompose) -> str:
        directory = rendered.instance_dir
        if directory.is_symlink():
            return "unsafe"
        compose_ok = rendered.compose_file.is_file() and not rendered.compose_file.is_symlink()
        env_ok = rendered.env_file.is_file() and not rendered.env_file.is_symlink()
        if compose_ok and env_ok:
            return "complete"
        if not rendered.compose_file.exists() and not rendered.env_file.exists():
            return "missing"
        return "partial"

    @staticmethod
    def _services_complete(docker_status: Dict[str, Any]) -> bool:
        services = docker_status.get("services")
        if not isinstance(services, list) or not services:
            return False
        names = {
            str(service.get("Service"))
            for service in services
            if isinstance(service, dict) and service.get("Service")
        }
        return {"mt5-runtime", "worker"}.issubset(names) if names else len(services) >= 2

    @classmethod
    def _services_running(cls, docker_status: Dict[str, Any]) -> bool:
        services = docker_status.get("services")
        if not cls._services_complete(docker_status) or not isinstance(services, list):
            return False
        return all(
            isinstance(service, dict)
            and str(service.get("State", "")).lower() == "running"
            and str(service.get("Health", "")).lower() in {"", "healthy"}
            for service in services
        )

    @staticmethod
    def _services_stopped(docker_status: Dict[str, Any]) -> bool:
        services = docker_status.get("services")
        if not isinstance(services, list) or not services:
            return True
        return all(
            isinstance(service, dict)
            and str(service.get("State", "")).lower() in {"created", "exited", "stopped"}
            for service in services
        )

    def _template_checksum(self) -> str:
        if self.config.runtime_target == "mock":
            return "mock-runtime-no-golden-template"
        archive = self.config.mt5_template_archive
        if archive.is_symlink() or not archive.is_file():
            raise ProvisioningError(f"Golden template assente o non regolare: {archive}.")
        expected = self.config.mt5_template_sha256
        checksum_file = Path(f"{archive}.sha256")
        if not expected and checksum_file.is_file() and not checksum_file.is_symlink():
            expected = checksum_file.read_text(encoding="utf-8").strip().split()[0].lower()
        if len(expected) != 64 or any(char not in "0123456789abcdef" for char in expected):
            raise ProvisioningError(
                "Checksum golden template mancante/non valido; configurare MT5_TEMPLATE_SHA256 "
                f"o {checksum_file}."
            )
        digest = hashlib.sha256()
        with archive.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        actual = digest.hexdigest()
        if actual != expected:
            raise ProvisioningError("Checksum golden template non corrispondente.")
        return actual

    @staticmethod
    def _public_result(state: InstanceState, **extra: Any) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "connection_id": state.connection_id,
            "project_name": state.project_name,
            "status": state.status.value,
        }
        result.update(extra)
        return result

    def _save_status(
        self,
        connection_id: str,
        status: InstanceStatus,
        *,
        previous: Optional[InstanceState] = None,
        job: Optional[ProvisioningJob] = None,
        error: Optional[str] = None,
    ) -> InstanceState:
        state = InstanceState(
            connection_id=connection_id,
            project_name=project_name(connection_id),
            status=status,
            updated_at=utc_now(),
            account_number=(job.account_number if job else None)
            or (previous.account_number if previous else None),
            server=(job.server if job else None) or (previous.server if previous else None),
            tradejournal_api_url=(job.tradejournal_api_url if job else None)
            or (previous.tradejournal_api_url if previous else None),
            last_job_id=(job.job_id if job else None)
            or (previous.last_job_id if previous else None),
            error=error,
        )
        self.state_store.save_instance(state)
        return state

    def _assert_same_identity(self, existing: InstanceState, job: ProvisioningJob) -> None:
        expected = (existing.account_number, existing.server, existing.tradejournal_api_url)
        requested = (job.account_number, job.server, job.tradejournal_api_url)
        if expected != requested:
            raise ProvisioningError(
                "connection_id gia' associato a una configurazione diversa; deprovisionare "
                "prima di riutilizzarlo."
            )

    def _provision(self, job: ProvisioningJob) -> Dict[str, Any]:
        if job.action is not Action.PROVISION:
            raise ProvisioningError("provision() richiede action=provision.")
        with self.lock_manager.acquire(job.connection_id):
            previous = self.state_store.load_instance(job.connection_id)
            if previous and previous.status is not InstanceStatus.DELETED:
                self._assert_same_identity(previous, job)
            already_known = bool(previous and previous.status is not InstanceStatus.DELETED)

            self._save_status(
                job.connection_id,
                InstanceStatus.PENDING,
                previous=previous,
                job=job,
            )
            try:
                secrets = self.secret_store.validate_connection(job.connection_id)
                checksum = self._template_checksum()
                self._save_status(
                    job.connection_id,
                    InstanceStatus.PROCESSING,
                    previous=previous,
                    job=job,
                )
                rendered = self.renderer.render(job, secrets, template_sha256=checksum)
                self.docker.provision(rendered)
                state = self._save_status(
                    job.connection_id,
                    InstanceStatus.ACTIVE,
                    previous=previous,
                    job=job,
                )
            except Exception as exc:
                self._save_status(
                    job.connection_id,
                    InstanceStatus.ERROR,
                    previous=previous,
                    job=job,
                    error=f"{type(exc).__name__}: {exc}",
                )
                raise
            result = self._public_result(state, idempotent=False)
            result["idempotent"] = already_known
            return result

    def provision(self, job: ProvisioningJob) -> Dict[str, Any]:
        if job.action is not Action.PROVISION:
            raise ProvisioningError("provision() richiede action=provision.")
        return self.execute_job(job)

    def _require_instance(self, connection_id: str) -> InstanceState:
        canonical = validate_uuid(connection_id, "connection_id")
        state = self.state_store.load_instance(canonical)
        if state is None or state.status is InstanceStatus.DELETED:
            raise ProvisioningError("Istanza non provisionata o gia' eliminata.")
        return state

    def start(self, connection_id: str) -> Dict[str, Any]:
        canonical = validate_uuid(connection_id, "connection_id")
        with self.lock_manager.acquire(canonical):
            state = self._require_instance(canonical)
            rendered = self._rendered_for(canonical)
            configuration = self._configuration_state(rendered)
            if configuration != "complete":
                raise ProvisioningError(
                    f"Configurazione istanza {configuration}; impossibile riconciliare start."
                )
            self.secret_store.validate_connection(canonical)
            try:
                observed = self.docker.status(rendered)
                if self._services_running(observed):
                    state = self._save_status(canonical, InstanceStatus.ACTIVE, previous=state)
                    return self._public_result(state, idempotent=True, reconciled=True)
                self._save_status(canonical, InstanceStatus.PROCESSING, previous=state)
                if self._services_complete(observed):
                    self.docker.start(rendered)
                else:
                    # I file persistiti sono sufficienti a ricreare servizi mancanti senza
                    # ricostruire il job o leggere secret nel processo host.
                    self.docker.provision(rendered)
                state = self._save_status(canonical, InstanceStatus.ACTIVE, previous=state)
            except Exception as exc:
                self._save_status(
                    canonical,
                    InstanceStatus.ERROR,
                    previous=state,
                    error=f"{type(exc).__name__}: {exc}",
                )
                raise
            return self._public_result(state)

    def stop(self, connection_id: str) -> Dict[str, Any]:
        canonical = validate_uuid(connection_id, "connection_id")
        with self.lock_manager.acquire(canonical):
            state = self._require_instance(canonical)
            rendered = self._rendered_for(canonical)
            configuration = self._configuration_state(rendered)
            if configuration != "complete":
                raise ProvisioningError(
                    f"Configurazione istanza {configuration}; impossibile riconciliare stop."
                )
            try:
                observed = self.docker.status(rendered)
                if self._services_stopped(observed):
                    state = self._save_status(canonical, InstanceStatus.STOPPED, previous=state)
                    return self._public_result(state, idempotent=True, reconciled=True)
                self.docker.stop(rendered)
                state = self._save_status(canonical, InstanceStatus.STOPPED, previous=state)
            except Exception as exc:
                self._save_status(
                    canonical,
                    InstanceStatus.ERROR,
                    previous=state,
                    error=f"{type(exc).__name__}: {exc}",
                )
                raise
            return self._public_result(state)

    def restart(self, connection_id: str) -> Dict[str, Any]:
        canonical = validate_uuid(connection_id, "connection_id")
        with self.lock_manager.acquire(canonical):
            state = self._require_instance(canonical)
            rendered = self._rendered_for(canonical)
            configuration = self._configuration_state(rendered)
            if configuration != "complete":
                raise ProvisioningError(
                    f"Configurazione istanza {configuration}; impossibile riconciliare restart."
                )
            self.secret_store.validate_connection(canonical)
            try:
                observed = self.docker.status(rendered)
                state = self._save_status(
                    canonical, InstanceStatus.PROCESSING, previous=state
                )
                if self._services_complete(observed):
                    self.docker.restart(rendered)
                else:
                    self.docker.provision(rendered)
                state = self._save_status(canonical, InstanceStatus.ACTIVE, previous=state)
            except Exception as exc:
                self._save_status(
                    canonical,
                    InstanceStatus.ERROR,
                    previous=state,
                    error=f"{type(exc).__name__}: {exc}",
                )
                raise
            return self._public_result(state)

    def status(self, connection_id: str) -> Dict[str, Any]:
        canonical = validate_uuid(connection_id, "connection_id")
        with self.lock_manager.acquire(canonical):
            state = self.state_store.load_instance(canonical)
            if state is None:
                raise ProvisioningError("Istanza sconosciuta.")
            docker_status: Dict[str, Any] = {"services": []}
            if state.status is not InstanceStatus.DELETED:
                rendered = self._rendered_for(canonical)
                configuration = self._configuration_state(rendered)
                if configuration == "complete":
                    docker_status = self.docker.status(rendered)
                else:
                    docker_status = {
                        "services": [],
                        "available": False,
                        "reason": f"configuration_{configuration}",
                    }
            return self._public_result(state, docker=docker_status)

    def _delete_instance_files(self, connection_id: str) -> None:
        directory = self._rendered_for(connection_id).instance_dir
        root = self.config.instances_root.resolve()
        if not directory.exists():
            return
        if directory.is_symlink() or not directory.is_dir() or root not in directory.resolve().parents:
            raise ProvisioningError("Rifiutata rimozione di un instance path non sicuro.")
        shutil.rmtree(directory)

    def deprovision(self, connection_id: str) -> Dict[str, Any]:
        canonical = validate_uuid(connection_id, "connection_id")
        with self.lock_manager.acquire(canonical):
            state = self.state_store.load_instance(canonical)
            rendered = self._rendered_for(canonical)
            configuration = self._configuration_state(rendered)
            if state and state.status is InstanceStatus.DELETED:
                if configuration == "unsafe":
                    raise ProvisioningError(
                        "Instance path non sicuro; cleanup manuale richiesto."
                    )
                self.secret_store.delete_connection(canonical)
                self._delete_instance_files(canonical)
                return self._public_result(state, idempotent=True)
            if configuration != "complete":
                # Senza entrambi i file non possiamo dimostrare che `docker compose down -v` abbia
                # rimosso tutte le risorse. Conservare metadata e secret rende possibile il
                # recovery manuale invece di dichiarare falsamente l'istanza eliminata.
                raise ProvisioningError(
                    f"Configurazione istanza {configuration}; deprovision rifiutato per evitare "
                    "risorse Docker orfane. Secret e stato sono stati conservati."
                )
            if state is None:
                state = self._save_status(canonical, InstanceStatus.DELETING)
            else:
                state = self._save_status(canonical, InstanceStatus.DELETING, previous=state)
            try:
                self.docker.deprovision(rendered)
                self.secret_store.delete_connection(canonical)
            except Exception as exc:
                self._save_status(
                    canonical,
                    InstanceStatus.ERROR,
                    previous=state,
                    error=f"{type(exc).__name__}: {exc}",
                )
                raise
            # La configurazione Compose resta disponibile finche' il successo di Docker e la
            # rimozione dei secret non sono stati registrati atomicamente. Se il cleanup finale
            # fallisce, un retry vede DELETED e completa soltanto la rimozione dei file host.
            state = self._save_status(canonical, InstanceStatus.DELETED, previous=state)
            self._delete_instance_files(canonical)
            return self._public_result(state)

    def execute_job(self, job: ProvisioningJob) -> Dict[str, Any]:
        with self.job_lock_manager.acquire(job.job_id):
            record = self.state_store.begin_job(job)
            if record.get("completed") is True:
                result = record.get("result")
                if not isinstance(result, dict):
                    raise ProvisioningError("Job ledger completato senza risultato valido.")
                return dict(result)
            if job.action is Action.PROVISION:
                result = self._provision(job)
            else:
                operations = {
                    Action.START: self.start,
                    Action.STOP: self.stop,
                    Action.RESTART: self.restart,
                    Action.STATUS: self.status,
                    Action.DEPROVISION: self.deprovision,
                }
                result = operations[job.action](job.connection_id)
            self.state_store.complete_job(job, result)
            return result
