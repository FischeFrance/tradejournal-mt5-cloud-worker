from __future__ import annotations

import json
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from windows_agent import deploy_guard
from windows_agent.runtime_config import AgentRuntimeConfig


REVISION = "a" * 40
DEPLOYMENT_ID = "00000000-0000-4000-8000-000000000001"
FPM_CONNECTION_ID = "00000000-0000-4000-8000-000000000002"
OLD_EXPERT = b"old-bridge"
NEW_EXPERT = b"new-bridge"


def _request(
    action: str,
    payload: dict[str, object] | None = None,
    *,
    nonce: str | None = None,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "action": action,
        "nonce": nonce or str(uuid4()),
        "deployment_id": DEPLOYMENT_ID,
        "source_revision": REVISION,
        "payload": payload or {},
    }


def _config(root: Path, **changes: object) -> AgentRuntimeConfig:
    values: dict[str, object] = {
        "base_url": "https://example.supabase.co/trading-agent",
        "secrets_root": root / "secrets",
        "instances_root": root / "instances",
        "instance_pool_root": root / "pool",
        "instance_pool_target_size": 1,
        "instance_pool_max_size": 2,
        "source_terminal": root / "mt5-template" / "terminal64.exe",
        "expert_binary": root
        / "mt5-template"
        / "MQL5"
        / "Experts"
        / "TradeJournal"
        / "TradeJournalBridge.ex5",
        "terminal_sha256": "1" * 64,
        "expert_sha256": "2" * 64,
        "trading_ingestion_url": "https://example.supabase.co/ingest",
        "mt5_interactive_user": "TradeJournalMT5",
        "mt5_maintenance_enabled": True,
        "mt5_maintenance_local_time": time(23, 30),
        "mt5_maintenance_timezone": "Europe/Rome",
        "mt5_maintenance_grace_minutes": 120,
        "mt5_maintenance_state_path": root / "state" / "maintenance.json",
    }
    values.update(changes)
    return AgentRuntimeConfig(**values)  # type: ignore[arg-type]


@pytest.fixture
def guard_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "TradeJournal"
    release_root = root / "releases"
    release = release_root / f"agent-{REVISION[:12]}"
    old_release = release_root / "agent-old"
    release.mkdir(parents=True)
    old_release.mkdir()
    current = root / "current"
    current.symlink_to(old_release, target_is_directory=True)
    golden = root / "mt5-template"
    expert = golden / "MQL5" / "Experts" / "TradeJournal" / "TradeJournalBridge.ex5"
    expert.parent.mkdir(parents=True)
    expert.write_bytes(OLD_EXPERT)
    (golden / "terminal64.exe").write_bytes(b"terminal")
    artifact = root / "artifacts" / "mql5" / "TradeJournalBridge.ex5"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(NEW_EXPERT)

    monkeypatch.setattr(deploy_guard, "TRADEJOURNAL_ROOT", root)
    monkeypatch.setattr(deploy_guard, "RELEASE_ROOT", release_root)
    monkeypatch.setattr(deploy_guard, "CURRENT_PATH", current)
    monkeypatch.setattr(deploy_guard, "GOLDEN_ROOT", golden)
    monkeypatch.setattr(deploy_guard, "GOLDEN_EXPERT_PATH", expert)
    monkeypatch.setattr(
        deploy_guard,
        "GOLDEN_MARKER_PATH",
        golden / ".tradejournal-vendor-update.json",
    )
    monkeypatch.setattr(deploy_guard, "ARTIFACT_EXPERT_PATH", artifact)
    monkeypatch.setattr(deploy_guard, "STATE_ROOT", root / "state" / "deploy-guard")
    monkeypatch.setattr(deploy_guard, "REQUEST_PATH", root / "state" / "request.json")
    monkeypatch.setattr(deploy_guard, "RESULT_PATH", root / "state" / "result.json")
    readiness = root / "state" / "agent-readiness.json"
    monkeypatch.setattr(deploy_guard, "AGENT_READINESS_PATH", readiness)
    monkeypatch.setattr(deploy_guard, "SERVICE_READINESS_PATH", readiness)
    monkeypatch.setattr(
        deploy_guard,
        "verify_release",
        lambda path: {"source_revision": REVISION, "path": str(path)},
    )
    monkeypatch.setattr(deploy_guard, "_service_status", lambda: ("stopped", 0))
    return root


