from __future__ import annotations

from pathlib import Path
from threading import Event, RLock
from uuid import uuid4

import pytest

from windows_agent.provisioning.mt5_instance import InstanceProvisioner
from windows_agent.provisioning.mt5_instance_pool import (
    InstancePoolError,
    Mt5InstancePool,
)
from windows_agent.state_store import read_json


def _pool(tmp_path: Path, terminal: Path, digest: str) -> Mt5InstancePool:
    return Mt5InstancePool(
        pool_root=tmp_path / "pool",
        instances_root=tmp_path / "instances",
        secrets_root=tmp_path / "secrets",
        source_terminal=terminal,
        expected_terminal_sha256=digest,
        target_size=2,
        max_size=3,
        template_lock=RLock(),
    )


def test_template_rotation_discards_and_synchronously_rebuilds_every_ready_slot(
    tmp_path: Path,
) -> None:
    terminal = tmp_path / "template" / "terminal64.exe"
    terminal.parent.mkdir()
    terminal.write_bytes(b"terminal-v1")
    certificate = terminal.parent / "Config" / "certificates" / "client.cer"
    certificate.parent.mkdir(parents=True)
    certificate.write_bytes(b"template-private-certificate")
    old_digest = InstanceProvisioner._sha256(terminal)
    pool = _pool(tmp_path, terminal, old_digest)
    assert pool.maintain_once() == 2
    old_slots = {slot.name for slot in pool.ready_slots()}
    assert all(
        not (slot / "terminal" / "Config" / "certificates").exists()
        for slot in pool.ready_slots()
    )

    terminal.write_bytes(b"terminal-v2")
    new_digest = InstanceProvisioner._sha256(terminal)
    discarded = pool.accept_template_rotation(new_digest, replenish=True)

    assert discarded == 2
    assert pool.ready_count() == pool.target_size
    assert not old_slots.intersection(slot.name for slot in pool.ready_slots())
    current_code = InstanceProvisioner._code_manifest(terminal.parent)
    for slot in pool.ready_slots():
        state = read_json(slot / "state" / "instance.json")
        assert state["terminal_sha256"] == new_digest
        assert state["template_code_manifest_sha256"] == current_code
        assert (slot / "terminal" / "terminal64.exe").read_bytes() == b"terminal-v2"
        assert not (slot / "terminal" / "Config" / "certificates").exists()

    connection_id = str(uuid4())
    claimed = pool.claim(connection_id)
    assert claimed == tmp_path / "instances" / connection_id
    assert (claimed / "terminal" / "terminal64.exe").read_bytes() == b"terminal-v2"
    assert not (claimed / "terminal" / "Config" / "certificates").exists()


def test_invalid_rotation_digest_does_not_mutate_pool_expectation_or_slots(
    tmp_path: Path,
) -> None:
    terminal = tmp_path / "template" / "terminal64.exe"
    terminal.parent.mkdir()
    terminal.write_bytes(b"terminal-v1")
    digest = InstanceProvisioner._sha256(terminal)
    pool = _pool(tmp_path, terminal, digest)
    pool.maintain_once()
    slots_before = tuple(slot.name for slot in pool.ready_slots())

    with pytest.raises(InstancePoolError, match="digest mismatch"):
        pool.accept_template_rotation("f" * 64, replenish=True)

    assert pool.expected_terminal_sha256 == digest
    assert tuple(slot.name for slot in pool.ready_slots()) == slots_before


def test_cancelled_pool_rebuild_is_reported_as_incomplete(tmp_path: Path) -> None:
    terminal = tmp_path / "template" / "terminal64.exe"
    terminal.parent.mkdir()
    terminal.write_bytes(b"terminal-v1")
    digest = InstanceProvisioner._sha256(terminal)
    pool = _pool(tmp_path, terminal, digest)
    pool.maintain_once()
    terminal.write_bytes(b"terminal-v2")
    stop_event = Event()
    stop_event.set()

    with pytest.raises(InstancePoolError, match="interrupted"):
        pool.accept_template_rotation(
            InstanceProvisioner._sha256(terminal),
            replenish=True,
            stop_event=stop_event,
        )

    assert pool.ready_count() == 0
