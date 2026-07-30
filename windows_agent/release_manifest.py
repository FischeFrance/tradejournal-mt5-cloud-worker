"""Build and verify immutable Windows Agent release directories.

The service must run a named, content-addressed release rather than a mutable
working copy.  This module intentionally knows nothing about credentials, MT5
instances, or deployment; it only copies a small allowlisted source set and
binds every file to a manifest before the directory is published atomically.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Any, Iterable, Mapping

from worker.atomic_file import fsync_directory


RELEASE_SCHEMA_VERSION = 1
RELEASE_MANIFEST_NAME = "release-manifest.json"
RELEASE_CONTENTS = (
    "windows_agent",
    "worker",
    "contracts/mt5-agent-v1",
    # The running terminal template owns the compiled EX5 and pins it through
    # its own template manifest.  Keep the repository source here for review;
    # never package a stale or developer-local binary as Agent code.
    "mt5/experts/TradeJournalBridge.mq5",
    "scripts/windows",
    "requirements.txt",
    "requirements-windows.txt",
)
_REVISION = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ReleaseManifestError(RuntimeError):
    """The release source, manifest, or published tree is not trustworthy."""


def _is_reparse_point(path: Path) -> bool:
    try:
        value = path.lstat()
    except OSError:
        return True
    attributes = getattr(value, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return path.is_symlink() or bool(attributes & reparse_flag)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ReleaseManifestError("release file cannot be read") from exc
    return digest.hexdigest()


def _safe_relative(value: str) -> str:
    normalized = value.replace("\\", "/")
    parts = normalized.split("/")
    if (
        not normalized
        or normalized.startswith(("/", "//"))
        or (len(normalized) > 1 and normalized[1] == ":")
        or any(part in ("", ".", "..") for part in parts)
    ):
        raise ReleaseManifestError("release path is unsafe")
    return normalized


def _safe_source(root: Path, relative: str) -> Path:
    safe_relative = _safe_relative(relative)
    source = root.joinpath(*safe_relative.split("/"))
    try:
        source.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise ReleaseManifestError("release source escapes root") from exc
    if _is_reparse_point(source):
        raise ReleaseManifestError("release source reparse point is forbidden")
    return source


def _iter_files(root: Path, relative: str) -> Iterable[tuple[str, Path]]:
    source = _safe_source(root, relative)
    if source.is_file():
        yield _safe_relative(relative), source
        return
    if not source.is_dir():
        raise ReleaseManifestError("release source is unavailable")
    for path in sorted(source.rglob("*"), key=lambda item: item.as_posix()):
        if path.name == "__pycache__" or path.suffix == ".pyc":
            continue
        if _is_reparse_point(path):
            raise ReleaseManifestError("release source reparse point is forbidden")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ReleaseManifestError("release source contains a non-regular file")
        yield _safe_relative(path.relative_to(root).as_posix()), path


def _write_manifest(path: Path, payload: Mapping[str, Any]) -> None:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    with path.open("xb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())


def build_release(
    source_root: str | Path,
    output_root: str | Path,
    *,
    revision: str,
    contents: tuple[str, ...] = RELEASE_CONTENTS,
) -> Path:
    """Publish one complete immutable release directory.

    Callers are expected to check that ``source_root`` is a clean, committed
    checkout first.  We additionally make the output fail-closed: the target
    name is derived from the revision and is never overwritten.
    """

    if not isinstance(revision, str) or not _REVISION.fullmatch(revision):
        raise ReleaseManifestError("release revision is invalid")
    source = Path(source_root)
    destination_root = Path(output_root)
    if _is_reparse_point(source) or not source.is_dir():
        raise ReleaseManifestError("release source root is invalid")
    destination_root.mkdir(parents=True, exist_ok=True)
    if _is_reparse_point(destination_root) or not destination_root.is_dir():
        raise ReleaseManifestError("release output root is invalid")
    release_name = f"agent-{revision[:12]}"
    destination = destination_root / release_name
    if destination.exists():
        raise ReleaseManifestError("release revision is already published")

    stage = Path(tempfile.mkdtemp(prefix=".release-", dir=destination_root))
    try:
        files: list[dict[str, object]] = []
        copied: set[str] = set()
        for allowed in contents:
            for relative, source_file in _iter_files(source, allowed):
                if relative in copied:
                    raise ReleaseManifestError("release source files overlap")
                copied.add(relative)
                target = stage.joinpath(*relative.split("/"))
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source_file, target)
                with target.open("rb") as handle:
                    os.fsync(handle.fileno())
                files.append(
                    {
                        "path": relative,
                        "sha256": _sha256(target),
                        "size": target.stat().st_size,
                    }
                )
        if not files:
            raise ReleaseManifestError("release has no files")
        files.sort(key=lambda item: str(item["path"]))
        _write_manifest(
            stage / RELEASE_MANIFEST_NAME,
            {
                "schema_version": RELEASE_SCHEMA_VERSION,
                "source_revision": revision,
                "files": files,
            },
        )
        verify_release(stage)
        os.replace(stage, destination)
        fsync_directory(destination_root)
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return destination


def verify_release(release_root: str | Path) -> dict[str, Any]:
    """Verify the release manifest and reject added, changed, or unsafe files."""

    root = Path(release_root)
    manifest_path = root / RELEASE_MANIFEST_NAME
    if _is_reparse_point(root) or not root.is_dir() or _is_reparse_point(manifest_path):
        raise ReleaseManifestError("release root is invalid")
    try:
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseManifestError("release manifest cannot be read") from exc
    if (
        not isinstance(document, Mapping)
        or set(document) != {"schema_version", "source_revision", "files"}
        or document["schema_version"] != RELEASE_SCHEMA_VERSION
        or not isinstance(document["source_revision"], str)
        or not _REVISION.fullmatch(document["source_revision"])
        or not isinstance(document["files"], list)
        or not document["files"]
    ):
        raise ReleaseManifestError("release manifest is invalid")
    expected: dict[str, tuple[str, int]] = {}
    for item in document["files"]:
        if (
            not isinstance(item, Mapping)
            or set(item) != {"path", "sha256", "size"}
            or not isinstance(item["path"], str)
            or not isinstance(item["sha256"], str)
            or not _SHA256.fullmatch(item["sha256"])
            or not isinstance(item["size"], int)
            or isinstance(item["size"], bool)
            or item["size"] < 0
        ):
            raise ReleaseManifestError("release manifest file is invalid")
        relative = _safe_relative(item["path"])
        if relative in expected:
            raise ReleaseManifestError("release manifest contains duplicate files")
        expected[relative] = (item["sha256"], item["size"])
    actual: set[str] = set()
    for path in root.rglob("*"):
        if path == manifest_path:
            continue
        if _is_reparse_point(path):
            raise ReleaseManifestError("release contains a reparse point")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ReleaseManifestError("release contains a non-regular file")
        relative = _safe_relative(path.relative_to(root).as_posix())
        actual.add(relative)
        expected_digest, expected_size = expected.get(relative, ("", -1))
        if path.stat().st_size != expected_size or _sha256(path) != expected_digest:
            raise ReleaseManifestError("release file binding is invalid")
    if actual != set(expected):
        raise ReleaseManifestError("release file set is invalid")
    return dict(document)
