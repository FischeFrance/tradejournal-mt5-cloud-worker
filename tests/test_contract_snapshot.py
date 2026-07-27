from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "verify_contract_sync.py"
SPEC = importlib.util.spec_from_file_location("verify_contract_sync", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_pinned_contract_matches_checksums() -> None:
    MODULE.verify_contract(ROOT / "contracts" / "mt5-agent-v1")


def test_peer_contract_mismatch_is_rejected(tmp_path: Path) -> None:
    source = ROOT / "contracts" / "mt5-agent-v1"
    peer = tmp_path / "peer"
    peer.mkdir()
    for name in ("schema.json", "fixtures.json"):
        (peer / name).write_bytes((source / name).read_bytes())
    (peer / "schema.json").write_text("{}\n", encoding="utf-8")

    with pytest.raises(MODULE.ContractSyncError, match="differs from peer"):
        MODULE.verify_contract(source, peer)
