from __future__ import annotations

import json
import os
import subprocess
import threading
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from provisioning.compose_renderer import ComposeRenderer, RenderedCompose
from provisioning.config import ProvisioningConfig, load_config
from provisioning.docker_runner import DockerRunner
from provisioning.engine import ProvisioningEngine, ProvisioningError
from provisioning.locks import ConnectionLockManager
from provisioning.models import Action, InstanceState, InstanceStatus, ProvisioningJob
from provisioning.naming import network_name, project_name
from provisioning.secret_store import SECRET_NAMES, SecretStore, SecretStoreError
from provisioning.state_store import (
    JobLedgerConflictError,
    StateStore,
    atomic_write_json,
    utc_now,
)
from provisioning.validation import ValidationError, validate_job_data, validate_tradejournal_url


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent


def _config(tmp_path: Path, **overrides) -> ProvisioningConfig:
    values = {
        "repository_root": REPOSITORY_ROOT,
        "instances_root": tmp_path / "instances",
        "state_root": tmp_path / "state",
        "locks_root": tmp_path / "locks",
        "secrets_root": tmp_path / "secrets",
        "queue_root": tmp_path / "queue",
        "compose_template": REPOSITORY_ROOT / "deploy/instance/compose.yaml",
        "mt5_template_archive": tmp_path / "missing-template.tar.zst",
        "mt5_template_sha256": "",
        "runtime_target": "mock",
        "secret_owner_uid": os.getuid(),
    }
    values.update(overrides)
    return ProvisioningConfig(**values)


def _job(
    *,
    action: Action = Action.PROVISION,
    job_id: str | None = None,
    connection_id: str | None = None,
) -> ProvisioningJob:
    return ProvisioningJob(
        version=1,
        job_id=job_id or str(uuid4()),
        action=action,
        connection_id=connection_id or str(uuid4()),
        account_number="12345" if action is Action.PROVISION else None,
        server="Broker-Demo" if action is Action.PROVISION else None,
        tradejournal_api_url="https://example.invalid/events"
        if action is Action.PROVISION
        else None,
        created_at=datetime.now(timezone.utc).isoformat(),
    )


def _raw_job() -> dict:
    return _job().to_dict()


def _write_secrets(engine: ProvisioningEngine, connection_id: str) -> None:
    for name in SECRET_NAMES:
        engine.secret_store.write_for_local_test(connection_id, name, f"fake-{name}")


class FakeDocker:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.services: list[dict] = []
        self.restart_entered: threading.Event | None = None
        self.restart_release: threading.Event | None = None

    def provision(self, _rendered) -> None:
        self.calls.append("provision")
        self.services = [
            {"Service": "mt5-runtime", "State": "running", "Health": "healthy"},
            {"Service": "worker", "State": "running", "Health": "healthy"},
        ]

    def start(self, _rendered) -> None:
        self.calls.append("start")
        self.provision(_rendered)

    def stop(self, _rendered) -> None:
        self.calls.append("stop")
        self.services = [
            {"Service": "mt5-runtime", "State": "exited"},
            {"Service": "worker", "State": "exited"},
        ]

    def restart(self, _rendered) -> None:
        self.calls.append("restart")
        if self.restart_entered:
            self.restart_entered.set()
        if self.restart_release:
            assert self.restart_release.wait(5)

    def deprovision(self, _rendered) -> None:
        self.calls.append("deprovision")
        self.services = []

    def status(self, _rendered) -> dict:
        self.calls.append("status")
        return {"services": list(self.services)}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("version", True),
        ("job_id", "../not-a-uuid"),
        ("connection_id", "not-a-uuid"),
        ("action", "destroy"),
        ("account_number", "12;rm -rf"),
        ("server", "Broker\nInjected"),
        ("server", "../Broker"),
        ("tradejournal_api_url", "http://example.com/events"),
        ("tradejournal_api_url", "https://example.com/%2e%2e/events"),
        ("tradejournal_api_url", "https://example.com/events%0aInjected"),
    ],
)
def test_job_validation_rejects_unsafe_contract(field, value):
    raw = _raw_job()
    raw[field] = value
    with pytest.raises(ValidationError):
        validate_job_data(raw, allow_insecure_http=True)


