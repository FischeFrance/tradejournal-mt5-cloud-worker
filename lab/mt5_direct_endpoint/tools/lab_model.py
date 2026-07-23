from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import PureWindowsPath
from typing import Any, Mapping


CONFIG_SCHEMA_VERSION = 4
EVIDENCE_SCHEMA_VERSION = 6
PROOF_BINDING_SCHEMA_VERSION = 5
IDENTITY_PROBE_SCHEMA_VERSION = 3
IDENTITY_PROBE_VERSION = "3.0.0"
EXPERIMENT_MANIFEST_SCHEMA_VERSION = 2
CONTROL_PLAN_SCHEMA_VERSION = 2
DIRECT_CAMPAIGN_MANIFEST_SCHEMA_VERSION = 2
CANDIDATE_HANDOFF_SCHEMA_VERSION = 2
TIMELINE_SCHEMA_VERSION = 1
POLICY_VERSION = "mt5-direct-endpoint-policy-v2"
PROBE_POLICY = "DEMO_INVESTOR_READ_ONLY_V1"
EXTERNAL_DENY_ROLE = "DEFENSE_IN_DEPTH_NON_PROBATORY"
C012_LIFECYCLE_MODE = "C012_SINGLE_PROCESS_SESSION"
DIRECT_LIFECYCLE_MODE = "INDEPENDENT_DISPOSABLE_CONTROL"
C012_ROOT_GENERATION_POLICY = "SINGLE_SHARED_C0_C1_C2"
C012_TRANSIENT_POLICY = "C2_CONFIG_SUBMITTER_SAME_JOB_ONLY"
CLEAN_PRE_STATE_KEYS = (
    "portable_root_new",
    "disposable_clone_new",
    "windows_user_new",
    "accounts_dat_absent",
    "servers_dat_absent",
    "bases_absent",
    "appdata_absent",
    "registry_clean",
    "credential_manager_empty",
    "community_identity_absent",
    "no_shared_storage",
    "sensitive_bootstrap_absent",
    "prior_processes_absent",
    "terminal_data_path_matches",
)
# UTC is millisecond-granular while QPC is high resolution.  A phase whose two
# clocks disagree by more than this cannot be treated as an ordered duration.
TIMELINE_CLOCK_TOLERANCE_MS = 1_000
IDENTITY_PROBE_TERMINAL_RESULTS = (
    "CONNECTED_IDENTITY_AVAILABLE",
    "IDENTITY_MISMATCH",
    "TIMEOUT",
    "NOT_CONNECTED",
    "INPUT_INVALID",
    "OUTPUT_FAILURE",
)
UNBOUND_PROBE_RUN_ID = "00000000-0000-4000-8000-000000000000"
# Kept as the config/plan version for callers that imported the original name.
SCHEMA_VERSION = CONFIG_SCHEMA_VERSION
CONTROLS = ("C0", "C1", "C2", "C3", "C4", "C5")
TRADE_MODES = ("DEMO", "CONTEST", "REAL")
OBSERVED_PHASES = ("NONE", "LOGIN", "DIRECT_ONLY", "ENDPOINT_BLOCKED", "DIRECT_REPEAT")
PROVENANCE_PRODUCERS = {
    "SYNTHETIC_FIXTURE": "OFFLINE_TEST_FIXTURE",
    "CAPTURED_EXPORT": "LAB_CAPTURE_EXPORT",
}

FORBIDDEN_ADDRESS_NETWORKS = tuple(
    ipaddress.ip_network(cidr, strict=False)
    for cidr in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.0.2.0/24",
        "192.31.196.0/24",
        "192.52.193.0/24",
        "192.88.99.0/24",
        "192.168.0.0/16",
        "192.175.48.0/24",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/4",
        "240.0.0.0/4",
        "::/96",
        "64:ff9b::/96",
        "64:ff9b:1::/48",
        "100::/64",
        "2001::/23",
        "2001:db8::/32",
        "2002::/16",
        "3fff::/20",
        "5f00::/16",
        "fc00::/7",
        "fe80::/10",
        "ff00::/8",
    )
)

# These destinations are never useful as MT5 trade access points.  A candidate on a
# different non-standard port can still be tested, but only after it was observed in C2.
DANGEROUS_REMOTE_PORTS = frozenset(
    {
        22,
        23,
        25,
        53,
        67,
        68,
        69,
        110,
        111,
        135,
        137,
        138,
        139,
        161,
        389,
        445,
        1433,
        2375,
        2376,
        3306,
        3389,
        5432,
        5985,
        5986,
        6379,
        9200,
        11211,
    }
)

FORBIDDEN_EVIDENCE_KEYS = frozenset(
    {
        "password",
        "investor_password",
        "master_password",
        "secret",
        "token",
        "credential",
        "credential_envelope",
        "login",
        "account_number",
        "account_id",
        "connection_id",
        "client_name",
        "customer_name",
        "balance",
        "equity",
        "positions",
        "orders",
        "deals",
        "history",
        "servers_dat",
        "accounts_dat",
        "startup_config",
        "pcap_payload",
    }
)

_SECRET_TEXT_PATTERNS = (
    re.compile(
        r"(?i)(?<![0-9A-Za-z_])(?:password|passwd|pwd|secret|token)"
        r"(?![0-9A-Za-z_])"
    ),
    re.compile(r"(?i)authorization\s*:\s*bearer\s+"),
    re.compile(r"(?i)(?:^|\s)/login:\d+"),
    re.compile(r"(?i)(?:^|[\r\n])login\s*=\s*\d+"),
)

_SHA256 = re.compile(r"[0-9a-fA-F]{64}\Z")
_CLEAN_TEXT = re.compile(r"[^\x00-\x1f\x7f]{1,256}\Z")
_FORMATTED_ACCOUNT_NUMBER_FIELDS = frozenset(
    {
        "requested_server_label",
        "expected_identity.server",
        "expected_identity.company",
        "run_context.expected_server",
        "run_context.expected_company",
        "identity.server",
        "identity.company",
        "identity probe account_server",
        "identity probe account_company",
        "expected identity server",
        "expected identity company",
    }
)

_DIGEST_DOMAIN_PREFIX = b"MT5_DIRECT_ENDPOINT\x00"

# Default-ignorable format characters are mostly Unicode category C and are
# rejected below through unicodedata.category().  These ranges cover the
# remaining invisible marks/fillers which are not category C on every Python
# Unicode database version (for example variation selectors and CGJ).
_INVISIBLE_CODEPOINT_RANGES = (
    (0x034F, 0x034F),
    (0x115F, 0x1160),
    (0x17B4, 0x17B5),
    (0x180B, 0x180D),
    (0x3164, 0x3164),
    (0xFE00, 0xFE0F),
    (0xFFA0, 0xFFA0),
    (0x1D173, 0x1D17A),
    (0xE0100, 0xE01EF),
)


class LabValidationError(ValueError):
    """Raised when a plan or evidence artifact violates a lab safety invariant."""


@dataclass(frozen=True)
class Evaluation:
    outcome: str
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {"outcome": self.outcome, "reasons": list(self.reasons)}


def canonical_json(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def evidence_digest(payload: object) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def contract_digest(
    artifact_type: str,
    schema_version: int,
    payload: object,
) -> str:
    """Return a domain-separated digest for one validated contract body.

    ``evidence_digest`` remains available for the small legacy sub-bindings
    which already include an unambiguous field set.  Authoritative lab
    artifacts use this function so that the same JSON body cannot be
    reinterpreted as a manifest, plan, handoff, timeline, or evidence.
    """

    if (
        not isinstance(artifact_type, str)
        or not re.fullmatch(r"[A-Z][A-Z0-9_]{2,63}", artifact_type)
    ):
        raise LabValidationError("artifact_type is invalid")
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version < 1
    ):
        raise LabValidationError("contract schema_version must be a positive integer")
    header = (
        _DIGEST_DOMAIN_PREFIX
        + artifact_type.encode("ascii")
        + b"\x00"
        + str(schema_version).encode("ascii")
        + b"\x00"
    )
    return hashlib.sha256(
        header + canonical_json(payload).encode("utf-8")
    ).hexdigest()


def windows_path_digest(path: object, field: str = "path") -> str:
    canonical = _validated_windows_absolute_path(path, field)
    return contract_digest("WINDOWS_PATH", 1, {"canonical_path": canonical})


def negative_query_contract(experiment_id: object) -> dict[str, object]:
    """Return the deterministic, non-sensitive negative discovery query."""

    normalized_experiment_id = _validated_uuid(
        experiment_id, "negative query experiment_id"
    )
    label = f"TJ-NO-SUCH-{normalized_experiment_id}"
    return {
        "schema_version": 1,
        "label": label,
        "label_sha256": contract_digest(
            "NEGATIVE_QUERY_LABEL",
            1,
            {
                "experiment_id": normalized_experiment_id,
                "label": label,
            },
        ),
        "expected_result_count": 0,
    }


def initial_c012_pre_state_body(
    *,
    experiment_id: object,
    c012_session_id: object,
    portable_root_path_sha256: object,
    checks: object | None = None,
) -> dict[str, object]:
    """Canonical standalone body acquired once before C0.

    The body intentionally contains no plan/evidence digest, so all C0-C2 plans
    can pre-commit the same snapshot without a digest cycle.
    """

    clean_checks = (
        {key: True for key in CLEAN_PRE_STATE_KEYS}
        if checks is None
        else _require_bool_mapping(
            checks, set(CLEAN_PRE_STATE_KEYS), "C012 initial pre-state checks"
        )
    )
    return {
        "schema_version": 1,
        "artifact_type": "C012_INITIAL_PRE_STATE",
        "experiment_id": _validated_uuid(
            experiment_id, "C012 initial pre-state experiment_id"
        ),
        "c012_session_id": _validated_uuid(
            c012_session_id, "C012 initial pre-state c012_session_id"
        ),
        "portable_root_path_sha256": _validated_sha256(
            portable_root_path_sha256,
            "C012 initial pre-state portable_root_path_sha256",
        ),
        "checks": clean_checks,
    }


def initial_c012_pre_state_digest(body: object) -> str:
    clean = _require_mapping(body, "C012 initial pre-state")
    _require_exact_keys(
        clean,
        {
            "schema_version",
            "artifact_type",
            "experiment_id",
            "c012_session_id",
            "portable_root_path_sha256",
            "checks",
        },
        "C012 initial pre-state",
    )
    _require_exact_version(
        clean["schema_version"], 1, "C012 initial pre-state schema_version"
    )
    if clean["artifact_type"] != "C012_INITIAL_PRE_STATE":
        raise LabValidationError("unsupported C012 initial pre-state artifact")
    canonical = initial_c012_pre_state_body(
        experiment_id=clean["experiment_id"],
        c012_session_id=clean["c012_session_id"],
        portable_root_path_sha256=clean["portable_root_path_sha256"],
        checks=clean["checks"],
    )
    if canonical_json(clean) != canonical_json(canonical):
        raise LabValidationError("C012 initial pre-state body is not canonical")
    return contract_digest("C012_INITIAL_PRE_STATE", 1, canonical)


def lifecycle_binding_digest(
    *,
    run_id: object,
    control: object,
    control_plan_sha256: object,
    lifecycle_binding: object,
) -> str:
    return contract_digest(
        "LIFECYCLE_BINDING",
        1,
        {
            "run_id": _validated_uuid(run_id, "lifecycle binding run_id"),
            "control": _validated_control(control, "lifecycle binding control"),
            "control_plan_sha256": _validated_sha256(
                control_plan_sha256,
                "lifecycle binding control_plan_sha256",
            ),
            "lifecycle_binding": lifecycle_binding,
        },
    )


def job_portable_root_binding_digest(
    *,
    run_id: object,
    job_manifest_sha256: object,
    job_identity_sha256: object,
    root_process_generation_sha256: object,
    portable_root_path_sha256: object,
) -> str:
    return contract_digest(
        "JOB_PORTABLE_ROOT_BINDING",
        1,
        {
            "run_id": _validated_uuid(run_id, "Job/root binding run_id"),
            "job_manifest_sha256": _validated_sha256(
                job_manifest_sha256, "Job/root binding job_manifest_sha256"
            ),
            "job_identity_sha256": _validated_sha256(
                job_identity_sha256, "Job/root binding job_identity_sha256"
            ),
            "root_process_generation_sha256": _validated_sha256(
                root_process_generation_sha256,
                "Job/root binding root_process_generation_sha256",
            ),
            "portable_root_path_sha256": _validated_sha256(
                portable_root_path_sha256,
                "Job/root binding portable_root_path_sha256",
            ),
        },
    )


def firewall_portable_root_binding_digest(
    *,
    run_id: object,
    control_plan_sha256: object,
    firewall_plan_sha256: object,
    portable_root_path_sha256: object,
    candidate_endpoint_sha256: object,
) -> str:
    return contract_digest(
        "FIREWALL_PORTABLE_ROOT_BINDING",
        1,
        {
            "run_id": _validated_uuid(run_id, "firewall/root binding run_id"),
            "control_plan_sha256": _validated_sha256(
                control_plan_sha256,
                "firewall/root binding control_plan_sha256",
            ),
            "firewall_plan_sha256": _validated_sha256(
                firewall_plan_sha256,
                "firewall/root binding firewall_plan_sha256",
            ),
            "portable_root_path_sha256": _validated_sha256(
                portable_root_path_sha256,
                "firewall/root binding portable_root_path_sha256",
            ),
            "candidate_endpoint_sha256": _validated_sha256(
                candidate_endpoint_sha256,
                "firewall/root binding candidate_endpoint_sha256",
            ),
        },
    )


def probe_path_binding_digest(
    *,
    run_id: object,
    job_manifest_sha256: object,
    portable_root_path_sha256: object,
    control_plan_sha256: object,
    terminal_path_sha256: object,
    terminal_data_path_sha256: object,
    identity_probe_output_sha256: object,
    probe_generated_at_unix: object,
) -> str:
    """Bind sanitized probe paths to the run, Job, portable root and plan."""

    body = {
        "run_id": _validated_uuid(run_id, "probe path binding run_id"),
        "job_manifest_sha256": _validated_sha256(
            job_manifest_sha256,
            "probe path binding job_manifest_sha256",
        ),
        "portable_root_path_sha256": _validated_sha256(
            portable_root_path_sha256,
            "probe path binding portable_root_path_sha256",
        ),
        "control_plan_sha256": _validated_sha256(
            control_plan_sha256,
            "probe path binding control_plan_sha256",
        ),
        "terminal_path_sha256": _validated_sha256(
            terminal_path_sha256,
            "probe path binding terminal_path_sha256",
        ),
        "terminal_data_path_sha256": _validated_sha256(
            terminal_data_path_sha256,
            "probe path binding terminal_data_path_sha256",
        ),
        "identity_probe_output_sha256": _validated_sha256(
            identity_probe_output_sha256,
            "probe path binding identity_probe_output_sha256",
        ),
    }
    generated_at = probe_generated_at_unix
    if (
        isinstance(generated_at, bool)
        or not isinstance(generated_at, int)
        or generated_at < 1
    ):
        raise LabValidationError(
            "probe path binding probe_generated_at_unix must be positive"
        )
    body["probe_generated_at_unix"] = generated_at
    return contract_digest("PROBE_PATH_BINDING", 1, body)


def requested_label_manifest_digest(config: Mapping[str, object]) -> str:
    """Digest the immutable authoritative label subset of validated config."""

    return evidence_digest(
        {
            "schema_version": EXPERIMENT_MANIFEST_SCHEMA_VERSION,
            "policy_version": config["policy_version"],
            "experiment_id": config["experiment_id"],
            "requested_server_label": config["requested_server_label"],
        }
    )


def compose_identity(
    probe_payload: object,
    config_payload: object,
    *,
    expected_run_id: str,
    investor_provenance_confirmed: bool,
    probe_hash_verified: bool,
    probe_static_guard_passed: bool,
    control_plan_payload: object | None = None,
    probe_output_sha256: str | None = None,
) -> dict[str, object] | None:
    """Convert the MQL5 probe output to the narrower sanitized evidence identity."""

    probe = _validate_identity_probe(probe_payload)
    config = validate_config(config_payload)
    normalized_expected_run_id = _validated_uuid(expected_run_id, "expected_run_id")
    if normalized_expected_run_id == UNBOUND_PROBE_RUN_ID:
        raise LabValidationError("expected_run_id cannot be the unbound sentinel")
    if probe["run_id"] != normalized_expected_run_id:
        raise LabValidationError("identity probe run_id does not match expected_run_id")
    manifest = build_experiment_manifest(config)
    control_plan = (
        None
        if control_plan_payload is None
        else validate_control_plan(
            control_plan_payload,
            manifest_payload=manifest,
        )
    )
    if control_plan is not None and control_plan["run_id"] != normalized_expected_run_id:
        raise LabValidationError("identity probe run_id does not match control plan")
    expected = config["expected_identity"]
    assert isinstance(expected, dict)
    for name, value in (
        ("investor_provenance_confirmed", investor_provenance_confirmed),
        ("probe_hash_verified", probe_hash_verified),
        ("probe_static_guard_passed", probe_static_guard_passed),
    ):
        if not isinstance(value, bool):
            raise LabValidationError(f"{name} must be boolean")

    terminal_result = probe["terminal_result"]
    if terminal_result in {"TIMEOUT", "NOT_CONNECTED"}:
        return None
    if terminal_result in {"INPUT_INVALID", "OUTPUT_FAILURE"}:
        raise LabValidationError(
            f"identity probe terminal_result {terminal_result} is not composable"
        )

    terminal_path_sha256 = windows_path_digest(
        probe["terminal_path"], "identity probe terminal_path"
    )
    terminal_data_path_sha256 = windows_path_digest(
        probe["terminal_data_path"], "identity probe terminal_data_path"
    )
    computed_output_digest = contract_digest(
        "IDENTITY_PROBE_OUTPUT",
        IDENTITY_PROBE_SCHEMA_VERSION,
        probe,
    )
    if (
        probe_output_sha256 is not None
        and _validated_sha256(
            probe_output_sha256,
            "identity_probe_output_sha256",
        )
        != computed_output_digest
    ):
        raise LabValidationError(
            "identity_probe_output_sha256 does not match the validated probe"
        )
    output_digest = computed_output_digest
    path_binding_verified = False
    if control_plan is not None:
        path_bindings = control_plan["path_bindings"]
        assert isinstance(path_bindings, Mapping)
        path_binding_verified = bool(
            terminal_path_sha256 == path_bindings["terminal_path_sha256"]
            and terminal_data_path_sha256
            == path_bindings["terminal_data_path_sha256"]
        )

    result = {
        "probe_run_id": probe["run_id"],
        "probe_generated_at_unix": probe["generated_at_unix"],
        "account_match": probe["account_match"],
        "expected_server_match": probe["account_server"] == expected["server"],
        "expected_company_match": probe["account_company"] == expected["company"],
        "account_trade_allowed": probe["account_trade_allowed"],
        "account_trade_expert": probe["account_trade_expert"],
        "terminal_connected": probe["terminal_connected"],
        "terminal_trade_allowed": probe["terminal_trade_allowed"],
        "investor_provenance_confirmed": investor_provenance_confirmed,
        "probe_hash_verified": probe_hash_verified,
        "probe_static_guard_passed": probe_static_guard_passed,
        "terminal_build": probe["terminal_build"],
        "terminal_path_sha256": terminal_path_sha256,
        "terminal_data_path_sha256": terminal_data_path_sha256,
        "identity_probe_output_sha256": output_digest,
        "probe_path_binding_verified": path_binding_verified,
        "server": probe["account_server"],
        "company": probe["account_company"],
        "trade_mode": probe["account_trade_mode"],
    }
    return _require_identity(result) or {}


def validate_candidate(candidate: object) -> dict[str, object]:
    data = _require_mapping(candidate, "candidate_endpoint")
    _require_exact_keys(
        data,
        {
            "ip",
            "port",
            "source_control",
            "observed_phase",
            "process_scoped",
        },
        "candidate_endpoint",
    )

    raw_ip = data["ip"]
    if not isinstance(raw_ip, str) or raw_ip != raw_ip.strip() or "%" in raw_ip:
        raise LabValidationError("candidate IP must be one canonical literal address")
    try:
        parsed = ipaddress.ip_address(raw_ip)
    except ValueError as exc:
        raise LabValidationError("candidate must be a literal IPv4 or IPv6 address") from exc

    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        raise LabValidationError("IPv4-mapped IPv6 candidates are forbidden")
    if parsed.compressed != raw_ip:
        raise LabValidationError("candidate IP must use canonical compressed syntax")
    if not parsed.is_global:
        raise LabValidationError("candidate must be globally routable")
    if any(
        (
            parsed.is_private,
            parsed.is_loopback,
            parsed.is_link_local,
            parsed.is_multicast,
            parsed.is_reserved,
            parsed.is_unspecified,
        )
    ):
        raise LabValidationError("candidate belongs to a forbidden address class")
    if any(parsed in network for network in FORBIDDEN_ADDRESS_NETWORKS if network.version == parsed.version):
        raise LabValidationError("candidate belongs to a forbidden special-purpose network")
    if parsed.version == 6 and parsed not in ipaddress.ip_network("2000::/3"):
        raise LabValidationError("candidate IPv6 address is not global unicast")

    port = data["port"]
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise LabValidationError("candidate port must be an integer in 1..65535")
    if port in DANGEROUS_REMOTE_PORTS:
        raise LabValidationError("candidate port is forbidden by the lab safety policy")
    if data["source_control"] != "C2" or data["observed_phase"] != "LOGIN":
        raise LabValidationError("candidate must originate from the C2 LOGIN phase")
    if data["process_scoped"] is not True:
        raise LabValidationError("candidate must have process-scoped observation evidence")

    return {
        "ip": parsed.compressed,
        "port": port,
        "source_control": "C2",
        "observed_phase": "LOGIN",
        "process_scoped": True,
    }


def validate_config(payload: object) -> dict[str, object]:
    data = _require_mapping(payload, "experiment config")
    _require_exact_keys(
        data,
        {
            "schema_version",
            "policy_version",
            "experiment_id",
            "region",
            "lab_root",
            "terminal",
            "lifecycle",
            "requested_server_label",
            "expected_identity",
            "probe",
            "network_policy",
            "durations_seconds",
        },
        "experiment config",
    )
    config_schema_version = data["schema_version"]
    if (
        isinstance(config_schema_version, bool)
        or not isinstance(config_schema_version, int)
        or config_schema_version != SCHEMA_VERSION
    ):
        raise LabValidationError("unsupported experiment config schema_version")
    if data["policy_version"] != POLICY_VERSION:
        raise LabValidationError("unsupported experiment policy_version")
    experiment_id = _validated_uuid(data["experiment_id"], "experiment_id")
    region = _validated_text(data["region"], "region", 64)
    lab_root = _validated_lab_root(data["lab_root"])

    terminal = _require_mapping(data["terminal"], "terminal")
    _require_exact_keys(
        terminal,
        {"path", "sha256", "publisher", "signer_policy_sha256"},
        "terminal",
    )
    terminal_path = _validated_windows_absolute_path(terminal["path"], "terminal.path")
    if PureWindowsPath(terminal_path).name.casefold() != "terminal64.exe":
        raise LabValidationError("terminal.path must end in terminal64.exe")
    terminal_sha = _validated_sha256(terminal["sha256"], "terminal.sha256")
    publisher = _validated_text(terminal["publisher"], "terminal.publisher", 128)
    signer_policy_sha256 = _validated_sha256(
        terminal["signer_policy_sha256"],
        "terminal.signer_policy_sha256",
    )
    lifecycle = _require_mapping(data["lifecycle"], "lifecycle")
    _require_exact_keys(
        lifecycle,
        {
            "schema_version",
            "lifecycle_mode",
            "c012_session_id",
            "launch_control",
            "teardown_control",
            "root_process_generation_policy",
            "allowed_transient_process_policy",
        },
        "lifecycle",
    )
    _require_exact_version(
        lifecycle["schema_version"], 1, "lifecycle.schema_version"
    )
    expected_lifecycle_constants = {
        "lifecycle_mode": C012_LIFECYCLE_MODE,
        "launch_control": "C0",
        "teardown_control": "C2",
        "root_process_generation_policy": C012_ROOT_GENERATION_POLICY,
        "allowed_transient_process_policy": C012_TRANSIENT_POLICY,
    }
    for key, expected_value in expected_lifecycle_constants.items():
        if lifecycle[key] != expected_value:
            raise LabValidationError(f"lifecycle.{key} is invalid")
    c012_session_id = _validated_uuid(
        lifecycle["c012_session_id"], "lifecycle.c012_session_id"
    )
    if uuid.UUID(c012_session_id).version != 4:
        raise LabValidationError("lifecycle.c012_session_id must be UUIDv4")
    validated_lifecycle = {
        "schema_version": 1,
        "lifecycle_mode": C012_LIFECYCLE_MODE,
        "c012_session_id": c012_session_id,
        "launch_control": "C0",
        "teardown_control": "C2",
        "root_process_generation_policy": C012_ROOT_GENERATION_POLICY,
        "allowed_transient_process_policy": C012_TRANSIENT_POLICY,
    }
    requested_server_label = _validated_text(
        data["requested_server_label"], "requested_server_label", 128
    )
    if (
        requested_server_label
        == negative_query_contract(experiment_id)["label"]
    ):
        raise LabValidationError(
            "requested_server_label must differ from the negative query"
        )

    identity = _require_mapping(data["expected_identity"], "expected_identity")
    _require_exact_keys(
        identity, {"server", "company", "trade_mode"}, "expected_identity"
    )
    expected_server = _validated_text(identity["server"], "expected_identity.server", 128)
    expected_company = _validated_text(identity["company"], "expected_identity.company", 128)
    trade_mode = identity["trade_mode"]
    if trade_mode != "DEMO":
        raise LabValidationError(
            "expected_identity.trade_mode must be DEMO; live/contest accounts are out of scope"
        )

    probe = _require_mapping(data["probe"], "probe")
    _require_exact_keys(
        probe,
        {
            "schema_version",
            "probe_version",
            "source_sha256",
            "policy",
            "symbol",
        },
        "probe",
    )
    _require_exact_version(
        probe["schema_version"],
        IDENTITY_PROBE_SCHEMA_VERSION,
        "configured probe schema_version",
    )
    if probe["probe_version"] != IDENTITY_PROBE_VERSION:
        raise LabValidationError("unsupported configured probe_version")
    if probe["policy"] != PROBE_POLICY:
        raise LabValidationError("unsupported configured probe policy")
    validated_probe = {
        "schema_version": IDENTITY_PROBE_SCHEMA_VERSION,
        "probe_version": IDENTITY_PROBE_VERSION,
        "source_sha256": _validated_sha256(
            probe["source_sha256"], "probe.source_sha256"
        ),
        "policy": PROBE_POLICY,
        "symbol": _validated_text(probe["symbol"], "probe.symbol", 64),
    }

    network_policy = _require_mapping(data["network_policy"], "network_policy")
    _require_exact_keys(
        network_policy,
        {
            "transport",
            "allowed_remote_ports",
            "candidate_address_policy",
            "candidate_source_policy",
            "direct_egress_policy",
            "direct_dns_events_max",
            "direct_other_tcp_flows_max",
            "external_deny_role",
        },
        "network_policy",
    )
    if network_policy["transport"] != "TCP":
        raise LabValidationError("network_policy.transport must be TCP")
    raw_allowed_ports = network_policy["allowed_remote_ports"]
    if (
        not isinstance(raw_allowed_ports, list)
        or not raw_allowed_ports
        or len(raw_allowed_ports) > 64
    ):
        raise LabValidationError(
            "network_policy.allowed_remote_ports must be a non-empty bounded list"
        )
    allowed_ports: list[int] = []
    for raw_port in raw_allowed_ports:
        if (
            isinstance(raw_port, bool)
            or not isinstance(raw_port, int)
            or not 1 <= raw_port <= 65535
            or raw_port in DANGEROUS_REMOTE_PORTS
        ):
            raise LabValidationError(
                "network_policy.allowed_remote_ports contains an unsafe port"
            )
        allowed_ports.append(raw_port)
    if allowed_ports != sorted(set(allowed_ports)):
        raise LabValidationError(
            "network_policy.allowed_remote_ports must be sorted and unique"
        )
    expected_network_constants = {
        "candidate_address_policy": "GLOBAL_LITERAL_ONLY",
        "candidate_source_policy": "C2_LOGIN_PROCESS_SCOPED",
        "direct_egress_policy": "CANDIDATE_ONLY",
        "direct_dns_events_max": 0,
        "direct_other_tcp_flows_max": 0,
        "external_deny_role": EXTERNAL_DENY_ROLE,
    }
    for key, expected_value in expected_network_constants.items():
        if network_policy[key] != expected_value:
            raise LabValidationError(f"network_policy.{key} is invalid")
    validated_network_policy = {
        "transport": "TCP",
        "allowed_remote_ports": allowed_ports,
        **expected_network_constants,
    }

    durations = _require_mapping(data["durations_seconds"], "durations_seconds")
    duration_keys = {
        "baseline",
        "negative_discovery",
        "exact_discovery",
        "login_timeout",
        "connected_steady",
        "network_interruption",
        "reconnect_observation",
        "blocked_timeout",
        "c4_elapsed_tolerance",
        "c5_separation_minimum",
        "probe_timestamp_tolerance_seconds",
    }
    _require_exact_keys(durations, duration_keys, "durations_seconds")
    duration_bounds = {
        "baseline": (600, 3600),
        "negative_discovery": (120, 3600),
        "exact_discovery": (180, 3600),
        "login_timeout": (1, 120),
        "connected_steady": (600, 3600),
        "network_interruption": (30, 3600),
        "reconnect_observation": (300, 3600),
        "blocked_timeout": (1, 180),
        "c4_elapsed_tolerance": (0, 30),
        "c5_separation_minimum": (1800, 3600),
        "probe_timestamp_tolerance_seconds": (0, 5),
    }
    clean_durations: dict[str, int] = {}
    for key in sorted(duration_keys):
        value = durations[key]
        minimum, maximum = duration_bounds[key]
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not minimum <= value <= maximum
        ):
            raise LabValidationError(
                f"durations_seconds.{key} must be in {minimum}..{maximum}"
            )
        clean_durations[key] = value

    clean = {
        "schema_version": SCHEMA_VERSION,
        "policy_version": POLICY_VERSION,
        "experiment_id": experiment_id,
        "region": region,
        "lab_root": lab_root,
        "terminal": {
            "path": terminal_path,
            "sha256": terminal_sha,
            "publisher": publisher,
            "signer_policy_sha256": signer_policy_sha256,
        },
        "lifecycle": validated_lifecycle,
        "requested_server_label": requested_server_label,
        "expected_identity": {
            "server": expected_server,
            "company": expected_company,
            "trade_mode": trade_mode,
        },
        "probe": validated_probe,
        "network_policy": validated_network_policy,
        "durations_seconds": clean_durations,
    }
    reject_sensitive_content(clean)
    return clean


