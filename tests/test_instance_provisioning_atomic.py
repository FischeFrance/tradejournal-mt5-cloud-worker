from __future__ import annotations

import hashlib
import stat
from pathlib import Path
from uuid import uuid4

import pytest

from windows_agent.provisioning.mt5_instance import InstanceProvisioner
from windows_agent.state_store import read_json


def _template(root: Path) -> Path:
    terminal = root / "template" / "terminal64.exe"
    terminal.parent.mkdir()
    terminal.write_bytes(b"terminal")
    (terminal.parent / "config.dat").write_bytes(b"configuration")
    return terminal


def _sealed_runtime_instance(
    tmp_path: Path,
) -> tuple[InstanceProvisioner, str, Path, Path]:
    terminal = _template(tmp_path)
    terminal_root = terminal.parent
    assets = (
        Path("MQL5/Experts/TradeJournal/TradeJournalBridge.ex5"),
        Path("MQL5/Scripts/TradeJournal/TradeJournalDiscovery.ex5"),
        Path("MQL5/Scripts/TradeJournal/TradeJournalLoader.ex5"),
    )
    for relative in assets:
        asset = terminal_root / relative
        asset.parent.mkdir(parents=True, exist_ok=True)
        asset.write_bytes(relative.as_posix().encode("utf-8"))
    connection_id = str(uuid4())
    provisioner = InstanceProvisioner(
        tmp_path / "instances", tmp_path / "secrets"
    )
    root = provisioner.provision(connection_id, terminal)
    provisioner.seal_runtime_assets(connection_id)
    return provisioner, connection_id, root, root / "terminal" / assets[0]


def test_template_is_published_atomically_with_verified_manifest(
    tmp_path: Path,
) -> None:
    terminal = _template(tmp_path)
    expected = hashlib.sha256(b"terminal").hexdigest()
    provisioner = InstanceProvisioner(
        tmp_path / "instances", tmp_path / "secrets"
    )

    root = provisioner.provision(
        str(uuid4()), terminal, expected_terminal_sha256=expected
    )
    state = read_json(root / "state" / "instance.json")

    assert state["terminal_sha256"] == expected
    assert len(state["template_manifest_sha256"]) == 64
    assert len(state["template_code_manifest_sha256"]) == 64
    assert (root / "terminal" / "config.dat").read_bytes() == b"configuration"


def test_existing_instance_is_revalidated_before_reuse(tmp_path: Path) -> None:
    terminal = _template(tmp_path)
    expected = hashlib.sha256(b"terminal").hexdigest()
    connection_id = str(uuid4())
    provisioner = InstanceProvisioner(
        tmp_path / "instances", tmp_path / "secrets"
    )
    root = provisioner.provision(
        connection_id, terminal, expected_terminal_sha256=expected
    )

    (root / "terminal" / "config.dat").write_bytes(b"tampered")

    # Runtime-owned data/configuration may legitimately change after first launch.
    assert provisioner.validate(
        connection_id, expected_terminal_sha256=expected
    ) == root

    (root / "terminal" / "plugin.dll").write_bytes(b"untrusted code")
    with pytest.raises(ValueError, match="code manifest mismatch"):
        provisioner.validate(connection_id, expected_terminal_sha256=expected)


def test_existing_instance_rejects_tampered_terminal(tmp_path: Path) -> None:
    terminal = _template(tmp_path)
    expected = hashlib.sha256(b"terminal").hexdigest()
    connection_id = str(uuid4())
    provisioner = InstanceProvisioner(
        tmp_path / "instances", tmp_path / "secrets"
    )
    root = provisioner.provision(
        connection_id, terminal, expected_terminal_sha256=expected
    )

    (root / "terminal" / "terminal64.exe").write_bytes(b"tampered")

    with pytest.raises(ValueError, match="terminal digest mismatch"):
        provisioner.provision(
            connection_id, terminal, expected_terminal_sha256=expected
        )


