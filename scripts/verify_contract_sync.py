"""Verify the pinned MT5 Agent V1 contract and optionally compare a peer copy."""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path


class ContractSyncError(ValueError):
    pass


def _read_expected(contract_dir: Path) -> dict[str, str]:
    checksums = contract_dir / "SHA256SUMS"
    expected: dict[str, str] = {}
    for line in checksums.read_text(encoding="ascii").splitlines():
        digest, separator, name = line.partition("  ")
        if (
            not separator
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
            or name not in {"schema.json", "fixtures.json"}
            or name in expected
        ):
            raise ContractSyncError("invalid SHA256SUMS entry")
        expected[name] = digest
    if set(expected) != {"schema.json", "fixtures.json"}:
        raise ContractSyncError("SHA256SUMS must pin schema.json and fixtures.json")
    return expected


def verify_contract(contract_dir: Path, peer_dir: Path | None = None) -> None:
    root = contract_dir.resolve(strict=True)
    expected = _read_expected(root)
    for name, expected_digest in expected.items():
        payload = (root / name).read_bytes()
        actual = hashlib.sha256(payload).hexdigest()
        if actual != expected_digest:
            raise ContractSyncError(f"{name} does not match SHA256SUMS")
        if peer_dir is not None and payload != (peer_dir.resolve(strict=True) / name).read_bytes():
            raise ContractSyncError(f"{name} differs from peer contract")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--contract-dir",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "contracts" / "mt5-agent-v1",
    )
    parser.add_argument("--peer-dir", type=Path)
    args = parser.parse_args()
    try:
        verify_contract(args.contract_dir, args.peer_dir)
    except (ContractSyncError, OSError) as exc:
        parser.exit(1, f"CONTRACT_MISMATCH: {exc}\n")
    print("CONTRACT_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