def build_experiment_manifest(config_payload: object) -> dict[str, object]:
    """Build the immutable pre-candidate experiment manifest."""

    config = validate_config(config_payload)
    terminal = config["terminal"]
    assert isinstance(terminal, Mapping)
    body: dict[str, object] = {
        "schema_version": EXPERIMENT_MANIFEST_SCHEMA_VERSION,
        "artifact_type": "EXPERIMENT_MANIFEST",
        "policy_version": POLICY_VERSION,
        "experiment_id": config["experiment_id"],
        "region": config["region"],
        "lab_root": config["lab_root"],
        "terminal": {
            "source_canonical_path": terminal["path"],
            "source_path_sha256": windows_path_digest(
                terminal["path"], "terminal.path"
            ),
            "sha256": terminal["sha256"],
            "publisher": terminal["publisher"],
            "publisher_policy": "EXACT_PUBLISHER_AND_SIGNER_POLICY",
            "signer_policy_sha256": terminal["signer_policy_sha256"],
        },
        "lifecycle": config["lifecycle"],
        "negative_query": negative_query_contract(config["experiment_id"]),
        "requested_server_label": config["requested_server_label"],
        "expected_identity": config["expected_identity"],
        "durations_seconds": config["durations_seconds"],
        "probe": config["probe"],
        "network_policy": config["network_policy"],
    }
    manifest = {
        **body,
        "experiment_manifest_sha256": contract_digest(
            "EXPERIMENT_MANIFEST",
            EXPERIMENT_MANIFEST_SCHEMA_VERSION,
            body,
        ),
    }
    reject_sensitive_content(manifest)
    return manifest


def validate_experiment_manifest(payload: object) -> dict[str, object]:
    data = _require_mapping(payload, "experiment manifest")
    required = {
        "schema_version",
        "artifact_type",
        "policy_version",
        "experiment_id",
        "region",
        "lab_root",
        "terminal",
        "lifecycle",
        "negative_query",
        "requested_server_label",
        "expected_identity",
        "durations_seconds",
        "probe",
        "network_policy",
        "experiment_manifest_sha256",
    }
    _require_exact_keys(data, required, "experiment manifest")
    _require_exact_version(
        data["schema_version"],
        EXPERIMENT_MANIFEST_SCHEMA_VERSION,
        "experiment manifest schema_version",
    )
    if (
        data["artifact_type"] != "EXPERIMENT_MANIFEST"
        or data["policy_version"] != POLICY_VERSION
    ):
        raise LabValidationError("unsupported experiment manifest contract")

    terminal = _require_mapping(data["terminal"], "experiment manifest terminal")
    _require_exact_keys(
        terminal,
        {
            "source_canonical_path",
            "source_path_sha256",
            "sha256",
            "publisher",
            "publisher_policy",
            "signer_policy_sha256",
        },
        "experiment manifest terminal",
    )
    if terminal["publisher_policy"] != "EXACT_PUBLISHER_AND_SIGNER_POLICY":
        raise LabValidationError("experiment manifest publisher policy is invalid")

    # Reuse the config validator so manifest/config policy cannot drift.  The
    # manifest deliberately has no candidate field.
    reconstructed_config = {
        "schema_version": CONFIG_SCHEMA_VERSION,
        "policy_version": POLICY_VERSION,
        "experiment_id": data["experiment_id"],
        "region": data["region"],
        "lab_root": data["lab_root"],
        "terminal": {
            "path": terminal["source_canonical_path"],
            "sha256": terminal["sha256"],
            "publisher": terminal["publisher"],
            "signer_policy_sha256": terminal["signer_policy_sha256"],
        },
        "lifecycle": data["lifecycle"],
        "requested_server_label": data["requested_server_label"],
        "expected_identity": data["expected_identity"],
        "probe": data["probe"],
        "network_policy": data["network_policy"],
        "durations_seconds": data["durations_seconds"],
    }
    clean_config = validate_config(reconstructed_config)
    expected = build_experiment_manifest(clean_config)
    if canonical_json(data["negative_query"]) != canonical_json(
        expected["negative_query"]
    ):
        raise LabValidationError("experiment manifest negative query is invalid")
    if data["experiment_manifest_sha256"] != expected[
        "experiment_manifest_sha256"
    ]:
        raise LabValidationError("experiment manifest digest mismatch")
    if terminal["source_path_sha256"] != expected["terminal"][
        "source_path_sha256"
    ]:
        raise LabValidationError("experiment manifest source path digest mismatch")
    # Exact canonical equality catches uncommitted fields as well as digest
    # changes with a recomputed but semantically different object.
    if canonical_json(data) != canonical_json(expected):
        raise LabValidationError("experiment manifest body is not canonical")
    return expected


def _resolve_experiment_manifest(
    *,
    config_payload: object | None,
    manifest_payload: object | None,
) -> dict[str, object] | None:
    if manifest_payload is None and config_payload is None:
        return None
    if manifest_payload is None:
        return build_experiment_manifest(config_payload)
    manifest = validate_experiment_manifest(manifest_payload)
    if config_payload is not None:
        config_manifest = build_experiment_manifest(config_payload)
        if (
            manifest["experiment_manifest_sha256"]
            != config_manifest["experiment_manifest_sha256"]
        ):
            raise LabValidationError(
                "config does not match the supplied experiment manifest"
            )
    return manifest


def build_control_plan(
    config: object,
    control: str,
    candidate_endpoint: object | None = None,
    candidate_handoff: object | None = None,
    *,
    run_id: str | None = None,
) -> dict[str, object]:
    clean = validate_config(config)
    manifest = build_experiment_manifest(clean)
    if control not in CONTROLS:
        raise LabValidationError("control must be one of C0..C5")
    handoff: dict[str, object] | None = None
    candidate: dict[str, object] | None = None
    if control in {"C0", "C1", "C2"}:
        if candidate_endpoint is not None or candidate_handoff is not None:
            raise LabValidationError(
                f"{control} cannot receive a candidate or candidate handoff"
            )
    else:
        if candidate_handoff is None:
            raise LabValidationError(
                f"{control} requires an authoritative candidate handoff"
            )
        handoff = validate_candidate_handoff(candidate_handoff)
        if handoff["experiment_manifest_sha256"] != manifest[
            "experiment_manifest_sha256"
        ]:
            raise LabValidationError(
                "candidate handoff does not match the experiment manifest"
            )
        candidate = validate_candidate(handoff["candidate_endpoint"])
        if candidate_endpoint is not None:
            supplied = validate_candidate(candidate_endpoint)
            if canonical_json(supplied) != canonical_json(candidate):
                raise LabValidationError(
                    "candidate endpoint does not match candidate handoff"
                )
    normalized_run_id = (
        str(uuid.uuid4())
        if run_id is None
        else _validated_uuid(run_id, "control plan run_id")
    )
    if candidate is not None and int(candidate["port"]) not in clean[
        "network_policy"
    ]["allowed_remote_ports"]:  # type: ignore[index]
        raise LabValidationError(
            "candidate port is not committed by network_policy.allowed_remote_ports"
        )

    experiment_root = PureWindowsPath(str(clean["lab_root"])) / str(clean["experiment_id"])
    cohort = "C012" if control in {"C0", "C1", "C2"} else control
    cohort_root = experiment_root / cohort
    run_root = experiment_root / "runs" / normalized_run_id / control
    terminal = cohort_root / "terminal" / "terminal64.exe"
    terminal_data_path = terminal.parent
    private_config = run_root / "private" / "startup.ini"
    raw = run_root / "raw"
    sanitized = run_root / "sanitized"

    negative_query = (
        manifest["negative_query"] if control == "C1" else None
    )
    actions = _control_plan_actions(
        control,
        candidate,
        raw_path=str(raw),
        sanitized_path=str(sanitized),
        negative_query=(
            negative_query if isinstance(negative_query, Mapping) else None
        ),
    )
    lifecycle_control = _lifecycle_control_contract(
        control,
        clean["lifecycle"],  # type: ignore[arg-type]
    )
    portable_root_path_sha256 = windows_path_digest(
        str(terminal_data_path),
        "control plan terminal data path",
    )
    if control in {"C0", "C1", "C2"}:
        lifecycle = clean["lifecycle"]
        assert isinstance(lifecycle, Mapping)
        initial_body = initial_c012_pre_state_body(
            experiment_id=clean["experiment_id"],
            c012_session_id=lifecycle["c012_session_id"],
            portable_root_path_sha256=portable_root_path_sha256,
        )
        initial_pre_state_binding = {
            "schema_version": 1,
            "scope": (
                "C012_INITIAL" if control == "C0" else "C012_REFERENCE"
            ),
            "portable_root_path_sha256": portable_root_path_sha256,
            "initial_c012_pre_state_sha256": (
                initial_c012_pre_state_digest(initial_body)
            ),
        }
    else:
        initial_pre_state_binding = {
            "schema_version": 1,
            "scope": "COLD_BOOT_INITIAL",
            "portable_root_path_sha256": portable_root_path_sha256,
            "initial_c012_pre_state_sha256": None,
        }

    duration_contract = _duration_contract(
        control, clean["durations_seconds"]  # type: ignore[arg-type]
    )
    required_phase_codes = list(_required_phase_markers(control))
    body: dict[str, object] = {
        "schema_version": CONTROL_PLAN_SCHEMA_VERSION,
        "artifact_type": "CONTROL_PLAN",
        "plan_id": str(uuid.uuid4()),
        "experiment_id": clean["experiment_id"],
        "run_id": normalized_run_id,
        "control": control,
        "experiment_manifest_sha256": manifest[
            "experiment_manifest_sha256"
        ],
        "candidate_handoff_manifest_sha256": (
            None
            if handoff is None
            else handoff["candidate_handoff_manifest_sha256"]
        ),
        "direct_campaign_manifest_sha256": (
            None
            if handoff is None
            else handoff["direct_campaign_manifest_sha256"]
        ),
        "lifecycle_control": lifecycle_control,
        "initial_pre_state_binding": initial_pre_state_binding,
        "negative_query": negative_query,
        "safety": {
            "plan_only": True,
            "mt5_start_enabled": False,
            "firewall_apply_enabled": False,
            "credential_access_enabled": False,
            "registry_promotion_enabled": False,
        },
        "paths": {
            "experiment_root": str(experiment_root),
            "cohort_root": str(cohort_root),
            "run_root": str(run_root),
            "source_terminal": str(clean["terminal"]["path"]),
            "terminal": str(terminal),
            "private_config": str(private_config),
            "raw": str(raw),
            "sanitized": str(sanitized),
        },
        "path_bindings": {
            "source_terminal_path_sha256": manifest["terminal"][
                "source_path_sha256"
            ],  # type: ignore[index]
            "terminal_path_sha256": windows_path_digest(
                str(terminal), "control plan terminal path"
            ),
            "terminal_data_path_sha256": windows_path_digest(
                str(terminal_data_path), "control plan terminal data path"
            ),
        },
        "terminal_command": (
            [str(terminal), "/portable"]
            if control == "C0"
            else (
                None
                if control == "C1"
                else [
                    str(terminal),
                    "/portable",
                    f"/config:{private_config}",
                ]
            )
        ),
        "candidate_endpoint": candidate,
        "cohort": cohort,
        "requested_server_label_sha256": evidence_digest(
            {"requested_server_label": clean["requested_server_label"]}
        ),
        "expected_identity": clean["expected_identity"],
        "probe": clean["probe"],
        "network_policy": clean["network_policy"],
        "duration_contract": duration_contract,
        "required_phase_codes": required_phase_codes,
        "actions": actions,
    }
    plan = {
        **body,
        "control_plan_sha256": contract_digest(
            "CONTROL_PLAN",
            CONTROL_PLAN_SCHEMA_VERSION,
            body,
        ),
    }
    reject_sensitive_content(plan)
    return plan


def validate_control_plan(
    payload: object,
    *,
    manifest_payload: object | None = None,
    candidate_handoff: object | None = None,
) -> dict[str, object]:
    data = _require_mapping(payload, "control plan")
    required = {
        "schema_version",
        "artifact_type",
        "plan_id",
        "experiment_id",
        "run_id",
        "control",
        "experiment_manifest_sha256",
        "candidate_handoff_manifest_sha256",
        "direct_campaign_manifest_sha256",
        "lifecycle_control",
        "initial_pre_state_binding",
        "negative_query",
        "safety",
        "paths",
        "path_bindings",
        "terminal_command",
        "candidate_endpoint",
        "cohort",
        "requested_server_label_sha256",
        "expected_identity",
        "probe",
        "network_policy",
        "duration_contract",
        "required_phase_codes",
        "actions",
        "control_plan_sha256",
    }
    _require_exact_keys(data, required, "control plan")
    _require_exact_version(
        data["schema_version"],
        CONTROL_PLAN_SCHEMA_VERSION,
        "control plan schema_version",
    )
    if data["artifact_type"] != "CONTROL_PLAN":
        raise LabValidationError("unsupported control plan contract")
    _validated_uuid(data["plan_id"], "control plan plan_id")
    _validated_uuid(data["experiment_id"], "control plan experiment_id")
    _validated_uuid(data["run_id"], "control plan run_id")
    control = data["control"]
    if control not in CONTROLS:
        raise LabValidationError("control plan control is invalid")
    expected_cohort = "C012" if control in {"C0", "C1", "C2"} else control
    if data["cohort"] != expected_cohort:
        raise LabValidationError("control plan cohort is invalid")

    manifest_digest = _validated_sha256(
        data["experiment_manifest_sha256"],
        "control plan experiment_manifest_sha256",
    )
    handoff_digest = _validated_nullable_sha256(
        data["candidate_handoff_manifest_sha256"],
        "control plan candidate_handoff_manifest_sha256",
    )
    direct_manifest_digest = _validated_nullable_sha256(
        data["direct_campaign_manifest_sha256"],
        "control plan direct_campaign_manifest_sha256",
    )
    manifest = (
        None
        if manifest_payload is None
        else validate_experiment_manifest(manifest_payload)
    )
    manifest_lifecycle = (
        None if manifest is None else manifest["lifecycle"]
    )
    assert manifest_lifecycle is None or isinstance(
        manifest_lifecycle, Mapping
    )
    lifecycle_control = _validate_lifecycle_control(
        data["lifecycle_control"], str(control), manifest_lifecycle
    )
    initial_pre_state_binding = _validate_initial_pre_state_binding(
        data["initial_pre_state_binding"], str(control)
    )
    negative_query = (
        None
        if data["negative_query"] is None
        else _validate_negative_query(data["negative_query"])
    )
    if control == "C1":
        if negative_query is None:
            raise LabValidationError("C1 control plan requires negative_query")
    elif negative_query is not None:
        raise LabValidationError(
            f"{control} control plan cannot contain negative_query"
        )
    safety = _require_bool_mapping(
        data["safety"],
        {
            "plan_only",
            "mt5_start_enabled",
            "firewall_apply_enabled",
            "credential_access_enabled",
            "registry_promotion_enabled",
        },
        "control plan safety",
    )
    if safety != {
        "plan_only": True,
        "mt5_start_enabled": False,
        "firewall_apply_enabled": False,
        "credential_access_enabled": False,
        "registry_promotion_enabled": False,
    }:
        raise LabValidationError("control plan safety capabilities are not hard-disabled")

    paths = _require_mapping(data["paths"], "control plan paths")
    _require_exact_keys(
        paths,
        {
            "experiment_root",
            "cohort_root",
            "run_root",
            "source_terminal",
            "terminal",
            "private_config",
            "raw",
            "sanitized",
        },
        "control plan paths",
    )
    clean_paths = {
        key: _validated_windows_absolute_path(
            paths[key], f"control plan paths.{key}"
        )
        for key in sorted(paths)
    }
    if dict(paths) != clean_paths:
        raise LabValidationError("control plan paths are not canonical")
    if PureWindowsPath(clean_paths["terminal"]).name.casefold() != "terminal64.exe":
        raise LabValidationError("control plan target terminal path is invalid")
    if PureWindowsPath(clean_paths["source_terminal"]).name.casefold() != "terminal64.exe":
        raise LabValidationError("control plan source terminal path is invalid")

    path_bindings = _require_mapping(
        data["path_bindings"], "control plan path_bindings"
    )
    _require_exact_keys(
        path_bindings,
        {
            "source_terminal_path_sha256",
            "terminal_path_sha256",
            "terminal_data_path_sha256",
        },
        "control plan path_bindings",
    )
    clean_path_bindings = {
        key: _validated_sha256(
            path_bindings[key], f"control plan path_bindings.{key}"
        )
        for key in sorted(path_bindings)
    }
    if clean_path_bindings["source_terminal_path_sha256"] != windows_path_digest(
        clean_paths["source_terminal"], "control plan source terminal"
    ):
        raise LabValidationError("control plan source terminal path binding mismatch")
    if clean_path_bindings["terminal_path_sha256"] != windows_path_digest(
        clean_paths["terminal"], "control plan target terminal"
    ):
        raise LabValidationError("control plan terminal path binding mismatch")
    if clean_path_bindings["terminal_data_path_sha256"] != windows_path_digest(
        str(PureWindowsPath(clean_paths["terminal"]).parent),
        "control plan terminal data path",
    ):
        raise LabValidationError("control plan terminal data path binding mismatch")
    if (
        initial_pre_state_binding["portable_root_path_sha256"]
        != clean_path_bindings["terminal_data_path_sha256"]
    ):
        raise LabValidationError(
            "control plan initial pre-state portable-root binding mismatch"
        )

    command = data["terminal_command"]
    expected_command = (
        [clean_paths["terminal"], "/portable"]
        if control == "C0"
        else (
            None
            if control == "C1"
            else [
                clean_paths["terminal"],
                "/portable",
                f"/config:{clean_paths['private_config']}",
            ]
        )
    )
    if command != expected_command:
        raise LabValidationError("control plan terminal command is not canonical")

    candidate = (
        None
        if data["candidate_endpoint"] is None
        else validate_candidate(data["candidate_endpoint"])
    )
    if control in {"C0", "C1", "C2"}:
        if candidate is not None or handoff_digest is not None or direct_manifest_digest is not None:
            raise LabValidationError(
                f"{control} control plan cannot bind a candidate handoff"
            )
    else:
        if candidate is None or handoff_digest is None or direct_manifest_digest is None:
            raise LabValidationError(
                f"{control} control plan requires candidate/handoff bindings"
            )
    requested_digest = _validated_sha256(
        data["requested_server_label_sha256"],
        "control plan requested_server_label_sha256",
    )
    expected_identity = _validate_expected_identity(data["expected_identity"])
    probe = _validate_config_probe(data["probe"])
    network_policy = _validate_network_policy(data["network_policy"])
    durations = _validate_durations(
        data["duration_contract"], "control plan duration_contract"
    )
    required_codes = data["required_phase_codes"]
    if required_codes != list(_required_phase_markers(str(control))):
        raise LabValidationError("control plan required phase sequence mismatch")
    expected_actions = _control_plan_actions(
        str(control),
        candidate,
        raw_path=clean_paths["raw"],
        sanitized_path=clean_paths["sanitized"],
        negative_query=negative_query,
    )
    if canonical_json(data["actions"]) != canonical_json(expected_actions):
        raise LabValidationError(
            "control plan actions differ from the committed policy"
        )
    action_phase_codes = [
        str(action["marker"])
        for action in data["actions"]
        if isinstance(action, Mapping)
        and action.get("action") == "mark_phase"
        and "marker" in action
    ]
    if action_phase_codes != list(_required_phase_markers(str(control))):
        raise LabValidationError(
            "control plan marker actions do not match required phase sequence"
        )

    if manifest is not None:
        if manifest_digest != manifest["experiment_manifest_sha256"]:
            raise LabValidationError("control plan experiment manifest mismatch")
        if data["experiment_id"] != manifest["experiment_id"]:
            raise LabValidationError("control plan experiment ID mismatch")
        expected_experiment_root = str(
            PureWindowsPath(str(manifest["lab_root"]))
            / str(manifest["experiment_id"])
        )
        expected_cohort_root = str(
            PureWindowsPath(expected_experiment_root) / expected_cohort
        )
        expected_run_root = str(
            PureWindowsPath(expected_experiment_root)
            / "runs"
            / str(data["run_id"])
            / str(control)
        )
        expected_layout = {
            "experiment_root": expected_experiment_root,
            "cohort_root": expected_cohort_root,
            "run_root": expected_run_root,
            "source_terminal": manifest["terminal"]["source_canonical_path"],
            "terminal": str(
                PureWindowsPath(expected_cohort_root)
                / "terminal"
                / "terminal64.exe"
            ),
            "private_config": str(
                PureWindowsPath(expected_run_root) / "private" / "startup.ini"
            ),
            "raw": str(PureWindowsPath(expected_run_root) / "raw"),
            "sanitized": str(PureWindowsPath(expected_run_root) / "sanitized"),
        }
        if clean_paths != expected_layout:
            raise LabValidationError("control plan path layout differs from manifest")
        if clean_paths["source_terminal"] != manifest["terminal"][
            "source_canonical_path"
        ]:
            raise LabValidationError("control plan source terminal differs from manifest")
        if clean_path_bindings["source_terminal_path_sha256"] != manifest[
            "terminal"
        ]["source_path_sha256"]:
            raise LabValidationError("control plan source path digest differs from manifest")
        if expected_identity != manifest["expected_identity"]:
            raise LabValidationError("control plan expected identity differs from manifest")
        if probe != manifest["probe"]:
            raise LabValidationError("control plan probe differs from manifest")
        if network_policy != manifest["network_policy"]:
            raise LabValidationError("control plan network policy differs from manifest")
        if (
            candidate is not None
            and int(candidate["port"])
            not in manifest["network_policy"]["allowed_remote_ports"]
        ):
            raise LabValidationError("control plan candidate port is not allowed")
        if durations != manifest["durations_seconds"]:
            raise LabValidationError("control plan durations differ from manifest")
        if control in {"C0", "C1", "C2"}:
            expected_initial_body = initial_c012_pre_state_body(
                experiment_id=manifest["experiment_id"],
                c012_session_id=manifest["lifecycle"][
                    "c012_session_id"
                ],  # type: ignore[index]
                portable_root_path_sha256=clean_path_bindings[
                    "terminal_data_path_sha256"
                ],
            )
            if (
                initial_pre_state_binding[
                    "initial_c012_pre_state_sha256"
                ]
                != initial_c012_pre_state_digest(expected_initial_body)
            ):
                raise LabValidationError(
                    "control plan initial C012 pre-state digest mismatch"
                )
        expected_negative_query = (
            manifest["negative_query"] if control == "C1" else None
        )
        if canonical_json(negative_query) != canonical_json(
            expected_negative_query
        ):
            raise LabValidationError(
                "control plan negative query differs from manifest"
            )
        expected_requested_digest = evidence_digest(
            {"requested_server_label": manifest["requested_server_label"]}
        )
        if requested_digest != expected_requested_digest:
            raise LabValidationError("control plan requested label differs from manifest")

    if candidate_handoff is not None:
        handoff = validate_candidate_handoff(candidate_handoff)
        if control in {"C0", "C1", "C2"}:
            raise LabValidationError("early control plan cannot receive a handoff")
        if handoff_digest != handoff["candidate_handoff_manifest_sha256"]:
            raise LabValidationError("control plan candidate handoff digest mismatch")
        if direct_manifest_digest != handoff["direct_campaign_manifest_sha256"]:
            raise LabValidationError("control plan direct campaign digest mismatch")
        if canonical_json(candidate) != canonical_json(handoff["candidate_endpoint"]):
            raise LabValidationError("control plan candidate differs from handoff")

    body = {key: data[key] for key in data if key != "control_plan_sha256"}
    expected_plan_digest = contract_digest(
        "CONTROL_PLAN", CONTROL_PLAN_SCHEMA_VERSION, body
    )
    if data["control_plan_sha256"] != expected_plan_digest:
        raise LabValidationError("control plan digest mismatch")
    reject_sensitive_content(data)
    return dict(data)


