from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from windows_agent.provisioning.mt5_instance import (
    InstanceProvisioner,
    _MANAGED_RUNTIME_ASSETS,
)
from windows_agent.provisioning.mt5_template import (
    Mt5TemplateError,
    Mt5TemplateManager,
)
from windows_agent.state_store import read_json


SIGNER = "CN=MetaQuotes Ltd., O=MetaQuotes Ltd., S=Lemesos, C=CY"


def _template(tmp_path: Path) -> Path:
    terminal = tmp_path / "mt5-template" / "terminal64.exe"
    terminal.parent.mkdir()
    terminal.write_bytes(b"terminal-v1")
    for relative in _MANAGED_RUNTIME_ASSETS:
        asset = terminal.parent / relative
        asset.parent.mkdir(parents=True, exist_ok=True)
        asset.write_bytes(relative.as_posix().encode("utf-8"))
    return terminal


def _bundle(tmp_path: Path) -> tuple[Path, Path]:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    updater = bundle / "terminal64.exe"
    updater.write_bytes(b"signed-updater")
    (bundle / "payload.6090").write_bytes(b"payload")
    return bundle, updater


def test_verified_update_atomically_rotates_clean_template(tmp_path: Path) -> None:
    terminal = _template(tmp_path)
    base_digest = InstanceProvisioner._sha256(terminal)
    bundle, updater = _bundle(tmp_path)
    config = tmp_path / "login.ini"
    config.write_text("temporary", encoding="utf-8")
    process = Mock(returncode=0)
    process.poll.return_value = 0

    def launch(command: list[str], **_kwargs: object) -> Mock:
        target = Path(next(value[6:] for value in command if value.startswith("/path:")))
        (target / "terminal64.exe").write_bytes(b"terminal-v2")
        private = target / "Config" / "accounts.dat"
        private.parent.mkdir(parents=True, exist_ok=True)
        private.write_bytes(b"must-not-survive")
        return process

    manager = Mt5TemplateManager(
        terminal,
        base_digest,
        process_launcher=launch,
    )
    with (
        patch.object(manager, "_verify_metaquotes_signature", return_value=SIGNER),
        patch(
            "windows_agent.provisioning.mt5_template.WindowsSecretStore.restrict_acl"
        ),
    ):
        digest = manager.promote_verified_update(
            bundle,
            updater,
            config,
            SIGNER,
        )

    assert digest == InstanceProvisioner._sha256(terminal)
    assert terminal.read_bytes() == b"terminal-v2"
    assert not (terminal.parent / "Config" / "accounts.dat").exists()
    marker = read_json(terminal.parent / ".tradejournal-vendor-update.json")
    assert marker["base_terminal_sha256"] == base_digest
    assert marker["previous_terminal_sha256"] == base_digest
    assert marker["terminal_sha256"] == digest

    restarted = Mt5TemplateManager(terminal, base_digest)
    with patch.object(
        restarted,
        "_verify_metaquotes_signature",
        return_value=SIGNER,
    ):
        assert restarted.current_sha256 == digest


def test_failed_template_update_preserves_original_template(tmp_path: Path) -> None:
    terminal = _template(tmp_path)
    base_digest = InstanceProvisioner._sha256(terminal)
    bundle, updater = _bundle(tmp_path)
    config = tmp_path / "login.ini"
    config.write_text("temporary", encoding="utf-8")
    process = Mock(returncode=0)
    process.poll.return_value = 0

    def launch(command: list[str], **_kwargs: object) -> Mock:
        target = Path(next(value[6:] for value in command if value.startswith("/path:")))
        managed = target / _MANAGED_RUNTIME_ASSETS[0]
        managed.write_bytes(b"tampered")
        return process

    manager = Mt5TemplateManager(
        terminal,
        base_digest,
        process_launcher=launch,
    )
    with (
        patch.object(manager, "_verify_metaquotes_signature", return_value=SIGNER),
        pytest.raises(Mt5TemplateError, match="managed runtime assets"),
    ):
        manager.promote_verified_update(bundle, updater, config, SIGNER)

    assert terminal.read_bytes() == b"terminal-v1"
    assert InstanceProvisioner._sha256(terminal) == base_digest
    assert not (terminal.parent / ".tradejournal-vendor-update.json").exists()