def test_http_test_mode_is_loopback_only():
    assert (
        validate_tradejournal_url("http://127.0.0.1:8080/events", allow_insecure_http=True)
        == "http://127.0.0.1:8080/events"
    )
    with pytest.raises(ValidationError, match="localhost/loopback"):
        validate_tradejournal_url("http://example.com/events", allow_insecure_http=True)


@pytest.mark.parametrize("value", ["nan", "inf", "-inf", "0", "-1"])
def test_config_rejects_non_finite_or_non_positive_floats(value):
    with pytest.raises(ValueError):
        load_config({"TJ_PROVISIONING_ROOT": "/tmp/tj", "TJ_FILESYSTEM_POLL_SECONDS": value})


def test_worker_smoke_defaults_and_overrides(tmp_path):
    default = load_config({"TJ_PROVISIONING_ROOT": str(tmp_path)})
    assert default.worker_dry_run is True
    assert default.worker_poll_seconds == 5
    changed = load_config(
        {
            "TJ_PROVISIONING_ROOT": str(tmp_path),
            "TJ_WORKER_DRY_RUN": "false",
            "TJ_WORKER_POLL_SECONDS": "9",
        }
    )
    assert changed.worker_dry_run is False
    assert changed.worker_poll_seconds == 9
    with pytest.raises(ValueError):
        load_config(
            {"TJ_PROVISIONING_ROOT": str(tmp_path), "TJ_WORKER_POLL_SECONDS": "0"}
        )


@pytest.mark.parametrize("name", ["TJ_WORKER_DRY_RUN", "TJ_ALLOW_INSECURE_HTTP"])
def test_security_booleans_fail_closed_on_typo(tmp_path, name):
    with pytest.raises(ValueError, match=name):
        load_config({"TJ_PROVISIONING_ROOT": str(tmp_path), name: "tru"})


def test_config_rejects_secret_uid_that_disagrees_with_container_images(tmp_path):
    with pytest.raises(ValueError, match="deve essere 1000"):
        load_config(
            {
                "TJ_PROVISIONING_ROOT": str(tmp_path),
                "TJ_SECRET_OWNER_UID": "1001",
            }
        )


def test_empty_terminal_path_uses_safe_default(tmp_path):
    config = load_config(
        {"TJ_PROVISIONING_ROOT": str(tmp_path), "MT5_TERMINAL_PATH": ""}
    )
    assert config.mt5_terminal_path == r"C:\Program Files\MetaTrader 5\terminal64.exe"


def test_mock_render_uses_dev_null_and_explicit_worker_settings(tmp_path):
    config = _config(tmp_path, worker_dry_run=True, worker_poll_seconds=7)
    job = _job()
    secret_dir = tmp_path / "secret-input"
    secret_dir.mkdir()
    paths = {name: secret_dir / name for name in SECRET_NAMES}
    rendered = ComposeRenderer(config).render(
        job, paths, template_sha256="mock-runtime-no-golden-template"
    )
    env_text = rendered.env_file.read_text(encoding="utf-8")
    assert "MT5_TEMPLATE_ARCHIVE='/dev/null'" in env_text
    assert "DRY_RUN='true'" in env_text
    assert "POLL_INTERVAL_SECONDS='7'" in env_text


def test_render_rejects_preexisting_instance_symlink(tmp_path):
    config = _config(tmp_path)
    job = _job()
    outside = tmp_path / "outside"
    outside.mkdir()
    project_path = config.instances_root / project_name(job.connection_id)
    config.instances_root.mkdir()
    project_path.symlink_to(outside, target_is_directory=True)
    secret_dir = tmp_path / "secret-input"
    secret_dir.mkdir()
    paths = {name: secret_dir / name for name in SECRET_NAMES}

    with pytest.raises(ValueError, match="Instance path"):
        ComposeRenderer(config).render(
            job, paths, template_sha256="mock-runtime-no-golden-template"
        )


