from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path
from uuid import uuid4

import pytest

from windows_agent.provisioning.mt5_instance import InstanceProvisioner
from windows_agent.provisioning.mt5_instance_pool import (
    InstancePoolError,
    Mt5InstancePool,
)
from windows_agent.state_store import read_json


def _template(tmp_path: Path) -> tuple[Path, str]:
    terminal = tmp_path / "template" / "terminal64.exe"
    terminal.parent.mkdir(parents=True)
    terminal.write_bytes(b"pinned-terminal")
    (terminal.parent / "plugin.dll").write_bytes(b"pinned-plugin")
    config = terminal.parent / "Config"
    config.mkdir()
    (config / "servers.dat").write_bytes(b"public-broker-catalog")
    (config / "accounts.dat").write_bytes(b"private-account")
    (config / "accounts.ini").write_bytes(b"private-account-ini")
    (config / "community.ini").write_bytes(b"private-community")
    (config / "signals.ini").write_bytes(b"private-signals")
    files = terminal.parent / "MQL5" / "Files" / "TradeJournal"
    files.mkdir(parents=True)
    (files / "account.json").write_text(
        '{"password":"must-not-survive"}',
        encoding="utf-8",
    )
    bases = terminal.parent / "Bases" / "Broker"
    bases.mkdir(parents=True)
    (bases / "cache.dat").write_bytes(b"private-cache")
    logs = terminal.parent / "Logs"
    logs.mkdir()
    (logs / "terminal.log").write_text("private log", encoding="utf-8")
    digest = hashlib.sha256(terminal.read_bytes()).hexdigest()
    return terminal, digest


def _pool(
    tmp_path: Path,
    *,
    target_size: int = 2,
    max_size: int = 3,
) -> Mt5InstancePool:
    terminal, digest = _template(tmp_path)
    return Mt5InstancePool(
        pool_root=tmp_path / "pool",
        instances_root=tmp_path / "instances",
        secrets_root=tmp_path / "secrets",
        source_terminal=terminal,
        expected_terminal_sha256=digest,
        target_size=target_size,
        max_size=max_size,
    )


def _assert_anonymous(root: Path) -> None:
    assert not (root / "terminal" / "Bases").exists()
    assert not (root / "terminal" / "Logs").exists()
    assert not (root / "terminal" / "MQL5" / "Files").exists()
    for name in (
        "accounts.dat",
        "accounts.ini",
        "community.ini",
        "signals.ini",
    ):
        assert not (root / "terminal" / "Config" / name).exists()
    for name in ("worker", "secrets", "logs", "data"):
        assert not tuple((root / name).iterdir())


def test_replenisher_builds_two_independent_anonymous_ready_slots(
    tmp_path: Path,
) -> None:
    pool = _pool(tmp_path)

    assert pool.maintain_once() == 2
    slots = pool.ready_slots()
    assert len(slots) == 2
    for slot in slots:
        _assert_anonymous(slot)
        assert (
            slot / "terminal" / "Config" / "servers.dat"
        ).read_bytes() == b"public-broker-catalog"

    first = slots[0] / "terminal" / "plugin.dll"
    second = slots[1] / "terminal" / "plugin.dll"
    source = pool.source_terminal.parent / "plugin.dll"
    first.write_bytes(b"slot-specific-change")
    assert second.read_bytes() == b"pinned-plugin"
    assert source.read_bytes() == b"pinned-plugin"
    if os.name != "nt":
        assert first.stat().st_ino != second.stat().st_ino
        assert first.stat().st_ino != source.stat().st_ino

    # One maintenance pass never grows beyond the configured target/max.
    assert pool.maintain_once() == 2
    pool.build_one()
    assert pool.ready_count() == 3
    with pytest.raises(InstancePoolError, match="already full"):
        pool.build_one()


def test_claim_atomically_publishes_rebound_instance(tmp_path: Path) -> None:
    pool = _pool(tmp_path)
    pool.maintain_once()
    connection_id = str(uuid4())

    root = pool.claim(connection_id)

    assert root == tmp_path / "instances" / connection_id
    assert pool.ready_count() == 1
    assert not (root / "state" / "pool.json").exists()
    state = read_json(root / "state" / "instance.json")
    assert state["connection_id"] == connection_id
    assert state["status"] == "provisioned"
    assert state["pool_slot_id"]
    assert state["terminal"] == str(
        root / "terminal" / "terminal64.exe"
    )
    _assert_anonymous(root)
    assert (
        InstanceProvisioner(
            tmp_path / "instances",
            tmp_path / "secrets",
        ).validate(connection_id, pool.expected_terminal_sha256)
        == root
    )


