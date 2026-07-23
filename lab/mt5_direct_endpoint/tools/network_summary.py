from __future__ import annotations

import hashlib
import ipaddress
import json
import uuid
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping

from lab_model import (
    LabValidationError,
    contract_digest,
    reject_sensitive_content,
    validate_candidate,
)


SUMMARY_SCHEMA_VERSION = 2
SANITIZED_EVENT_SCHEMA_VERSION = 2
PHASES = (
    "C0_BASELINE",
    "C1_DISCOVERY_NEGATIVE",
    "C1_DISCOVERY_EXACT",
    "C2_LOGIN",
    "C2_CONNECTED",
    "C2_NETWORK_INTERRUPTION",
    "C2_RECONNECT",
    "C3_DIRECT_LOGIN",
    "C3_CONNECTED_STEADY",
    "C4_ENDPOINT_BLOCKED",
    "C5_DIRECT_LOGIN",
    "C5_CONNECTED_STEADY",
    "TEARDOWN",
)
C3_DIRECT_PHASES = ("C3_DIRECT_LOGIN", "C3_CONNECTED_STEADY")
C5_DIRECT_PHASES = ("C5_DIRECT_LOGIN", "C5_CONNECTED_STEADY")
DIRECT_PHASES = (*C3_DIRECT_PHASES, "C4_ENDPOINT_BLOCKED", *C5_DIRECT_PHASES)
MAX_LINE_BYTES = 128 * 1024
MAX_EVENTS = 5_000_000
FLOW_DISPOSITIONS = ("connected", "blocked", "attempted_not_connected")

_EVENT_KEYS = {
    "schema_version",
    "run_id",
    "phase",
    "timestamp_utc",
    "category",
    "provider_name",
    "provider_guid",
    "event_id",
    "task",
    "opcode",
    "header_process_id",
    "header_thread_id",
    "payload_process_id",
    "parent_process_id",
    "process_guid",
    "parent_process_guid",
    "connection_id_sha256",
    "image_name",
    "image_path_sha256",
    "image_matches_terminal",
    "protocol",
    "local_address",
    "local_port",
    "remote_address",
    "remote_port",
    "dns_query_name",
    "dns_result_addresses",
    "status",
}


