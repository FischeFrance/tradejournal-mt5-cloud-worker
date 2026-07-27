from __future__ import annotations

import hashlib
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
