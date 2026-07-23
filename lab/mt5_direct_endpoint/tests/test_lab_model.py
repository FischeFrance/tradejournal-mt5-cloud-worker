from __future__ import annotations

import copy
import sys
import unittest
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath


LAB_ROOT = Path(__file__).resolve().parents[1]
TOOLS = LAB_ROOT / "tools"
sys.path.insert(0, str(TOOLS))

from lab_model import (  # noqa: E402
    C012_LIFECYCLE_MODE,
    DIRECT_LIFECYCLE_MODE,
    EXTERNAL_DENY_ROLE,
    POLICY_VERSION,
    PROBE_POLICY,
    LabValidationError,
    build_candidate_handoff,
    build_control_plan,
    build_direct_campaign_manifest,
    build_experiment_manifest,
    canonical_json,
    compose_identity,
    contract_digest,
    derive_timing_from_timeline,
    evidence_digest,
    evaluate_campaign,
    evaluate_evidence,
    firewall_portable_root_binding_digest,
    job_portable_root_binding_digest,
    lifecycle_binding_digest,
    probe_path_binding_digest,
    requested_label_manifest_digest,
    validate_candidate,
    validate_candidate_handoff,
    validate_config,
    validate_control_plan,
    validate_direct_campaign_manifest,
    validate_evidence,
    validate_experiment_manifest,
    windows_path_digest,
)


EXPERIMENT_ID = "11111111-1111-4111-8111-111111111111"
C012_SESSION_ID = "33333333-3333-4333-8333-333333333333"
CREDENTIAL_SET_ID = "22222222-2222-4222-8222-222222222222"
REQUESTED_SERVER_LABEL = "Synthetic Broker Demo"
REQUESTED_SERVER_LABEL_SHA256 = evidence_digest(
    {"requested_server_label": REQUESTED_SERVER_LABEL}
)
CANDIDATE = {
    "ip": "8.8.8.8",
    "port": 443,
    "source_control": "C2",
    "observed_phase": "LOGIN",
    "process_scoped": True,
}
CONTROLS = ("C0", "C1", "C2", "C3", "C4", "C5")
RUN_IDS = {
    control: f"00000000-0000-4000-8000-{index:012d}"
    for index, control in enumerate(CONTROLS, start=1)
}
JOB_IDS = {
    control: (
        "44444444-4444-4444-8444-444444444444"
        if control in {"C0", "C1", "C2"}
        else f"55555555-5555-4555-8555-{int(control[1]):012d}"
    )
    for control in CONTROLS
}
STARTS = {
    "C0": 1_000,
    "C1": 2_000,
    "C2": 3_000,
    "C3": 10_000,
    "C4": 11_000,
    # C3's last marker is 10_720_001 ms.  This starts C5 more than 1,800
    # seconds later without relying on a self-asserted separation field.
    "C5": 12_521,
}
QPC_STARTS = {
    "C0": 1_000_000,
    "C1": 3_000_000,
    "C2": 5_000_000,
    "C3": 1_000_000,
    "C4": 1_000_000,
    "C5": 1_000_000,
}
PHASES = {
    "C0": (("C0_BASELINE", 600),),
    "C1": (
        ("C1_DISCOVERY_NEGATIVE", 120),
        ("C1_DISCOVERY_EXACT", 180),
    ),
    "C2": (
        ("C2_LOGIN", 120),
        ("C2_CONNECTED", 600),
        ("C2_NETWORK_INTERRUPTION", 30),
        ("C2_RECONNECT", 300),
    ),
    "C3": (
        ("C3_DIRECT_LOGIN", 120),
        ("C3_CONNECTED_STEADY", 600),
    ),
    "C4": (("C4_ENDPOINT_BLOCKED", 180),),
    "C5": (
        ("C5_DIRECT_LOGIN", 120),
        ("C5_CONNECTED_STEADY", 600),
    ),
}


def make_config() -> dict[str, object]:
    """Return the authoritative, candidate-free Patch 7 config fixture."""

    return {
        "schema_version": 4,
        "policy_version": POLICY_VERSION,
        "experiment_id": EXPERIMENT_ID,
        "region": "offline-test",
        "lab_root": r"C:\TJLab",
        "terminal": {
            "path": r"C:\TJLabSource\terminal64.exe",
            "sha256": "d" * 64,
            "publisher": "Verified Test Publisher",
            "signer_policy_sha256": "b" * 64,
        },
        "lifecycle": {
            "schema_version": 1,
            "lifecycle_mode": C012_LIFECYCLE_MODE,
            "c012_session_id": C012_SESSION_ID,
            "launch_control": "C0",
            "teardown_control": "C2",
            "root_process_generation_policy": "SINGLE_SHARED_C0_C1_C2",
            "allowed_transient_process_policy": (
                "C2_CONFIG_SUBMITTER_SAME_JOB_ONLY"
            ),
        },
        "requested_server_label": REQUESTED_SERVER_LABEL,
        "expected_identity": {
            "server": "Broker-Demo",
            "company": "Example Broker Ltd",
            "trade_mode": "DEMO",
        },
        "probe": {
            "schema_version": 3,
            "probe_version": "3.0.0",
            "source_sha256": "c" * 64,
            "policy": PROBE_POLICY,
            "symbol": "EURUSD",
        },
        "network_policy": {
            "transport": "TCP",
            "allowed_remote_ports": [443],
            "candidate_address_policy": "GLOBAL_LITERAL_ONLY",
            "candidate_source_policy": "C2_LOGIN_PROCESS_SCOPED",
            "direct_egress_policy": "CANDIDATE_ONLY",
            "direct_dns_events_max": 0,
            "direct_other_tcp_flows_max": 0,
            "external_deny_role": EXTERNAL_DENY_ROLE,
        },
        "durations_seconds": {
            "baseline": 600,
            "negative_discovery": 120,
            "exact_discovery": 180,
            "login_timeout": 120,
            "connected_steady": 600,
            "network_interruption": 30,
            "reconnect_observation": 300,
            "blocked_timeout": 180,
            "c4_elapsed_tolerance": 30,
            "c5_separation_minimum": 1800,
            "probe_timestamp_tolerance_seconds": 2,
        },
    }


def _timeline(control: str) -> dict[str, object]:
    timestamp = STARTS[control] * 1000
    qpc = QPC_STARTS[control]
    sequence = 1
    events: list[dict[str, object]] = []
    for phase_index, (phase, duration_seconds) in enumerate(PHASES[control]):
        events.append(
            {
                "code": f"{phase}_START",
                "sequence": sequence,
                "timestamp_unix_ms": timestamp,
                "qpc": qpc,
            }
        )
        sequence += 1
        timestamp += duration_seconds * 1000
        qpc += duration_seconds * 1000
        events.append(
            {
                "code": f"{phase}_END",
                "sequence": sequence,
                "timestamp_unix_ms": timestamp,
                "qpc": qpc,
            }
        )
        sequence += 1
        if phase_index + 1 < len(PHASES[control]):
            timestamp += 1
            qpc += 1
    return {
        "schema_version": 1,
        "qpc_frequency_hz": 1000,
        "events": events,
    }


def _run_context(
    control: str,
    timeline: dict[str, object],
    plan: dict[str, object],
) -> dict[str, object]:
    direct = control in {"C3", "C4", "C5"}
    identity_control = control in {"C2", "C3", "C4", "C5"}
    cohort = "C012" if control in {"C0", "C1", "C2"} else control
    digest_chars = {
        "C012": ("1", "2", "3"),
        "C3": ("4", "5", "6"),
        "C4": ("7", "8", "9"),
        "C5": ("a", "b", "c"),
    }
    clone_char, user_char, _ = digest_chars[cohort]
    path_bindings = plan["path_bindings"]
    assert isinstance(path_bindings, dict)
    events = timeline["events"]
    assert isinstance(events, list)
    last_timestamp_ms = int(events[-1]["timestamp_unix_ms"])
    return {
        "experiment_id": EXPERIMENT_ID,
        "cohort": cohort,
        "clone_id_sha256": clone_char * 64,
        "windows_user_sid_sha256": user_char * 64,
        "portable_root_path_sha256": path_bindings[
            "terminal_data_path_sha256"
        ],
        "terminal_sha256": "d" * 64,
        "terminal_build": 5000,
        "expected_server": "Broker-Demo" if identity_control else None,
        "expected_company": "Example Broker Ltd" if identity_control else None,
        "expected_trade_mode": "DEMO" if identity_control else None,
        "requested_server_label_sha256": (
            None if direct else REQUESTED_SERVER_LABEL_SHA256
        ),
        "credential_set_id": (
            CREDENTIAL_SET_ID if identity_control else None
        ),
        "candidate_endpoint": (
            copy.deepcopy(CANDIDATE)
            if control in {"C2", "C3", "C4", "C5"}
            else None
        ),
        "started_at_unix": STARTS[control],
        "completed_at_unix": (last_timestamp_ms + 999) // 1000,
    }


def _identity_probe(
    plan: dict[str, object],
    control: str,
    *,
    terminal_build: int = 5000,
    terminal_path: str | None = None,
    terminal_data_path: str | None = None,
) -> dict[str, object]:
    paths = plan["paths"]
    assert isinstance(paths, dict)
    planned_terminal = str(paths["terminal"])
    return {
        "schema_version": 3,
        "probe_version": "3.0.0",
        "run_id": RUN_IDS[control],
        "generated_at_unix": STARTS[control] + 1,
        "terminal_result": "CONNECTED_IDENTITY_AVAILABLE",
        "expected_login_loaded": True,
        "terminal_connected": True,
        "account_match": True,
        "account_trade_allowed": False,
        "account_trade_expert": False,
        "terminal_trade_allowed": False,
        "account_server": "Broker-Demo",
        "account_company": "Example Broker Ltd",
        "account_trade_mode": "DEMO",
        "terminal_build": terminal_build,
        "terminal_path": (
            planned_terminal if terminal_path is None else terminal_path
        ),
        "terminal_data_path": (
            str(PureWindowsPath(planned_terminal).parent)
            if terminal_data_path is None
            else terminal_data_path
        ),
    }


def _fixture_digest(control: str, kind: str) -> str:
    return contract_digest(
        "OFFLINE_TEST_FIXTURE",
        1,
        {"control": control, "kind": kind},
    )


def _network(control: str) -> dict[str, object]:
    candidate = control in {"C2", "C3", "C4", "C5"}
    connected = control in {"C2", "C3", "C5"}
    blocked = control == "C4"
    other = 1 if control == "C1" else 0
    candidate_flows = 1 if candidate else 0
    total = candidate_flows + other
    observed_phase = {
        "C0": "NONE",
        "C1": "NONE",
        "C2": "LOGIN",
        "C3": "DIRECT_ONLY",
        "C4": "ENDPOINT_BLOCKED",
        "C5": "DIRECT_REPEAT",
    }[control]
    flow_digest = contract_digest(
        "NETWORK_FLOW_RECORD_SET",
        2,
        {
            "run_id": RUN_IDS[control],
            "control": control,
            "candidate_endpoint": CANDIDATE if candidate else None,
            "candidate_tcp_flows": candidate_flows,
            "other_tcp_flows": other,
        },
    )
    return {
        "candidate_attempt_observed": candidate,
        "candidate_connected": connected,
        "candidate_block_observed": blocked,
        "candidate_endpoint_safe": candidate,
        "candidate_tuple_match": candidate,
        "candidate_observed_phase": observed_phase,
        "attribution_unambiguous": total > 0,
        "process_scoped_tcp_flows": total,
        "candidate_tcp_flows": candidate_flows,
        "other_tcp_flows": other,
        "dns_events": 0,
        "non_tcp_network_events": 0,
        "flow_record_set_sha256": flow_digest,
        "flow_record_set_verified": True,
    }


def _discovery(
    control: str,
    network: dict[str, object],
    manifest: dict[str, object],
) -> dict[str, object]:
    if control == "C1":
        negative_query = manifest["negative_query"]
        assert isinstance(negative_query, dict)
        return {
            "credentials_supplied": False,
            "negative_exact_query_completed": True,
            "expected_exact_result_count": 1,
            "exact_selection_completed": True,
            "helper_secret_accessed": False,
            "unsafe_endpoint_promoted": False,
            "cache_influence_excluded": True,
            "endpoint_delta_acquired": True,
            "endpoint_delta_source": "PROCESS_SCOPED_TCP_FLOW_SET",
            "endpoint_delta_source_sha256": network[
                "flow_record_set_sha256"
            ],
            "endpoint_delta_source_verified": True,
            "selected_server_label_sha256": REQUESTED_SERVER_LABEL_SHA256,
            "exact_label_match_verified": True,
            "negative_query_label_sha256": negative_query["label_sha256"],
            "negative_query_result_count": 0,
            "negative_query_ui_binding_verified": True,
        }
    return {
        "credentials_supplied": False,
        "negative_exact_query_completed": False,
        "expected_exact_result_count": 0,
        "exact_selection_completed": False,
        "helper_secret_accessed": False,
        "unsafe_endpoint_promoted": False,
        "cache_influence_excluded": False,
        "endpoint_delta_acquired": False,
        "endpoint_delta_source": "NONE",
        "endpoint_delta_source_sha256": None,
        "endpoint_delta_source_verified": False,
        "selected_server_label_sha256": None,
        "exact_label_match_verified": False,
        "negative_query_label_sha256": None,
        "negative_query_result_count": None,
        "negative_query_ui_binding_verified": False,
    }