def _write_preflight(
    root: Path,
    *,
    fpm: str = FPM_CONNECTION_ID,
    environment: list[str] | None = None,
    provisioned_count: int = 1,
) -> None:
    environment = environment or ["A=one"]
    marker = (
        deploy_guard.GOLDEN_MARKER_PATH.read_bytes()
        if deploy_guard.GOLDEN_MARKER_PATH.is_file()
        else None
    )
    path = deploy_guard._record_path(DEPLOYMENT_ID, "preflight")
    deploy_guard._exclusive_json(
        path,
        {
            "schema_version": 1,
            "deployment_id": DEPLOYMENT_ID,
            "source_revision": REVISION,
            "nonce": str(uuid4()),
            "checked_at_unix_ms": 1,
            "golden_terminal_sha256": "1" * 64,
            "old_expert_sha256": deploy_guard._sha256_bytes(OLD_EXPERT),
            "old_marker_present": marker is not None,
            "old_marker_sha256": (
                deploy_guard._sha256_bytes(marker) if marker is not None else None
            ),
            "old_service_environment_sha256": deploy_guard._sha256_bytes(
                deploy_guard._json_bytes({"environment": environment})
            ),
            "old_release_present": True,
            "old_release_path": str(root / "releases" / "agent-old"),
            "new_expert_sha256": deploy_guard._sha256_bytes(NEW_EXPERT),
            "service_environment_sha256": "3" * 64,
            "release_path": str(root / "releases" / f"agent-{REVISION[:12]}"),
            "fpm_connection_id": fpm,
            "live_instance_count": 1,
            "provisioned_instance_count": provisioned_count,
        },
    )


def test_service_import_contract_exports_canonical_readiness_path() -> None:
    assert deploy_guard.SERVICE_READINESS_PATH == deploy_guard.AGENT_READINESS_PATH
    assert callable(deploy_guard.assert_service_activation_allowed)


