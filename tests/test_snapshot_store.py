"""Durabilita' e fail-closed del checkpoint snapshot, solo su filesystem temporaneo."""

from __future__ import annotations

import json
import os
import stat
from unittest.mock import patch

import pytest

import main as worker_main
from snapshot_store import SnapshotStore, SnapshotStoreError


def _snapshot(ticket: str = "1"):
    return {
        "positions": {ticket: {"ticket": ticket, "symbol": "EURUSD"}},
        "orders": {},
        "deals": {},
    }


def test_update_uses_atomic_replace_fsyncs_file_and_directory_and_sets_0600(tmp_path):
    path = tmp_path / "snapshot.json"
    store = SnapshotStore(str(path))
    real_replace = os.replace
    real_fsync = os.fsync

    with patch("snapshot_store.os.replace", wraps=real_replace) as replace, patch(
        "snapshot_store.os.fsync", wraps=real_fsync
    ) as fsync:
        store.update(_snapshot())

    replace.assert_called_once()
    source, destination = replace.call_args.args
    assert os.path.dirname(source) == str(tmp_path)
    assert destination == str(path)
    assert fsync.call_count >= 2  # file temporaneo + directory dopo il rename
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not list(tmp_path.glob("*.tmp"))
    assert SnapshotStore(str(path)).get() == _snapshot()


def test_memory_advances_only_after_persistence_succeeds(tmp_path):
    path = tmp_path / "snapshot.json"
    store = SnapshotStore(str(path))
    initial = _snapshot("1")
    store.update(initial)

    with patch.object(
        store,
        "_save_to_disk",
        side_effect=SnapshotStoreError("simulated persistence failure"),
    ):
        with pytest.raises(SnapshotStoreError, match="simulated"):
            store.update(_snapshot("2"))

    assert store.get() == initial
    assert SnapshotStore(str(path)).get() == initial


def test_replace_failure_keeps_previous_checkpoint_and_removes_temp_file(tmp_path):
    path = tmp_path / "snapshot.json"
    store = SnapshotStore(str(path))
    initial = _snapshot("1")
    store.update(initial)

    with patch("snapshot_store.os.replace", side_effect=OSError("simulated crash")):
        with pytest.raises(SnapshotStoreError, match="durevole"):
            store.update(_snapshot("2"))

    assert store.get() == initial
    assert SnapshotStore(str(path)).get() == initial
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize(
    "content",
    [
        '{"positions":',
        json.dumps({"positions": {}, "orders": {}}),
        json.dumps({"positions": [], "orders": {}, "deals": {}}),
    ],
)
def test_corrupt_or_incomplete_snapshot_fails_closed(tmp_path, content):
    path = tmp_path / "snapshot.json"
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)

    with pytest.raises(SnapshotStoreError):
        SnapshotStore(str(path))


def test_snapshot_symlink_is_rejected(tmp_path):
    target = tmp_path / "target.json"
    target.write_text(json.dumps(_snapshot()), encoding="utf-8")
    target.chmod(0o600)
    link = tmp_path / "snapshot.json"
    link.symlink_to(target)

    with pytest.raises(SnapshotStoreError, match="symlink"):
        SnapshotStore(str(link))


def test_existing_snapshot_permissions_are_migrated_to_0600(tmp_path):
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(_snapshot()), encoding="utf-8")
    path.chmod(0o644)

    assert SnapshotStore(str(path)).get() == _snapshot()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_main_handles_corrupt_persistent_snapshot_without_network_call(monkeypatch, tmp_path):
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_text('{"positions":', encoding="utf-8")
    snapshot_path.chmod(0o600)
    monkeypatch.setattr(worker_main, "SNAPSHOT_FILE_REAL_MODE", str(snapshot_path))
    monkeypatch.setattr(
        worker_main, "EVENT_OUTBOX_FILE_REAL_MODE", str(tmp_path / "event_outbox.json")
    )

    result = worker_main.run(
        {
            "MOCK_MODE": "false",
            "MT5_CLIENT_SOURCE": "bridge",
            "MT5_BRIDGE_URL": "http://bridge.invalid:8090",
            "MT5_BRIDGE_TOKEN": "test-bridge-token",
            "DRY_RUN": "true",
        }
    )

    assert result == 1