def build_direct_campaign_manifest(
    config_payload: object,
    c2_evidence_payload: object,
    c2_control_plan_payload: object,
) -> dict[str, object]:
    manifest = build_experiment_manifest(config_payload)
    c2_plan = validate_control_plan(
        c2_control_plan_payload,
        manifest_payload=manifest,
    )
    c2_evidence = validate_evidence(c2_evidence_payload)
    if c2_plan["control"] != "C2" or c2_evidence["control"] != "C2":
        raise LabValidationError("direct campaign manifest requires C2 artifacts")
    c2_proof = c2_evidence["proof_binding"]
    c2_context = c2_evidence["run_context"]
    assert isinstance(c2_proof, Mapping)
    assert isinstance(c2_context, Mapping)
    if c2_evidence["run_id"] != c2_plan["run_id"]:
        raise LabValidationError("C2 plan/evidence run mismatch")
    if (
        c2_proof["experiment_manifest_sha256"]
        != manifest["experiment_manifest_sha256"]
        or c2_proof["control_plan_sha256"]
        != c2_plan["control_plan_sha256"]
    ):
        raise LabValidationError("C2 evidence is not bound to manifest/control plan")
    c2_provenance = c2_proof["provenance"]
    assert isinstance(c2_provenance, Mapping)
    c2_result = _evaluate_validated_evidence(
        c2_evidence,
        config_manifest=manifest,
        control_plan=c2_plan,
        candidate_handoff=None,
        campaign_handoff_verified=False,
        allow_synthetic=c2_provenance["origin"] == "SYNTHETIC_FIXTURE",
        campaign_c3_completed_at_unix_ms=None,
    )
    if c2_result.outcome != "SYNTHETIC_PASS":
        raise LabValidationError(
            "candidate handoff requires a positively evaluated C2 fixture; "
            "captured handoff remains disabled pending the independent verifier"
        )
    candidate = c2_context["candidate_endpoint"]
    identity = c2_evidence["identity"]
    if candidate is None or not isinstance(identity, Mapping):
        raise LabValidationError("C2 evidence lacks candidate or canonical identity")
    if int(candidate["port"]) not in manifest["network_policy"][
        "allowed_remote_ports"
    ]:
        raise LabValidationError("C2 candidate port is outside manifest policy")

    c2_evidence_sha256 = contract_digest(
        "EVIDENCE",
        EVIDENCE_SCHEMA_VERSION,
        c2_evidence,
    )
    terminal = {
        "sha256": c2_context["terminal_sha256"],
        "build": c2_context["terminal_build"],
    }
    produced_at = int(c2_evidence["timeline"]["events"][-1]["timestamp_unix_ms"])
    descriptors = {
        control: _direct_control_descriptor(
            control,
            manifest["durations_seconds"],  # type: ignore[arg-type]
        )
        for control in ("C3", "C4", "C5")
    }
    body: dict[str, object] = {
        "schema_version": DIRECT_CAMPAIGN_MANIFEST_SCHEMA_VERSION,
        "artifact_type": "DIRECT_CAMPAIGN_MANIFEST",
        "policy_version": POLICY_VERSION,
        "experiment_id": manifest["experiment_id"],
        "experiment_manifest_sha256": manifest[
            "experiment_manifest_sha256"
        ],
        "c2_run_id": c2_evidence["run_id"],
        "c2_control_plan_sha256": c2_plan["control_plan_sha256"],
        "c2_evidence_sha256": c2_evidence_sha256,
        "candidate_endpoint": candidate,
        "canonical_identity": {
            "server": identity["server"],
            "company": identity["company"],
            "trade_mode": identity["trade_mode"],
        },
        "terminal": terminal,
        "produced_at_unix_ms": produced_at,
        "canonical_order": ["C3", "C4", "C5"],
        "temporal_policy": {
            "schema_version": 1,
            "ordering_policy": "STRICT_NON_OVERLAPPING",
            "c5_separation_anchor": "C3_COMPLETED_AT_UNIX",
            "c5_separation_minimum_seconds": manifest[
                "durations_seconds"
            ]["c5_separation_minimum"],  # type: ignore[index]
        },
        "controls": descriptors,
    }
    return {
        **body,
        "direct_campaign_manifest_sha256": contract_digest(
            "DIRECT_CAMPAIGN_MANIFEST",
            DIRECT_CAMPAIGN_MANIFEST_SCHEMA_VERSION,
            body,
        ),
    }


def validate_direct_campaign_manifest(
    payload: object,
    *,
    manifest_payload: object | None = None,
) -> dict[str, object]:
    data = _require_mapping(payload, "direct campaign manifest")
    required = {
        "schema_version",
        "artifact_type",
        "policy_version",
        "experiment_id",
        "experiment_manifest_sha256",
        "c2_run_id",
        "c2_control_plan_sha256",
        "c2_evidence_sha256",
        "candidate_endpoint",
        "canonical_identity",
        "terminal",
        "produced_at_unix_ms",
        "canonical_order",
        "temporal_policy",
        "controls",
        "direct_campaign_manifest_sha256",
    }
    _require_exact_keys(data, required, "direct campaign manifest")
    _require_exact_version(
        data["schema_version"],
        DIRECT_CAMPAIGN_MANIFEST_SCHEMA_VERSION,
        "direct campaign manifest schema_version",
    )
    if (
        data["artifact_type"] != "DIRECT_CAMPAIGN_MANIFEST"
        or data["policy_version"] != POLICY_VERSION
    ):
        raise LabValidationError("unsupported direct campaign manifest")
    _validated_uuid(data["experiment_id"], "direct campaign experiment_id")
    _validated_uuid(data["c2_run_id"], "direct campaign c2_run_id")
    for key in (
        "experiment_manifest_sha256",
        "c2_control_plan_sha256",
        "c2_evidence_sha256",
    ):
        _validated_sha256(data[key], f"direct campaign {key}")
    candidate = validate_candidate(data["candidate_endpoint"])
    identity = _validate_expected_identity(data["canonical_identity"])
    terminal = _require_mapping(data["terminal"], "direct campaign terminal")
    _require_exact_keys(terminal, {"sha256", "build"}, "direct campaign terminal")
    _validated_sha256(terminal["sha256"], "direct campaign terminal.sha256")
    if (
        isinstance(terminal["build"], bool)
        or not isinstance(terminal["build"], int)
        or terminal["build"] < 1
    ):
        raise LabValidationError("direct campaign terminal build is invalid")
    produced_at = data["produced_at_unix_ms"]
    if isinstance(produced_at, bool) or not isinstance(produced_at, int) or produced_at < 1:
        raise LabValidationError("direct campaign produced_at_unix_ms is invalid")
    if data["canonical_order"] != ["C3", "C4", "C5"]:
        raise LabValidationError("direct campaign canonical order is invalid")
    temporal_policy = _require_mapping(
        data["temporal_policy"], "direct campaign temporal_policy"
    )
    _require_exact_keys(
        temporal_policy,
        {
            "schema_version",
            "ordering_policy",
            "c5_separation_anchor",
            "c5_separation_minimum_seconds",
        },
        "direct campaign temporal_policy",
    )
    _require_exact_version(
        temporal_policy["schema_version"],
        1,
        "direct campaign temporal_policy.schema_version",
    )
    if (
        temporal_policy["ordering_policy"] != "STRICT_NON_OVERLAPPING"
        or temporal_policy["c5_separation_anchor"]
        != "C3_COMPLETED_AT_UNIX"
    ):
        raise LabValidationError("direct campaign temporal policy is invalid")
    temporal_minimum = temporal_policy["c5_separation_minimum_seconds"]
    if (
        isinstance(temporal_minimum, bool)
        or not isinstance(temporal_minimum, int)
        or not 1800 <= temporal_minimum <= 3600
    ):
        raise LabValidationError(
            "direct campaign temporal minimum is invalid"
        )
    controls = _require_mapping(data["controls"], "direct campaign controls")
    _require_exact_keys(controls, {"C3", "C4", "C5"}, "direct campaign controls")
    for control in ("C3", "C4", "C5"):
        _validate_direct_control_descriptor(controls[control], control)

    if manifest_payload is not None:
        manifest = validate_experiment_manifest(manifest_payload)
        if data["experiment_manifest_sha256"] != manifest[
            "experiment_manifest_sha256"
        ]:
            raise LabValidationError("direct campaign experiment manifest mismatch")
        if data["experiment_id"] != manifest["experiment_id"]:
            raise LabValidationError("direct campaign experiment ID mismatch")
        if identity != manifest["expected_identity"]:
            raise LabValidationError("direct campaign identity differs from manifest")
        if terminal["sha256"] != manifest["terminal"]["sha256"]:
            raise LabValidationError("direct campaign terminal hash differs from manifest")
        if int(candidate["port"]) not in manifest["network_policy"][
            "allowed_remote_ports"
        ]:
            raise LabValidationError("direct campaign candidate port is not allowed")
        for control in ("C3", "C4", "C5"):
            expected_descriptor = _direct_control_descriptor(
                control,
                manifest["durations_seconds"],  # type: ignore[arg-type]
            )
            if canonical_json(controls[control]) != canonical_json(expected_descriptor):
                raise LabValidationError(
                    "direct campaign descriptor differs from manifest policy"
                )
        if (
            temporal_minimum
            != manifest["durations_seconds"]["c5_separation_minimum"]
        ):
            raise LabValidationError(
                "direct campaign temporal policy differs from manifest"
            )
    body = {
        key: data[key]
        for key in data
        if key != "direct_campaign_manifest_sha256"
    }
    expected_digest = contract_digest(
        "DIRECT_CAMPAIGN_MANIFEST",
        DIRECT_CAMPAIGN_MANIFEST_SCHEMA_VERSION,
        body,
    )
    if data["direct_campaign_manifest_sha256"] != expected_digest:
        raise LabValidationError("direct campaign manifest digest mismatch")
    reject_sensitive_content(data)
    return dict(data)


def build_candidate_handoff(
    config_payload: object,
    c2_evidence_payload: object,
    c2_control_plan_payload: object,
    *,
    direct_campaign_manifest: object | None = None,
) -> dict[str, object]:
    manifest = build_experiment_manifest(config_payload)
    c2_evidence = validate_evidence(c2_evidence_payload)
    expected_direct = build_direct_campaign_manifest(
        config_payload,
        c2_evidence_payload,
        c2_control_plan_payload,
    )
    if direct_campaign_manifest is None:
        direct = expected_direct
    else:
        direct = validate_direct_campaign_manifest(
            direct_campaign_manifest,
            manifest_payload=manifest,
        )
        if canonical_json(direct) != canonical_json(expected_direct):
            raise LabValidationError(
                "direct campaign manifest does not match the supplied C2 artifacts"
            )
    terminal = direct["terminal"]
    assert isinstance(terminal, Mapping)
    c2_lifecycle = c2_evidence["lifecycle_binding"]
    c2_initial_pre_state = c2_evidence["initial_pre_state_binding"]
    c2_proof = c2_evidence["proof_binding"]
    assert isinstance(c2_lifecycle, Mapping)
    assert isinstance(c2_initial_pre_state, Mapping)
    assert isinstance(c2_proof, Mapping)
    body: dict[str, object] = {
        "schema_version": CANDIDATE_HANDOFF_SCHEMA_VERSION,
        "artifact_type": "CANDIDATE_HANDOFF",
        "policy_version": POLICY_VERSION,
        "experiment_id": direct["experiment_id"],
        "experiment_manifest_sha256": direct[
            "experiment_manifest_sha256"
        ],
        "c2_run_id": direct["c2_run_id"],
        "c2_control_plan_sha256": direct["c2_control_plan_sha256"],
        "c2_evidence_sha256": direct["c2_evidence_sha256"],
        "lifecycle_mode": c2_lifecycle["lifecycle_mode"],
        "c012_session_id": c2_lifecycle["c012_session_id"],
        "initial_c012_pre_state_sha256": c2_initial_pre_state[
            "initial_c012_pre_state_sha256"
        ],
        "c2_lifecycle_binding_sha256": c2_proof[
            "lifecycle_binding_sha256"
        ],
        "candidate_endpoint": direct["candidate_endpoint"],
        "canonical_identity": direct["canonical_identity"],
        "terminal_sha256": terminal["sha256"],
        "terminal_build": terminal["build"],
        "produced_at_unix_ms": direct["produced_at_unix_ms"],
        "direct_campaign_manifest_sha256": direct[
            "direct_campaign_manifest_sha256"
        ],
    }
    return {
        **body,
        "candidate_handoff_manifest_sha256": contract_digest(
            "CANDIDATE_HANDOFF",
            CANDIDATE_HANDOFF_SCHEMA_VERSION,
            body,
        ),
    }


def validate_candidate_handoff(
    payload: object,
    *,
    manifest_payload: object | None = None,
    direct_campaign_manifest: object | None = None,
) -> dict[str, object]:
    data = _require_mapping(payload, "candidate handoff")
    required = {
        "schema_version",
        "artifact_type",
        "policy_version",
        "experiment_id",
        "experiment_manifest_sha256",
        "c2_run_id",
        "c2_control_plan_sha256",
        "c2_evidence_sha256",
        "lifecycle_mode",
        "c012_session_id",
        "initial_c012_pre_state_sha256",
        "c2_lifecycle_binding_sha256",
        "candidate_endpoint",
        "canonical_identity",
        "terminal_sha256",
        "terminal_build",
        "produced_at_unix_ms",
        "direct_campaign_manifest_sha256",
        "candidate_handoff_manifest_sha256",
    }
    _require_exact_keys(data, required, "candidate handoff")
    _require_exact_version(
        data["schema_version"],
        CANDIDATE_HANDOFF_SCHEMA_VERSION,
        "candidate handoff schema_version",
    )
    if (
        data["artifact_type"] != "CANDIDATE_HANDOFF"
        or data["policy_version"] != POLICY_VERSION
    ):
        raise LabValidationError("unsupported candidate handoff contract")
    _validated_uuid(data["experiment_id"], "candidate handoff experiment_id")
    _validated_uuid(data["c2_run_id"], "candidate handoff c2_run_id")
    c012_session_id = _validated_uuid(
        data["c012_session_id"], "candidate handoff c012_session_id"
    )
    if data["lifecycle_mode"] != C012_LIFECYCLE_MODE:
        raise LabValidationError("candidate handoff lifecycle mode is invalid")
    for key in (
        "experiment_manifest_sha256",
        "c2_control_plan_sha256",
        "c2_evidence_sha256",
        "terminal_sha256",
        "direct_campaign_manifest_sha256",
        "initial_c012_pre_state_sha256",
        "c2_lifecycle_binding_sha256",
    ):
        _validated_sha256(data[key], f"candidate handoff {key}")
    candidate = validate_candidate(data["candidate_endpoint"])
    identity = _validate_expected_identity(data["canonical_identity"])
    if (
        isinstance(data["terminal_build"], bool)
        or not isinstance(data["terminal_build"], int)
        or data["terminal_build"] < 1
    ):
        raise LabValidationError("candidate handoff terminal_build is invalid")
    if (
        isinstance(data["produced_at_unix_ms"], bool)
        or not isinstance(data["produced_at_unix_ms"], int)
        or data["produced_at_unix_ms"] < 1
    ):
        raise LabValidationError("candidate handoff produced_at_unix_ms is invalid")

    if manifest_payload is not None:
        manifest = validate_experiment_manifest(manifest_payload)
        if data["experiment_manifest_sha256"] != manifest[
            "experiment_manifest_sha256"
        ]:
            raise LabValidationError("candidate handoff experiment manifest mismatch")
        if data["experiment_id"] != manifest["experiment_id"]:
            raise LabValidationError("candidate handoff experiment ID mismatch")
        if c012_session_id != manifest["lifecycle"]["c012_session_id"]:
            raise LabValidationError(
                "candidate handoff C012 session differs from manifest"
            )
        if data["terminal_sha256"] != manifest["terminal"]["sha256"]:
            raise LabValidationError("candidate handoff terminal hash mismatch")
        if identity != manifest["expected_identity"]:
            raise LabValidationError("candidate handoff canonical identity mismatch")
        if int(candidate["port"]) not in manifest["network_policy"][
            "allowed_remote_ports"
        ]:
            raise LabValidationError("candidate handoff port is outside manifest policy")

    if direct_campaign_manifest is not None:
        direct = validate_direct_campaign_manifest(
            direct_campaign_manifest,
            manifest_payload=manifest_payload,
        )
        comparisons = {
            "experiment_manifest_sha256": direct[
                "experiment_manifest_sha256"
            ],
            "c2_run_id": direct["c2_run_id"],
            "c2_control_plan_sha256": direct["c2_control_plan_sha256"],
            "c2_evidence_sha256": direct["c2_evidence_sha256"],
            "candidate_endpoint": direct["candidate_endpoint"],
            "canonical_identity": direct["canonical_identity"],
            "produced_at_unix_ms": direct["produced_at_unix_ms"],
            "direct_campaign_manifest_sha256": direct[
                "direct_campaign_manifest_sha256"
            ],
        }
        direct_terminal = direct["terminal"]
        assert isinstance(direct_terminal, Mapping)
        comparisons["terminal_sha256"] = direct_terminal["sha256"]
        comparisons["terminal_build"] = direct_terminal["build"]
        for key, expected in comparisons.items():
            if canonical_json(data[key]) != canonical_json(expected):
                raise LabValidationError(
                    f"candidate handoff {key} differs from direct campaign"
                )
    body = {
        key: data[key]
        for key in data
        if key != "candidate_handoff_manifest_sha256"
    }
    expected_digest = contract_digest(
        "CANDIDATE_HANDOFF",
        CANDIDATE_HANDOFF_SCHEMA_VERSION,
        body,
    )
    if data["candidate_handoff_manifest_sha256"] != expected_digest:
        raise LabValidationError("candidate handoff digest mismatch")
    reject_sensitive_content(data)
    return dict(data)


def validate_evidence(payload: object) -> dict[str, object]:
    reject_sensitive_content(payload)
    data = _require_mapping(payload, "evidence")
    required = {
        "schema_version",
        "run_id",
        "control",
        "run_context",
        "lifecycle_binding",
        "initial_pre_state_binding",
        "state_transition",
        "capture_integrity",
        "pre_state",
        "identity",
        "credential_bundle_investor_confirmed",
        "network",
        "discovery",
        "environment_health",
        "proof_binding",
        "timeline",
        "timing",
        "phase_markers",
    }
    _require_exact_keys(data, required, "evidence")
    evidence_schema_version = data["schema_version"]
    if (
        isinstance(evidence_schema_version, bool)
        or not isinstance(evidence_schema_version, int)
        or evidence_schema_version != EVIDENCE_SCHEMA_VERSION
    ):
        raise LabValidationError("unsupported evidence schema_version")
    run_id = _validated_uuid(data["run_id"], "run_id")
    if data["control"] not in CONTROLS:
        raise LabValidationError("invalid evidence control")
    control = str(data["control"])
    context = _require_run_context(data["run_context"], control)
    lifecycle_binding = _require_lifecycle_binding(
        data["lifecycle_binding"], control
    )
    initial_pre_state_binding = _validate_initial_pre_state_binding(
        data["initial_pre_state_binding"], control
    )
    state_transition = _require_state_transition(
        data["state_transition"], control
    )

    integrity = _require_bool_int_mapping(
        data["capture_integrity"],
        bool_keys={"etw_started", "etw_stopped", "required_markers_present"},
        int_keys={"events_lost", "buffers_lost"},
        context="capture_integrity",
    )
    if data["pre_state"] is None:
        pre_state = None
    else:
        pre_state = _require_bool_mapping(
            data["pre_state"], set(CLEAN_PRE_STATE_KEYS), "pre_state"
        )
    if control in {"C1", "C2"}:
        if pre_state is not None:
            raise LabValidationError(
                f"{control} must reference the single C012 initial pre-state"
            )
    elif pre_state is None:
        raise LabValidationError(f"{control} requires a pre-state snapshot")
    if (
        context["portable_root_path_sha256"]
        != initial_pre_state_binding["portable_root_path_sha256"]
    ):
        raise LabValidationError(
            "evidence portable root differs from pre-state binding"
        )
    network = _require_network(data["network"])
    discovery = _require_discovery(data["discovery"])
    health = _require_bool_mapping(
        data["environment_health"],
        {
            "build_unchanged",
            "clock_synchronized",
            "firewall_policy_verified",
            "account_available",
            "external_outage_excluded",
            "baseline_stable",
            "ui_compatible",
        },
        "environment_health",
    )
    identity = _require_identity(data["identity"])
    credential_bundle_investor_confirmed = data[
        "credential_bundle_investor_confirmed"
    ]
    if not isinstance(credential_bundle_investor_confirmed, bool):
        raise LabValidationError(
            "credential_bundle_investor_confirmed must be boolean"
        )
    proof_binding = _require_proof_binding(data["proof_binding"])
    credential_set_id = context["credential_set_id"]
    if control in {"C0", "C1"}:
        if credential_set_id is not None:
            raise LabValidationError(f"{control} cannot bind a credential_set_id")
        if proof_binding["credential_set_binding_verified"]:
            raise LabValidationError(
                f"{control} cannot claim a credential-set proof binding"
            )
        if proof_binding["credential_set_binding_sha256"] is not None:
            raise LabValidationError(
                f"{control} cannot contain a credential-set binding digest"
            )
    elif credential_set_id is None:
        raise LabValidationError(f"{control} requires a credential_set_id")
    if context["candidate_endpoint"] is None and (
        proof_binding["candidate_endpoint_sha256"] is not None
        or proof_binding["candidate_tuple_bound"]
    ):
        raise LabValidationError(
            "proof_binding cannot bind a candidate absent from run_context"
        )
    timeline = _require_timeline(data["timeline"], control, context)
    asserted_timing = _require_timing(data["timing"])
    timing = _derive_timing_from_timeline(control, timeline)
    if asserted_timing != timing:
        raise LabValidationError(
            "timing assertions do not match the authoritative timeline"
        )
    markers = data["phase_markers"]
    if not isinstance(markers, list) or not all(
        isinstance(item, str) and _CLEAN_TEXT.fullmatch(item) for item in markers
    ):
        raise LabValidationError("phase_markers must be clean strings")
    required_markers = list(_required_phase_markers(control))
    derived_markers = [
        str(event["code"]) for event in timeline["events"]  # type: ignore[index]
    ]
    if markers != required_markers or markers != derived_markers:
        raise LabValidationError(
            "phase_markers must exactly match the ordered timeline codes"
        )
    integrity["required_markers_present"] = bool(
        integrity["required_markers_present"]
        and markers == required_markers
    )
    expected_timeline_digest = contract_digest(
        "PHASE_TIMELINE",
        TIMELINE_SCHEMA_VERSION,
        {
            "run_id": run_id,
            "control": control,
            "timeline": timeline,
        },
    )
    if (
        proof_binding["phase_timeline_sha256"] is not None
        and proof_binding["phase_timeline_sha256"] != expected_timeline_digest
    ):
        raise LabValidationError("proof_binding phase timeline digest mismatch")
    if control != "C5" and timing["separation_from_c3_seconds"] != 0:
        raise LabValidationError(
            "timing.separation_from_c3_seconds is only valid for C5"
        )
    _validate_control_invariants(
        control=control,
        context=context,
        pre_state=pre_state,
        lifecycle_binding=lifecycle_binding,
        state_transition=state_transition,
        identity=identity,
        credential_bundle_investor_confirmed=credential_bundle_investor_confirmed,
        network=network,
        discovery=discovery,
        health=health,
        proof_binding=proof_binding,
        timing=timing,
    )

    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "run_id": run_id,
        "control": str(data["control"]),
        "run_context": context,
        "lifecycle_binding": lifecycle_binding,
        "initial_pre_state_binding": initial_pre_state_binding,
        "state_transition": state_transition,
        "capture_integrity": integrity,
        "pre_state": pre_state,
        "identity": identity,
        "credential_bundle_investor_confirmed": (
            credential_bundle_investor_confirmed
        ),
        "network": network,
        "discovery": discovery,
        "environment_health": health,
        "proof_binding": proof_binding,
        "timeline": timeline,
        "timing": timing,
        "phase_markers": list(markers),
    }


def evaluate_evidence(
    payload: object,
    *,
    config_payload: object | None = None,
    manifest_payload: object | None = None,
    control_plan_payload: object | None = None,
    candidate_handoff_payload: object | None = None,
    allow_synthetic: bool = False,
) -> Evaluation:
    if not isinstance(allow_synthetic, bool):
        raise LabValidationError("allow_synthetic must be boolean")
    evidence = validate_evidence(payload)
    manifest = _resolve_experiment_manifest(
        config_payload=config_payload,
        manifest_payload=manifest_payload,
    )
    plan = (
        None
        if control_plan_payload is None
        else validate_control_plan(
            control_plan_payload,
            manifest_payload=manifest,
            candidate_handoff=candidate_handoff_payload,
        )
    )
    handoff = (
        None
        if candidate_handoff_payload is None
        else validate_candidate_handoff(
            candidate_handoff_payload,
            manifest_payload=manifest,
        )
    )
    return _evaluate_validated_evidence(
        evidence,
        config_manifest=manifest,
        control_plan=plan,
        candidate_handoff=handoff,
        campaign_handoff_verified=False,
        allow_synthetic=allow_synthetic,
        campaign_c3_completed_at_unix_ms=None,
    )


