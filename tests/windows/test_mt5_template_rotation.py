from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

import windows_agent.provisioning.mt5_template as template_module
from windows_agent.provisioning.mt5_instance import (
    InstanceProvisioner,
    _MANAGED_RUNTIME_ASSETS,
)
from windows_agent.provisioning.mt5_template import (
    Mt5TemplateError,
    Mt5TemplateManager,
    Mt5TemplateRecoveryRequired,
)
from windows_agent.provisioning.mt5_update_store import (
    UPDATER_ONLY_CONFIG_BYTES,
    UPDATER_ONLY_CONFIG_NAME,
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


def _candidate_setup(
    tmp_path: Path,
) -> tuple[Mt5TemplateManager, Path, str, Path, Path, Path]:
    terminal = _template(tmp_path)
    base_digest = InstanceProvisioner._sha256(terminal)
    bundle, updater = _bundle(tmp_path)
    config = bundle / UPDATER_ONLY_CONFIG_NAME
    config.write_bytes(UPDATER_ONLY_CONFIG_BYTES)
    process = Mock(returncode=0)
    process.poll.return_value = 0

    def launch(command: list[str], **_kwargs: object) -> Mock:
        target = Path(
            next(value[6:] for value in command if value.startswith("/path:"))
        )
        (target / "terminal64.exe").write_bytes(b"terminal-v2")
        return process

    manager = Mt5TemplateManager(
        terminal,
        base_digest,
        process_launcher=launch,
    )
    return manager, terminal, base_digest, bundle, updater, config


def test_prepare_verified_update_does_not_publish_golden(tmp_path: Path) -> None:
    manager, terminal, base_digest, bundle, updater, config = _candidate_setup(
        tmp_path
    )
    golden_manifest = InstanceProvisioner._tree_manifest(terminal.parent)

    with (
        patch.object(manager, "_verify_metaquotes_signature", return_value=SIGNER),
        patch.object(manager, "_stop_staged_terminal"),
        patch.object(manager, "_stop_interrupted_rotation_processes"),
        patch(
            "windows_agent.provisioning.mt5_template.WindowsSecretStore.restrict_acl"
        ),
    ):
        prepared = manager.prepare_verified_update(
            bundle,
            updater,
            config,
            SIGNER,
        )

    assert terminal.read_bytes() == b"terminal-v1"
    assert InstanceProvisioner._sha256(terminal) == base_digest
    assert InstanceProvisioner._tree_manifest(terminal.parent) == golden_manifest
    assert manager.current_sha256 == base_digest
    assert not (terminal.parent / ".tradejournal-vendor-update.json").exists()
    assert prepared.root != terminal.parent
    assert (prepared.root / "terminal64.exe").read_bytes() == b"terminal-v2"
    assert (prepared.root / ".tradejournal-vendor-update.json").is_file()


def test_prepare_consumes_a_working_copy_not_the_pending_receipt(
    tmp_path: Path,
) -> None:
    terminal = _template(tmp_path)
    base_digest = InstanceProvisioner._sha256(terminal)
    bundle, updater = _bundle(tmp_path)
    config = bundle / UPDATER_ONLY_CONFIG_NAME
    config.write_bytes(UPDATER_ONLY_CONFIG_BYTES)
    originals = {
        child.name: child.read_bytes()
        for child in bundle.iterdir()
        if child.is_file()
    }
    process = Mock(returncode=0)
    process.poll.return_value = 0

    def launch(command: list[str], **kwargs: object) -> Mock:
        working = Path(str(kwargs["cwd"]))
        assert working != bundle
        assert Path(command[0]).parent == working
        (working / "payload.6090").unlink()
        target = Path(
            next(value[6:] for value in command if value.startswith("/path:"))
        )
        (target / "terminal64.exe").write_bytes(b"terminal-v2")
        return process

    manager = Mt5TemplateManager(
        terminal,
        base_digest,
        process_launcher=launch,
    )
    with (
        patch.object(manager, "_verify_metaquotes_signature", return_value=SIGNER),
        patch.object(manager, "_stop_staged_terminal"),
        patch(
            "windows_agent.provisioning.mt5_template.WindowsSecretStore.restrict_acl"
        ),
    ):
        prepared = manager.prepare_verified_update(bundle, updater, config, SIGNER)

    assert {
        child.name: child.read_bytes()
        for child in bundle.iterdir()
        if child.is_file()
    } == originals
    assert not (
        terminal.parent.parent / f".{terminal.parent.name}.vendor-working"
    ).exists()
    manager.discard_prepared_update(prepared)


def test_prepare_removes_generated_examples_before_target_manifest_binding(
    tmp_path: Path,
) -> None:
    terminal = _template(tmp_path)
    base_digest = InstanceProvisioner._sha256(terminal)
    source_code = InstanceProvisioner._code_manifest(terminal.parent)
    bundle, updater = _bundle(tmp_path)
    config = bundle / UPDATER_ONLY_CONFIG_NAME
    config.write_bytes(UPDATER_ONLY_CONFIG_BYTES)

    expected_root = tmp_path / "runtime-sanitized-target"
    shutil.copytree(terminal.parent, expected_root)
    expected_terminal = expected_root / "terminal64.exe"
    expected_terminal.write_bytes(b"terminal-v2")
    target_digest = InstanceProvisioner._sha256(expected_terminal)
    target_code = InstanceProvisioner._code_manifest(expected_root)

    process = Mock(returncode=0)
    process.poll.return_value = 0

    def launch(command: list[str], **_kwargs: object) -> Mock:
        target = Path(
            next(value[6:] for value in command if value.startswith("/path:"))
        )
        (target / "terminal64.exe").write_bytes(b"terminal-v2")
        generated = target / "MQL5" / "Experts" / "Examples" / "foo.ex5"
        generated.parent.mkdir(parents=True, exist_ok=True)
        generated.write_bytes(b"vendor-generated-example")
        return process

    manager = Mt5TemplateManager(
        terminal,
        base_digest,
        process_launcher=launch,
    )
    events: list[str] = []
    sanitize_template = manager._sanitize_template

    def stop_staged_terminal(
        _terminal: Path,
        _updater: Path | None = None,
    ) -> None:
        events.append("stopped")

    def sanitize(root: Path) -> None:
        events.append("sanitize")
        assert events == ["stopped", "sanitize"]
        sanitize_template(root)

    with (
        patch.object(
            manager,
            "_verify_metaquotes_signature",
            return_value=SIGNER,
        ),
        patch.object(
            manager,
            "_stop_staged_terminal",
            side_effect=stop_staged_terminal,
        ),
        patch.object(manager, "_sanitize_template", side_effect=sanitize),
        patch(
            "windows_agent.provisioning.mt5_template.WindowsSecretStore.restrict_acl"
        ),
    ):
        prepared = manager.prepare_verified_update(
            bundle,
            updater,
            config,
            SIGNER,
            expected_source_terminal_sha256=base_digest,
            expected_source_code_manifest_sha256=source_code,
            expected_target_terminal_sha256=target_digest,
            expected_target_code_manifest_sha256=target_code,
        )
        assert prepared.target_code_manifest_sha256 == target_code
        assert not (
            prepared.root / "MQL5" / "Experts" / "Examples"
        ).exists()
        manager.commit_prepared_update(prepared)

    assert events == ["stopped", "sanitize"]
    assert InstanceProvisioner._code_manifest(terminal.parent) == target_code
    assert not (terminal.parent / "MQL5" / "Experts" / "Examples").exists()


def test_commit_publishes_prepared_candidate(tmp_path: Path) -> None:
    manager, terminal, _base_digest, bundle, updater, config = _candidate_setup(
        tmp_path
    )

    with (
        patch.object(manager, "_verify_metaquotes_signature", return_value=SIGNER),
        patch.object(manager, "_stop_staged_terminal"),
        patch(
            "windows_agent.provisioning.mt5_template.WindowsSecretStore.restrict_acl"
        ),
    ):
        prepared = manager.prepare_verified_update(
            bundle,
            updater,
            config,
            SIGNER,
        )
        digest = manager.commit_prepared_update(prepared)

    assert digest == prepared.target_terminal_sha256
    assert manager.current_sha256 == digest
    assert terminal.read_bytes() == b"terminal-v2"
    assert not prepared.root.exists()
    marker = read_json(terminal.parent / ".tradejournal-vendor-update.json")
    assert marker["terminal_sha256"] == digest
    assert marker["code_manifest_sha256"] == (
        prepared.target_code_manifest_sha256
    )


def test_prepared_release_chain_publishes_only_final_candidate(
    tmp_path: Path,
) -> None:
    terminal = _template(tmp_path)
    base_digest = InstanceProvisioner._sha256(terminal)
    source_code = InstanceProvisioner._code_manifest(terminal.parent)

    def make_bundle(name: str) -> tuple[Path, Path, Path]:
        bundle = tmp_path / name
        bundle.mkdir()
        updater = bundle / "terminal64.exe"
        updater.write_bytes(b"signed-updater")
        (bundle / "payload.6090").write_bytes(name.encode("ascii"))
        config = bundle / UPDATER_ONLY_CONFIG_NAME
        config.write_bytes(UPDATER_ONLY_CONFIG_BYTES)
        return bundle, updater, config

    bundle_ab, updater_ab, config_ab = make_bundle("bundle-ab")
    bundle_bc, updater_bc, config_bc = make_bundle("bundle-bc")
    release_b_root = tmp_path / "release-b"
    release_c_root = tmp_path / "release-c"
    shutil.copytree(terminal.parent, release_b_root)
    shutil.copytree(terminal.parent, release_c_root)
    (release_b_root / "terminal64.exe").write_bytes(b"terminal-v2")
    (release_c_root / "terminal64.exe").write_bytes(b"terminal-v3")
    release_b_terminal = InstanceProvisioner._sha256(
        release_b_root / "terminal64.exe"
    )
    release_b_code = InstanceProvisioner._code_manifest(release_b_root)
    release_c_terminal = InstanceProvisioner._sha256(
        release_c_root / "terminal64.exe"
    )
    release_c_code = InstanceProvisioner._code_manifest(release_c_root)
    process = Mock(returncode=0)
    process.poll.return_value = 0
    targets = iter((b"terminal-v2", b"terminal-v3"))

    def launch(command: list[str], **_kwargs: object) -> Mock:
        target = Path(
            next(value[6:] for value in command if value.startswith("/path:"))
        )
        (target / "terminal64.exe").write_bytes(next(targets))
        return process

    manager = Mt5TemplateManager(
        terminal,
        base_digest,
        process_launcher=launch,
    )
    with (
        patch.object(manager, "_verify_metaquotes_signature", return_value=SIGNER),
        patch.object(manager, "_stop_staged_terminal"),
        patch(
            "windows_agent.provisioning.mt5_template.WindowsSecretStore.restrict_acl"
        ),
    ):
        prepared = manager.prepare_verified_update(
            bundle_ab,
            updater_ab,
            config_ab,
            SIGNER,
            expected_source_terminal_sha256=base_digest,
            expected_source_code_manifest_sha256=source_code,
            expected_target_terminal_sha256=release_b_terminal,
            expected_target_code_manifest_sha256=release_b_code,
        )
        advanced = manager.advance_prepared_update(
            prepared,
            bundle_bc,
            updater_bc,
            config_bc,
            SIGNER,
            expected_source_terminal_sha256=release_b_terminal,
            expected_source_code_manifest_sha256=release_b_code,
            expected_target_terminal_sha256=release_c_terminal,
            expected_target_code_manifest_sha256=release_c_code,
        )
        assert terminal.read_bytes() == b"terminal-v1"
        digest = manager.commit_prepared_update(advanced)

    assert digest == release_c_terminal
    assert terminal.read_bytes() == b"terminal-v3"
    marker = read_json(terminal.parent / ".tradejournal-vendor-update.json")
    assert marker["schema_version"] == 2
    assert marker["code_manifest_sha256"] == release_c_code


def test_failed_second_hop_cleanup_cannot_discard_live_candidate(
    tmp_path: Path,
) -> None:
    manager, _terminal, _base_digest, bundle, updater, config = _candidate_setup(
        tmp_path
    )
    with (
        patch.object(manager, "_verify_metaquotes_signature", return_value=SIGNER),
        patch.object(manager, "_stop_staged_terminal"),
        patch(
            "windows_agent.provisioning.mt5_template.WindowsSecretStore.restrict_acl"
        ),
    ):
        prepared = manager.prepare_verified_update(bundle, updater, config, SIGNER)

    next_bundle = tmp_path / "bundle-next"
    next_bundle.mkdir()
    next_updater = next_bundle / "terminal64.exe"
    next_updater.write_bytes(b"signed-updater")
    (next_bundle / "payload.6091").write_bytes(b"payload-next")
    next_config = next_bundle / UPDATER_ONLY_CONFIG_NAME
    next_config.write_bytes(UPDATER_ONLY_CONFIG_BYTES)
    target_root = tmp_path / "release-next"
    shutil.copytree(prepared.root, target_root)
    (target_root / "terminal64.exe").write_bytes(b"terminal-v3")
    target_terminal = InstanceProvisioner._sha256(
        target_root / "terminal64.exe"
    )
    target_code = InstanceProvisioner._code_manifest(target_root)
    process = Mock(returncode=0)
    process.poll.return_value = 0

    def launch(command: list[str], **_kwargs: object) -> Mock:
        destination = Path(
            next(value[6:] for value in command if value.startswith("/path:"))
        )
        (destination / "terminal64.exe").write_bytes(b"terminal-v3")
        return process

    manager._process_launcher = launch
    with (
        patch.object(manager, "_verify_metaquotes_signature", return_value=SIGNER),
        patch.object(
            manager,
            "_stop_staged_terminal",
            side_effect=Mt5TemplateError("quiescence failed"),
        ),
        patch.object(
            manager,
            "_stop_interrupted_rotation_processes",
            side_effect=Mt5TemplateError("process still live"),
        ),
        pytest.raises(
            Mt5TemplateRecoveryRequired,
            match="cleanup failed",
        ),
    ):
        manager.advance_prepared_update(
            prepared,
            next_bundle,
            next_updater,
            next_config,
            SIGNER,
            expected_source_terminal_sha256=(
                prepared.target_terminal_sha256
            ),
            expected_source_code_manifest_sha256=(
                prepared.target_code_manifest_sha256
            ),
            expected_target_terminal_sha256=target_terminal,
            expected_target_code_manifest_sha256=target_code,
        )

    assert prepared.root.is_dir()
    with pytest.raises(Mt5TemplateRecoveryRequired, match="requires recovery"):
        manager.discard_prepared_update(prepared)


def test_rotated_marker_rejects_companion_code_tampering(
    tmp_path: Path,
) -> None:
    manager, terminal, base_digest, bundle, updater, config = _candidate_setup(
        tmp_path
    )
    with (
        patch.object(manager, "_verify_metaquotes_signature", return_value=SIGNER),
        patch.object(manager, "_stop_staged_terminal"),
        patch(
            "windows_agent.provisioning.mt5_template.WindowsSecretStore.restrict_acl"
        ),
    ):
        prepared = manager.prepare_verified_update(bundle, updater, config, SIGNER)
        manager.commit_prepared_update(prepared)

    (terminal.parent / "vendor-companion.dll").write_bytes(b"tampered")
    restarted = Mt5TemplateManager(terminal, base_digest)
    with (
        patch.object(restarted, "_verify_metaquotes_signature", return_value=SIGNER),
        pytest.raises(Mt5TemplateError, match="rotation record is invalid"),
    ):
        _ = restarted.current_sha256


def test_legacy_schema_one_marker_is_validated_without_rewriting(
    tmp_path: Path,
) -> None:
    terminal = _template(tmp_path)
    base_digest = InstanceProvisioner._sha256(terminal)
    marker_path = terminal.parent / ".tradejournal-vendor-update.json"
    template_module.atomic_json(
        marker_path,
        {
            "schema_version": 1,
            "base_terminal_sha256": base_digest,
            "previous_terminal_sha256": base_digest,
            "terminal_sha256": base_digest,
            "signer_subject": SIGNER,
            "verified_at_unix_ms": 1,
        },
    )
    before = marker_path.read_bytes()
    manager = Mt5TemplateManager(terminal, base_digest)

    with (
        patch.object(
            manager,
            "_verify_metaquotes_signature",
            return_value=SIGNER,
        ),
        patch(
            "windows_agent.provisioning.mt5_template.WindowsSecretStore.restrict_acl"
        ),
    ):
        assert manager.current_sha256 == base_digest

    assert marker_path.read_bytes() == before


def test_rotated_marker_allows_managed_bridge_release_replacement(
    tmp_path: Path,
) -> None:
    manager, terminal, base_digest, bundle, updater, config = _candidate_setup(
        tmp_path
    )
    with (
        patch.object(manager, "_verify_metaquotes_signature", return_value=SIGNER),
        patch.object(manager, "_stop_staged_terminal"),
        patch(
            "windows_agent.provisioning.mt5_template.WindowsSecretStore.restrict_acl"
        ),
    ):
        prepared = manager.prepare_verified_update(bundle, updater, config, SIGNER)
        manager.commit_prepared_update(prepared)

    managed = terminal.parent / _MANAGED_RUNTIME_ASSETS[0]
    previous = tmp_path / "previous-bridge.ex5"
    shutil.copy2(managed, previous)
    previous_digest = InstanceProvisioner._sha256(previous)
    managed.write_bytes(b"new-tradejournal-release")
    next_digest = InstanceProvisioner._sha256(managed)
    with (
        patch.object(
            manager,
            "_verify_metaquotes_signature",
            return_value=SIGNER,
        ),
        patch(
            "windows_agent.provisioning.mt5_template.WindowsSecretStore.restrict_acl"
        ),
    ):
        manager.reseal_managed_code_deployment(
            previous,
            previous_digest,
            next_digest,
        )
        first_marker = (terminal.parent / ".tradejournal-vendor-update.json").read_bytes()
        manager.reseal_managed_code_deployment(
            previous,
            previous_digest,
            next_digest,
        )
        assert (
            terminal.parent / ".tradejournal-vendor-update.json"
        ).read_bytes() == first_marker
    restarted = Mt5TemplateManager(terminal, base_digest)
    with patch.object(
        restarted,
        "_verify_metaquotes_signature",
        return_value=SIGNER,
    ):
        assert restarted.current_sha256 == InstanceProvisioner._sha256(terminal)


def test_quiesced_validation_never_discards_interrupted_rotation(
    tmp_path: Path,
) -> None:
    manager, terminal, _base_digest, _bundle, _updater, _config = _candidate_setup(
        tmp_path
    )
    staging = terminal.parent.parent / f".{terminal.parent.name}.vendor-staging"
    staging.mkdir()

    with pytest.raises(Mt5TemplateRecoveryRequired, match="requires recovery"):
        manager.validate_current_quiesced()

    assert staging.is_dir()


def test_commit_fsync_failure_restores_old_golden_without_losing_candidate(
    tmp_path: Path,
) -> None:
    manager, terminal, base_digest, bundle, updater, config = _candidate_setup(
        tmp_path
    )

    with (
        patch.object(manager, "_verify_metaquotes_signature", return_value=SIGNER),
        patch.object(manager, "_stop_staged_terminal"),
        patch(
            "windows_agent.provisioning.mt5_template.WindowsSecretStore.restrict_acl"
        ),
    ):
        prepared = manager.prepare_verified_update(
            bundle,
            updater,
            config,
            SIGNER,
        )

    with (
        patch.object(manager, "_verify_metaquotes_signature", return_value=SIGNER),
        patch(
            "windows_agent.provisioning.mt5_template.fsync_directory",
            side_effect=(OSError("fsync failed"), None),
        ),
    ):
        with pytest.raises(OSError, match="fsync failed"):
            manager.commit_prepared_update(prepared)

    assert terminal.read_bytes() == b"terminal-v1"
    assert manager.current_sha256 == base_digest
    assert prepared.root.is_dir()
    assert not (
        terminal.parent.parent / f".{terminal.parent.name}.vendor-backup"
    ).exists()
    manager.discard_prepared_update(prepared)


def test_same_process_recovery_restores_golden_after_rollback_failure(
    tmp_path: Path,
) -> None:
    manager, terminal, base_digest, bundle, updater, config = _candidate_setup(
        tmp_path
    )
    with (
        patch.object(manager, "_verify_metaquotes_signature", return_value=SIGNER),
        patch.object(manager, "_stop_staged_terminal"),
        patch(
            "windows_agent.provisioning.mt5_template.WindowsSecretStore.restrict_acl"
        ),
    ):
        prepared = manager.prepare_verified_update(bundle, updater, config, SIGNER)

    original_replace = template_module._replace_directory_with_retry
    calls = 0

    def fail_publish_and_rollback(source: Path, destination: Path) -> None:
        nonlocal calls
        calls += 1
        if calls in (2, 3):
            raise OSError("simulated directory move failure")
        original_replace(source, destination)

    with (
        patch.object(manager, "_verify_metaquotes_signature", return_value=SIGNER),
        patch(
            "windows_agent.provisioning.mt5_template._replace_directory_with_retry",
            side_effect=fail_publish_and_rollback,
        ),
        pytest.raises(
            Mt5TemplateRecoveryRequired,
            match="publication rollback failed",
        ),
    ):
        manager.commit_prepared_update(prepared)

    assert not terminal.parent.exists()
    with pytest.raises(Mt5TemplateRecoveryRequired, match="requires recovery"):
        manager.discard_prepared_update(prepared)

    with patch.object(manager, "_stop_interrupted_rotation_processes"):
        assert manager.recover_interrupted_rotation() == base_digest

    assert terminal.read_bytes() == b"terminal-v1"
    assert not prepared.root.exists()


def test_discard_removes_candidate_without_publishing(tmp_path: Path) -> None:
    manager, terminal, base_digest, bundle, updater, config = _candidate_setup(
        tmp_path
    )

    with (
        patch.object(manager, "_verify_metaquotes_signature", return_value=SIGNER),
        patch.object(manager, "_stop_staged_terminal"),
        patch(
            "windows_agent.provisioning.mt5_template.WindowsSecretStore.restrict_acl"
        ),
    ):
        prepared = manager.prepare_verified_update(
            bundle,
            updater,
            config,
            SIGNER,
        )

    manager.discard_prepared_update(prepared)

    assert not prepared.root.exists()
    assert terminal.read_bytes() == b"terminal-v1"
    assert manager.current_sha256 == base_digest
    with pytest.raises(Mt5TemplateError, match="candidate is invalid"):
        manager.discard_prepared_update(prepared)


@pytest.mark.parametrize(
    ("binding_kwargs", "message"),
    (
        (
            {
                "expected_source_terminal_sha256": "f" * 64,
                "expected_source_code_manifest_sha256": "e" * 64,
            },
            "source release mismatch",
        ),
        (
            {
                "expected_target_terminal_sha256": "f" * 64,
                "expected_target_code_manifest_sha256": "e" * 64,
            },
            "target release mismatch",
        ),
    ),
)
def test_prepare_rejects_source_or_target_release_mismatch(
    tmp_path: Path,
    binding_kwargs: dict[str, str],
    message: str,
) -> None:
    manager, terminal, base_digest, bundle, updater, config = _candidate_setup(
        tmp_path
    )
    staging = terminal.parent.parent / f".{terminal.parent.name}.vendor-staging"

    with (
        patch.object(manager, "_verify_metaquotes_signature", return_value=SIGNER),
        patch.object(manager, "_stop_staged_terminal"),
        patch.object(manager, "_stop_interrupted_rotation_processes"),
        patch(
            "windows_agent.provisioning.mt5_template.WindowsSecretStore.restrict_acl"
        ),
        pytest.raises(Mt5TemplateError, match=message),
    ):
        manager.prepare_verified_update(
            bundle,
            updater,
            config,
            SIGNER,
            **binding_kwargs,
        )

    assert terminal.read_bytes() == b"terminal-v1"
    assert InstanceProvisioner._sha256(terminal) == base_digest
    assert not staging.exists()


def test_commit_rejects_candidate_modified_after_prepare(tmp_path: Path) -> None:
    manager, terminal, base_digest, bundle, updater, config = _candidate_setup(
        tmp_path
    )

    with (
        patch.object(manager, "_verify_metaquotes_signature", return_value=SIGNER),
        patch.object(manager, "_stop_staged_terminal"),
        patch(
            "windows_agent.provisioning.mt5_template.WindowsSecretStore.restrict_acl"
        ),
    ):
        prepared = manager.prepare_verified_update(
            bundle,
            updater,
            config,
            SIGNER,
        )
        (prepared.root / "unexpected.dat").write_bytes(b"tampered")
        with pytest.raises(Mt5TemplateError, match="candidate changed"):
            manager.commit_prepared_update(prepared)

    assert terminal.read_bytes() == b"terminal-v1"
    assert InstanceProvisioner._sha256(terminal) == base_digest
    assert prepared.root.is_dir()
    manager.discard_prepared_update(prepared)
    assert not prepared.root.exists()


def test_verified_update_atomically_rotates_clean_template(tmp_path: Path) -> None:
    terminal = _template(tmp_path)
    base_digest = InstanceProvisioner._sha256(terminal)
    bundle, updater = _bundle(tmp_path)
    config = bundle / UPDATER_ONLY_CONFIG_NAME
    config.write_bytes(UPDATER_ONLY_CONFIG_BYTES)
    process = Mock(returncode=0)
    process.poll.return_value = 0

    def launch(command: list[str], **_kwargs: object) -> Mock:
        target = Path(
            next(value[6:] for value in command if value.startswith("/path:"))
        )
        (target / "terminal64.exe").write_bytes(b"terminal-v2")
        private = target / "Config" / "accounts.dat"
        private.parent.mkdir(parents=True, exist_ok=True)
        private.write_bytes(b"must-not-survive")
        certificates = target / "Config" / "certificates"
        certificates.mkdir()
        (certificates / "client.cer").write_bytes(b"must-not-survive")
        return process

    manager = Mt5TemplateManager(
        terminal,
        base_digest,
        process_launcher=launch,
    )
    with (
        patch.object(manager, "_verify_metaquotes_signature", return_value=SIGNER),
        patch.object(manager, "_stop_staged_terminal"),
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
    assert not (terminal.parent / "Config" / "certificates").exists()
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
    config = bundle / UPDATER_ONLY_CONFIG_NAME
    config.write_bytes(UPDATER_ONLY_CONFIG_BYTES)
    process = Mock(returncode=0)
    process.poll.return_value = 0

    def launch(command: list[str], **_kwargs: object) -> Mock:
        target = Path(
            next(value[6:] for value in command if value.startswith("/path:"))
        )
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
        patch.object(manager, "_stop_staged_terminal"),
        patch.object(manager, "_stop_interrupted_rotation_processes"),
        pytest.raises(Mt5TemplateError, match="managed runtime assets"),
    ):
        manager.promote_verified_update(bundle, updater, config, SIGNER)

    assert terminal.read_bytes() == b"terminal-v1"
    assert InstanceProvisioner._sha256(terminal) == base_digest
    assert not (terminal.parent / ".tradejournal-vendor-update.json").exists()


def test_restart_stops_exact_orphan_update_processes_before_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminal = _template(tmp_path)
    base_digest = InstanceProvisioner._sha256(terminal)
    manager = Mt5TemplateManager(terminal, base_digest)
    parent = terminal.parent.parent
    working = parent / f".{terminal.parent.name}.vendor-working"
    staging = parent / f".{terminal.parent.name}.vendor-staging"
    working.mkdir()
    staging.mkdir()
    working_updater = working / "terminal64.exe"
    staged_terminal = staging / "terminal64.exe"
    working_updater.write_bytes(b"orphan-updater")
    staged_terminal.write_bytes(b"orphan-relaunch")

    events: list[str] = []
    updater_process = Mock(info={"pid": 41, "exe": str(working_updater)})
    updater_process.terminate.side_effect = lambda: events.append(
        "terminate-working"
    )
    staged_process = Mock(info={"pid": 42, "exe": str(staged_terminal)})
    staged_process.terminate.side_effect = lambda: events.append(
        "terminate-staging"
    )
    unrelated_process = Mock(info={"pid": 43, "exe": str(terminal)})
    scans = iter(
        (
            [updater_process, staged_process, unrelated_process],
            [],
        )
    )

    def process_iter(_attrs: tuple[str, str]) -> list[Mock]:
        events.append("scan")
        return next(scans, [])

    original_remove = manager._remove_tree

    def tracked_remove(path: Path) -> None:
        events.append(f"remove-{path.name}")
        original_remove(path)

    monkeypatch.setattr(manager, "_remove_tree", tracked_remove)
    with (
        patch("psutil.process_iter", side_effect=process_iter),
        patch(
            "psutil.wait_procs",
            return_value=([updater_process, staged_process], []),
        ) as wait_procs,
    ):
        assert manager.current_sha256 == base_digest

    updater_process.terminate.assert_called_once_with()
    staged_process.terminate.assert_called_once_with()
    unrelated_process.terminate.assert_not_called()
    wait_procs.assert_called_once_with(
        [updater_process, staged_process],
        timeout=15.0,
    )
    assert events[:4] == [
        "scan",
        "terminate-working",
        "terminate-staging",
        "scan",
    ]
    assert not working.exists()
    assert not staging.exists()


def test_update_process_quiescence_catches_delayed_terminal_relaunch(
    tmp_path: Path,
) -> None:
    terminal = _template(tmp_path)
    process = Mock(info={"pid": 42, "exe": str(terminal)})
    scans = iter(([], [process], [], [], []))

    with (
        patch("psutil.process_iter", side_effect=lambda _attrs: next(scans)),
        patch("psutil.wait_procs", return_value=([process], [])) as wait_procs,
        patch("windows_agent.provisioning.mt5_template.time.sleep"),
    ):
        Mt5TemplateManager._stop_interrupted_rotation_processes((terminal,))

    process.terminate.assert_called_once_with()
    wait_procs.assert_called_once_with([process], timeout=15.0)


def test_update_process_quiescence_fails_closed_on_access_denied(
    tmp_path: Path,
) -> None:
    import psutil

    terminal = _template(tmp_path)
    info = Mock()
    info.get.side_effect = psutil.AccessDenied(pid=42)
    process = Mock(info=info)

    with (
        patch("psutil.process_iter", return_value=[process]),
        pytest.raises(
            Mt5TemplateError,
            match="recovery process scan failed",
        ),
    ):
        Mt5TemplateManager._stop_interrupted_rotation_processes((terminal,))


def test_failed_updater_stops_relaunched_candidate_before_cleanup(
    tmp_path: Path,
) -> None:
    terminal = _template(tmp_path)
    base_digest = InstanceProvisioner._sha256(terminal)
    bundle, updater = _bundle(tmp_path)
    config = bundle / UPDATER_ONLY_CONFIG_NAME
    config.write_bytes(UPDATER_ONLY_CONFIG_BYTES)
    failed_updater = Mock(returncode=17)
    failed_updater.poll.return_value = 17

    def launch(command: list[str], **_kwargs: object) -> Mock:
        target = Path(
            next(value[6:] for value in command if value.startswith("/path:"))
        )
        (target / "terminal64.exe").write_bytes(b"partial-terminal-v2")
        return failed_updater

    manager = Mt5TemplateManager(
        terminal,
        base_digest,
        process_launcher=launch,
    )
    parent = terminal.parent.parent
    working = parent / f".{terminal.parent.name}.vendor-working"
    staging = parent / f".{terminal.parent.name}.vendor-staging"
    staged_process = Mock(
        info={"pid": 42, "exe": str(staging / "terminal64.exe")}
    )

    with (
        patch.object(manager, "_verify_metaquotes_signature", return_value=SIGNER),
        patch(
            "psutil.process_iter",
            side_effect=([staged_process], [], [], []),
        ),
        patch(
            "psutil.wait_procs",
            return_value=([staged_process], []),
        ) as wait_procs,
        pytest.raises(Mt5TemplateError, match="MT5 template update failed"),
    ):
        manager.prepare_verified_update(bundle, updater, config, SIGNER)

    staged_process.terminate.assert_called_once_with()
    wait_procs.assert_called_once_with([staged_process], timeout=15.0)
    assert terminal.read_bytes() == b"terminal-v1"
    assert not working.exists()
    assert not staging.exists()


def test_restart_preserves_rotation_debris_when_process_stop_is_unconfirmed(
    tmp_path: Path,
) -> None:
    terminal = _template(tmp_path)
    base_digest = InstanceProvisioner._sha256(terminal)
    manager = Mt5TemplateManager(terminal, base_digest)
    parent = terminal.parent.parent
    working = parent / f".{terminal.parent.name}.vendor-working"
    staging = parent / f".{terminal.parent.name}.vendor-staging"
    working.mkdir()
    staging.mkdir()
    working_updater = working / "terminal64.exe"
    working_updater.write_bytes(b"orphan-updater")
    process = Mock(info={"pid": 41, "exe": str(working_updater)})

    with (
        patch("psutil.process_iter", return_value=[process]),
        patch("psutil.wait_procs", return_value=([process], [])),
        pytest.raises(
            Mt5TemplateError,
            match="^MT5 template recovery process stop failed$",
        ),
    ):
        _ = manager.current_sha256

    assert working.is_dir()
    assert staging.is_dir()


def test_committed_template_cleanup_is_deferred_without_stale_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminal = _template(tmp_path)
    base_digest = InstanceProvisioner._sha256(terminal)
    bundle, updater = _bundle(tmp_path)
    config = bundle / UPDATER_ONLY_CONFIG_NAME
    config.write_bytes(UPDATER_ONLY_CONFIG_BYTES)
    process = Mock(returncode=0)
    process.poll.return_value = 0

    def launch(command: list[str], **_kwargs: object) -> Mock:
        target = Path(
            next(value[6:] for value in command if value.startswith("/path:"))
        )
        (target / "terminal64.exe").write_bytes(b"terminal-v2")
        return process

    manager = Mt5TemplateManager(
        terminal,
        base_digest,
        process_launcher=launch,
    )
    backup = terminal.parent.parent / f".{terminal.parent.name}.vendor-backup"
    original_remove = manager._remove_tree

    def fail_published_backup_cleanup(path: Path) -> None:
        if path == backup and path.exists():
            raise OSError("sharing violation")
        original_remove(path)

    monkeypatch.setattr(manager, "_remove_tree", fail_published_backup_cleanup)
    with (
        patch.object(manager, "_verify_metaquotes_signature", return_value=SIGNER),
        patch.object(manager, "_stop_staged_terminal"),
        patch(
            "windows_agent.provisioning.mt5_template.WindowsSecretStore.restrict_acl"
        ),
    ):
        digest = manager.promote_verified_update(bundle, updater, config, SIGNER)

    assert digest == InstanceProvisioner._sha256(terminal)
    assert manager.current_sha256 == digest
    assert terminal.read_bytes() == b"terminal-v2"
    assert backup.is_dir()

    monkeypatch.setattr(manager, "_remove_tree", original_remove)
    restarted = Mt5TemplateManager(terminal, base_digest)
    with patch.object(
        restarted,
        "_verify_metaquotes_signature",
        return_value=SIGNER,
    ), patch.object(restarted, "_stop_interrupted_rotation_processes"):
        assert restarted.current_sha256 == digest
    assert not backup.exists()


def test_restart_restores_valid_backup_before_rejecting_corrupt_candidate(
    tmp_path: Path,
) -> None:
    terminal = _template(tmp_path)
    base_digest = InstanceProvisioner._sha256(terminal)
    backup = terminal.parent.parent / f".{terminal.parent.name}.vendor-backup"
    shutil.copytree(terminal.parent, backup)
    terminal.write_bytes(b"corrupt-published-candidate")

    restarted = Mt5TemplateManager(terminal, base_digest)
    with patch.object(restarted, "_stop_interrupted_rotation_processes"):
        assert restarted.current_sha256 == base_digest

    assert terminal.read_bytes() == b"terminal-v1"
    assert not backup.exists()


def test_template_directory_move_retries_transient_windows_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    (source / "marker").write_text("complete", encoding="utf-8")
    real_replace = template_module.durable_replace
    attempts = 0
    delays: list[float] = []

    def flaky_replace(current: Path, target: Path) -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            error = OSError("sharing violation")
            error.winerror = 32
            raise error
        real_replace(current, target)

    monkeypatch.setattr(template_module, "durable_replace", flaky_replace)
    monkeypatch.setattr(template_module.time, "sleep", delays.append)

    template_module._replace_directory_with_retry(source, destination)

    assert attempts == 3
    assert delays == [0.1, 0.2]
    assert (destination / "marker").read_text(encoding="utf-8") == "complete"
