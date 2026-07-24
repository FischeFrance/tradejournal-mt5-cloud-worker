"""Offline, fail-closed assembler for already materialized evidence artifacts."""
from __future__ import annotations

import hashlib
import json
import os
import re
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


def assemble_run(source_dir: str | Path, manifest: Mapping[str, object], output_dir: str | Path) -> dict[str, object]:
    """Assemble only files named by a caller-supplied, controlled manifest.

    The function emits artifacts and a report only. It never evaluates evidence.
    """
    run_id = _run_id(manifest.get("run_id"))
    source = Path(source_dir)
    output = Path(output_dir)
    files = manifest.get("files")
    if not isinstance(files, Mapping):
        raise AssemblerError("manifest.files must be an object")
    entries: list[dict[str, object]] = []
    errors: list[str] = []
    evidence_outputs: list[str] = []
    for role, relative in files.items():
        if role not in _ROLES:
            raise AssemblerError(f"unknown manifest role: {role}")
        path = _safe_path(source, relative)
        if not path.is_file() or path.stat().st_size == 0:
            errors.append(f"artifact_missing_or_empty:{role}")
            continue
        digest = _sha256(path)
        entry = {"role": role, "relative_path": str(relative).replace("\\", "/"), "sha256": digest, "size_bytes": path.stat().st_size}
        entries.append(entry)
        if role.startswith("evidence_"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                validated = validate_evidence(payload)
                provenance = validated["proof_binding"]["provenance"]
                if provenance["origin"] == "CAPTURED_EXPORT" and provenance["synthetic_fixture"]:
                    raise AssemblerError("synthetic fixture cannot be marked CAPTURED_EXPORT")
                target = output / path.name
                _atomic_json(target, validated)
                evidence_outputs.append(target.name)
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, LabValidationError, KeyError) as exc:
                errors.append(f"evidence_invalid:{role}:{exc}")
    report = {"schema_version": 1, "run_id": run_id, "assembler": "offline", "verdict": None,
              "errors": errors, "degraded": bool(errors), "evidence_outputs": evidence_outputs}
    artifact_manifest = {"schema_version": 1, "run_id": run_id, "files": entries}
    _atomic_json(output / f"{run_id}.artifact-manifest.json", artifact_manifest)
    _atomic_json(output / f"{run_id}.assembler-report.json", report)
    if errors:
        raise AssemblerError("assembly failed: " + "; ".join(errors))
    return report