def _evaluate_validated_evidence(
    evidence: Mapping[str, object],
    *,
    config_manifest: Mapping[str, object] | None,
    control_plan: Mapping[str, object] | None,
    candidate_handoff: Mapping[str, object] | None,
    campaign_handoff_verified: bool,
    allow_synthetic: bool,
    campaign_c3_completed_at_unix_ms: int | None,
) -> Evaluation:
    control = str(evidence["control"])
    integrity = evidence["capture_integrity"]
    pre = evidence["pre_state"]
    lifecycle_binding = evidence["lifecycle_binding"]
    initial_pre_state_binding = evidence["initial_pre_state_binding"]
    state_transition = evidence["state_transition"]
    network = evidence["network"]
    identity = evidence["identity"]
    discovery = evidence["discovery"]
    health = evidence["environment_health"]
    proof = evidence["proof_binding"]
    context = evidence["run_context"]
    timeline = evidence["timeline"]
    timing = evidence["timing"]
    credential_bundle_investor_confirmed = evidence[
        "credential_bundle_investor_confirmed"
    ]
    assert isinstance(integrity, dict)
    assert pre is None or isinstance(pre, dict)
    assert isinstance(lifecycle_binding, dict)
    assert isinstance(initial_pre_state_binding, dict)
    assert isinstance(state_transition, dict)
    assert isinstance(network, dict)
    assert isinstance(discovery, dict)
    assert isinstance(health, dict)
    assert isinstance(proof, dict)
    assert isinstance(context, dict)
    assert isinstance(timeline, dict)
    assert isinstance(timing, dict)
    assert isinstance(credential_bundle_investor_confirmed, bool)

    clean_state = pre is not None and all(pre.values())
    elapsed_seconds = int(context["completed_at_unix"]) - int(context["started_at_unix"])

    # Explicit contradictions take precedence over missing proof so that a second
    # absent predicate cannot mask a falsification in a combined mutation.
    if proof["run_id"] != evidence["run_id"]:
        return Evaluation("FAIL", ("proof_binding_run_id_mismatch",))
    if control == "C0" and not clean_state:
        return Evaluation("FAIL", ("c012_pre_state_not_clean",))
    credential_set_id = context["credential_set_id"]
    credential_binding_digest = proof["credential_set_binding_sha256"]
    if (
        credential_set_id is not None
        and credential_binding_digest is not None
        and credential_binding_digest
        != evidence_digest(
            {
                "credential_set_id": credential_set_id,
                "account_available": health["account_available"],
                "credential_bundle_investor_confirmed": (
                    credential_bundle_investor_confirmed
                ),
            }
        )
    ):
        return Evaluation("FAIL", ("credential_set_binding_digest_mismatch",))
    candidate = context["candidate_endpoint"]
    candidate_digest = proof["candidate_endpoint_sha256"]
    digest_mismatch = bool(
        candidate is not None
        and candidate_digest is not None
        and candidate_digest != evidence_digest(candidate)
    )

    if discovery["helper_secret_accessed"]:
        return Evaluation("FAIL", ("helper_accessed_sensitive_material",))
    if discovery["unsafe_endpoint_promoted"]:
        return Evaluation("FAIL", ("unsafe_endpoint_promoted",))

    manifest_binding_gap: str | None = None
    plan_binding_gap: str | None = None
    handoff_binding_gap: str | None = None
    lifecycle_binding_gap: str | None = None
    portable_root_binding_gap: str | None = None
    pre_state_binding_gap: str | None = None
    transition_binding_gap: str | None = None
    firewall_root_binding_gap: str | None = None
    direct_c2_context_missing = False
    if config_manifest is None:
        manifest_binding_gap = "evidence_experiment_manifest_missing"
    else:
        expected_manifest_digest = config_manifest[
            "experiment_manifest_sha256"
        ]
        if proof["experiment_manifest_sha256"] is None:
            manifest_binding_gap = "experiment_manifest_binding_not_proven"
        elif proof["experiment_manifest_sha256"] != expected_manifest_digest:
            return Evaluation("FAIL", ("experiment_manifest_digest_mismatch",))
        manifest_terminal = config_manifest["terminal"]
        assert isinstance(manifest_terminal, Mapping)
        if context["terminal_sha256"] != manifest_terminal["sha256"]:
            return Evaluation("FAIL", ("terminal_hash_manifest_mismatch",))
        manifest_network_policy = config_manifest["network_policy"]
        assert isinstance(manifest_network_policy, Mapping)
        if (
            candidate is not None
            and int(candidate["port"])
            not in manifest_network_policy["allowed_remote_ports"]
        ):
            return Evaluation("FAIL", ("candidate_port_manifest_policy_mismatch",))

    if control_plan is None:
        plan_binding_gap = "control_plan_binding_not_proven"
    else:
        if (
            control_plan["run_id"] != evidence["run_id"]
            or control_plan["control"] != control
            or control_plan["experiment_id"] != context["experiment_id"]
        ):
            return Evaluation("FAIL", ("control_plan_run_binding_mismatch",))
        if proof["control_plan_sha256"] is None:
            plan_binding_gap = "control_plan_binding_not_proven"
        elif proof["control_plan_sha256"] != control_plan[
            "control_plan_sha256"
        ]:
            return Evaluation("FAIL", ("control_plan_digest_mismatch",))
        if (
            config_manifest is not None
            and control_plan["experiment_manifest_sha256"]
            != config_manifest["experiment_manifest_sha256"]
        ):
            return Evaluation("FAIL", ("control_plan_manifest_mismatch",))
        plan_paths = control_plan["path_bindings"]
        assert isinstance(plan_paths, Mapping)
        portable_root_path_sha256 = context[
            "portable_root_path_sha256"
        ]
        if (
            portable_root_path_sha256
            != plan_paths["terminal_data_path_sha256"]
        ):
            return Evaluation(
                "FAIL",
                ("portable_root_control_plan_path_mismatch",),
            )
        plan_pre_state = control_plan["initial_pre_state_binding"]
        assert isinstance(plan_pre_state, Mapping)
        if canonical_json(initial_pre_state_binding) != canonical_json(
            plan_pre_state
        ):
            return Evaluation(
                "FAIL",
                ("initial_pre_state_plan_binding_mismatch",),
            )
        plan_lifecycle = control_plan["lifecycle_control"]
        assert isinstance(plan_lifecycle, Mapping)
        for key in (
            "lifecycle_mode",
            "c012_session_id",
            "session_role",
        ):
            if lifecycle_binding[key] != plan_lifecycle[key]:
                return Evaluation(
                    "FAIL",
                    ("lifecycle_control_binding_mismatch",),
                )
        if (
            lifecycle_binding["job_manifest_sha256"]
            != proof["job_manifest_sha256"]
            or lifecycle_binding["job_identity_sha256"]
            != proof["job_identity_sha256"]
            or lifecycle_binding["root_process_generation_sha256"]
            != proof["root_process_generation_sha256"]
        ):
            return Evaluation(
                "FAIL",
                ("lifecycle_job_process_binding_mismatch",),
            )
        expected_lifecycle_binding = lifecycle_binding_digest(
            run_id=evidence["run_id"],
            control=control,
            control_plan_sha256=control_plan["control_plan_sha256"],
            lifecycle_binding=lifecycle_binding,
        )
        if proof["lifecycle_binding_sha256"] is None:
            lifecycle_binding_gap = "lifecycle_binding_not_proven"
        elif (
            proof["lifecycle_binding_sha256"]
            != expected_lifecycle_binding
        ):
            return Evaluation(
                "FAIL",
                ("lifecycle_binding_digest_mismatch",),
            )
        if (
            proof["job_manifest_sha256"] is None
            or proof["job_identity_sha256"] is None
            or proof["root_process_generation_sha256"] is None
        ):
            portable_root_binding_gap = (
                "job_portable_root_binding_not_proven"
            )
        else:
            expected_job_root_binding = job_portable_root_binding_digest(
                run_id=evidence["run_id"],
                job_manifest_sha256=proof["job_manifest_sha256"],
                job_identity_sha256=proof["job_identity_sha256"],
                root_process_generation_sha256=proof[
                    "root_process_generation_sha256"
                ],
                portable_root_path_sha256=portable_root_path_sha256,
            )
            if proof["job_portable_root_binding_sha256"] is None:
                portable_root_binding_gap = (
                    "job_portable_root_binding_not_proven"
                )
            elif (
                proof["job_portable_root_binding_sha256"]
                != expected_job_root_binding
            ):
                return Evaluation(
                    "FAIL",
                    ("job_portable_root_binding_mismatch",),
                )
        expected_pre_state_binding = contract_digest(
            "PRE_STATE_BINDING",
            1,
            {
                "run_id": evidence["run_id"],
                "control": control,
                "initial_pre_state_binding": initial_pre_state_binding,
                "pre_state": pre,
            },
        )
        if proof["pre_state_binding_sha256"] is None:
            pre_state_binding_gap = "pre_state_binding_not_proven"
        elif (
            proof["pre_state_binding_sha256"]
            != expected_pre_state_binding
        ):
            return Evaluation(
                "FAIL",
                ("pre_state_binding_digest_mismatch",),
            )
        expected_transition_binding = contract_digest(
            "STATE_TRANSITION",
            1,
            {
                "run_id": evidence["run_id"],
                "control": control,
                "state_transition": state_transition,
            },
        )
        if proof["state_transition_sha256"] is None:
            transition_binding_gap = "state_transition_not_proven"
        elif (
            proof["state_transition_sha256"]
            != expected_transition_binding
        ):
            return Evaluation(
                "FAIL",
                ("state_transition_digest_mismatch",),
            )
        if control in {"C3", "C4", "C5"}:
            if (
                proof["firewall_plan_sha256"] is None
                or proof["candidate_endpoint_sha256"] is None
            ):
                firewall_root_binding_gap = (
                    "firewall_portable_root_binding_not_proven"
                )
            else:
                expected_firewall_root_binding = (
                    firewall_portable_root_binding_digest(
                        run_id=evidence["run_id"],
                        control_plan_sha256=control_plan[
                            "control_plan_sha256"
                        ],
                        firewall_plan_sha256=proof[
                            "firewall_plan_sha256"
                        ],
                        portable_root_path_sha256=portable_root_path_sha256,
                        candidate_endpoint_sha256=proof[
                            "candidate_endpoint_sha256"
                        ],
                    )
                )
                if (
                    proof["firewall_portable_root_binding_sha256"]
                    is None
                ):
                    firewall_root_binding_gap = (
                        "firewall_portable_root_binding_not_proven"
                    )
                elif (
                    proof["firewall_portable_root_binding_sha256"]
                    != expected_firewall_root_binding
                ):
                    return Evaluation(
                        "FAIL",
                        ("firewall_portable_root_binding_mismatch",),
                    )

    if control in {"C0", "C1", "C2"}:
        if proof["candidate_handoff_manifest_sha256"] is not None:
            return Evaluation("FAIL", ("early_control_bound_candidate_handoff",))
    elif candidate_handoff is None:
        handoff_binding_gap = "candidate_handoff_binding_not_proven"
    else:
        if proof["candidate_handoff_manifest_sha256"] is None:
            handoff_binding_gap = "candidate_handoff_binding_not_proven"
        elif proof["candidate_handoff_manifest_sha256"] != candidate_handoff[
            "candidate_handoff_manifest_sha256"
        ]:
            return Evaluation("FAIL", ("candidate_handoff_digest_mismatch",))
        if (
            context["experiment_id"] != candidate_handoff["experiment_id"]
            or context["terminal_sha256"] != candidate_handoff["terminal_sha256"]
            or context["terminal_build"] != candidate_handoff["terminal_build"]
            or canonical_json(context["candidate_endpoint"])
            != canonical_json(candidate_handoff["candidate_endpoint"])
        ):
            return Evaluation("FAIL", ("candidate_handoff_context_mismatch",))
        first_event = timeline["events"][0]
        assert isinstance(first_event, Mapping)
        if int(first_event["timestamp_unix_ms"]) < int(
            candidate_handoff["produced_at_unix_ms"]
        ):
            return Evaluation(
                "INCONCLUSIVE",
                ("direct_control_started_before_candidate_handoff",),
            )
        if control_plan is not None and (
            control_plan["candidate_handoff_manifest_sha256"]
            != candidate_handoff["candidate_handoff_manifest_sha256"]
            or control_plan["direct_campaign_manifest_sha256"]
            != candidate_handoff["direct_campaign_manifest_sha256"]
        ):
            return Evaluation("FAIL", ("candidate_handoff_plan_mismatch",))
        if not campaign_handoff_verified:
            direct_c2_context_missing = True

    config_experiment_id_mismatch = bool(
        config_manifest is not None
        and context["experiment_id"] != config_manifest["experiment_id"]
    )
    config_expected_identity_mismatch = False
    if config_manifest is not None and control in {"C2", "C3", "C4", "C5"}:
        authoritative_identity = config_manifest["expected_identity"]
        assert isinstance(authoritative_identity, Mapping)
        config_expected_identity_mismatch = any(
            context[context_key] != authoritative_identity[config_key]
            for context_key, config_key in (
                ("expected_server", "server"),
                ("expected_company", "company"),
                ("expected_trade_mode", "trade_mode"),
            )
        )
    requested_label_binding_missing = False
    requested_label_manifest_gap: str | None = None
    negative_query_binding_gap: str | None = None
    if control in {"C0", "C1", "C2"}:
        requested_digest = context["requested_server_label_sha256"]
        selected_digest = discovery["selected_server_label_sha256"]
        if (
            control == "C1"
            and requested_digest is not None
            and selected_digest is not None
            and selected_digest != requested_digest
        ):
            return Evaluation("FAIL", ("discovery_selected_label_mismatch",))
        if config_manifest is None:
            requested_label_manifest_gap = "evidence_config_manifest_missing"
        elif config_experiment_id_mismatch:
            requested_label_manifest_gap = (
                "evidence_config_experiment_id_mismatch"
            )
        else:
            authoritative_requested_digest = evidence_digest(
                {
                    "requested_server_label": config_manifest[
                        "requested_server_label"
                    ]
                }
            )
            if requested_digest != authoritative_requested_digest:
                requested_label_manifest_gap = (
                    "c012_requested_label_mismatch"
                )
        label_binding_digest = proof["requested_label_binding_sha256"]
        requested_label_binding_missing = bool(
            requested_digest is None
            or label_binding_digest is None
            or not proof["requested_label_binding_verified"]
            or (
                control == "C1"
                and (
                    selected_digest is None
                    or not discovery["exact_label_match_verified"]
                )
            )
        )
        if (
            requested_label_manifest_gap is None
            and not requested_label_binding_missing
        ):
            expected_label_binding_digest = evidence_digest(
                {
                    "run_id": evidence["run_id"],
                    "control": control,
                    "experiment_id": context["experiment_id"],
                    "requested_label_manifest_sha256": (
                        requested_label_manifest_digest(config_manifest)
                    ),
                    "job_identity_sha256": proof["job_identity_sha256"],
                    "phase_timeline_sha256": proof["phase_timeline_sha256"],
                    "requested_server_label_sha256": requested_digest,
                    "selected_server_label_sha256": selected_digest,
                }
            )
            if label_binding_digest != expected_label_binding_digest:
                return Evaluation(
                    "FAIL",
                    ("discovery_requested_label_binding_mismatch",),
                )
    if control == "C1":
        if config_manifest is None or control_plan is None:
            negative_query_binding_gap = (
                "negative_query_manifest_or_plan_missing"
            )
        else:
            negative_query = config_manifest["negative_query"]
            plan_negative_query = control_plan["negative_query"]
            assert isinstance(negative_query, Mapping)
            if canonical_json(negative_query) != canonical_json(
                plan_negative_query
            ):
                return Evaluation(
                    "FAIL",
                    ("negative_query_plan_binding_mismatch",),
                )
            if (
                negative_query["label"]
                == config_manifest["requested_server_label"]
                or negative_query["label_sha256"]
                == context["requested_server_label_sha256"]
            ):
                return Evaluation(
                    "FAIL",
                    ("negative_query_matches_requested_label",),
                )
            observed_negative_digest = discovery[
                "negative_query_label_sha256"
            ]
            observed_negative_count = discovery[
                "negative_query_result_count"
            ]
            if (
                observed_negative_count is not None
                and observed_negative_count != 0
            ):
                return Evaluation(
                    "FAIL",
                    ("negative_query_returned_results",),
                )
            if (
                observed_negative_digest is None
                or observed_negative_count is None
                or not discovery["negative_query_ui_binding_verified"]
                or proof["negative_query_binding_sha256"] is None
            ):
                negative_query_binding_gap = (
                    "negative_query_binding_not_proven"
                )
            else:
                if (
                    observed_negative_digest
                    != negative_query["label_sha256"]
                ):
                    return Evaluation(
                        "FAIL",
                        ("negative_query_label_binding_mismatch",),
                    )
                expected_negative_binding = contract_digest(
                    "NEGATIVE_QUERY_BINDING",
                    1,
                    {
                        "run_id": evidence["run_id"],
                        "control": control,
                        "control_plan_sha256": control_plan[
                            "control_plan_sha256"
                        ],
                        "phase_timeline_sha256": proof[
                            "phase_timeline_sha256"
                        ],
                        "negative_query_label_sha256": (
                            observed_negative_digest
                        ),
                        "negative_query_result_count": (
                            observed_negative_count
                        ),
                        "negative_query_ui_binding_verified": True,
                    },
                )
                if (
                    proof["negative_query_binding_sha256"]
                    != expected_negative_binding
                ):
                    return Evaluation(
                        "FAIL",
                        ("negative_query_binding_digest_mismatch",),
                    )

    if control == "C0":
        if identity is not None:
            return Evaluation("FAIL", ("baseline_exposed_account_identity",))
        if not clean_state:
            return Evaluation("FAIL", ("baseline_not_clean",))
    if control == "C1":
        if identity is not None:
            return Evaluation("FAIL", ("identity_present_during_credential_free_discovery",))
        if discovery["credentials_supplied"]:
            return Evaluation("FAIL", ("credentials_used_during_discovery",))
        if discovery["expected_exact_result_count"] != 1:
            return Evaluation("FAIL", ("discovery_not_unique_exact_match",))
    if control == "C2":
        if not network["candidate_endpoint_safe"]:
            return Evaluation("FAIL", ("unsafe_candidate_endpoint",))
        if digest_mismatch or (
            network["candidate_attempt_observed"] and not network["candidate_tuple_match"]
        ):
            return Evaluation("FAIL", ("observed_candidate_tuple_mismatch",))
    if control in {"C3", "C5"}:
        if not clean_state:
            return Evaluation("FAIL", ("direct_boot_not_clean",))
        if network["other_tcp_flows"] or network["non_tcp_network_events"]:
            return Evaluation("FAIL", ("direct_boot_used_unexpected_egress",))
        if network["dns_events"]:
            return Evaluation("FAIL", ("direct_boot_used_dns",))
        if not network["candidate_endpoint_safe"]:
            return Evaluation("FAIL", ("unsafe_candidate_endpoint",))
        if digest_mismatch or (
            network["candidate_attempt_observed"] and not network["candidate_tuple_match"]
        ):
            return Evaluation("FAIL", ("direct_candidate_tuple_mismatch",))
    if control == "C4":
        if not clean_state:
            return Evaluation("FAIL", ("negative_control_not_clean",))
        if not network["candidate_endpoint_safe"]:
            return Evaluation("FAIL", ("unsafe_candidate_endpoint",))
        if (
            network["candidate_connected"]
            or network["other_tcp_flows"]
            or network["non_tcp_network_events"]
        ):
            return Evaluation("FAIL", ("negative_control_bypassed",))
        if network["dns_events"]:
            return Evaluation("FAIL", ("dns_fallback_during_negative_control",))
        if identity is not None:
            return Evaluation("FAIL", ("identity_present_while_candidate_blocked",))
        if digest_mismatch or (
            network["candidate_attempt_observed"] and not network["candidate_tuple_match"]
        ):
            return Evaluation("FAIL", ("negative_control_candidate_tuple_mismatch",))

    if control in {"C2", "C3", "C5"}:
        if identity is not None and identity["probe_run_id"] != evidence["run_id"]:
            return Evaluation("FAIL", ("identity_probe_run_id_mismatch",))
        identity_contradiction = _evaluate_identity(
            identity,
            context,
            control_plan=control_plan,
            proof_binding=proof,
            timeline=timeline,
            config_manifest=config_manifest,
        )
        if identity_contradiction is not None and identity_contradiction.outcome == "FAIL":
            return identity_contradiction

    if config_experiment_id_mismatch:
        return Evaluation(
            "INCONCLUSIVE",
            ("evidence_config_experiment_id_mismatch",),
        )
    if config_expected_identity_mismatch:
        return Evaluation(
            "INCONCLUSIVE",
            ("evidence_config_expected_identity_mismatch",),
        )
    if requested_label_manifest_gap is not None:
        return Evaluation("INCONCLUSIVE", (requested_label_manifest_gap,))
    if requested_label_binding_missing:
        return Evaluation(
            "INCONCLUSIVE",
            ("discovery_requested_label_binding_missing",),
        )
    if negative_query_binding_gap is not None:
        return Evaluation("INCONCLUSIVE", (negative_query_binding_gap,))
    if manifest_binding_gap is not None:
        return Evaluation("INCONCLUSIVE", (manifest_binding_gap,))
    if plan_binding_gap is not None:
        return Evaluation("INCONCLUSIVE", (plan_binding_gap,))
    if not state_transition["transition_verified"]:
        return Evaluation(
            "INCONCLUSIVE", ("state_transition_not_verified",)
        )
    for binding_gap in (
        lifecycle_binding_gap,
        portable_root_binding_gap,
        pre_state_binding_gap,
        transition_binding_gap,
        firewall_root_binding_gap,
    ):
        if binding_gap is not None:
            return Evaluation("INCONCLUSIVE", (binding_gap,))
    if handoff_binding_gap is not None:
        return Evaluation("INCONCLUSIVE", (handoff_binding_gap,))

    if control in {"C2", "C3", "C4", "C5"} and (
        not proof["credential_set_binding_verified"]
        or proof["credential_set_binding_sha256"] is None
    ):
        return Evaluation("INCONCLUSIVE", ("credential_set_binding_not_proven",))
    if (
        control in {"C2", "C3", "C4", "C5"}
        and not credential_bundle_investor_confirmed
    ):
        return Evaluation(
            "INCONCLUSIVE",
            ("credential_bundle_investor_not_confirmed",),
        )

    if not (
        integrity["etw_started"]
        and integrity["etw_stopped"]
        and integrity["required_markers_present"]
        and integrity["events_lost"] == 0
        and integrity["buffers_lost"] == 0
    ):
        return Evaluation("INCONCLUSIVE", ("capture_integrity_not_proven",))

    common_proof_gap = _common_etw_proof_gap(proof)
    if common_proof_gap is not None:
        return common_proof_gap

    assert config_manifest is not None
    durations = config_manifest["durations_seconds"]
    assert isinstance(durations, Mapping)

    if control == "C0":
        if not health["build_unchanged"] or not health["baseline_stable"]:
            return Evaluation("INCONCLUSIVE", ("baseline_environment_unstable",))
        if (
            timing["baseline_seconds"] < durations["baseline"]
            or elapsed_seconds < timing["baseline_seconds"]
        ):
            return Evaluation("INCONCLUSIVE", ("baseline_window_too_short",))
        return _positive_result(
            proof, "baseline_complete", allow_synthetic=allow_synthetic
        )

    if control == "C1":
        if not health["ui_compatible"]:
            return Evaluation("INCONCLUSIVE", ("discovery_ui_incompatible",))
        if (
            timing["negative_discovery_seconds"] < durations["negative_discovery"]
            or timing["exact_discovery_seconds"] < durations["exact_discovery"]
            or elapsed_seconds
            < timing["negative_discovery_seconds"] + timing["exact_discovery_seconds"]
        ):
            return Evaluation("INCONCLUSIVE", ("discovery_windows_too_short",))
        if not discovery["cache_influence_excluded"]:
            return Evaluation("INCONCLUSIVE", ("discovery_cache_influence_unexplained",))
        if not (
            discovery["negative_exact_query_completed"]
            and discovery["exact_selection_completed"]
            and discovery["endpoint_delta_acquired"]
        ):
            return Evaluation("INCONCLUSIVE", ("discovery_sequence_incomplete",))
        if (
            discovery["endpoint_delta_source"] == "NONE"
            or discovery["endpoint_delta_source_sha256"] is None
            or not discovery["endpoint_delta_source_verified"]
            or (
                discovery["endpoint_delta_source"]
                == "PROCESS_SCOPED_TCP_FLOW_SET"
                and (
                    network["process_scoped_tcp_flows"] < 1
                    or network["flow_record_set_sha256"] is None
                    or not network["flow_record_set_verified"]
                    or discovery["endpoint_delta_source_sha256"]
                    != network["flow_record_set_sha256"]
                )
            )
        ):
            return Evaluation(
                "INCONCLUSIVE",
                ("discovery_endpoint_delta_source_not_proven",),
            )
        if (
            discovery["endpoint_delta_source"]
            == "PROCESS_SCOPED_TCP_FLOW_SET"
            and not network["attribution_unambiguous"]
        ):
            return Evaluation(
                "INCONCLUSIVE",
                ("discovery_flow_attribution_ambiguous",),
            )
        return _positive_result(
            proof,
            "credential_free_exact_discovery_complete",
            allow_synthetic=allow_synthetic,
        )

    if control == "C2":
        if candidate is None:
            return Evaluation("INCONCLUSIVE", ("candidate_endpoint_not_bound",))
        if (
            not proof["candidate_tuple_bound"]
            or candidate_digest is None
            or not network["candidate_attempt_observed"]
            or not network["candidate_connected"]
            or not network["candidate_tuple_match"]
            or network["candidate_observed_phase"] != "LOGIN"
            or network["candidate_tcp_flows"] < 1
            or not network["attribution_unambiguous"]
            or not network["flow_record_set_verified"]
            or network["flow_record_set_sha256"] is None
        ):
            return Evaluation("INCONCLUSIVE", ("candidate_login_flow_not_proven",))
        if not all(
            health[key]
            for key in (
                "build_unchanged",
                "clock_synchronized",
                "account_available",
                "external_outage_excluded",
            )
        ):
            return Evaluation(
                "INCONCLUSIVE", ("login_environment_health_not_proven",)
            )
        if (
            not 1
            <= timing["login_observation_seconds"]
            <= durations["login_timeout"]
            or timing["connected_steady_seconds"] < durations["connected_steady"]
            or timing["network_interruption_seconds"]
            < durations["network_interruption"]
            or timing["reconnect_observation_seconds"]
            < durations["reconnect_observation"]
            or elapsed_seconds
            < timing["login_observation_seconds"]
            + timing["connected_steady_seconds"]
            + timing["network_interruption_seconds"]
            + timing["reconnect_observation_seconds"]
        ):
            return Evaluation("INCONCLUSIVE", ("login_observation_windows_too_short",))
        identity_result = _evaluate_identity(
            identity,
            context,
            control_plan=control_plan,
            proof_binding=proof,
            timeline=timeline,
            config_manifest=config_manifest,
        )
        if identity_result is not None:
            return identity_result
        return _positive_result(
            proof, "normal_investor_login_observed", allow_synthetic=allow_synthetic
        )

    if control in {"C3", "C5"}:
        if candidate is None:
            return Evaluation("INCONCLUSIVE", ("candidate_endpoint_not_bound",))
        if (
            not 1
            <= timing["login_observation_seconds"]
            <= durations["login_timeout"]
            or timing["connected_steady_seconds"] < durations["connected_steady"]
            or elapsed_seconds
            < timing["login_observation_seconds"]
            + timing["connected_steady_seconds"]
        ):
            return Evaluation("INCONCLUSIVE", ("connected_window_too_short",))
        if not health["firewall_policy_verified"]:
            return Evaluation("INCONCLUSIVE", ("direct_firewall_policy_not_proven",))
        if not all(
            health[key]
            for key in (
                "build_unchanged",
                "clock_synchronized",
                "account_available",
                "external_outage_excluded",
            )
        ):
            return Evaluation("INCONCLUSIVE", ("direct_environment_health_not_proven",))
        if not proof["wfp_proof_capable"] or proof["wfp_evidence_sha256"] is None:
            return Evaluation(
                "INCONCLUSIVE", ("direct_wfp_evidence_not_proof_capable",)
            )
        if (
            not proof["firewall_plan_bound"]
            or proof["firewall_plan_sha256"] is None
            or not proof["candidate_tuple_bound"]
            or candidate_digest is None
        ):
            return Evaluation(
                "INCONCLUSIVE", ("direct_candidate_policy_binding_not_proven",)
            )
        if not network["attribution_unambiguous"]:
            return Evaluation("INCONCLUSIVE", ("direct_flow_attribution_ambiguous",))
        required_phase = "DIRECT_ONLY" if control == "C3" else "DIRECT_REPEAT"
        if (
            not network["candidate_attempt_observed"]
            or not network["candidate_connected"]
            or not network["candidate_tuple_match"]
            or network["candidate_observed_phase"] != required_phase
            or network["candidate_tcp_flows"] < 1
            or network["other_tcp_flows"] != 0
            or network["dns_events"] != 0
            or network["non_tcp_network_events"] != 0
            or not network["flow_record_set_verified"]
            or network["flow_record_set_sha256"] is None
        ):
            return Evaluation("INCONCLUSIVE", ("direct_candidate_flow_not_proven",))
        identity_result = _evaluate_identity(
            identity,
            context,
            control_plan=control_plan,
            proof_binding=proof,
            timeline=timeline,
            config_manifest=config_manifest,
        )
        if identity_result is not None:
            return identity_result
        if control == "C5":
            if campaign_c3_completed_at_unix_ms is None:
                return Evaluation(
                    "INCONCLUSIVE",
                    ("c5_campaign_timing_context_required",),
                )
            first_event = timeline["events"][0]
            assert isinstance(first_event, Mapping)
            separation_ms = (
                int(first_event["timestamp_unix_ms"])
                - campaign_c3_completed_at_unix_ms
            )
            if separation_ms < 0:
                return Evaluation(
                    "INCONCLUSIVE", ("c5_started_before_c3_completed",)
                )
            if separation_ms < durations["c5_separation_minimum"] * 1000:
                return Evaluation(
                    "INCONCLUSIVE", ("c3_c5_separation_not_proven",)
                )
        if direct_c2_context_missing:
            return Evaluation(
                "INCONCLUSIVE",
                ("candidate_handoff_c2_context_required",),
            )
        return _positive_result(
            proof, "direct_only_login_verified", allow_synthetic=allow_synthetic
        )

    # C4: the exact candidate is locally blocked.  Success, fallback, or any
    # authenticated identity falsifies the no-fallback claim.
    if candidate is None:
        return Evaluation("INCONCLUSIVE", ("candidate_endpoint_not_bound",))
    if (
        not 1
        <= timing["blocked_observation_seconds"]
        <= durations["blocked_timeout"]
        or elapsed_seconds < timing["blocked_observation_seconds"]
        or elapsed_seconds
        > durations["blocked_timeout"] + durations["c4_elapsed_tolerance"]
    ):
        return Evaluation("INCONCLUSIVE", ("negative_control_window_invalid",))
    if not health["firewall_policy_verified"]:
        return Evaluation("INCONCLUSIVE", ("negative_control_firewall_not_proven",))
    if not all(
        health[key]
        for key in (
            "build_unchanged",
            "clock_synchronized",
            "account_available",
            "external_outage_excluded",
        )
    ):
        return Evaluation(
            "INCONCLUSIVE", ("negative_control_environment_health_not_proven",)
        )
    if not proof["wfp_proof_capable"] or proof["wfp_evidence_sha256"] is None:
        return Evaluation(
            "INCONCLUSIVE", ("negative_control_wfp_evidence_not_proof_capable",)
        )
    if (
        not proof["firewall_plan_bound"]
        or proof["firewall_plan_sha256"] is None
        or not proof["candidate_tuple_bound"]
        or candidate_digest is None
    ):
        return Evaluation(
            "INCONCLUSIVE", ("negative_control_candidate_policy_binding_not_proven",)
        )
    if not (
        network["candidate_attempt_observed"]
        and network["candidate_block_observed"]
        and network["candidate_tuple_match"]
        and network["candidate_observed_phase"] == "ENDPOINT_BLOCKED"
        and network["candidate_tcp_flows"] >= 1
        and network["other_tcp_flows"] == 0
        and network["dns_events"] == 0
        and network["non_tcp_network_events"] == 0
        and network["flow_record_set_verified"]
        and network["flow_record_set_sha256"] is not None
        and network["attribution_unambiguous"]
    ):
        return Evaluation("INCONCLUSIVE", ("candidate_block_not_proven",))
    if direct_c2_context_missing:
        return Evaluation(
            "INCONCLUSIVE",
            ("candidate_handoff_c2_context_required",),
        )
    return _positive_result(
        proof, "candidate_blocked_without_fallback", allow_synthetic=allow_synthetic
    )


