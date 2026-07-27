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


def test_service_installer_preserves_non_pythonpath_service_environment() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert "$preserved" in source
    assert "$_ -notlike 'PYTHONPATH=*'" in source
    assert "$serviceEnvironment = @($preserved)" in source
    assert "Count -ne 1" in source