def test_request_parser_is_strict_and_nonce_bound(
    guard_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _request("snapshot")
    deploy_guard.REQUEST_PATH.parent.mkdir(parents=True)
    deploy_guard.REQUEST_PATH.write_text(json.dumps(request), encoding="utf-8")
    monkeypatch.setattr(deploy_guard, "_assert_request_acl", lambda _path: None)

    assert deploy_guard._load_request() == request

    request["unexpected"] = True
    deploy_guard.REQUEST_PATH.write_text(json.dumps(request), encoding="utf-8")
    with pytest.raises(deploy_guard.DeployGuardError, match="request_invalid"):
        deploy_guard._load_request()

    request.pop("unexpected")
    request["nonce"] = "not-a-uuid"
    deploy_guard.REQUEST_PATH.write_text(json.dumps(request), encoding="utf-8")
    with pytest.raises(deploy_guard.DeployGuardError, match="request_nonce_invalid"):
        deploy_guard._load_request()


def test_effective_environment_merges_machine_and_service_case_insensitively(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        deploy_guard.os,
        "environ",
        {"MACHINE_ONLY": "kept", "SETTING": "machine", "Path": "machine-path"},
    )
    effective = deploy_guard._effective_environment(
        {"SETTING": "service", "PATH": "service-path"}
    )

    assert effective["MACHINE_ONLY"] == "kept"
    assert effective["SETTING"] == "service"
    assert effective["PATH"] == "service-path"
    assert "Path" not in effective


@pytest.mark.parametrize(
    "changes",
    [
        {"mt5_maintenance_local_time": time(12, 0)},
        {"mt5_maintenance_timezone": "UTC"},
        {"mt5_maintenance_grace_minutes": 60},
        {"mt5_maintenance_enabled": False},
    ],
)
def test_guard_hard_codes_the_2330_rome_policy(
    guard_root: Path, changes: dict[str, object]
) -> None:
    with pytest.raises(deploy_guard.DeployGuardError, match="maintenance_policy_invalid"):
        deploy_guard._assert_maintenance_policy(_config(guard_root, **changes))


def test_snapshot_requires_durable_preflight_and_preserves_exact_bytes(
    guard_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = ["B=two", "A=one"]
    monkeypatch.setattr(deploy_guard, "_get_service_environment", lambda: environment)
    marker = b'{"schema_version":2}\r\n'
    deploy_guard.GOLDEN_MARKER_PATH.write_bytes(marker)
    request = _request("snapshot")

    with pytest.raises(deploy_guard.DeployGuardError, match="deployment_preflight_invalid"):
        deploy_guard._snapshot(request)  # type: ignore[arg-type]

    _write_preflight(guard_root, environment=environment)
    details = deploy_guard._snapshot(request)  # type: ignore[arg-type]
    snapshot, bridge, observed_marker = deploy_guard._load_snapshot(request)

    assert bridge == OLD_EXPERT
    assert observed_marker == marker
    assert snapshot["environment"] == environment
    assert details["snapshot_sha256"] == snapshot["snapshot_sha256"]


def test_snapshot_retry_with_new_nonce_is_idempotent(
    guard_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_preflight(guard_root, environment=["A=one"])
    monkeypatch.setattr(deploy_guard, "_get_service_environment", lambda: ["A=one"])
    first = deploy_guard._snapshot(_request("snapshot"))
    second = deploy_guard._snapshot(_request("snapshot"))
    assert first == second


def test_switch_reseals_and_retry_with_new_nonce_does_not_mutate_again(
    guard_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = ["A=next"]
    holder = {"environment": ["A=old"]}
    config = _config(
        guard_root,
        expert_sha256=deploy_guard._sha256_bytes(NEW_EXPERT),
        terminal_sha256=deploy_guard._sha256_bytes(b"terminal"),
    )
    release = guard_root / "releases" / f"agent-{REVISION[:12]}"
    new_hash = deploy_guard._sha256_bytes(NEW_EXPERT)
    old_hash = deploy_guard._sha256_bytes(OLD_EXPERT)
    _write_preflight(guard_root, environment=["A=old"])
    preflight_path = deploy_guard._record_path(DEPLOYMENT_ID, "preflight")
    preflight = json.loads(preflight_path.read_text(encoding="utf-8"))
    preflight["service_environment_sha256"] = deploy_guard._sha256_bytes(
        deploy_guard._json_bytes({"environment": environment})
    )
    preflight_path.write_bytes(deploy_guard._json_bytes(preflight))
    monkeypatch.setattr(deploy_guard, "_get_service_environment", lambda: list(holder["environment"]))
    monkeypatch.setattr(
        deploy_guard,
        "_set_service_environment",
        lambda value: holder.__setitem__("environment", list(value)),
    )
    monkeypatch.setattr(
        deploy_guard,
        "_validated_next_config",
        lambda _request, _payload: (environment, {"A": "next"}, config, release),
    )
    monkeypatch.setattr(
        deploy_guard,
        "Mt5TemplateManager",
        lambda *_args, **_kwargs: SimpleNamespace(
            reseal_managed_code_deployment=lambda *_values: "c" * 64
        ),
    )
    monkeypatch.setattr(deploy_guard, "_restrict_shared", lambda _path: None)
    deploy_guard._snapshot(_request("snapshot"))
    payload = {
        "release_path": str(release),
        "new_expert_path": str(deploy_guard.ARTIFACT_EXPERT_PATH),
        "next_environment": environment,
        "previous_expert_sha256": old_hash,
        "new_expert_sha256": new_hash,
    }

    first = deploy_guard._switch(_request("switch", payload))
    second = deploy_guard._switch(_request("switch", payload))

    assert first == second == {"code_manifest_sha256": "c" * 64}
    assert deploy_guard.GOLDEN_EXPERT_PATH.read_bytes() == NEW_EXPERT
    assert deploy_guard._current_target() == release.resolve()


def test_restore_is_exact_without_identity_and_permanently_kills_transaction(
    guard_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = ["A=old", "B=exact-order"]
    holder = {"environment": list(environment)}
    marker = b"old-marker-bytes\x00"
    deploy_guard.GOLDEN_MARKER_PATH.write_bytes(marker)
    _write_preflight(guard_root, environment=environment)
    monkeypatch.setattr(deploy_guard, "_get_service_environment", lambda: list(holder["environment"]))
    monkeypatch.setattr(
        deploy_guard,
        "_set_service_environment",
        lambda value: holder.__setitem__("environment", list(value)),
    )
    deploy_guard._snapshot(_request("snapshot"))
    deploy_guard.GOLDEN_EXPERT_PATH.write_bytes(NEW_EXPERT)
    deploy_guard.GOLDEN_MARKER_PATH.write_bytes(b"new-marker")
    holder["environment"] = ["A=new"]
    identity_calls = []
    monkeypatch.setattr(
        deploy_guard,
        "verify_interactive_task_identity",
        lambda *_args: identity_calls.append(True),
    )

    first = deploy_guard._restore(_request("restore"))
    second = deploy_guard._restore(_request("restore"))

    assert first == second == {"restored": True}
    assert deploy_guard.GOLDEN_EXPERT_PATH.read_bytes() == OLD_EXPERT
    assert deploy_guard.GOLDEN_MARKER_PATH.read_bytes() == marker
    assert holder["environment"] == environment
    assert identity_calls == []
    with pytest.raises(deploy_guard.DeployGuardError, match="deployment_was_restored"):
        deploy_guard._barrier(_request("barrier"))


def test_barrier_requires_fresh_identity_and_is_idempotent_across_nonce(
    guard_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _request("barrier")
    _write_preflight(guard_root, environment=["A=old"])
    deploy_guard._exclusive_json(
        deploy_guard._record_path(DEPLOYMENT_ID, "arm"),
        {
            "schema_version": 1,
            "deployment_id": DEPLOYMENT_ID,
            "source_revision": REVISION,
            "nonce": str(uuid4()),
            "armed_at_unix_ms": 1,
            "window_started_at_unix_ms": 1,
            "window_ends_at_unix_ms": int(deploy_guard.time.time() * 1000) + 60_000,
            "activation_mode": "scheduled",
        },
    )
    monkeypatch.setattr(deploy_guard, "_assert_switch_intact", lambda _request: None)
    monkeypatch.setattr(
        deploy_guard,
        "_assert_pre_barrier_identity",
        lambda _request: (_ for _ in ()).throw(
            deploy_guard.DeployGuardError("interactive_identity_invalid")
        ),
    )
    with pytest.raises(deploy_guard.DeployGuardError, match="interactive_identity_invalid"):
        deploy_guard._barrier(request)  # type: ignore[arg-type]

    monkeypatch.setattr(deploy_guard, "_assert_pre_barrier_identity", lambda _request: None)
    first = deploy_guard._barrier(request)  # type: ignore[arg-type]
    second = deploy_guard._barrier(_request("barrier"))
    assert first == second


def test_barrier_removes_stale_readiness_and_status_is_authoritative(
    guard_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_preflight(guard_root)
    deploy_guard._exclusive_json(
        deploy_guard._record_path(DEPLOYMENT_ID, "arm"),
        {
            "schema_version": 1,
            "deployment_id": DEPLOYMENT_ID,
            "source_revision": REVISION,
            "nonce": str(uuid4()),
            "armed_at_unix_ms": 1,
            "window_started_at_unix_ms": 1,
            "window_ends_at_unix_ms": int(deploy_guard.time.time() * 1000) + 60_000,
            "activation_mode": "scheduled",
        },
    )
    deploy_guard.AGENT_READINESS_PATH.parent.mkdir(parents=True, exist_ok=True)
    deploy_guard.AGENT_READINESS_PATH.write_text("stale", encoding="utf-8")
    monkeypatch.setattr(deploy_guard, "_assert_switch_intact", lambda _request: None)
    monkeypatch.setattr(deploy_guard, "_assert_pre_barrier_identity", lambda _request: None)
    request = _request("barrier")

    before = deploy_guard._barrier_status(_request("barrier_status"))
    result = deploy_guard._barrier(request)  # type: ignore[arg-type]
    after = deploy_guard._barrier_status(_request("barrier_status"))

    assert before["activation_barrier_crossed"] is False
    assert not deploy_guard.AGENT_READINESS_PATH.exists()
    assert after == {
        "activation_barrier_crossed": True,
        "activation_started_at_unix_ms": result["activation_started_at_unix_ms"],
    }


def test_switch_arm_restore_then_barrier_is_never_allowed(
    guard_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # This regression is independent of token state: restore is a terminal
    # transaction outcome and a stale arm can never become a point of no return.
    _write_preflight(guard_root, environment=["A=old"])
    monkeypatch.setattr(deploy_guard, "_get_service_environment", lambda: ["A=old"])
    monkeypatch.setattr(deploy_guard, "_set_service_environment", lambda _value: None)
    deploy_guard._snapshot(_request("snapshot"))
    deploy_guard._exclusive_json(
        deploy_guard._record_path(DEPLOYMENT_ID, "restore"),
        {
            "schema_version": 1,
            "deployment_id": DEPLOYMENT_ID,
            "source_revision": REVISION,
            "nonce": str(uuid4()),
            "restored_at_unix_ms": 1,
            "snapshot_sha256": deploy_guard._load_snapshot(_request("snapshot"))[0][
                "snapshot_sha256"
            ],
        },
    )
    with pytest.raises(deploy_guard.DeployGuardError, match="deployment_was_restored"):
        deploy_guard._barrier(_request("barrier"))


def test_activation_api_requires_exact_environment_and_barrier(
    guard_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    barrier = {
        "schema_version": 1,
        "deployment_id": DEPLOYMENT_ID,
        "source_revision": REVISION,
        "nonce": str(uuid4()),
        "activation_started_at_unix_ms": 123,
    }
    deploy_guard._exclusive_json(
        deploy_guard._record_path(DEPLOYMENT_ID, "barrier"), barrier
    )
    deploy_guard._exclusive_json(
        deploy_guard._record_path(DEPLOYMENT_ID, "converge"),
        {
            "schema_version": 1,
            "deployment_id": DEPLOYMENT_ID,
            "source_revision": REVISION,
            "nonce": str(uuid4()),
            "converged_at_unix_ms": 124,
            "release_id": "c" * 64,
            "pool_ready_count": 2,
            "fleet_count": 1,
            "fpm_connection_id": FPM_CONNECTION_ID,
        },
    )
    monkeypatch.setenv("TRADEJOURNAL_AGENT_RELEASE_REVISION", REVISION)
    monkeypatch.setenv("TRADEJOURNAL_AGENT_DEPLOYMENT_ID", DEPLOYMENT_ID)
    monkeypatch.setenv(
        "TRADEJOURNAL_AGENT_READINESS_PATH", str(deploy_guard.AGENT_READINESS_PATH)
    )

    assert deploy_guard.assert_service_activation_allowed(REVISION, DEPLOYMENT_ID) == {
        "schema_version": 1,
        "source_revision": REVISION,
        "deployment_id": DEPLOYMENT_ID,
        "activation_started_at_unix_ms": 123,
    }

    monkeypatch.setenv("TRADEJOURNAL_AGENT_DEPLOYMENT_ID", str(uuid4()))
    with pytest.raises(
        deploy_guard.DeployGuardError, match="service_environment_binding_invalid"
    ):
        deploy_guard.assert_service_activation_allowed(REVISION, DEPLOYMENT_ID)


def test_initial_activation_snapshot_restores_absent_old_junction(
    guard_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deploy_guard._remove_current_target()
    environment = ["A=initial"]
    _write_preflight(guard_root, environment=environment)
    preflight_path = deploy_guard._record_path(DEPLOYMENT_ID, "preflight")
    preflight = json.loads(preflight_path.read_text(encoding="utf-8"))
    preflight["old_release_present"] = False
    preflight["old_release_path"] = None
    preflight_path.write_bytes(deploy_guard._json_bytes(preflight))
    holder = {"environment": environment}
    monkeypatch.setattr(
        deploy_guard, "_get_service_environment", lambda: list(holder["environment"])
    )
    monkeypatch.setattr(
        deploy_guard,
        "_set_service_environment",
        lambda value: holder.__setitem__("environment", list(value)),
    )

    deploy_guard._snapshot(_request("snapshot"))
    deploy_guard._replace_current_target(
        guard_root / "releases" / f"agent-{REVISION[:12]}"
    )
    deploy_guard.GOLDEN_EXPERT_PATH.write_bytes(NEW_EXPERT)
    deploy_guard._restore(_request("restore"))

    assert deploy_guard._current_target_optional() is None
    assert deploy_guard.GOLDEN_EXPERT_PATH.read_bytes() == OLD_EXPERT


def test_provisioned_count_includes_an_instance_without_a_running_process(
    guard_root: Path,
) -> None:
    config = _config(guard_root)
    running = FPM_CONNECTION_ID
    stopped = "00000000-0000-4000-8000-000000000003"
    ignored = "00000000-0000-4000-8000-000000000004"
    for connection_id, status in (
        (running, "provisioned"),
        (stopped, "provisioned"),
        (ignored, "deprovisioned"),
    ):
        state = config.instances_root / connection_id / "state"
        state.mkdir(parents=True)
        (state / "instance.json").write_text(
            json.dumps({"status": status}), encoding="utf-8"
        )

    assert deploy_guard._provisioned_instance_count(config) == 2


def test_pre_barrier_rejects_fleet_changes_since_preflight(
    guard_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(guard_root)
    fpm_executable = (
        config.instances_root
        / FPM_CONNECTION_ID
        / "terminal"
        / "terminal64.exe"
    )
    other_executable = (
        config.instances_root
        / "00000000-0000-4000-8000-000000000003"
        / "terminal"
        / "terminal64.exe"
    )
    _write_preflight(guard_root, provisioned_count=1)
    monkeypatch.setattr(
        deploy_guard, "_assert_switch_intact", lambda _request: (config, None)
    )
    monkeypatch.setattr(
        deploy_guard, "verify_interactive_task_identity", lambda _user: None
    )
    monkeypatch.setattr(
        deploy_guard,
        "_iter_terminal_processes",
        lambda _config: [(41, fpm_executable)],
    )
    monkeypatch.setattr(
        deploy_guard, "_provisioned_instance_count", lambda _config: 2
    )

    with pytest.raises(deploy_guard.DeployGuardError, match="deployment_fleet_drifted"):
        deploy_guard._assert_pre_barrier_identity(_request("arm"))

    monkeypatch.setattr(
        deploy_guard, "_provisioned_instance_count", lambda _config: 1
    )
    monkeypatch.setattr(
        deploy_guard,
        "_iter_terminal_processes",
        lambda _config: [(41, fpm_executable), (42, other_executable)],
    )

    with pytest.raises(deploy_guard.DeployGuardError, match="deployment_fleet_drifted"):
        deploy_guard._assert_pre_barrier_identity(_request("barrier"))


def test_converge_runs_explicitly_even_if_daily_scheduler_was_completed(
    guard_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(guard_root)
    target = SimpleNamespace(release_id="d" * 64)
    report = SimpleNamespace(
        checked_connections=(FPM_CONNECTION_ID,), current_release=target
    )
    calls = []
    coordinator = SimpleNamespace(
        _current_release=lambda: target,
        _canaries=lambda _target: (SimpleNamespace(connection_id=FPM_CONNECTION_ID),),
        run_once=lambda stop: calls.append(stop) or report,
    )
    completed_state = config.mt5_maintenance_state_path
    completed_state.parent.mkdir(parents=True)
    completed_state.write_text('{"status":"completed"}', encoding="utf-8")
    _write_preflight(guard_root, provisioned_count=4)
    monkeypatch.setattr(deploy_guard, "_activation_config", lambda _request: config)
    monkeypatch.setattr(
        deploy_guard, "_build_activation_coordinator", lambda _config: (coordinator, None)
    )
    monkeypatch.setattr(deploy_guard, "verify_interactive_task_identity", lambda _user: None)
    monkeypatch.setattr(
        deploy_guard, "_fleet_postconditions", lambda *_args, **_kwargs: (4, 2)
    )
    monkeypatch.setattr(deploy_guard, "_write_activation_schedule_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        deploy_guard,
        "_convergence_attempt",
        lambda _config, request: (
            guard_root / "state" / "attempt.json",
            {
                **deploy_guard._binding(request),
                "nonce": request["nonce"],
                "status": "running",
                "scheduled_local_date": "2026-08-28",
                "started_at_unix_ms": 1,
                "finished_at_unix_ms": None,
                "attempts": 1,
                "failure_code": None,
                "failure_detail": None,
            },
        ),
    )

    result = deploy_guard._converge(
        _request("converge", {"fpm_connection_id": FPM_CONNECTION_ID})
    )

    assert len(calls) == 1
    assert result["release_id"] == "d" * 64
    assert result["fleet_count"] == 4
    assert result["pool_ready_count"] == 2


def test_converge_explicitly_probes_requested_readonly_recovery_account(
    guard_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(guard_root)
    other = "00000000-0000-4000-8000-000000000000"
    fpm_root = config.instances_root / FPM_CONNECTION_ID
    (fpm_root / "state").mkdir(parents=True)
    (fpm_root / "state" / "instance.json").write_text(
        '{"status":"provisioned"}', encoding="utf-8"
    )
    target = SimpleNamespace(release_id="f" * 64)
    probes: list[str] = []

    class Secrets:
        @staticmethod
        def read(_connection_id: str, name: str) -> str:
            return "42" if name == "mt5_login" else "FivePercentOnline-Real"

    coordinator = SimpleNamespace(
        rotator=SimpleNamespace(_instance_root=lambda _connection_id: fpm_root),
        secrets=Secrets(),
        _current_release=lambda: target,
        _canaries=lambda _target: (SimpleNamespace(connection_id=other),),
        _probe=lambda canary, _stop: probes.append(canary.connection_id),
        run_once=lambda _stop: SimpleNamespace(
            checked_connections=(other,), current_release=target
        ),
    )
    _write_preflight(guard_root, provisioned_count=4)
    monkeypatch.setattr(deploy_guard, "_activation_config", lambda _request: config)
    monkeypatch.setattr(
        deploy_guard, "_build_activation_coordinator", lambda _config: (coordinator, None)
    )
    monkeypatch.setattr(deploy_guard, "verify_interactive_task_identity", lambda _user: None)
    monkeypatch.setattr(
        deploy_guard, "_fleet_postconditions", lambda *_args, **_kwargs: (4, 2)
    )
    monkeypatch.setattr(deploy_guard, "_write_activation_schedule_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        deploy_guard,
        "_convergence_attempt",
        lambda _config, request: (
            guard_root / "state" / "forced-attempt.json",
            {
                **deploy_guard._binding(request),
                "nonce": request["nonce"],
                "status": "running",
                "scheduled_local_date": "2026-08-28",
                "started_at_unix_ms": 1,
                "finished_at_unix_ms": None,
                "attempts": 1,
                "failure_code": None,
                "failure_detail": None,
            },
        ),
    )

    result = deploy_guard._converge(
        _request("converge", {"fpm_connection_id": FPM_CONNECTION_ID})
    )

    assert probes == [FPM_CONNECTION_ID]
    assert result["release_id"] == "f" * 64


@pytest.mark.parametrize(
    "value",
    ("", " leading", "trailing ", "bad/server", "line\nbreak"),
)
def test_recovery_server_rejects_unsafe_values(value: str) -> None:
    with pytest.raises(deploy_guard.DeployGuardError, match="recovery_server_invalid"):
        deploy_guard._required_recovery_server(value)


def test_recovery_server_accepts_active_broker_identity() -> None:
    assert (
        deploy_guard._required_recovery_server("FivePercentOnline-Real")
        == "FivePercentOnline-Real"
    )


def test_failed_or_uncertain_convergence_resumes_idempotently_in_same_window(
    guard_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(guard_root)
    start = datetime(2026, 8, 28, 23, 30, tzinfo=timezone(timedelta(hours=2)))
    monkeypatch.setattr(
        deploy_guard,
        "_maintenance_window",
        lambda _config: (start, start + timedelta(minutes=120)),
    )
    deploy_guard._exclusive_json(
        deploy_guard._record_path(DEPLOYMENT_ID, "arm"),
        {
            "schema_version": 1,
            "deployment_id": DEPLOYMENT_ID,
            "source_revision": REVISION,
            "nonce": str(uuid4()),
            "armed_at_unix_ms": 1,
            "window_started_at_unix_ms": int(start.timestamp() * 1000),
            "window_ends_at_unix_ms": int(
                (start + timedelta(minutes=120)).timestamp() * 1000
            ),
            "activation_mode": "scheduled",
        },
    )
    first = _request("converge", {"fpm_connection_id": FPM_CONNECTION_ID})
    deploy_guard._convergence_attempt(config, first)

    _, resumed = deploy_guard._convergence_attempt(
        config,
        _request("converge", {"fpm_connection_id": FPM_CONNECTION_ID}),
    )
    assert resumed["status"] == "running"
    assert resumed["attempts"] == 2


def test_converge_retry_with_new_nonce_uses_durable_evidence(
    guard_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(guard_root)
    target = SimpleNamespace(release_id="e" * 64)
    _write_preflight(guard_root, provisioned_count=4)
    monkeypatch.setattr(deploy_guard, "_activation_config", lambda _request: config)
    monkeypatch.setattr(
        deploy_guard,
        "Mt5TemplateManager",
        lambda *_args, **_kwargs: SimpleNamespace(current_sha256="1" * 64),
    )
    monkeypatch.setattr(
        deploy_guard.Mt5TemplateRelease,
        "from_template",
        lambda *_args, **_kwargs: target,
    )
    monkeypatch.setattr(
        deploy_guard, "_fleet_postconditions", lambda *_args, **_kwargs: (4, 2)
    )
    deploy_guard._exclusive_json(
        deploy_guard._record_path(DEPLOYMENT_ID, "converge"),
        {
            "schema_version": 1,
            "deployment_id": DEPLOYMENT_ID,
            "source_revision": REVISION,
            "nonce": str(uuid4()),
            "converged_at_unix_ms": 1,
            "release_id": "e" * 64,
            "pool_ready_count": 2,
            "fleet_count": 4,
            "fpm_connection_id": FPM_CONNECTION_ID,
        },
    )

    result = deploy_guard._converge(
        _request("converge", {"fpm_connection_id": FPM_CONNECTION_ID})
    )
    assert result["release_id"] == "e" * 64


def test_verify_active_rejects_readiness_from_another_service_pid(
    guard_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(deploy_guard, "_service_status", lambda: ("running", 42))
    deploy_guard.AGENT_READINESS_PATH.parent.mkdir(parents=True, exist_ok=True)
    deploy_guard.AGENT_READINESS_PATH.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source_revision": REVISION,
                "deployment_id": DEPLOYMENT_ID,
                "service_process_id": 99,
                "activation_started_at_unix_ms": 100,
                "ready_at_unix_ms": int(deploy_guard.time.time() * 1000),
            }
        ),
        encoding="utf-8",
    )
    deploy_guard._exclusive_json(
        deploy_guard._record_path(DEPLOYMENT_ID, "barrier"),
        {
            "schema_version": 1,
            "deployment_id": DEPLOYMENT_ID,
            "source_revision": REVISION,
            "nonce": str(uuid4()),
            "activation_started_at_unix_ms": 100,
        },
    )
    deploy_guard._exclusive_json(
        deploy_guard._record_path(DEPLOYMENT_ID, "converge"),
        {
            "schema_version": 1,
            "deployment_id": DEPLOYMENT_ID,
            "source_revision": REVISION,
            "nonce": str(uuid4()),
            "converged_at_unix_ms": 1,
            "release_id": "e" * 64,
            "pool_ready_count": 1,
            "fleet_count": 1,
            "fpm_connection_id": FPM_CONNECTION_ID,
        },
    )

    with pytest.raises(deploy_guard.DeployGuardError, match="service_readiness_invalid"):
        deploy_guard._verify_active(
            _request(
                "verify_active",
                {
                    "fpm_connection_id": FPM_CONNECTION_ID,
                    "max_heartbeat_age_seconds": 30,
                },
            )
        )


def test_verify_active_supports_clean_host_without_fpm_account(
    guard_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import psutil

    now_ms = int(deploy_guard.time.time() * 1000)
    activation_ms = now_ms - 5_000
    service_created_ms = now_ms - 4_000
    ready_ms = now_ms - 1_000
    target = SimpleNamespace(release_id="e" * 64)
    config = _config(
        guard_root,
        instance_pool_target_size=0,
        instance_pool_max_size=1,
    )
    deploy_guard._replace_current_target(
        guard_root / "releases" / f"agent-{REVISION[:12]}"
    )
    deploy_guard._exclusive_json(
        deploy_guard._record_path(DEPLOYMENT_ID, "barrier"),
        {
            "schema_version": 1,
            "deployment_id": DEPLOYMENT_ID,
            "source_revision": REVISION,
            "nonce": str(uuid4()),
            "activation_started_at_unix_ms": activation_ms,
        },
    )
    deploy_guard._exclusive_json(
        deploy_guard._record_path(DEPLOYMENT_ID, "converge"),
        {
            "schema_version": 1,
            "deployment_id": DEPLOYMENT_ID,
            "source_revision": REVISION,
            "nonce": str(uuid4()),
            "converged_at_unix_ms": ready_ms,
            "release_id": target.release_id,
            "pool_ready_count": 0,
            "fleet_count": 0,
            "fpm_connection_id": "",
        },
    )
    deploy_guard.AGENT_READINESS_PATH.parent.mkdir(parents=True, exist_ok=True)
    deploy_guard.AGENT_READINESS_PATH.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source_revision": REVISION,
                "deployment_id": DEPLOYMENT_ID,
                "service_process_id": 42,
                "activation_started_at_unix_ms": activation_ms,
                "ready_at_unix_ms": ready_ms,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(deploy_guard, "_service_status", lambda: ("running", 42))
    monkeypatch.setattr(
        psutil,
        "Process",
        lambda _pid: SimpleNamespace(
            create_time=lambda: service_created_ms / 1000
        ),
    )
    monkeypatch.setattr(deploy_guard, "_get_service_environment", lambda: [])
    monkeypatch.setattr(
        deploy_guard, "load_runtime_config", lambda _environment: config
    )
    monkeypatch.setattr(
        deploy_guard,
        "Mt5TemplateManager",
        lambda *_args, **_kwargs: SimpleNamespace(
            validate_current_quiesced=lambda: "1" * 64
        ),
    )
    monkeypatch.setattr(
        deploy_guard.Mt5TemplateRelease,
        "from_template",
        lambda *_args, **_kwargs: target,
    )
    monkeypatch.setattr(
        deploy_guard, "_fleet_postconditions", lambda *_args, **_kwargs: (0, 0)
    )

    result = deploy_guard._verify_active(
        _request(
            "verify_active",
            {"fpm_connection_id": "", "max_heartbeat_age_seconds": 30},
        )
    )

    assert result == {
        "service_process_id": 42,
        "fpm_connection_id": "",
        "fpm_process_id": None,
        "fpm_process_creation_time_unix_ms": None,
        "heartbeat_generated_at_unix_ms": None,
        "heartbeat_sequence": None,
        "pool_ready_count": 0,
        "fleet_count": 0,
    }


def test_main_returns_only_sanitized_error_code(
    guard_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _request("snapshot")
    monkeypatch.setattr(deploy_guard, "_assert_local_system", lambda: None)
    monkeypatch.setattr(deploy_guard, "_load_request", lambda: request)
    monkeypatch.setattr(
        deploy_guard,
        "run_request",
        lambda _request: (_ for _ in ()).throw(RuntimeError("secret C:\\private")),
    )

    assert deploy_guard.main([]) == 1
    result = json.loads(deploy_guard.RESULT_PATH.read_text(encoding="utf-8"))
    assert result["code"] == "deploy_guard_internal_error"
    assert result["nonce"] == request["nonce"]
    assert "private" not in json.dumps(result)


def test_rome_window_includes_previous_day_grace_after_midnight(
    guard_root: Path,
) -> None:
    config = _config(guard_root)
    start, end = deploy_guard._maintenance_window(
        config,
        datetime(2026, 8, 28, 0, 15, tzinfo=timezone(timedelta(hours=2))),
    )
    assert start.isoformat().startswith("2026-08-27T23:30:00+02:00")
    assert end - start == timedelta(minutes=120)


def test_immediate_activation_is_bounded_and_keeps_nightly_claim_available(
    guard_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(guard_root)
    current = datetime(2026, 8, 28, 16, 30, tzinfo=timezone(timedelta(hours=2)))
    monkeypatch.setattr(
        deploy_guard,
        "_outside_maintenance_window",
        lambda _config, now=None: current,
    )

    start, end, mode = deploy_guard._arm_activation_window(
        config, immediate=True
    )

    assert start == current
    assert end - start == timedelta(minutes=120)
    assert mode == "operator_approved_immediate"

    deploy_guard._exclusive_json(
        deploy_guard._record_path(DEPLOYMENT_ID, "arm"),
        {
            "schema_version": 1,
            "deployment_id": DEPLOYMENT_ID,
            "source_revision": REVISION,
            "nonce": str(uuid4()),
            "armed_at_unix_ms": int(current.timestamp() * 1000),
            "window_started_at_unix_ms": int(current.timestamp() * 1000),
            "window_ends_at_unix_ms": int(end.timestamp() * 1000),
            "activation_mode": mode,
        },
    )
    deploy_guard._write_activation_schedule_state(
        config,
        _request("converge", {"fpm_connection_id": FPM_CONNECTION_ID}),
        status="completed",
    )
    assert not config.mt5_maintenance_state_path.exists()


def test_immediate_activation_is_rejected_inside_nightly_window(
    guard_root: Path,
) -> None:
    config = _config(guard_root)
    inside = datetime(2026, 8, 28, 23, 45, tzinfo=timezone(timedelta(hours=2)))

    with pytest.raises(
        deploy_guard.DeployGuardError,
        match="immediate_activation_during_maintenance_window",
    ):
        deploy_guard._outside_maintenance_window(config, inside)


def test_health_envelopes_require_one_matching_positive_sequence(
    guard_root: Path,
) -> None:
    path = guard_root / "heartbeat.json"
    record = {
        "schema_version": 1,
        "generated_at": "2026-08-28T00:00:00Z",
        "sequence": 7,
        "account_identity": {"login": "42", "server": "FPMTrading-Live"},
        "server_identity": "FPMTrading-Live",
        "payload": {"terminal_connected": True},
    }
    path.write_text(json.dumps(record), encoding="utf-8")
    assert deploy_guard._read_envelope(path, "invalid")[3] == 7

    record["sequence"] = 0
    path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(deploy_guard.DeployGuardError, match="invalid"):
        deploy_guard._read_envelope(path, "invalid")
