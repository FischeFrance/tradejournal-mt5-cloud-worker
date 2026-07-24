"""Tests for tools/lab_evidence_verifier.py.

Fixtures are built in-memory, then materialized into a temporary run
directory per test (tempfile.TemporaryDirectory), mirroring the in-memory
builder convention already used by test_lab_model.py -- but this file's
builder is deliberately its own, independent implementation (not an import of
test_lab_model.py's private helpers), consistent with the verifier's own
"do not share fate with the thing it verifies" principle.

Every evidence body constructed here is modeled directly on
examples/evidence.c0.synthetic-pass.json (the one real, schema-valid example
checked into the repository) and on lab_model.py's own per-control expected
values, read directly from source rather than guessed.
"""

from __future__ import annotations

import copy
import hashlib
import json
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

LAB_ROOT = Path(__file__).resolve().parents[1]
TOOLS = LAB_ROOT / "tools"
sys.path.insert(0, str(TOOLS))

from lab_evidence_verifier import (  # noqa: E402
    EvidenceVerifierError,
    _evaluate_direct_campaign_sequence,
    build_run_artifact_manifest,
    validate_run_id,
    verifier_contract_digest,
    verify_captured_run,
)

RUN_ID = "00000000-0000-4000-8000-000000000001"
SESSION_ID = "33333333-3333-4333-8333-333333333333"
JOB_ID = "44444444-4444-4444-8444-444444444444"
EXPERIMENT_ID = "11111111-1111-4111-8111-111111111111"

ROOT_PID = 4821
ROOT_KERNEL_CREATION_UTC_TICKS = 638123456789012345

JOB_IDENTITY_PAYLOAD = {
    "c012_session_id": SESSION_ID,
    "job_id": JOB_ID,
    "kill_on_job_close_verified": True,
    "breakaway_allowed": False,
    "silent_breakaway_allowed": False,
    "windows_session_id": 1,
}
ROOT_GENERATION_PAYLOAD = {
    "root_pid": ROOT_PID,
    "root_kernel_creation_utc_ticks": ROOT_KERNEL_CREATION_UTC_TICKS,
}

JOB_IDENTITY_SHA256 = verifier_contract_digest("JOB_IDENTITY", 1, JOB_IDENTITY_PAYLOAD)
ROOT_GENERATION_SHA256 = verifier_contract_digest(
    "ROOT_PROCESS_GENERATION", 1, ROOT_GENERATION_PAYLOAD
)