def _environment_health(control: str) -> dict[str, bool]:
    direct = control in {"C3", "C4", "C5"}
    identity_control = control in {"C2", "C3", "C4", "C5"}
    return {
        "build_unchanged": True,
        "clock_synchronized": True,
        "firewall_policy_verified": direct,
        "account_available": identity_control,
        "external_outage_excluded": identity_control,
        "baseline_stable": control == "C0",
        "ui_compatible": control == "C1",
    }


def _make_evidence(
    control: str,
    config: dict[str, object],
    manifest: dict[str, object],
    plan: dict[str, object],
    *,
    handoff: dict[str, object] | None = None,
) -> dict[str, object]:
    timeline = _timeline(control)
    context = _run_context(control, timeline, plan)
    network = _network(control)
    discovery = _discovery(control, network, manifest)
    health = _environment_health(control)
    candidate = context["candidate_endpoint"]
    identity = None
    if control in {"C2", "C3", "C5"}:
        identity = compose_identity(
            _identity_probe(plan, control),
            config,
            expected_run_id=RUN_IDS[control],
            investor_provenance_confirmed=True,
            probe_hash_verified=True,
            probe_static_guard_passed=True,
            control_plan_payload=plan,
        )

    phase_timeline_sha256 = contract_digest(
        "PHASE_TIMELINE",
        1,
        {
            "run_id": RUN_IDS[control],
            "control": control,
            "timeline": timeline,
        },
    )
    cohort_key = "C012" if control in {"C0", "C1", "C2"} else control
    job_identity_sha256 = _fixture_digest(cohort_key, "job-identity")
    root_process_sha256 = _fixture_digest(
        cohort_key, "root-process-generation"
    )
    job_manifest_sha256 = _fixture_digest(cohort_key, "job-manifest")
    lifecycle_values = {
        "C0": (
            C012_LIFECYCLE_MODE,
            C012_SESSION_ID,
            "LAUNCH_RETAIN",
            False,
            True,
            True,
            False,
            False,
            None,
            False,
        ),
        "C1": (
            C012_LIFECYCLE_MODE,
            C012_SESSION_ID,
            "REUSE_RETAIN",
            True,
            True,
            True,
            False,
            False,
            None,
            False,
        ),
        "C2": (
            C012_LIFECYCLE_MODE,
            C012_SESSION_ID,
            "REUSE_CONFIG_SUBMIT_TEARDOWN",
            True,
            False,
            False,
            True,
            True,
            _fixture_digest("C2", "transient-process-set"),
            True,
        ),
    }
    (
        lifecycle_mode,
        c012_session_id,
        session_role,
        terminal_alive_at_start,
        terminal_alive_at_end,
        session_retained,
        teardown_completed,
        bootstrap_submitted,
        transient_process_set_sha256,
        transient_same_job,
    ) = lifecycle_values.get(
        control,
        (
            DIRECT_LIFECYCLE_MODE,
            None,
            "INDEPENDENT_LAUNCH_TEARDOWN",
            False,
            False,
            False,
            True,
            False,
            None,
            False,
        ),
    )
    lifecycle_binding = {
        "schema_version": 1,
        "lifecycle_mode": lifecycle_mode,
        "c012_session_id": c012_session_id,
        "session_role": session_role,
        "job_id": JOB_IDS[control],
        "job_manifest_sha256": job_manifest_sha256,
        "job_identity_sha256": job_identity_sha256,
        "root_process_generation_sha256": root_process_sha256,
        "terminal_alive_at_start": terminal_alive_at_start,
        "terminal_alive_at_end": terminal_alive_at_end,
        "session_retained": session_retained,
        "teardown_completed": teardown_completed,
        "bootstrap_submitted_to_existing_session": bootstrap_submitted,
        "transient_process_set_sha256": transient_process_set_sha256,
        "transient_process_same_job_verified": transient_same_job,
    }
    initial_pre_state_binding = copy.deepcopy(
        plan["initial_pre_state_binding"]
    )
    clean_pre_state = {
        "portable_root_new": True,
        "disposable_clone_new": True,
        "windows_user_new": True,
        "accounts_dat_absent": True,
        "servers_dat_absent": True,
        "bases_absent": True,
        "appdata_absent": True,
        "registry_clean": True,
        "credential_manager_empty": True,
        "community_identity_absent": True,
        "no_shared_storage": True,
        "sensitive_bootstrap_absent": True,
        "prior_processes_absent": True,
        "terminal_data_path_matches": True,
    }
    pre_state = (
        None
        if control in {"C1", "C2"}
        else copy.deepcopy(clean_pre_state)
    )
    transition_values = {
        "C0": ("C0_INITIAL", "ABSENT", "ABSENT", None),
        "C1": (
            "C1_DISCOVERY_COMPLETE",
            "CREATED_RECORDED",
            "ABSENT_RECORDED",
            _fixture_digest("C1", "state-transition"),
        ),
        "C2": (
            "C2_LOGIN_COMPLETE",
            "INHERITED_RECORDED",
            "CREATED_RECORDED",
            _fixture_digest("C2", "state-transition"),
        ),
        "C3": ("COLD_BOOT_INITIAL", "ABSENT", "ABSENT", None),
        "C4": ("COLD_BOOT_INITIAL", "ABSENT", "ABSENT", None),
        "C5": ("COLD_BOOT_INITIAL", "ABSENT", "ABSENT", None),
    }
    (
        transition_stage,
        broker_cache_state,
        account_cache_state,
        transition_evidence_sha256,
    ) = transition_values[control]
    state_transition = {
        "schema_version": 1,
        "stage": transition_stage,
        "broker_cache_state": broker_cache_state,
        "account_cache_state": account_cache_state,
        "sensitive_material_exported": False,
        "transition_evidence_sha256": transition_evidence_sha256,
        "transition_verified": True,
    }
    requested_binding = None
    if control in {"C0", "C1", "C2"}:
        requested_binding = evidence_digest(
            {
                "run_id": RUN_IDS[control],
                "control": control,
                "experiment_id": EXPERIMENT_ID,
                "requested_label_manifest_sha256": (
                    requested_label_manifest_digest(manifest)
                ),
                "job_identity_sha256": job_identity_sha256,
                "phase_timeline_sha256": phase_timeline_sha256,
                "requested_server_label_sha256": (
                    REQUESTED_SERVER_LABEL_SHA256
                ),
                "selected_server_label_sha256": discovery[
                    "selected_server_label_sha256"
                ],
            }
        )
    credential_binding = None
    if control in {"C2", "C3", "C4", "C5"}:
        credential_binding = evidence_digest(
            {
                "credential_set_id": CREDENTIAL_SET_ID,
                "account_available": True,
                "credential_bundle_investor_confirmed": True,
            }
        )
    direct = control in {"C3", "C4", "C5"}
    probe_binding = None
    if identity is not None:
        probe_binding = probe_path_binding_digest(
            run_id=RUN_IDS[control],
            job_manifest_sha256=job_manifest_sha256,
            portable_root_path_sha256=context[
                "portable_root_path_sha256"
            ],
            control_plan_sha256=plan["control_plan_sha256"],
            terminal_path_sha256=identity["terminal_path_sha256"],
            terminal_data_path_sha256=identity[
                "terminal_data_path_sha256"
            ],
            identity_probe_output_sha256=identity[
                "identity_probe_output_sha256"
            ],
            probe_generated_at_unix=identity[
                "probe_generated_at_unix"
            ],
        )
    candidate_endpoint_sha256 = (
        evidence_digest(candidate) if candidate is not None else None
    )
    lifecycle_binding_sha256 = lifecycle_binding_digest(
        run_id=RUN_IDS[control],
        control=control,
        control_plan_sha256=plan["control_plan_sha256"],
        lifecycle_binding=lifecycle_binding,
    )
    job_root_binding_sha256 = job_portable_root_binding_digest(
        run_id=RUN_IDS[control],
        job_manifest_sha256=job_manifest_sha256,
        job_identity_sha256=job_identity_sha256,
        root_process_generation_sha256=root_process_sha256,
        portable_root_path_sha256=context[
            "portable_root_path_sha256"
        ],
    )
    pre_state_binding_sha256 = contract_digest(
        "PRE_STATE_BINDING",
        1,
        {
            "run_id": RUN_IDS[control],
            "control": control,
            "initial_pre_state_binding": initial_pre_state_binding,
            "pre_state": pre_state,
        },
    )
    state_transition_sha256 = contract_digest(
        "STATE_TRANSITION",
        1,
        {
            "run_id": RUN_IDS[control],
            "control": control,
            "state_transition": state_transition,
        },
    )
    firewall_root_binding_sha256 = None
    firewall_plan_sha256 = (
        _fixture_digest(control, "firewall-plan") if direct else None
    )
    if direct:
        assert firewall_plan_sha256 is not None
        assert candidate_endpoint_sha256 is not None
        firewall_root_binding_sha256 = (
            firewall_portable_root_binding_digest(
                run_id=RUN_IDS[control],
                control_plan_sha256=plan["control_plan_sha256"],
                firewall_plan_sha256=firewall_plan_sha256,
                portable_root_path_sha256=context[
                    "portable_root_path_sha256"
                ],
                candidate_endpoint_sha256=candidate_endpoint_sha256,
            )
        )
    negative_query_binding_sha256 = None
    if control == "C1":
        negative_query_binding_sha256 = contract_digest(
            "NEGATIVE_QUERY_BINDING",
            1,
            {
                "run_id": RUN_IDS[control],
                "control": control,
                "control_plan_sha256": plan["control_plan_sha256"],
                "phase_timeline_sha256": phase_timeline_sha256,
                "negative_query_label_sha256": discovery[
                    "negative_query_label_sha256"
                ],
                "negative_query_result_count": discovery[
                    "negative_query_result_count"
                ],
                "negative_query_ui_binding_verified": True,
            },
        )
    proof = {
        "schema_version": 5,
        "run_id": RUN_IDS[control],
        "provenance": {
            "origin": "SYNTHETIC_FIXTURE",
            "synthetic_fixture": True,
            "producer": "OFFLINE_TEST_FIXTURE",
            "artifact_set_id": (
                f"10000000-0000-4000-8000-{int(control[1]) + 1:012d}"
            ),
        },
        "job_manifest_sha256": job_manifest_sha256,
        "job_identity_sha256": job_identity_sha256,
        "root_process_generation_sha256": root_process_sha256,
        "phase_timeline_sha256": phase_timeline_sha256,
        "etw_evidence_sha256": _fixture_digest(control, "etw-evidence"),
        "wfp_evidence_sha256": (
            _fixture_digest(control, "wfp-evidence") if direct else None
        ),
        "firewall_plan_sha256": firewall_plan_sha256,
        "candidate_endpoint_sha256": candidate_endpoint_sha256,
        "credential_set_binding_sha256": credential_binding,
        "requested_label_binding_sha256": requested_binding,
        "experiment_manifest_sha256": manifest[
            "experiment_manifest_sha256"
        ],
        "control_plan_sha256": plan["control_plan_sha256"],
        "candidate_handoff_manifest_sha256": (
            handoff["candidate_handoff_manifest_sha256"]
            if handoff is not None
            else None
        ),
        "probe_path_binding_sha256": probe_binding,
        "lifecycle_binding_sha256": lifecycle_binding_sha256,
        "job_portable_root_binding_sha256": job_root_binding_sha256,
        "pre_state_binding_sha256": pre_state_binding_sha256,
        "state_transition_sha256": state_transition_sha256,
        "firewall_portable_root_binding_sha256": (
            firewall_root_binding_sha256
        ),
        "negative_query_binding_sha256": (
            negative_query_binding_sha256
        ),
        "job_process_binding_verified": True,
        "phase_binding_verified": True,
        "etw_proof_capable": True,
        "wfp_proof_capable": direct,
        "firewall_plan_bound": direct,
        "candidate_tuple_bound": candidate is not None,
        "credential_set_binding_verified": credential_binding is not None,
        "requested_label_binding_verified": control in {"C0", "C1", "C2"},
    }
    return {
        "schema_version": 6,
        "run_id": RUN_IDS[control],
        "control": control,
        "run_context": context,
        "lifecycle_binding": lifecycle_binding,
        "initial_pre_state_binding": initial_pre_state_binding,
        "state_transition": state_transition,
        "capture_integrity": {
            "etw_started": True,
            "etw_stopped": True,
            "required_markers_present": True,
            "events_lost": 0,
            "buffers_lost": 0,
        },
        "pre_state": pre_state,
        "identity": identity,
        "credential_bundle_investor_confirmed": (
            control in {"C2", "C3", "C4", "C5"}
        ),
        "network": network,
        "discovery": discovery,
        "environment_health": health,
        "proof_binding": proof,
        "timeline": timeline,
        "timing": derive_timing_from_timeline(
            control,
            timeline,
            context,
        ),
        "phase_markers": [
            event["code"] for event in timeline["events"]  # type: ignore[index]
        ],
    }


