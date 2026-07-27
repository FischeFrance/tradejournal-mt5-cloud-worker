from pathlib import Path


SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "windows"
    / "prepare-mt5-template.ps1"
)
INSTALL_SCRIPT = SCRIPT.with_name("install-mt5.ps1")


def test_template_compiles_all_required_mql5_binaries_before_copy() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert "compile-readonly-ea.ps1" in source
    assert "compile-loader-script.ps1" in source
    assert "compile-discovery-script.ps1" in source
    assert "& $compileBridgeScript" in source
    assert "& $compileLoaderScript" in source
    assert "& $compileDiscoveryScript" in source


def test_template_installs_loader_and_discovery_in_scripts_directory() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert r"'MQL5\Scripts\TradeJournal'" in source
    assert "TradeJournalLoader.ex5" in source
    assert "TradeJournalDiscovery.ex5" in source
    assert "loader_sha256" in source
    assert "discovery_sha256" in source


def test_installer_creates_private_download_directory_before_download() -> None:
    source = INSTALL_SCRIPT.read_text(encoding="utf-8")

    create_index = source.index("New-Item -ItemType Directory")
    download_index = source.index("Invoke-WebRequest")
    assert create_index < download_index
    assert "Split-Path $installer" in source
