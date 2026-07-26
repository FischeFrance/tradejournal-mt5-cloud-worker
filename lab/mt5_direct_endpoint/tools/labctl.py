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
from lab_evidence_verifier import verify_captured_run

# account_onboarding/account_worker/credential_provider/mt5_dry_run/endpoint_registry use
# package-relative imports among themselves (`from .foo import ...`), so -- unlike lab_model/
# lab_evidence_verifier above -- they can only be imported as part of the `tools` package, not
# as bare top-level modules. `tools`' own parent directory must be on sys.path for that.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.account_worker import AccountWorker, WorkerError  # noqa: E402
from tools.credential_provider import CredentialLeakError, FakeCredentialProvider, json_dumps_safe  # noqa: E402
from tools.endpoint_registry import RegistryError, resolve_verified  # noqa: E402
from tools.mt5_dry_run import DryRunError, mt5_config_dry_run  # noqa: E402
from tools.worker_host import (  # noqa: E402
    DEFAULT_HARMLESS_SLEEPER_SECONDS,
    DEFAULT_MAX_RESTARTS,
    DEFAULT_POLL_INTERVAL_SECONDS,
    DEFAULT_STARTUP_TIMEOUT_SECONDS,
    read_persistent_status,
    request_stop_and_wait,
    run_worker_host,
    start_persistent_worker,
)
from tools.worker_supervisor import SupervisorError  # noqa: E402


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

    verifier_parser = subparsers.add_parser(
        "verify-captured-evidence",
        help="independently verify a materialized captured-evidence run",
    )
    verifier_parser.add_argument("--run-dir", required=True)
    verifier_parser.add_argument("--run-id", required=True)
    verifier_parser.add_argument("--output", default="-")

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

    # ---- account/worker lifecycle (offline, credential-free, HARD_DISABLED for real launch) ----

    create_account_parser = subparsers.add_parser(
        "create-account",
        help="create an isolated per-account worker directory and persistent state file",
    )
    create_account_parser.add_argument("--account-id", required=True)
    create_account_parser.add_argument("--worker-root", required=True)
    create_account_parser.add_argument("--registry", required=True)
    create_account_parser.add_argument("--broker-label", required=True)
    create_account_parser.add_argument("--output", default="-")

    start_worker_parser = subparsers.add_parser(
        "start-worker",
        help="spawn (or confirm already-running) the persistent worker-host process for an account",
    )
    start_worker_parser.add_argument("--account-id", required=True)
    start_worker_parser.add_argument("--worker-root", required=True)
    start_worker_parser.add_argument("--poll-interval-seconds", type=float, default=DEFAULT_POLL_INTERVAL_SECONDS)
    start_worker_parser.add_argument("--max-restarts", type=int, default=DEFAULT_MAX_RESTARTS)
    start_worker_parser.add_argument(
        "--harmless-sleeper-seconds", type=float, default=DEFAULT_HARMLESS_SLEEPER_SECONDS,
        help="lifetime of the harmless child process; lower this only to test crash/restart behavior",
    )
    start_worker_parser.add_argument("--startup-timeout-seconds", type=float, default=DEFAULT_STARTUP_TIMEOUT_SECONDS)
    start_worker_parser.add_argument("--output", default="-")

    stop_worker_parser = subparsers.add_parser(
        "stop-worker",
        help="ask the running worker-host process to stop in an ordered way and wait for it",
    )
    stop_worker_parser.add_argument("--account-id", required=True)
    stop_worker_parser.add_argument("--worker-root", required=True)
    stop_worker_parser.add_argument("--output", default="-")

    worker_status_parser = subparsers.add_parser(
        "worker-status",
        help="report a worker's real status: persisted state plus a live OS-level process check",
    )
    worker_status_parser.add_argument("--account-id", required=True)
    worker_status_parser.add_argument("--worker-root", required=True)
    worker_status_parser.add_argument("--output", default="-")

    worker_host_parser = subparsers.add_parser(
        "worker-host",
        help=(
            "run the persistent worker-host process in the foreground; "
            "harmless-only, never MT5 (spawned detached by start-worker; also directly runnable for testing)"
        ),
    )
    worker_host_parser.add_argument("--account-id", required=True)
    worker_host_parser.add_argument("--worker-root", required=True)
    worker_host_parser.add_argument("--poll-interval-seconds", type=float, default=DEFAULT_POLL_INTERVAL_SECONDS)
    worker_host_parser.add_argument("--max-restarts", type=int, default=DEFAULT_MAX_RESTARTS)
    worker_host_parser.add_argument("--harmless-sleeper-seconds", type=float, default=DEFAULT_HARMLESS_SLEEPER_SECONDS)

    verify_endpoint_parser = subparsers.add_parser(
        "verify-endpoint",
        help="resolve exactly one current VERIFIED broker endpoint, failing closed on ambiguity",
    )
    verify_endpoint_parser.add_argument("--registry", required=True)
    verify_endpoint_parser.add_argument("--broker-label", required=True)
    verify_endpoint_parser.add_argument("--now-unix-ms", type=int)
    verify_endpoint_parser.add_argument("--output", default="-")

    generate_config_dry_run_parser = subparsers.add_parser(
        "generate-config-dry-run",
        help="generate a credential-free MT5 config plan; never launches MT5",
    )
    generate_config_dry_run_parser.add_argument("--registry", required=True)
    generate_config_dry_run_parser.add_argument("--broker-label", required=True)
    generate_config_dry_run_parser.add_argument("--now-unix-ms", type=int)
    generate_config_dry_run_parser.add_argument("--output", default="-")

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
        if args.command == "verify-captured-evidence":
            evaluation = verify_captured_run(args.run_dir, args.run_id)
            _emit(evaluation.to_dict(), args.output)
            return {"PASS": 0, "FAIL": 1, "INCONCLUSIVE": 2, "SYNTHETIC_PASS": 3}[evaluation.outcome]
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
        if args.command == "create-account":
            worker = AccountWorker.create(
                args.worker_root, args.account_id,
                credential_provider=FakeCredentialProvider(),
                registry_path=args.registry, broker_label=args.broker_label,
            )
            payload = read_persistent_status(worker)
            json_dumps_safe(payload)
            _emit(payload, args.output)
            return 0
        if args.command == "start-worker":
            worker = AccountWorker.resume(
                args.worker_root, args.account_id, credential_provider=FakeCredentialProvider()
            )
            payload = start_persistent_worker(
                worker,
                poll_interval_seconds=args.poll_interval_seconds,
                max_restarts=args.max_restarts,
                harmless_sleeper_seconds=args.harmless_sleeper_seconds,
                startup_timeout_seconds=args.startup_timeout_seconds,
            )
            json_dumps_safe(payload)
            _emit(payload, args.output)
            return 0 if payload["process_alive"] else 64
        if args.command == "stop-worker":
            worker = AccountWorker.resume(
                args.worker_root, args.account_id, credential_provider=FakeCredentialProvider()
            )
            payload = request_stop_and_wait(worker)
            json_dumps_safe(payload)
            _emit(payload, args.output)
            return 0
        if args.command == "worker-status":
            worker = AccountWorker.resume(
                args.worker_root, args.account_id, credential_provider=FakeCredentialProvider()
            )
            payload = read_persistent_status(worker)
            json_dumps_safe(payload)
            _emit(payload, args.output)
            return 0
        if args.command == "worker-host":
            return run_worker_host(
                args.worker_root, args.account_id,
                poll_interval_seconds=args.poll_interval_seconds,
                max_restarts=args.max_restarts,
                harmless_sleeper_seconds=args.harmless_sleeper_seconds,
            )
        if args.command == "verify-endpoint":
            records = resolve_verified(args.registry, broker_label=args.broker_label, now_unix_ms=args.now_unix_ms)
            usable = len(records) == 1
            payload = {
                "broker_label": args.broker_label,
                "verified_count": len(records),
                "usable": usable,
                "endpoint": f"{records[0]['host']}:{records[0]['port']}" if usable else None,
            }
            _emit(payload, args.output)
            return 0 if usable else 2
        if args.command == "generate-config-dry-run":
            with mt5_config_dry_run(args.registry, args.broker_label, now_unix_ms=args.now_unix_ms) as plan:
                payload = {
                    "server": plan.server,
                    "config_path": str(plan.config_path),
                    "command": list(plan.command),
                    "launched": False,
                }
                json_dumps_safe(payload)
                _emit(payload, args.output)
            return 0
    except (
        OSError,
        json.JSONDecodeError,
        LabValidationError,
        WorkerError,
        RegistryError,
        DryRunError,
        CredentialLeakError,
        SupervisorError,
    ) as exc:
        sys.stderr.write(f"ERROR: {exc}\n")
        return 64
    return 64


if __name__ == "__main__":
    raise SystemExit(main())