def _dummy_sha256(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


_PHASE_MARKERS = {
    "C0": ("C0_BASELINE_START", "C0_BASELINE_END"),
    "C1": (
        "C1_DISCOVERY_NEGATIVE_START",
        "C1_DISCOVERY_NEGATIVE_END",
        "C1_DISCOVERY_EXACT_START",
        "C1_DISCOVERY_EXACT_END",
    ),
    "C2": (
        "C2_LOGIN_START",
        "C2_LOGIN_END",
        "C2_CONNECTED_START",
        "C2_CONNECTED_END",
        "C2_NETWORK_INTERRUPTION_START",
        "C2_NETWORK_INTERRUPTION_END",
        "C2_RECONNECT_START",
        "C2_RECONNECT_END",
    ),
    "C3": ("C3_DIRECT_LOGIN_START", "C3_DIRECT_LOGIN_END", "C3_CONNECTED_STEADY_START", "C3_CONNECTED_STEADY_END"),
    "C4": ("C4_ENDPOINT_BLOCKED_START", "C4_ENDPOINT_BLOCKED_END"),
    "C5": ("C5_DIRECT_LOGIN_START", "C5_DIRECT_LOGIN_END", "C5_CONNECTED_STEADY_START", "C5_CONNECTED_STEADY_END"),
}

_LIFECYCLE_EXPECTED = {
    "C0": dict(
        session_role="LAUNCH_RETAIN",
        terminal_alive_at_start=False,
        terminal_alive_at_end=True,
        session_retained=True,
        teardown_completed=False,
        bootstrap_submitted_to_existing_session=False,
        transient_process_same_job_verified=False,
    ),
    "C1": dict(
        session_role="REUSE_RETAIN",
        terminal_alive_at_start=True,
        terminal_alive_at_end=True,
        session_retained=True,
        teardown_completed=False,
        bootstrap_submitted_to_existing_session=False,
        transient_process_same_job_verified=False,
    ),
    "C2": dict(
        session_role="REUSE_CONFIG_SUBMIT_TEARDOWN",
        terminal_alive_at_start=True,
        terminal_alive_at_end=False,
        session_retained=False,
        teardown_completed=True,
        bootstrap_submitted_to_existing_session=True,
        transient_process_same_job_verified=True,
    ),
}

_STATE_TRANSITION_EXPECTED = {
    "C0": dict(stage="C0_INITIAL", broker_cache_state="ABSENT", account_cache_state="ABSENT"),
    "C1": dict(
        stage="C1_DISCOVERY_COMPLETE",
        broker_cache_state="ABSENT_RECORDED",
        account_cache_state="ABSENT_RECORDED",
    ),
    "C2": dict(
        stage="C2_LOGIN_COMPLETE",
        broker_cache_state="ABSENT_RECORDED",
        account_cache_state="ABSENT_RECORDED",
    ),
}


def _timeline_for(control: str, *, start_qpc: int = 1_000_000, step: int = 600_000) -> dict:
    codes = _PHASE_MARKERS[control]
    events = []
    qpc = start_qpc
    timestamp = 1_000_000
    for index, code in enumerate(codes, start=1):
        events.append(
            {"code": code, "sequence": index, "timestamp_unix_ms": timestamp, "qpc": qpc}
        )
        qpc += step
        timestamp += step
    return {"schema_version": 1, "qpc_frequency_hz": 1000, "events": events}


def _run_context_for(control: str) -> dict:
    early = control in ("C0", "C1")
    return {
        "experiment_id": EXPERIMENT_ID,
        "cohort": "C012" if control in ("C0", "C1", "C2") else control,
        "clone_id_sha256": _dummy_sha256("clone"),
        "windows_user_sid_sha256": _dummy_sha256("sid"),
        "portable_root_path_sha256": _dummy_sha256("portable-root"),
        "terminal_sha256": _dummy_sha256("terminal"),
        "terminal_build": 5000,
        "expected_server": None if early else "Synthetic Broker Demo",
        "expected_company": None if early else "Synthetic Broker Ltd",
        "expected_trade_mode": None if early else "DEMO",
        "requested_server_label_sha256": _dummy_sha256("requested-label"),
        "credential_set_id": None if early else "55555555-5555-4555-8555-555555555555",
        "candidate_endpoint": None if early else {
            "ip": "8.8.8.8",
            "port": 443,
            "source_control": control,
            "observed_phase": "LOGIN",
            "process_scoped": True,
        },
        "started_at_unix": 1000,
        "completed_at_unix": 10000,
    }


def _network_neutral() -> dict:
    return {
        "attribution_unambiguous": False,
        "candidate_attempt_observed": False,
        "candidate_block_observed": False,
        "candidate_connected": False,
        "candidate_endpoint_safe": False,
        "candidate_observed_phase": "NONE",
        "candidate_tcp_flows": 0,
        "candidate_tuple_match": False,
        "dns_events": 0,
        "flow_record_set_sha256": None,
        "flow_record_set_verified": False,
        "non_tcp_network_events": 0,
        "other_tcp_flows": 0,
        "process_scoped_tcp_flows": 0,
    }


def _discovery_neutral() -> dict:
    return {
        "cache_influence_excluded": False,
        "credentials_supplied": False,
        "endpoint_delta_acquired": False,
        "endpoint_delta_source": "NONE",
        "endpoint_delta_source_sha256": None,
        "endpoint_delta_source_verified": False,
        "exact_label_match_verified": False,
        "exact_selection_completed": False,
        "expected_exact_result_count": 0,
        "helper_secret_accessed": False,
        "negative_exact_query_completed": False,
        "negative_query_label_sha256": None,
        "negative_query_result_count": None,
        "negative_query_ui_binding_verified": False,
        "selected_server_label_sha256": None,
        "unsafe_endpoint_promoted": False,
    }


def _environment_health(control: str = "C0") -> dict:
    return {
        "account_available": False,
        "baseline_stable": control == "C0",
        "build_unchanged": True,
        "clock_synchronized": True,
        "external_outage_excluded": False,
        "firewall_policy_verified": False,
        "ui_compatible": control == "C1",
    }


def _pre_state() -> dict:
    return {
        "accounts_dat_absent": True,
        "appdata_absent": True,
        "bases_absent": True,
        "community_identity_absent": True,
        "credential_manager_empty": True,
        "disposable_clone_new": True,
        "no_shared_storage": True,
        "portable_root_new": True,
        "prior_processes_absent": True,
        "registry_clean": True,
        "sensitive_bootstrap_absent": True,
        "servers_dat_absent": True,
        "terminal_data_path_matches": True,
        "windows_user_new": True,
    }


def _timing_neutral() -> dict:
    return {
        "baseline_seconds": 0,
        "blocked_observation_seconds": 0,
        "connected_steady_seconds": 0,
        "exact_discovery_seconds": 0,
        "login_observation_seconds": 0,
        "negative_discovery_seconds": 0,
        "network_interruption_seconds": 0,
        "reconnect_observation_seconds": 0,
        "separation_from_c3_seconds": 0,
    }


def build_evidence_body(
    control: str,
    *,
    provenance_origin: str = "CAPTURED_EXPORT",
    job_identity_sha256: str = JOB_IDENTITY_SHA256,
    root_process_generation_sha256: str = ROOT_GENERATION_SHA256,
    etw_evidence_sha256: str | None = None,
    wfp_evidence_sha256: str | None = None,
    network: dict | None = None,
) -> dict:
    lifecycle_expected = _LIFECYCLE_EXPECTED[control]
    state_expected = _STATE_TRANSITION_EXPECTED[control]
    synthetic = provenance_origin == "SYNTHETIC_FIXTURE"

    timeline = _timeline_for(control)
    identity = None
    if control == "C2":
        identity = {
            "account_match": True,
            "expected_server_match": True,
            "expected_company_match": True,
            "account_trade_allowed": False,
            "account_trade_expert": False,
            "terminal_connected": True,
            "terminal_trade_allowed": False,
            "investor_provenance_confirmed": True,
            "probe_hash_verified": True,
            "probe_static_guard_passed": True,
            "probe_path_binding_verified": True,
            "probe_run_id": RUN_ID,
            "probe_generated_at_unix": 1000,
            "server": "Synthetic Broker Demo",
            "company": "Synthetic Broker Ltd",
            "trade_mode": "DEMO",
            "terminal_build": 5000,
            "terminal_path_sha256": _dummy_sha256("terminal"),
            "terminal_data_path_sha256": _dummy_sha256("data"),
            "identity_probe_output_sha256": _dummy_sha256("probe"),
        }
    body = {
        "schema_version": 6,
        "run_id": RUN_ID,
        "control": control,
        "capture_integrity": {
            "buffers_lost": 0,
            "etw_started": True,
            "etw_stopped": True,
            "events_lost": 0,
            "required_markers_present": True,
        },
        "credential_bundle_investor_confirmed": control == "C2",
        "discovery": _discovery_neutral(),
        "environment_health": _environment_health(control),
        "identity": identity,
        "initial_pre_state_binding": {
            "schema_version": 1,
            "scope": "C012_INITIAL" if control == "C0" else "C012_REFERENCE",
            "initial_c012_pre_state_sha256": _dummy_sha256("initial-pre-state"),
            "portable_root_path_sha256": _dummy_sha256("portable-root"),
        },
        "lifecycle_binding": {
            "schema_version": 1,
            "lifecycle_mode": "C012_SINGLE_PROCESS_SESSION",
            "c012_session_id": SESSION_ID,
            "session_role": lifecycle_expected["session_role"],
            "job_id": JOB_ID,
            "job_manifest_sha256": _dummy_sha256("job-manifest"),
            "job_identity_sha256": job_identity_sha256,
            "root_process_generation_sha256": root_process_generation_sha256,
            "terminal_alive_at_start": lifecycle_expected["terminal_alive_at_start"],
            "terminal_alive_at_end": lifecycle_expected["terminal_alive_at_end"],
            "session_retained": lifecycle_expected["session_retained"],
            "teardown_completed": lifecycle_expected["teardown_completed"],
            "bootstrap_submitted_to_existing_session": lifecycle_expected[
                "bootstrap_submitted_to_existing_session"
            ],
            "transient_process_set_sha256": (
                _dummy_sha256("transient-process-set") if control == "C2" else None
            ),
            "transient_process_same_job_verified": lifecycle_expected[
                "transient_process_same_job_verified"
            ],
        },
        "network": network if network is not None else _network_neutral(),
        "phase_markers": list(_PHASE_MARKERS[control]),
        "pre_state": _pre_state() if control == "C0" else None,
        "proof_binding": {
            "schema_version": 5,
            "run_id": RUN_ID,
            "provenance": {
                "origin": provenance_origin,
                "synthetic_fixture": synthetic,
                "producer": "OFFLINE_TEST_FIXTURE" if synthetic else "LAB_CAPTURE_EXPORT",
                "artifact_set_id": "10000000-0000-4000-8000-000000000001",
            },
            "job_manifest_sha256": _dummy_sha256("job-manifest"),
            "phase_timeline_sha256": verifier_contract_digest(
                "PHASE_TIMELINE", 1, {"run_id": RUN_ID, "control": control, "timeline": timeline}
            ),
            "etw_evidence_sha256": etw_evidence_sha256,
            "wfp_evidence_sha256": wfp_evidence_sha256 if control not in ("C0", "C1", "C2") else None,
            "firewall_plan_sha256": None,
            "candidate_endpoint_sha256": None,
            "credential_set_binding_sha256": None,
            "requested_label_binding_sha256": _dummy_sha256("requested-label-binding"),
            "job_identity_sha256": job_identity_sha256,
            "root_process_generation_sha256": root_process_generation_sha256,
            "experiment_manifest_sha256": _dummy_sha256("experiment-manifest"),
            "control_plan_sha256": _dummy_sha256("control-plan"),
            "candidate_handoff_manifest_sha256": None,
            "probe_path_binding_sha256": None,
            "lifecycle_binding_sha256": _dummy_sha256("lifecycle-binding"),
            "job_portable_root_binding_sha256": _dummy_sha256("job-portable-root-binding"),
            "pre_state_binding_sha256": _dummy_sha256("pre-state-binding"),
            "state_transition_sha256": (
                _dummy_sha256("state-transition") if control in ("C1", "C2") else None
            ),
            "firewall_portable_root_binding_sha256": None,
            "negative_query_binding_sha256": None,
            "job_process_binding_verified": True,
            "phase_binding_verified": True,
            "etw_proof_capable": etw_evidence_sha256 is not None,
            "wfp_proof_capable": wfp_evidence_sha256 is not None and control not in ("C0", "C1", "C2"),
            "firewall_plan_bound": False,
            "candidate_tuple_bound": False,
            "credential_set_binding_verified": False,
            "requested_label_binding_verified": True,
        },
        "run_context": _run_context_for(control),
        "state_transition": {
            "schema_version": 1,
            "stage": state_expected["stage"],
            "broker_cache_state": state_expected["broker_cache_state"],
            "account_cache_state": state_expected["account_cache_state"],
            "sensitive_material_exported": False,
            "transition_evidence_sha256": (
                _dummy_sha256("transition-evidence") if control in ("C1", "C2") else None
            ),
            "transition_verified": True,
        },
        "timeline": timeline,
        "timing": {
            **_timing_neutral(),
            **({"baseline_seconds": 600} if control == "C0" else {}),
            **({"negative_discovery_seconds": 600, "exact_discovery_seconds": 600} if control == "C1" else {}),
            **({"login_observation_seconds": 600, "connected_steady_seconds": 600, "network_interruption_seconds": 600, "reconnect_observation_seconds": 600} if control == "C2" else {}),
        },
    }
    return body


class _RunDirFixture:
    """Materializes evidence bodies and companion export/preimage files into a
    temporary run directory using the exact filename templates
    lab_evidence_verifier.py's manifest expects."""

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.run_dir = Path(self._tmp.name)

    def close(self) -> None:
        self._tmp.cleanup()

    def _write_json(self, filename: str, payload: object) -> str:
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        (self.run_dir / filename).write_text(text, encoding="utf-8")
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def write_evidence(self, control: str, body: dict) -> None:
        (self.run_dir / f"{RUN_ID}.evidence.{control.lower()}.json").write_text(
            json.dumps(body, ensure_ascii=False), encoding="utf-8"
        )

    def write_job_identity_preimage(self, control: str, payload: dict) -> None:
        (self.run_dir / f"{RUN_ID}.job-identity.{control.lower()}.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )

    def write_root_generation_preimage(self, control: str, payload: dict) -> None:
        (self.run_dir / f"{RUN_ID}.root-process-generation.{control.lower()}.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )

    def write_etw_export(self, payload: object) -> str:
        return self._write_json(f"{RUN_ID}.etw-evidence.sanitized.json", payload)

    def write_wfp_export(self, lines: list[dict]) -> str:
        text = "\n".join(json.dumps(line, sort_keys=True) for line in lines) + "\n"
        (self.run_dir / f"{RUN_ID}.wfp-security.sanitized.jsonl").write_text(text, encoding="utf-8")
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def write_network_summary(self, payload: dict) -> str:
        return self._write_json(f"{RUN_ID}.network-summary.json", payload)

    def write_all_preimages(self) -> None:
        for control in ("C0", "C1", "C2"):
            self.write_job_identity_preimage(control, JOB_IDENTITY_PAYLOAD)
            self.write_root_generation_preimage(control, ROOT_GENERATION_PAYLOAD)


def _full_captured_fixture() -> _RunDirFixture:
    """A complete, internally-consistent CAPTURED_EXPORT run: all three C012
    evidence bodies, all preimages, ETW/WFP exports and a network summary that
    matches what each evidence body declares. This is the only scenario in
    this test file (and, per the design, the only kind of scenario anywhere in
    the lab) that should reach a genuine PASS."""

    fixture = _RunDirFixture()

    etw_payload = {"process_events": [{"pid": ROOT_PID, "event_type": "START"}]}
    etw_sha256 = fixture.write_etw_export(etw_payload)

    wfp_sha256 = fixture.write_wfp_export([{"pid": ROOT_PID, "event": "connection_permitted"}])

    network_summary_payload = {
        "schema_version": 1,
        "flows": [{"disposition": "connected", "process_scoped": True, "attributed": True}],
        "dns_events": [{"attributed": True}],
    }
    summary_digest = verifier_contract_digest("NETWORK_SUMMARY", 1, network_summary_payload)
    fixture.write_network_summary(network_summary_payload)

    network = _network_neutral()
    network.update(
        {
            "process_scoped_tcp_flows": 1,
            "candidate_tcp_flows": 0,
            "other_tcp_flows": 1,
            "attribution_unambiguous": True,
            "dns_events": 1,
            "flow_record_set_sha256": summary_digest,
            "flow_record_set_verified": True,
        }
    )

    for control in ("C0", "C1", "C2"):
        body = build_evidence_body(
            control,
            provenance_origin="CAPTURED_EXPORT",
            etw_evidence_sha256=etw_sha256,
            wfp_evidence_sha256=wfp_sha256,
            network=network,
        )
        fixture.write_evidence(control, body)

    fixture.write_all_preimages()
    return fixture


class ValidateRunIdTests(unittest.TestCase):
    def test_accepts_canonical_uuid(self) -> None:
        self.assertEqual(validate_run_id(RUN_ID), RUN_ID)

    def test_rejects_non_uuid(self) -> None:
        with self.assertRaises(EvidenceVerifierError):
            validate_run_id("../../etc/passwd")

    def test_rejects_uppercase_uuid(self) -> None:
        with self.assertRaises(EvidenceVerifierError):
            validate_run_id("ABCDEF00-0000-4000-8000-000000000001")

    def test_rejects_braced_uuid(self) -> None:
        with self.assertRaises(EvidenceVerifierError):
            validate_run_id("{" + RUN_ID + "}")

    def test_rejects_empty_string(self) -> None:
        with self.assertRaises(EvidenceVerifierError):
            validate_run_id("")


class ArtifactManifestPathSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.run_dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_manifest_ignores_absent_files(self) -> None:
        manifest = build_run_artifact_manifest(self.run_dir, RUN_ID)
        self.assertEqual(manifest, {})

    def test_manifest_rejects_invalid_run_id_before_touching_disk(self) -> None:
        with self.assertRaises(EvidenceVerifierError):
            build_run_artifact_manifest(self.run_dir, "../../etc/passwd")

    def test_manifest_rejects_symlink_escape(self) -> None:
        outside = Path(tempfile.mkdtemp())
        try:
            secret = outside / "secret.json"
            secret.write_text("{}", encoding="utf-8")
            link_path = self.run_dir / f"{RUN_ID}.evidence.c0.json"
            try:
                link_path.symlink_to(secret)
            except OSError:
                self.skipTest("symlinks not supported in this test environment")
            with self.assertRaises(EvidenceVerifierError):
                build_run_artifact_manifest(self.run_dir, RUN_ID)
        finally:
            import shutil

            shutil.rmtree(outside, ignore_errors=True)


class VerifyCapturedRunTests(unittest.TestCase):
    def test_full_captured_evidence_reaches_pass(self) -> None:
        fixture = _full_captured_fixture()
        try:
            result = verify_captured_run(fixture.run_dir, RUN_ID)
            self.assertEqual(result.outcome, "PASS", result.reasons)
        finally:
            fixture.close()

    def test_synthetic_evidence_never_reaches_pass_without_allow_synthetic(self) -> None:
        fixture = _full_captured_fixture()
        try:
            for control in ("C0", "C1", "C2"):
                body = json.loads(
                    (fixture.run_dir / f"{RUN_ID}.evidence.{control.lower()}.json").read_text()
                )
                body["proof_binding"]["provenance"] = {
                    "origin": "SYNTHETIC_FIXTURE",
                    "synthetic_fixture": True,
                    "producer": "OFFLINE_TEST_FIXTURE",
                    "artifact_set_id": "10000000-0000-4000-8000-000000000001",
                }
                fixture.write_evidence(control, body)
            result = verify_captured_run(fixture.run_dir, RUN_ID)
            self.assertEqual(result.outcome, "FAIL")
            self.assertTrue(any("synthetic_evidence_rejected" in r for r in result.reasons))
        finally:
            fixture.close()

    def test_synthetic_evidence_reaches_synthetic_pass_with_allow_synthetic(self) -> None:
        fixture = _full_captured_fixture()
        try:
            for control in ("C0", "C1", "C2"):
                body = json.loads(
                    (fixture.run_dir / f"{RUN_ID}.evidence.{control.lower()}.json").read_text()
                )
                body["proof_binding"]["provenance"] = {
                    "origin": "SYNTHETIC_FIXTURE",
                    "synthetic_fixture": True,
                    "producer": "OFFLINE_TEST_FIXTURE",
                    "artifact_set_id": "10000000-0000-4000-8000-000000000001",
                }
                fixture.write_evidence(control, body)
            result = verify_captured_run(fixture.run_dir, RUN_ID, allow_synthetic=True)
            self.assertEqual(result.outcome, "SYNTHETIC_PASS", result.reasons)
        finally:
            fixture.close()

    def test_captured_export_missing_artifact_fails(self) -> None:
        fixture = _full_captured_fixture()
        try:
            (fixture.run_dir / f"{RUN_ID}.etw-evidence.sanitized.json").unlink()
            result = verify_captured_run(fixture.run_dir, RUN_ID)
            self.assertEqual(result.outcome, "FAIL")
            self.assertTrue(any("captured_export_artifact_missing" in r for r in result.reasons))
        finally:
            fixture.close()

    def test_tampered_digest_fails(self) -> None:
        fixture = _full_captured_fixture()
        try:
            body = json.loads((fixture.run_dir / f"{RUN_ID}.evidence.c0.json").read_text())
            body["proof_binding"]["etw_evidence_sha256"] = _dummy_sha256("tampered")
            fixture.write_evidence("C0", body)
            result = verify_captured_run(fixture.run_dir, RUN_ID)
            self.assertEqual(result.outcome, "FAIL")
            self.assertTrue(any("digest_mismatch" in r for r in result.reasons))
        finally:
            fixture.close()

    def test_provenance_internal_inconsistency_fails(self) -> None:
        fixture = _full_captured_fixture()
        try:
            body = json.loads((fixture.run_dir / f"{RUN_ID}.evidence.c0.json").read_text())
            body["proof_binding"]["provenance"]["synthetic_fixture"] = True
            fixture.write_evidence("C0", body)
            result = verify_captured_run(fixture.run_dir, RUN_ID)
            self.assertEqual(result.outcome, "FAIL")
        finally:
            fixture.close()

    def test_job_identity_digest_mismatch_across_controls_fails(self) -> None:
        fixture = _full_captured_fixture()
        try:
            body = json.loads((fixture.run_dir / f"{RUN_ID}.evidence.c1.json").read_text())
            body["lifecycle_binding"]["job_identity_sha256"] = _dummy_sha256("different-job-identity")
            body["proof_binding"]["job_identity_sha256"] = _dummy_sha256("different-job-identity")
            fixture.write_evidence("C1", body)
            result = verify_captured_run(fixture.run_dir, RUN_ID)
            self.assertEqual(result.outcome, "FAIL")
            self.assertIn("job_root_digest_mismatch_across_controls", result.reasons)
        finally:
            fixture.close()

    def test_job_identity_digest_equal_but_no_preimage_is_inconclusive(self) -> None:
        fixture = _full_captured_fixture()
        try:
            for control in ("C0", "C1", "C2"):
                (fixture.run_dir / f"{RUN_ID}.job-identity.{control.lower()}.json").unlink()
                (fixture.run_dir / f"{RUN_ID}.root-process-generation.{control.lower()}.json").unlink()
            result = verify_captured_run(fixture.run_dir, RUN_ID)
            self.assertEqual(result.outcome, "INCONCLUSIVE", result.reasons)
            self.assertIn("job_root_continuity_unverified_digest_only", result.reasons)
        finally:
            fixture.close()

    def test_job_identity_preimage_digest_mismatch_fails(self) -> None:
        fixture = _full_captured_fixture()
        try:
            tampered = dict(JOB_IDENTITY_PAYLOAD)
            tampered["windows_session_id"] = 99
            fixture.write_job_identity_preimage("C1", tampered)
            result = verify_captured_run(fixture.run_dir, RUN_ID)
            self.assertEqual(result.outcome, "FAIL")
            self.assertTrue(any("digest_mismatch:job_identity_preimage_c1" in r for r in result.reasons))
        finally:
            fixture.close()

    def test_candidate_ip_out_of_range_fails(self) -> None:
        fixture = _RunDirFixture()
        try:
            body = build_evidence_body("C0")
            body["run_context"] = _run_context_for("C2")
            body["run_context"]["candidate_endpoint"] = {
                "ip": "10.0.0.1",
                "port": 443,
                "source_control": "C2",
                "observed_phase": "LOGIN",
                "process_scoped": True,
            }
            fixture.write_evidence("C0", body)
            result = verify_captured_run(fixture.run_dir, RUN_ID)
            self.assertEqual(result.outcome, "FAIL")
        finally:
            fixture.close()

    def test_candidate_port_blocked_fails(self) -> None:
        fixture = _RunDirFixture()
        try:
            body = build_evidence_body("C0")
            body["run_context"] = _run_context_for("C2")
            body["run_context"]["candidate_endpoint"] = {
                "ip": "8.8.8.8",
                "port": 22,
                "source_control": "C2",
                "observed_phase": "LOGIN",
                "process_scoped": True,
            }
            fixture.write_evidence("C0", body)
            result = verify_captured_run(fixture.run_dir, RUN_ID)
            self.assertEqual(result.outcome, "FAIL")
        finally:
            fixture.close()

    def test_dns_event_count_mismatch_fails(self) -> None:
        fixture = _full_captured_fixture()
        try:
            body = json.loads((fixture.run_dir / f"{RUN_ID}.evidence.c0.json").read_text())
            body["network"]["dns_events"] = 5
            fixture.write_evidence("C0", body)
            result = verify_captured_run(fixture.run_dir, RUN_ID)
            self.assertEqual(result.outcome, "FAIL")
            self.assertTrue(any("dns_event_count_mismatch" in r for r in result.reasons))
        finally:
            fixture.close()

    def test_other_tcp_flow_unattributed_fails(self) -> None:
        fixture = _full_captured_fixture()
        try:
            body = json.loads((fixture.run_dir / f"{RUN_ID}.evidence.c0.json").read_text())
            body["network"]["attribution_unambiguous"] = False
            fixture.write_evidence("C0", body)
            result = verify_captured_run(fixture.run_dir, RUN_ID)
            self.assertEqual(result.outcome, "FAIL")
            self.assertTrue(any("unattributed_foreign_tcp_flow" in r for r in result.reasons))
        finally:
            fixture.close()

    def test_timeline_overlap_c3_c5_fails(self) -> None:
        evidence = {
            "C3": {"run_context": {"completed_at_unix": 10}},
            "C4": {"run_context": {"started_at_unix": 1, "completed_at_unix": 20}},
            "C5": {"run_context": {"started_at_unix": 15}},
        }
        outcome, reasons = _evaluate_direct_campaign_sequence(evidence)
        self.assertEqual(outcome, "FAIL")
        self.assertTrue(any("direct_campaign" in reason for reason in reasons))

    def test_etw_export_missing_is_inconclusive(self) -> None:
        fixture = _full_captured_fixture()
        try:
            for control in ("C0", "C1", "C2"):
                body = json.loads(
                    (fixture.run_dir / f"{RUN_ID}.evidence.{control.lower()}.json").read_text()
                )
                body["proof_binding"]["etw_evidence_sha256"] = None
                body["proof_binding"]["etw_proof_capable"] = False
                fixture.write_evidence(control, body)
            (fixture.run_dir / f"{RUN_ID}.etw-evidence.sanitized.json").unlink()
            result = verify_captured_run(fixture.run_dir, RUN_ID)
            self.assertEqual(result.outcome, "INCONCLUSIVE")
        finally:
            fixture.close()

    def test_wfp_export_missing_is_inconclusive(self) -> None:
        fixture = _full_captured_fixture()
        try:
            for control in ("C0", "C1", "C2"):
                body = json.loads(
                    (fixture.run_dir / f"{RUN_ID}.evidence.{control.lower()}.json").read_text()
                )
                body["proof_binding"]["wfp_evidence_sha256"] = None
                body["proof_binding"]["wfp_proof_capable"] = False
                fixture.write_evidence(control, body)
            (fixture.run_dir / f"{RUN_ID}.wfp-security.sanitized.jsonl").unlink()
            result = verify_captured_run(fixture.run_dir, RUN_ID)
            self.assertEqual(result.outcome, "PASS")
        finally:
            fixture.close()

    def test_no_evidence_artifacts_found_fails(self) -> None:
        fixture = _RunDirFixture()
        try:
            result = verify_captured_run(fixture.run_dir, RUN_ID)
            self.assertEqual(result.outcome, "FAIL")
            self.assertIn("no_evidence_artifacts_found", result.reasons)
        finally:
            fixture.close()


if __name__ == "__main__":
    unittest.main()
