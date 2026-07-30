from __future__ import annotations

from pathlib import Path

import pytest

from windows_agent.release_manifest import (
    RELEASE_MANIFEST_NAME,
    ReleaseManifestError,
    build_release,
    verify_release,
)


REVISION = "0123456789abcdef0123456789abcdef01234567"


def _source(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    (source / "windows_agent").mkdir(parents=True)
    (source / "windows_agent" / "agent.py").write_text("VALUE = 1\n", encoding="utf-8")
    (source / "worker").mkdir()
    (source / "worker" / "atomic.py").write_text("VALUE = 2\n", encoding="utf-8")
    (source / "contracts" / "mt5-agent-v1").mkdir(parents=True)
    (source / "contracts" / "mt5-agent-v1" / "contract.json").write_text("{}", encoding="utf-8")
    (source / "mt5" / "experts").mkdir(parents=True)
    (source / "mt5" / "experts" / "TradeJournalBridge.mq5").write_text("// source\n", encoding="utf-8")
    (source / "scripts" / "windows").mkdir(parents=True)
    (source / "scripts" / "windows" / "install.ps1").write_text("Write-Host ok\n", encoding="utf-8")
    (source / "requirements.txt").write_text("httpx\n", encoding="utf-8")
    (source / "requirements-windows.txt").write_text("pywin32\n", encoding="utf-8")
    return source


def test_builds_immutable_manifest_and_verifies_each_file(tmp_path: Path) -> None:
    source = _source(tmp_path)
    release = build_release(source, tmp_path / "releases", revision=REVISION)

    assert release.name == "agent-0123456789ab"
    assert (release / RELEASE_MANIFEST_NAME).is_file()
    assert verify_release(release)["source_revision"] == REVISION
    with pytest.raises(ReleaseManifestError, match="already published"):
        build_release(source, tmp_path / "releases", revision=REVISION)


@pytest.mark.parametrize("mutation", ("tamper", "extra"))
def test_verifier_rejects_changed_or_added_release_content(tmp_path: Path, mutation: str) -> None:
    release = build_release(_source(tmp_path), tmp_path / "releases", revision=REVISION)
    target = release / "windows_agent" / "agent.py"
    if mutation == "tamper":
        target.write_text("VALUE = 99\n", encoding="utf-8")
    else:
        (release / "unexpected.txt").write_text("unexpected\n", encoding="utf-8")

    with pytest.raises(ReleaseManifestError, match="release file"):
        verify_release(release)


def test_builder_rejects_symlinked_source(tmp_path: Path) -> None:
    source = _source(tmp_path)
    target = source / "windows_agent" / "linked.py"
    try:
        target.symlink_to(source / "worker" / "atomic.py")
    except OSError:
        pytest.skip("symlinks unavailable in this test environment")

    with pytest.raises(ReleaseManifestError, match="reparse"):
        build_release(source, tmp_path / "releases", revision=REVISION)