def _refresh_probe_path_binding(
    evidence: dict[str, object],
    plan: dict[str, object],
) -> None:
    identity = evidence["identity"]
    proof = evidence["proof_binding"]
    context = evidence["run_context"]
    assert isinstance(identity, dict)
    assert isinstance(proof, dict)
    assert isinstance(context, dict)
    proof["probe_path_binding_sha256"] = probe_path_binding_digest(
        run_id=evidence["run_id"],
        job_manifest_sha256=proof["job_manifest_sha256"],
        portable_root_path_sha256=context[
            "portable_root_path_sha256"
        ],
        control_plan_sha256=plan["control_plan_sha256"],
        terminal_path_sha256=identity["terminal_path_sha256"],
        terminal_data_path_sha256=identity[
            "terminal_data_path_sha256"
        ],
        identity_probe_output_sha256=identity[
            "identity_probe_output_sha256"
        ],
        probe_generated_at_unix=identity["probe_generated_at_unix"],
    )


@dataclass
class CampaignArtifacts:
    config: dict[str, object]
    manifest: dict[str, object]
    plans: dict[str, dict[str, object]]
    evidence: dict[str, dict[str, object]]
    direct_manifest: dict[str, object]
    handoff: dict[str, object]

    def evidence_list(self) -> list[dict[str, object]]:
        return [self.evidence[control] for control in CONTROLS]


def build_artifacts(
    config_payload: dict[str, object] | None = None,
) -> CampaignArtifacts:
    config = validate_config(
        make_config() if config_payload is None else config_payload
    )
    manifest = build_experiment_manifest(config)
    plans = {
        control: build_control_plan(
            config,
            control,
            run_id=RUN_IDS[control],
        )
        for control in ("C0", "C1", "C2")
    }
    evidence = {
        control: _make_evidence(
            control,
            config,
            manifest,
            plans[control],
        )
        for control in ("C0", "C1", "C2")
    }
    direct_manifest = build_direct_campaign_manifest(
        config,
        evidence["C2"],
        plans["C2"],
    )
    handoff = build_candidate_handoff(
        config,
        evidence["C2"],
        plans["C2"],
        direct_campaign_manifest=direct_manifest,
    )
    for control in ("C3", "C4", "C5"):
        plans[control] = build_control_plan(
            config,
            control,
            candidate_handoff=handoff,
            run_id=RUN_IDS[control],
        )
        evidence[control] = _make_evidence(
            control,
            config,
            manifest,
            plans[control],
            handoff=handoff,
        )
    return CampaignArtifacts(
        config=config,
        manifest=manifest,
        plans=plans,
        evidence=evidence,
        direct_manifest=direct_manifest,
        handoff=handoff,
    )


def evaluate_artifacts(
    artifacts: CampaignArtifacts,
    *,
    allow_synthetic: bool = True,
):
    return evaluate_campaign(
        artifacts.evidence_list(),
        config_payload=artifacts.config,
        manifest_payload=artifacts.manifest,
        control_plans_payload=artifacts.plans,
        candidate_handoff_payload=artifacts.handoff,
        direct_campaign_manifest_payload=artifacts.direct_manifest,
        allow_synthetic=allow_synthetic,
    )


def evaluate_control(
    artifacts: CampaignArtifacts,
    control: str,
    *,
    allow_synthetic: bool = True,
):
    return evaluate_evidence(
        artifacts.evidence[control],
        config_payload=artifacts.config,
        manifest_payload=artifacts.manifest,
        control_plan_payload=artifacts.plans[control],
        candidate_handoff_payload=(
            artifacts.handoff
            if control in {"C3", "C4", "C5"}
            else None
        ),
        allow_synthetic=allow_synthetic,
    )