def _positive_result(
    proof: Mapping[str, object], reason: str, *, allow_synthetic: bool
) -> Evaluation:
    """Never turn self-asserted provenance into a real probatory PASS."""

    provenance = proof["provenance"]
    assert isinstance(provenance, dict)
    if provenance["origin"] == "CAPTURED_EXPORT":
        return Evaluation(
            "INCONCLUSIVE",
            ("captured_artifact_binding_not_independently_verified",),
        )
    if not allow_synthetic:
        return Evaluation(
            "INCONCLUSIVE",
            ("synthetic_fixture_requires_test_only_authorization",),
        )
    return Evaluation("SYNTHETIC_PASS", (reason,))


def _common_etw_proof_gap(proof: Mapping[str, object]) -> Evaluation | None:
    if not proof["etw_proof_capable"] or proof["etw_evidence_sha256"] is None:
        return Evaluation("INCONCLUSIVE", ("etw_evidence_not_proof_capable",))
    if not proof["job_process_binding_verified"] or proof["job_manifest_sha256"] is None:
        return Evaluation("INCONCLUSIVE", ("job_process_binding_not_proven",))
    if not proof["phase_binding_verified"] or proof["phase_timeline_sha256"] is None:
        return Evaluation("INCONCLUSIVE", ("phase_binding_not_proven",))
    return None


def _validate_c012_timeline_order(
    evidence_by_control: Mapping[str, Mapping[str, object]],
) -> Evaluation | None:
    timelines = {
        control: evidence_by_control[control]["timeline"]
        for control in ("C0", "C1", "C2")
    }
    assert all(isinstance(timeline, Mapping) for timeline in timelines.values())
    frequencies = {
        int(timeline["qpc_frequency_hz"])
        for timeline in timelines.values()
    }
    if len(frequencies) != 1:
        return Evaluation("INCONCLUSIVE", ("c012_timeline_frequency_mismatch",))

    for earlier, later in (("C0", "C1"), ("C1", "C2")):
        earlier_events = timelines[earlier]["events"]
        later_events = timelines[later]["events"]
        assert isinstance(earlier_events, list)
        assert isinstance(later_events, list)
        earlier_last = earlier_events[-1]
        later_first = later_events[0]
        assert isinstance(earlier_last, Mapping)
        assert isinstance(later_first, Mapping)
        if int(earlier_last["timestamp_unix_ms"]) > int(
            later_first["timestamp_unix_ms"]
        ):
            return Evaluation("INCONCLUSIVE", ("c012_timeline_timestamp_order_invalid",))
        if int(earlier_last["qpc"]) >= int(later_first["qpc"]):
            return Evaluation("INCONCLUSIVE", ("c012_timeline_qpc_order_invalid",))
    return None


def evaluate_campaign(
    payloads: object,
    *,
    config_payload: object | None = None,
    manifest_payload: object | None = None,
    control_plans_payload: object | None = None,
    candidate_handoff_payload: object | None = None,
    direct_campaign_manifest_payload: object | None = None,
    allow_synthetic: bool = False,
) -> Evaluation:
    """Evaluate the complete C0-C5 protocol without promoting an endpoint."""

    if not isinstance(allow_synthetic, bool):
        raise LabValidationError("allow_synthetic must be boolean")
    if not isinstance(payloads, list):
        raise LabValidationError("campaign evidence must be a JSON array")
    manifest = _resolve_experiment_manifest(
        config_payload=config_payload,
        manifest_payload=manifest_payload,
    )
    handoff = (
        None
        if candidate_handoff_payload is None
        else validate_candidate_handoff(
            candidate_handoff_payload,
            manifest_payload=manifest,
            direct_campaign_manifest=direct_campaign_manifest_payload,
        )
    )
    direct_manifest = (
        None
        if direct_campaign_manifest_payload is None
        else validate_direct_campaign_manifest(
            direct_campaign_manifest_payload,
            manifest_payload=manifest,
        )
    )
    plans_by_control: dict[str, dict[str, object]] = {}
    if control_plans_payload is not None:
        raw_plans: list[object]
        if isinstance(control_plans_payload, Mapping):
            raw_plans = list(control_plans_payload.values())
        elif isinstance(control_plans_payload, list):
            raw_plans = list(control_plans_payload)
        else:
            raise LabValidationError(
                "campaign control plans must be an object or array"
            )
        for raw_plan in raw_plans:
            plan = validate_control_plan(
                raw_plan,
                manifest_payload=manifest,
                candidate_handoff=(
                    handoff
                    if isinstance(raw_plan, Mapping)
                    and raw_plan.get("control") in {"C3", "C4", "C5"}
                    else None
                ),
            )
            plan_control = str(plan["control"])
            if plan_control in plans_by_control:
                raise LabValidationError("duplicate campaign control plan")
            plans_by_control[plan_control] = plan
    validated = [validate_evidence(item) for item in payloads]
    by_control: dict[str, dict[str, object]] = {}
    for item in validated:
        control = str(item["control"])
        if control in by_control:
            return Evaluation("INCONCLUSIVE", ("duplicate_control_evidence",))
        by_control[control] = item
    if set(by_control) != set(CONTROLS):
        return Evaluation("INCONCLUSIVE", ("campaign_controls_incomplete",))

    # These two observations are global falsifications. Check them before any
    # cross-control proof gap so a missing/mismatched manifest cannot mask
    # sensitive helper access or unsafe endpoint promotion.
    for control in CONTROLS:
        campaign_discovery = by_control[control]["discovery"]
        assert isinstance(campaign_discovery, dict)
        if campaign_discovery["helper_secret_accessed"]:
            return Evaluation(
                "FAIL",
                (f"{control}_helper_accessed_sensitive_material",),
            )
        if campaign_discovery["unsafe_endpoint_promoted"]:
            return Evaluation(
                "FAIL",
                (f"{control}_unsafe_endpoint_promoted",),
            )

    # Cross-control identity contradictions are falsifications, not merely proof
    # gaps. Evaluate them before provenance/sufficiency downgrades.
    early_contexts = {
        control: by_control[control]["run_context"] for control in CONTROLS
    }
    early_proofs = {
        control: by_control[control]["proof_binding"] for control in CONTROLS
    }
    assert all(isinstance(value, dict) for value in early_contexts.values())
    assert all(isinstance(value, dict) for value in early_proofs.values())
    for key, reason in (
        ("job_manifest_sha256", "c012_job_manifest_mismatch"),
        ("job_identity_sha256", "c012_job_identity_mismatch"),
        (
            "root_process_generation_sha256",
            "c012_process_generation_mismatch",
        ),
    ):
        if len(
            {
                str(early_proofs[control][key])
                for control in ("C0", "C1", "C2")
            }
        ) != 1:
            return Evaluation("INCONCLUSIVE", (reason,))
    early_lifecycle = {
        control: by_control[control]["lifecycle_binding"]
        for control in ("C0", "C1", "C2")
    }
    early_pre_state_bindings = {
        control: by_control[control]["initial_pre_state_binding"]
        for control in ("C0", "C1", "C2")
    }
    assert all(isinstance(value, Mapping) for value in early_lifecycle.values())
    assert all(
        isinstance(value, Mapping)
        for value in early_pre_state_bindings.values()
    )
    for lifecycle_key in ("c012_session_id", "job_id", "lifecycle_mode"):
        if len(
            {
                str(early_lifecycle[control][lifecycle_key])
                for control in ("C0", "C1", "C2")
            }
        ) != 1:
            return Evaluation(
                "INCONCLUSIVE", ("c012_lifecycle_session_mismatch",)
            )
    initial_c012_digests = {
        str(
            early_pre_state_bindings[control][
                "initial_c012_pre_state_sha256"
            ]
        )
        for control in ("C0", "C1", "C2")
    }
    if len(initial_c012_digests) != 1:
        return Evaluation(
            "INCONCLUSIVE",
            ("c012_initial_pre_state_commitment_mismatch",),
        )
    c0_pre_state = by_control["C0"]["pre_state"]
    assert isinstance(c0_pre_state, Mapping)
    expected_c0_initial_body = initial_c012_pre_state_body(
        experiment_id=early_contexts["C0"]["experiment_id"],
        c012_session_id=early_lifecycle["C0"]["c012_session_id"],
        portable_root_path_sha256=early_contexts["C0"][
            "portable_root_path_sha256"
        ],
        checks=c0_pre_state,
    )
    if (
        next(iter(initial_c012_digests))
        != initial_c012_pre_state_digest(expected_c0_initial_body)
    ):
        return Evaluation(
            "FAIL", ("c012_initial_pre_state_digest_mismatch",)
        )
    c1_transition = by_control["C1"]["state_transition"]
    c2_transition = by_control["C2"]["state_transition"]
    assert isinstance(c1_transition, Mapping)
    assert isinstance(c2_transition, Mapping)
    allowed_broker_cache_transitions = {
        ("CREATED_RECORDED", "INHERITED_RECORDED"),
        ("ABSENT_RECORDED", "CREATED_RECORDED"),
        ("ABSENT_RECORDED", "ABSENT_RECORDED"),
    }
    observed_broker_cache_transition = (
        str(c1_transition["broker_cache_state"]),
        str(c2_transition["broker_cache_state"]),
    )
    if (
        c1_transition["transition_verified"] is True
        and c2_transition["transition_verified"] is True
        and observed_broker_cache_transition
        not in allowed_broker_cache_transitions
    ):
        return Evaluation(
            "FAIL",
            ("c012_state_transition_incompatible",),
        )
    if len(
        {
            str(early_contexts[control]["requested_server_label_sha256"])
            for control in ("C0", "C1", "C2")
        }
    ) != 1:
        return Evaluation("INCONCLUSIVE", ("c012_requested_label_mismatch",))
    if manifest is not None:
        if any(
            context["experiment_id"] != manifest["experiment_id"]
            for context in early_contexts.values()
        ):
            return Evaluation(
                "INCONCLUSIVE",
                ("campaign_config_experiment_id_mismatch",),
            )
        manifest_requested_label_digest = evidence_digest(
            {
                "requested_server_label": manifest[
                    "requested_server_label"
                ]
            }
        )
        if any(
            early_contexts[control]["requested_server_label_sha256"]
            != manifest_requested_label_digest
            for control in ("C0", "C1", "C2")
        ):
            return Evaluation(
                "INCONCLUSIVE",
                ("c012_requested_label_mismatch",),
            )
    for key in ("expected_server", "expected_company", "expected_trade_mode"):
        if len(
            {
                str(early_contexts[control][key])
                for control in ("C2", "C3", "C4", "C5")
            }
        ) != 1:
            return Evaluation("FAIL", ("canonical_identity_changed",))
    early_identities = [
        by_control[control]["identity"] for control in ("C2", "C3", "C5")
    ]
    if all(isinstance(identity, dict) for identity in early_identities):
        for key in ("server", "company", "trade_mode"):
            if len({str(identity[key]) for identity in early_identities}) != 1:
                return Evaluation("FAIL", ("verified_identity_changed",))
    if manifest is not None:
        authoritative_identity = manifest["expected_identity"]
        assert isinstance(authoritative_identity, Mapping)
        if any(
            early_contexts[control][context_key]
            != authoritative_identity[config_key]
            for control in ("C2", "C3", "C4", "C5")
            for context_key, config_key in (
                ("expected_server", "server"),
                ("expected_company", "company"),
                ("expected_trade_mode", "trade_mode"),
            )
        ):
            return Evaluation(
                "INCONCLUSIVE",
                ("campaign_config_expected_identity_mismatch",),
            )

    c3_context_for_timing = by_control["C3"]["run_context"]
    assert isinstance(c3_context_for_timing, dict)
    c3_completed_for_timing = int(c3_context_for_timing["completed_at_unix"])
    c3_timeline_for_timing = by_control["C3"]["timeline"]
    assert isinstance(c3_timeline_for_timing, Mapping)
    c3_completed_marker_ms = int(
        c3_timeline_for_timing["events"][-1]["timestamp_unix_ms"]  # type: ignore[index]
    )
    direct_context_intervals = {
        control: (
            int(by_control[control]["run_context"]["started_at_unix"]),  # type: ignore[index]
            int(by_control[control]["run_context"]["completed_at_unix"]),  # type: ignore[index]
        )
        for control in ("C3", "C4", "C5")
    }
    direct_timeline_intervals = {
        control: (
            int(by_control[control]["timeline"]["events"][0]["timestamp_unix_ms"]),  # type: ignore[index]
            int(by_control[control]["timeline"]["events"][-1]["timestamp_unix_ms"]),  # type: ignore[index]
        )
        for control in ("C3", "C4", "C5")
    }
    manifest_separation_seconds = (
        None
        if manifest is None
        else int(
            manifest["durations_seconds"]["c5_separation_minimum"]  # type: ignore[index]
        )
    )
    if (
        direct_context_intervals["C3"][1]
        > direct_context_intervals["C4"][0]
        or direct_context_intervals["C4"][1]
        > direct_context_intervals["C5"][0]
        or direct_timeline_intervals["C3"][1]
        > direct_timeline_intervals["C4"][0]
        or direct_timeline_intervals["C4"][1]
        > direct_timeline_intervals["C5"][0]
        or (
            manifest_separation_seconds is not None
            and (
                direct_context_intervals["C5"][0]
                - direct_context_intervals["C3"][1]
                < manifest_separation_seconds
                or direct_timeline_intervals["C5"][0]
                - direct_timeline_intervals["C3"][1]
                < manifest_separation_seconds * 1000
            )
        )
    ):
        return Evaluation(
            "INCONCLUSIVE",
            ("direct_controls_temporal_order_invalid",),
        )
    c2_context_for_timing = by_control["C2"]["run_context"]
    assert isinstance(c2_context_for_timing, dict)
    c2_completed_for_timing = int(c2_context_for_timing["completed_at_unix"])
    for control in ("C3", "C5"):
        cold_context = by_control[control]["run_context"]
        assert isinstance(cold_context, dict)
        if int(cold_context["started_at_unix"]) < c2_completed_for_timing:
            return Evaluation(
                "INCONCLUSIVE",
                ("cold_boot_started_before_candidate_produced",),
            )
    c4_context_for_timing = by_control["C4"]["run_context"]
    assert isinstance(c4_context_for_timing, dict)
    if int(c4_context_for_timing["started_at_unix"]) < c2_completed_for_timing:
        return Evaluation(
            "INCONCLUSIVE",
            ("negative_control_started_before_candidate_produced",),
        )
    if handoff is not None:
        c2_evidence_digest = contract_digest(
            "EVIDENCE",
            EVIDENCE_SCHEMA_VERSION,
            by_control["C2"],
        )
        c2_plan = plans_by_control.get("C2")
        c2_identity = by_control["C2"]["identity"]
        c2_lifecycle_binding = by_control["C2"][
            "lifecycle_binding"
        ]
        c2_initial_pre_state_binding = by_control["C2"][
            "initial_pre_state_binding"
        ]
        c2_proof_binding = by_control["C2"]["proof_binding"]
        if c2_plan is None:
            return Evaluation(
                "INCONCLUSIVE",
                ("candidate_handoff_c2_plan_missing",),
            )
        if (
            handoff["c2_run_id"] != by_control["C2"]["run_id"]
            or handoff["c2_control_plan_sha256"]
            != c2_plan["control_plan_sha256"]
            or handoff["c2_evidence_sha256"] != c2_evidence_digest
        ):
            return Evaluation("FAIL", ("candidate_handoff_c2_binding_mismatch",))
        assert isinstance(c2_lifecycle_binding, Mapping)
        assert isinstance(c2_initial_pre_state_binding, Mapping)
        assert isinstance(c2_proof_binding, Mapping)
        if (
            handoff["lifecycle_mode"]
            != c2_lifecycle_binding["lifecycle_mode"]
            or handoff["c012_session_id"]
            != c2_lifecycle_binding["c012_session_id"]
            or handoff["initial_c012_pre_state_sha256"]
            != c2_initial_pre_state_binding[
                "initial_c012_pre_state_sha256"
            ]
            or handoff["c2_lifecycle_binding_sha256"]
            != c2_proof_binding["lifecycle_binding_sha256"]
        ):
            return Evaluation(
                "FAIL",
                ("candidate_handoff_c012_lifecycle_binding_mismatch",),
            )
        if not isinstance(c2_identity, Mapping):
            return Evaluation(
                "INCONCLUSIVE",
                ("candidate_handoff_c2_identity_missing",),
            )
        expected_handoff_identity = {
            "server": c2_identity["server"],
            "company": c2_identity["company"],
            "trade_mode": c2_identity["trade_mode"],
        }
        if canonical_json(handoff["canonical_identity"]) != canonical_json(
            expected_handoff_identity
        ):
            return Evaluation("FAIL", ("candidate_handoff_identity_mismatch",))
        c2_timeline = by_control["C2"]["timeline"]
        assert isinstance(c2_timeline, Mapping)
        c2_produced_at = int(c2_timeline["events"][-1]["timestamp_unix_ms"])  # type: ignore[index]
        if handoff["produced_at_unix_ms"] != c2_produced_at:
            return Evaluation(
                "FAIL",
                ("candidate_handoff_timestamp_mismatch",),
            )
        if direct_manifest is None:
            return Evaluation(
                "INCONCLUSIVE",
                ("direct_campaign_manifest_missing",),
            )
        if (
            handoff["direct_campaign_manifest_sha256"]
            != direct_manifest["direct_campaign_manifest_sha256"]
        ):
            return Evaluation(
                "FAIL",
                ("candidate_handoff_direct_manifest_mismatch",),
            )
    outcomes = {
        control: _evaluate_validated_evidence(
            by_control[control],
            config_manifest=manifest,
            control_plan=plans_by_control.get(control),
            candidate_handoff=(
                handoff if control in {"C3", "C4", "C5"} else None
            ),
            campaign_handoff_verified=(
                handoff is not None
                and direct_manifest is not None
                and control in {"C3", "C4", "C5"}
            ),
            allow_synthetic=allow_synthetic,
            campaign_c3_completed_at_unix_ms=(
                c3_completed_marker_ms if control == "C5" else None
            ),
        )
        for control in CONTROLS
    }
    failed = [control for control, result in outcomes.items() if result.outcome == "FAIL"]
    if failed:
        return Evaluation("FAIL", tuple(f"{control}_failed" for control in failed))
    if manifest is None:
        return Evaluation(
            "INCONCLUSIVE",
            ("campaign_config_manifest_missing",),
        )
    expected_positive = "SYNTHETIC_PASS" if allow_synthetic else "PASS"
    incomplete = [
        control
        for control, result in outcomes.items()
        if result.outcome != expected_positive
    ]
    if incomplete:
        return Evaluation(
            "INCONCLUSIVE", tuple(f"{control}_inconclusive" for control in incomplete)
        )

    c012_timeline_gap = _validate_c012_timeline_order(by_control)
    if c012_timeline_gap is not None:
        return c012_timeline_gap

    contexts = {control: by_control[control]["run_context"] for control in CONTROLS}
    proofs = {control: by_control[control]["proof_binding"] for control in CONTROLS}
    assert all(isinstance(value, dict) for value in contexts.values())
    assert all(isinstance(value, dict) for value in proofs.values())
    if by_control["C1"]["identity"] is not None:
        return Evaluation("FAIL", ("c1_identity_not_null",))
    if len({str(item["run_id"]) for item in by_control.values()}) != len(CONTROLS):
        return Evaluation("INCONCLUSIVE", ("run_ids_not_unique",))
    if len({str(value["experiment_id"]) for value in contexts.values()}) != 1:
        return Evaluation("INCONCLUSIVE", ("experiment_id_mismatch",))
    if len({str(value["terminal_sha256"]) for value in contexts.values()}) != 1:
        return Evaluation("INCONCLUSIVE", ("terminal_hash_changed",))
    if len({int(value["terminal_build"]) for value in contexts.values()}) != 1:
        return Evaluation("INCONCLUSIVE", ("terminal_build_changed",))

    c012 = [contexts[control] for control in ("C0", "C1", "C2")]
    for key in (
        "clone_id_sha256",
        "windows_user_sid_sha256",
        "portable_root_path_sha256",
    ):
        if len({str(value[key]) for value in c012}) != 1:
            return Evaluation("INCONCLUSIVE", ("c012_not_same_logical_instance",))
    for key, reason in (
        ("job_identity_sha256", "c012_job_identity_mismatch"),
        (
            "root_process_generation_sha256",
            "c012_process_generation_mismatch",
        ),
    ):
        if len({str(proofs[control][key]) for control in ("C0", "C1", "C2")}) != 1:
            return Evaluation("INCONCLUSIVE", (reason,))
    for earlier, later in (("C0", "C1"), ("C1", "C2")):
        if int(contexts[earlier]["completed_at_unix"]) > int(
            contexts[later]["started_at_unix"]
        ):
            return Evaluation("INCONCLUSIVE", ("c012_phase_order_mismatch",))

    cold = [contexts[control] for control in ("C3", "C4", "C5")]
    for key in (
        "clone_id_sha256",
        "windows_user_sid_sha256",
        "portable_root_path_sha256",
    ):
        cold_values = {str(value[key]) for value in cold}
        if len(cold_values) != 3 or str(c012[0][key]) in cold_values:
            return Evaluation("INCONCLUSIVE", ("cold_boot_instances_not_independent",))
    for key, reason in (
        ("job_identity_sha256", "cold_clone_job_identity_reused"),
        (
            "root_process_generation_sha256",
            "cold_clone_process_generation_reused",
        ),
    ):
        c012_value = str(proofs["C0"][key])
        cold_values = {str(proofs[control][key]) for control in ("C3", "C4", "C5")}
        if len(cold_values) != 3 or c012_value in cold_values:
            return Evaluation("INCONCLUSIVE", (reason,))

    candidates = [contexts[control]["candidate_endpoint"] for control in ("C2", "C3", "C4", "C5")]
    if any(candidate is None for candidate in candidates):
        return Evaluation("INCONCLUSIVE", ("campaign_candidate_not_bound",))
    if len({canonical_json(candidate) for candidate in candidates}) != 1:
        return Evaluation("INCONCLUSIVE", ("candidate_changed_across_controls",))
    candidate_digests = {
        str(proofs[control]["candidate_endpoint_sha256"])
        for control in ("C2", "C3", "C4", "C5")
    }
    if len(candidate_digests) != 1:
        return Evaluation("INCONCLUSIVE", ("candidate_proof_binding_changed",))

    credential_set_ids = {
        str(contexts[control]["credential_set_id"])
        for control in ("C2", "C3", "C4", "C5")
    }
    if len(credential_set_ids) != 1:
        return Evaluation("INCONCLUSIVE", ("campaign_credential_set_mismatch",))
    artifact_set_ids = {
        str(proofs[control]["provenance"]["artifact_set_id"])  # type: ignore[index]
        for control in CONTROLS
    }
    if len(artifact_set_ids) != len(CONTROLS):
        return Evaluation("INCONCLUSIVE", ("proof_binding_reused_across_runs",))
    for field, reason in (
        ("phase_timeline_sha256", "phase_timeline_reused_across_runs"),
        ("etw_evidence_sha256", "etw_evidence_reused_across_runs"),
    ):
        if len({str(proofs[control][field]) for control in CONTROLS}) != len(CONTROLS):
            return Evaluation("INCONCLUSIVE", (reason,))

    for field, reason in (
        ("job_manifest_sha256", "cold_clone_job_manifest_reused"),
        ("wfp_evidence_sha256", "cold_clone_wfp_evidence_reused"),
        ("firewall_plan_sha256", "cold_clone_firewall_plan_reused"),
    ):
        if len({str(proofs[control][field]) for control in ("C3", "C4", "C5")}) != 3:
            return Evaluation("INCONCLUSIVE", (reason,))

    c5_timeline = by_control["C5"]["timeline"]
    assert isinstance(c5_timeline, Mapping)
    c5_start_marker_ms = int(
        c5_timeline["events"][0]["timestamp_unix_ms"]  # type: ignore[index]
    )
    derived_separation_ms = c5_start_marker_ms - c3_completed_marker_ms
    if derived_separation_ms < 0:
        return Evaluation("INCONCLUSIVE", ("c5_started_before_c3_completed",))
    assert manifest is not None
    manifest_durations = manifest["durations_seconds"]
    assert isinstance(manifest_durations, Mapping)
    if (
        derived_separation_ms
        < manifest_durations["c5_separation_minimum"] * 1000
    ):
        return Evaluation("INCONCLUSIVE", ("c3_c5_separation_not_proven",))

    identities = [by_control[control]["identity"] for control in ("C2", "C3", "C5")]
    assert all(isinstance(identity, dict) for identity in identities)
    for key in ("server", "company", "trade_mode"):
        if len({str(identity[key]) for identity in identities}) != 1:
            return Evaluation("FAIL", ("verified_identity_changed",))
    for key in ("expected_server", "expected_company", "expected_trade_mode"):
        if len(
            {
                str(contexts[control][key])
                for control in ("C2", "C3", "C4", "C5")
            }
        ) != 1:
                return Evaluation("FAIL", ("canonical_identity_changed",))

    probe_output_digests = {
        str(by_control[control]["identity"]["identity_probe_output_sha256"])  # type: ignore[index]
        for control in ("C2", "C3", "C5")
    }
    if None in (
        by_control["C2"]["identity"],
        by_control["C3"]["identity"],
        by_control["C5"]["identity"],
    ):
        return Evaluation("INCONCLUSIVE", ("identity_probe_output_missing",))
    if (
        "None" in probe_output_digests
        or len(probe_output_digests) != 3
    ):
        return Evaluation(
            "INCONCLUSIVE",
            ("identity_probe_output_reused_across_runs",),
        )

    origins = {
        str(proofs[control]["provenance"]["origin"])  # type: ignore[index]
        for control in CONTROLS
    }
    if origins != {"SYNTHETIC_FIXTURE"}:
        return Evaluation("INCONCLUSIVE", ("campaign_provenance_not_verified",))
    return Evaluation(
        "SYNTHETIC_PASS",
        ("c0_c5_campaign_supports_direct_candidate_reuse",),
    )


