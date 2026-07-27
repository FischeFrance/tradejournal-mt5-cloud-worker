"""Offline, fail-closed assembler for already materialized evidence artifacts."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Mapping

from lab_model import LabValidationError, validate_evidence

_SHA256 = re.compile(r"[0-9a-f]{64}")
_ROLES = {"evidence_c0", "evidence_c1", "evidence_c2", "evidence_c3", "evidence_c4", "evidence_c5",
          "etw_evidence_export", "wfp_evidence_export", "network_summary",
          "job_identity_preimage_c0", "job_identity_preimage_c1", "job_identity_preimage_c2",
          "root_process_generation_preimage_c0", "root_process_generation_preimage_c1", "root_process_generation_preimage_c2"}


class AssemblerError(Exception):
    pass


def _run_id(value: object) -> str:
    if not isinstance(value, str) or str(uuid.UUID(value)) != value or value.lower() != value:
        raise AssemblerError("run_id must be canonical lowercase UUID")
    return value


def _safe_path(root: Path, relative: object) -> Path:
    if not isinstance(relative, str) or not relative or "\x00" in relative:
        raise AssemblerError("unsafe manifest path")
    if relative.startswith(("/", "\\", "//", "\\\\", "\\\\?\\", "\\\\.\\")) or (len(relative) > 1 and relative[1] == ":"):
        raise AssemblerError("absolute, UNC or device path forbidden")
    normalized = relative.replace("\\", "/")
    if any(part in ("", ".", "..") for part in normalized.split("/")):
        raise AssemblerError("path traversal forbidden")
    base = root.resolve(strict=True)
    probe = base
    for part in normalized.split("/"):
        probe = probe / part
        if probe.is_symlink():
            raise AssemblerError("symlink/reparse point forbidden")
    resolved = probe.resolve(strict=False)
    try:
        resolved.relative_to(base)
    except ValueError as exc:
        raise AssemblerError("path escapes source directory") from exc
    return resolved


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(
        os.fspath(path),
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _durable_replace(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        move_file_ex = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
        move_file_ex.argtypes = (
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.DWORD,
        )
        move_file_ex.restype = wintypes.BOOL
        if not move_file_ex(
            str(source),
            str(destination),
            0x00000001 | 0x00000008,  # REPLACE_EXISTING | WRITE_THROUGH
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return
    os.replace(source, destination)
    _fsync_directory(destination.parent)


def _copy_durable(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as input_handle, destination.open("xb") as output_handle:
        shutil.copyfileobj(input_handle, output_handle, 1024 * 1024)
        output_handle.flush()
        os.fsync(output_handle.fileno())


def _remove_empty_parents(path: Path, stop: Path) -> None:
    current = path
    while current != stop:
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def _sync_directory_chain(path: Path, stop: Path) -> None:
    current = path
    while True:
        _fsync_directory(current)
        if current == stop:
            return
        if stop not in current.parents:
            raise AssemblerError("publication directory escapes output")
        current = current.parent


def assemble_run(source_dir: str | Path, manifest: Mapping[str, object], output_dir: str | Path) -> dict[str, object]:
    """Assemble only files named by a caller-supplied, controlled manifest.

    The function emits artifacts and a report only. It never evaluates evidence.
    """
    run_id = _run_id(manifest.get("run_id"))
    source = Path(source_dir)
    output = Path(output_dir)
    publication_manifest = output / f"{run_id}.artifact-manifest.json"
    if publication_manifest.exists():
        raise AssemblerError("run_id is already published")
    files = manifest.get("files")
    if not isinstance(files, Mapping):
        raise AssemblerError("manifest.files must be an object")
    errors: list[str] = []
    evidence_outputs: list[str] = []
    prepared: list[tuple[str, str, Path, dict[str, object] | None]] = []
    relative_paths: set[str] = set()
    for role, relative in files.items():
        if role not in _ROLES:
            raise AssemblerError(f"unknown manifest role: {role}")
        path = _safe_path(source, relative)
        normalized = str(relative).replace("\\", "/")
        if normalized in relative_paths:
            raise AssemblerError("multiple roles cannot publish the same path")
        relative_paths.add(normalized)
        if not path.is_file() or path.stat().st_size == 0:
            errors.append(f"artifact_missing_or_empty:{role}")
            continue
        validated_payload: dict[str, object] | None = None
        if role.startswith("evidence_"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                validated = validate_evidence(payload)
                provenance = validated["proof_binding"]["provenance"]
                if provenance["origin"] == "CAPTURED_EXPORT" and provenance["synthetic_fixture"]:
                    raise AssemblerError("synthetic fixture cannot be marked CAPTURED_EXPORT")
                validated_payload = validated
                evidence_outputs.append(normalized)
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, LabValidationError, KeyError) as exc:
                errors.append(f"evidence_invalid:{role}:{exc}")
                continue
        prepared.append((role, normalized, path, validated_payload))
    report = {"schema_version": 1, "run_id": run_id, "assembler": "offline", "verdict": None,
              "errors": errors, "degraded": bool(errors), "evidence_outputs": evidence_outputs}
    if errors:
        # A failure report is the only publishable failure artifact. Evidence and its manifest are
        # withheld together so a verifier can never consume a partially assembled run.
        _atomic_json(output / f"{run_id}.assembler-report.json", report)
        raise AssemblerError("assembly failed: " + "; ".join(errors))

    reserved = {
        f"{run_id}.artifact-manifest.json",
        f"{run_id}.assembler-report.json",
    }
    if relative_paths & reserved:
        raise AssemblerError("manifest path collides with assembler output")

    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(
        tempfile.mkdtemp(
            prefix=f".{run_id}.",
            suffix=".assembler-staging",
            dir=output.parent,
        )
    )
    published: list[Path] = []
    try:
        entries: list[dict[str, object]] = []
        for role, relative, path, payload in prepared:
            staged_path = stage / relative
            if payload is None:
                _copy_durable(path, staged_path)
            else:
                _atomic_json(staged_path, payload)
            entries.append(
                {
                    "role": role,
                    "relative_path": relative,
                    "sha256": _sha256(staged_path),
                    "size_bytes": staged_path.stat().st_size,
                }
            )

        artifact_manifest = {
            "schema_version": 1,
            "run_id": run_id,
            "files": entries,
        }
        report_name = f"{run_id}.assembler-report.json"
        manifest_name = f"{run_id}.artifact-manifest.json"
        _atomic_json(stage / report_name, report)
        _atomic_json(stage / manifest_name, artifact_manifest)

        publication_order = [
            *(relative for _, relative, _, _ in prepared),
            report_name,
        ]
        targets = [output / relative for relative in (*publication_order, manifest_name)]
        if any(target.exists() for target in targets):
            raise AssemblerError("refusing to overwrite existing run artifacts")

        output.mkdir(parents=True, exist_ok=True)
        _fsync_directory(output.parent)
        _fsync_directory(output)
        for relative in publication_order:
            destination = output / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            _sync_directory_chain(destination.parent, output)
            _durable_replace(stage / relative, destination)
            published.append(destination)
        # The manifest is the commit marker and is made visible only after every referenced
        # artifact and the report have reached durable storage.
        _durable_replace(stage / manifest_name, publication_manifest)
        published.append(publication_manifest)
        return report
    except Exception:
        for path in reversed(published):
            try:
                path.unlink()
                _remove_empty_parents(path.parent, output)
            except OSError:
                pass
        raise
    finally:
        shutil.rmtree(stage, ignore_errors=True)