class Patch7ContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.base = build_artifacts()
        base_result = evaluate_artifacts(cls.base)
        if base_result.outcome != "SYNTHETIC_PASS":
            raise AssertionError(
                f"invalid Patch 7 fixture: {base_result.to_dict()}"
            )

    def fresh(self) -> CampaignArtifacts:
        return copy.deepcopy(self.base)

    def assertNotPositive(self, result: object) -> None:
        outcome = getattr(result, "outcome", None)
        self.assertNotIn(outcome, {"PASS", "SYNTHETIC_PASS"})

    def assertAccountLabelRejected(self, label: str) -> None:
        config = make_config()
        config["requested_server_label"] = label
        with self.assertRaises(LabValidationError):
            validate_config(config)

    @staticmethod
    def action_names(plan: dict[str, object]) -> list[str]:
        actions = plan["actions"]
        assert isinstance(actions, list)
        return [
            str(action["action"])
            for action in actions
            if isinstance(action, dict)
        ]

    @staticmethod
    def shift_control_clock(
        evidence: dict[str, object],
        *,
        seconds: int,
    ) -> None:
        context = evidence["run_context"]
        timeline = evidence["timeline"]
        proof = evidence["proof_binding"]
        assert isinstance(context, dict)
        assert isinstance(timeline, dict)
        assert isinstance(proof, dict)
        context["started_at_unix"] += seconds
        context["completed_at_unix"] += seconds
        events = timeline["events"]
        assert isinstance(events, list)
        for event in events:
            assert isinstance(event, dict)
            event["timestamp_unix_ms"] += seconds * 1000
        proof["phase_timeline_sha256"] = contract_digest(
            "PHASE_TIMELINE",
            1,
            {
                "run_id": evidence["run_id"],
                "control": evidence["control"],
                "timeline": timeline,
            },
        )

    # -- Baseline contract and real API construction ---------------------

    def test_patch7_fixture_campaign_is_synthetic_pass(self) -> None:
        result = evaluate_artifacts(self.fresh())
        self.assertEqual(result.outcome, "SYNTHETIC_PASS")
        self.assertEqual(
            result.reasons,
            ("c0_c5_campaign_supports_direct_candidate_reuse",),
        )

    def test_normal_pass_is_unreachable_for_synthetic_fixture(self) -> None:
        result = evaluate_artifacts(self.fresh(), allow_synthetic=False)
        self.assertNotEqual(result.outcome, "PASS")
        self.assertEqual(result.outcome, "INCONCLUSIVE")

    def test_config_v4_is_canonical(self) -> None:
        config = make_config()
        self.assertEqual(validate_config(config), config)
        self.assertEqual(config["schema_version"], 4)
        self.assertEqual(config["policy_version"], POLICY_VERSION)

    def test_all_real_artifact_apis_validate(self) -> None:
        artifacts = self.fresh()
        self.assertEqual(
            validate_experiment_manifest(artifacts.manifest),
            artifacts.manifest,
        )
        for control in CONTROLS:
            self.assertEqual(
                validate_control_plan(
                    artifacts.plans[control],
                    manifest_payload=artifacts.manifest,
                    candidate_handoff=(
                        artifacts.handoff
                        if control in {"C3", "C4", "C5"}
                        else None
                    ),
                ),
                artifacts.plans[control],
            )
            self.assertEqual(
                validate_evidence(artifacts.evidence[control]),
                artifacts.evidence[control],
            )
        self.assertEqual(
            validate_direct_campaign_manifest(
                artifacts.direct_manifest,
                manifest_payload=artifacts.manifest,
            ),
            artifacts.direct_manifest,
        )
        self.assertEqual(
            validate_candidate_handoff(
                artifacts.handoff,
                manifest_payload=artifacts.manifest,
                direct_campaign_manifest=artifacts.direct_manifest,
            ),
            artifacts.handoff,
        )

    def test_early_controls_are_positive_with_complete_bindings(self) -> None:
        artifacts = self.fresh()
        for control in ("C0", "C1", "C2"):
            with self.subTest(control=control):
                self.assertEqual(
                    evaluate_control(artifacts, control).outcome,
                    "SYNTHETIC_PASS",
                )

    def test_direct_standalone_requires_c2_context(self) -> None:
        artifacts = self.fresh()
        for control in ("C3", "C4"):
            with self.subTest(control=control):
                result = evaluate_control(artifacts, control)
                self.assertEqual(result.outcome, "INCONCLUSIVE")
                self.assertIn(
                    "candidate_handoff_c2_context_required",
                    result.reasons,
                )

    def test_c5_requires_campaign_timing_context(self) -> None:
        result = evaluate_control(self.fresh(), "C5")
        self.assertEqual(result.outcome, "INCONCLUSIVE")
        self.assertIn("c5_campaign_timing_context_required", result.reasons)

    def test_control_plans_keep_every_runtime_capability_disabled(self) -> None:
        artifacts = self.fresh()
        expected = {
            "plan_only": True,
            "mt5_start_enabled": False,
            "firewall_apply_enabled": False,
            "credential_access_enabled": False,
            "registry_promotion_enabled": False,
        }
        for control in CONTROLS:
            with self.subTest(control=control):
                self.assertEqual(artifacts.plans[control]["safety"], expected)

    # -- Patch 7 C0 -> C1 -> C2 lifecycle contract ----------------------

    def test_c0_retains_c012_session_for_c1(self) -> None:
        artifacts = self.fresh()
        plan = artifacts.plans["C0"]
        lifecycle = plan["lifecycle_control"]
        observed = artifacts.evidence["C0"]["lifecycle_binding"]
        actions = self.action_names(plan)
        self.assertIn("start_terminal_without_identity", actions)
        self.assertIn("retain_c012_session", actions)
        self.assertNotIn("close_job_object", actions)
        self.assertEqual(lifecycle["expected_exit_state"], "RETAINED")
        self.assertTrue(observed["terminal_alive_at_end"])
        self.assertTrue(observed["session_retained"])

    def test_c1_requires_existing_c012_process(self) -> None:
        artifacts = self.fresh()
        plan = artifacts.plans["C1"]
        lifecycle = plan["lifecycle_control"]
        observed = artifacts.evidence["C1"]["lifecycle_binding"]
        actions = self.action_names(plan)
        self.assertIn("assert_existing_c012_root_process", actions)
        self.assertIn(
            "assert_same_c012_job_and_process_generation",
            actions,
        )
        self.assertNotIn("assert_terminal_not_running", actions)
        self.assertEqual(lifecycle["expected_entry_state"], "RUNNING")
        self.assertTrue(observed["terminal_alive_at_start"])

    def test_c1_does_not_teardown_c012(self) -> None:
        artifacts = self.fresh()
        actions = self.action_names(artifacts.plans["C1"])
        observed = artifacts.evidence["C1"]["lifecycle_binding"]
        self.assertIn("retain_c012_session", actions)
        self.assertNotIn("close_job_object", actions)
        self.assertNotIn("destroy_disposable_clone_after_export", actions)
        self.assertTrue(observed["terminal_alive_at_end"])
        self.assertFalse(observed["teardown_completed"])

    def test_c2_explicitly_submits_bootstrap_to_existing_session(self) -> None:
        artifacts = self.fresh()
        plan = artifacts.plans["C2"]
        observed = artifacts.evidence["C2"]["lifecycle_binding"]
        actions = self.action_names(plan)
        self.assertIn(
            "submit_login_bootstrap_to_existing_terminal",
            actions,
        )
        self.assertIn("verify_transient_submitter_same_job", actions)
        self.assertNotIn("start_terminal_without_identity", actions)
        self.assertTrue(
            observed["bootstrap_submitted_to_existing_session"]
        )
        self.assertTrue(observed["transient_process_same_job_verified"])
        self.assertIsNotNone(observed["transient_process_set_sha256"])
        action_objects = plan["actions"]
        login_start_index = next(
            index
            for index, action in enumerate(action_objects)
            if action.get("action") == "mark_phase"
            and action.get("marker") == "C2_LOGIN_START"
        )
        submit_index = next(
            index
            for index, action in enumerate(action_objects)
            if action.get("action")
            == "submit_login_bootstrap_to_existing_terminal"
        )
        self.assertLess(login_start_index, submit_index)

    def test_c2_is_only_c012_teardown_control(self) -> None:
        artifacts = self.fresh()
        for control in ("C0", "C1", "C2"):
            with self.subTest(control=control):
                actions = self.action_names(artifacts.plans[control])
                observed = artifacts.evidence[control][
                    "lifecycle_binding"
                ]
                if control == "C2":
                    self.assertIn("close_job_object", actions)
                    self.assertIn(
                        "destroy_disposable_clone_after_export",
                        actions,
                    )
                    self.assertTrue(observed["teardown_completed"])
                else:
                    self.assertNotIn("close_job_object", actions)
                    self.assertNotIn(
                        "destroy_disposable_clone_after_export",
                        actions,
                    )
                    self.assertFalse(observed["teardown_completed"])

    def test_c012_lifecycle_plan_is_executable_and_consistent(self) -> None:
        artifacts = self.fresh()
        plans = [artifacts.plans[key] for key in ("C0", "C1", "C2")]
        evidence = [
            artifacts.evidence[key] for key in ("C0", "C1", "C2")
        ]
        self.assertEqual(
            {
                plan["lifecycle_control"]["c012_session_id"]
                for plan in plans
            },
            {C012_SESSION_ID},
        )
        self.assertEqual(
            {
                item["lifecycle_binding"]["job_id"]
                for item in evidence
            },
            {JOB_IDS["C0"]},
        )
        self.assertEqual(
            {
                item["lifecycle_binding"][
                    "root_process_generation_sha256"
                ]
                for item in evidence
            },
            {
                artifacts.evidence["C0"]["lifecycle_binding"][
                    "root_process_generation_sha256"
                ]
            },
        )
        self.assertTrue(
            artifacts.evidence["C0"]["lifecycle_binding"][
                "terminal_alive_at_end"
            ]
        )
        self.assertTrue(
            artifacts.evidence["C1"]["lifecycle_binding"][
                "terminal_alive_at_start"
            ]
        )
        self.assertTrue(
            artifacts.evidence["C1"]["lifecycle_binding"][
                "terminal_alive_at_end"
            ]
        )
        self.assertTrue(
            artifacts.evidence["C2"]["lifecycle_binding"][
                "terminal_alive_at_start"
            ]
        )
        self.assertFalse(
            artifacts.evidence["C2"]["lifecycle_binding"][
                "terminal_alive_at_end"
            ]
        )
        self.assertEqual(
            evaluate_artifacts(artifacts).outcome,
            "SYNTHETIC_PASS",
        )

    def test_terminal_command_matches_lifecycle_role(self) -> None:
        artifacts = self.fresh()
        c0_paths = artifacts.plans["C0"]["paths"]
        self.assertEqual(
            artifacts.plans["C0"]["terminal_command"],
            [c0_paths["terminal"], "/portable"],
        )
        self.assertIsNone(artifacts.plans["C1"]["terminal_command"])
        for control in ("C2", "C3", "C4", "C5"):
            with self.subTest(control=control):
                plan = artifacts.plans[control]
                paths = plan["paths"]
                self.assertEqual(
                    plan["terminal_command"],
                    [
                        paths["terminal"],
                        "/portable",
                        f"/config:{paths['private_config']}",
                    ],
                )

    def test_early_plans_cannot_receive_candidate_or_handoff(self) -> None:
        artifacts = self.fresh()
        for control in ("C0", "C1", "C2"):
            with self.subTest(control=control):
                with self.assertRaises(LabValidationError):
                    build_control_plan(
                        artifacts.config,
                        control,
                        candidate_endpoint=CANDIDATE,
                        run_id=RUN_IDS[control],
                    )

    def test_direct_plans_require_authoritative_handoff(self) -> None:
        artifacts = self.fresh()
        for control in ("C3", "C4", "C5"):
            with self.subTest(control=control):
                with self.assertRaises(LabValidationError):
                    build_control_plan(
                        artifacts.config,
                        control,
                        candidate_endpoint=CANDIDATE,
                        run_id=RUN_IDS[control],
                    )

    # -- Network accounting and delta provenance ------------------------

    def test_direct_controls_reject_unclassified_process_flows(self) -> None:
        artifacts = self.fresh()
        for control in ("C3", "C5"):
            with self.subTest(control=control):
                evidence = copy.deepcopy(artifacts.evidence[control])
                # Correlated G1 mutation: total changes while both exclusive
                # classified counters remain unchanged.
                evidence["network"]["process_scoped_tcp_flows"] += 9
                with self.assertRaisesRegex(
                    LabValidationError,
                    r"total=candidate\+other",
                ):
                    validate_evidence(evidence)

    def test_c4_rejects_unclassified_process_flows(self) -> None:
        artifacts = self.fresh()
        evidence = copy.deepcopy(artifacts.evidence["C4"])
        evidence["network"]["process_scoped_tcp_flows"] += 9
        with self.assertRaisesRegex(
            LabValidationError,
            r"total=candidate\+other",
        ):
            validate_evidence(evidence)

    def test_c1_delta_requires_observed_source(self) -> None:
        artifacts = self.fresh()
        evidence = copy.deepcopy(artifacts.evidence["C1"])
        evidence["discovery"].update(
            {
                "endpoint_delta_source": "NONE",
                "endpoint_delta_source_sha256": None,
                "endpoint_delta_source_verified": False,
            }
        )
        with self.assertRaisesRegex(
            LabValidationError,
            "endpoint delta requires an observed, verified source",
        ):
            validate_evidence(evidence)

    def test_c1_ambiguous_attribution_is_inconclusive(self) -> None:
        artifacts = self.fresh()
        artifacts.evidence["C1"]["network"][
            "attribution_unambiguous"
        ] = False
        result = evaluate_control(artifacts, "C1")
        self.assertEqual(result.outcome, "INCONCLUSIVE")
        self.assertIn(
            "discovery_flow_attribution_ambiguous",
            result.reasons,
        )

    def test_c1_negative_query_is_committed(self) -> None:
        artifacts = self.fresh()
        manifest_query = artifacts.manifest["negative_query"]
        plan_query = artifacts.plans["C1"]["negative_query"]
        discovery = artifacts.evidence["C1"]["discovery"]
        proof = artifacts.evidence["C1"]["proof_binding"]
        self.assertEqual(plan_query, manifest_query)
        self.assertEqual(
            discovery["negative_query_label_sha256"],
            manifest_query["label_sha256"],
        )
        self.assertEqual(discovery["negative_query_result_count"], 0)
        self.assertTrue(
            discovery["negative_query_ui_binding_verified"]
        )
        self.assertIsNotNone(proof["negative_query_binding_sha256"])
        discovery["negative_query_ui_binding_verified"] = False
        result = evaluate_control(artifacts, "C1")
        self.assertEqual(result.outcome, "INCONCLUSIVE")
        self.assertIn("negative_query_binding_not_proven", result.reasons)

    def test_c1_negative_query_must_differ_from_requested_label(self) -> None:
        config = make_config()
        manifest = build_experiment_manifest(config)
        negative_query = manifest["negative_query"]
        self.assertNotEqual(
            negative_query["label"],
            config["requested_server_label"],
        )
        config["requested_server_label"] = negative_query["label"]
        with self.assertRaises(LabValidationError):
            validate_config(config)

    def test_c1_negative_query_nonzero_result_is_failure(self) -> None:
        artifacts = self.fresh()
        evidence = artifacts.evidence["C1"]
        evidence["discovery"]["negative_query_result_count"] = 1
        evidence["proof_binding"][
            "negative_query_binding_sha256"
        ] = contract_digest(
            "NEGATIVE_QUERY_BINDING",
            1,
            {
                "run_id": evidence["run_id"],
                "control": "C1",
                "control_plan_sha256": artifacts.plans["C1"][
                    "control_plan_sha256"
                ],
                "phase_timeline_sha256": evidence["proof_binding"][
                    "phase_timeline_sha256"
                ],
                "negative_query_label_sha256": evidence["discovery"][
                    "negative_query_label_sha256"
                ],
                "negative_query_result_count": 1,
                "negative_query_ui_binding_verified": True,
            },
        )
        result = evaluate_control(artifacts, "C1")
        self.assertEqual(result.outcome, "FAIL")
        self.assertIn("negative_query_returned_results", result.reasons)

    def test_direct_other_tcp_flow_is_falsification(self) -> None:
        artifacts = self.fresh()
        for control in ("C3", "C5"):
            with self.subTest(control=control):
                evidence = artifacts.evidence[control]
                evidence["network"]["other_tcp_flows"] = 1
                evidence["network"]["process_scoped_tcp_flows"] = 2
                result = evaluate_artifacts(artifacts)
                self.assertEqual(result.outcome, "FAIL")
                artifacts = self.fresh()

    def test_c4_other_tcp_flow_is_falsification(self) -> None:
        artifacts = self.fresh()
        evidence = artifacts.evidence["C4"]
        evidence["network"]["other_tcp_flows"] = 1
        evidence["network"]["process_scoped_tcp_flows"] = 2
        result = evaluate_artifacts(artifacts)
        self.assertEqual(result.outcome, "FAIL")

    def test_direct_dns_event_is_falsification(self) -> None:
        artifacts = self.fresh()
        artifacts.evidence["C3"]["network"]["dns_events"] = 1
        self.assertEqual(evaluate_artifacts(artifacts).outcome, "FAIL")

    def test_direct_non_tcp_event_is_falsification(self) -> None:
        artifacts = self.fresh()
        artifacts.evidence["C5"]["network"]["non_tcp_network_events"] = 1
        self.assertEqual(evaluate_artifacts(artifacts).outcome, "FAIL")

    def test_c1_delta_digest_must_match_flow_record_set(self) -> None:
        artifacts = self.fresh()
        evidence = artifacts.evidence["C1"]
        evidence["discovery"]["endpoint_delta_source_sha256"] = "e" * 64
        with self.assertRaisesRegex(
            LabValidationError,
            "endpoint delta requires an observed, verified source",
        ):
            validate_evidence(evidence)

    def test_candidate_attempt_is_derived_from_candidate_flow_count(self) -> None:
        evidence = copy.deepcopy(self.base.evidence["C3"])
        evidence["network"]["candidate_attempt_observed"] = False
        with self.assertRaisesRegex(
            LabValidationError,
            "must derive from candidate_tcp_flows",
        ):
            validate_evidence(evidence)

    def test_candidate_dispositions_are_mutually_exclusive(self) -> None:
        evidence = copy.deepcopy(self.base.evidence["C4"])
        evidence["network"]["candidate_connected"] = True
        with self.assertRaisesRegex(
            LabValidationError,
            "mutually exclusive",
        ):
            validate_evidence(evidence)

    def test_verified_flow_set_requires_digest(self) -> None:
        evidence = copy.deepcopy(self.base.evidence["C3"])
        evidence["network"]["flow_record_set_sha256"] = None
        with self.assertRaisesRegex(
            LabValidationError,
            "verified flow record set requires a digest",
        ):
            validate_evidence(evidence)

    # -- Timeline and duration contract ---------------------------------

    def test_phase_markers_reversed_cannot_pass(self) -> None:
        artifacts = self.fresh()
        for control in CONTROLS:
            with self.subTest(control=control):
                evidence = copy.deepcopy(artifacts.evidence[control])
                evidence["phase_markers"].reverse()
                evidence["timeline"]["events"].reverse()
                with self.assertRaises(LabValidationError):
                    validate_evidence(evidence)

    def test_phase_markers_interleaved_cannot_pass(self) -> None:
        evidence = copy.deepcopy(self.base.evidence["C1"])
        events = evidence["timeline"]["events"]
        # negative START -> exact START -> negative END -> exact END
        interleaved = [events[0], events[2], events[1], events[3]]
        for sequence, event in enumerate(interleaved, start=1):
            event["sequence"] = sequence
        evidence["timeline"]["events"] = interleaved
        evidence["phase_markers"] = [
            event["code"] for event in interleaved
        ]
        with self.assertRaisesRegex(
            LabValidationError,
            "phase code order",
        ):
            validate_evidence(evidence)

    def test_timing_must_equal_timeline_derivation(self) -> None:
        evidence = copy.deepcopy(self.base.evidence["C2"])
        evidence["timing"]["connected_steady_seconds"] += 1
        with self.assertRaisesRegex(
            LabValidationError,
            "timing assertions do not match",
        ):
            validate_evidence(evidence)

    def test_timeline_sequence_must_be_contiguous(self) -> None:
        evidence = copy.deepcopy(self.base.evidence["C2"])
        evidence["timeline"]["events"][2]["sequence"] = 99
        with self.assertRaisesRegex(
            LabValidationError,
            "sequence must be contiguous",
        ):
            validate_evidence(evidence)

    def test_timeline_qpc_must_be_strictly_increasing(self) -> None:
        evidence = copy.deepcopy(self.base.evidence["C2"])
        events = evidence["timeline"]["events"]
        events[1]["qpc"] = events[0]["qpc"]
        with self.assertRaisesRegex(
            LabValidationError,
            "QPC values are not strictly increasing",
        ):
            validate_evidence(evidence)

    def test_c012_timeline_boundaries_are_ordered_and_frequency_shared(self) -> None:
        artifacts = self.fresh()
        timelines = {
            control: artifacts.evidence[control]["timeline"]
            for control in ("C0", "C1", "C2")
        }
        self.assertEqual(
            {timeline["qpc_frequency_hz"] for timeline in timelines.values()},
            {1000},
        )
        for earlier, later in (("C0", "C1"), ("C1", "C2")):
            earlier_last = timelines[earlier]["events"][-1]
            later_first = timelines[later]["events"][0]
            self.assertLessEqual(
                earlier_last["timestamp_unix_ms"],
                later_first["timestamp_unix_ms"],
            )
            self.assertLess(earlier_last["qpc"], later_first["qpc"])
        self.assertEqual(evaluate_artifacts(artifacts).outcome, "SYNTHETIC_PASS")

    def test_c012_timeline_one_ms_overlap_is_rejected(self) -> None:
        artifacts = self.fresh()
        c0_events = artifacts.evidence["C0"]["timeline"]["events"]
        c1 = artifacts.evidence["C1"]
        c1_events = c1["timeline"]["events"]
        shift_ms = (
            int(c0_events[-1]["timestamp_unix_ms"])
            - int(c1_events[0]["timestamp_unix_ms"])
            - 1
        )
        for event in c1_events:
            event["timestamp_unix_ms"] += shift_ms
        c1["run_context"]["started_at_unix"] = (
            int(c1_events[0]["timestamp_unix_ms"]) // 1000
        )
        c1["run_context"]["completed_at_unix"] = (
            int(c1_events[-1]["timestamp_unix_ms"]) // 1000
        )
        c1["proof_binding"]["phase_timeline_sha256"] = contract_digest(
            "PHASE_TIMELINE",
            1,
            {
                "run_id": c1["run_id"],
                "control": "C1",
                "timeline": c1["timeline"],
            },
        )
        c1["proof_binding"]["requested_label_binding_sha256"] = evidence_digest(
            {
                "run_id": c1["run_id"],
                "control": "C1",
                "experiment_id": EXPERIMENT_ID,
                "requested_label_manifest_sha256": requested_label_manifest_digest(
                    artifacts.manifest
                ),
                "job_identity_sha256": c1["proof_binding"][
                    "job_identity_sha256"
                ],
                "phase_timeline_sha256": c1["proof_binding"][
                    "phase_timeline_sha256"
                ],
                "requested_server_label_sha256": REQUESTED_SERVER_LABEL_SHA256,
                "selected_server_label_sha256": REQUESTED_SERVER_LABEL_SHA256,
            }
        )
        c1["proof_binding"]["negative_query_binding_sha256"] = contract_digest(
            "NEGATIVE_QUERY_BINDING",
            1,
            {
                "run_id": c1["run_id"],
                "control": "C1",
                "control_plan_sha256": artifacts.plans["C1"][
                    "control_plan_sha256"
                ],
                "phase_timeline_sha256": c1["proof_binding"][
                    "phase_timeline_sha256"
                ],
                "negative_query_label_sha256": c1["discovery"][
                    "negative_query_label_sha256"
                ],
                "negative_query_result_count": c1["discovery"][
                    "negative_query_result_count"
                ],
                "negative_query_ui_binding_verified": True,
            },
        )
        result = evaluate_artifacts(artifacts)
        self.assertEqual(result.outcome, "INCONCLUSIVE")
        self.assertIn("c012_timeline_timestamp_order_invalid", result.reasons)

    def test_c012_timeline_qpc_reset_at_control_boundary_is_rejected(self) -> None:
        artifacts = self.fresh()
        c0_events = artifacts.evidence["C0"]["timeline"]["events"]
        c1 = artifacts.evidence["C1"]
        c1_events = c1["timeline"]["events"]
        qpc_shift = int(c0_events[-1]["qpc"]) - int(c1_events[0]["qpc"])
        for event in c1_events:
            event["qpc"] += qpc_shift
        c1["proof_binding"]["phase_timeline_sha256"] = contract_digest(
            "PHASE_TIMELINE",
            1,
            {
                "run_id": c1["run_id"],
                "control": "C1",
                "timeline": c1["timeline"],
            },
        )
        c1["proof_binding"]["requested_label_binding_sha256"] = evidence_digest(
            {
                "run_id": c1["run_id"],
                "control": "C1",
                "experiment_id": EXPERIMENT_ID,
                "requested_label_manifest_sha256": requested_label_manifest_digest(
                    artifacts.manifest
                ),
                "job_identity_sha256": c1["proof_binding"][
                    "job_identity_sha256"
                ],
                "phase_timeline_sha256": c1["proof_binding"][
                    "phase_timeline_sha256"
                ],
                "requested_server_label_sha256": REQUESTED_SERVER_LABEL_SHA256,
                "selected_server_label_sha256": REQUESTED_SERVER_LABEL_SHA256,
            }
        )
        c1["proof_binding"]["negative_query_binding_sha256"] = contract_digest(
            "NEGATIVE_QUERY_BINDING",
            1,
            {
                "run_id": c1["run_id"],
                "control": "C1",
                "control_plan_sha256": artifacts.plans["C1"][
                    "control_plan_sha256"
                ],
                "phase_timeline_sha256": c1["proof_binding"][
                    "phase_timeline_sha256"
                ],
                "negative_query_label_sha256": c1["discovery"][
                    "negative_query_label_sha256"
                ],
                "negative_query_result_count": c1["discovery"][
                    "negative_query_result_count"
                ],
                "negative_query_ui_binding_verified": True,
            },
        )
        result = evaluate_artifacts(artifacts)
        self.assertEqual(result.outcome, "INCONCLUSIVE")
        self.assertIn("c012_timeline_qpc_order_invalid", result.reasons)

    def test_c012_timeline_one_ms_overlap_c1_c2_is_rejected(self) -> None:
        artifacts = self.fresh()
        c1 = artifacts.evidence["C1"]
        c1_events = c1["timeline"]["events"]
        c2_events = artifacts.evidence["C2"]["timeline"]["events"]
        shift_ms = (
            int(c2_events[0]["timestamp_unix_ms"])
            - int(c1_events[-1]["timestamp_unix_ms"])
            + 1
        )
        for event in c1_events:
            event["timestamp_unix_ms"] += shift_ms
        c1["run_context"]["started_at_unix"] = (
            int(c1_events[0]["timestamp_unix_ms"]) // 1000
        )
        c1["run_context"]["completed_at_unix"] = (
            int(c1_events[-1]["timestamp_unix_ms"]) // 1000
        )
        c1["proof_binding"]["phase_timeline_sha256"] = contract_digest(
            "PHASE_TIMELINE",
            1,
            {
                "run_id": c1["run_id"],
                "control": "C1",
                "timeline": c1["timeline"],
            },
        )
        c1["proof_binding"]["requested_label_binding_sha256"] = evidence_digest(
            {
                "run_id": c1["run_id"],
                "control": "C1",
                "experiment_id": EXPERIMENT_ID,
                "requested_label_manifest_sha256": requested_label_manifest_digest(
                    artifacts.manifest
                ),
                "job_identity_sha256": c1["proof_binding"][
                    "job_identity_sha256"
                ],
                "phase_timeline_sha256": c1["proof_binding"][
                    "phase_timeline_sha256"
                ],
                "requested_server_label_sha256": REQUESTED_SERVER_LABEL_SHA256,
                "selected_server_label_sha256": c1["discovery"][
                    "selected_server_label_sha256"
                ],
            }
        )
        c1["proof_binding"]["negative_query_binding_sha256"] = contract_digest(
            "NEGATIVE_QUERY_BINDING",
            1,
            {
                "run_id": c1["run_id"],
                "control": "C1",
                "control_plan_sha256": artifacts.plans["C1"][
                    "control_plan_sha256"
                ],
                "phase_timeline_sha256": c1["proof_binding"][
                    "phase_timeline_sha256"
                ],
                "negative_query_label_sha256": c1["discovery"][
                    "negative_query_label_sha256"
                ],
                "negative_query_result_count": c1["discovery"][
                    "negative_query_result_count"
                ],
                "negative_query_ui_binding_verified": True,
            },
        )
        result = evaluate_artifacts(artifacts)
        self.assertEqual(result.outcome, "INCONCLUSIVE")
        self.assertIn("c012_timeline_timestamp_order_invalid", result.reasons)

    def test_c012_timeline_qpc_at_or_below_c1_is_rejected_for_c2(self) -> None:
        artifacts = self.fresh()
        c1 = artifacts.evidence["C1"]
        c1_events = c1["timeline"]["events"]
        c2_events = artifacts.evidence["C2"]["timeline"]["events"]
        qpc_shift = int(c2_events[0]["qpc"]) - int(c1_events[-1]["qpc"])
        for event in c1_events:
            event["qpc"] += qpc_shift
        c1["proof_binding"]["phase_timeline_sha256"] = contract_digest(
            "PHASE_TIMELINE",
            1,
            {
                "run_id": c1["run_id"],
                "control": "C1",
                "timeline": c1["timeline"],
            },
        )
        c1["proof_binding"]["requested_label_binding_sha256"] = evidence_digest(
            {
                "run_id": c1["run_id"],
                "control": "C1",
                "experiment_id": EXPERIMENT_ID,
                "requested_label_manifest_sha256": requested_label_manifest_digest(
                    artifacts.manifest
                ),
                "job_identity_sha256": c1["proof_binding"][
                    "job_identity_sha256"
                ],
                "phase_timeline_sha256": c1["proof_binding"][
                    "phase_timeline_sha256"
                ],
                "requested_server_label_sha256": REQUESTED_SERVER_LABEL_SHA256,
                "selected_server_label_sha256": c1["discovery"][
                    "selected_server_label_sha256"
                ],
            }
        )
        c1["proof_binding"]["negative_query_binding_sha256"] = contract_digest(
            "NEGATIVE_QUERY_BINDING",
            1,
            {
                "run_id": c1["run_id"],
                "control": "C1",
                "control_plan_sha256": artifacts.plans["C1"][
                    "control_plan_sha256"
                ],
                "phase_timeline_sha256": c1["proof_binding"][
                    "phase_timeline_sha256"
                ],
                "negative_query_label_sha256": c1["discovery"][
                    "negative_query_label_sha256"
                ],
                "negative_query_result_count": c1["discovery"][
                    "negative_query_result_count"
                ],
                "negative_query_ui_binding_verified": True,
            },
        )
        result = evaluate_artifacts(artifacts)
        self.assertEqual(result.outcome, "INCONCLUSIVE")
        self.assertIn("c012_timeline_qpc_order_invalid", result.reasons)

    def test_c012_timeline_frequency_mismatch_is_rejected(self) -> None:
        artifacts = self.fresh()
        c1 = artifacts.evidence["C1"]
        c1["timeline"]["qpc_frequency_hz"] = 2000
        for event in c1["timeline"]["events"]:
            event["qpc"] *= 2
        c1["proof_binding"]["phase_timeline_sha256"] = contract_digest(
            "PHASE_TIMELINE",
            1,
            {
                "run_id": c1["run_id"],
                "control": "C1",
                "timeline": c1["timeline"],
            },
        )
        c1["proof_binding"]["requested_label_binding_sha256"] = evidence_digest(
            {
                "run_id": c1["run_id"],
                "control": "C1",
                "experiment_id": EXPERIMENT_ID,
                "requested_label_manifest_sha256": requested_label_manifest_digest(
                    artifacts.manifest
                ),
                "job_identity_sha256": c1["proof_binding"][
                    "job_identity_sha256"
                ],
                "phase_timeline_sha256": c1["proof_binding"][
                    "phase_timeline_sha256"
                ],
                "requested_server_label_sha256": REQUESTED_SERVER_LABEL_SHA256,
                "selected_server_label_sha256": c1["discovery"][
                    "selected_server_label_sha256"
                ],
            }
        )
        c1["proof_binding"]["negative_query_binding_sha256"] = contract_digest(
            "NEGATIVE_QUERY_BINDING",
            1,
            {
                "run_id": c1["run_id"],
                "control": "C1",
                "control_plan_sha256": artifacts.plans["C1"][
                    "control_plan_sha256"
                ],
                "phase_timeline_sha256": c1["proof_binding"][
                    "phase_timeline_sha256"
                ],
                "negative_query_label_sha256": c1["discovery"][
                    "negative_query_label_sha256"
                ],
                "negative_query_result_count": c1["discovery"][
                    "negative_query_result_count"
                ],
                "negative_query_ui_binding_verified": True,
            },
        )
        result = evaluate_artifacts(artifacts)
        self.assertEqual(result.outcome, "INCONCLUSIVE")
        self.assertIn("c012_timeline_frequency_mismatch", result.reasons)

    def test_timeline_event_must_be_inside_run_interval(self) -> None:
        evidence = copy.deepcopy(self.base.evidence["C0"])
        evidence["timeline"]["events"][0]["timestamp_unix_ms"] -= 1
        with self.assertRaisesRegex(
            LabValidationError,
            "outside the declared run interval",
        ):
            validate_evidence(evidence)

    def test_timeline_digest_is_run_and_control_bound(self) -> None:
        evidence = copy.deepcopy(self.base.evidence["C0"])
        evidence["proof_binding"]["phase_timeline_sha256"] = "e" * 64
        with self.assertRaisesRegex(
            LabValidationError,
            "phase timeline digest mismatch",
        ):
            validate_evidence(evidence)

    def test_config_duration_minimum_is_enforced(self) -> None:
        config = make_config()
        config["durations_seconds"]["baseline"] = 1200
        config["durations_seconds"]["c5_separation_minimum"] = 3600
        artifacts = build_artifacts(config)
        result = evaluate_artifacts(artifacts)
        self.assertNotPositive(result)

    def test_config_duration_bounds_are_validated(self) -> None:
        cases = {
            "baseline": 599,
            "negative_discovery": 119,
            "exact_discovery": 179,
            "login_timeout": 121,
            "connected_steady": 599,
            "network_interruption": 29,
            "reconnect_observation": 299,
            "blocked_timeout": 181,
            "c4_elapsed_tolerance": 31,
            "c5_separation_minimum": 1799,
            "probe_timestamp_tolerance_seconds": 6,
        }
        for key, invalid in cases.items():
            with self.subTest(key=key):
                config = make_config()
                config["durations_seconds"][key] = invalid
                with self.assertRaises(LabValidationError):
                    validate_config(config)

    def test_c4_elapsed_over_maximum_is_not_pass(self) -> None:
        artifacts = self.fresh()
        evidence = artifacts.evidence["C4"]
        evidence["run_context"]["completed_at_unix"] += 3600
        result = evaluate_control(artifacts, "C4")
        self.assertNotPositive(result)
        self.assertIn("negative_control_window_invalid", result.reasons)

    def test_c5_separation_is_derived_from_utc_markers(self) -> None:
        artifacts = self.fresh()
        evidence = artifacts.evidence["C5"]
        shift_ms = 1_000
        for event in evidence["timeline"]["events"]:
            event["timestamp_unix_ms"] -= shift_ms
        evidence["run_context"]["started_at_unix"] -= 1
        evidence["run_context"]["completed_at_unix"] -= 1
        evidence["proof_binding"]["phase_timeline_sha256"] = contract_digest(
            "PHASE_TIMELINE",
            1,
            {
                "run_id": evidence["run_id"],
                "control": "C5",
                "timeline": evidence["timeline"],
            },
        )
        result = evaluate_artifacts(artifacts)
        self.assertNotPositive(result)

    def test_c4_cannot_overlap_c3(self) -> None:
        artifacts = self.fresh()
        # C3 ends at ~10,721; move C4 from 11,000 to 10,700.
        self.shift_control_clock(
            artifacts.evidence["C4"],
            seconds=-300,
        )
        result = evaluate_artifacts(artifacts)
        self.assertEqual(result.outcome, "INCONCLUSIVE")
        self.assertIn(
            "direct_controls_temporal_order_invalid",
            result.reasons,
        )

    def test_c5_cannot_start_before_c4_completion(self) -> None:
        artifacts = self.fresh()
        # C4 ends at 11,180; move C5 from 12,521 to 11,100.
        self.shift_control_clock(
            artifacts.evidence["C5"],
            seconds=-1421,
        )
        artifacts.evidence["C5"]["identity"][
            "probe_generated_at_unix"
        ] -= 1421
        result = evaluate_artifacts(artifacts)
        self.assertEqual(result.outcome, "INCONCLUSIVE")
        self.assertIn(
            "direct_controls_temporal_order_invalid",
            result.reasons,
        )

    # -- Manifest, plans, direct manifest, and handoff ------------------

    def test_terminal_hash_must_match_manifest(self) -> None:
        artifacts = self.fresh()
        # Correlated G3 mutation: all evidence agree with each other while the
        # authoritative manifest and handoff remain unchanged.
        for control in CONTROLS:
            artifacts.evidence[control]["run_context"][
                "terminal_sha256"
            ] = "e" * 64
        result = evaluate_artifacts(artifacts)
        self.assertEqual(result.outcome, "FAIL")

    def test_candidate_handoff_binds_c2_to_direct_controls(self) -> None:
        artifacts = self.fresh()
        evidence = artifacts.evidence["C3"]
        evidence["proof_binding"][
            "candidate_handoff_manifest_sha256"
        ] = "e" * 64
        result = evaluate_control(artifacts, "C3")
        self.assertEqual(result.outcome, "FAIL")
        self.assertIn("candidate_handoff_digest_mismatch", result.reasons)

    def test_control_plan_digest_mismatch_is_not_pass(self) -> None:
        artifacts = self.fresh()
        artifacts.evidence["C2"]["proof_binding"][
            "control_plan_sha256"
        ] = "e" * 64
        result = evaluate_control(artifacts, "C2")
        self.assertNotPositive(result)
        self.assertIn("control_plan_digest_mismatch", result.reasons)

    def test_manifest_body_tamper_breaks_digest(self) -> None:
        manifest = copy.deepcopy(self.base.manifest)
        manifest["terminal"]["publisher"] = "Different Publisher"
        with self.assertRaisesRegex(
            LabValidationError,
            "manifest digest mismatch",
        ):
            validate_experiment_manifest(manifest)

    def test_control_plan_body_tamper_breaks_digest(self) -> None:
        plan = copy.deepcopy(self.base.plans["C0"])
        plan["duration_contract"]["baseline"] = 1200
        with self.assertRaisesRegex(
            LabValidationError,
            r"(control plan durations differ|control plan digest mismatch)",
        ):
            validate_control_plan(
                plan,
                manifest_payload=self.base.manifest,
            )

    def test_direct_manifest_body_tamper_breaks_digest(self) -> None:
        direct = copy.deepcopy(self.base.direct_manifest)
        direct["terminal"]["build"] = 9999
        with self.assertRaises(LabValidationError):
            validate_direct_campaign_manifest(
                direct,
                manifest_payload=self.base.manifest,
            )

    def test_handoff_body_tamper_breaks_digest(self) -> None:
        handoff = copy.deepcopy(self.base.handoff)
        handoff["terminal_build"] = 9999
        with self.assertRaises(LabValidationError):
            validate_candidate_handoff(
                handoff,
                manifest_payload=self.base.manifest,
                direct_campaign_manifest=self.base.direct_manifest,
            )

    def test_manifest_and_config_changed_after_plan_generation_are_rejected(
        self,
    ) -> None:
        artifacts = self.fresh()
        changed_config = copy.deepcopy(artifacts.config)
        changed_config["terminal"]["publisher"] = "Replacement Publisher"
        changed_manifest = build_experiment_manifest(changed_config)
        # Correlated mutation: config and manifest are internally consistent,
        # but every existing plan remains committed to the original manifest.
        with self.assertRaises(LabValidationError):
            evaluate_campaign(
                artifacts.evidence_list(),
                config_payload=changed_config,
                manifest_payload=changed_manifest,
                control_plans_payload=artifacts.plans,
                candidate_handoff_payload=artifacts.handoff,
                direct_campaign_manifest_payload=artifacts.direct_manifest,
                allow_synthetic=True,
            )

    def test_missing_manifest_cannot_pass(self) -> None:
        artifacts = self.fresh()
        result = evaluate_campaign(
            artifacts.evidence_list(),
            control_plans_payload=artifacts.plans,
            candidate_handoff_payload=artifacts.handoff,
            direct_campaign_manifest_payload=artifacts.direct_manifest,
            allow_synthetic=True,
        )
        self.assertNotPositive(result)

    def test_missing_plans_cannot_pass(self) -> None:
        artifacts = self.fresh()
        result = evaluate_campaign(
            artifacts.evidence_list(),
            config_payload=artifacts.config,
            manifest_payload=artifacts.manifest,
            candidate_handoff_payload=artifacts.handoff,
            direct_campaign_manifest_payload=artifacts.direct_manifest,
            allow_synthetic=True,
        )
        self.assertNotPositive(result)

    def test_missing_handoff_cannot_pass(self) -> None:
        artifacts = self.fresh()
        result = evaluate_campaign(
            artifacts.evidence_list(),
            config_payload=artifacts.config,
            manifest_payload=artifacts.manifest,
            control_plans_payload=artifacts.plans,
            direct_campaign_manifest_payload=artifacts.direct_manifest,
            allow_synthetic=True,
        )
        self.assertNotPositive(result)

    def test_direct_candidate_change_cannot_pass(self) -> None:
        artifacts = self.fresh()
        evidence = artifacts.evidence["C3"]
        evidence["run_context"]["candidate_endpoint"]["ip"] = "8.8.4.4"
        evidence["proof_binding"]["candidate_endpoint_sha256"] = (
            evidence_digest(evidence["run_context"]["candidate_endpoint"])
        )
        result = evaluate_artifacts(artifacts)
        self.assertNotPositive(result)

    def test_requested_label_is_manifest_bound(self) -> None:
        artifacts = self.fresh()
        artifacts.evidence["C0"]["run_context"][
            "requested_server_label_sha256"
        ] = "e" * 64
        result = evaluate_artifacts(artifacts)
        self.assertNotPositive(result)

    def test_direct_campaign_descriptors_commit_durations(self) -> None:
        artifacts = self.fresh()
        controls = artifacts.direct_manifest["controls"]
        for control in ("C3", "C4", "C5"):
            with self.subTest(control=control):
                self.assertEqual(
                    controls[control]["duration_contract"],
                    artifacts.manifest["durations_seconds"],
                )

    def test_external_deny_is_non_probatory_defense_in_depth(self) -> None:
        artifacts = self.fresh()
        self.assertEqual(
            artifacts.manifest["network_policy"]["external_deny_role"],
            "DEFENSE_IN_DEPTH_NON_PROBATORY",
        )
        self.assertNotIn(
            "gateway_deny_sha256",
            artifacts.evidence["C3"]["proof_binding"],
        )

    # -- Probe identity and path/build binding ---------------------------

    def test_portable_root_must_equal_control_plan_data_path_c012(
        self,
    ) -> None:
        for control in ("C0", "C1", "C2"):
            with self.subTest(control=control):
                artifacts = self.fresh()
                evidence = artifacts.evidence[control]
                evidence["run_context"][
                    "portable_root_path_sha256"
                ] = "e" * 64
                evidence["initial_pre_state_binding"][
                    "portable_root_path_sha256"
                ] = "e" * 64
                result = evaluate_control(artifacts, control)
                self.assertEqual(result.outcome, "FAIL")
                self.assertIn(
                    "portable_root_control_plan_path_mismatch",
                    result.reasons,
                )

    def test_portable_root_must_equal_control_plan_data_path_c4(
        self,
    ) -> None:
        artifacts = self.fresh()
        evidence = artifacts.evidence["C4"]
        evidence["run_context"]["portable_root_path_sha256"] = "e" * 64
        evidence["initial_pre_state_binding"][
            "portable_root_path_sha256"
        ] = "e" * 64
        result = evaluate_control(artifacts, "C4")
        self.assertEqual(result.outcome, "FAIL")
        self.assertIn(
            "portable_root_control_plan_path_mismatch",
            result.reasons,
        )

    def test_portable_root_must_equal_control_plan_data_path_c3_c5(
        self,
    ) -> None:
        for control in ("C3", "C5"):
            with self.subTest(control=control):
                artifacts = self.fresh()
                evidence = artifacts.evidence[control]
                evidence["run_context"][
                    "portable_root_path_sha256"
                ] = "e" * 64
                evidence["initial_pre_state_binding"][
                    "portable_root_path_sha256"
                ] = "e" * 64
                result = evaluate_control(artifacts, control)
                self.assertEqual(result.outcome, "FAIL")
                self.assertIn(
                    "portable_root_control_plan_path_mismatch",
                    result.reasons,
                )

    def test_probe_timestamp_before_login_is_failure(self) -> None:
        for control in ("C2", "C3", "C5"):
            with self.subTest(control=control):
                artifacts = self.fresh()
                artifacts.evidence[control]["identity"][
                    "probe_generated_at_unix"
                ] = STARTS[control] - 3
                _refresh_probe_path_binding(
                    artifacts.evidence[control],
                    artifacts.plans[control],
                )
                result = evaluate_control(artifacts, control)
                self.assertEqual(result.outcome, "FAIL")
                self.assertIn(
                    "identity_probe_timestamp_outside_authenticated_window",
                    result.reasons,
                )

    def test_probe_timestamp_after_control_is_failure(self) -> None:
        for control in ("C2", "C3", "C5"):
            with self.subTest(control=control):
                artifacts = self.fresh()
                completed_at = artifacts.evidence[control]["run_context"][
                    "completed_at_unix"
                ]
                artifacts.evidence[control]["identity"][
                    "probe_generated_at_unix"
                ] = completed_at + 3
                _refresh_probe_path_binding(
                    artifacts.evidence[control],
                    artifacts.plans[control],
                )
                result = evaluate_control(artifacts, control)
                self.assertEqual(result.outcome, "FAIL")
                self.assertIn(
                    "identity_probe_timestamp_outside_authenticated_window",
                    result.reasons,
                )

    def test_probe_timestamp_inside_authenticated_window_is_accepted(
        self,
    ) -> None:
        artifacts = self.fresh()
        for control in ("C2", "C3", "C5"):
            with self.subTest(control=control):
                generated_at = artifacts.evidence[control]["identity"][
                    "probe_generated_at_unix"
                ]
                self.assertGreaterEqual(generated_at, STARTS[control])
        self.assertEqual(
            evaluate_artifacts(artifacts).outcome,
            "SYNTHETIC_PASS",
        )

    def test_probe_terminal_build_mismatch_is_failure(self) -> None:
        artifacts = self.fresh()
        plan = artifacts.plans["C2"]
        # Correlated G5 mutation: compose from a different observed build while
        # leaving the run context and manifest at build 5000.
        artifacts.evidence["C2"]["identity"] = compose_identity(
            _identity_probe(plan, "C2", terminal_build=9999),
            artifacts.config,
            expected_run_id=RUN_IDS["C2"],
            investor_provenance_confirmed=True,
            probe_hash_verified=True,
            probe_static_guard_passed=True,
            control_plan_payload=plan,
        )
        result = evaluate_control(artifacts, "C2")
        self.assertEqual(result.outcome, "FAIL")
        self.assertIn("probe_terminal_build_mismatch", result.reasons)

    def test_probe_path_binding_missing_is_inconclusive(self) -> None:
        artifacts = self.fresh()
        identity = artifacts.evidence["C2"]["identity"]
        identity["terminal_path_sha256"] = None
        identity["terminal_data_path_sha256"] = None
        identity["probe_path_binding_verified"] = False
        result = evaluate_control(artifacts, "C2")
        self.assertEqual(result.outcome, "INCONCLUSIVE")
        self.assertIn("probe_path_binding_missing", result.reasons)

    def test_probe_path_mismatch_is_failure(self) -> None:
        artifacts = self.fresh()
        identity = artifacts.evidence["C2"]["identity"]
        identity["terminal_path_sha256"] = windows_path_digest(
            r"C:\Different\terminal64.exe"
        )
        identity["probe_path_binding_verified"] = True
        result = evaluate_control(artifacts, "C2")
        self.assertEqual(result.outcome, "FAIL")
        self.assertIn("probe_path_binding_mismatch", result.reasons)

    def test_compose_identity_binds_build_paths_and_output(self) -> None:
        identity = self.base.evidence["C2"]["identity"]
        plan = self.base.plans["C2"]
        self.assertEqual(identity["terminal_build"], 5000)
        self.assertEqual(
            identity["terminal_path_sha256"],
            plan["path_bindings"]["terminal_path_sha256"],
        )
        self.assertEqual(
            identity["terminal_data_path_sha256"],
            plan["path_bindings"]["terminal_data_path_sha256"],
        )
        self.assertTrue(identity["probe_path_binding_verified"])
        self.assertEqual(len(identity["identity_probe_output_sha256"]), 64)
        self.assertNotIn("terminal_path", identity)
        self.assertNotIn("terminal_data_path", identity)

    def test_probe_output_digest_reuse_cannot_pass(self) -> None:
        artifacts = self.fresh()
        digest = artifacts.evidence["C2"]["identity"][
            "identity_probe_output_sha256"
        ]
        c3 = artifacts.evidence["C3"]
        c3["identity"]["identity_probe_output_sha256"] = digest
        c3["proof_binding"][
            "probe_path_binding_sha256"
        ] = probe_path_binding_digest(
            run_id=c3["run_id"],
            job_manifest_sha256=c3["proof_binding"][
                "job_manifest_sha256"
            ],
            portable_root_path_sha256=c3["run_context"][
                "portable_root_path_sha256"
            ],
            control_plan_sha256=artifacts.plans["C3"][
                "control_plan_sha256"
            ],
            terminal_path_sha256=c3["identity"][
                "terminal_path_sha256"
            ],
            terminal_data_path_sha256=c3["identity"][
                "terminal_data_path_sha256"
            ],
            identity_probe_output_sha256=digest,
            probe_generated_at_unix=c3["identity"][
                "probe_generated_at_unix"
            ],
        )
        result = evaluate_artifacts(artifacts)
        self.assertNotPositive(result)
        self.assertIn("identity_probe_output_reused_across_runs", result.reasons)

    def test_probe_run_id_mismatch_is_rejected_during_composition(self) -> None:
        artifacts = self.fresh()
        probe = _identity_probe(artifacts.plans["C2"], "C2")
        probe["run_id"] = RUN_IDS["C3"]
        with self.assertRaisesRegex(
            LabValidationError,
            "run_id does not match",
        ):
            compose_identity(
                probe,
                artifacts.config,
                expected_run_id=RUN_IDS["C2"],
                investor_provenance_confirmed=True,
                probe_hash_verified=True,
                probe_static_guard_passed=True,
                control_plan_payload=artifacts.plans["C2"],
            )

    def test_trading_permission_is_falsification(self) -> None:
        artifacts = self.fresh()
        artifacts.evidence["C2"]["identity"]["account_trade_allowed"] = True
        result = evaluate_control(artifacts, "C2")
        self.assertEqual(result.outcome, "FAIL")
        self.assertIn("account_trading_permission_enabled", result.reasons)

    def test_probe_attestation_missing_is_inconclusive(self) -> None:
        artifacts = self.fresh()
        artifacts.evidence["C2"]["identity"]["probe_hash_verified"] = False
        result = evaluate_control(artifacts, "C2")
        self.assertEqual(result.outcome, "INCONCLUSIVE")
        self.assertIn("identity_probe_not_attested", result.reasons)

    def test_compose_timeout_returns_no_identity(self) -> None:
        artifacts = self.fresh()
        probe = _identity_probe(artifacts.plans["C2"], "C2")
        probe.update(
            {
                "terminal_result": "TIMEOUT",
                "account_match": False,
                "account_server": "",
                "account_company": "",
                "account_trade_mode": "UNKNOWN",
            }
        )
        identity = compose_identity(
            probe,
            artifacts.config,
            expected_run_id=RUN_IDS["C2"],
            investor_provenance_confirmed=True,
            probe_hash_verified=True,
            probe_static_guard_passed=True,
            control_plan_payload=artifacts.plans["C2"],
        )
        self.assertIsNone(identity)

    # -- Candidate and config safety ------------------------------------

    def test_candidate_accepts_only_observed_global_literal(self) -> None:
        self.assertEqual(validate_candidate(CANDIDATE), CANDIDATE)
        bad_candidates = [
            {**CANDIDATE, "ip": "127.0.0.1"},
            {**CANDIDATE, "ip": "192.168.1.10"},
            {**CANDIDATE, "port": 22},
            {**CANDIDATE, "source_control": "C1"},
            {**CANDIDATE, "process_scoped": False},
        ]
        for candidate in bad_candidates:
            with self.subTest(candidate=candidate):
                with self.assertRaises(LabValidationError):
                    validate_candidate(candidate)

    def test_real_trade_mode_remains_out_of_scope(self) -> None:
        config = make_config()
        config["expected_identity"]["trade_mode"] = "REAL"
        with self.assertRaisesRegex(
            LabValidationError,
            "must be DEMO",
        ):
            validate_config(config)

    def test_config_rejects_uncommitted_candidate_field(self) -> None:
        config = make_config()
        config["candidate_endpoint"] = CANDIDATE
        with self.assertRaises(LabValidationError):
            validate_config(config)

    def test_config_requires_terminal_signer_policy(self) -> None:
        config = make_config()
        del config["terminal"]["signer_policy_sha256"]
        with self.assertRaises(LabValidationError):
            validate_config(config)

    def test_config_rejects_unsafe_lab_root(self) -> None:
        for root in ("C:\\", r"C:\Windows", r"C:\ProgramData"):
            with self.subTest(root=root):
                config = make_config()
                config["lab_root"] = root
                with self.assertRaises(LabValidationError):
                    validate_config(config)

    def test_sensitive_keys_are_rejected_recursively(self) -> None:
        config = make_config()
        config["metadata"] = {"password": "redacted"}
        with self.assertRaises(LabValidationError):
            validate_config(config)

    def test_sensitive_text_patterns_are_rejected(self) -> None:
        config = make_config()
        config["requested_server_label"] = "Broker password = redacted"
        with self.assertRaises(LabValidationError):
            validate_config(config)

    def test_account_like_number_with_underscore_is_rejected(self) -> None:
        self.assertAccountLabelRejected("Broker 123_456_78")

    def test_account_like_number_with_colon_is_rejected(self) -> None:
        self.assertAccountLabelRejected("Broker 123:456:78")

    def test_account_like_number_with_parentheses_is_rejected(self) -> None:
        self.assertAccountLabelRejected("Broker (123)45678")

    def test_account_like_number_with_bullet_is_rejected(self) -> None:
        self.assertAccountLabelRejected("Broker 123•456•78")

    def test_account_like_number_with_plus_is_rejected(self) -> None:
        self.assertAccountLabelRejected("Broker 123+456+78")

    def test_legitimate_broker_label_corpus_remains_accepted(self) -> None:
        labels = (
            "FPMTrading-Live",
            "MetaQuotes-Demo",
            "IC Markets Europe",
            "Pepperstone Demo",
            "Example Broker Ltd",
        )
        for label in labels:
            with self.subTest(label=label):
                config = make_config()
                config["requested_server_label"] = label
                validated = validate_config(config)
                self.assertEqual(validated["requested_server_label"], label)

    def test_account_sanitizer_does_not_cross_letters(self) -> None:
        config = make_config()
        config["requested_server_label"] = "Broker 123ABC45678"
        self.assertEqual(
            validate_config(config)["requested_server_label"],
            "Broker 123ABC45678",
        )

    def test_nfkc_or_invisible_label_is_rejected(self) -> None:
        labels = ("Broker\u200bDemo", "Broker \uff11\uff12\uff13 Demo")
        for label in labels:
            with self.subTest(label=label):
                self.assertAccountLabelRejected(label)

    # -- Cross-run campaign invariants and digest primitives ------------

    def test_c012_uses_one_initial_pre_state_commitment(self) -> None:
        artifacts = self.fresh()
        bindings = {
            control: artifacts.evidence[control][
                "initial_pre_state_binding"
            ]
            for control in ("C0", "C1", "C2")
        }
        self.assertEqual(
            {
                binding["initial_c012_pre_state_sha256"]
                for binding in bindings.values()
            },
            {
                bindings["C0"]["initial_c012_pre_state_sha256"],
            },
        )
        self.assertEqual(bindings["C0"]["scope"], "C012_INITIAL")
        self.assertEqual(bindings["C1"]["scope"], "C012_REFERENCE")
        self.assertEqual(bindings["C2"]["scope"], "C012_REFERENCE")
        self.assertIsNotNone(artifacts.evidence["C0"]["pre_state"])
        self.assertIsNone(artifacts.evidence["C1"]["pre_state"])
        self.assertIsNone(artifacts.evidence["C2"]["pre_state"])

    def test_c1_cannot_invent_a_second_initial_pre_state(self) -> None:
        artifacts = self.fresh()
        artifacts.evidence["C1"]["pre_state"] = copy.deepcopy(
            artifacts.evidence["C0"]["pre_state"]
        )
        with self.assertRaisesRegex(
            LabValidationError,
            "single C012 initial pre-state",
        ):
            validate_evidence(artifacts.evidence["C1"])

    def test_c1_cache_transition_is_explicit(self) -> None:
        artifacts = self.fresh()
        transition = artifacts.evidence["C1"]["state_transition"]
        proof = artifacts.evidence["C1"]["proof_binding"]
        self.assertEqual(
            transition["stage"],
            "C1_DISCOVERY_COMPLETE",
        )
        self.assertEqual(
            transition["broker_cache_state"],
            "CREATED_RECORDED",
        )
        self.assertEqual(
            transition["account_cache_state"],
            "ABSENT_RECORDED",
        )
        self.assertTrue(transition["transition_verified"])
        self.assertIsNotNone(
            transition["transition_evidence_sha256"]
        )
        self.assertEqual(
            proof["state_transition_sha256"],
            contract_digest(
                "STATE_TRANSITION",
                1,
                {
                    "run_id": RUN_IDS["C1"],
                    "control": "C1",
                    "state_transition": transition,
                },
            ),
        )

    def test_c1_c2_cache_transitions_form_one_state_machine(self) -> None:
        artifacts = self.fresh()
        c1 = artifacts.evidence["C1"]
        c1["state_transition"]["broker_cache_state"] = "ABSENT_RECORDED"
        c1["proof_binding"]["state_transition_sha256"] = contract_digest(
            "STATE_TRANSITION",
            1,
            {
                "run_id": c1["run_id"],
                "control": "C1",
                "state_transition": c1["state_transition"],
            },
        )
        result = evaluate_artifacts(artifacts)
        self.assertEqual(result.outcome, "FAIL")
        self.assertIn(
            "c012_state_transition_incompatible",
            result.reasons,
        )

        unverified = self.fresh()
        c1 = unverified.evidence["C1"]
        c1["state_transition"].update(
            {
                "broker_cache_state": "ABSENT_RECORDED",
                "transition_verified": False,
            }
        )
        c1["proof_binding"]["state_transition_sha256"] = contract_digest(
            "STATE_TRANSITION",
            1,
            {
                "run_id": c1["run_id"],
                "control": "C1",
                "state_transition": c1["state_transition"],
            },
        )
        self.assertEqual(
            evaluate_artifacts(unverified).outcome,
            "INCONCLUSIVE",
        )

    def test_c2_cannot_invent_a_second_initial_pre_state(self) -> None:
        artifacts = self.fresh()
        artifacts.evidence["C2"]["pre_state"] = copy.deepcopy(
            artifacts.evidence["C0"]["pre_state"]
        )
        with self.assertRaisesRegex(
            LabValidationError,
            "single C012 initial pre-state",
        ):
            validate_evidence(artifacts.evidence["C2"])

    def test_cold_boot_pre_state_remains_clean(self) -> None:
        artifacts = self.fresh()
        for control in ("C3", "C4", "C5"):
            with self.subTest(control=control):
                pre_state = artifacts.evidence[control]["pre_state"]
                binding = artifacts.evidence[control][
                    "initial_pre_state_binding"
                ]
                self.assertTrue(all(pre_state.values()))
                self.assertEqual(binding["scope"], "COLD_BOOT_INITIAL")
                self.assertIsNone(
                    binding["initial_c012_pre_state_sha256"]
                )
        artifacts.evidence["C4"]["pre_state"][
            "servers_dat_absent"
        ] = False
        c4 = artifacts.evidence["C4"]
        c4["proof_binding"]["pre_state_binding_sha256"] = contract_digest(
            "PRE_STATE_BINDING",
            1,
            {
                "run_id": c4["run_id"],
                "control": "C4",
                "initial_pre_state_binding": c4[
                    "initial_pre_state_binding"
                ],
                "pre_state": c4["pre_state"],
            },
        )
        result = evaluate_control(artifacts, "C4")
        self.assertEqual(result.outcome, "FAIL")
        self.assertIn("negative_control_not_clean", result.reasons)

    def test_c0_cache_present_before_launch_is_failure(self) -> None:
        artifacts = self.fresh()
        evidence = artifacts.evidence["C0"]
        evidence["pre_state"]["servers_dat_absent"] = False
        evidence["proof_binding"]["pre_state_binding_sha256"] = (
            contract_digest(
                "PRE_STATE_BINDING",
                1,
                {
                    "run_id": evidence["run_id"],
                    "control": "C0",
                    "initial_pre_state_binding": evidence[
                        "initial_pre_state_binding"
                    ],
                    "pre_state": evidence["pre_state"],
                },
            )
        )
        result = evaluate_control(artifacts, "C0")
        self.assertEqual(result.outcome, "FAIL")
        self.assertIn("c012_pre_state_not_clean", result.reasons)

    def test_campaign_run_ids_must_be_unique(self) -> None:
        artifacts = self.fresh()
        artifacts.evidence["C5"]["run_id"] = RUN_IDS["C4"]
        artifacts.evidence["C5"]["proof_binding"]["run_id"] = RUN_IDS["C4"]
        with self.assertRaises(LabValidationError):
            validate_evidence(artifacts.evidence["C5"])

    def test_c012_logical_instance_must_be_shared(self) -> None:
        artifacts = self.fresh()
        artifacts.evidence["C1"]["run_context"][
            "clone_id_sha256"
        ] = "e" * 64
        result = evaluate_artifacts(artifacts)
        self.assertNotPositive(result)
        self.assertIn("c012_not_same_logical_instance", result.reasons)

    def test_cold_boot_instances_must_be_independent(self) -> None:
        artifacts = self.fresh()
        artifacts.evidence["C4"]["run_context"][
            "clone_id_sha256"
        ] = artifacts.evidence["C3"]["run_context"]["clone_id_sha256"]
        result = evaluate_artifacts(artifacts)
        self.assertNotPositive(result)
        self.assertIn("cold_boot_instances_not_independent", result.reasons)

    def test_identity_change_across_direct_runs_is_failure(self) -> None:
        artifacts = self.fresh()
        identity = artifacts.evidence["C5"]["identity"]
        identity["server"] = "Different-Demo"
        identity["expected_server_match"] = False
        result = evaluate_artifacts(artifacts)
        self.assertEqual(result.outcome, "FAIL")

    def test_global_helper_secret_access_is_failure(self) -> None:
        artifacts = self.fresh()
        artifacts.evidence["C0"]["discovery"]["helper_secret_accessed"] = True
        result = evaluate_artifacts(artifacts)
        self.assertEqual(result.outcome, "FAIL")
        self.assertIn("C0_helper_accessed_sensitive_material", result.reasons)

    def test_unsafe_endpoint_promotion_is_failure(self) -> None:
        artifacts = self.fresh()
        artifacts.evidence["C1"]["discovery"][
            "unsafe_endpoint_promoted"
        ] = True
        result = evaluate_artifacts(artifacts)
        self.assertEqual(result.outcome, "FAIL")
        self.assertIn("C1_unsafe_endpoint_promoted", result.reasons)

    def test_contract_digest_is_domain_separated(self) -> None:
        payload = {"same": "body"}
        self.assertNotEqual(
            contract_digest("EXPERIMENT_MANIFEST", 1, payload),
            contract_digest("CONTROL_PLAN", 1, payload),
        )
        self.assertNotEqual(
            contract_digest("CONTROL_PLAN", 1, payload),
            contract_digest("CONTROL_PLAN", 2, payload),
        )

    def test_contract_digest_is_canonical(self) -> None:
        left = {"b": 2, "a": {"d": 4, "c": 3}}
        right = {"a": {"c": 3, "d": 4}, "b": 2}
        self.assertEqual(canonical_json(left), canonical_json(right))
        self.assertEqual(
            contract_digest("TEST", 1, left),
            contract_digest("TEST", 1, right),
        )

    def test_windows_path_digest_is_canonical_and_domain_bound(self) -> None:
        self.assertEqual(
            windows_path_digest(r"C:\TJLab\terminal64.exe"),
            windows_path_digest("C:/TJLab/terminal64.exe"),
        )
        self.assertNotEqual(
            windows_path_digest(r"C:\TJLab\terminal64.exe"),
            evidence_digest(
                {"canonical_path": r"c:\tjlab\terminal64.exe"}
            ),
        )

    def test_manifest_digest_commits_every_authoritative_section(self) -> None:
        original = self.base.manifest["experiment_manifest_sha256"]
        mutations = (
            ("region", "different-region"),
            ("requested_server_label", "Different Broker Demo"),
        )
        for field, value in mutations:
            with self.subTest(field=field):
                config = copy.deepcopy(self.base.config)
                config[field] = value
                rebuilt = build_experiment_manifest(config)
                self.assertNotEqual(
                    rebuilt["experiment_manifest_sha256"],
                    original,
                )

    def test_synthetic_provenance_requires_explicit_authorization(self) -> None:
        artifacts = self.fresh()
        result = evaluate_control(
            artifacts,
            "C0",
            allow_synthetic=False,
        )
        self.assertEqual(result.outcome, "INCONCLUSIVE")
        self.assertIn(
            "synthetic_fixture_requires_test_only_authorization",
            result.reasons,
        )

    def test_handoff_builder_rejects_other_c2(self) -> None:
        artifacts = self.fresh()
        with self.assertRaises(LabValidationError):
            build_candidate_handoff(
                artifacts.config,
                {"invalid": "different C2 evidence"},
                None,
                direct_campaign_manifest=artifacts.direct_manifest,
            )

    def test_probe_output_digest_mismatch_rejected(self) -> None:
        artifacts = self.fresh()
        with self.assertRaisesRegex(
            LabValidationError,
            "does not match the validated probe",
        ):
            compose_identity(
                _identity_probe(artifacts.plans["C2"], "C2"),
                artifacts.config,
                expected_run_id=RUN_IDS["C2"],
                investor_provenance_confirmed=True,
                probe_hash_verified=True,
                probe_static_guard_passed=True,
                control_plan_payload=artifacts.plans["C2"],
                probe_output_sha256="a" * 64,
            )

    def test_probe_path_binds_job_portable_root(self) -> None:
        for field in (
            "job_manifest_sha256",
            "portable_root_path_sha256",
        ):
            with self.subTest(field=field):
                artifacts = self.fresh()
                evidence = artifacts.evidence["C2"]
                if field == "job_manifest_sha256":
                    evidence["proof_binding"][field] = "e" * 64
                    expected_reason = (
                        "lifecycle_job_process_binding_mismatch"
                    )
                else:
                    evidence["run_context"][field] = "e" * 64
                    evidence["initial_pre_state_binding"][
                        "portable_root_path_sha256"
                    ] = "e" * 64
                    expected_reason = (
                        "portable_root_control_plan_path_mismatch"
                    )
                result = evaluate_control(artifacts, "C2")
                self.assertEqual(result.outcome, "FAIL")
                self.assertIn(expected_reason, result.reasons)

    def test_timeline_qpc_utc_duration_mismatch_cannot_pass(self) -> None:
        artifacts = self.fresh()
        evidence = artifacts.evidence["C0"]
        events = evidence["timeline"]["events"]
        events[-1]["timestamp_unix_ms"] = events[0]["timestamp_unix_ms"]
        phase_digest = contract_digest(
            "PHASE_TIMELINE",
            1,
            {
                "run_id": evidence["run_id"],
                "control": "C0",
                "timeline": evidence["timeline"],
            },
        )
        evidence["proof_binding"]["phase_timeline_sha256"] = phase_digest
        evidence["proof_binding"][
            "requested_label_binding_sha256"
        ] = evidence_digest(
            {
                "run_id": evidence["run_id"],
                "control": "C0",
                "experiment_id": EXPERIMENT_ID,
                "requested_label_manifest_sha256": (
                    requested_label_manifest_digest(artifacts.manifest)
                ),
                "job_identity_sha256": evidence["proof_binding"][
                    "job_identity_sha256"
                ],
                "phase_timeline_sha256": phase_digest,
                "requested_server_label_sha256": (
                    REQUESTED_SERVER_LABEL_SHA256
                ),
                "selected_server_label_sha256": None,
            }
        )
        with self.assertRaisesRegex(
            LabValidationError,
            "QPC and UTC clocks disagree",
        ):
            validate_evidence(evidence)

    def test_rehashed_plan_action_rejected(self) -> None:
        artifacts = self.fresh()
        plan = artifacts.plans["C0"]
        plan["actions"][0]["action"] = "arbitrary_uncommitted_semantics"
        body = {
            key: value
            for key, value in plan.items()
            if key != "control_plan_sha256"
        }
        plan["control_plan_sha256"] = contract_digest(
            "CONTROL_PLAN",
            2,
            body,
        )
        with self.assertRaisesRegex(
            LabValidationError,
            "actions differ from the committed policy",
        ):
            validate_control_plan(
                plan,
                manifest_payload=artifacts.manifest,
            )

    def test_c1_alternate_artifact_requires_process_scope(self) -> None:
        artifacts = self.fresh()
        evidence = artifacts.evidence["C1"]
        evidence["discovery"][
            "endpoint_delta_source"
        ] = "SANITIZED_ENDPOINT_ARTIFACT"
        with self.assertRaisesRegex(
            LabValidationError,
            "endpoint_delta_source is invalid",
        ):
            validate_evidence(evidence)


if __name__ == "__main__":
    unittest.main()
