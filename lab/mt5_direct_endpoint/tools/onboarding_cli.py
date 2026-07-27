"""Credential-free account, endpoint and broker-suggestion CLI commands."""
from __future__ import annotations

import argparse
from typing import Any, Callable

from .account_worker import AccountWorker, WorkerError
from .broker_identity_resolver import (
    BrokerIdentityError,
    OpenAIServerBrokerProvider,
    resolve_broker_for_wizard,
)
from .credential_provider import (
    CredentialLeakError,
    FakeCredentialProvider,
    json_dumps_safe,
)
from .endpoint_registry import RegistryError, resolve_verified
from .mt5_dry_run import DryRunError, mt5_config_dry_run
from .worker_host import (
    DEFAULT_HARMLESS_SLEEPER_SECONDS,
    DEFAULT_MAX_RESTARTS,
    DEFAULT_POLL_INTERVAL_SECONDS,
    DEFAULT_STARTUP_TIMEOUT_SECONDS,
    read_persistent_status,
    request_stop_and_wait,
    run_worker_host,
    start_persistent_worker,
)
from .worker_supervisor import SupervisorError


class OnboardingCliError(ValueError):
    """Sanitized error boundary returned to the top-level CLI."""


_DOMAIN_ERRORS = (
    WorkerError,
    RegistryError,
    DryRunError,
    CredentialLeakError,
    SupervisorError,
    BrokerIdentityError,
)


def register_onboarding_commands(subparsers: Any) -> None:
    create_account = subparsers.add_parser(
        "create-account",
        help="create an isolated per-account worker directory and state file",
    )
    create_account.add_argument("--account-id", required=True)
    create_account.add_argument("--worker-root", required=True)
    create_account.add_argument("--registry", required=True)
    create_account.add_argument("--broker-label", required=True)
    create_account.add_argument("--output", default="-")

    start_worker = subparsers.add_parser(
        "start-worker",
        help="spawn or confirm the credential-free persistent worker-host",
    )
    start_worker.add_argument("--account-id", required=True)
    start_worker.add_argument("--worker-root", required=True)
    start_worker.add_argument(
        "--poll-interval-seconds",
        type=float,
        default=DEFAULT_POLL_INTERVAL_SECONDS,
    )
    start_worker.add_argument("--max-restarts", type=int, default=DEFAULT_MAX_RESTARTS)
    start_worker.add_argument(
        "--harmless-sleeper-seconds",
        type=float,
        default=DEFAULT_HARMLESS_SLEEPER_SECONDS,
    )
    start_worker.add_argument(
        "--startup-timeout-seconds",
        type=float,
        default=DEFAULT_STARTUP_TIMEOUT_SECONDS,
    )
    start_worker.add_argument("--output", default="-")

    stop_worker = subparsers.add_parser(
        "stop-worker",
        help="request ordered worker-host shutdown and wait for completion",
    )
    stop_worker.add_argument("--account-id", required=True)
    stop_worker.add_argument("--worker-root", required=True)
    stop_worker.add_argument("--output", default="-")

    worker_status = subparsers.add_parser(
        "worker-status",
        help="report persisted state plus a live OS-level process check",
    )
    worker_status.add_argument("--account-id", required=True)
    worker_status.add_argument("--worker-root", required=True)
    worker_status.add_argument("--output", default="-")

    worker_host = subparsers.add_parser(
        "worker-host",
        help="run the harmless-only persistent worker-host in the foreground",
    )
    worker_host.add_argument("--account-id", required=True)
    worker_host.add_argument("--worker-root", required=True)
    worker_host.add_argument(
        "--poll-interval-seconds",
        type=float,
        default=DEFAULT_POLL_INTERVAL_SECONDS,
    )
    worker_host.add_argument("--max-restarts", type=int, default=DEFAULT_MAX_RESTARTS)
    worker_host.add_argument(
        "--harmless-sleeper-seconds",
        type=float,
        default=DEFAULT_HARMLESS_SLEEPER_SECONDS,
    )

    verify_endpoint = subparsers.add_parser(
        "verify-endpoint",
        help="resolve exactly one current VERIFIED endpoint",
    )
    verify_endpoint.add_argument("--registry", required=True)
    verify_endpoint.add_argument("--broker-label", required=True)
    verify_endpoint.add_argument("--now-unix-ms", type=int)
    verify_endpoint.add_argument("--output", default="-")

    config_dry_run = subparsers.add_parser(
        "generate-config-dry-run",
        help="generate a credential-free MT5 config plan; never launch MT5",
    )
    config_dry_run.add_argument("--registry", required=True)
    config_dry_run.add_argument("--broker-label", required=True)
    config_dry_run.add_argument("--now-unix-ms", type=int)
    config_dry_run.add_argument("--output", default="-")

    broker_identity = subparsers.add_parser(
        "resolve-broker-for-wizard",
        help="resolve a server to a suggestion-only broker search",
    )
    broker_identity.add_argument("--server", required=True)
    broker_identity.add_argument("--model", default="gpt-5.6")
    broker_identity.add_argument("--output", default="-")