def summarize_events(
    events: Iterable[Mapping[str, Any]],
    *,
    run_id: str,
    candidate: object | None,
    source_sha256: str,
) -> dict[str, object]:
    _validate_run_id(run_id)
    if (
        not isinstance(source_sha256, str)
        or len(source_sha256) != 64
        or any(character not in "0123456789abcdefABCDEF" for character in source_sha256)
    ):
        raise LabValidationError("source_sha256 must contain 64 hexadecimal digits")
    validated_candidate = None if candidate is None else validate_candidate(candidate)
    endpoints: dict[str, set[str]] = defaultdict(set)
    network_counts: Counter[str] = Counter()
    non_tcp_network_counts: Counter[str] = Counter()
    dns_counts: Counter[str] = Counter()
    candidate_counts: Counter[str] = Counter()
    flow_records: dict[str, dict[tuple[object, ...], dict[str, object]]] = (
        defaultdict(dict)
    )
    connection_bindings: dict[str, tuple[object, ...]] = {}
    incoherent_connection_ids: set[str] = set()
    unphased_relevant = 0
    total = 0

    for event in events:
        total += 1
        if total > MAX_EVENTS:
            raise LabValidationError("sanitized event count exceeds the offline safety limit")
        clean = _validate_event(event, run_id)
        phase = clean["phase"] if clean["phase"] is not None else "UNPHASED"
        category = clean["category"]
        if category == "DNS":
            dns_counts[phase] += 1
            if phase == "UNPHASED":
                unphased_relevant += 1
        if category != "NETWORK":
            continue
        network_counts[phase] += 1
        if phase == "UNPHASED":
            unphased_relevant += 1
        if not _is_tcp(clean["protocol"]):
            non_tcp_network_counts[phase] += 1
            continue
        process_identity = _process_identity(clean)
        if process_identity is None:
            raise LabValidationError(
                "sanitized TCP event lacks process-scoped attribution"
            )
        remote_address = clean["remote_address"]
        remote_port = clean["remote_port"]
        endpoint = (
            _format_endpoint(remote_address, remote_port)
            if remote_address is not None and remote_port is not None
            else None
        )
        if endpoint is not None:
            endpoints[phase].add(endpoint)
        candidate_match = (
            endpoint is not None
            and validated_candidate is not None
            and endpoint == _candidate_text(validated_candidate)
        )
        if candidate_match:
            candidate_counts[phase] += 1
        connection_id_sha256 = clean["connection_id_sha256"]
        connection_binding = (
            run_id,
            phase,
            process_identity,
            clean["local_address"],
            clean["local_port"],
            remote_address,
            remote_port,
        )
        if isinstance(connection_id_sha256, str):
            previous_binding = connection_bindings.setdefault(
                connection_id_sha256, connection_binding
            )
            if previous_binding != connection_binding:
                incoherent_connection_ids.add(connection_id_sha256)
        flow_key = _flow_key(
            connection_id_sha256,
            total,
            incoherent=connection_id_sha256 in incoherent_connection_ids,
        )
        record = {
            "phase": phase,
            "process_identity": list(process_identity),
            "connection_id_sha256": connection_id_sha256,
            "protocol": "TCP",
            "local_address": clean["local_address"],
            "local_port": clean["local_port"],
            "remote_address": remote_address,
            "remote_port": remote_port,
            "classification": "candidate" if candidate_match else "other",
            "disposition": _flow_disposition(clean),
        }
        existing = flow_records[phase].get(flow_key)
        if existing is None:
            flow_records[phase][flow_key] = record
        else:
            if existing["classification"] != record["classification"]:
                assert isinstance(connection_id_sha256, str)
                incoherent_connection_ids.add(connection_id_sha256)
                flow_records[phase][("DIAGNOSTIC_RECORD", total)] = record
            else:
                try:
                    existing["disposition"] = _merge_flow_disposition(
                        str(existing["disposition"]),
                        str(record["disposition"]),
                    )
                except LabValidationError:
                    assert isinstance(connection_id_sha256, str)
                    incoherent_connection_ids.add(connection_id_sha256)
                    flow_records[phase][("DIAGNOSTIC_RECORD", total)] = record

    baseline = endpoints["C0_BASELINE"]
    negative = endpoints["C1_DISCOVERY_NEGATIVE"]
    exact = endpoints["C1_DISCOVERY_EXACT"]
    pre_login = baseline | negative | exact
    login = endpoints["C2_LOGIN"]
    direct_unexpected: dict[str, list[str]] = {}
    candidate_text = None
    if validated_candidate is not None:
        candidate_text = _candidate_text(validated_candidate)
        for phase in DIRECT_PHASES:
            direct_unexpected[phase] = sorted(
                endpoint for endpoint in endpoints[phase] if endpoint != candidate_text
            )
    else:
        for phase in DIRECT_PHASES:
            direct_unexpected[phase] = sorted(endpoints[phase])

    phase_accounting: dict[str, dict[str, object]] = {}
    all_flow_records: list[dict[str, object]] = []
    for records in flow_records.values():
        for record in records.values():
            connection_digest = record["connection_id_sha256"]
            record["connection_binding_verified"] = (
                isinstance(connection_digest, str)
                and connection_digest not in incoherent_connection_ids
            )
    for phase in (*PHASES, "UNPHASED"):
        records = list(flow_records[phase].values())
        all_flow_records.extend(records)
        candidate_records = [
            record for record in records if record["classification"] == "candidate"
        ]
        other_records = [
            record for record in records if record["classification"] == "other"
        ]
        candidate_dispositions = _disposition_counts(candidate_records)
        other_dispositions = _disposition_counts(other_records)
        process_scoped_tcp_flows = len(records)
        candidate_tcp_flows = len(candidate_records)
        other_tcp_flows = len(other_records)
        if process_scoped_tcp_flows != candidate_tcp_flows + other_tcp_flows:
            raise LabValidationError("exclusive TCP flow accounting invariant failed")
        if candidate_tcp_flows != sum(candidate_dispositions.values()):
            raise LabValidationError("candidate TCP disposition accounting failed")
        if other_tcp_flows != sum(other_dispositions.values()):
            raise LabValidationError("other TCP disposition accounting failed")
        phase_accounting[phase] = {
            "process_scoped_tcp_flows": process_scoped_tcp_flows,
            "candidate_tcp_flows": candidate_tcp_flows,
            "other_tcp_flows": other_tcp_flows,
            "non_tcp_network_events": non_tcp_network_counts[phase],
            "dns_events": dns_counts[phase],
            "candidate_dispositions": candidate_dispositions,
            "other_dispositions": other_dispositions,
            "flow_records_verified": all(
                record["connection_binding_verified"] is True for record in records
            ),
        }

    exact_delta_endpoints = exact - negative - baseline
    exact_delta_records = [
        record
        for record in flow_records["C1_DISCOVERY_EXACT"].values()
        if _record_endpoint(record) in exact_delta_endpoints
    ]
    discovery_delta_source = (
        {
            "kind": "PROCESS_SCOPED_TCP_FLOW_SET",
            "sha256": _digest_flow_records(
                exact_delta_records,
                run_id=run_id,
                set_kind="C1_DISCOVERY_EXACT_DELTA",
            ),
            "verified": all(
                record["connection_binding_verified"] is True
                for record in exact_delta_records
            ),
        }
        if exact_delta_records
        else {"kind": "NONE", "sha256": None, "verified": False}
    )

    summary = {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "run_id": run_id,
        "source_events_sha256": source_sha256.casefold(),
        "source_event_count": total,
        "candidate_endpoint": candidate_text,
        "flow_record_set_sha256": _digest_flow_records(
            all_flow_records,
            run_id=run_id,
            set_kind="ALL_PHASES",
        ),
        "flow_record_set_verified": all(
            record["connection_binding_verified"] is True
            for record in all_flow_records
        ),
        "flow_accounting_by_phase": phase_accounting,
        "discovery_delta_source": discovery_delta_source,
        "phase_endpoint_sets": {
            phase: sorted(endpoints[phase]) for phase in PHASES
        },
        "network_events_by_phase": {
            phase: network_counts[phase] for phase in (*PHASES, "UNPHASED")
        },
        "dns_events_by_phase": {
            phase: dns_counts[phase] for phase in (*PHASES, "UNPHASED")
        },
        "candidate_events_by_phase": {
            phase: candidate_counts[phase] for phase in PHASES
        },
        "non_tcp_network_events_by_phase": {
            phase: non_tcp_network_counts[phase]
            for phase in (*PHASES, "UNPHASED")
        },
        "deltas": {
            "discovery_common": sorted(negative & exact),
            "discovery_exact_only": sorted(exact_delta_endpoints),
            "login_only": sorted(login - pre_login),
            "connected_only": sorted(endpoints["C2_CONNECTED"] - pre_login - login),
            "reconnect_only": sorted(endpoints["C2_RECONNECT"] - pre_login - login),
        },
        "unexpected_direct_endpoints": direct_unexpected,
        "unphased_relevant_events": unphased_relevant,
        "interpretation": {
            "fallback_present": any(direct_unexpected.values()),
            "direct_dns_present": any(dns_counts[phase] > 0 for phase in DIRECT_PHASES),
            "direct_non_tcp_network_present": any(
                non_tcp_network_counts[phase] > 0 for phase in DIRECT_PHASES
            ),
            "candidate_seen_in_c3": any(
                candidate_counts[phase] > 0 for phase in C3_DIRECT_PHASES
            ),
            "candidate_seen_in_c4": candidate_counts["C4_ENDPOINT_BLOCKED"] > 0,
            "candidate_seen_in_c5": any(
                candidate_counts[phase] > 0 for phase in C5_DIRECT_PHASES
            ),
            "discovery_delta_has_process_scoped_source": (
                discovery_delta_source["kind"]
                == "PROCESS_SCOPED_TCP_FLOW_SET"
                and discovery_delta_source["verified"] is True
            ),
            "exclusive_tcp_accounting_verified": True,
            "connection_success_proven": False,
            "candidate_block_proven": False,
            "requires_wfp_and_identity_corroboration": True,
        },
    }
    reject_sensitive_content(summary)
    return summary


