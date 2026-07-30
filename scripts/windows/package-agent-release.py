"""Create a verifiable Windows Agent release from a clean git revision."""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from windows_agent.release_manifest import (  # noqa: E402
    ReleaseManifestError,
    build_release,
)


def _git(*arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise ReleaseManifestError("git metadata is unavailable")
    return completed.stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-root", required=True)
    arguments = parser.parse_args()
    if _git("status", "--porcelain=v1", "--untracked-files=all"):
        raise ReleaseManifestError("refusing to package a dirty working tree")
    revision = _git("rev-parse", "--verify", "HEAD")
    print(build_release(ROOT, arguments.output_root, revision=revision))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ReleaseManifestError as exc:
        print(f"release package failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
