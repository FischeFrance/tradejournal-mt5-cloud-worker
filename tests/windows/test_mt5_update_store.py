from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from windows_agent.provisioning.mt5_update_store import (
    Mt5PendingUpdateStore,
    Mt5PendingUpdateStoreError,
    Mt5UpdateRelease,
    load_verified_update_bundle,
    mark_update_bundle_healthy,
    seal_applied_update_bundle,
    stage_verified_update_bundle,
    write_updater_only_config,
)


CONNECTION_ID = "00000000-0000-4000-8000-000000000001"
SIGNER = "CN=MetaQuotes Ltd., O=MetaQuotes Ltd., S=Lemesos, C=CY"


def _release(tmp_path: Path, name: str, terminal: bytes) -> Mt5UpdateRelease:
    root = tmp_path / name
    (root / "MQL5" / "Experts").mkdir(parents=True)
    (root / "terminal64.exe").write_bytes(terminal)
    (root / "MQL5" / "Experts" / "bridge.ex5").write_bytes(b"bridge")
    return Mt5UpdateRelease.from_terminal_root(root)


def _bundle(
    tmp_path: Path,
    source: Mt5UpdateRelease,
    target: Mt5UpdateRelease,
    *,
    healthy: bool,
) -> Path:
    root = tmp_path / "live-update-fixture"
    root.mkdir()
    updater = root / "terminal64.exe"
    updater.write_bytes(b"signed-updater")
    (root / "mt5onnx64.6090").write_bytes(b"opaque-payload")
    (root / "temp").mkdir()
    config = write_updater_only_config(root / "tradejournal-update.ini")
    stage_verified_update_bundle(
        root,
        source_connection_id=CONNECTION_ID,
        source_release=source,
        managed_assets_manifest_sha256="a" * 64,
        signer_subject=SIGNER,
        updater=updater,
        updater_config=config,
    )
    seal_applied_update_bundle(root, target)
    if healthy:
        mark_update_bundle_healthy(root)
    return root


def test_store_publishes_sanitized_health_verified_receipt_atomically(
    tmp_path: Path,
) -> None:
    source = _release(tmp_path, "source", b"terminal-v1")
    target = _release(tmp_path, "target", b"terminal-v2")
    bundle = _bundle(tmp_path, source, target, healthy=True)
    store = Mt5PendingUpdateStore(tmp_path / "pending")
    metadata = load_verified_update_bundle(bundle, require_healthy=True)

    receipt = store.capture(
        bundle,
        metadata.updater,
        metadata.updater_config,
        metadata.signer_subject,
    )

    assert receipt.source_release == source
    assert receipt.target_release == target
    assert receipt.signer_subject == SIGNER
    assert receipt.updater.read_bytes() == b"signed-updater"
    assert b"Password" not in receipt.updater_config.read_bytes()
    assert store.pending() == (receipt,)
    assert not any("secret" in path.read_text(errors="ignore") for path in receipt.root.rglob("*") if path.is_file())

    duplicate = store.capture(bundle)
    assert duplicate == receipt
    assert len(store.pending()) == 1

    store.complete(receipt.receipt_id)
    assert store.pending() == ()


def test_store_rejects_applied_bundle_before_health_gate(tmp_path: Path) -> None:
    source = _release(tmp_path, "source", b"terminal-v1")
    target = _release(tmp_path, "target", b"terminal-v2")
    bundle = _bundle(tmp_path, source, target, healthy=False)
    store = Mt5PendingUpdateStore(tmp_path / "pending")

    with pytest.raises(Mt5PendingUpdateStoreError, match="phase"):
        store.capture(bundle)

    assert store.pending() == ()
    assert bundle.is_dir()


def test_store_rejects_tampered_payload_and_callback_metadata(
    tmp_path: Path,
) -> None:
    source = _release(tmp_path, "source", b"terminal-v1")
    target = _release(tmp_path, "target", b"terminal-v2")
    bundle = _bundle(tmp_path, source, target, healthy=True)
    store = Mt5PendingUpdateStore(tmp_path / "pending")
    metadata = load_verified_update_bundle(bundle, require_healthy=True)

    with pytest.raises(Mt5PendingUpdateStoreError, match="callback metadata"):
        store.capture(bundle, metadata.updater, metadata.updater_config, "CN=Other")

    (bundle / "mt5onnx64.6090").write_bytes(b"tampered")
    with pytest.raises(Mt5PendingUpdateStoreError, match="integrity"):
        store.capture(bundle)


def test_complete_hides_receipt_atomically_before_deferred_cleanup(
    tmp_path: Path,
) -> None:
    source = _release(tmp_path, "source", b"terminal-v1")
    target = _release(tmp_path, "target", b"terminal-v2")
    bundle = _bundle(tmp_path, source, target, healthy=True)
    store = Mt5PendingUpdateStore(tmp_path / "pending")
    receipt = store.capture(bundle)

    with patch.object(store, "_remove_tree", side_effect=OSError("locked")):
        store.complete(receipt.receipt_id)

    assert not receipt.root.exists()
    assert any(
        child.name.endswith(".deleting")
        for child in store.root.iterdir()
    )
    # The next locked store operation reaps the tombstone and does not expose
    # a partially deleted READY receipt.
    assert store.pending() == ()
    assert tuple(store.root.iterdir()) == ()


def test_quarantine_durably_hides_stale_receipt_without_deleting_it(
    tmp_path: Path,
) -> None:
    source = _release(tmp_path, "source", b"terminal-v1")
    target = _release(tmp_path, "target", b"terminal-v2")
    bundle = _bundle(tmp_path, source, target, healthy=True)
    store = Mt5PendingUpdateStore(tmp_path / "pending")
    receipt = store.capture(bundle)

    quarantined = store.quarantine(receipt.receipt_id)

    assert quarantined is not None
    assert quarantined.is_dir()
    assert not receipt.root.exists()
    assert store.pending() == ()
    assert Mt5PendingUpdateStore(store.root).pending() == ()
