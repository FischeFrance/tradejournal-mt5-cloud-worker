"""Independent captured-evidence verifier for the MT5 direct-endpoint lab.

Deliberately does not import lab_model.evaluate_evidence, canonical_json, or
contract_digest: those are exactly what an independent verifier must not simply
trust, because they are the same code a producer would have used to compute the
values being checked here. Only two things are reused from lab_model:

  * validate_evidence -- pure structural/shape validation (equivalent to a very
    expressive JSON Schema: fixed per-control policy constants, internal
    arithmetic consistency). It computes nothing that is later trusted as proof
    of anything external; it only rejects a body that could not possibly be
    well-formed evidence.
  * validate_candidate and its underlying policy data (forbidden address
    ranges, dangerous ports) -- fixed external policy, not something a
    producer could have "gotten right by luck"; validate_evidence already
    calls it internally, so a structurally valid body has already satisfied it.

Every digest that is actually load-bearing for a PASS verdict (proof_binding.*)
is recomputed here with this module's own canonical-JSON and contract-digest
implementation, from the raw artifact file the manifest resolves to -- never
from a path embedded in the evidence body itself, and never by trusting a
digest the evidence body merely asserts.

Never starts ETW/WFP capture, never opens a socket, never touches Windows
Firewall/WFP or MT5/MetaEditor. Operates only on already-materialized files on
disk.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, MutableMapping, Sequence

from lab_model import LabValidationError, validate_evidence

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Same domain-separation prefix as lab_model.contract_digest, by specification
# -- not imported, reimplemented, so a bug or change in lab_model's own digest
# function cannot silently make the producer and this verifier agree on a
# wrong value.
_DIGEST_DOMAIN_PREFIX = b"MT5_DIRECT_ENDPOINT\x00"
_ARTIFACT_TYPE_RE = re.compile(r"[A-Z][A-Z0-9_]{2,63}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")

CONTROLS_C012 = ("C0", "C1", "C2")
CONTROLS_DIRECT = ("C3", "C4", "C5")

# Ordered worst-to-best; used to fold many sub-check verdicts into one overall
# outcome (the worst sub-check always wins).
_SEVERITY_ORDER = {"FAIL": 0, "INCONCLUSIVE": 1, "SYNTHETIC_PASS": 2, "PASS": 3}

_TIMELINE_CLOCK_TOLERANCE_MS = 1_000

# Fixed, closed set of artifact roles a run directory may contain. A digest in
# an evidence body can only ever be checked against one of these -- never
# against a path read out of the evidence body itself.
_ROLE_TEMPLATES: dict[str, str] = {
    "evidence_c0": "{run_id}.evidence.c0.json",
    "evidence_c1": "{run_id}.evidence.c1.json",
    "evidence_c2": "{run_id}.evidence.c2.json",
    "evidence_c3": "{run_id}.evidence.c3.json",
    "evidence_c4": "{run_id}.evidence.c4.json",
    "evidence_c5": "{run_id}.evidence.c5.json",
    "etw_evidence_export": "{run_id}.etw-evidence.sanitized.json",
    "wfp_evidence_export": "{run_id}.wfp-security.sanitized.jsonl",
    "network_summary": "{run_id}.network-summary.json",
    "wpr_start_intent": "{run_id}.wpr-start.json",
    "wpr_stop_intent": "{run_id}.wpr-stop.json",
    "job_identity_preimage_c0": "{run_id}.job-identity.c0.json",
    "job_identity_preimage_c1": "{run_id}.job-identity.c1.json",
    "job_identity_preimage_c2": "{run_id}.job-identity.c2.json",
    "root_process_generation_preimage_c0": "{run_id}.root-process-generation.c0.json",
    "root_process_generation_preimage_c1": "{run_id}.root-process-generation.c1.json",
    "root_process_generation_preimage_c2": "{run_id}.root-process-generation.c2.json",
}

_EVIDENCE_ROLE_FOR_CONTROL = {control: f"evidence_{control.lower()}" for control in CONTROLS_C012 + CONTROLS_DIRECT}


class EvidenceVerifierError(Exception):
    """Raised for verifier-internal errors: bad arguments, an invalid run_id,
    or a run directory that cannot even be scanned. Never raised for a normal
    FAIL/INCONCLUSIVE verdict -- those are reported through VerifierEvaluation."""


@dataclass(frozen=True)
class VerifierEvaluation:
    outcome: str
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {"outcome": self.outcome, "reasons": list(self.reasons)}


def _worse(a: str, b: str) -> str:
    return a if _SEVERITY_ORDER[a] <= _SEVERITY_ORDER[b] else b


# ---------------------------------------------------------------------------
# run_id validation (must happen before any filename is ever built from it)
# ---------------------------------------------------------------------------


def validate_run_id(run_id: object) -> str:
    """Accepts only a canonical, lowercase, hyphenated UUID string. Rejects
    everything else -- including anything containing '/', '\\', '..', a UNC
    or device-path prefix -- before it is ever used to build a filename."""

    if not isinstance(run_id, str) or not run_id:
        raise EvidenceVerifierError("run_id must be a non-empty string")
    if "\x00" in run_id:
        raise EvidenceVerifierError("run_id must not contain a NUL byte")
    try:
        parsed = uuid.UUID(run_id)
    except (ValueError, AttributeError, TypeError) as exc:
        raise EvidenceVerifierError("run_id must be a canonical UUID") from exc
    canonical = str(parsed)
    if run_id != canonical:
        raise EvidenceVerifierError(
            "run_id must be in canonical lowercase hyphenated UUID form"
        )
    return canonical


# ---------------------------------------------------------------------------
# Independent canonical JSON / contract digest (never imported from lab_model)
# ---------------------------------------------------------------------------


def verifier_canonical_json(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def verifier_contract_digest(artifact_type: str, schema_version: int, payload: object) -> str:
    if not isinstance(artifact_type, str) or _ARTIFACT_TYPE_RE.fullmatch(artifact_type) is None:
        raise EvidenceVerifierError("artifact_type is invalid")
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version < 1
    ):
        raise EvidenceVerifierError("schema_version must be a positive integer")
    body = (
        _DIGEST_DOMAIN_PREFIX
        + artifact_type.encode("utf-8")
        + b"\x00"
        + str(schema_version).encode("utf-8")
        + b"\x00"
        + verifier_canonical_json(payload).encode("utf-8")
    )
    return hashlib.sha256(body).hexdigest()


def _sha256_file_bytes(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json_file(path: Path) -> object:
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise EvidenceVerifierError(f"{path.name} is not readable") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise EvidenceVerifierError(f"{path.name} is not valid JSON") from exc


# ---------------------------------------------------------------------------
# Run-directory artifact manifest (path safety)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArtifactManifestEntry:
    role: str
    relative_path: str
    resolved_path: Path


def _reject_unsafe_relative_path(relative_path: str) -> None:
    if not relative_path or relative_path != relative_path.strip():
        raise EvidenceVerifierError(f"unsafe artifact path: {relative_path!r}")
    if "\x00" in relative_path:
        raise EvidenceVerifierError("artifact path contains a NUL byte")
    if relative_path.startswith(("\\\\", "//")):
        raise EvidenceVerifierError(f"UNC-style artifact path is forbidden: {relative_path!r}")
    if relative_path.startswith(("\\\\?\\", "\\\\.\\")):
        raise EvidenceVerifierError(f"device-style artifact path is forbidden: {relative_path!r}")
    if os.path.isabs(relative_path):
        raise EvidenceVerifierError(f"absolute artifact path is forbidden: {relative_path!r}")
    if len(relative_path) >= 2 and relative_path[1] == ":":
        raise EvidenceVerifierError(f"drive-qualified artifact path is forbidden: {relative_path!r}")
    normalized = relative_path.replace("\\", "/")
    segments = normalized.split("/")
    if any(segment in ("", "..") for segment in segments):
        raise EvidenceVerifierError(f"artifact path traversal is forbidden: {relative_path!r}")


def _resolve_contained_path(run_dir: Path, relative_path: str) -> Path:
    """Resolves relative_path under run_dir, refusing anything that is not a
    plain, contained, non-symlinked descendant. Every ancestor component
    between run_dir and the file itself is checked individually so a reparse
    point/symlink anywhere in the middle of the path is caught, not only one
    at the very end."""

    _reject_unsafe_relative_path(relative_path)
    resolved_run_dir = run_dir.resolve(strict=True)
    probe = resolved_run_dir
    for segment in relative_path.split("/"):
        probe = probe / segment
        if probe.is_symlink():
            raise EvidenceVerifierError(
                f"artifact path crosses a symlink/reparse point: {relative_path!r}"
            )
    resolved = probe.resolve(strict=False)
    try:
        resolved.relative_to(resolved_run_dir)
    except ValueError as exc:
        raise EvidenceVerifierError(
            f"artifact path escapes the run directory: {relative_path!r}"
        ) from exc
    return resolved


def build_run_artifact_manifest(
    run_dir: Path, run_id: str
) -> dict[str, ArtifactManifestEntry]:
    """Scans run_dir for the fixed, expected filenames per role (see
    _ROLE_TEMPLATES). Only roles whose file actually exists are included --
    absence is not itself an error here; it becomes one later, only for roles
    a claimed digest actually requires."""

    canonical_run_id = validate_run_id(run_id)
    if not run_dir.is_dir():
        raise EvidenceVerifierError(f"run_dir does not exist or is not a directory: {run_dir}")
    manifest: dict[str, ArtifactManifestEntry] = {}
    for role, template in _ROLE_TEMPLATES.items():
        relative_path = template.format(run_id=canonical_run_id)
        resolved = _resolve_contained_path(run_dir, relative_path)
        if resolved.is_file():
            manifest[role] = ArtifactManifestEntry(
                role=role, relative_path=relative_path, resolved_path=resolved
            )
    return manifest


def _require_manifest_file(
    manifest: Mapping[str, ArtifactManifestEntry], role: str, reasons: list[str]
) -> Path | None:
    entry = manifest.get(role)
    if entry is None:
        reasons.append(f"captured_export_artifact_missing:{role}")
        return None
    try:
        size = entry.resolved_path.stat().st_size
    except OSError:
        reasons.append(f"captured_export_artifact_unreadable:{role}")
        return None
    if size == 0:
        reasons.append(f"captured_export_artifact_empty:{role}")
        return None
    return entry.resolved_path


# ---------------------------------------------------------------------------
# Digest verification against the manifest
# ---------------------------------------------------------------------------


def _verify_raw_file_digest(
    manifest: Mapping[str, ArtifactManifestEntry],
    role: str,
    claimed_digest: object,
    reasons: list[str],
) -> str:
    """Verifies a plain SHA-256 of raw file bytes (used for opaque exports:
    ETW/WFP sanitized exports, network summary). Returns "verified", "missing",
    or "mismatch"."""

    if claimed_digest is None:
        return "missing"
    if not isinstance(claimed_digest, str) or _SHA256_RE.fullmatch(claimed_digest) is None:
        reasons.append(f"claimed_digest_malformed:{role}")
        return "mismatch"
    path = _require_manifest_file(manifest, role, reasons)
    if path is None:
        return "missing"
    recomputed = _sha256_file_bytes(path)
    if recomputed != claimed_digest:
        reasons.append(f"digest_mismatch:{role}")
        return "mismatch"
    return "verified"


def _verify_structured_digest(
    manifest: Mapping[str, ArtifactManifestEntry],
    role: str,
    claimed_digest: object,
    artifact_type: str,
    schema_version: int,
    reasons: list[str],
) -> tuple[str, object | None]:
    """Verifies a contract_digest-style digest of a JSON preimage body.
    Returns (status, parsed_payload_or_None); status is "verified", "missing",
    or "mismatch"."""

    if claimed_digest is None:
        return "missing", None
    if not isinstance(claimed_digest, str) or _SHA256_RE.fullmatch(claimed_digest) is None:
        reasons.append(f"claimed_digest_malformed:{role}")
        return "mismatch", None
    path = _require_manifest_file(manifest, role, reasons)
    if path is None:
        return "missing", None
    try:
        payload = _read_json_file(path)
    except EvidenceVerifierError:
        reasons.append(f"captured_export_artifact_unparseable:{role}")
        return "missing", None
    recomputed = verifier_contract_digest(artifact_type, schema_version, payload)
    if recomputed != claimed_digest:
        reasons.append(f"digest_mismatch:{role}")
        return "mismatch", None
    return "verified", payload


# ---------------------------------------------------------------------------
# Provenance gate (unconditional; SYNTHETIC_PASS reachable only when the
# caller explicitly opts in, never from the public CLI command)
# ---------------------------------------------------------------------------


def _check_provenance(
    provenance: Mapping[str, object], *, allow_synthetic: bool
) -> tuple[str, list[str]]:
    origin = provenance.get("origin")
    synthetic = provenance.get("synthetic_fixture")
    if origin == "SYNTHETIC_FIXTURE" and synthetic is not True:
        return "FAIL", ["provenance_origin_synthetic_flag_mismatch"]
    if origin == "CAPTURED_EXPORT" and synthetic is not False:
        return "FAIL", ["provenance_origin_captured_flag_mismatch"]
    if origin == "SYNTHETIC_FIXTURE" or synthetic is True:
        if not allow_synthetic:
            return "FAIL", ["synthetic_evidence_rejected_outside_test_mode"]
        return "SYNTHETIC_PASS", ["synthetic_provenance"]
    if origin != "CAPTURED_EXPORT":
        return "FAIL", [f"unknown_provenance_origin:{origin!r}"]
    return "PASS", []


def _check_captured_artifacts_present(
    body: Mapping[str, object], manifest: Mapping[str, ArtifactManifestEntry]
) -> tuple[str, list[str]]:
    """provenance=CAPTURED_EXPORT is a claim that referenced export artifacts
    genuinely exist; if any digest the body claims cannot be resolved to a
    present, non-empty, readable file, that claim is false -- a definitive
    contradiction, not merely missing corroboration."""

    reasons: list[str] = []
    proof = body["proof_binding"]
    ok = True
    for digest_key, role in (
        ("etw_evidence_sha256", "etw_evidence_export"),
        ("wfp_evidence_sha256", "wfp_evidence_export"),
    ):
        if proof.get(digest_key) is not None and _require_manifest_file(manifest, role, reasons) is None:
            ok = False
    network_digest = body["network"].get("flow_record_set_sha256")
    if network_digest is not None and _require_manifest_file(manifest, "network_summary", reasons) is None:
        ok = False
    return ("PASS" if ok else "FAIL"), reasons


# ---------------------------------------------------------------------------
# proof_binding digest recomputation (ETW/WFP/network summary)
# ---------------------------------------------------------------------------


def _evaluate_proof_binding_digests(
    control: str,
    body: Mapping[str, object],
    manifest: Mapping[str, ArtifactManifestEntry],
) -> tuple[str, list[str]]:
    reasons: list[str] = []
    proof = body["proof_binding"]
    overall = "PASS"
    checks = [("etw_evidence_sha256", "etw_evidence_export")]
    if control in CONTROLS_DIRECT:
        checks.append(("wfp_evidence_sha256", "wfp_evidence_export"))
    for digest_key, role in checks:
        status = _verify_raw_file_digest(manifest, role, proof.get(digest_key), reasons)
        if status == "mismatch":
            overall = _worse(overall, "FAIL")
        elif status == "missing" and proof.get(digest_key) is not None:
            overall = _worse(overall, "FAIL")
        elif status == "missing":
            overall = _worse(overall, "INCONCLUSIVE")
    return overall, reasons


# ---------------------------------------------------------------------------
# Job identity / root process generation continuity (corrected design)
#
# JOB_IDENTITY preimage: session/Job-level invariants only -- never a
# per-control observation. Deliberately excludes `control`, root_pid, and
# root_kernel_creation_utc_ticks: those belong to ROOT_PROCESS_GENERATION.
#   { c012_session_id, job_id, kill_on_job_close_verified, breakaway_allowed,
#     silent_breakaway_allowed, windows_session_id }
# A genuinely continuous session produces byte-identical preimages (and thus
# identical digests) at C0, C1, and C2 by construction.
#
# ROOT_PROCESS_GENERATION preimage: the root process's own identity.
#   { root_pid, root_kernel_creation_utc_ticks }
# ---------------------------------------------------------------------------

_JOB_IDENTITY_ARTIFACT_TYPE = "JOB_IDENTITY"
_ROOT_PROCESS_GENERATION_ARTIFACT_TYPE = "ROOT_PROCESS_GENERATION"
_JOB_IDENTITY_SCHEMA_VERSION = 1
_ROOT_PROCESS_GENERATION_SCHEMA_VERSION = 1

_JOB_IDENTITY_PAYLOAD_KEYS = (
    "c012_session_id",
    "job_id",
    "kill_on_job_close_verified",
    "breakaway_allowed",
    "silent_breakaway_allowed",
    "windows_session_id",
)
_ROOT_PROCESS_GENERATION_PAYLOAD_KEYS = (
    "root_pid",
    "root_kernel_creation_utc_ticks",
)


def _preimage_payload(raw: object, keys: Sequence[str]) -> dict[str, object] | None:
    if not isinstance(raw, Mapping):
        return None
    if set(raw.keys()) != set(keys):
        return None
    return {key: raw[key] for key in keys}


def _evaluate_job_root_continuity(
    evidence_bodies: Mapping[str, Mapping[str, object]],
    manifest: Mapping[str, ArtifactManifestEntry],
) -> tuple[str, list[str]]:
    reasons: list[str] = []
    present_controls = [c for c in CONTROLS_C012 if c in evidence_bodies]
    if len(present_controls) < len(CONTROLS_C012):
        missing = sorted(set(CONTROLS_C012) - set(present_controls))
        reasons.append("continuity_control_missing:" + ",".join(missing))
        return "INCONCLUSIVE", reasons

    job_identity_digests: set[object] = set()
    root_generation_digests: set[object] = set()
    for control in CONTROLS_C012:
        binding = evidence_bodies[control]["lifecycle_binding"]
        job_identity_digests.add(binding["job_identity_sha256"])
        root_generation_digests.add(binding["root_process_generation_sha256"])

    # Digest equality across C0/C1/C2 demonstrates continuity of the *claim*
    # but, per the approved design, must never by itself promote the verdict
    # to PASS -- only a contradiction (inequality) is decisive on its own.
    if len(job_identity_digests) != 1 or len(root_generation_digests) != 1:
        reasons.append("job_root_digest_mismatch_across_controls")
        return "FAIL", reasons

    claimed_job_identity = next(iter(job_identity_digests))
    claimed_root_generation = next(iter(root_generation_digests))

    all_preimages_verified = True
    job_identity_payloads: list[dict[str, object]] = []
    root_generation_payloads: list[dict[str, object]] = []

    for control in CONTROLS_C012:
        suffix = control.lower()

        status, raw_payload = _verify_structured_digest(
            manifest,
            f"job_identity_preimage_{suffix}",
            claimed_job_identity,
            _JOB_IDENTITY_ARTIFACT_TYPE,
            _JOB_IDENTITY_SCHEMA_VERSION,
            reasons,
        )
        if status == "mismatch":
            return "FAIL", reasons
        if status != "verified":
            all_preimages_verified = False
        else:
            payload = _preimage_payload(raw_payload, _JOB_IDENTITY_PAYLOAD_KEYS)
            if payload is None:
                reasons.append(f"job_identity_preimage_shape_invalid:{control}")
                return "FAIL", reasons
            job_identity_payloads.append(payload)

        status, raw_payload = _verify_structured_digest(
            manifest,
            f"root_process_generation_preimage_{suffix}",
            claimed_root_generation,
            _ROOT_PROCESS_GENERATION_ARTIFACT_TYPE,
            _ROOT_PROCESS_GENERATION_SCHEMA_VERSION,
            reasons,
        )
        if status == "mismatch":
            return "FAIL", reasons
        if status != "verified":
            all_preimages_verified = False
        else:
            payload = _preimage_payload(raw_payload, _ROOT_PROCESS_GENERATION_PAYLOAD_KEYS)
            if payload is None:
                reasons.append(f"root_process_generation_preimage_shape_invalid:{control}")
                return "FAIL", reasons
            root_generation_payloads.append(payload)

    if not all_preimages_verified:
        reasons.append("job_root_continuity_unverified_digest_only")
        return "INCONCLUSIVE", reasons

    # Every preimage's own recomputed digest already matched the single
    # claimed digest shared by C0/C1/C2, which -- given SHA-256 -- already
    # proves the three preimage bodies are identical. The explicit pairwise
    # comparison below is redundant with that guarantee but kept as a direct,
    # human-auditable statement of exactly what "same Job/root generation"
    # means, rather than relying solely on the digest argument.
    if any(payload != job_identity_payloads[0] for payload in job_identity_payloads[1:]):
        reasons.append("job_identity_preimage_content_mismatch")
        return "FAIL", reasons
    if any(payload != root_generation_payloads[0] for payload in root_generation_payloads[1:]):
        reasons.append("root_process_generation_preimage_content_mismatch")
        return "FAIL", reasons

    return "PASS", reasons


# ---------------------------------------------------------------------------
# Network verification
# ---------------------------------------------------------------------------


def _evaluate_network(
    control: str,
    body: Mapping[str, object],
    manifest: Mapping[str, ArtifactManifestEntry],
) -> tuple[str, list[str]]:
    reasons: list[str] = []
    network = body["network"]
    run_context = body["run_context"]
    overall = "PASS"

    # candidate IP/port policy (globally routable, not a forbidden special-
    # purpose range, not a blocked port) is already enforced by
    # validate_evidence -> _require_run_context -> validate_candidate; a body
    # that reached this function has already satisfied it. Independently
    # re-deriving process-scoped/other flow accounting and DNS event counts
    # from the raw network summary export is this function's own addition.
    if network["candidate_tcp_flows"] > 0 and run_context["candidate_endpoint"] is None:
        reasons.append("candidate_tcp_flows_without_candidate_endpoint")
        overall = _worse(overall, "FAIL")

    if network["other_tcp_flows"] > 0 and not network["attribution_unambiguous"]:
        reasons.append("unattributed_foreign_tcp_flow")
        overall = _worse(overall, "FAIL")

    claimed_digest = network.get("flow_record_set_sha256")
    status, summary_payload = _verify_structured_digest(
        manifest, "network_summary", claimed_digest, "NETWORK_SUMMARY", 1, reasons
    ) if claimed_digest is not None else ("missing", None)
    if status == "mismatch":
        overall = _worse(overall, "FAIL")
    elif status == "missing":
        overall = _worse(overall, "INCONCLUSIVE")
    elif isinstance(summary_payload, Mapping):
        overall = _worse(
            overall,
            _cross_check_network_summary_content(network, summary_payload, reasons),
        )

    return overall, reasons


def _cross_check_network_summary_content(
    network: Mapping[str, object], summary: Mapping[str, object], reasons: list[str]
) -> str:
    """Independently recounts flows/DNS events from the raw, digest-verified
    network summary and compares them to what the evidence body declares.
    Expects a minimal, verifier-owned summary shape:
        {"flows": [{"disposition": str, "process_scoped": bool, "attributed": bool}, ...],
         "dns_events": [...]}
    This shape is this verifier's own documented expectation and has not yet
    been aligned with tools/network_summary.py's real output format -- that
    alignment is future work, out of scope here. The digest check above
    (raw file hash) already provides real, independent tamper-evidence even
    before that alignment lands; this cross-check adds a second, content-
    level layer whenever the summary happens to already match this shape."""

    flows = summary.get("flows")
    dns_events = summary.get("dns_events")
    if not isinstance(flows, list) or not isinstance(dns_events, list):
        reasons.append("network_summary_shape_unrecognized")
        return "INCONCLUSIVE"

    process_scoped_count = sum(1 for flow in flows if isinstance(flow, Mapping) and flow.get("process_scoped"))
    unattributed = [
        flow
        for flow in flows
        if isinstance(flow, Mapping) and not flow.get("process_scoped") and not flow.get("attributed")
    ]
    if unattributed:
        reasons.append("network_summary_unattributed_flow_present")
        return "FAIL"
    if process_scoped_count != network["process_scoped_tcp_flows"]:
        reasons.append("network_summary_process_scoped_count_mismatch")
        return "FAIL"
    if len(dns_events) != network["dns_events"]:
        reasons.append("network_summary_dns_event_count_mismatch")
        return "FAIL"
    return "PASS"


# ---------------------------------------------------------------------------
# Timeline verification
# ---------------------------------------------------------------------------


def _evaluate_timeline_clock_coherence(
    timeline: Mapping[str, object],
) -> tuple[str, list[str]]:
    reasons: list[str] = []
    frequency = int(timeline["qpc_frequency_hz"])
    events = list(timeline["events"])
    for previous, current in zip(events, events[1:]):
        qpc_delta = int(current["qpc"]) - int(previous["qpc"])
        utc_delta_ms = int(current["timestamp_unix_ms"]) - int(previous["timestamp_unix_ms"])
        disagreement = abs(qpc_delta * 1_000 - utc_delta_ms * frequency)
        if disagreement > _TIMELINE_CLOCK_TOLERANCE_MS * frequency:
            reasons.append("timeline_qpc_utc_disagreement")
            return "FAIL", reasons
    return "PASS", reasons


def _evaluate_direct_campaign_sequence(
    evidence_bodies: Mapping[str, Mapping[str, object]]
) -> tuple[str, list[str]]:
    reasons: list[str] = []
    present = [c for c in CONTROLS_DIRECT if c in evidence_bodies]
    if len(present) < len(CONTROLS_DIRECT):
        missing = sorted(set(CONTROLS_DIRECT) - set(present))
        reasons.append("direct_campaign_control_missing:" + ",".join(missing))
        return "INCONCLUSIVE", reasons

    c3_completed = evidence_bodies["C3"]["run_context"]["completed_at_unix"]
    c4_started = evidence_bodies["C4"]["run_context"]["started_at_unix"]
    c4_completed = evidence_bodies["C4"]["run_context"]["completed_at_unix"]
    c5_started = evidence_bodies["C5"]["run_context"]["started_at_unix"]

    if c3_completed > c4_started:
        reasons.append("direct_campaign_c3_c4_overlap")
        return "FAIL", reasons
    if c4_completed > c5_started:
        reasons.append("direct_campaign_c4_c5_overlap")
        return "FAIL", reasons
    return "PASS", reasons


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------


def verify_captured_run(
    run_dir: str | Path,
    run_id: str,
    *,
    allow_synthetic: bool = False,
) -> VerifierEvaluation:
    canonical_run_id = validate_run_id(run_id)
    manifest = build_run_artifact_manifest(Path(run_dir), canonical_run_id)

    evidence_bodies: dict[str, dict[str, object]] = {}
    all_reasons: list[str] = []
    overall = "PASS"

    for control in CONTROLS_C012 + CONTROLS_DIRECT:
        entry = manifest.get(_EVIDENCE_ROLE_FOR_CONTROL[control])
        if entry is None:
            continue
        try:
            raw_payload = _read_json_file(entry.resolved_path)
        except EvidenceVerifierError:
            all_reasons.append(f"{control}:evidence_artifact_unreadable")
            overall = _worse(overall, "FAIL")
            continue
        try:
            validated = validate_evidence(raw_payload)
        except LabValidationError as exc:
            all_reasons.append(f"{control}:evidence_structural_invalid:{exc}")
            overall = _worse(overall, "FAIL")
            continue
        if validated.get("control") != control:
            all_reasons.append(f"{control}:evidence_control_field_mismatch")
            overall = _worse(overall, "FAIL")
            continue
        evidence_bodies[control] = validated

    if not evidence_bodies:
        return VerifierEvaluation(
            "FAIL", tuple(["no_evidence_artifacts_found"] + all_reasons)
        )

    for control, body in evidence_bodies.items():
        provenance = body["proof_binding"]["provenance"]
        verdict, reasons = _check_provenance(provenance, allow_synthetic=allow_synthetic)
        overall = _worse(overall, verdict)
        all_reasons.extend(f"{control}:{reason}" for reason in reasons)

        if provenance.get("origin") == "CAPTURED_EXPORT":
            verdict, reasons = _check_captured_artifacts_present(body, manifest)
            overall = _worse(overall, verdict)
            all_reasons.extend(f"{control}:{reason}" for reason in reasons)

        verdict, reasons = _evaluate_proof_binding_digests(control, body, manifest)
        overall = _worse(overall, verdict)
        all_reasons.extend(f"{control}:{reason}" for reason in reasons)

        verdict, reasons = _evaluate_network(control, body, manifest)
        overall = _worse(overall, verdict)
        all_reasons.extend(f"{control}:{reason}" for reason in reasons)

        verdict, reasons = _evaluate_timeline_clock_coherence(body["timeline"])
        overall = _worse(overall, verdict)
        all_reasons.extend(f"{control}:{reason}" for reason in reasons)

    if all(control in evidence_bodies for control in CONTROLS_C012):
        verdict, reasons = _evaluate_job_root_continuity(evidence_bodies, manifest)
        overall = _worse(overall, verdict)
        all_reasons.extend(reasons)

    if any(control in evidence_bodies for control in CONTROLS_DIRECT):
        verdict, reasons = _evaluate_direct_campaign_sequence(evidence_bodies)
        overall = _worse(overall, verdict)
        all_reasons.extend(reasons)

    return VerifierEvaluation(overall, tuple(all_reasons))
