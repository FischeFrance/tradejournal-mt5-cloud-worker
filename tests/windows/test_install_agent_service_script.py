from pathlib import Path


SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "windows"
    / "install-agent-service.ps1"
)


def test_service_installer_registers_a_service_scoped_pywin32_path() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert r"HKLM:\SYSTEM\CurrentControlSet\Services\TradeJournalMT5Agent" in source
    assert "-PropertyType MultiString" in source
    assert "'win32'" in source
    assert r"'win32\lib'" in source
    assert "'Pythonwin'" in source
    assert '"PYTHONPATH=$pythonPath"' in source
    assert "[Environment]::SetEnvironmentVariable" not in source
    assert "'Machine'" not in source
    assert "[string]$ReleaseRoot" in source
    assert "[string]$PythonExecutable" in source
    assert "release-manifest.json" in source
    assert "verify_release" in source
    assert "& $python -B -c" in source
    assert '"PYTHONDONTWRITEBYTECODE=1"' in source
    assert '"PYTHONPYCACHEPREFIX=$bytecodeCache"' in source
    assert "icacls.exe $repo" in source
    assert "Release immutability ACL registration failed" in source


def test_service_installer_preserves_non_pythonpath_service_environment() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert "$preserved" in source
    assert "$_ -notlike 'PYTHONPATH=*'" in source
    assert "$serviceEnvironment = @($preserved)" in source
    assert "Count -ne 1" in source
    assert "$_ -notlike 'PYTHONDONTWRITEBYTECODE=*'" in source
    assert "bytecode suppression registration failed" in source
    assert "$_ -notlike 'PYTHONPYCACHEPREFIX=*'" in source
    assert "bytecode cache registration failed" in source


def test_service_installer_updates_only_a_stopped_existing_service() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert "Get-Service -Name TradeJournalMT5Agent" in source
    assert "$existingService.Status -ne 'Stopped'" in source
    assert "$serviceCommand = if ($existingService) { 'update' } else { 'install' }" in source
    assert "--startup auto $serviceCommand" in source
    assert "& $python -m windows_agent.service.windows_service" in source


def test_service_installer_configures_bounded_scm_recovery() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert "sc.exe failure TradeJournalMT5Agent" in source
    assert "restart/60000/restart/60000/restart/300000" in source
    assert "sc.exe failureflag TradeJournalMT5Agent 1" in source