def test_naming_is_deterministic_and_uuid_only():
    connection_id = "11111111-2222-4333-8444-555555555555"
    assert project_name(connection_id) == "tjmt5-11111111222243338444555555555555"
    assert network_name(connection_id).startswith(project_name(connection_id))
    with pytest.raises(ValidationError):
        project_name("../../unsafe")


def test_atomic_state_write_replaces_complete_json(tmp_path):
    path = tmp_path / "state" / "instance.json"
    atomic_write_json(path, {"value": 1})
    atomic_write_json(path, {"value": 2})
    assert json.loads(path.read_text(encoding="utf-8")) == {"value": 2}
    assert not list(path.parent.glob(f".{path.name}.*"))


def test_connection_lock_serializes_threads(tmp_path):
    manager = ConnectionLockManager(tmp_path / "locks")
    connection_id = str(uuid4())
    entered = threading.Event()
    release = threading.Event()
    order: list[str] = []

    def first():
        with manager.acquire(connection_id):
            order.append("first")
            entered.set()
            assert release.wait(5)

    def second():
        assert entered.wait(5)
        with manager.acquire(connection_id):
            order.append("second")

    one = threading.Thread(target=first)
    two = threading.Thread(target=second)
    one.start()
    two.start()
    assert entered.wait(5)
    assert order == ["first"]
    release.set()
    one.join(5)
    two.join(5)
    assert order == ["first", "second"]


def test_ledger_binds_job_id_to_entire_contract(tmp_path):
    store = StateStore(tmp_path / "state")
    job = _job()
    store.begin_job(job)
    changed = replace(job, server="Other-Broker")
    with pytest.raises(JobLedgerConflictError, match="contratto differente"):
        store.begin_job(changed)


def test_provision_reconciles_new_job_but_same_job_is_not_reexecuted(tmp_path):
    docker = FakeDocker()
    engine = ProvisioningEngine(_config(tmp_path), docker=docker)
    first = _job()
    _write_secrets(engine, first.connection_id)
    assert engine.execute_job(first)["idempotent"] is False
    assert engine.execute_job(first)["idempotent"] is False
    assert docker.calls.count("provision") == 1

    second = replace(first, job_id=str(uuid4()), created_at=datetime.now(timezone.utc).isoformat())
    assert engine.execute_job(second)["idempotent"] is True
    assert docker.calls.count("provision") == 2


def test_job_lock_prevents_concurrent_restart_reexecution(tmp_path):
    docker = FakeDocker()
    engine = ProvisioningEngine(_config(tmp_path), docker=docker)
    provision = _job()
    _write_secrets(engine, provision.connection_id)
    engine.execute_job(provision)
    restart = _job(action=Action.RESTART, connection_id=provision.connection_id)
    docker.restart_entered = threading.Event()
    docker.restart_release = threading.Event()
    results: list[dict] = []

    threads = [threading.Thread(target=lambda: results.append(engine.execute_job(restart))) for _ in range(2)]
    for thread in threads:
        thread.start()
    assert docker.restart_entered.wait(5)
    docker.restart_release.set()
    for thread in threads:
        thread.join(5)
    assert len(results) == 2
    assert docker.calls.count("restart") == 1


def test_start_reconciles_docker_instead_of_trusting_active_state(tmp_path):
    docker = FakeDocker()
    engine = ProvisioningEngine(_config(tmp_path), docker=docker)
    job = _job()
    _write_secrets(engine, job.connection_id)
    engine.execute_job(job)
    docker.services = [
        {"Service": "mt5-runtime", "State": "exited"},
        {"Service": "worker", "State": "exited"},
    ]
    engine.start(job.connection_id)
    assert "start" in docker.calls


def test_restart_recreates_missing_services(tmp_path):
    docker = FakeDocker()
    engine = ProvisioningEngine(_config(tmp_path), docker=docker)
    job = _job()
    _write_secrets(engine, job.connection_id)
    engine.execute_job(job)
    docker.services = []

    engine.restart(job.connection_id)

    assert docker.calls[-2:] == ["status", "provision"]