def reject_sensitive_content(payload: object) -> None:
    def walk(value: object, path: tuple[str, ...]) -> None:
        if isinstance(value, Mapping):
            for raw_key, child in value.items():
                if not isinstance(raw_key, str):
                    raise LabValidationError("JSON object keys must be strings")
                normalized = raw_key.casefold().replace("-", "_")
                if normalized in FORBIDDEN_EVIDENCE_KEYS:
                    location = ".".join((*path, raw_key))
                    raise LabValidationError(f"forbidden sensitive field: {location}")
                walk(child, (*path, raw_key))
        elif isinstance(value, (list, tuple)):
            for index, child in enumerate(value):
                walk(child, (*path, str(index)))
        elif isinstance(value, str):
            if any(pattern.search(value) for pattern in _SECRET_TEXT_PATTERNS):
                location = ".".join(path) or "<root>"
                raise LabValidationError(f"sensitive text pattern found at {location}")

    walk(payload, ())


def _validate_identity_probe(value: object) -> dict[str, object]:
    reject_sensitive_content(value)
    data = _require_mapping(value, "identity probe")
    bool_keys = {
        "expected_login_loaded",
        "terminal_connected",
        "account_match",
        "account_trade_allowed",
        "account_trade_expert",
        "terminal_trade_allowed",
    }
    required = set(bool_keys) | {
        "schema_version",
        "probe_version",
        "run_id",
        "generated_at_unix",
        "terminal_result",
        "account_server",
        "account_company",
        "account_trade_mode",
        "terminal_build",
        "terminal_path",
        "terminal_data_path",
    }
    _require_exact_keys(data, required, "identity probe")
    if (
        isinstance(data["schema_version"], bool)
        or not isinstance(data["schema_version"], int)
        or data["schema_version"] != IDENTITY_PROBE_SCHEMA_VERSION
    ):
        raise LabValidationError("unsupported identity probe schema_version")
    if data["probe_version"] != IDENTITY_PROBE_VERSION:
        raise LabValidationError("unsupported identity probe probe_version")
    terminal_result = data["terminal_result"]
    if terminal_result not in IDENTITY_PROBE_TERMINAL_RESULTS:
        raise LabValidationError("identity probe terminal_result is invalid")
    result: dict[str, object] = {
        "schema_version": IDENTITY_PROBE_SCHEMA_VERSION,
        "probe_version": IDENTITY_PROBE_VERSION,
        "run_id": _validated_uuid(data["run_id"], "identity probe run_id"),
        "terminal_result": terminal_result,
    }
    for key in sorted(bool_keys):
        if not isinstance(data[key], bool):
            raise LabValidationError(f"identity probe {key} must be boolean")
        result[key] = data[key]
    for key in ("generated_at_unix", "terminal_build"):
        number = data[key]
        if isinstance(number, bool) or not isinstance(number, int) or number < 0:
            raise LabValidationError(f"identity probe {key} must be non-negative")
        result[key] = number
    for key in ("account_server", "account_company"):
        text_value = data[key]
        if not isinstance(text_value, str):
            raise LabValidationError(f"identity probe {key} is invalid")
        result[key] = (
            _validated_text(text_value, f"identity probe {key}", 128)
            if text_value
            else ""
        )
    if data["account_trade_mode"] not in (*TRADE_MODES, "UNKNOWN"):
        raise LabValidationError("identity probe account_trade_mode is invalid")
    result["account_trade_mode"] = data["account_trade_mode"]
    for key in ("terminal_path", "terminal_data_path"):
        path_value = data[key]
        if not isinstance(path_value, str) or any(
            character in path_value for character in "\r\n\x00"
        ):
            raise LabValidationError(f"identity probe {key} is invalid")
        result[key] = (
            _validated_windows_absolute_path(path_value, f"identity probe {key}")
            if path_value
            else ""
        )
    if result["account_match"] and not result["terminal_connected"]:
        raise LabValidationError("identity probe account_match cannot be true while disconnected")

    valid_input_results = {
        "CONNECTED_IDENTITY_AVAILABLE",
        "IDENTITY_MISMATCH",
        "TIMEOUT",
        "NOT_CONNECTED",
    }
    if terminal_result in valid_input_results:
        if not result["expected_login_loaded"]:
            raise LabValidationError(
                "identity probe terminal result requires expected login input"
            )
        if result["run_id"] == UNBOUND_PROBE_RUN_ID:
            raise LabValidationError(
                "identity probe terminal result cannot use unbound run_id"
            )
        if not result["terminal_path"] or not result["terminal_data_path"]:
            raise LabValidationError(
                "identity probe terminal result requires terminal paths"
            )
        if result["terminal_build"] < 1:
            raise LabValidationError(
                "identity probe terminal result requires a positive terminal build"
            )

    if terminal_result == "CONNECTED_IDENTITY_AVAILABLE":
        if not result["terminal_connected"] or not result["account_match"]:
            raise LabValidationError("connected identity result has inconsistent flags")
        if not result["account_server"] or not result["account_company"] or result["account_trade_mode"] not in TRADE_MODES:
            raise LabValidationError("connected identity result lacks complete identity")
    elif terminal_result == "IDENTITY_MISMATCH":
        if not result["terminal_connected"] or result["account_match"]:
            raise LabValidationError("identity mismatch result has inconsistent flags")
        if not result["account_server"] or not result["account_company"] or result["account_trade_mode"] not in TRADE_MODES:
            raise LabValidationError("identity mismatch result lacks complete identity")
    elif terminal_result == "TIMEOUT":
        if not result["terminal_connected"] or result["account_match"]:
            raise LabValidationError("timeout result has inconsistent connection flags")
    elif terminal_result == "NOT_CONNECTED":
        if result["terminal_connected"] or result["account_match"]:
            raise LabValidationError("not-connected result has inconsistent flags")
        if (
            result["account_server"]
            or result["account_company"]
            or result["account_trade_mode"] != "UNKNOWN"
            or result["account_trade_allowed"]
            or result["account_trade_expert"]
        ):
            raise LabValidationError("not-connected result contains account identity")
    return result


def _evaluate_identity(
    identity: object,
    context: Mapping[str, object],
    *,
    control_plan: Mapping[str, object] | None,
    proof_binding: Mapping[str, object],
    timeline: Mapping[str, object],
    config_manifest: Mapping[str, object] | None,
) -> Evaluation | None:
    if identity is None:
        return Evaluation("INCONCLUSIVE", ("identity_not_available",))
    assert isinstance(identity, dict)
    if not identity["account_match"]:
        return Evaluation("FAIL", ("account_identity_mismatch",))
    if (
        not identity["expected_server_match"]
        or not identity["expected_company_match"]
        or identity["server"] != context["expected_server"]
        or identity["company"] != context["expected_company"]
    ):
        return Evaluation("FAIL", ("server_or_company_mismatch",))
    if identity["trade_mode"] != context["expected_trade_mode"] or identity["trade_mode"] != "DEMO":
        return Evaluation("FAIL", ("trade_mode_not_demo",))
    if identity["terminal_build"] != context["terminal_build"]:
        return Evaluation("FAIL", ("probe_terminal_build_mismatch",))
    if (
        not proof_binding["phase_binding_verified"]
        or proof_binding["phase_timeline_sha256"] is None
        or config_manifest is None
    ):
        return Evaluation(
            "INCONCLUSIVE",
            ("probe_timestamp_timeline_binding_missing",),
        )
    authenticated_markers = {
        "C2": ("C2_LOGIN_START", "C2_CONNECTED_END"),
        "C3": ("C3_DIRECT_LOGIN_START", "C3_CONNECTED_STEADY_END"),
        "C5": ("C5_DIRECT_LOGIN_START", "C5_CONNECTED_STEADY_END"),
    }
    marker_pair = authenticated_markers.get(
        str(control_plan["control"]) if control_plan is not None else ""
    )
    if marker_pair is None:
        return Evaluation(
            "INCONCLUSIVE",
            ("probe_authenticated_window_missing",),
        )
    event_by_code = {
        str(event["code"]): event
        for event in timeline["events"]  # type: ignore[index]
        if isinstance(event, Mapping)
    }
    if any(marker not in event_by_code for marker in marker_pair):
        return Evaluation(
            "INCONCLUSIVE",
            ("probe_authenticated_window_missing",),
        )
    durations = config_manifest["durations_seconds"]
    assert isinstance(durations, Mapping)
    tolerance_ms = int(
        durations["probe_timestamp_tolerance_seconds"]
    ) * 1000
    probe_timestamp_ms = int(identity["probe_generated_at_unix"]) * 1000
    authenticated_start_ms = int(
        event_by_code[marker_pair[0]]["timestamp_unix_ms"]
    )
    authenticated_end_ms = int(
        event_by_code[marker_pair[1]]["timestamp_unix_ms"]
    )
    if (
        probe_timestamp_ms < authenticated_start_ms
        or probe_timestamp_ms > authenticated_end_ms + tolerance_ms
    ):
        return Evaluation(
            "FAIL",
            ("identity_probe_timestamp_outside_authenticated_window",),
        )
    if control_plan is not None:
        path_bindings = control_plan["path_bindings"]
        assert isinstance(path_bindings, Mapping)
        for identity_key, plan_key in (
            ("terminal_path_sha256", "terminal_path_sha256"),
            ("terminal_data_path_sha256", "terminal_data_path_sha256"),
        ):
            observed = identity[identity_key]
            if observed is not None and observed != path_bindings[plan_key]:
                return Evaluation("FAIL", ("probe_path_binding_mismatch",))
    asserted_path_binding = proof_binding["probe_path_binding_sha256"]
    if (
        control_plan is not None
        and proof_binding["job_manifest_sha256"] is not None
        and identity["terminal_path_sha256"] is not None
        and identity["terminal_data_path_sha256"] is not None
        and identity["identity_probe_output_sha256"] is not None
    ):
        expected_path_binding = probe_path_binding_digest(
            run_id=identity["probe_run_id"],
            job_manifest_sha256=proof_binding["job_manifest_sha256"],
            portable_root_path_sha256=context[
                "portable_root_path_sha256"
            ],
            control_plan_sha256=control_plan["control_plan_sha256"],
            terminal_path_sha256=identity["terminal_path_sha256"],
            terminal_data_path_sha256=identity["terminal_data_path_sha256"],
            identity_probe_output_sha256=identity[
                "identity_probe_output_sha256"
            ],
            probe_generated_at_unix=identity[
                "probe_generated_at_unix"
            ],
        )
        if (
            asserted_path_binding is not None
            and asserted_path_binding != expected_path_binding
        ):
            return Evaluation("FAIL", ("probe_job_portable_binding_mismatch",))
    if (
        identity["terminal_path_sha256"] is None
        or identity["terminal_data_path_sha256"] is None
        or identity["identity_probe_output_sha256"] is None
        or not identity["probe_path_binding_verified"]
        or asserted_path_binding is None
        or control_plan is None
        or proof_binding["job_manifest_sha256"] is None
    ):
        return Evaluation("INCONCLUSIVE", ("probe_path_binding_missing",))
    if not identity["investor_provenance_confirmed"]:
        return Evaluation("INCONCLUSIVE", ("investor_provenance_not_confirmed",))
    if identity["account_trade_allowed"] or identity["account_trade_expert"]:
        return Evaluation("FAIL", ("account_trading_permission_enabled",))
    if identity["terminal_trade_allowed"]:
        return Evaluation("FAIL", ("terminal_trading_permission_enabled",))
    if not identity["probe_hash_verified"] or not identity["probe_static_guard_passed"]:
        return Evaluation("INCONCLUSIVE", ("identity_probe_not_attested",))
    if not identity["terminal_connected"]:
        return Evaluation("INCONCLUSIVE", ("terminal_not_connected",))
    return None


def _require_run_context(value: object, control: str) -> dict[str, object]:
    data = _require_mapping(value, "run_context")
    _require_exact_keys(
        data,
        {
            "experiment_id",
            "cohort",
            "clone_id_sha256",
            "windows_user_sid_sha256",
            "portable_root_path_sha256",
            "terminal_sha256",
            "terminal_build",
            "expected_server",
            "expected_company",
            "expected_trade_mode",
            "requested_server_label_sha256",
            "credential_set_id",
            "candidate_endpoint",
            "started_at_unix",
            "completed_at_unix",
        },
        "run_context",
    )
    expected_cohort = "C012" if control in {"C0", "C1", "C2"} else control
    if data["cohort"] != expected_cohort:
        raise LabValidationError(f"run_context.cohort must be {expected_cohort} for {control}")

    result: dict[str, object] = {
        "experiment_id": _validated_uuid(data["experiment_id"], "run_context.experiment_id"),
        "cohort": expected_cohort,
    }
    if control in {"C0", "C1"}:
        if (
            data["expected_server"] is not None
            or data["expected_company"] is not None
            or data["expected_trade_mode"] is not None
        ):
            raise LabValidationError(
                f"{control} cannot contain canonical expected identity fields"
            )
        result["expected_server"] = None
        result["expected_company"] = None
        result["expected_trade_mode"] = None
    else:
        result["expected_server"] = _validated_text(
            data["expected_server"], "run_context.expected_server", 128
        )
        result["expected_company"] = _validated_text(
            data["expected_company"], "run_context.expected_company", 128
        )
        if data["expected_trade_mode"] != "DEMO":
            raise LabValidationError("run_context.expected_trade_mode must be DEMO")
        result["expected_trade_mode"] = "DEMO"

    requested_label_digest = data["requested_server_label_sha256"]
    if control in {"C0", "C1", "C2"}:
        if (
            not isinstance(requested_label_digest, str)
            or _SHA256.fullmatch(requested_label_digest) is None
        ):
            raise LabValidationError(
                f"{control} requires run_context.requested_server_label_sha256"
            )
        result["requested_server_label_sha256"] = (
            requested_label_digest.casefold()
        )
    else:
        if requested_label_digest is not None:
            raise LabValidationError(
                f"{control} cannot contain requested-server-label state"
            )
        result["requested_server_label_sha256"] = None
    credential_set_id = data["credential_set_id"]
    if credential_set_id is None:
        result["credential_set_id"] = None
    else:
        normalized_credential_set_id = _validated_uuid(
            credential_set_id, "run_context.credential_set_id"
        )
        if uuid.UUID(normalized_credential_set_id).version != 4:
            raise LabValidationError(
                "run_context.credential_set_id must be an opaque UUIDv4"
            )
        result["credential_set_id"] = normalized_credential_set_id
    for key in (
        "clone_id_sha256",
        "windows_user_sid_sha256",
        "portable_root_path_sha256",
        "terminal_sha256",
    ):
        digest = data[key]
        if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
            raise LabValidationError(f"run_context.{key} must be a SHA-256 digest")
        result[key] = digest.casefold()

    terminal_build = data["terminal_build"]
    if isinstance(terminal_build, bool) or not isinstance(terminal_build, int) or terminal_build < 1:
        raise LabValidationError("run_context.terminal_build must be a positive integer")
    result["terminal_build"] = terminal_build

    candidate = data["candidate_endpoint"]
    if candidate is None:
        result["candidate_endpoint"] = None
    else:
        result["candidate_endpoint"] = validate_candidate(candidate)
    if control in {"C0", "C1"} and result["candidate_endpoint"] is not None:
        raise LabValidationError(f"{control} cannot bind a login candidate endpoint")

    for key in ("started_at_unix", "completed_at_unix"):
        timestamp = data[key]
        if isinstance(timestamp, bool) or not isinstance(timestamp, int) or timestamp < 1:
            raise LabValidationError(f"run_context.{key} must be a positive Unix timestamp")
        result[key] = timestamp
    if int(result["completed_at_unix"]) < int(result["started_at_unix"]):
        raise LabValidationError("run_context completion precedes start")
    return result


def _require_timing(value: object) -> dict[str, int]:
    keys = {
        "baseline_seconds",
        "negative_discovery_seconds",
        "exact_discovery_seconds",
        "login_observation_seconds",
        "connected_steady_seconds",
        "network_interruption_seconds",
        "reconnect_observation_seconds",
        "blocked_observation_seconds",
        "separation_from_c3_seconds",
    }
    data = _require_mapping(value, "timing")
    _require_exact_keys(data, keys, "timing")
    result: dict[str, int] = {}
    for key in sorted(keys):
        item = data[key]
        if isinstance(item, bool) or not isinstance(item, int) or not 0 <= item <= 86400:
            raise LabValidationError(f"timing.{key} must be an integer in 0..86400")
        result[key] = item
    return result


def _require_timeline(
    value: object,
    control: str,
    context: Mapping[str, object],
) -> dict[str, object]:
    data = _require_mapping(value, "timeline")
    _require_exact_keys(
        data,
        {"schema_version", "qpc_frequency_hz", "events"},
        "timeline",
    )
    _require_exact_version(
        data["schema_version"],
        TIMELINE_SCHEMA_VERSION,
        "timeline schema_version",
    )
    frequency = data["qpc_frequency_hz"]
    if (
        isinstance(frequency, bool)
        or not isinstance(frequency, int)
        or not 1 <= frequency <= 10_000_000_000
    ):
        raise LabValidationError("timeline.qpc_frequency_hz is invalid")
    raw_events = data["events"]
    required_codes = _required_phase_markers(control)
    if not isinstance(raw_events, list) or len(raw_events) != len(required_codes):
        raise LabValidationError("timeline event count is invalid")
    events: list[dict[str, object]] = []
    previous_qpc: int | None = None
    previous_timestamp: int | None = None
    for index, (raw_event, expected_code) in enumerate(
        zip(raw_events, required_codes),
        start=1,
    ):
        event = _require_mapping(raw_event, f"timeline.events[{index - 1}]")
        _require_exact_keys(
            event,
            {"code", "sequence", "timestamp_unix_ms", "qpc"},
            f"timeline.events[{index - 1}]",
        )
        if event["code"] != expected_code:
            raise LabValidationError("timeline phase code order is invalid")
        if event["sequence"] != index:
            raise LabValidationError(
                "timeline sequence must be contiguous and strictly increasing"
            )
        timestamp = event["timestamp_unix_ms"]
        qpc = event["qpc"]
        if (
            isinstance(timestamp, bool)
            or not isinstance(timestamp, int)
            or timestamp < 1
        ):
            raise LabValidationError("timeline timestamp_unix_ms is invalid")
        if isinstance(qpc, bool) or not isinstance(qpc, int) or qpc < 0:
            raise LabValidationError("timeline qpc is invalid")
        if previous_timestamp is not None and timestamp < previous_timestamp:
            raise LabValidationError("timeline timestamps are not monotonic")
        if previous_qpc is not None and qpc <= previous_qpc:
            raise LabValidationError("timeline QPC values are not strictly increasing")
        previous_timestamp = timestamp
        previous_qpc = qpc
        events.append(
            {
                "code": expected_code,
                "sequence": index,
                "timestamp_unix_ms": timestamp,
                "qpc": qpc,
            }
        )
    first_timestamp = int(events[0]["timestamp_unix_ms"])
    last_timestamp = int(events[-1]["timestamp_unix_ms"])
    started_ms = int(context["started_at_unix"]) * 1000
    completed_ms_upper = (int(context["completed_at_unix"]) + 1) * 1000
    if first_timestamp < started_ms or last_timestamp >= completed_ms_upper:
        raise LabValidationError(
            "timeline events fall outside the declared run interval"
        )
    return {
        "schema_version": TIMELINE_SCHEMA_VERSION,
        "qpc_frequency_hz": frequency,
        "events": events,
    }


def _derive_timing_from_timeline(
    control: str,
    timeline: Mapping[str, object],
) -> dict[str, int]:
    frequency = int(timeline["qpc_frequency_hz"])
    events = timeline["events"]
    assert isinstance(events, list)
    for previous, current in zip(events, events[1:]):
        assert isinstance(previous, Mapping)
        assert isinstance(current, Mapping)
        qpc_delta = int(current["qpc"]) - int(previous["qpc"])
        utc_delta_ms = (
            int(current["timestamp_unix_ms"])
            - int(previous["timestamp_unix_ms"])
        )
        disagreement = abs(
            qpc_delta * 1_000 - utc_delta_ms * frequency
        )
        if disagreement > TIMELINE_CLOCK_TOLERANCE_MS * frequency:
            raise LabValidationError(
                "timeline QPC and UTC clocks disagree beyond policy tolerance"
            )
    qpc_by_code = {
        str(event["code"]): int(event["qpc"])
        for event in events
        if isinstance(event, Mapping)
    }

    def duration(phase: str) -> int:
        ticks = qpc_by_code[f"{phase}_END"] - qpc_by_code[f"{phase}_START"]
        if ticks <= 0:
            raise LabValidationError(f"timeline phase {phase} has no duration")
        return ticks // frequency

    result = {
        "baseline_seconds": 0,
        "negative_discovery_seconds": 0,
        "exact_discovery_seconds": 0,
        "login_observation_seconds": 0,
        "connected_steady_seconds": 0,
        "network_interruption_seconds": 0,
        "reconnect_observation_seconds": 0,
        "blocked_observation_seconds": 0,
        # Cross-run separation is derived by evaluate_campaign from UTC
        # timeline endpoints; it cannot be asserted by one evidence file.
        "separation_from_c3_seconds": 0,
    }
    if control == "C0":
        result["baseline_seconds"] = duration("C0_BASELINE")
    elif control == "C1":
        result["negative_discovery_seconds"] = duration(
            "C1_DISCOVERY_NEGATIVE"
        )
        result["exact_discovery_seconds"] = duration("C1_DISCOVERY_EXACT")
    elif control == "C2":
        result["login_observation_seconds"] = duration("C2_LOGIN")
        result["connected_steady_seconds"] = duration("C2_CONNECTED")
        result["network_interruption_seconds"] = duration(
            "C2_NETWORK_INTERRUPTION"
        )
        result["reconnect_observation_seconds"] = duration("C2_RECONNECT")
    elif control == "C3":
        result["login_observation_seconds"] = duration("C3_DIRECT_LOGIN")
        result["connected_steady_seconds"] = duration(
            "C3_CONNECTED_STEADY"
        )
    elif control == "C4":
        result["blocked_observation_seconds"] = duration(
            "C4_ENDPOINT_BLOCKED"
        )
    else:
        result["login_observation_seconds"] = duration("C5_DIRECT_LOGIN")
        result["connected_steady_seconds"] = duration(
            "C5_CONNECTED_STEADY"
        )
    return result