def dispatch_onboarding_command(
    args: argparse.Namespace,
    emit: Callable[[object, str], None],
) -> int | None:
    try:
        if args.command == "create-account":
            worker = AccountWorker.create(
                args.worker_root,
                args.account_id,
                credential_provider=FakeCredentialProvider(),
                registry_path=args.registry,
                broker_label=args.broker_label,
            )
            payload = read_persistent_status(worker)
            json_dumps_safe(payload)
            emit(payload, args.output)
            return 0
        if args.command == "start-worker":
            worker = AccountWorker.resume(
                args.worker_root,
                args.account_id,
                credential_provider=FakeCredentialProvider(),
            )
            payload = start_persistent_worker(
                worker,
                poll_interval_seconds=args.poll_interval_seconds,
                max_restarts=args.max_restarts,
                harmless_sleeper_seconds=args.harmless_sleeper_seconds,
                startup_timeout_seconds=args.startup_timeout_seconds,
            )
            json_dumps_safe(payload)
            emit(payload, args.output)
            return 0 if payload["process_alive"] else 64
        if args.command == "stop-worker":
            worker = AccountWorker.resume(
                args.worker_root,
                args.account_id,
                credential_provider=FakeCredentialProvider(),
            )
            payload = request_stop_and_wait(worker)
            json_dumps_safe(payload)
            emit(payload, args.output)
            return 0
        if args.command == "worker-status":
            worker = AccountWorker.resume(
                args.worker_root,
                args.account_id,
                credential_provider=FakeCredentialProvider(),
            )
            payload = read_persistent_status(worker)
            json_dumps_safe(payload)
            emit(payload, args.output)
            return 0
        if args.command == "worker-host":
            return run_worker_host(
                args.worker_root,
                args.account_id,
                poll_interval_seconds=args.poll_interval_seconds,
                max_restarts=args.max_restarts,
                harmless_sleeper_seconds=args.harmless_sleeper_seconds,
            )
        if args.command == "verify-endpoint":
            records = resolve_verified(
                args.registry,
                broker_label=args.broker_label,
                now_unix_ms=args.now_unix_ms,
            )
            usable = len(records) == 1
            payload = {
                "broker_label": args.broker_label,
                "verified_count": len(records),
                "usable": usable,
                "endpoint": (
                    f"{records[0]['host']}:{records[0]['port']}"
                    if usable
                    else None
                ),
            }
            emit(payload, args.output)
            return 0 if usable else 2
        if args.command == "generate-config-dry-run":
            with mt5_config_dry_run(
                args.registry,
                args.broker_label,
                now_unix_ms=args.now_unix_ms,
            ) as plan:
                payload = {
                    "server": plan.server,
                    "config_path": str(plan.config_path),
                    "command": list(plan.command),
                    "launched": False,
                }
                json_dumps_safe(payload)
                emit(payload, args.output)
            return 0
        if args.command == "resolve-broker-for-wizard":
            payload = resolve_broker_for_wizard(
                args.server,
                OpenAIServerBrokerProvider(model=args.model),
            )
            emit(payload, args.output)
            return 0 if payload["outcome"] == "SUGGESTION" else 2
    except _DOMAIN_ERRORS as exc:
        raise OnboardingCliError(str(exc)) from exc
    return None