def test_status_error_without_compose_is_non_throwing(tmp_path):
    docker = FakeDocker()
    engine = ProvisioningEngine(_config(tmp_path), docker=docker)
    connection_id = str(uuid4())
    engine.state_store.save_instance(
        InstanceState(
            connection_id=connection_id,
            project_name=project_name(connection_id),
            status=InstanceStatus.ERROR,
            updated_at=utc_now(),
            error="pre-render failure",
        )
    )
    result = engine.status(connection_id)
    assert result["docker"]["reason"] == "configuration_missing"
    assert "status" not in docker.calls


def test_deprovision_refuses_missing_config_and_preserves_secrets(tmp_path):
    docker = FakeDocker()
    engine = ProvisioningEngine(_config(tmp_path), docker=docker)
    connection_id = str(uuid4())
    _write_secrets(engine, connection_id)
    engine.state_store.save_instance(
        InstanceState(
            connection_id=connection_id,
            project_name=project_name(connection_id),
            status=InstanceStatus.ERROR,
            updated_at=utc_now(),
        )
    )
    secret = engine.secret_store.path_for(connection_id, "mt5_password")
    with pytest.raises(ProvisioningError, match="risorse Docker orfane"):
        engine.deprovision(connection_id)
    assert secret.exists()
    assert "deprovision" not in docker.calls


def test_deprovision_retry_finishes_host_cleanup_after_durable_deleted_state(
    tmp_path, monkeypatch
):
    docker = FakeDocker()
    engine = ProvisioningEngine(_config(tmp_path), docker=docker)
    job = _job()
    _write_secrets(engine, job.connection_id)
    engine.execute_job(job)
    instance_dir = engine._rendered_for(job.connection_id).instance_dir
    original_cleanup = engine._delete_instance_files

    def fail_cleanup(_connection_id):
        raise OSError("simulated cleanup interruption")

    monkeypatch.setattr(engine, "_delete_instance_files", fail_cleanup)
    with pytest.raises(OSError, match="cleanup interruption"):
        engine.deprovision(job.connection_id)

    state = engine.state_store.load_instance(job.connection_id)
    assert state is not None and state.status is InstanceStatus.DELETED
    assert instance_dir.exists()
    assert not engine.secret_store.connection_dir(job.connection_id).exists()

    monkeypatch.setattr(engine, "_delete_instance_files", original_cleanup)
    result = engine.deprovision(job.connection_id)
    assert result["idempotent"] is True
    assert not instance_dir.exists()


def test_secret_store_enforces_mode_owner_and_no_symlink_follow(tmp_path):
    store = SecretStore(tmp_path / "secrets", expected_owner_uid=os.getuid())
    connection_id = str(uuid4())
    path = store.write_for_local_test(connection_id, "mt5_password", "fake-value")
    store.validate_file(path)
    path.chmod(0o640)
    with pytest.raises(SecretStoreError, match="0400 o 0600"):
        store.validate_file(path)
    path.chmod(0o600)
    wrong_owner = SecretStore(tmp_path / "secrets", expected_owner_uid=os.getuid() + 1)
    with pytest.raises(SecretStoreError, match="Owner UID"):
        wrong_owner.validate_file(path)

    path.unlink()
    outside = tmp_path / "outside"
    outside.write_text("untouched", encoding="utf-8")
    path.symlink_to(outside)
    with pytest.raises(SecretStoreError):
        store.write_for_local_test(connection_id, "mt5_password", "replacement")
    assert outside.read_text(encoding="utf-8") == "untouched"


def test_docker_runner_always_uses_argument_list_and_shell_false(tmp_path):
    compose = tmp_path / "compose.yaml"
    env_file = tmp_path / "instance.env"
    compose.write_text("services: {}\n", encoding="utf-8")
    env_file.write_text("SAFE='yes'\n", encoding="utf-8")
    rendered = RenderedCompose(
        project_name="tjmt5-" + "a" * 32,
        instance_dir=tmp_path,
        compose_file=compose,
        env_file=env_file,
    )
    observed = {}

    def fake_run(command, **kwargs):
        observed["command"] = command
        observed.update(kwargs)
        return subprocess.CompletedProcess(command, 0, stdout="[]", stderr="")

    DockerRunner(run_fn=fake_run).status(rendered)
    assert isinstance(observed["command"], list)
    assert observed["shell"] is False
