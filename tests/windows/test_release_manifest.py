from __future__ import annotations

from pathlib import Path

import pytest

from windows_agent.release_manifest import (
    RELEASE_CONTENTS,
    ReleaseManifestError,
    build_release,
    verify_release_matches_source,
)


REVISION = "1" * 40
COLLIDING_REVISION = ("1" * 12) + ("2" * 28)
CONTENTS = ("payload",)
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _source_tree(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    payload = source / "payload"
    payload.mkdir(parents=True)
    (payload / "agent.py").write_text("VERSION = 1\n", encoding="utf-8")
    return source


def test_published_release_can_be_reused_only_for_identical_source(
    tmp_path: Path,
) -> None:
    source = _source_tree(tmp_path)
    release = build_release(
        source,
        tmp_path / "releases",
        revision=REVISION,
        contents=CONTENTS,
    )

    document = verify_release_matches_source(
        release,
        source,
        revision=REVISION,
        contents=CONTENTS,
    )

    assert document["source_revision"] == REVISION


def test_release_omits_complete_python_cache_directories(tmp_path: Path) -> None:
    source = _source_tree(tmp_path)
    cache = source / "payload" / "__pycache__"
    cache.mkdir()
    (cache / "agent.pyc").write_bytes(b"bytecode")
    (cache / "unexpected.txt").write_text("ignored\n", encoding="utf-8")

    release = build_release(
        source,
        tmp_path / "releases",
        revision=REVISION,
        contents=CONTENTS,
    )
    document = verify_release_matches_source(
        release,
        source,
        revision=REVISION,
        contents=CONTENTS,
    )

    assert not (release / "payload" / "__pycache__").exists()
    assert all("__pycache__" not in item["path"] for item in document["files"])


def test_release_reuse_rejects_a_full_revision_collision(tmp_path: Path) -> None:
    source = _source_tree(tmp_path)
    release = build_release(
        source,
        tmp_path / "releases",
        revision=REVISION,
        contents=CONTENTS,
    )

    with pytest.raises(ReleaseManifestError, match="revision"):
        verify_release_matches_source(
            release,
            source,
            revision=COLLIDING_REVISION,
            contents=CONTENTS,
        )


def test_release_reuse_rejects_changed_source_bytes(tmp_path: Path) -> None:
    source = _source_tree(tmp_path)
    release = build_release(
        source,
        tmp_path / "releases",
        revision=REVISION,
        contents=CONTENTS,
    )
    (source / "payload" / "agent.py").write_text("VERSION = 2\n", encoding="utf-8")

    with pytest.raises(ReleaseManifestError, match="does not match"):
        verify_release_matches_source(
            release,
            source,
            revision=REVISION,
            contents=CONTENTS,
        )


def test_release_reuse_rejects_changed_published_bytes(tmp_path: Path) -> None:
    source = _source_tree(tmp_path)
    release = build_release(
        source,
        tmp_path / "releases",
        revision=REVISION,
        contents=CONTENTS,
    )
    (release / "payload" / "agent.py").write_text("VERSION = 2\n", encoding="utf-8")

    with pytest.raises(ReleaseManifestError, match="binding"):
        verify_release_matches_source(
            release,
            source,
            revision=REVISION,
            contents=CONTENTS,
        )


def test_release_reuse_rejects_allowlist_expansion(tmp_path: Path) -> None:
    source = _source_tree(tmp_path)
    release = build_release(
        source,
        tmp_path / "releases",
        revision=REVISION,
        contents=CONTENTS,
    )
    extra = source / "additional"
    extra.mkdir()
    (extra / "required.txt").write_text("required\n", encoding="utf-8")

    with pytest.raises(ReleaseManifestError, match="does not match"):
        verify_release_matches_source(
            release,
            source,
            revision=REVISION,
            contents=("payload", "additional"),
        )


def test_release_allowlist_contains_all_dependency_locks() -> None:
    assert "requirements.txt" in RELEASE_CONTENTS
    assert "requirements-ai.txt" in RELEASE_CONTENTS
    assert "requirements-windows.txt" in RELEASE_CONTENTS


def test_deployment_gate_binds_and_rechecks_the_source_checkout() -> None:
    script = (
        REPOSITORY_ROOT / "scripts" / "windows" / "deploy-history-import-release.ps1"
    ).read_text(encoding="utf-8")

    assert "rev-parse --verify 'HEAD^{commit}'" in script
    assert "status --porcelain=v1 --untracked-files=all" in script
    assert script.count("Assert-CleanSourceCheckout") >= 3
    assert "verify_release_matches_source" in script
    assert script.index("$service = Get-Service") < script.index("build_release")
    assert "Invoke-DeployGuard -Action preflight" in script
    assert script.index("build_release") < script.index(
        "$preflightResult = Invoke-DeployGuard -Action preflight"
    )
    assert "GetEnvironmentVariables('Machine')" in script
    assert "Assert-EffectiveRuntimeConfiguration -ServiceEnvironment" in script
    assert "load_runtime_config(dict(entry.split" not in script
    assert "pip install" not in script.lower()
    for release_source in RELEASE_CONTENTS:
        assert f"'{release_source}'" in script


def test_deployment_gate_includes_direct_tests_and_powershell_parser() -> None:
    script = (
        REPOSITORY_ROOT / "scripts" / "windows" / "deploy-history-import-release.ps1"
    ).read_text(encoding="utf-8")

    for path in (
        "test_event_supervisor.py",
        "test_real_handlers.py",
        "test_mt5_discovery_contract.py",
        "test_interactive_identity.py",
        "test_deploy_guard.py",
        "test_release_manifest.py",
        "test_event_normalizer.py",
        "test_mql5_ea_no_trading.py",
        "test_windows_smoke.py') + '::test_powershell_scripts_parse",
    ):
        assert path in script


def test_deployment_uses_a_system_guard_and_forward_only_barrier() -> None:
    script = (
        REPOSITORY_ROOT / "scripts" / "windows" / "deploy-history-import-release.ps1"
    ).read_text(encoding="utf-8")

    assert "/RU SYSTEM /RL HIGHEST" in script
    assert "runpy.run_module('windows_agent.deploy_guard'" in script
    assert "sys.argv.pop(1)" in script
    assert '" -I -B -c "' in script
    assert "/Delete /TN $taskName /F" in script
    assert "Global\\TradeJournalMT5AgentDeployment" in script
    assert "$deploymentMutex.WaitOne(0)" in script
    assert "$maximumAttempts = if ($Action -eq 'converge') { 3 } else { 2 }" in script
    assert "for ($attempt = 1; $attempt -le $maximumAttempts; $attempt++)" in script
    assert "DeployGuardCommitAmbiguous" in script
    assert "$barrierAttempted = $true" in script
    assert "$serviceWasRunning = $service.Status -eq 'Running'" in script
    assert "if ($serviceWasRunning)" in script
    assert "$service.Status -notin @('Running', 'Stopped')" in script
    ambiguous = script.index("$deploymentFailure.Exception.Data.Contains('DeployGuardCommitAmbiguous')")
    barrier_probe = script.index("Invoke-DeployGuard -Action barrier_status", ambiguous)
    forward_only = script.index("if ($activationBarrierCrossed)", barrier_probe)
    restore = script.index("Invoke-DeployGuard -Action restore", forward_only)
    assert ambiguous < barrier_probe < forward_only < restore
    rollback_restart_gate = script.index(
        "if (-not $restoreFailed -and $serviceWasRunning)", forward_only
    )
    old_start = script.index("Start-Service -Name $serviceName", rollback_restart_gate)
    assert rollback_restart_gate < old_start

    stop = script.index("Stop-Service -Name $serviceName -Force")
    snapshot = script.index("Invoke-DeployGuard -Action snapshot", stop)
    switch = script.index("Invoke-DeployGuard -Action switch", snapshot)
    arm = script.index("Invoke-DeployGuard -Action arm", switch)
    barrier = script.index("Invoke-DeployGuard -Action barrier", arm)
    converge = script.index("Invoke-DeployGuard -Action converge", barrier)
    start = script.index("Start-Service -Name $serviceName", converge)
    verify = script.index("Invoke-DeployGuard -Action verify_active", start)
    assert stop < snapshot < switch < arm < barrier < converge < start < verify
    assert script.index("$activationBarrierCrossed = $true", barrier) < converge
    assert "Invoke-DeployGuard -Action restore" in script
    assert "barrier_status" in script

    # Mutations covered by the snapshot are performed only by the LocalSystem
    # guard; the administrative wrapper cannot independently reconstruct them.
    assert "Copy-Item $expertBackup" not in script
    assert "Set-ItemProperty $serviceRegistry -Name Environment" not in script
    assert 'New-Item -ItemType Junction -Path $currentPath' not in script


def test_deployment_binds_readiness_and_fpm_health_to_the_release() -> None:
    script = (
        REPOSITORY_ROOT / "scripts" / "windows" / "deploy-history-import-release.ps1"
    ).read_text(encoding="utf-8")

    for setting in (
        "TRADEJOURNAL_AGENT_RELEASE_REVISION",
        "TRADEJOURNAL_AGENT_DEPLOYMENT_ID",
        "TRADEJOURNAL_AGENT_READINESS_PATH",
        "PYTHONDONTWRITEBYTECODE",
    ):
        assert setting in script
    assert "agent-readiness.json" in script
    assert "max_heartbeat_age_seconds = 30" in script
    assert "fpm_process_creation_time_unix_ms" in script
    assert "TotalSeconds -ge 30" in script
    assert "$stableIdentity = $null" in script
    assert "provisioned_instance_count" in script
    assert "$ExpectedTerminalCount = $provisionedInstanceCountBefore" in script
    assert "$ExpectedTerminalCount = $terminalCountBefore" not in script
    assert "[string]$RecoveryConnectionId = ''" in script
    assert "$isBootstrapActivation" in script
    assert "(-not $isBootstrapActivation -and -not $RecoveryConnectionId)" in script
    assert "if ($isBootstrapActivation)" in script
    assert "heartbeat_sequence" in script
    assert "$lastHeartbeatSequence -gt $heartbeatSequenceBaseline" in script
    assert script.count("fpm_connection_id = $RecoveryConnectionId") >= 3


def test_prepare_only_returns_after_guarded_preflight_before_any_activation() -> None:
    script = (
        REPOSITORY_ROOT / "scripts" / "windows" / "deploy-history-import-release.ps1"
    ).read_text(encoding="utf-8")

    build = script.index("build_release")
    verify = script.index("verify_release_matches_source", build)
    preflight = script.index("$preflightResult = Invoke-DeployGuard -Action preflight")
    prepare = script.index("if ($PrepareOnly)", preflight)
    prepare_return = script.index("return", prepare)
    recovery_policy = script.index("$scCommand.Source failure", prepare_return)
    stop = script.index("Stop-Service -Name $serviceName -Force", recovery_policy)
    assert build < verify < preflight < prepare < prepare_return < recovery_policy < stop
    prepare_block = script[prepare:recovery_policy]
    for forbidden in (
        "Stop-Service",
        "Invoke-DeployGuard -Action snapshot",
        "Invoke-DeployGuard -Action switch",
        "Invoke-DeployGuard -Action arm",
        "Invoke-DeployGuard -Action barrier",
        "Invoke-DeployGuard -Action converge",
        "Start-Service",
    ):
        assert forbidden not in prepare_block


def test_convergence_evidence_precedes_service_start_and_has_full_window_timeout() -> None:
    script = (
        REPOSITORY_ROOT / "scripts" / "windows" / "deploy-history-import-release.ps1"
    ).read_text(encoding="utf-8")

    converge = script.index("Invoke-DeployGuard -Action converge")
    assert "-TimeoutSeconds 7200" in script[converge : converge + 300]
    completed = script.index("$convergenceCompleted = $true", converge)
    start = script.index("Start-Service -Name $serviceName", completed)
    assert converge < completed < start
    catch_gate = script.index("if (-not $convergenceCompleted)", start)
    recovery_start = script.index("Start-Service -Name $serviceName", catch_gate)
    assert catch_gate < recovery_start


def test_service_installer_is_fail_fast_and_uses_the_pinned_runtime() -> None:
    script = (
        REPOSITORY_ROOT / "scripts" / "windows" / "install-agent-service.ps1"
    ).read_text(encoding="utf-8")

    assert "#Requires -Version 5.1" in script
    assert "#Requires -RunAsAdministrator" in script
    assert "Set-StrictMode -Version Latest" in script
    assert "$ErrorActionPreference = 'Stop'" in script
    assert "$DeploymentPython" in script
    assert "Get-Command sc.exe" in script
    assert script.count("$LASTEXITCODE -ne 0") >= 4