def test_runtime_assets_are_pinned_per_instance_not_global_template(
    tmp_path: Path,
) -> None:
    terminal = _template(tmp_path)
    connection_id = str(uuid4())
    provisioner = InstanceProvisioner(
        tmp_path / "instances", tmp_path / "secrets"
    )
    root = provisioner.provision(connection_id, terminal)
    assets = (
        "MQL5/Experts/TradeJournal/TradeJournalBridge.ex5",
        "MQL5/Scripts/TradeJournal/TradeJournalDiscovery.ex5",
        "MQL5/Scripts/TradeJournal/TradeJournalLoader.ex5",
    )
    for relative in assets:
        asset = root / "terminal" / relative
        asset.parent.mkdir(parents=True, exist_ok=True)
        asset.write_bytes(relative.encode("utf-8"))

    first_pin = provisioner.seal_runtime_assets(connection_id)
    assert provisioner.validate_runtime_assets(connection_id) == first_pin

    (root / "terminal" / assets[0]).write_bytes(b"tampered")
    with pytest.raises(ValueError, match="runtime assets mismatch"):
        provisioner.validate_runtime_assets(connection_id)


def test_template_symlink_is_rejected(tmp_path: Path) -> None:
    terminal = _template(tmp_path)
    outside = tmp_path / "outside.dat"
    outside.write_bytes(b"outside")
    try:
        (terminal.parent / "linked.dat").symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    provisioner = InstanceProvisioner(
        tmp_path / "instances", tmp_path / "secrets"
    )

    with pytest.raises(ValueError, match="reparse"):
        provisioner.provision(str(uuid4()), terminal)


def test_copy_failure_rolls_back_staging_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    terminal = _template(tmp_path)
    connection_id = str(uuid4())
    instances = tmp_path / "instances"
    provisioner = InstanceProvisioner(instances, tmp_path / "secrets")

    def fail_copy(*args: object, **kwargs: object) -> None:
        raise OSError("fixture copy failure")

    monkeypatch.setattr(
        "windows_agent.provisioning.mt5_instance.shutil.copy2", fail_copy
    )
    with pytest.raises(OSError, match="fixture copy failure"):
        provisioner.provision(connection_id, terminal)

    assert not (instances / connection_id).exists()
    assert not list(instances.glob(".*.staging"))


def test_read_only_release_expert_rotates_without_copying_file_metadata(
    tmp_path: Path,
) -> None:
    provisioner, connection_id, root, target = _sealed_runtime_instance(tmp_path)
    replacement = tmp_path / "TradeJournalBridge-release.ex5"
    replacement.write_bytes(b"bridge-v2-content-addressed")
    expected = hashlib.sha256(replacement.read_bytes()).hexdigest()
    replacement.chmod(stat.S_IREAD)

    try:
        sealed = provisioner.rotate_managed_expert(
            connection_id, replacement, expected
        )

        assert target.read_bytes() == b"bridge-v2-content-addressed"
        assert target.stat().st_mode & stat.S_IWUSR
        assert not replacement.stat().st_mode & stat.S_IWUSR
        assert provisioner.validate_runtime_assets(connection_id) == sealed
        assert provisioner.validate(connection_id) == root
        assert not list(root.rglob("*.upgrade"))
        assert not list(root.rglob("*.rollback"))
    finally:
        replacement.chmod(stat.S_IREAD | stat.S_IWRITE)


def test_failed_rotation_cleans_read_only_upgrade_and_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provisioner, connection_id, root, target = _sealed_runtime_instance(tmp_path)
    previous = target.read_bytes()
    previous_seal = provisioner.validate_runtime_assets(connection_id)
    replacement = tmp_path / "TradeJournalBridge-release.ex5"
    replacement.write_bytes(b"bridge-v2")
    expected = hashlib.sha256(replacement.read_bytes()).hexdigest()
    original_sha256 = InstanceProvisioner._sha256
    observed = {"upgrade": False, "rollback": False}

    def reject_upgrade(path: Path) -> str:
        candidate = Path(path)
        digest = original_sha256(candidate)
        if candidate.name.endswith(".upgrade"):
            rollback = target.with_name(f"{target.name}.rollback")
            candidate.chmod(stat.S_IREAD)
            rollback.chmod(stat.S_IREAD)
            observed["upgrade"] = True
            observed["rollback"] = True
            return "0" * 64
        return digest

    monkeypatch.setattr(
        InstanceProvisioner,
        "_sha256",
        staticmethod(reject_upgrade),
    )

    with pytest.raises(ValueError, match="copy integrity"):
        provisioner.rotate_managed_expert(connection_id, replacement, expected)

    assert observed == {"upgrade": True, "rollback": True}
    assert target.read_bytes() == previous
    assert target.stat().st_mode & stat.S_IWUSR
    assert provisioner.validate_runtime_assets(connection_id) == previous_seal
    assert not list(root.rglob("*.upgrade"))
    assert not list(root.rglob("*.rollback"))