def derive_timing_from_timeline(
    control: str,
    timeline_payload: object,
    run_context: Mapping[str, object],
) -> dict[str, int]:
    """Public strict helper used by fixture/export producers."""

    if control not in CONTROLS:
        raise LabValidationError("control must be one of C0..C5")
    timeline = _require_timeline(timeline_payload, control, run_context)
    return _derive_timing_from_timeline(control, timeline)


def _validate_control_invariants(
    *,
    control: str,
    context: Mapping[str, object],
    pre_state: Mapping[str, bool] | None,
    lifecycle_binding: Mapping[str, object],
    state_transition: Mapping[str, object],
    identity: Mapping[str, object] | None,
    credential_bundle_investor_confirmed: bool,
    network: Mapping[str, object],
    discovery: Mapping[str, object],
    health: Mapping[str, object],
    proof_binding: Mapping[str, object],
    timing: Mapping[str, int],
) -> None:
    """Reject contradictory or non-applicable evidence before evaluation.

    Proof sufficiency remains an evaluator concern.  That distinction keeps the
    explicit FAIL/INCONCLUSIVE reason codes reachable while ensuring a shared
    object shape cannot smuggle observations from another control.
    """

    def require_values(
        values: Mapping[str, object],
        expected: Mapping[str, object],
        namespace: str,
    ) -> None:
        for key, expected_value in expected.items():
            if values[key] != expected_value:
                raise LabValidationError(
                    f"{namespace}.{key} is not applicable to {control}"
                )

    candidate = context["candidate_endpoint"]
    if (
        identity is None
        and proof_binding["probe_path_binding_sha256"] is not None
    ):
        raise LabValidationError(
            "probe path binding is not applicable without identity evidence"
        )
    candidate_neutral = {
        "candidate_attempt_observed": False,
        "candidate_connected": False,
        "candidate_block_observed": False,
        "candidate_endpoint_safe": False,
        "candidate_tuple_match": False,
        "candidate_observed_phase": "NONE",
        "candidate_tcp_flows": 0,
    }
    if candidate is None:
        require_values(network, candidate_neutral, "network")
        require_values(
            proof_binding,
            {
                "candidate_endpoint_sha256": None,
                "candidate_tuple_bound": False,
            },
            "proof_binding",
        )
    else:
        if network["candidate_connected"] and not network["candidate_attempt_observed"]:
            raise LabValidationError(
                "network.candidate_connected requires a candidate attempt"
            )
        if network["candidate_block_observed"] and not network[
            "candidate_attempt_observed"
        ]:
            raise LabValidationError(
                "network.candidate_block_observed requires a candidate attempt"
            )
        if network["candidate_tcp_flows"] and not network[
            "candidate_attempt_observed"
        ]:
            raise LabValidationError(
                "candidate process-scoped flows require a candidate attempt"
            )
        if (
            network["candidate_observed_phase"] == "NONE"
            and (
                network["candidate_attempt_observed"]
                or network["candidate_connected"]
                or network["candidate_block_observed"]
                or network["candidate_tuple_match"]
                or network["candidate_tcp_flows"]
            )
        ):
            raise LabValidationError(
                "candidate activity cannot use candidate_observed_phase NONE"
            )

    if discovery["selected_server_label_sha256"] is None and discovery[
        "exact_label_match_verified"
    ]:
        raise LabValidationError(
            "exact label match cannot be verified without a selected-label digest"
        )

    neutral_discovery = {
        "credentials_supplied": False,
        "negative_exact_query_completed": False,
        "expected_exact_result_count": 0,
        "exact_selection_completed": False,
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
    zero_timing = {key: 0 for key in timing}

    if control in {"C0", "C3", "C4", "C5"}:
        assert pre_state is not None
    elif pre_state is not None:
        raise LabValidationError(
            f"{control} must not duplicate the C012 initial pre_state"
        )

    if control == "C0":
        if identity is not None:
            raise LabValidationError("C0 identity must be null")
        if credential_bundle_investor_confirmed:
            raise LabValidationError(
                "C0 cannot confirm an investor credential bundle"
            )
        require_values(discovery, neutral_discovery, "discovery")
        require_values(
            timing,
            {
                key: value
                for key, value in zero_timing.items()
                if key != "baseline_seconds"
            },
            "timing",
        )
        require_values(
            health,
            {
                "build_unchanged": True,
                "clock_synchronized": True,
                "firewall_policy_verified": False,
                "account_available": False,
                "external_outage_excluded": False,
                "baseline_stable": True,
                "ui_compatible": False,
            },
            "environment_health",
        )
    elif control == "C1":
        if identity is not None:
            raise LabValidationError("C1 identity must be null")
        if credential_bundle_investor_confirmed:
            raise LabValidationError(
                "C1 cannot confirm an investor credential bundle"
            )
        if discovery["credentials_supplied"]:
            raise LabValidationError(
                "C1 discovery cannot contain supplied credentials"
            )
        if discovery["endpoint_delta_acquired"] and (
            discovery["endpoint_delta_source"] == "NONE"
            or discovery["endpoint_delta_source_sha256"] is None
            or not discovery["endpoint_delta_source_verified"]
            or (
                discovery["endpoint_delta_source"]
                == "PROCESS_SCOPED_TCP_FLOW_SET"
                and (
                    network["process_scoped_tcp_flows"] < 1
                    or network["flow_record_set_sha256"] is None
                    or not network["flow_record_set_verified"]
                    or discovery["endpoint_delta_source_sha256"]
                    != network["flow_record_set_sha256"]
                )
            )
        ):
            raise LabValidationError(
                "C1 endpoint delta requires an observed, verified source"
            )
        require_values(
            timing,
            {
                key: value
                for key, value in zero_timing.items()
                if key
                not in {"negative_discovery_seconds", "exact_discovery_seconds"}
            },
            "timing",
        )
        require_values(
            health,
            {
                "build_unchanged": True,
                "clock_synchronized": True,
                "firewall_policy_verified": False,
                "account_available": False,
                "external_outage_excluded": False,
                "baseline_stable": False,
                "ui_compatible": True,
            },
            "environment_health",
        )
    elif control == "C2":
        if identity is None:
            raise LabValidationError("C2 identity must be present")
        if candidate is None:
            raise LabValidationError("C2 candidate endpoint must be present")
        require_values(discovery, neutral_discovery, "discovery")
        require_values(
            network,
            {
                "candidate_block_observed": False,
            },
            "network",
        )
        require_values(
            timing,
            {
                key: value
                for key, value in zero_timing.items()
                if key
                not in {
                    "login_observation_seconds",
                    "connected_steady_seconds",
                    "network_interruption_seconds",
                    "reconnect_observation_seconds",
                }
            },
            "timing",
        )
        require_values(
            health,
            {
                "firewall_policy_verified": False,
                "baseline_stable": False,
                "ui_compatible": False,
            },
            "environment_health",
        )
    elif control in {"C3", "C5"}:
        if identity is None:
            raise LabValidationError(f"{control} identity must be present")
        if candidate is None:
            raise LabValidationError(f"{control} candidate endpoint must be present")
        require_values(discovery, neutral_discovery, "discovery")
        allowed_timing = {
            "login_observation_seconds",
            "connected_steady_seconds",
        }
        if control == "C5":
            allowed_timing.add("separation_from_c3_seconds")
        require_values(
            timing,
            {
                key: value
                for key, value in zero_timing.items()
                if key not in allowed_timing
            },
            "timing",
        )
        if network["candidate_block_observed"]:
            raise LabValidationError(
                f"{control} cannot contain a blocked-candidate observation"
            )
        require_values(
            health,
            {
                "baseline_stable": False,
                "ui_compatible": False,
            },
            "environment_health",
        )
    else:
        if identity is not None:
            raise LabValidationError("C4 identity must be null")
        if candidate is None:
            raise LabValidationError("C4 candidate endpoint must be present")
        require_values(discovery, neutral_discovery, "discovery")
        require_values(
            timing,
            {
                key: value
                for key, value in zero_timing.items()
                if key != "blocked_observation_seconds"
            },
            "timing",
        )
        if network["candidate_connected"]:
            raise LabValidationError(
                "C4 cannot contain a connected-candidate observation"
            )
        require_values(
            health,
            {
                "baseline_stable": False,
                "ui_compatible": False,
            },
            "environment_health",
        )

    if control in {"C0", "C1", "C2"}:
        require_values(
            proof_binding,
            {
                "wfp_evidence_sha256": None,
                "firewall_plan_sha256": None,
                "firewall_portable_root_binding_sha256": None,
                "wfp_proof_capable": False,
                "firewall_plan_bound": False,
                "candidate_handoff_manifest_sha256": None,
            },
            "proof_binding",
        )
    else:
        require_values(
            context,
            {"requested_server_label_sha256": None},
            "run_context",
        )
        require_values(
            discovery,
            {
                "selected_server_label_sha256": None,
                "exact_label_match_verified": False,
            },
            "discovery",
        )
        require_values(
            proof_binding,
            {
                "requested_label_binding_sha256": None,
                "requested_label_binding_verified": False,
                "negative_query_binding_sha256": None,
            },
            "proof_binding",
        )

    if control != "C1":
        require_values(
            discovery,
            {
                "selected_server_label_sha256": None,
                "exact_label_match_verified": False,
                "negative_query_label_sha256": None,
                "negative_query_result_count": None,
                "negative_query_ui_binding_verified": False,
            },
            "discovery",
        )
        require_values(
            proof_binding,
            {"negative_query_binding_sha256": None},
            "proof_binding",
        )

    allowed_phase = {
        "C0": "NONE",
        "C1": "NONE",
        "C2": "LOGIN",
        "C3": "DIRECT_ONLY",
        "C4": "ENDPOINT_BLOCKED",
        "C5": "DIRECT_REPEAT",
    }[control]
    if network["candidate_observed_phase"] not in {"NONE", allowed_phase}:
        raise LabValidationError(
            f"network.candidate_observed_phase is not valid for {control}"
        )


def _required_phase_markers(control: str) -> tuple[str, ...]:
    phases = {
        "C0": ("C0_BASELINE",),
        "C1": (
            "C1_DISCOVERY_NEGATIVE",
            "C1_DISCOVERY_EXACT",
        ),
        "C2": (
            "C2_LOGIN",
            "C2_CONNECTED",
            "C2_NETWORK_INTERRUPTION",
            "C2_RECONNECT",
        ),
        "C3": (
            "C3_DIRECT_LOGIN",
            "C3_CONNECTED_STEADY",
        ),
        "C4": ("C4_ENDPOINT_BLOCKED",),
        "C5": (
            "C5_DIRECT_LOGIN",
            "C5_CONNECTED_STEADY",
        ),
    }[control]
    return tuple(
        f"{phase}_{boundary}"
        for phase in phases
        for boundary in ("START", "END")
    )


def _all_phase_markers() -> set[str]:
    return {
        marker
        for control in CONTROLS
        for marker in _required_phase_markers(control)
    }


def _require_identity(value: object) -> dict[str, object] | None:
    if value is None:
        return None
    data = _require_mapping(value, "identity")
    bool_keys = {
        "account_match",
        "expected_server_match",
        "expected_company_match",
        "account_trade_allowed",
        "account_trade_expert",
        "terminal_connected",
        "terminal_trade_allowed",
        "investor_provenance_confirmed",
        "probe_hash_verified",
        "probe_static_guard_passed",
        "probe_path_binding_verified",
    }
    required = set(bool_keys) | {
        "probe_run_id",
        "probe_generated_at_unix",
        "server",
        "company",
        "trade_mode",
        "terminal_build",
        "terminal_path_sha256",
        "terminal_data_path_sha256",
        "identity_probe_output_sha256",
    }
    _require_exact_keys(data, required, "identity")
    result: dict[str, object] = {}
    for key in sorted(bool_keys):
        if not isinstance(data[key], bool):
            raise LabValidationError(f"identity.{key} must be boolean")
        result[key] = data[key]
    result["probe_run_id"] = _validated_uuid(
        data["probe_run_id"], "identity.probe_run_id"
    )
    probe_generated_at_unix = data["probe_generated_at_unix"]
    if (
        isinstance(probe_generated_at_unix, bool)
        or not isinstance(probe_generated_at_unix, int)
        or probe_generated_at_unix < 1
    ):
        raise LabValidationError(
            "identity.probe_generated_at_unix must be positive"
        )
    result["probe_generated_at_unix"] = probe_generated_at_unix
    result["server"] = _validated_text(data["server"], "identity.server", 128)
    result["company"] = _validated_text(data["company"], "identity.company", 128)
    if data["trade_mode"] not in TRADE_MODES:
        raise LabValidationError("identity.trade_mode is invalid")
    result["trade_mode"] = data["trade_mode"]
    terminal_build = data["terminal_build"]
    if (
        isinstance(terminal_build, bool)
        or not isinstance(terminal_build, int)
        or terminal_build < 1
    ):
        raise LabValidationError("identity.terminal_build must be positive")
    result["terminal_build"] = terminal_build
    for key in (
        "terminal_path_sha256",
        "terminal_data_path_sha256",
        "identity_probe_output_sha256",
    ):
        result[key] = _validated_nullable_sha256(data[key], f"identity.{key}")
    return result


def _require_network(value: object) -> dict[str, object]:
    data = _require_mapping(value, "network")
    bool_keys = {
        "candidate_attempt_observed",
        "candidate_connected",
        "candidate_block_observed",
        "candidate_endpoint_safe",
        "candidate_tuple_match",
        "attribution_unambiguous",
        "flow_record_set_verified",
    }
    int_keys = {
        "process_scoped_tcp_flows",
        "candidate_tcp_flows",
        "other_tcp_flows",
        "dns_events",
        "non_tcp_network_events",
    }
    _require_exact_keys(
        data,
        set(bool_keys)
        | set(int_keys)
        | {"candidate_observed_phase", "flow_record_set_sha256"},
        "network",
    )
    result = _require_bool_int_mapping(
        {key: data[key] for key in set(bool_keys) | set(int_keys)},
        bool_keys,
        int_keys,
        "network",
    )
    observed_phase = data["candidate_observed_phase"]
    if observed_phase not in OBSERVED_PHASES:
        raise LabValidationError("network.candidate_observed_phase is invalid")
    result["candidate_observed_phase"] = observed_phase
    result["flow_record_set_sha256"] = _validated_nullable_sha256(
        data["flow_record_set_sha256"],
        "network.flow_record_set_sha256",
    )
    if result["process_scoped_tcp_flows"] != (
        result["candidate_tcp_flows"] + result["other_tcp_flows"]
    ):
        raise LabValidationError(
            "network TCP accounting must satisfy total=candidate+other"
        )
    candidate_flow_present = result["candidate_tcp_flows"] > 0
    if result["candidate_attempt_observed"] != candidate_flow_present:
        raise LabValidationError(
            "network candidate_attempt_observed must derive from candidate_tcp_flows"
        )
    if (
        result["candidate_connected"] or result["candidate_block_observed"]
    ) and not candidate_flow_present:
        raise LabValidationError(
            "candidate disposition requires a candidate TCP flow"
        )
    if result["candidate_connected"] and result["candidate_block_observed"]:
        raise LabValidationError(
            "candidate connected and blocked dispositions are mutually exclusive"
        )
    if result["flow_record_set_verified"] and result["flow_record_set_sha256"] is None:
        raise LabValidationError(
            "verified flow record set requires a digest"
        )
    return result


def _require_lifecycle_binding(
    value: object, control: str
) -> dict[str, object]:
    data = _require_mapping(value, "lifecycle_binding")
    _require_exact_keys(
        data,
        {
            "schema_version",
            "lifecycle_mode",
            "c012_session_id",
            "session_role",
            "job_id",
            "job_manifest_sha256",
            "job_identity_sha256",
            "root_process_generation_sha256",
            "terminal_alive_at_start",
            "terminal_alive_at_end",
            "session_retained",
            "teardown_completed",
            "bootstrap_submitted_to_existing_session",
            "transient_process_set_sha256",
            "transient_process_same_job_verified",
        },
        "lifecycle_binding",
    )
    _require_exact_version(
        data["schema_version"], 1, "lifecycle_binding.schema_version"
    )
    early = control in {"C0", "C1", "C2"}
    expected = {
        "C0": {
            "lifecycle_mode": C012_LIFECYCLE_MODE,
            "session_role": "LAUNCH_RETAIN",
            "terminal_alive_at_start": False,
            "terminal_alive_at_end": True,
            "session_retained": True,
            "teardown_completed": False,
            "bootstrap_submitted_to_existing_session": False,
            "transient_process_same_job_verified": False,
        },
        "C1": {
            "lifecycle_mode": C012_LIFECYCLE_MODE,
            "session_role": "REUSE_RETAIN",
            "terminal_alive_at_start": True,
            "terminal_alive_at_end": True,
            "session_retained": True,
            "teardown_completed": False,
            "bootstrap_submitted_to_existing_session": False,
            "transient_process_same_job_verified": False,
        },
        "C2": {
            "lifecycle_mode": C012_LIFECYCLE_MODE,
            "session_role": "REUSE_CONFIG_SUBMIT_TEARDOWN",
            "terminal_alive_at_start": True,
            "terminal_alive_at_end": False,
            "session_retained": False,
            "teardown_completed": True,
            "bootstrap_submitted_to_existing_session": True,
            "transient_process_same_job_verified": True,
        },
    }.get(
        control,
        {
            "lifecycle_mode": DIRECT_LIFECYCLE_MODE,
            "session_role": "INDEPENDENT_LAUNCH_TEARDOWN",
            "terminal_alive_at_start": False,
            "terminal_alive_at_end": False,
            "session_retained": False,
            "teardown_completed": True,
            "bootstrap_submitted_to_existing_session": False,
            "transient_process_same_job_verified": False,
        },
    )
    for key, expected_value in expected.items():
        if data[key] != expected_value:
            raise LabValidationError(
                f"lifecycle_binding.{key} is invalid for {control}"
            )
    session_id = data["c012_session_id"]
    if early:
        normalized_session_id: str | None = _validated_uuid(
            session_id, "lifecycle_binding.c012_session_id"
        )
    else:
        if session_id is not None:
            raise LabValidationError(
                f"{control} cannot contain a C012 session ID"
            )
        normalized_session_id = None
    transient_digest = _validated_nullable_sha256(
        data["transient_process_set_sha256"],
        "lifecycle_binding.transient_process_set_sha256",
    )
    if control == "C2":
        if transient_digest is None:
            raise LabValidationError(
                "C2 requires a transient process-set commitment"
            )
    elif transient_digest is not None:
        raise LabValidationError(
            f"{control} cannot contain a transient process-set commitment"
        )
    return {
        "schema_version": 1,
        "lifecycle_mode": expected["lifecycle_mode"],
        "c012_session_id": normalized_session_id,
        "session_role": expected["session_role"],
        "job_id": _validated_uuid(data["job_id"], "lifecycle_binding.job_id"),
        "job_manifest_sha256": _validated_sha256(
            data["job_manifest_sha256"],
            "lifecycle_binding.job_manifest_sha256",
        ),
        "job_identity_sha256": _validated_sha256(
            data["job_identity_sha256"],
            "lifecycle_binding.job_identity_sha256",
        ),
        "root_process_generation_sha256": _validated_sha256(
            data["root_process_generation_sha256"],
            "lifecycle_binding.root_process_generation_sha256",
        ),
        "terminal_alive_at_start": expected["terminal_alive_at_start"],
        "terminal_alive_at_end": expected["terminal_alive_at_end"],
        "session_retained": expected["session_retained"],
        "teardown_completed": expected["teardown_completed"],
        "bootstrap_submitted_to_existing_session": expected[
            "bootstrap_submitted_to_existing_session"
        ],
        "transient_process_set_sha256": transient_digest,
        "transient_process_same_job_verified": expected[
            "transient_process_same_job_verified"
        ],
    }


def _require_state_transition(
    value: object, control: str
) -> dict[str, object]:
    data = _require_mapping(value, "state_transition")
    _require_exact_keys(
        data,
        {
            "schema_version",
            "stage",
            "broker_cache_state",
            "account_cache_state",
            "sensitive_material_exported",
            "transition_evidence_sha256",
            "transition_verified",
        },
        "state_transition",
    )
    _require_exact_version(
        data["schema_version"], 1, "state_transition.schema_version"
    )
    transition_policy = {
        "C0": ("C0_INITIAL", {"ABSENT"}, {"ABSENT"}),
        "C1": (
            "C1_DISCOVERY_COMPLETE",
            {"CREATED_RECORDED", "ABSENT_RECORDED"},
            {"ABSENT_RECORDED"},
        ),
        "C2": (
            "C2_LOGIN_COMPLETE",
            {
                "INHERITED_RECORDED",
                "CREATED_RECORDED",
                "ABSENT_RECORDED",
            },
            {"CREATED_RECORDED", "ABSENT_RECORDED"},
        ),
        "C3": ("COLD_BOOT_INITIAL", {"ABSENT"}, {"ABSENT"}),
        "C4": ("COLD_BOOT_INITIAL", {"ABSENT"}, {"ABSENT"}),
        "C5": ("COLD_BOOT_INITIAL", {"ABSENT"}, {"ABSENT"}),
    }[control]
    stage, allowed_broker_states, allowed_account_states = transition_policy
    if data["stage"] != stage:
        raise LabValidationError(
            f"state_transition.stage is invalid for {control}"
        )
    if data["broker_cache_state"] not in allowed_broker_states:
        raise LabValidationError(
            f"state_transition.broker_cache_state is invalid for {control}"
        )
    if data["account_cache_state"] not in allowed_account_states:
        raise LabValidationError(
            f"state_transition.account_cache_state is invalid for {control}"
        )
    if data["sensitive_material_exported"] is not False:
        raise LabValidationError(
            "state_transition cannot export sensitive material"
        )
    if not isinstance(data["transition_verified"], bool):
        raise LabValidationError(
            "state_transition.transition_verified must be boolean"
        )
    transition_digest = _validated_nullable_sha256(
        data["transition_evidence_sha256"],
        "state_transition.transition_evidence_sha256",
    )
    if control in {"C1", "C2"}:
        if transition_digest is None:
            raise LabValidationError(
                f"{control} requires transition evidence"
            )
    elif transition_digest is not None:
        raise LabValidationError(
            f"{control} transition evidence is not applicable"
        )
    return {
        "schema_version": 1,
        "stage": stage,
        "broker_cache_state": data["broker_cache_state"],
        "account_cache_state": data["account_cache_state"],
        "sensitive_material_exported": False,
        "transition_evidence_sha256": transition_digest,
        "transition_verified": data["transition_verified"],
    }


def _require_proof_binding(value: object) -> dict[str, object]:
    data = _require_mapping(value, "proof_binding")
    digest_keys = {
        "job_manifest_sha256",
        "phase_timeline_sha256",
        "etw_evidence_sha256",
        "wfp_evidence_sha256",
        "firewall_plan_sha256",
        "candidate_endpoint_sha256",
        "credential_set_binding_sha256",
        "requested_label_binding_sha256",
        "job_identity_sha256",
        "root_process_generation_sha256",
        "experiment_manifest_sha256",
        "control_plan_sha256",
        "candidate_handoff_manifest_sha256",
        "probe_path_binding_sha256",
        "lifecycle_binding_sha256",
        "job_portable_root_binding_sha256",
        "pre_state_binding_sha256",
        "state_transition_sha256",
        "firewall_portable_root_binding_sha256",
        "negative_query_binding_sha256",
    }
    bool_keys = {
        "job_process_binding_verified",
        "phase_binding_verified",
        "etw_proof_capable",
        "wfp_proof_capable",
        "firewall_plan_bound",
        "candidate_tuple_bound",
        "credential_set_binding_verified",
        "requested_label_binding_verified",
    }
    _require_exact_keys(
        data,
        {"schema_version", "run_id", "provenance"} | digest_keys | bool_keys,
        "proof_binding",
    )
    proof_schema_version = data["schema_version"]
    if (
        isinstance(proof_schema_version, bool)
        or not isinstance(proof_schema_version, int)
        or proof_schema_version != PROOF_BINDING_SCHEMA_VERSION
    ):
        raise LabValidationError("unsupported proof_binding schema_version")

    provenance = _require_mapping(data["provenance"], "proof_binding.provenance")
    _require_exact_keys(
        provenance,
        {"origin", "synthetic_fixture", "producer", "artifact_set_id"},
        "proof_binding.provenance",
    )
    origin = provenance["origin"]
    if origin not in PROVENANCE_PRODUCERS:
        raise LabValidationError("proof_binding.provenance.origin is invalid")
    synthetic = provenance["synthetic_fixture"]
    if not isinstance(synthetic, bool):
        raise LabValidationError("proof_binding.provenance.synthetic_fixture must be boolean")
    if synthetic != (origin == "SYNTHETIC_FIXTURE"):
        raise LabValidationError("proof_binding provenance origin/synthetic marker mismatch")
    producer = provenance["producer"]
    if producer != PROVENANCE_PRODUCERS[origin]:
        raise LabValidationError(
            "proof_binding provenance origin/producer code mismatch"
        )

    result: dict[str, object] = {
        "schema_version": PROOF_BINDING_SCHEMA_VERSION,
        "run_id": _validated_uuid(data["run_id"], "proof_binding.run_id"),
        "provenance": {
            "origin": origin,
            "synthetic_fixture": synthetic,
            "producer": producer,
            "artifact_set_id": _validated_uuid(
                provenance["artifact_set_id"],
                "proof_binding.provenance.artifact_set_id",
            ),
        },
    }
    for key in sorted(digest_keys):
        digest = data[key]
        if digest is None:
            result[key] = None
        elif not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
            raise LabValidationError(f"proof_binding.{key} must be null or a SHA-256 digest")
        else:
            result[key] = digest.casefold()
    for key in ("job_identity_sha256", "root_process_generation_sha256"):
        if result[key] is None:
            raise LabValidationError(f"proof_binding.{key} must be a SHA-256 digest")
    for key in sorted(bool_keys):
        if not isinstance(data[key], bool):
            raise LabValidationError(f"proof_binding.{key} must be boolean")
        result[key] = data[key]
    return result


def _require_discovery(value: object) -> dict[str, object]:
    data = _require_mapping(value, "discovery")
    _require_exact_keys(
        data,
        {
            "credentials_supplied",
            "negative_exact_query_completed",
            "expected_exact_result_count",
            "exact_selection_completed",
            "helper_secret_accessed",
            "unsafe_endpoint_promoted",
            "cache_influence_excluded",
            "endpoint_delta_acquired",
            "endpoint_delta_source",
            "endpoint_delta_source_sha256",
            "endpoint_delta_source_verified",
            "selected_server_label_sha256",
            "exact_label_match_verified",
            "negative_query_label_sha256",
            "negative_query_result_count",
            "negative_query_ui_binding_verified",
        },
        "discovery",
    )
    result: dict[str, object] = {}
    for key in (
        "credentials_supplied",
        "negative_exact_query_completed",
        "exact_selection_completed",
        "helper_secret_accessed",
        "unsafe_endpoint_promoted",
        "cache_influence_excluded",
        "endpoint_delta_acquired",
        "endpoint_delta_source_verified",
        "exact_label_match_verified",
        "negative_query_ui_binding_verified",
    ):
        if not isinstance(data[key], bool):
            raise LabValidationError(f"discovery.{key} must be boolean")
        result[key] = data[key]
    count = data["expected_exact_result_count"]
    if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= 1000:
        raise LabValidationError("discovery.expected_exact_result_count is invalid")
    result["expected_exact_result_count"] = count
    negative_count = data["negative_query_result_count"]
    if (
        negative_count is not None
        and (
            isinstance(negative_count, bool)
            or not isinstance(negative_count, int)
            or not 0 <= negative_count <= 1000
        )
    ):
        raise LabValidationError(
            "discovery.negative_query_result_count is invalid"
        )
    result["negative_query_result_count"] = negative_count
    result["negative_query_label_sha256"] = _validated_nullable_sha256(
        data["negative_query_label_sha256"],
        "discovery.negative_query_label_sha256",
    )
    selected_digest = data["selected_server_label_sha256"]
    if selected_digest is None:
        result["selected_server_label_sha256"] = None
    elif not isinstance(selected_digest, str) or _SHA256.fullmatch(selected_digest) is None:
        raise LabValidationError(
            "discovery.selected_server_label_sha256 must be null or a SHA-256 digest"
        )
    else:
        result["selected_server_label_sha256"] = selected_digest.casefold()
    source = data["endpoint_delta_source"]
    if source not in {
        "NONE",
        "PROCESS_SCOPED_TCP_FLOW_SET",
    }:
        raise LabValidationError("discovery.endpoint_delta_source is invalid")
    result["endpoint_delta_source"] = source
    result["endpoint_delta_source_sha256"] = _validated_nullable_sha256(
        data["endpoint_delta_source_sha256"],
        "discovery.endpoint_delta_source_sha256",
    )
    if source == "NONE":
        if (
            result["endpoint_delta_source_sha256"] is not None
            or result["endpoint_delta_source_verified"]
        ):
            raise LabValidationError(
                "discovery NONE delta source must be neutral"
            )
    elif (
        result["endpoint_delta_source_sha256"] is None
        or not result["endpoint_delta_source_verified"]
    ):
        raise LabValidationError(
            "discovery endpoint delta source must carry a verified digest"
        )
    if not result["endpoint_delta_acquired"] and source != "NONE":
        raise LabValidationError(
            "discovery delta source is not applicable without an acquired delta"
        )
    return result


def _require_bool_mapping(
    value: object, keys: set[str], context: str
) -> dict[str, bool]:
    data = _require_mapping(value, context)
    _require_exact_keys(data, keys, context)
    result: dict[str, bool] = {}
    for key in sorted(keys):
        if not isinstance(data[key], bool):
            raise LabValidationError(f"{context}.{key} must be boolean")
        result[key] = data[key]
    return result


def _require_bool_int_mapping(
    value: object,
    bool_keys: set[str],
    int_keys: set[str],
    context: str,
) -> dict[str, object]:
    data = _require_mapping(value, context)
    _require_exact_keys(data, set(bool_keys) | set(int_keys), context)
    result: dict[str, object] = {}
    for key in sorted(bool_keys):
        if not isinstance(data[key], bool):
            raise LabValidationError(f"{context}.{key} must be boolean")
        result[key] = data[key]
    for key in sorted(int_keys):
        value_at_key = data[key]
        if isinstance(value_at_key, bool) or not isinstance(value_at_key, int) or value_at_key < 0:
            raise LabValidationError(f"{context}.{key} must be a non-negative integer")
        result[key] = value_at_key
    return result


def _require_mapping(value: object, context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise LabValidationError(f"{context} must be a JSON object")
    return value


def _require_exact_keys(
    payload: Mapping[str, Any], expected: set[str], context: str
) -> None:
    if set(payload) != expected:
        missing = sorted(expected - set(payload))
        extra = sorted(set(payload) - expected)
        raise LabValidationError(
            f"{context} has invalid fields (missing={missing}, extra={extra})"
        )


def _validated_sha256(value: object, field: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise LabValidationError(f"{field} must be a SHA-256 digest")
    return value.casefold()


def _require_exact_version(value: object, expected: int, field: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value != expected
    ):
        raise LabValidationError(f"unsupported {field}")
    return expected


def _validated_nullable_sha256(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _validated_sha256(value, field)


def _validate_expected_identity(value: object) -> dict[str, object]:
    data = _require_mapping(value, "expected identity")
    _require_exact_keys(
        data,
        {"server", "company", "trade_mode"},
        "expected identity",
    )
    if data["trade_mode"] != "DEMO":
        raise LabValidationError("expected identity trade_mode must be DEMO")
    return {
        "server": _validated_text(data["server"], "expected identity server", 128),
        "company": _validated_text(
            data["company"], "expected identity company", 128
        ),
        "trade_mode": "DEMO",
    }


def _validate_config_probe(value: object) -> dict[str, object]:
    data = _require_mapping(value, "configured probe")
    _require_exact_keys(
        data,
        {
            "schema_version",
            "probe_version",
            "source_sha256",
            "policy",
            "symbol",
        },
        "configured probe",
    )
    _require_exact_version(
        data["schema_version"],
        IDENTITY_PROBE_SCHEMA_VERSION,
        "configured probe schema_version",
    )
    if (
        data["probe_version"] != IDENTITY_PROBE_VERSION
        or data["policy"] != PROBE_POLICY
    ):
        raise LabValidationError("configured probe contract is invalid")
    return {
        "schema_version": IDENTITY_PROBE_SCHEMA_VERSION,
        "probe_version": IDENTITY_PROBE_VERSION,
        "source_sha256": _validated_sha256(
            data["source_sha256"], "configured probe source_sha256"
        ),
        "policy": PROBE_POLICY,
        "symbol": _validated_text(data["symbol"], "configured probe symbol", 64),
    }


def _validate_network_policy(value: object) -> dict[str, object]:
    data = _require_mapping(value, "network policy")
    _require_exact_keys(
        data,
        {
            "transport",
            "allowed_remote_ports",
            "candidate_address_policy",
            "candidate_source_policy",
            "direct_egress_policy",
            "direct_dns_events_max",
            "direct_other_tcp_flows_max",
            "external_deny_role",
        },
        "network policy",
    )
    if data["transport"] != "TCP":
        raise LabValidationError("network policy transport must be TCP")
    raw_ports = data["allowed_remote_ports"]
    if not isinstance(raw_ports, list) or not raw_ports or len(raw_ports) > 64:
        raise LabValidationError("network policy allowed ports are invalid")
    ports: list[int] = []
    for port in raw_ports:
        if (
            isinstance(port, bool)
            or not isinstance(port, int)
            or not 1 <= port <= 65535
            or port in DANGEROUS_REMOTE_PORTS
        ):
            raise LabValidationError("network policy contains an unsafe port")
        ports.append(port)
    if ports != sorted(set(ports)):
        raise LabValidationError("network policy ports must be sorted and unique")
    constants = {
        "candidate_address_policy": "GLOBAL_LITERAL_ONLY",
        "candidate_source_policy": "C2_LOGIN_PROCESS_SCOPED",
        "direct_egress_policy": "CANDIDATE_ONLY",
        "direct_dns_events_max": 0,
        "direct_other_tcp_flows_max": 0,
        "external_deny_role": EXTERNAL_DENY_ROLE,
    }
    for key, expected in constants.items():
        if data[key] != expected:
            raise LabValidationError(f"network policy {key} is invalid")
    return {
        "transport": "TCP",
        "allowed_remote_ports": ports,
        **constants,
    }


def _validate_durations(value: object, context: str) -> dict[str, int]:
    data = _require_mapping(value, context)
    bounds = {
        "baseline": (600, 3600),
        "negative_discovery": (120, 3600),
        "exact_discovery": (180, 3600),
        "login_timeout": (1, 120),
        "connected_steady": (600, 3600),
        "network_interruption": (30, 3600),
        "reconnect_observation": (300, 3600),
        "blocked_timeout": (1, 180),
        "c4_elapsed_tolerance": (0, 30),
        "c5_separation_minimum": (1800, 3600),
        "probe_timestamp_tolerance_seconds": (0, 5),
    }
    _require_exact_keys(data, set(bounds), context)
    result: dict[str, int] = {}
    for key in sorted(bounds):
        item = data[key]
        minimum, maximum = bounds[key]
        if (
            isinstance(item, bool)
            or not isinstance(item, int)
            or not minimum <= item <= maximum
        ):
            raise LabValidationError(
                f"{context}.{key} must be in {minimum}..{maximum}"
            )
        result[key] = item
    return result


def _lifecycle_control_contract(
    control: str, lifecycle: Mapping[str, object]
) -> dict[str, object]:
    if control not in CONTROLS:
        raise LabValidationError("control must be one of C0..C5")
    if control in {"C0", "C1", "C2"}:
        roles = {
            "C0": ("LAUNCH_RETAIN", "NOT_RUNNING", "RETAINED"),
            "C1": ("REUSE_RETAIN", "RUNNING", "RETAINED"),
            "C2": (
                "REUSE_CONFIG_SUBMIT_TEARDOWN",
                "RUNNING",
                "TERMINATED",
            ),
        }
        role, entry, exit_state = roles[control]
        return {
            "schema_version": 1,
            "lifecycle_mode": C012_LIFECYCLE_MODE,
            "c012_session_id": _validated_uuid(
                lifecycle["c012_session_id"],
                "lifecycle_control.c012_session_id",
            ),
            "session_role": role,
            "expected_entry_state": entry,
            "expected_exit_state": exit_state,
            "launch_control": "C0",
            "teardown_control": "C2",
            "root_process_generation_policy": C012_ROOT_GENERATION_POLICY,
            "allowed_transient_process_policy": C012_TRANSIENT_POLICY,
        }
    return {
        "schema_version": 1,
        "lifecycle_mode": DIRECT_LIFECYCLE_MODE,
        "c012_session_id": None,
        "session_role": "INDEPENDENT_LAUNCH_TEARDOWN",
        "expected_entry_state": "NOT_RUNNING",
        "expected_exit_state": "TERMINATED",
        "launch_control": control,
        "teardown_control": control,
        "root_process_generation_policy": "UNIQUE_PER_CONTROL",
        "allowed_transient_process_policy": "NONE",
    }


def _validate_lifecycle_control(
    value: object,
    control: str,
    lifecycle: Mapping[str, object] | None,
) -> dict[str, object]:
    data = _require_mapping(value, "control plan lifecycle_control")
    expected = _lifecycle_control_contract(
        control,
        (
            lifecycle
            if lifecycle is not None
            else {
                "c012_session_id": data.get("c012_session_id"),
            }
        ),
    )
    _require_exact_keys(data, set(expected), "control plan lifecycle_control")
    if canonical_json(data) != canonical_json(expected):
        raise LabValidationError(
            "control plan lifecycle differs from the committed policy"
        )
    return expected


def _validate_initial_pre_state_binding(
    value: object,
    control: str,
) -> dict[str, object]:
    data = _require_mapping(value, "initial_pre_state_binding")
    _require_exact_keys(
        data,
        {
            "schema_version",
            "scope",
            "portable_root_path_sha256",
            "initial_c012_pre_state_sha256",
        },
        "initial_pre_state_binding",
    )
    _require_exact_version(
        data["schema_version"], 1, "initial_pre_state_binding.schema_version"
    )
    expected_scope = (
        "C012_INITIAL"
        if control == "C0"
        else "C012_REFERENCE"
        if control in {"C1", "C2"}
        else "COLD_BOOT_INITIAL"
    )
    if data["scope"] != expected_scope:
        raise LabValidationError("initial_pre_state_binding scope is invalid")
    result = {
        "schema_version": 1,
        "scope": expected_scope,
        "portable_root_path_sha256": _validated_sha256(
            data["portable_root_path_sha256"],
            "initial_pre_state_binding.portable_root_path_sha256",
        ),
        "initial_c012_pre_state_sha256": _validated_nullable_sha256(
            data["initial_c012_pre_state_sha256"],
            "initial_pre_state_binding.initial_c012_pre_state_sha256",
        ),
    }
    if control in {"C0", "C1", "C2"}:
        if result["initial_c012_pre_state_sha256"] is None:
            raise LabValidationError(
                f"{control} requires initial C012 pre-state commitment"
            )
    elif result["initial_c012_pre_state_sha256"] is not None:
        raise LabValidationError(
            f"{control} cannot reuse the C012 initial pre-state commitment"
        )
    return result


def _validate_negative_query(value: object) -> dict[str, object]:
    data = _require_mapping(value, "negative_query")
    _require_exact_keys(
        data,
        {
            "schema_version",
            "label",
            "label_sha256",
            "expected_result_count",
        },
        "negative_query",
    )
    _require_exact_version(
        data["schema_version"], 1, "negative_query.schema_version"
    )
    experiment_match = re.fullmatch(
        r"TJ-NO-SUCH-([0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
        r"[89ab][0-9a-f]{3}-[0-9a-f]{12})",
        str(data["label"]),
    )
    if experiment_match is None:
        raise LabValidationError("negative_query.label is not canonical")
    expected = negative_query_contract(experiment_match.group(1))
    if canonical_json(data) != canonical_json(expected):
        raise LabValidationError("negative_query contract is not canonical")
    return expected


def _control_plan_actions(
    control: str,
    candidate: Mapping[str, object] | None,
    *,
    raw_path: str,
    sanitized_path: str,
    negative_query: Mapping[str, object] | None = None,
) -> list[dict[str, object]]:
    """Reconstruct the complete action policy for control-plan validation."""

    actions: list[dict[str, object]] = [
        {"action": "assert_disposable_vm", "required": True},
    ]
    if control in {"C0", "C3", "C4", "C5"}:
        actions.append(
            {"action": "assert_terminal_not_running", "required": True}
        )
    else:
        actions.append(
            {
                "action": "assert_existing_c012_root_process",
                "required": True,
            }
        )
        actions.append(
            {
                "action": "assert_same_c012_job_and_process_generation",
                "required": True,
            }
        )
    actions.extend(
        [
            {"action": "assert_terminal_hash_and_signature", "required": True},
            {"action": "start_etw_capture", "output_directory": raw_path},
        ]
    )
    if control == "C0":
        actions.extend(
            [
                {"action": "assert_clean_portable_state"},
                {
                    "action": "create_c012_job_and_root_process_generation",
                    "required": True,
                },
                {"action": "mark_phase", "marker": "C0_BASELINE_START"},
                {
                    "action": "start_terminal_without_identity",
                    "duration_key": "baseline",
                },
                {"action": "mark_phase", "marker": "C0_BASELINE_END"},
            ]
        )
    elif control == "C1":
        if negative_query is None:
            raise LabValidationError("C1 action policy requires negative query")
        actions.extend(
            [
                {"action": "assert_no_sensitive_config"},
                {
                    "action": "mark_phase",
                    "marker": "C1_DISCOVERY_NEGATIVE_START",
                },
                {
                    "action": "exact_search_negative_label",
                    "duration_key": "negative_discovery",
                    "negative_query_label_sha256": negative_query[
                        "label_sha256"
                    ],
                    "expected_result_count": 0,
                },
                {
                    "action": "mark_phase",
                    "marker": "C1_DISCOVERY_NEGATIVE_END",
                },
                {
                    "action": "mark_phase",
                    "marker": "C1_DISCOVERY_EXACT_START",
                },
                {
                    "action": "exact_search_expected_server",
                    "duration_key": "exact_discovery",
                },
                {
                    "action": "mark_phase",
                    "marker": "C1_DISCOVERY_EXACT_END",
                },
                {"action": "assert_no_identity_submission"},
            ]
        )
    elif control == "C2":
        actions.extend(
            [
                {"action": "assert_dedicated_demo_investor_source"},
                {
                    "action": "prepare_private_bootstrap_interactively",
                    "persist_secret": False,
                },
                {"action": "mark_phase", "marker": "C2_LOGIN_START"},
                {
                    "action": "submit_login_bootstrap_to_existing_terminal",
                    "transient_process_policy": C012_TRANSIENT_POLICY,
                },
                {
                    "action": "verify_transient_submitter_same_job",
                    "required": True,
                },
                {"action": "observe_login_window", "duration_key": "login_timeout"},
                {"action": "mark_phase", "marker": "C2_LOGIN_END"},
                {"action": "mark_phase", "marker": "C2_CONNECTED_START"},
                {
                    "action": "observe_connected_state",
                    "duration_key": "connected_steady",
                },
                {"action": "mark_phase", "marker": "C2_CONNECTED_END"},
                {
                    "action": "mark_phase",
                    "marker": "C2_NETWORK_INTERRUPTION_START",
                },
                {
                    "action": "interrupt_network",
                    "duration_key": "network_interruption",
                },
                {
                    "action": "mark_phase",
                    "marker": "C2_NETWORK_INTERRUPTION_END",
                },
                {"action": "mark_phase", "marker": "C2_RECONNECT_START"},
                {
                    "action": "observe_reconnect",
                    "duration_key": "reconnect_observation",
                },
                {"action": "mark_phase", "marker": "C2_RECONNECT_END"},
            ]
        )
    elif control in {"C3", "C5"}:
        if candidate is None:
            raise LabValidationError(
                f"{control} action policy requires a candidate endpoint"
            )
        if control == "C5":
            actions.append(
                {
                    "action": "assert_minimum_separation_from_c3",
                    "duration_key": "c5_separation_minimum",
                }
            )
        actions.extend(
            [
                {"action": "assert_clean_portable_state"},
                {
                    "action": "apply_candidate_only_egress_in_disposable_vm",
                    "candidate": dict(candidate),
                },
                {
                    "action": "prepare_private_bootstrap_interactively",
                    "persist_secret": False,
                },
                {
                    "action": "mark_phase",
                    "marker": f"{control}_DIRECT_LOGIN_START",
                },
                {
                    "action": "start_direct_without_gui",
                    "duration_key": "login_timeout",
                },
                {
                    "action": "mark_phase",
                    "marker": f"{control}_DIRECT_LOGIN_END",
                },
                {
                    "action": "mark_phase",
                    "marker": f"{control}_CONNECTED_STEADY_START",
                },
                {
                    "action": "observe_connected_state",
                    "duration_key": "connected_steady",
                },
                {
                    "action": "mark_phase",
                    "marker": f"{control}_CONNECTED_STEADY_END",
                },
            ]
        )
    elif control == "C4":
        if candidate is None:
            raise LabValidationError(
                "C4 action policy requires a candidate endpoint"
            )
        actions.extend(
            [
                {"action": "assert_clean_portable_state"},
                {
                    "action": "apply_default_deny_without_candidate_allow",
                    "candidate": dict(candidate),
                },
                {
                    "action": "prepare_private_bootstrap_interactively",
                    "persist_secret": False,
                },
                {
                    "action": "mark_phase",
                    "marker": "C4_ENDPOINT_BLOCKED_START",
                },
                {
                    "action": "start_direct_expect_block",
                    "duration_key": "blocked_timeout",
                },
                {
                    "action": "mark_phase",
                    "marker": "C4_ENDPOINT_BLOCKED_END",
                },
                {"action": "assert_no_fallback"},
            ]
        )
    else:
        raise LabValidationError("control must be one of C0..C5")
    actions.extend(
        [
            {"action": "stop_etw_capture"},
            {
                "action": "assert_capture_integrity",
                "events_lost": 0,
                "buffers_lost": 0,
            },
            {"action": "sanitize_evidence", "output_directory": sanitized_path},
        ]
    )
    if control in {"C0", "C1"}:
        actions.append({"action": "retain_c012_session"})
    else:
        actions.extend(
            [
                {"action": "close_job_object"},
                {"action": "destroy_disposable_clone_after_export"},
            ]
        )
    return actions


def _duration_contract(
    control: str,
    durations: Mapping[str, object],
) -> dict[str, int]:
    if control not in CONTROLS:
        raise LabValidationError("duration contract control is invalid")
    return _validate_durations(durations, "duration contract")


def _direct_control_descriptor(
    control: str,
    durations: Mapping[str, object],
) -> dict[str, object]:
    if control not in {"C3", "C4", "C5"}:
        raise LabValidationError("direct descriptor control is invalid")
    return {
        "control": control,
        "required_phase_codes": list(_required_phase_markers(control)),
        "duration_contract": _duration_contract(control, durations),
        "network_contract": {
            "candidate_only": True,
            "dns_events_max": 0,
            "other_tcp_flows_max": 0,
            "non_tcp_network_events_max": 0,
            "candidate_disposition": (
                "BLOCKED" if control == "C4" else "CONNECTED"
            ),
        },
    }


def _validate_direct_control_descriptor(
    value: object,
    control: str,
) -> dict[str, object]:
    data = _require_mapping(value, f"direct descriptor {control}")
    _require_exact_keys(
        data,
        {
            "control",
            "required_phase_codes",
            "duration_contract",
            "network_contract",
        },
        f"direct descriptor {control}",
    )
    if data["control"] != control:
        raise LabValidationError("direct descriptor control mismatch")
    if data["required_phase_codes"] != list(_required_phase_markers(control)):
        raise LabValidationError("direct descriptor phase sequence mismatch")
    durations = _validate_durations(
        data["duration_contract"],
        f"direct descriptor {control} duration_contract",
    )
    network = _require_mapping(
        data["network_contract"],
        f"direct descriptor {control} network_contract",
    )
    _require_exact_keys(
        network,
        {
            "candidate_only",
            "dns_events_max",
            "other_tcp_flows_max",
            "non_tcp_network_events_max",
            "candidate_disposition",
        },
        f"direct descriptor {control} network_contract",
    )
    expected_network = {
        "candidate_only": True,
        "dns_events_max": 0,
        "other_tcp_flows_max": 0,
        "non_tcp_network_events_max": 0,
        "candidate_disposition": "BLOCKED" if control == "C4" else "CONNECTED",
    }
    if dict(network) != expected_network:
        raise LabValidationError("direct descriptor network contract mismatch")
    return {
        "control": control,
        "required_phase_codes": list(_required_phase_markers(control)),
        "duration_contract": durations,
        "network_contract": expected_network,
    }


def _validated_control(value: object, field: str) -> str:
    if not isinstance(value, str) or value not in CONTROLS:
        raise LabValidationError(f"{field} must be one of C0..C5")
    return value


def _validated_uuid(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise LabValidationError(f"{field} must be a UUID string")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise LabValidationError(f"{field} must be a UUID string") from exc
    if str(parsed) != value:
        raise LabValidationError(f"{field} must use canonical lowercase UUID syntax")
    return str(parsed)


def _validated_text(value: object, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise LabValidationError(f"{field} is invalid")
    normalized = unicodedata.normalize("NFKC", value)
    if (
        normalized != value
        or any(
            unicodedata.category(character).startswith("C")
            or _is_invisible_codepoint(ord(character))
            for character in normalized
        )
        or normalized != normalized.strip()
        or not 1 <= len(normalized) <= maximum
        or _CLEAN_TEXT.fullmatch(normalized) is None
        or _contains_account_like_human_number(normalized, field)
        or any(pattern.search(normalized) for pattern in _SECRET_TEXT_PATTERNS)
        or any(separator in normalized for separator in ("/", "\\"))
    ):
        raise LabValidationError(f"{field} is invalid")
    return normalized


def _contains_account_like_human_number(value: str, field: str) -> bool:
    # Contiguous account-like decimal runs remain forbidden in every human
    # field.  ``Nd`` covers Unicode decimal digits after the required NFKC
    # normalization.
    contiguous = 0
    for character in value:
        if unicodedata.category(character) == "Nd":
            contiguous += 1
            if 6 <= contiguous <= 19:
                return True
        else:
            contiguous = 0
    if field not in _FORMATTED_ACCOUNT_NUMBER_FIELDS:
        return False

    # For broker/server/company labels, treat any Unicode separator,
    # punctuation, or symbol as formatting inside a number.  Letters and all
    # other categories terminate the group, so two numeric fragments separated
    # by a word are never joined.
    index = 0
    while index < len(value):
        if unicodedata.category(value[index]) != "Nd":
            index += 1
            continue
        digits = 0
        cursor = index
        while cursor < len(value):
            category = unicodedata.category(value[cursor])
            if category == "Nd":
                digits += 1
                cursor += 1
                continue
            if category[:1] in {"Z", "P", "S"}:
                cursor += 1
                continue
            break
        if 6 <= digits <= 19:
            return True
        index = max(cursor, index + 1)
    return False


def _is_invisible_codepoint(codepoint: int) -> bool:
    return any(
        lower <= codepoint <= upper
        for lower, upper in _INVISIBLE_CODEPOINT_RANGES
    )


def _validated_windows_absolute_path(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or any(character in value for character in "\r\n\x00")
    ):
        raise LabValidationError(f"{field} is invalid")
    path = PureWindowsPath(value)
    if (
        not path.is_absolute()
        or re.fullmatch(r"[A-Za-z]:", path.drive) is None
        or ":" in str(path)[2:]
    ):
        raise LabValidationError(f"{field} must be an absolute local Windows drive path")
    if any(part in {".", ".."} for part in path.parts):
        raise LabValidationError(f"{field} cannot contain relative segments")
    return str(path)


def _validated_lab_root(value: object) -> str:
    normalized = _validated_windows_absolute_path(value, "lab_root")
    path = PureWindowsPath(normalized)
    if len(path.parts) < 2:
        raise LabValidationError("lab_root cannot be a drive root")
    forbidden_top_level = {
        "windows",
        "program files",
        "program files (x86)",
        "programdata",
    }
    if path.parts[1].casefold() in forbidden_top_level:
        raise LabValidationError("lab_root must be a dedicated non-system directory")
    return normalized
