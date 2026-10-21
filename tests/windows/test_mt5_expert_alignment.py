from __future__ import annotations

from pathlib import Path
from threading import Event, RLock
from unittest.mock import Mock, patch

import pytest

from windows_agent.mt5_expert_alignment import (
    JOURNAL_NAME,
    Mt5TemplateExpertError,
    Mt5TemplateExpertSync,
)
from windows_agent.provisioning.mt5_instance import (
    _MANAGED_RUNTIME_ASSETS,
    InstanceProvisioner,
)
from windows_agent.provisioning.mt5_template import Mt5TemplateError, Mt5TemplateManager
from windows_agent.provisioning.mt5_update_store import (
    UPDATER_ONLY_CONFIG_BYTES,
    UPDATER_ONLY_CONFIG_NAME,
)
from windows_agent.state_store import atomic_json, read_json

SIGNER = "CN=MetaQuotes Ltd., O=MetaQuotes Ltd., S=Lemesos, C=CY"


class FakePool:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bool]] = []

    def accept_template_rotation(self, digest: str, *, replenish: bool, stop_event: Event) -> int:
        self.calls.append((digest, replenish))
        return 2


def _rotated_template(tmp_path: Path) -> tuple[Mt5TemplateManager, Path]:
    """A golden template after one MetaQuotes update, so it carries a schema-2 seal."""
    terminal = tmp_path / "mt5-template" / "terminal64.exe"
    terminal.parent.mkdir()
    terminal.write_bytes(b"terminal-v1")
    for relative in _MANAGED_RUNTIME_ASSETS:
        asset = terminal.parent / relative
        asset.parent.mkdir(parents=True, exist_ok=True)
        asset.write_bytes(relative.as_posix().encode("utf-8"))
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    updater = bundle / "terminal64.exe"
    updater.write_bytes(b"signed-updater")
    (bundle / "payload.6090").write_bytes(b"payload")
    config = bundle / UPDATER_ONLY_CONFIG_NAME
    config.write_bytes(UPDATER_ONLY_CONFIG_BYTES)
    process = Mock(returncode=0)
    process.poll.return_value = 0

    def launch(command: list[str], **_kwargs: object) -> Mock:
        target = Path(next(value[6:] for value in command if value.startswith("/path:")))
        (target / "terminal64.exe").write_bytes(b"terminal-v2")
        return process

    manager = Mt5TemplateManager(
        terminal,
        InstanceProvisioner._sha256(terminal),
        process_launcher=launch,
    )
    with _trusted(manager), patch.object(manager, "_stop_staged_terminal"):
        manager.commit_prepared_update(
            manager.prepare_verified_update(bundle, updater, config, SIGNER)
        )
    assert read_json(terminal.parent / ".tradejournal-vendor-update.json")["schema_version"] == 2
    return manager, terminal


def _trusted(manager: Mt5TemplateManager):
    signature = patch.object(manager, "_verify_metaquotes_signature", return_value=SIGNER)
    acl = patch("windows_agent.provisioning.mt5_template.WindowsSecretStore.restrict_acl")

    class Both:
        def __enter__(self):
            signature.__enter__()
            acl.__enter__()

        def __exit__(self, *exc):
            acl.__exit__(*exc)
            signature.__exit__(*exc)

    return Both()


def _sync(tmp_path: Path, manager: Mt5TemplateManager, pool: FakePool | None = None):
    expert = tmp_path / "TradeJournalBridge-new.ex5"
    expert.write_bytes(b"tradejournal-bridge-1.01")
    sync = Mt5TemplateExpertSync(
        manager,
        template_lock=RLock(),
        expert_binary=expert,
        expert_sha256=InstanceProvisioner._sha256(expert),
        backup_root=tmp_path / "state" / "mt5-expert-backups",
        instance_pool=pool,
    )
    return sync, expert


def test_the_template_takes_the_pinned_bridge_with_a_new_seal(tmp_path: Path) -> None:
    manager, terminal = _rotated_template(tmp_path)
    pool = FakePool()
    sync, expert = _sync(tmp_path, manager, pool)
    bridge = terminal.parent / _MANAGED_RUNTIME_ASSETS[0]
    previous = InstanceProvisioner._sha256(bridge)

    with _trusted(manager):
        assert sync.align(Event()) is True
        restarted = Mt5TemplateManager(terminal, manager.configured_sha256)
        with patch.object(restarted, "_verify_metaquotes_signature", return_value=SIGNER):
            assert restarted.validate_current_quiesced() == InstanceProvisioner._sha256(terminal)

    assert bridge.read_bytes() == expert.read_bytes()
    marker = read_json(terminal.parent / ".tradejournal-vendor-update.json")
    assert marker["code_manifest_sha256"] == InstanceProvisioner._code_manifest(terminal.parent)
    assert (sync.backup_root / f"TradeJournalBridge-{previous}.ex5").is_file()
    assert not sync.journal.exists()
    assert pool.calls == [(InstanceProvisioner._sha256(terminal), True)]
    assert not list(bridge.parent.glob("*.staged"))