def test_concurrent_claims_receive_distinct_slots(tmp_path: Path) -> None:
    pool = _pool(tmp_path)
    pool.maintain_once()
    connection_ids = (str(uuid4()), str(uuid4()))
    roots: dict[str, Path | None] = {}
    barrier = threading.Barrier(3)

    def claim(connection_id: str) -> None:
        barrier.wait()
        roots[connection_id] = pool.claim(connection_id)

    threads = tuple(
        threading.Thread(target=claim, args=(connection_id,))
        for connection_id in connection_ids
    )
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)
    assert all(roots[connection_id] is not None for connection_id in connection_ids)
    slot_ids = {
        read_json(
            roots[connection_id] / "state" / "instance.json"  # type: ignore[operator]
        )["pool_slot_id"]
        for connection_id in connection_ids
    }
    assert len(slot_ids) == 2
    assert pool.ready_count() == 0


def test_same_connection_cannot_consume_two_slots(tmp_path: Path) -> None:
    pool = _pool(tmp_path)
    pool.maintain_once()
    connection_id = str(uuid4())
    outcomes: list[Path | Exception | None] = []
    barrier = threading.Barrier(3)

    def claim() -> None:
        barrier.wait()
        try:
            outcomes.append(pool.claim(connection_id))
        except Exception as exc:  # expected loser is fail-closed
            outcomes.append(exc)

    threads = (threading.Thread(target=claim), threading.Thread(target=claim))
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=5)

    assert any(isinstance(value, Path) for value in outcomes)
    assert all(
        isinstance(value, (Path, InstancePoolError))
        for value in outcomes
    )
    assert len(
        {
            value
            for value in outcomes
            if isinstance(value, Path)
        }
    ) == 1
    assert pool.ready_count() == 1


def test_corrupt_ready_slot_is_destroyed_and_never_claimed(
    tmp_path: Path,
) -> None:
    pool = _pool(tmp_path, target_size=1)
    slot = pool.build_one()
    (slot / "terminal" / "terminal64.exe").write_bytes(b"tampered")

    assert pool.claim(str(uuid4())) is None
    assert not slot.exists()
    assert pool.ready_count() == 0


def test_consumed_or_failed_slot_is_never_returned_to_ready(
    tmp_path: Path,
) -> None:
    pool = _pool(tmp_path, target_size=1)
    pool.maintain_once()
    old_slot_id = pool.ready_slots()[0].name
    connection_id = str(uuid4())
    assert pool.claim(connection_id) is not None

    InstanceProvisioner(
        tmp_path / "instances",
        tmp_path / "secrets",
    ).discard_failed(connection_id)
    pool.maintain_once()

    assert pool.ready_count() == 1
    assert pool.ready_slots()[0].name != old_slot_id
    assert not (tmp_path / "instances" / connection_id).exists()


def test_empty_pool_returns_none_for_explicit_direct_copy_fallback(
    tmp_path: Path,
) -> None:
    pool = _pool(tmp_path, target_size=0)
    assert pool.claim(str(uuid4())) is None


def test_recovery_removes_only_incomplete_pool_work(tmp_path: Path) -> None:
    pool = _pool(tmp_path, target_size=1)
    pool.maintain_once()
    ready = pool.ready_slots()[0]
    connection_id = str(uuid4())
    final = InstanceProvisioner(
        tmp_path / "instances",
        tmp_path / "secrets",
    ).provision(
        connection_id,
        pool.source_terminal,
        pool.expected_terminal_sha256,
    )
    building = pool.building_root / str(uuid4())
    claimed = pool.claimed_root / str(uuid4())
    building.mkdir()
    claimed.mkdir()
    reservation = pool.reservations_root / str(uuid4())
    reservation.write_text("reserved\n", encoding="utf-8")

    pool.recover_incomplete()

    assert ready.exists()
    assert final.exists()
    assert not building.exists()
    assert not claimed.exists()
    assert not reservation.exists()


def test_build_failure_rolls_back_without_partial_ready_slot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = _pool(tmp_path, target_size=1)

    def fail_sanitize(root: Path) -> None:
        raise OSError("fixture sanitize failure")

    monkeypatch.setattr(pool, "_sanitize_slot", fail_sanitize)
    with pytest.raises(OSError, match="fixture sanitize failure"):
        pool.build_one()

    assert pool.ready_count() == 0
    assert not tuple(pool.building_root.iterdir())


def test_pool_rejects_unsafe_connection_identity(tmp_path: Path) -> None:
    pool = _pool(tmp_path, target_size=0)
    with pytest.raises(InstancePoolError, match="identity"):
        pool.claim("../unsafe")
    assert not tuple(pool.reservations_root.iterdir())


def test_pool_json_contains_no_credentials_or_private_payload(
    tmp_path: Path,
) -> None:
    pool = _pool(tmp_path, target_size=1)
    slot = pool.build_one()

    serialized = "\n".join(
        json.dumps(json.loads(path.read_text(encoding="utf-8")))
        for path in sorted(slot.rglob("*.json"))
    ).casefold()
    assert "must-not-survive" not in serialized
    assert "private-account" not in serialized
    assert '"password"' not in serialized
    assert '"token"' not in serialized