def load_and_summarize(
    path: Path, *, run_id: str, candidate: object | None
) -> dict[str, object]:
    digest = hashlib.sha256()
    with path.open("rb") as hash_handle:
        while True:
            chunk = hash_handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)

    def events() -> Iterable[Mapping[str, Any]]:
        with path.open("rb") as handle:
            for line_number, raw_line in enumerate(handle, start=1):
                if len(raw_line) > MAX_LINE_BYTES:
                    raise LabValidationError(
                        f"sanitized event line {line_number} exceeds {MAX_LINE_BYTES} bytes"
                    )
                if not raw_line.strip():
                    continue
                try:
                    decoded = json.loads(raw_line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise LabValidationError(
                        f"invalid sanitized JSONL at line {line_number}"
                    ) from exc
                if not isinstance(decoded, Mapping):
                    raise LabValidationError(
                        f"sanitized event line {line_number} is not an object"
                    )
                yield decoded

    return summarize_events(
        events(),
        run_id=run_id,
        candidate=candidate,
        source_sha256=digest.hexdigest(),
    )


def _validate_event(event: Mapping[str, Any], run_id: str) -> dict[str, Any]:
    reject_sensitive_content(event)
    if set(event) != _EVENT_KEYS:
        missing = sorted(_EVENT_KEYS - set(event))
        extra = sorted(set(event) - _EVENT_KEYS)
        raise LabValidationError(
            f"sanitized event fields mismatch (missing={missing}, extra={extra})"
        )
    event_schema_version = event["schema_version"]
    if (
        isinstance(event_schema_version, bool)
        or not isinstance(event_schema_version, int)
        or event_schema_version != SANITIZED_EVENT_SCHEMA_VERSION
        or event["run_id"] != run_id
    ):
        raise LabValidationError("sanitized event schema/run binding mismatch")
    phase = event["phase"]
    if phase is not None and phase not in PHASES:
        raise LabValidationError("sanitized event has an unknown phase")
    category = event["category"]
    if category not in {"DNS", "NETWORK", "PROCESS_OR_IMAGE", "MARKER", "OTHER_CAPTURED"}:
        raise LabValidationError("sanitized event has an unknown category")

    image_matches_terminal = event["image_matches_terminal"]
    if not isinstance(image_matches_terminal, bool):
        raise LabValidationError("image_matches_terminal must be boolean")
    payload_process_id = _nullable_nonnegative_integer(
        event["payload_process_id"], "payload_process_id"
    )
    header_process_id = _nullable_nonnegative_integer(
        event["header_process_id"], "header_process_id"
    )
    local_address = _nullable_ip_address(event["local_address"], "local_address")
    remote_address = _nullable_ip_address(event["remote_address"], "remote_address")
    local_port = _nullable_port(event["local_port"], "local_port")
    remote_port = _nullable_port(event["remote_port"], "remote_port")
    image_path_sha256 = event["image_path_sha256"]
    if image_path_sha256 is not None and (
        not isinstance(image_path_sha256, str)
        or len(image_path_sha256) != 64
        or any(
            character not in "0123456789abcdefABCDEF"
            for character in image_path_sha256
        )
    ):
        raise LabValidationError("image_path_sha256 is invalid")
    connection_id_sha256 = event["connection_id_sha256"]
    if connection_id_sha256 is not None and (
        not isinstance(connection_id_sha256, str)
        or len(connection_id_sha256) != 64
        or any(
            character not in "0123456789abcdefABCDEF"
            for character in connection_id_sha256
        )
    ):
        raise LabValidationError("connection_id_sha256 is invalid")
    protocol = event["protocol"]
    if protocol is not None and (
        isinstance(protocol, bool) or not isinstance(protocol, (str, int))
    ):
        raise LabValidationError("protocol must be a string, integer, or null")
    status = event["status"]
    if status is not None and (
        isinstance(status, bool) or not isinstance(status, (str, int))
    ):
        raise LabValidationError("status must be a string, integer, or null")
    return {
        **event,
        "header_process_id": header_process_id,
        "payload_process_id": payload_process_id,
        "image_path_sha256": (
            image_path_sha256.casefold()
            if isinstance(image_path_sha256, str)
            else None
        ),
        "connection_id_sha256": (
            connection_id_sha256.casefold()
            if isinstance(connection_id_sha256, str)
            else None
        ),
        "local_address": local_address,
        "local_port": local_port,
        "remote_address": remote_address,
        "remote_port": remote_port,
    }


def _nullable_nonnegative_integer(value: object, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise LabValidationError(f"{name} must be a non-negative integer or null")
    return value


def _nullable_ip_address(value: object, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise LabValidationError(f"{name} must be a literal string or null")
    try:
        return ipaddress.ip_address(value).compressed
    except ValueError as exc:
        raise LabValidationError(f"{name} is not a literal address") from exc


def _nullable_port(value: object, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise LabValidationError(f"{name} is invalid")
    return value


def _is_tcp(protocol: object) -> bool:
    if isinstance(protocol, int) and not isinstance(protocol, bool):
        return protocol == 6
    if isinstance(protocol, str):
        return protocol.strip().casefold() in {"6", "tcp"}
    return False


def _process_identity(event: Mapping[str, object]) -> tuple[object, ...] | None:
    payload_process_id = event["payload_process_id"]
    if isinstance(payload_process_id, int) and payload_process_id > 0:
        return ("PAYLOAD_PID", payload_process_id)
    if event["image_matches_terminal"] is True and isinstance(
        event["image_path_sha256"], str
    ):
        return ("TERMINAL_IMAGE_SHA256", event["image_path_sha256"])
    return None


def _flow_key(
    connection_id_sha256: object,
    ordinal: int,
    *,
    incoherent: bool,
) -> tuple[object, ...]:
    if isinstance(connection_id_sha256, str) and not incoherent:
        return ("CONNECTION_ID_SHA256", connection_id_sha256)
    # Without a stable verifier-issued connection identifier, each event is
    # only a diagnostic record and must not be deduplicated into a proved flow.
    return ("DIAGNOSTIC_RECORD", ordinal)


def _flow_disposition(event: Mapping[str, object]) -> str:
    raw_status = event["status"]
    if raw_status is None:
        return "attempted_not_connected"
    normalized = str(raw_status).strip().casefold().replace("-", "_").replace(" ", "_")
    if normalized in {
        "blocked",
        "block",
        "denied",
        "deny",
        "access_denied",
        "blocked_by_policy",
        "0xc0000022",
    }:
        return "blocked"
    if normalized in {
        "0",
        "0x0",
        "success",
        "succeeded",
        "connected",
        "established",
    }:
        return "connected"
    return "attempted_not_connected"


def _merge_flow_disposition(current: str, observed: str) -> str:
    if current == observed:
        return current
    definitive = {current, observed} - {"attempted_not_connected"}
    if definitive == {"connected", "blocked"}:
        raise LabValidationError(
            "one TCP flow has contradictory connected and blocked records"
        )
    if len(definitive) == 1:
        return definitive.pop()
    return "attempted_not_connected"


def _disposition_counts(
    records: Iterable[Mapping[str, object]],
) -> dict[str, int]:
    counts: Counter[str] = Counter(str(record["disposition"]) for record in records)
    return {disposition: counts[disposition] for disposition in FLOW_DISPOSITIONS}


def _record_endpoint(record: Mapping[str, object]) -> str | None:
    address = record["remote_address"]
    port = record["remote_port"]
    if not isinstance(address, str) or not isinstance(port, int):
        return None
    return _format_endpoint(address, port)


def _digest_flow_records(
    records: Iterable[Mapping[str, object]],
    *,
    run_id: str,
    set_kind: str,
) -> str:
    canonical_records = sorted(
        (dict(record) for record in records),
        key=lambda record: json.dumps(
            record,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
    )
    return contract_digest(
        "NETWORK_FLOW_RECORD_SET",
        SUMMARY_SCHEMA_VERSION,
        {
            "run_id": run_id,
            "set_kind": set_kind,
            "records": canonical_records,
        },
    )


def _format_endpoint(address: str, port: int) -> str:
    parsed = ipaddress.ip_address(address)
    return f"[{parsed.compressed}]:{port}" if parsed.version == 6 else f"{parsed.compressed}:{port}"


def _candidate_text(candidate: Mapping[str, object]) -> str:
    return _format_endpoint(str(candidate["ip"]), int(candidate["port"]))


def _validate_run_id(value: str) -> None:
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise LabValidationError("run_id must be a canonical UUID") from exc
    if str(parsed) != value.casefold():
        raise LabValidationError("run_id must be a canonical lowercase UUID")