def test_an_aligned_template_is_left_alone(tmp_path: Path) -> None:
    manager, terminal = _rotated_template(tmp_path)
    pool = FakePool()
    sync, _expert = _sync(tmp_path, manager, pool)
    with _trusted(manager):
        assert sync.align(Event()) is True
        marker = (terminal.parent / ".tradejournal-vendor-update.json").read_bytes()
        assert sync.align(Event()) is False

    assert (terminal.parent / ".tradejournal-vendor-update.json").read_bytes() == marker
    assert len(pool.calls) == 1


def test_a_refused_seal_puts_the_previous_bridge_back(tmp_path: Path) -> None:
    manager, terminal = _rotated_template(tmp_path)
    sync, _expert = _sync(tmp_path, manager)
    bridge = terminal.parent / _MANAGED_RUNTIME_ASSETS[0]
    previous = bridge.read_bytes()

    with (
        _trusted(manager),
        patch.object(
            manager,
            "reseal_managed_code_deployment",
            side_effect=Mt5TemplateError("MT5 managed deployment changed companion code"),
        ),
        pytest.raises(Mt5TemplateExpertError, match="update failed"),
    ):
        sync.align(Event())

    assert bridge.read_bytes() == previous
    assert not sync.journal.exists()
    with _trusted(manager):
        assert manager.recover_interrupted_rotation() == InstanceProvisioner._sha256(terminal)


def test_an_invalid_template_stops_phase_one_untouched(tmp_path: Path) -> None:
    manager, terminal = _rotated_template(tmp_path)
    sync, _expert = _sync(tmp_path, manager)
    bridge = terminal.parent / _MANAGED_RUNTIME_ASSETS[0]
    previous = bridge.read_bytes()
    # A companion script changed after the last seal: the template no longer validates.
    (terminal.parent / _MANAGED_RUNTIME_ASSETS[1]).write_bytes(b"unexpected-companion")

    with _trusted(manager), pytest.raises(Mt5TemplateError):
        sync.align(Event())

    assert bridge.read_bytes() == previous
    assert not sync.journal.exists()
    assert not sync.backup_root.exists()


def test_startup_undoes_a_swap_cut_off_before_its_seal(tmp_path: Path) -> None:
    manager, terminal = _rotated_template(tmp_path)
    sync, expert = _sync(tmp_path, manager)
    bridge = terminal.parent / _MANAGED_RUNTIME_ASSETS[0]
    previous = InstanceProvisioner._sha256(bridge)
    sync.backup_root.mkdir(parents=True)
    (sync.backup_root / f"TradeJournalBridge-{previous}.ex5").write_bytes(bridge.read_bytes())
    atomic_json(sync.journal, {
        "schema_version": 1,
        "previous_expert_sha256": previous,
        "expert_sha256": sync.expert_sha256,
    })
    bridge.write_bytes(expert.read_bytes())

    sync.recover_interrupted()

    assert InstanceProvisioner._sha256(bridge) == previous
    assert not sync.journal.exists()
    with _trusted(manager):
        assert manager.recover_interrupted_rotation() == InstanceProvisioner._sha256(terminal)


def test_startup_keeps_a_swap_whose_seal_was_written(tmp_path: Path) -> None:
    manager, terminal = _rotated_template(tmp_path)
    sync, expert = _sync(tmp_path, manager)
    with _trusted(manager):
        sync.align(Event())
    bridge = terminal.parent / _MANAGED_RUNTIME_ASSETS[0]
    atomic_json(sync.journal, {
        "schema_version": 1,
        "previous_expert_sha256": "0" * 64,
        "expert_sha256": sync.expert_sha256,
    })

    sync.recover_interrupted()

    assert bridge.read_bytes() == expert.read_bytes()
    assert not sync.journal.exists()


def test_an_unknown_template_bridge_stops_phase_one(tmp_path: Path) -> None:
    manager, terminal = _rotated_template(tmp_path)
    sync, _expert = _sync(tmp_path, manager)
    sync.backup_root.mkdir(parents=True)
    atomic_json(sync.journal, {
        "schema_version": 1,
        "previous_expert_sha256": "0" * 64,
        "expert_sha256": "1" * 64,
    })

    sync.recover_interrupted()
    assert sync.journal.exists()
    with _trusted(manager), pytest.raises(Mt5TemplateExpertError, match="needs attention"):
        sync.align(Event())
    assert (sync.backup_root / JOURNAL_NAME).exists()
