from pathlib import Path

import pytest

from windows_agent.maintenance_capability import (
    MAINTENANCE_ENV,
    MAINTENANCE_FILES,
    MAINTENANCE_TYPES,
    MaintenanceCapabilityError,
    assert_service_compatibility,
    parse_service_environment,
    verify_maintenance_capability,
)


def capable_release(root: Path) -> Path:
    for relative in MAINTENANCE_FILES:
        path = root / "windows_agent" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        class_name = MAINTENANCE_TYPES.get(relative)
        path.write_text(f"class {class_name}:\n    pass\n" if class_name else "def new_only_recovery_from():\n    pass\n")
    (root / "windows_agent" / "agent_daemon.py").write_text(
        "def build_runner(config):\n"
        "    coordinator = Mt5MaintenanceCoordinator(Mt5PublicReleaseProbe())\n"
        "    maintenance = Mt5MaintenanceScheduler(coordinator)\n"
        "    return JobRunner(scheduled_maintenance=maintenance)\n"
        "def run_forever(runner):\n"
        "    runner.scheduled_maintenance.run_if_due()\n"
    )
    (root / "windows_agent" / "runtime_config.py").write_text(
        "def load_runtime_config(source):\n"
        f"    return source.get('{MAINTENANCE_ENV}')\n"
    )
    return root


def test_rejects_the_observed_regression_even_with_a_preserved_enabled_flag(tmp_path):
    with pytest.raises(MaintenanceCapabilityError, match="module_missing"):
        assert_service_compatibility(tmp_path, {MAINTENANCE_ENV: "1"})


@pytest.mark.parametrize("file", MAINTENANCE_FILES)
def test_rejects_a_partially_packaged_scheduler(tmp_path, file):
    root = capable_release(tmp_path)
    (root / "windows_agent" / file).unlink()
    with pytest.raises(MaintenanceCapabilityError, match="module_missing"):
        verify_maintenance_capability(root)


@pytest.mark.parametrize("removed", ["Mt5MaintenanceCoordinator", "Mt5PublicReleaseProbe", "Mt5MaintenanceScheduler", "run_if_due", "scheduled_maintenance="])
def test_files_alone_do_not_prove_the_worker_runs_maintenance(tmp_path, removed):
    root = capable_release(tmp_path)
    path = root / "windows_agent" / "agent_daemon.py"
    path.write_text(path.read_text().replace(removed, "unused_" + removed))
    with pytest.raises(MaintenanceCapabilityError, match="wiring_missing"):
        verify_maintenance_capability(root)


def test_accepts_wired_maintenance_and_does_not_execute_candidate_code(tmp_path):
    root = capable_release(tmp_path)
    path = root / "windows_agent" / "mt5_maintenance.py"
    path.write_text(path.read_text() + "raise RuntimeError('candidate code must not execute')\n")
    assert_service_compatibility(root, {MAINTENANCE_ENV: "1"})


def test_disabled_maintenance_does_not_require_a_scheduler(tmp_path):
    assert_service_compatibility(tmp_path, {MAINTENANCE_ENV: "0"})


def test_duplicate_or_invalid_feature_flags_cannot_bypass_the_gate(tmp_path):
    with pytest.raises(MaintenanceCapabilityError, match="duplicate"):
        parse_service_environment([f"{MAINTENANCE_ENV}=1", f"{MAINTENANCE_ENV.lower()}=0"])
    with pytest.raises(MaintenanceCapabilityError, match="invalid"):
        assert_service_compatibility(tmp_path, {MAINTENANCE_ENV: "true"})


def test_registry_filter_never_returns_secrets():
    assert parse_service_environment([
        f"{MAINTENANCE_ENV}=1", "TRADEJOURNAL_AGENT_TOKEN=private", "OTHER_SECRET=private",
    ]) == {MAINTENANCE_ENV: "1"}


def test_empty_module_cannot_claim_a_maintenance_implementation(tmp_path):
    root = capable_release(tmp_path)
    (root / "windows_agent" / "mt5_maintenance.py").write_text("# missing coordinator\n")
    with pytest.raises(MaintenanceCapabilityError, match="implementation_missing"):
        verify_maintenance_capability(root)


def test_installer_checks_capability_before_any_service_or_acl_mutation():
    root = Path(__file__).resolve().parents[1]
    script = (root / "scripts/windows/install-agent-service.ps1").read_text()
    gate = script.index("-m windows_agent.maintenance_capability")
    for mutation in ("& icacls.exe", "-m windows_agent.service.windows_service", "New-ItemProperty", "& sc.exe failure"):
        assert gate < script.index(mutation)
