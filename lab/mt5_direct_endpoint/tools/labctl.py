from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any, Sequence

from lab_model import (
    EVIDENCE_SCHEMA_VERSION,
    LabValidationError,
    build_candidate_handoff,
    build_control_plan,
    build_direct_campaign_manifest,
    build_experiment_manifest,
    compose_identity,
    contract_digest,
    evidence_digest,
    evaluate_campaign,
    evaluate_evidence,
    validate_candidate_handoff,
    validate_config,
    validate_control_plan,
    validate_direct_campaign_manifest,
    validate_evidence,
    validate_experiment_manifest,
)


def _load_json(path: str) -> object:
    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise LabValidationError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle, object_pairs_hook=reject_duplicate_keys)


def _emit(payload: object, output: str) -> None:
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if output == "-":
        sys.stdout.write(rendered)
        return
    destination = Path(output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        f".{destination.name}.{uuid.uuid4().hex}.tmp"
    )
    descriptor: int | None = None
    try:
        descriptor = os.open(
            str(temporary),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            descriptor = None
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())

        # A same-filesystem hard-link is an atomic create-if-absent publish.
        # It fails instead of replacing a prior artifact or following a final
        # symlink. Unsupported filesystems also fail closed.
        os.link(temporary, destination)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Offline planner and strict schema-v6 evidence validator for the "
            "MT5 direct-endpoint lab."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_config_parser = subparsers.add_parser("validate-config")
    validate_config_parser.add_argument("--config", required=True)

    manifest_parser = subparsers.add_parser("build-manifest")
    manifest_parser.add_argument("--config", required=True)
    manifest_parser.add_argument("--output", default="-")

    validate_manifest_parser = subparsers.add_parser("validate-manifest")
    validate_manifest_parser.add_argument("--manifest", required=True)

    plan_parser = subparsers.add_parser("plan")
    plan_parser.add_argument("--config", required=True)
    plan_parser.add_argument("--control", required=True, choices=("C0", "C1", "C2", "C3", "C4", "C5"))
    plan_parser.add_argument("--candidate-endpoint")
    plan_parser.add_argument("--candidate-handoff")
    plan_parser.add_argument("--run-id")
    plan_parser.add_argument("--output", default="-")

    validate_plan_parser = subparsers.add_parser("validate-plan")
    validate_plan_parser.add_argument("--plan", required=True)
    validate_plan_parser.add_argument("--manifest")
    validate_plan_parser.add_argument("--candidate-handoff")

    direct_manifest_parser = subparsers.add_parser("build-direct-campaign")
    direct_manifest_parser.add_argument("--config", required=True)
    direct_manifest_parser.add_argument("--c2-evidence", required=True)
    direct_manifest_parser.add_argument("--c2-control-plan", required=True)
    direct_manifest_parser.add_argument("--output", default="-")

    validate_direct_manifest_parser = subparsers.add_parser(
        "validate-direct-campaign"
    )
    validate_direct_manifest_parser.add_argument(
        "--direct-campaign-manifest", required=True
    )
    validate_direct_manifest_parser.add_argument("--manifest")

    handoff_parser = subparsers.add_parser("build-candidate-handoff")
    handoff_parser.add_argument("--config", required=True)
    handoff_parser.add_argument("--c2-evidence", required=True)
    handoff_parser.add_argument("--c2-control-plan", required=True)
    handoff_parser.add_argument("--direct-campaign-manifest")
    handoff_parser.add_argument("--output", default="-")

    validate_handoff_parser = subparsers.add_parser("validate-candidate-handoff")
    validate_handoff_parser.add_argument("--candidate-handoff", required=True)
    validate_handoff_parser.add_argument("--manifest")
    validate_handoff_parser.add_argument("--direct-campaign-manifest")

    validate_evidence_parser = subparsers.add_parser("validate-evidence")
    validate_evidence_parser.add_argument("--evidence", required=True)

    evaluate_parser = subparsers.add_parser("evaluate")
    evaluate_parser.add_argument("--evidence", required=True)
    evaluate_parser.add_argument("--config", required=True)
    evaluate_parser.add_argument("--manifest")
    evaluate_parser.add_argument("--control-plan")
    evaluate_parser.add_argument("--candidate-handoff")
    evaluate_parser.add_argument("--output", default="-")

    fixture_parser = subparsers.add_parser(
        "evaluate-fixture",
        help="test-only synthetic branch evaluation; never returns attestation exit 0",
    )
    fixture_parser.add_argument("--evidence", required=True)
    fixture_parser.add_argument("--config", required=True)
    fixture_parser.add_argument("--manifest")
    fixture_parser.add_argument("--control-plan")
    fixture_parser.add_argument("--candidate-handoff")
    fixture_parser.add_argument("--output", default="-")

    campaign_parser = subparsers.add_parser("evaluate-campaign")
    campaign_parser.add_argument("--campaign", required=True)
    campaign_parser.add_argument("--config", required=True)
    campaign_parser.add_argument("--manifest")
    campaign_parser.add_argument("--control-plans")
    campaign_parser.add_argument("--candidate-handoff")
    campaign_parser.add_argument("--direct-campaign-manifest")
    campaign_parser.add_argument("--output", default="-")

    fixture_campaign_parser = subparsers.add_parser(
        "evaluate-fixture-campaign",
        help="test-only synthetic campaign evaluation; never returns attestation exit 0",
    )
    fixture_campaign_parser.add_argument("--campaign", required=True)
    fixture_campaign_parser.add_argument("--config", required=True)
    fixture_campaign_parser.add_argument("--manifest")
    fixture_campaign_parser.add_argument("--control-plans")
    fixture_campaign_parser.add_argument("--candidate-handoff")
    fixture_campaign_parser.add_argument("--direct-campaign-manifest")
    fixture_campaign_parser.add_argument("--output", default="-")

    identity_parser = subparsers.add_parser("compose-identity")
    identity_parser.add_argument("--probe", required=True)
    identity_parser.add_argument("--config", required=True)
    identity_parser.add_argument("--expected-run-id", required=True)
    identity_parser.add_argument("--investor-provenance-confirmed", action="store_true")
    identity_parser.add_argument("--probe-hash-verified", action="store_true")
    identity_parser.add_argument("--probe-static-guard-passed", action="store_true")
    identity_parser.add_argument("--control-plan")
    identity_parser.add_argument("--probe-output-sha256")
    identity_parser.add_argument("--output", default="-")

    digest_parser = subparsers.add_parser("digest")
    digest_parser.add_argument("--evidence", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "validate-config":
            validate_config(_load_json(args.config))
            sys.stdout.write("VALID\n")
            return 0
        if args.command == "build-manifest":
            _emit(build_experiment_manifest(_load_json(args.config)), args.output)
            return 0
        if args.command == "validate-manifest":
            validate_experiment_manifest(_load_json(args.manifest))
            sys.stdout.write("VALID\n")
            return 0
        if args.command == "plan":
            plan = build_control_plan(
                _load_json(args.config),
                args.control,
                (
                    None
                    if args.candidate_endpoint is None
                    else _load_json(args.candidate_endpoint)
                ),
                (
                    None
                    if args.candidate_handoff is None
                    else _load_json(args.candidate_handoff)
                ),
                run_id=args.run_id,
            )
            _emit(plan, args.output)
            return 0
        if args.command == "validate-plan":
            validate_control_plan(
                _load_json(args.plan),
                manifest_payload=(
                    None if args.manifest is None else _load_json(args.manifest)
                ),
                candidate_handoff=(
                    None
                    if args.candidate_handoff is None
                    else _load_json(args.candidate_handoff)
                ),
            )
            sys.stdout.write("VALID\n")
            return 0
        if args.command == "build-direct-campaign":
            direct = build_direct_campaign_manifest(
                _load_json(args.config),
                _load_json(args.c2_evidence),
                _load_json(args.c2_control_plan),
            )
            _emit(direct, args.output)
            return 0
        if args.command == "validate-direct-campaign":
            validate_direct_campaign_manifest(
                _load_json(args.direct_campaign_manifest),
                manifest_payload=(
                    None if args.manifest is None else _load_json(args.manifest)
                ),
            )
            sys.stdout.write("VALID\n")
            return 0
        if args.command == "build-candidate-handoff":
            handoff = build_candidate_handoff(
                _load_json(args.config),
                _load_json(args.c2_evidence),
                _load_json(args.c2_control_plan),
                direct_campaign_manifest=(
                    None
                    if args.direct_campaign_manifest is None
                    else _load_json(args.direct_campaign_manifest)
                ),
            )
            _emit(handoff, args.output)
            return 0
        if args.command == "validate-candidate-handoff":
            validate_candidate_handoff(
                _load_json(args.candidate_handoff),
                manifest_payload=(
                    None if args.manifest is None else _load_json(args.manifest)
                ),
                direct_campaign_manifest=(
                    None
                    if args.direct_campaign_manifest is None
                    else _load_json(args.direct_campaign_manifest)
                ),
            )
            sys.stdout.write("VALID\n")
            return 0
        if args.command == "validate-evidence":
            validate_evidence(_load_json(args.evidence))
            sys.stdout.write("VALID\n")
            return 0
        if args.command == "evaluate":
            evaluation = evaluate_evidence(
                _load_json(args.evidence),
                config_payload=_load_json(args.config),
                manifest_payload=(
                    None if args.manifest is None else _load_json(args.manifest)
                ),
                control_plan_payload=(
                    None
                    if args.control_plan is None
                    else _load_json(args.control_plan)
                ),
                candidate_handoff_payload=(
                    None
                    if args.candidate_handoff is None
                    else _load_json(args.candidate_handoff)
                ),
            )
            _emit(evaluation.to_dict(), args.output)
            return 0 if evaluation.outcome == "PASS" else 2
        if args.command == "evaluate-fixture":
            evaluation = evaluate_evidence(
                _load_json(args.evidence),
                config_payload=_load_json(args.config),
                manifest_payload=(
                    None if args.manifest is None else _load_json(args.manifest)
                ),
                control_plan_payload=(
                    None
                    if args.control_plan is None
                    else _load_json(args.control_plan)
                ),
                candidate_handoff_payload=(
                    None
                    if args.candidate_handoff is None
                    else _load_json(args.candidate_handoff)
                ),
                allow_synthetic=True,
            )
            _emit(evaluation.to_dict(), args.output)
            return 3 if evaluation.outcome == "SYNTHETIC_PASS" else 2
        if args.command == "evaluate-campaign":
            evaluation = evaluate_campaign(
                _load_json(args.campaign),
                config_payload=_load_json(args.config),
                manifest_payload=(
                    None if args.manifest is None else _load_json(args.manifest)
                ),
                control_plans_payload=(
                    None
                    if args.control_plans is None
                    else _load_json(args.control_plans)
                ),
                candidate_handoff_payload=(
                    None
                    if args.candidate_handoff is None
                    else _load_json(args.candidate_handoff)
                ),
                direct_campaign_manifest_payload=(
                    None
                    if args.direct_campaign_manifest is None
                    else _load_json(args.direct_campaign_manifest)
                ),
            )
            _emit(evaluation.to_dict(), args.output)
            return 0 if evaluation.outcome == "PASS" else 2
        if args.command == "evaluate-fixture-campaign":
            evaluation = evaluate_campaign(
                _load_json(args.campaign),
                config_payload=_load_json(args.config),
                manifest_payload=(
                    None if args.manifest is None else _load_json(args.manifest)
                ),
                control_plans_payload=(
                    None
                    if args.control_plans is None
                    else _load_json(args.control_plans)
                ),
                candidate_handoff_payload=(
                    None
                    if args.candidate_handoff is None
                    else _load_json(args.candidate_handoff)
                ),
                direct_campaign_manifest_payload=(
                    None
                    if args.direct_campaign_manifest is None
                    else _load_json(args.direct_campaign_manifest)
                ),
                allow_synthetic=True,
            )
            _emit(evaluation.to_dict(), args.output)
            return 3 if evaluation.outcome == "SYNTHETIC_PASS" else 2
        if args.command == "compose-identity":
            identity = compose_identity(
                _load_json(args.probe),
                _load_json(args.config),
                expected_run_id=args.expected_run_id,
                investor_provenance_confirmed=args.investor_provenance_confirmed,
                probe_hash_verified=args.probe_hash_verified,
                probe_static_guard_passed=args.probe_static_guard_passed,
                control_plan_payload=(
                    None
                    if args.control_plan is None
                    else _load_json(args.control_plan)
                ),
                probe_output_sha256=args.probe_output_sha256,
            )
            _emit(identity, args.output)
            return 0 if identity is not None else 2
        if args.command == "digest":
            evidence = validate_evidence(_load_json(args.evidence))
            sys.stdout.write(
                contract_digest(
                    "EVIDENCE",
                    EVIDENCE_SCHEMA_VERSION,
                    evidence,
                )
                + "\n"
            )
            return 0
    except (OSError, json.JSONDecodeError, LabValidationError) as exc:
        sys.stderr.write(f"ERROR: {exc}\n")
        return 64
    return 64


if __name__ == "__main__":
    raise SystemExit(main())
