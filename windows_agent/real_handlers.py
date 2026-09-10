from __future__ import annotations

import gc
import hashlib
import logging
import re
import time
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional
from uuid import uuid4

from worker.event_normalizer import normalize_event
from worker.event_outbox import EventOutbox

from .agent_errors import (
    AccountIdentityMismatch,
    AgentError,
    BrokerDiscoveryFailed,
    BrokerEndpointUnavailable,
    BrokerIdentityUnavailable,
    CredentialDecryptionFailed,
    CredentialEnvelopeInvalid,
    DeprovisionFailed,
    HistorySyncFailed,
    InstanceCleanupFailed,
    InstanceProvisionFailed,
    InvestorVerificationTimeout,
    InvestorAccessNotVerified,
    LiveSyncFailed,
    Mt5AuthorizationFailed,
    Mt5InitializeFailed,
    SecretStoreFailed,
    ServerIdentityMismatch,
    TerminalStartFailed,
)
from .agent_secrets import AGENT_SCOPE_ID, PROVISIONING_KEY_SECRET_NAME
from .broker_endpoint_resolver import (
    BrokerEndpointResolutionError,
    VerifiedBrokerEndpoint,
)
from .broker_endpoint_registry import (
    BrokerEndpointObservationError,
    BrokerEndpointPublicationError,
    EndpointInvalidation,
    EndpointPromotion,
    ObservedProcessEndpoint,
)
from .broker_identity import (
    BrokerIdentityError,
    BrokerIdentitySuggestion,
)
from .broker_wizard import (
    BrokerWizardError,
    BrokerWizardEvidence,
    LoginVerificationEvidence,
    write_login_verification_artifact,
)
from .mtapi_search import (
    MtApiEndpointCandidate,
    MtApiSearchError,
    MtApiSearchResult,
)
from .mt5_lifecycle import Mt5LifecycleCoordinator
from .mt5_recovery_window import new_only_recovery_from
from .credential_envelope import decrypt_credential_envelope
from .job_runner import LeaseLost
from .interactive_identity import InteractiveIdentityError
from .provisioning.instance_layout import InstanceLayout
from .provisioning.mt5_instance import InstanceProvisioner
from .provisioning.mt5_instance_pool import Mt5InstancePool
from .provisioning.mt5_template import Mt5TemplateManager
from .provisioning.mt5_update_store import Mt5PendingUpdateStore
from .provisioning.process_manager import ProcessManager
from .provisioning.secret_store import WindowsSecretStore
from .security import canonical_uuid
from .state_store import atomic_json, read_json
from .worker.adapter_errors import (
    IdentityMismatch,
    Mt5Error,
    Mt5IpcError,
    Mt5ProcessCrashed,
)
from .worker.dedup import PersistentDedup
from .worker.history_sync import HistoryMode, HistorySync
from .worker.historical_trade_import import (
    build_historical_pending_order_events,
    build_historical_trade_events,
)
from .worker.history_file_import import build_history_archive, deliver_history_archive
from .worker.live_sync import LiveSync
from .worker.local_event_sink import LocalEventSink
from .worker.mql5_file_adapter import Mql5FileMt5Adapter
from .worker.native_mt5_runtime import NativeMt5Error, NativeMt5Runtime
from .worker.trading_ingestion_sink import TradingIngestionSink


_MAX_MTAPI_LOGIN_ENDPOINTS = 2

logger = logging.getLogger(__name__)

SERVER_PATTERN = re.compile(r"[A-Za-z0-9._ -]{1,128}")
BROKER_PATTERN = re.compile(r"[A-Za-z0-9 .,&'()+_/-]{1,128}")
DEFAULT_EXPERT_BINARY = Path(r"C:\TradeJournal\mt5-template\MQL5\Experts\TradeJournal\TradeJournalBridge.ex5")
FAILED_PROVISION_LOCAL_STATUSES = frozenset(
    {
        "censusing_broker",
        "broker_censused",
        "broker_discovery_failed",
        "candidate_authentication_rejected",
        "provisioned",
        "starting_native_file_bridge",
        "authenticating",
        "terminal_started",
        "bridge_ready",
        "verifying_read_only",
        "read_only_verified",
        "importing_history",
        "history_ready",
        "starting_live_sync",
    }
)

JobHandler = Callable[[dict], dict]
BrokerWizard = Callable[
    [Path, str, str, str, Optional[Callable[[], None]]],
    BrokerWizardEvidence,
]
BrokerEndpointObserver = Callable[..., ObservedProcessEndpoint]
BrokerEndpointPublisher = Callable[
    [EndpointPromotion],
    VerifiedBrokerEndpoint,
]
BrokerEndpointInvalidator = Callable[[EndpointInvalidation], None]
MtApiSearch = Callable[[str], MtApiSearchResult]


class PersistentSnapshot:
    """Disk-backed snapshot for resumed live-sync checks."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def get(self) -> dict:
        return read_json(self.path, {"positions": {}, "orders": {}, "deals": {}})

    def save(self, value: dict) -> None:
        atomic_json(self.path, value)


def _progress(root: Path, **fields: Any) -> None:
    """Local-only progress tracking; these states are never sent to the control plane."""
    path = root / "state" / "job_progress.json"
    current = read_json(path)
    current.update(fields)
    atomic_json(path, current)


def _record_checkpoint(
    api: Any,
    job: dict,
    event_code: str,
    event_status: str,
    detail_code: str | None = None,
    *,
    root: Path | None = None,
    local_status: str | None = None,
) -> None:
    """Persist one sanitized milestone both remotely and, when available, locally.

    Progress is part of the job's observable contract. If acknowledgement is uncertain, the
    handler must not keep producing side effects under a timeline that falsely appears stalled.
    The API itself verifies the active agent lease and makes duplicate delivery idempotent.
    """

    try:
        response = api.progress(
            job["job_id"],
            job["lease_id"],
            event_code,
            event_status,
            detail_code,
        )
    except Exception as exc:
        raise LeaseLost("progress acknowledgement is uncertain") from exc
    if not isinstance(response, dict) or response.get("event_recorded") is not True:
        raise LeaseLost("progress lease is lost or acknowledgement is invalid")
    if root is not None:
        _progress(
            root,
            status=local_status or event_code,
            event_status=event_status,
            connection_id=str(job["connection_id"]),
        )


def _require_lease(api: Any, job: dict) -> None:
    guard = job.get("_lease_guard")
    if callable(guard):
        guard()
    heartbeat = api.heartbeat(job["job_id"], job["lease_id"])
    if not heartbeat.get("lease_valid", False):
        raise LeaseLost("lease lost")


def _verify_binary_pin(path: Path, expected_sha256: str | None) -> None:
    if expected_sha256 is None:
        return
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise TerminalStartFailed("pinned binary unavailable") from exc
    if digest.hexdigest() != expected_sha256:
        raise TerminalStartFailed("pinned binary digest mismatch")


def _recorded_instance_terminal_sha256(root: Path) -> str:
    """Return the immutable terminal pin published with one instance.

    A template rotation applies to *new* instances only.  An already published
    instance must continue to be checked against the digest recorded in its own
    state file; comparing it to the current template would incorrectly mark a
    healthy, older live session as corrupted.
    """
    state = read_json(root / "state" / "instance.json")
    value = state.get("terminal_sha256") if isinstance(state, dict) else None
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise InstanceProvisionFailed("published instance terminal digest invalid")
    return value


def _decrypt_envelope(payload: dict, secrets_root: Path) -> str:
    envelope = payload.get("credential_envelope")
    if not isinstance(envelope, dict):
        raise CredentialEnvelopeInvalid("missing credential_envelope in job payload")
    try:
        key = WindowsSecretStore(secrets_root).read(AGENT_SCOPE_ID, PROVISIONING_KEY_SECRET_NAME)
    except Exception as exc:
        raise SecretStoreFailed("provisioning encryption key unavailable") from exc
    try:
        credentials = decrypt_credential_envelope(envelope, key)
    except Exception as exc:
        raise CredentialDecryptionFailed("credential envelope decryption failed") from exc
    password = credentials.get("investor_password")
    if not isinstance(password, str) or not password or "\n" in password or "\r" in password:
        raise CredentialEnvelopeInvalid("investor_password missing or invalid in envelope")
    return password


def _expected_identity(payload: dict) -> tuple[int, str, str | None]:
    raw_login = payload.get("expected_login")
    try:
        login = int(raw_login)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise CredentialEnvelopeInvalid("expected_login missing or invalid") from exc
    if login <= 0:
        raise CredentialEnvelopeInvalid("expected_login must be positive")
    server = str(payload.get("expected_server") or "").strip()
    if not SERVER_PATTERN.fullmatch(server):
        raise CredentialEnvelopeInvalid("expected_server missing or invalid")
    raw_broker_label = payload.get("broker_label")
    broker_label = (
        raw_broker_label.strip()
        if isinstance(raw_broker_label, str)
        else None
    )
    if broker_label is not None and not BROKER_PATTERN.fullmatch(broker_label):
        raise CredentialEnvelopeInvalid("broker_label missing or invalid")
    return login, server, broker_label


def _broker_key(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def _mtapi_candidates_for_broker(
    result: MtApiSearchResult | None,
    broker_label: str | None,
) -> tuple[str, ...]:
    """Keep MTAPI endpoints only after broker identity has been selected.

    An ambiguous MTAPI company match may still contain useful endpoint candidates. We never
    select a company from MTAPI alone: the AI/Wizard identity is authoritative for the label,
    then candidates are restricted to company names that contain the selected identity (or are
    contained by it). Unrelated companies remain excluded.
    """

    if result is None or not result.candidates:
        return ()
    if result.outcome == "EXACT_MATCH":
        return tuple(
            candidate.server_address
            for candidate in result.candidates[:_MAX_MTAPI_LOGIN_ENDPOINTS]
        )
    if result.outcome != "AMBIGUOUS" or not broker_label:
        return ()
    selected = _broker_key(broker_label)
    if not selected:
        return ()
    return tuple(
        candidate.server_address
        for candidate in result.candidates
        if any(
            selected in _broker_key(company_name)
            or _broker_key(company_name) in selected
            for company_name in (
                candidate.company_names or (candidate.company_name,)
            )
        )
    )[:_MAX_MTAPI_LOGIN_ENDPOINTS]


def _wizard_identity_from_evidence(
    evidence: BrokerWizardEvidence,
    payload_broker_label: str | None,
) -> str:
    """Choose the authoritative broker label without losing UI provenance.

    A caller-provided broker is accepted only when it matches one of the two
    labels that the wizard actually observed on its selected row.  This keeps a
    canonical control-plane label where one is known, while never claiming a
    label that was absent from the native MT5 UI.
    """

    observed = evidence.selected_broker_labels or (
        evidence.selected_broker_label,
    )
    if payload_broker_label is not None:
        expected = _broker_key(payload_broker_label)
        if not expected or not any(
            _broker_key(label) == expected for label in observed
        ):
            raise BrokerIdentityUnavailable(
                "broker wizard identity conflicts with the request"
            )
        return payload_broker_label
    if not BROKER_PATTERN.fullmatch(evidence.selected_broker_label):
        raise BrokerIdentityUnavailable("broker wizard identity is invalid")
    return evidence.selected_broker_label


def _single_mtapi_broker_label(candidate: MtApiEndpointCandidate) -> str | None:
    """Return an MTAPI company only when the endpoint's identity is unique."""

    labels = candidate.company_names or (candidate.company_name,)
    return labels[0] if len(labels) == 1 else None


def _history_window(job: dict) -> tuple[HistoryMode, "datetime | None"]:
    mode = job.get("history_mode")
    if mode not in ("new_only", "from_date", "all_available"):
        raise CredentialEnvelopeInvalid("invalid or missing history_mode")
    from_date = None
    if mode == "from_date":
        raw = job.get("from_date")
        try:
            from_date = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except (TypeError, ValueError) as exc:
            raise CredentialEnvelopeInvalid("invalid from_date") from exc
        if from_date.tzinfo is None:
            from_date = from_date.replace(tzinfo=timezone.utc)
    return mode, from_date


def _map_mt5_error(exc: Mt5Error) -> AgentError:
    if isinstance(exc, IdentityMismatch):
        # Some injected test adapters do not distinguish which identity field mismatched.
        # account_identity_mismatch is the conservative primary code; the native runtime emits
        # separate identity_mismatch/server_identity_mismatch codes.
        return AccountIdentityMismatch(str(exc))
    if isinstance(exc, (Mt5IpcError, Mt5ProcessCrashed)):
        return Mt5InitializeFailed(str(exc))
    text = str(exc).casefold()
    if text == "server_identity_mismatch":
        return ServerIdentityMismatch("server_identity_mismatch")
    if (
        text in {"authorization_failed", "invalid_account"}
        or "authorization failed" in text
    ):
        return Mt5AuthorizationFailed(str(exc))
    return Mt5InitializeFailed(str(exc))


_ENDPOINT_FAILURE_REASONS = {
    "endpoint_connection_failed": "ENDPOINT_CONNECTION_FAILED",
    "endpoint_connection_refused": "ENDPOINT_CONNECTION_REFUSED",
    "endpoint_protocol_incompatible": "ENDPOINT_PROTOCOL_INCOMPATIBLE",
    "endpoint_server_unrecognized": "ENDPOINT_SERVER_UNRECOGNIZED",
    "server_identity_mismatch": "SERVER_IDENTITY_MISMATCH",
}


def _endpoint_failure_reason(exc: AgentError) -> str | None:
    """Classify only failures that disprove the exact endpoint.

    Authentication errors, timeouts, process failures and generic network
    failures deliberately return ``None`` so bad credentials or an unhealthy
    VPS cannot invalidate a shared verified endpoint.
    """

    if isinstance(exc, ServerIdentityMismatch):
        return "SERVER_IDENTITY_MISMATCH"
    return _ENDPOINT_FAILURE_REASONS.get(str(exc).strip().casefold())


def _invalidate_failed_endpoint(
    endpoint: VerifiedBrokerEndpoint | None,
    exc: AgentError,
    invalidator: BrokerEndpointInvalidator | None,
) -> None:
    if endpoint is None:
        return
    reason = _endpoint_failure_reason(exc)
    if reason is None:
        return
    if invalidator is None:
        raise BrokerEndpointUnavailable(
            "verified endpoint invalidation is not configured"
        ) from exc
    try:
        invalidator(
            EndpointInvalidation(
                endpoint=endpoint,
                reason=reason,
                invalidated_at_unix_ms=int(time.time() * 1000),
                event_id=str(uuid4()),
            )
        )
    except (BrokerEndpointPublicationError, OSError) as invalidation_exc:
        raise BrokerEndpointUnavailable(
            "verified endpoint invalidation failed"
        ) from invalidation_exc


def _ensure_no_stale_process(
    terminal: Path, state_path: Path, process_factory: Callable[[Path], Any]
) -> None:
    """Remove only an orphan using this isolated terminal path before a new job."""
    if terminal.is_file() and ProcessManager.find(terminal):
        process_factory(state_path).cleanup_path(terminal)


@dataclass(frozen=True)
class StartupReconciliation:
    """Sanitized result of reconciling isolated terminals after an Agent restart."""

    adopted: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    terminated: tuple[str, ...] = ()
    blocked: tuple[str, ...] = ()


@dataclass(frozen=True)
class StartupRecovery:
    """Sanitized result of one passwordless, local-only reboot recovery pass."""

    recovered: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()


def reconcile_startup_instances(
    instances_root: Path,
    secrets_root: Path,
    process_factory: Callable[[Path], Any] = ProcessManager,
) -> StartupReconciliation:
    """Adopt valid running terminals and clean only demonstrably unsafe or ambiguous ones.

    Restarting the Windows service must not interrupt a healthy MT5 process.  Every terminal is
    isolated under its connection UUID, so one exact process plus a valid published instance is
    safe to adopt into fresh local PID state.  A missing process is returned to the caller for a
    single passwordless local recovery pass. Invalid publications and duplicate exact-path
    processes remain fail-closed and are terminated.
    """

    adopted: list[str] = []
    missing: list[str] = []
    terminated: list[str] = []
    blocked: list[str] = []
    if not instances_root.exists():
        return StartupReconciliation()
    if (
        InstanceProvisioner._is_reparse_point(instances_root)
        or not instances_root.is_dir()
    ):
        raise ValueError("instances root is unsafe")

    provisioner = InstanceProvisioner(instances_root, secrets_root)
    for entry in sorted(instances_root.iterdir(), key=lambda value: value.name):
        if (
            InstanceProvisioner._is_reparse_point(entry)
            or not entry.is_dir()
        ):
            continue
        try:
            connection_id = canonical_uuid(entry.name)
        except ValueError:
            continue
        terminal = entry / "terminal" / "terminal64.exe"
        state_path = entry / "state" / "terminal-process.json"
        pids = ProcessManager.find(terminal)

        publication_valid = False
        try:
            published = provisioner.validate(connection_id, verify_code=False)
            publication_valid = (
                published.resolve() == entry.resolve()
                and terminal.is_file()
                and not InstanceProvisioner._is_reparse_point(terminal)
            )
        except (OSError, ValueError):
            publication_valid = False

        if publication_valid and not pids:
            missing.append(connection_id)
            continue
        if publication_valid and len(pids) == 1:
            try:
                process_factory(state_path).adopt(terminal)
            except InteractiveIdentityError:
                # A path match is insufficient after account demotion or a
                # stale RDP reconnect: an old Administrator token can keep
                # running the exact terminal.  Never adopt it into the new
                # service; terminate only this isolated instance and let the
                # passwordless standard-user recovery path recreate it.
                try:
                    cleaned = bool(
                        process_factory(state_path).cleanup_path(terminal)
                    )
                except (OSError, RuntimeError, ValueError):
                    cleaned = False
                if cleaned:
                    terminated.append(connection_id)
                    missing.append(connection_id)
                else:
                    blocked.append(connection_id)
                continue
            except (OSError, RuntimeError, ValueError):
                # The exact, valid terminal is already running.  A local state-write failure is
                # not authority to interrupt the user's connection; leave it alive and surface
                # the blocked reconciliation for operator repair.
                blocked.append(connection_id)
                continue
            else:
                adopted.append(connection_id)
                continue

        if not pids:
            continue
        try:
            cleaned = bool(
                process_factory(state_path).cleanup_path(terminal)
            )
        except (OSError, RuntimeError, ValueError):
            cleaned = False
        if cleaned:
            terminated.append(connection_id)
        else:
            blocked.append(connection_id)

    return StartupReconciliation(
        adopted=tuple(adopted),
        missing=tuple(missing),
        terminated=tuple(terminated),
        blocked=tuple(blocked),
    )


def recover_startup_instances(
    instances_root: Path,
    secrets_root: Path,
    connection_ids: tuple[str, ...],
    expert_binary: Path,
    expert_sha256: str | None,
    *,
    process_factory: Callable[[Path], Any] = ProcessManager,
    process_finder: Callable[[Path], list[int]] = ProcessManager.find,
    runtime_factory: Callable[[Path, str], Any] = NativeMt5Runtime,
    verified_update_callback: Callable[[Path, Path, Path, str], str | None]
    | None = None,
    verified_update_required: bool = False,
) -> StartupRecovery:
    """Resume missing managed terminals once, without a control-plane job or password.

    The terminal's protected ``accounts.dat`` is reused by ``NativeMt5Runtime.resume``. Only the
    stored account identity and server are decrypted; the investor password is never read. Every
    publication, executable and managed runtime asset is checked before launch, and the asset pin
    is checked again afterwards because resume reinstalls the pinned bridge binary.
    """

    recovered: list[str] = []
    failed: list[str] = []
    provisioner = InstanceProvisioner(instances_root, secrets_root)
    store = WindowsSecretStore(secrets_root)
    for raw_connection_id in connection_ids:
        runtime: Any | None = None
        try:
            connection_id = canonical_uuid(raw_connection_id)
            root = provisioner.validate(connection_id, verify_code=False)
            terminal = root / "terminal" / "terminal64.exe"
            state_path = root / "state" / "terminal-process.json"
            _verify_binary_pin(
                terminal,
                _recorded_instance_terminal_sha256(root),
            )
            provisioner.validate_runtime_assets(connection_id)

            running = process_finder(terminal)
            if len(running) > 1:
                raise RuntimeError("ambiguous terminal process identity")
            if len(running) == 1:
                process_factory(state_path).adopt(terminal)
                recovered.append(connection_id)
                continue

            try:
                login = int(store.read(connection_id, "mt5_login"))
                server = store.read(connection_id, "mt5_server")
                # A terminal without its ingestion credential must remain stopped: it could
                # generate local events but would have no authenticated route to deliver them.
                store.read(connection_id, "bridge_token")
            except Exception as exc:
                raise SecretStoreFailed(
                    "stored startup recovery identity unavailable"
                ) from exc
            if login <= 0 or not SERVER_PATTERN.fullmatch(server):
                raise SecretStoreFailed("stored startup recovery identity invalid")

            _verify_binary_pin(expert_binary, expert_sha256)
            runtime = runtime_factory(root, connection_id)
            if verified_update_callback is not None:
                setter = getattr(
                    runtime,
                    "set_verified_vendor_update_callback",
                    None,
                )
                if not callable(setter):
                    if verified_update_required:
                        raise RuntimeError(
                            "verified MT5 update callback is unavailable"
                        )
                else:
                    setter(
                        verified_update_callback,
                        required=verified_update_required,
                    )
            history_from = new_only_recovery_from(root)
            trusted_expert_sha256 = hashlib.sha256(
                expert_binary.read_bytes()
            ).hexdigest()
            runtime.install_expert(
                expert_binary,
                "new_only",
                history_from,
            )
            provisioner.record_verified_managed_asset_update(
                root,
                connection_id,
                trusted_expert_sha256,
            )
            runtime.resume(
                login=login,
                server=server,
                expert_binary=expert_binary,
                history_mode="new_only",
                history_from=history_from,
            )
            provisioner.validate_runtime_assets(connection_id)
            process_factory(state_path).adopt(terminal)
            _progress(
                root,
                status="connected",
                recovery="service_startup",
                connection_id=connection_id,
            )
            recovered.append(connection_id)
        except Exception as exc:
            if runtime is not None:
                try:
                    runtime.stop()
                except Exception:
                    pass
            connection_label = str(raw_connection_id)
            failed.append(connection_label)
            error_code = (
                str(exc)
                if isinstance(exc, NativeMt5Error)
                else type(exc).__name__
            )
            logger.error(
                "local MT5 startup recovery failed "
                "(connection=%s, error=%s, code=%s)",
                connection_label,
                type(exc).__name__,
                error_code,
            )
    return StartupRecovery(recovered=tuple(recovered), failed=tuple(failed))


def _history_dedup_key(kind: str, record: dict) -> str:
    ticket = record.get("ticket", record.get("deal_ticket", ""))
    return f"{kind}:{ticket}"


def _deduped_sink(dedup: PersistentDedup, sink: Callable[[dict], None]) -> Callable[[dict], None]:
    def wrapped(entry: dict) -> None:
        key = _history_dedup_key(entry.get("kind", ""), entry.get("record", {}))
        if dedup.contains(key):
            return
        sink(entry)
        dedup.add(key)

    return wrapped


def build_real_handlers(
    api: Any,
    *,
    instances_root: Path,
    secrets_root: Path,
    source_terminal: Path,
    adapter_factory: Callable[..., Any] | None = None,
    process_factory: Callable[[Path], Any] = ProcessManager,
    expert_binary: Path = DEFAULT_EXPERT_BINARY,
    runtime_factory: Callable[[Path, str], NativeMt5Runtime] = NativeMt5Runtime,
    trading_ingestion_url: str = "",
    terminal_sha256: str | None = None,
    expert_sha256: str | None = None,
    endpoint_resolver: Callable[
        [str | None, str],
        VerifiedBrokerEndpoint,
    ] | None = None,
    endpoint_observer: BrokerEndpointObserver | None = None,
    endpoint_publisher: BrokerEndpointPublisher | None = None,
    endpoint_invalidator: BrokerEndpointInvalidator | None = None,
    broker_identity_resolver: Callable[[str], BrokerIdentitySuggestion] | None = None,
    broker_wizard: BrokerWizard | None = None,
    mtapi_search: MtApiSearch | None = None,
    instance_pool: Mt5InstancePool | None = None,
    template_manager: Mt5TemplateManager | None = None,
    pending_update_store: Mt5PendingUpdateStore | None = None,
    lifecycle_coordinator: Mt5LifecycleCoordinator | None = None,
) -> dict[str, JobHandler]:
    """Real provision/historical_sync/deprovision handlers.

    Jobs are claimed by the managed JobRunner/control plane. Without an injected
    ``adapter_factory`` the Windows-native MQL5 file path is always used; the injection point
    exists only for isolated tests.
    """

    store = WindowsSecretStore(secrets_root)

    def _current_template_sha256() -> str | None:
        if template_manager is not None:
            return template_manager.current_sha256
        return terminal_sha256

    def _promote_verified_vendor_update(
        bundle_root: Path,
        updater: Path,
        config: Path,
        signer_subject: str,
    ) -> str | None:
        if pending_update_store is not None:
            return pending_update_store.capture(
                bundle_root,
                updater,
                config,
                signer_subject,
            ).receipt_id
        # A connection job is never allowed to mutate the shared golden
        # template. Production always supplies the durable pending store; a
        # deliberately minimal/test wiring simply leaves the account-local
        # verified update isolated.
        return None

    def _configure_runtime(runtime: Any, job: dict) -> Any:
        set_cancel_check = getattr(runtime, "set_cancel_check", None)
        if callable(set_cancel_check):
            set_cancel_check(job.get("_lease_guard"))
        set_update_callback = getattr(
            runtime,
            "set_verified_vendor_update_callback",
            None,
        )
        if callable(set_update_callback) and pending_update_store is not None:
            set_update_callback(
                _promote_verified_vendor_update,
                required=pending_update_store is not None,
            )
        return runtime

    def _provision_instance(connection_id: str) -> Path:
        if instance_pool is not None:
            pooled_root = instance_pool.claim(connection_id)
            if pooled_root is not None:
                return pooled_root
        template_guard = (
            template_manager.lock
            if template_manager is not None
            else nullcontext()
        )
        with template_guard:
            return InstanceProvisioner(
                instances_root,
                secrets_root,
            ).provision(
                connection_id,
                source_terminal,
                _current_template_sha256(),
            )

    def _provision_once(job: dict) -> dict:
        cid = canonical_uuid(str(job["connection_id"]))
        payload = job.get("payload") or {}
        _require_lease(api, job)
        _verify_binary_pin(source_terminal, _current_template_sha256())
        _verify_binary_pin(expert_binary, expert_sha256)
        login, server, broker_label = _expected_identity(payload)
        payload_broker_label = broker_label
        mode, from_date = _history_window(job)
        broker_search_text = broker_label
        verified_endpoint: VerifiedBrokerEndpoint | None = None
        mtapi_result: MtApiSearchResult | None = None
        mtapi_candidate_endpoints: tuple[str, ...] = ()
        mtapi_candidates_by_endpoint: dict[str, MtApiEndpointCandidate] = {}
        if broker_label is not None:
            _record_checkpoint(
                api,
                job,
                "broker_identity",
                "completed",
                "request_identity",
            )
        _record_checkpoint(api, job, "endpoint_resolution", "started")
        # A previously promoted exact server identifies its broker without an
        # AI call. Ambiguous or missing server matches fall through to the
        # identity resolver and normal broker-specific lookup.
        if broker_label is None and endpoint_resolver is not None:
            try:
                verified_endpoint = endpoint_resolver(None, server)
            except BrokerEndpointResolutionError:
                pass
            else:
                broker_label = verified_endpoint.broker_label
                broker_search_text = broker_label
                _record_checkpoint(
                    api,
                    job,
                    "broker_identity",
                    "completed",
                    "verified_registry_hit",
                )
        # MTAPI is always the first endpoint source after a VERIFIED registry miss. Its
        # candidates are tried directly; AI/Wizard is strictly a fallback after all candidates
        # have failed. MTAPI never authorizes an endpoint by itself.
        if verified_endpoint is None and mtapi_search is not None:
            try:
                mtapi_result = mtapi_search(server)
            except MtApiSearchError:
                _record_checkpoint(
                    api,
                    job,
                    "broker_discovery",
                    "info",
                    "mtapi_search_unavailable",
                )
            else:
                _record_checkpoint(
                    api,
                    job,
                    "broker_discovery",
                    "info",
                    f"mtapi_search_{mtapi_result.outcome.casefold()}",
                )
                if mtapi_result.candidates:
                    login_candidates = mtapi_result.candidates[
                        :_MAX_MTAPI_LOGIN_ENDPOINTS
                    ]
                    mtapi_candidate_endpoints = tuple(
                        candidate.server_address for candidate in login_candidates
                    )
                    mtapi_candidates_by_endpoint = {
                        candidate.server_address: candidate
                        for candidate in login_candidates
                    }
                    _record_checkpoint(
                        api,
                        job,
                        "endpoint_resolution",
                        "completed",
                        "mtapi_candidates_pending",
                    )
        if broker_label is None and not mtapi_candidate_endpoints:
            if broker_identity_resolver is None:
                raise BrokerIdentityUnavailable(
                    "broker identity resolver is not configured"
                )
            try:
                _record_checkpoint(
                    api,
                    job,
                    "broker_identity",
                    "started",
                    "identity_resolution_required",
                )
                identity = broker_identity_resolver(server)
                broker_label = identity.broker_label
                broker_search_text = identity.search_text
            except BrokerIdentityError as exc:
                raise BrokerIdentityUnavailable(
                    "broker identity suggestion is unavailable"
                ) from exc
            if not BROKER_PATTERN.fullmatch(broker_label):
                raise BrokerIdentityUnavailable(
                    "broker identity suggestion is invalid"
                )
            _record_checkpoint(
                api,
                job,
                "broker_identity",
                "completed",
                "identity_resolved",
            )
        wizard_evidence: BrokerWizardEvidence | None = None
        authenticated_mtapi_candidate: MtApiEndpointCandidate | None = None
        if mtapi_candidate_endpoints:
            connection_endpoint = mtapi_candidate_endpoints[0]
        elif endpoint_resolver is None:
            connection_endpoint = server
            _record_checkpoint(
                api,
                job,
                "endpoint_resolution",
                "skipped",
                "resolver_not_configured",
            )
        elif verified_endpoint is not None:
            connection_endpoint = verified_endpoint.server_address
            _record_checkpoint(
                api,
                job,
                "endpoint_resolution",
                "completed",
                "verified_registry_hit",
            )
        else:
            try:
                verified_endpoint = endpoint_resolver(broker_label, server)
                connection_endpoint = verified_endpoint.server_address
                _record_checkpoint(
                    api,
                    job,
                    "endpoint_resolution",
                    "completed",
                    "verified_registry_hit",
                )
            except BrokerEndpointResolutionError as exc:
                mtapi_candidate_endpoints = _mtapi_candidates_for_broker(
                    mtapi_result,
                    broker_label,
                )
                if mtapi_candidate_endpoints:
                    connection_endpoint = mtapi_candidate_endpoints[0]
                    _record_checkpoint(
                        api,
                        job,
                        "endpoint_resolution",
                        "completed",
                        "mtapi_candidates_pending",
                    )
                else:
                    if broker_wizard is None:
                        raise BrokerEndpointUnavailable(
                            "verified broker endpoint unavailable"
                        ) from exc
                    _record_checkpoint(
                        api,
                        job,
                        "endpoint_resolution",
                        "completed",
                        "discovery_required",
                    )
                    try:
                        root = _provision_instance(cid)
                    except Exception as provision_exc:
                        raise InstanceProvisionFailed(
                            "broker census instance provisioning failed"
                        ) from provision_exc
                    _progress(
                        root,
                        status="censusing_broker",
                        connection_id=cid,
                    )
                    wizard_search_text = broker_search_text or broker_label
                    if mtapi_result is not None:
                        try:
                            atomic_json(
                                root / "state" / "mtapi-search.json",
                                mtapi_result.to_audit_document(server),
                            )
                        except Exception as audit_exc:
                            raise InstanceProvisionFailed(
                                "MTAPI Search audit publication failed"
                            ) from audit_exc
                    _record_checkpoint(
                        api,
                        job,
                        "broker_discovery",
                        "started",
                        "wizard_required",
                        root=root,
                        local_status="censusing_broker",
                    )
                    try:
                        wizard_evidence = broker_wizard(
                            root,
                            wizard_search_text,
                            broker_label,
                            server,
                            job.get("_lease_guard"),
                        )
                    except BrokerWizardError as wizard_exc:
                        _record_checkpoint(
                            api,
                            job,
                            "broker_discovery",
                            "failed",
                            wizard_exc.detail_code,
                            root=root,
                            local_status="broker_discovery_failed",
                        )
                        raise BrokerDiscoveryFailed(
                            "broker wizard failed closed"
                        ) from wizard_exc
                    broker_label = _wizard_identity_from_evidence(
                        wizard_evidence,
                        payload_broker_label,
                    )
                    connection_endpoint = server
                    _record_checkpoint(
                        api,
                        job,
                        "broker_discovery",
                        "completed",
                        "server_matched",
                        root=root,
                        local_status="broker_censused",
                    )
                    try:
                        InstanceProvisioner(
                            instances_root, secrets_root
                        ).remove_generated_example_code(cid)
                    except Exception as cleanup_exc:
                        raise InstanceProvisionFailed(
                            "broker census executable cleanup failed"
                        ) from cleanup_exc
                    _require_lease(api, job)
        password = _decrypt_envelope(payload, secrets_root)

        bridge_token = payload.get("bridge_token")
        _record_checkpoint(api, job, "instance_preparation", "started")
        try:
            store.write(cid, "mt5_login", str(login))
            store.write(cid, "mt5_server", server)
            store.write(cid, "mt5_broker_label", broker_label or "MTAPI candidate")
            store.write(cid, "mt5_endpoint", connection_endpoint)
            store.write(cid, "mt5_investor_password", password)
            # Issued fresh per provision run by request_mt5_provisioning_job (mt5_managed only) --
            # the live_sync job reads this back to authenticate its HTTP delivery to
            # trading-mt5-events, exactly like the manual EA/self-hosted worker connectors already
            # do with their own customer-generated bridge tokens. Older re-provision runs may omit
            # it (server-side rollout not yet applied); live_sync raises a clear config error in
            # that case rather than silently never delivering anything.
            if isinstance(bridge_token, str) and bridge_token:
                store.write(cid, "bridge_token", bridge_token)
        except Exception as exc:
            raise SecretStoreFailed("failed to persist credentials to DPAPI") from exc
        finally:
            del password
            bridge_token = None
            gc.collect()

        try:
            if not source_terminal.is_file():
                raise TerminalStartFailed("golden MT5 terminal template missing")
            # Provision is also the integrity gate for an already-published instance:
            # retries may reuse it only after its pinned terminal and complete template
            # manifest have been independently recomputed.
            root = _provision_instance(cid)
        except TerminalStartFailed:
            raise
        except Exception as exc:
            raise InstanceProvisionFailed("instance provisioning failed") from exc
        _record_checkpoint(
            api,
            job,
            "instance_preparation",
            "completed",
            root=root,
            local_status="provisioned",
        )
        if mtapi_result is not None:
            try:
                atomic_json(
                    root / "state" / "mtapi-search.json",
                    mtapi_result.to_audit_document(server),
                )
            except Exception as audit_exc:
                raise InstanceProvisionFailed(
                    "MTAPI Search audit publication failed"
                ) from audit_exc

        try:
            if adapter_factory is None:
                result = None
                candidate_authorization_error: Mt5AuthorizationFailed | None = None
                if mtapi_candidate_endpoints:
                    for candidate_endpoint in mtapi_candidate_endpoints:
                        connection_endpoint = candidate_endpoint
                        store.write(cid, "mt5_endpoint", connection_endpoint)
                        candidate = mtapi_candidates_by_endpoint[
                            candidate_endpoint
                        ]
                        candidate_broker = _single_mtapi_broker_label(candidate)
                        # MTAPI company data is an untrusted hint.  Never let it
                        # overwrite a broker identity provided by the request or
                        # resolved from a previously verified registry entry.
                        if candidate_broker and broker_label is None:
                            store.write(cid, "mt5_broker_label", candidate_broker)
                        candidate_observer: BrokerEndpointObserver | None = None
                        if endpoint_observer is not None and candidate.is_ip_address:
                            def candidate_observer(
                                pid: int,
                                *,
                                _candidate: MtApiEndpointCandidate = candidate,
                            ) -> ObservedProcessEndpoint:
                                return endpoint_observer(
                                    pid,
                                    expected_host=_candidate.host,
                                    expected_port=_candidate.port,
                                )
                        try:
                            result = _start_file_bridge_and_sync(
                                job, api, root, cid, login, server,
                                connection_endpoint, mode, from_date, store,
                                process_factory, expert_binary, runtime_factory,
                                trading_ingestion_url,
                                endpoint_observer=candidate_observer,
                                runtime_configurer=_configure_runtime,
                            )
                            authenticated_mtapi_candidate = candidate
                            if candidate_broker and broker_label is None:
                                broker_label = candidate_broker
                            _record_checkpoint(
                                api,
                                job,
                                "endpoint_resolution",
                                "completed",
                                "mtapi_candidate_authenticated",
                                root=root,
                                local_status="candidate_endpoint_authenticated",
                            )
                            break
                        except AgentError as candidate_exc:
                            if isinstance(candidate_exc, Mt5AuthorizationFailed):
                                # Access points published for the same exact MT5
                                # server are peers. A newly issued login can be
                                # visible on one before another, so try the next
                                # bounded candidate without invalidating either
                                # endpoint. If none accepts the account, surface
                                # the authentication failure and do not add a
                                # third attempt through the broker wizard.
                                candidate_authorization_error = candidate_exc
                                _record_checkpoint(
                                    api,
                                    job,
                                    "endpoint_resolution",
                                    "info",
                                    "mtapi_candidate_authentication_rejected",
                                    root=root,
                                    local_status="candidate_authentication_rejected",
                                )
                                continue
                            reason = _endpoint_failure_reason(candidate_exc)
                            if reason is None:
                                # Authentication, timeout and process failures do not prove
                                # that a candidate endpoint is invalid.
                                raise
                            _record_checkpoint(
                                api,
                                job,
                                "endpoint_resolution",
                                "info",
                                f"mtapi_candidate_{reason.casefold()}",
                                root=root,
                                local_status="candidate_endpoint_rejected",
                            )
                    if result is None:
                        if candidate_authorization_error is not None:
                            raise candidate_authorization_error
                        if broker_wizard is None:
                            raise BrokerEndpointUnavailable(
                                "all MTAPI candidate endpoints failed"
                            )
                        if broker_label is None:
                            if broker_identity_resolver is None:
                                raise BrokerIdentityUnavailable(
                                    "broker identity resolver is not configured"
                                )
                            try:
                                identity = broker_identity_resolver(server)
                                broker_label = identity.broker_label
                                broker_search_text = identity.search_text
                            except BrokerIdentityError as exc:
                                raise BrokerIdentityUnavailable(
                                    "broker identity suggestion is unavailable"
                                ) from exc
                        _record_checkpoint(
                            api,
                            job,
                            "broker_discovery",
                            "started",
                            "wizard_required_after_mtapi_candidates",
                            root=root,
                            local_status="censusing_broker",
                        )
                        try:
                            wizard_evidence = broker_wizard(
                                root,
                                server,
                                broker_label,
                                server,
                                job.get("_lease_guard"),
                            )
                        except BrokerWizardError as wizard_exc:
                            _record_checkpoint(
                                api,
                                job,
                                "broker_discovery",
                                "failed",
                                wizard_exc.detail_code,
                                root=root,
                                local_status="broker_discovery_failed",
                            )
                            raise BrokerDiscoveryFailed(
                                "broker wizard failed closed"
                            ) from wizard_exc
                        broker_label = _wizard_identity_from_evidence(
                            wizard_evidence,
                            payload_broker_label,
                        )
                        connection_endpoint = server
                        result = _start_file_bridge_and_sync(
                            job, api, root, cid, login, server,
                            connection_endpoint, mode, from_date, store,
                            process_factory, expert_binary, runtime_factory,
                            trading_ingestion_url,
                            endpoint_observer=endpoint_observer,
                            runtime_configurer=_configure_runtime,
                        )
                else:
                    result = _start_file_bridge_and_sync(
                        job, api, root, cid, login, server, connection_endpoint,
                        mode, from_date, store, process_factory, expert_binary,
                        runtime_factory, trading_ingestion_url,
                        endpoint_observer=endpoint_observer,
                        runtime_configurer=_configure_runtime,
                    )
            else:
                result = _authenticate_and_sync(
                    job, api, root, cid, login, server, mode, from_date, store,
                    adapter_factory, process_factory, trading_ingestion_url,
                )
        except AgentError as exc:
            _invalidate_failed_endpoint(
                verified_endpoint,
                exc,
                endpoint_invalidator,
            )
            raise
        verification_pid = result.pop("_verification_pid", None)
        verification_observation = result.pop("_verification_observation", None)
        effective_server = str(
            result.get("effective_server_name") or server
        )
        redirected = effective_server.casefold() != server.casefold()
        result.update(
            {
                "requested_server_name": server,
                "effective_server_name": effective_server,
                "server_redirected": redirected,
            }
        )
        if redirected:
            result["server_redirect_code"] = "SERVER_REDIRECT_DETECTED"
        if verified_endpoint is not None:
            result.update(
                {
                    "verified_server_name": effective_server,
                    "verified_broker_label": broker_label,
                    "verification_method": "managed_investor_login",
                    "endpoint_protocol": verified_endpoint.protocol,
                    "endpoint_artifact_sha256": verified_endpoint.artifact_sha256,
                    "endpoint_verification_session_id": (
                        verified_endpoint.verification_session_id
                    ),
                }
            )
        elif wizard_evidence is not None or authenticated_mtapi_candidate is not None:
            verification_evidence: (
                BrokerWizardEvidence | LoginVerificationEvidence
            )
            if wizard_evidence is not None:
                verification_evidence = wizard_evidence
            else:
                # A successful direct endpoint attempt has MTAPI provenance,
                # not broker-wizard provenance.  Its identity is promotable
                # only when the request/registry supplied one, or MTAPI had a
                # single company for that exact IP:port.
                if broker_label is None:
                    result.update(
                        {
                            "verified_server_name": effective_server,
                            "verification_method": "managed_investor_login",
                            "endpoint_registry_status": "NOT_PROMOTED",
                            "endpoint_registry_reason": "broker_identity_ambiguous",
                        }
                    )
                    _progress(root, status="connected")
                    return result
                artifact_source = root / "state" / "mtapi-search.json"
                if not artifact_source.is_file():
                    raise BrokerDiscoveryFailed(
                        "MTAPI login verification artifact is unavailable"
                    )
                artifact_digest = hashlib.sha256(
                    artifact_source.read_bytes()
                ).hexdigest()
                verification_evidence = LoginVerificationEvidence(
                    run_id=str(uuid4()),
                    expected_server_name=server,
                    broker_label=broker_label,
                    source_kind="MTAPI_SEARCH",
                    source_artifact_path=artifact_source,
                    source_artifact_sha256=artifact_digest,
                )
            if (
                not isinstance(verification_pid, int)
                or isinstance(verification_pid, bool)
                or verification_pid <= 0
            ):
                process_factory(
                    root / "state" / "terminal-process.json"
                ).stop()
                raise BrokerDiscoveryFailed(
                    "login verification process identity is unavailable"
                )
            observation: ObservedProcessEndpoint | None = (
                verification_observation
                if isinstance(verification_observation, ObservedProcessEndpoint)
                else None
            )
            registry_reason: str | None = None
            if observation is None and (endpoint_observer is None or endpoint_publisher is None):
                registry_reason = "endpoint_promotion_not_configured"
            elif observation is None:
                try:
                    if (
                        authenticated_mtapi_candidate is not None
                        and authenticated_mtapi_candidate.is_ip_address
                    ):
                        observation = endpoint_observer(
                            verification_pid,
                            expected_host=authenticated_mtapi_candidate.host,
                            expected_port=authenticated_mtapi_candidate.port,
                        )
                    else:
                        observation = endpoint_observer(verification_pid)
                    if observation.pid != verification_pid:
                        raise BrokerEndpointObservationError(
                            "process endpoint PID mismatch"
                        )
                except BrokerEndpointObservationError:
                    registry_reason = "endpoint_observation_unavailable"
            try:
                artifact_path, artifact_digest = write_login_verification_artifact(
                    root / "state",
                    evidence=verification_evidence,
                    verification_pid=verification_pid,
                    verified_at_unix_ms=(
                        observation.observed_at_unix_ms
                        if observation is not None
                        else None
                    ),
                    process_creation_time_unix_ms=(
                        observation.process_creation_time_unix_ms
                        if observation is not None
                        else None
                    ),
                    remote_host=(
                        observation.host
                        if observation is not None
                        else None
                    ),
                    remote_port=(
                        observation.port
                        if observation is not None
                        else None
                    ),
                )
            except BrokerWizardError as exc:
                process_factory(
                    root / "state" / "terminal-process.json"
                ).stop()
                raise BrokerDiscoveryFailed(
                    "login verification evidence publication failed"
                ) from exc
            promoted_endpoint: VerifiedBrokerEndpoint | None = None
            if (
                observation is not None
                and endpoint_publisher is not None
            ):
                try:
                    promoted_endpoint = endpoint_publisher(
                        EndpointPromotion(
                            broker_label=broker_label,
                            server_name=server,
                            verification_session_id=verification_evidence.run_id,
                            verification_artifact=artifact_path,
                            verification_artifact_sha256=artifact_digest,
                            provenance_artifact=(
                                verification_evidence.artifact_path
                                if isinstance(
                                    verification_evidence,
                                    BrokerWizardEvidence,
                                )
                                else verification_evidence.source_artifact_path
                            ),
                            provenance_artifact_sha256=(
                                verification_evidence.artifact_sha256
                                if isinstance(
                                    verification_evidence,
                                    BrokerWizardEvidence,
                                )
                                else verification_evidence.source_artifact_sha256
                            ),
                            observation=observation,
                        )
                    )
                except (
                    BrokerEndpointPublicationError,
                    OSError,
                ):
                    registry_reason = "endpoint_registry_publication_failed"
                else:
                    try:
                        store.write(
                            cid,
                            "mt5_endpoint",
                            promoted_endpoint.server_address,
                        )
                    except Exception:
                        # The shared registry is already valid and future
                        # provisions can use it. A per-instance cache refresh
                        # failure must not terminate an authenticated account.
                        logger.warning(
                            "promoted endpoint could not refresh instance cache"
                        )
            result.update(
                {
                    "verified_server_name": effective_server,
                    "verified_broker_label": broker_label,
                    "verification_method": "managed_investor_login",
                    "endpoint_protocol": "TCP/TLS",
                    "endpoint_artifact_sha256": artifact_digest,
                    "endpoint_verification_session_id": verification_evidence.run_id,
                    "endpoint_registry_status": (
                        "PROMOTED"
                        if promoted_endpoint is not None
                        else "NOT_PROMOTED"
                    ),
                }
            )
            if promoted_endpoint is not None:
                result["promoted_endpoint"] = (
                    promoted_endpoint.server_address
                )
            elif registry_reason is not None:
                result["endpoint_registry_reason"] = registry_reason
        _progress(root, status="connected")
        return result

    def provision(job: dict) -> dict:
        cid = canonical_uuid(str(job["connection_id"]))
        root = InstanceLayout(instances_root, cid).path
        protected_at_start = _is_protected_instance(root, cid)
        stale_secrets = secrets_root / cid
        if protected_at_start:
            # A duplicate provision may reuse only a complete, independently
            # validated publication.  Ambiguous pre-existing state must be
            # rejected before decrypting or overwriting any stored credential.
            try:
                InstanceProvisioner(
                    instances_root,
                    secrets_root,
                ).validate(cid, _current_template_sha256())
            except Exception as exc:
                raise InstanceProvisionFailed(
                    "pre-existing instance integrity validation failed"
                ) from exc
        if not protected_at_start and (
            root.exists() or stale_secrets.exists()
        ):
            try:
                _discard_failed_provision(
                    instances_root,
                    secrets_root,
                    cid,
                    process_factory,
                )
            except Exception as cleanup_exc:
                raise InstanceCleanupFailed(
                    "stale failed provision cleanup could not be verified"
                ) from cleanup_exc
        try:
            return _provision_once(job)
        except LeaseLost:
            # Lease uncertainty is not authority to destroy local state: a
            # reclaimed job may already be continuing on another agent.
            raise
        except Exception:
            if protected_at_start or _is_protected_instance(root, cid):
                # A failed duplicate/re-provision attempt must never remove a
                # previously activated account.
                raise
            try:
                _discard_failed_provision(
                    instances_root,
                    secrets_root,
                    cid,
                    process_factory,
                )
            except Exception as cleanup_exc:
                raise InstanceCleanupFailed(
                    "failed provision cleanup could not be verified"
                ) from cleanup_exc
            raise

    def historical_sync(job: dict) -> dict:
        cid = canonical_uuid(str(job["connection_id"]))
        mode, from_date = _history_window(job)
        try:
            root = InstanceProvisioner(instances_root, secrets_root).validate(
                cid, verify_code=False
            )
            recorded_terminal_sha256 = _recorded_instance_terminal_sha256(root)
        except Exception as exc:
            raise InstanceProvisionFailed(
                "provisioned instance integrity validation failed"
            ) from exc
        try:
            login = int(store.read(cid, "mt5_login"))
            server = store.read(cid, "mt5_server")
        except Exception as exc:
            raise SecretStoreFailed("stored identity unavailable") from exc

        terminal = root / "terminal" / "terminal64.exe"
        _verify_binary_pin(terminal, recorded_terminal_sha256)
        state_path = root / "state" / "terminal-process.json"
        ingestion_sink = None
        if trading_ingestion_url:
            try:
                ingestion_sink = TradingIngestionSink(
                    root, trading_ingestion_url, store.read(cid, "bridge_token")
                )
            except Exception as exc:
                raise SecretStoreFailed("history ingestion token unavailable") from exc
        _record_checkpoint(
            api,
            job,
            "history_sync",
            "started",
            mode,
            root=root,
            local_status="importing_history",
        )
        if adapter_factory is None:
            # Reconfigure and restart only this isolated terminal. The persisted investor
            # session is reused; no password is read or retained for a later history job.
            _require_lease(api, job)
            provisioner = InstanceProvisioner(instances_root, secrets_root)
            try:
                provisioner.validate_runtime_assets(cid)
            except Exception as exc:
                raise InstanceProvisionFailed(
                    "provisioned managed runtime assets invalid"
                ) from exc
            _verify_binary_pin(expert_binary, expert_sha256)
            trusted_expert_sha256 = hashlib.sha256(expert_binary.read_bytes()).hexdigest()
            runtime = _configure_runtime(runtime_factory(root, cid), job)
            restored = False
            terminal_stopped = False
            try:
                if not runtime.stop():
                    raise TerminalStartFailed("terminal_stop_failed")
                terminal_stopped = True
                runtime.install_expert(expert_binary, mode, from_date)
                provisioner.record_verified_managed_asset_update(
                    root, cid, trusted_expert_sha256
                )
                runtime.resume(
                    login=login,
                    server=server,
                    expert_binary=expert_binary,
                    history_mode=mode,
                    history_from=from_date,
                )
                try:
                    process_factory(state_path).adopt(terminal)
                except (AttributeError, RuntimeError):
                    pass
                _progress(root, status="importing_history")
                adapter = Mql5FileMt5Adapter(
                    root / "terminal" / "MQL5" / "Files" / "TradeJournal",
                    cid,
                    login,
                    server,
                    root / "state",
                )
                _verify_investor_access(adapter)
                counts = _run_control_plane_history_import(
                    adapter,
                    root,
                    api,
                    job,
                    mode,
                    from_date,
                    str(login),
                    server,
                )
            except NativeMt5Error as exc:
                raise _map_mt5_error(exc) from exc
            finally:
                if terminal_stopped:
                    try:
                        recovery_from = new_only_recovery_from(root)
                        runtime.stop()
                        runtime.resume(
                            login=login,
                            server=server,
                            expert_binary=expert_binary,
                            history_mode="new_only",
                            history_from=recovery_from,
                        )
                        process_factory(state_path).adopt(terminal)
                        restored = True
                    except Exception as exc:
                        raise LiveSyncFailed(
                            "Auto-Sync could not be restored after history import"
                        ) from exc
            if restored and ingestion_sink is not None:
                live_adapter = Mql5FileMt5Adapter(
                    root / "terminal" / "MQL5" / "Files" / "TradeJournal",
                    cid,
                    login,
                    server,
                    root / "state",
                )
                _verify_investor_access(live_adapter)
                _run_live_sync_once(live_adapter, root, ingestion_sink)
        else:
            _require_lease(api, job)
            _progress(root, status="authenticating")
            try:
                investor_password = store.read(cid, "mt5_investor_password")
            except Exception as exc:
                raise SecretStoreFailed("stored credential unavailable") from exc
            _ensure_no_stale_process(terminal, state_path, process_factory)
            process = process_factory(state_path)
            adapter = adapter_factory(terminal, login, server)
            try:
                with adapter.session(investor_password):
                    investor_password = ""
                    gc.collect()
                    try:
                        process.adopt(terminal)
                    except (AttributeError, RuntimeError):
                        pass
                    _verify_investor_access(adapter)
                    _progress(root, status="importing_history")
                    _require_lease(api, job)
                    if mode == "new_only":
                        counts = _run_history_sync(
                            adapter, root, mode, from_date, ingestion_sink, str(login), server
                        )
                    else:
                        counts = _run_control_plane_history_import(
                            adapter,
                            root,
                            api,
                            job,
                            mode,
                            from_date,
                            str(login),
                            server,
                        )
            except Mt5Error as exc:
                raise _map_mt5_error(exc) from exc
        _record_checkpoint(
            api,
            job,
            "history_sync",
            "completed",
            mode,
            root=root,
            local_status="connected",
        )
        return {
            "imported_deals": counts["deals"],
            "imported_orders": counts["orders"],
            "imported_positions": counts.get("positions", 0),
            "history_events": counts.get("events", 0),
            "history_events_delivered": counts.get("delivered", 0),
            "history_event_duplicates": counts.get("duplicates", 0),
            "skipped_history_deals": counts.get("skipped_deals", 0),
        }

    def deprovision(job: dict) -> dict:
        cid = canonical_uuid(str(job["connection_id"]))
        layout = InstanceLayout(instances_root, cid)
        root = layout.path
        _record_checkpoint(
            api,
            job,
            "deprovision",
            "started",
            root=root if root.exists() else None,
            local_status="disconnecting",
        )
        try:
            # "ferma live sync"/"ferma history sync": no background thread survives a single job
            # in this architecture (see module docstring on job-at-a-time V1 design), so the only
            # real cleanup step is stopping the terminal process itself, idempotently -- safe to
            # call even if the instance was never fully provisioned (process.stop() is a no-op
            # when its state file is absent, and InstanceProvisioner.deprovision() is itself
            # idempotent, see test_fake_provision_deprovision_idempotent).
            process = process_factory(
                root / "state" / "terminal-process.json"
            )
            if process.stop() is not True:
                raise RuntimeError("terminal stop could not be verified")
            terminal = root / "terminal" / "terminal64.exe"
            if terminal.parent.exists() and process.cleanup_path(terminal) is not True:
                raise RuntimeError("terminal cleanup could not be verified")
            InstanceProvisioner(instances_root, secrets_root).deprovision(cid)
        except Exception as exc:
            raise DeprovisionFailed("deprovision failed") from exc
        _record_checkpoint(
            api,
            job,
            "deprovision",
            "completed",
            root=root if root.exists() else None,
            local_status="disconnected",
        )
        return {"deprovisioned": True}

    def live_sync(job: dict) -> dict:
        """Retired compatibility handler for historical queued ``live_sync`` jobs.

        New jobs of this type are no longer created or claimable. Ongoing trade/connection changes
        are handled by ``Mt5EventSupervisor`` and reboot recovery is local at service startup.
        Keeping this one-shot handler lets an old deployment be drained safely without restoring
        the former periodic chain.
        """
        cid = canonical_uuid(str(job["connection_id"]))
        try:
            root = InstanceProvisioner(instances_root, secrets_root).validate(
                cid,
                verify_code=False,
            )
        except Exception as exc:
            raise InstanceProvisionFailed(
                "provisioned instance integrity validation failed"
            ) from exc
        recorded_terminal_sha256 = _recorded_instance_terminal_sha256(root)
        _record_checkpoint(
            api,
            job,
            "live_sync",
            "started",
            root=root,
            local_status="live_sync_running",
        )
        try:
            login = int(store.read(cid, "mt5_login"))
            server = store.read(cid, "mt5_server")
            bridge_token = store.read(cid, "bridge_token")
        except Exception as exc:
            raise SecretStoreFailed("stored identity/bridge token unavailable") from exc
        if not trading_ingestion_url:
            raise SecretStoreFailed("trading_ingestion_url not configured on this agent")
        _require_lease(api, job)

        terminal = root / "terminal" / "terminal64.exe"
        state_path = root / "state" / "terminal-process.json"
        _verify_binary_pin(terminal, recorded_terminal_sha256)
        terminal_is_running = terminal.is_file() and ProcessManager.find(terminal)
        provisioner = InstanceProvisioner(instances_root, secrets_root)
        try:
            provisioner.validate_runtime_assets(cid)
        except ValueError as exc:
            # Legacy instances predate the per-instance runtime asset pin.  A
            # one-time seal is permitted only while their exact terminal is
            # already running and its immutable terminal pin has passed above.
            # The operation never overwrites an existing asset pin.
            if "asset pin missing" not in str(exc) or not terminal_is_running:
                raise InstanceProvisionFailed(
                    "provisioned managed runtime assets invalid"
                ) from exc
            try:
                provisioner.seal_runtime_assets(cid)
            except Exception as seal_exc:
                raise InstanceProvisionFailed(
                    "legacy runtime asset migration failed"
                ) from seal_exc
        if not terminal_is_running:
            try:
                provisioner.validate(
                    cid,
                    verify_code=False,
                )
            except Exception as exc:
                raise InstanceProvisionFailed(
                    "provisioned instance code integrity validation failed"
                ) from exc
            _verify_binary_pin(expert_binary, expert_sha256)
            try:
                runtime = _configure_runtime(runtime_factory(root, cid), job)
                recovery_from = new_only_recovery_from(root)
                trusted_expert_sha256 = hashlib.sha256(
                    expert_binary.read_bytes()
                ).hexdigest()
                runtime.install_expert(
                    expert_binary,
                    "new_only",
                    recovery_from,
                )
                provisioner.record_verified_managed_asset_update(
                    root,
                    cid,
                    trusted_expert_sha256,
                )
                runtime.resume(
                    login=login,
                    server=server,
                    expert_binary=expert_binary,
                    history_mode="new_only",
                    history_from=recovery_from,
                )
                provisioner.validate_runtime_assets(cid)
            except NativeMt5Error as exc:
                code = str(exc)
                if code in ("identity_mismatch", "server_identity_mismatch"):
                    raise AccountIdentityMismatch(code) from exc
                if code == "terminal_start_failed":
                    raise TerminalStartFailed(code) from exc
                raise Mt5InitializeFailed(code) from exc
            except (OSError, ValueError) as exc:
                raise InstanceProvisionFailed(
                    "managed runtime asset refresh failed"
                ) from exc
            try:
                process_factory(state_path).adopt(terminal)
            except (AttributeError, RuntimeError):
                pass

        adapter = Mql5FileMt5Adapter(root / "terminal" / "MQL5" / "Files" / "TradeJournal", cid, login, server, root / "state")
        try:
            account_info = _verify_investor_access(adapter)
            sink = TradingIngestionSink(root, trading_ingestion_url, bridge_token)
            if not sink.send_heartbeat(account_info):
                raise LiveSyncFailed("live sync heartbeat was not acknowledged")
            delivered = _run_live_sync_once(adapter, root, sink)
        except AgentError:
            raise
        except Mt5Error as exc:
            raise _map_mt5_error(exc) from exc
        except Exception as exc:
            raise LiveSyncFailed("live sync check failed") from exc
        _record_checkpoint(
            api,
            job,
            "live_sync",
            "completed",
            root=root,
            local_status="connected",
        )
        return {"live_sync_events_delivered": delivered}

    handlers: dict[str, JobHandler] = {
        "provision": provision,
        "historical_sync": historical_sync,
        "deprovision": deprovision,
        "live_sync": live_sync,
    }
    if lifecycle_coordinator is None:
        return handlers

    def _serialized(handler: JobHandler) -> JobHandler:
        def wrapped(job: dict) -> dict:
            connection_id = canonical_uuid(str(job["connection_id"]))
            with lifecycle_coordinator.connection(connection_id):
                return handler(job)

        return wrapped

    return {
        name: _serialized(handler)
        for name, handler in handlers.items()
    }


def _progress_if_exists(root: Path, **fields: Any) -> None:
    if root.exists():
        _progress(root, **fields)


def _is_protected_instance(root: Path, connection_id: str) -> bool:
    if not root.exists():
        return False
    progress_path = root / "state" / "job_progress.json"
    if not progress_path.is_file():
        # Existing state with no attributable lifecycle marker is ambiguous
        # and therefore protected from destructive automatic cleanup.
        return True
    try:
        progress = read_json(progress_path)
    except (OSError, ValueError):
        # Ambiguous published state is protected from automatic deletion.
        return True
    if progress.get("connection_id") != connection_id:
        return True
    status = progress.get("status")
    if not isinstance(status, str):
        return True
    return status not in FAILED_PROVISION_LOCAL_STATUSES


def _discard_failed_provision(
    instances_root: Path,
    secrets_root: Path,
    connection_id: str,
    process_factory: Callable[[Path], Any],
) -> None:
    root = InstanceLayout(instances_root, connection_id).path
    terminal = root / "terminal" / "terminal64.exe"
    terminal_root = terminal.parent
    state_path = root / "state" / "terminal-process.json"
    if root.exists() and (
        InstanceProvisioner._is_reparse_point(root) or not root.is_dir()
    ):
        raise ValueError("failed instance root is unsafe")
    if terminal_root.exists() and (
        InstanceProvisioner._is_reparse_point(terminal_root)
        or not terminal_root.is_dir()
    ):
        raise ValueError("failed terminal root is unsafe")
    if terminal_root.exists():
        cleaned = process_factory(state_path).cleanup_path(terminal)
        if cleaned is not True:
            raise RuntimeError("failed terminal cleanup is unverified")
    InstanceProvisioner(instances_root, secrets_root).discard_failed(
        connection_id
    )


def _verify_investor_access(adapter: Any) -> Any:
    account = adapter.account_info()
    terminal_info = adapter.terminal_info()
    if not bool(getattr(terminal_info, "connected", False)):
        raise Mt5InitializeFailed("terminal not connected")
    if bool(getattr(account, "trade_allowed", True)):
        raise InvestorAccessNotVerified("account is not read-only/investor")
    return account


def _history_time(value: object) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(value, timezone.utc).isoformat()
    if isinstance(value, str) and value:
        return value
    return datetime.now(timezone.utc).isoformat()


def _history_event(entry: dict, login: str, server: str) -> dict:
    record = dict(entry.get("record") or {})
    kind = entry.get("kind")
    ticket = record.get("position_id") or record.get("position_ticket") or record.get("ticket")
    event_time = _history_time(record.get("time", record.get("close_time")))
    direction = record.get("direction")
    if direction is None and isinstance(record.get("type"), int):
        direction = "buy" if record["type"] in (0, 2, 4, 6) else "sell"
    base = {
        "ticket": str(ticket),
        "symbol": record.get("symbol"),
        "direction": direction,
        "volume": record.get("volume", record.get("volume_current")),
        "stop_loss": record.get("sl", record.get("stop_loss")),
        "take_profit": record.get("tp", record.get("take_profit")),
        "event_time": event_time,
    }
    if kind == "orders":
        raw = {
            **base,
            "event_type": "pending_order_created",
            "price": record.get("price", record.get("price_open")),
        }
    else:
        entry_value = record.get("entry")
        normalized_entry = str(entry_value).upper() if entry_value is not None else ""
        if normalized_entry in ("0", "IN"):
            raw = {
                **base,
                "event_type": "trade_opened",
                "open_price": record.get("price", record.get("open_price")),
                "open_time": event_time,
            }
        elif normalized_entry in ("1", "2", "3", "OUT", "INOUT", "OUT_BY"):
            raw = {
                **base,
                "event_type": "trade_closed",
                "close_price": record.get("price", record.get("close_price")),
                "profit": record.get("profit"),
                "commission": record.get("commission"),
                "swap": record.get("swap"),
                "close_time": event_time,
            }
        else:
            raw = {
                **base,
                "event_type": "deal_recorded",
                "close_price": record.get("price", record.get("close_price")),
                "profit": record.get("profit"),
                "commission": record.get("commission"),
                "swap": record.get("swap"),
                "close_time": event_time,
            }
    return normalize_event(raw, login, server)


def _run_history_sync(
    adapter: Any,
    root: Path,
    mode: HistoryMode,
    from_date: "datetime | None",
    ingestion_sink: TradingIngestionSink | None = None,
    login: str | None = None,
    server: str | None = None,
) -> dict:
    dedup = PersistentDedup(root / "state" / "history-dedup.sqlite")
    local_sink = LocalEventSink(root / "data" / "history.jsonl")
    outbox = EventOutbox(str(root / "state" / "history-outbox.json"))

    def persist(entry: dict) -> None:
        local_sink(entry)
        if ingestion_sink is not None:
            if login is None or server is None:
                raise HistorySyncFailed("history ingestion identity missing")
            outbox.enqueue_many([_history_event(entry, login, server)])

    sink = _deduped_sink(dedup, persist)
    try:
        counts = HistorySync(adapter, root / "state" / "history.json", sink).run(mode, from_date)
        if ingestion_sink is not None:
            result = outbox.drain(ingestion_sink)
            dead_lettered = outbox.dead_letter_count()
            if result.pending or dead_lettered or result.dry_run:
                raise HistorySyncFailed(
                    "history delivery incomplete: "
                    f"pending={result.pending}, dead_lettered={dead_lettered}, "
                    f"dry_run={result.dry_run}"
                )
        return counts
    except Exception as exc:
        if isinstance(exc, HistorySyncFailed):
            raise
        raise HistorySyncFailed("history import failed") from exc
    finally:
        dedup.close()


def _run_control_plane_history_import(
    adapter: Any,
    root: Path,
    api: Any,
    job: dict,
    mode: HistoryMode,
    from_date: "datetime | None",
    login: str,
    server: str,
) -> dict[str, int]:
    now = datetime.now(timezone.utc)
    if mode == "all_available":
        start = datetime(1970, 1, 1, tzinfo=timezone.utc)
    elif mode == "from_date" and from_date is not None:
        start = from_date.astimezone(timezone.utc)
    else:
        raise HistorySyncFailed("full history import requires a bounded history window")

    try:
        raw_orders = adapter.history_orders(start, now)
        raw_deals = adapter.history_deals(start, now)
        orders = [
            value._asdict() if hasattr(value, "_asdict") else dict(value)
            for value in raw_orders
        ]
        deals = [
            value._asdict() if hasattr(value, "_asdict") else dict(value)
            for value in raw_deals
        ]
        events, aggregate_counts = build_historical_trade_events(
            deals, login, server
        )
        pending_events, pending_counts = build_historical_pending_order_events(
            orders, login, server
        )
        events.extend(pending_events)
        events.sort(key=lambda event: (str(event["event_time"]), str(event["event_type"])))
    except Exception as exc:
        raise HistorySyncFailed("history snapshot could not be reconstructed") from exc

    try:
        archive = build_history_archive(
            root,
            job_id=str(job["job_id"]),
            connection_id=str(job["connection_id"]),
            account_number=login,
            server=server,
            history_mode=mode,
            from_date=from_date,
            events=events,
        )
        response = deliver_history_archive(
            api,
            job_id=str(job["job_id"]),
            lease_id=str(job["lease_id"]),
            archive=archive,
            require_lease=lambda: _require_lease(api, job),
        )
        atomic_json(
            root / "state" / "history.json",
            {
                "through": now.isoformat(),
                "orders": len(raw_orders),
                "deals": len(raw_deals),
                "positions": aggregate_counts["positions"],
                "events": len(events),
                "pending_orders": pending_counts["pending_orders"],
                "pending_events": pending_counts["pending_events"],
            },
        )
    except LeaseLost:
        raise
    except Exception as exc:
        if str(exc) == "lease_lost":
            raise LeaseLost("lease_lost") from exc
        raise HistorySyncFailed("history file delivery failed") from exc

    return {
        "orders": len(raw_orders),
        "deals": len(raw_deals),
        "positions": aggregate_counts["positions"],
        "events": len(events),
        "pending_orders": pending_counts["pending_orders"],
        "pending_events": pending_counts["pending_events"],
        "delivered": int(response["inserted"]),
        "duplicates": int(response["duplicates"]),
        "skipped_deals": aggregate_counts["skipped_deals"],
        "skipped_orders": pending_counts["skipped_orders"],
    }


def _prime_live_state_after_initial_history(adapter: Any, root: Path) -> None:
    """Make the imported snapshot the live baseline without losing later events.

    Event files observed before the second snapshot are already represented by that snapshot and
    by the just-imported deal history. Files created afterwards retain a higher sequence and are
    delivered by the normal live poll.
    """
    pending_before = adapter.pending_events()
    current = adapter.snapshot()
    PersistentSnapshot(root / "state" / "live_snapshot.json").save(current)
    if pending_before:
        adapter.acknowledge_events(
            max(int(record["sequence"]) for record in pending_before)
        )


def _start_file_bridge_and_sync(
    job: dict,
    api: Any,
    root: Path,
    cid: str,
    login: int,
    server: str,
    connection_endpoint: str,
    mode: HistoryMode,
    from_date: "datetime | None",
    store: WindowsSecretStore,
    process_factory: Callable[[Path], Any],
    expert_binary: Path,
    runtime_factory: Callable[[Path, str], NativeMt5Runtime],
    trading_ingestion_url: str,
    endpoint_observer: BrokerEndpointObserver | None = None,
    runtime_configurer: Callable[[Any, dict], Any] | None = None,
) -> dict:
    """Launch MT5 once, then consume only EA-produced local files.

    No Python MT5 IPC session is created here. The password is only supplied to the protected
    startup config inside ``NativeMt5Runtime`` and is cleared before file parsing/history sync.
    """
    terminal = root / "terminal" / "terminal64.exe"
    state_path = root / "state" / "terminal-process.json"
    _ensure_no_stale_process(terminal, state_path, process_factory)
    _require_lease(api, job)
    _record_checkpoint(
        api,
        job,
        "terminal_start",
        "started",
        root=root,
        local_status="starting_native_file_bridge",
    )
    try:
        investor_password = store.read(cid, "mt5_investor_password")
    except Exception as exc:
        raise SecretStoreFailed("stored credential unavailable") from exc
    try:
        runtime = runtime_factory(root, cid)
        if runtime_configurer is not None:
            runtime = runtime_configurer(runtime, job)
        else:
            set_cancel_check = getattr(runtime, "set_cancel_check", None)
            if callable(set_cancel_check):
                set_cancel_check(job.get("_lease_guard"))
        status = runtime.start(
            login=login,
            server=server,
            connection_endpoint=connection_endpoint,
            investor_password=investor_password,
            expert_binary=expert_binary,
            history_mode=mode,
            history_from=from_date,
        )
    except NativeMt5Error as exc:
        code = str(exc)
        if code == "terminal_stop_failed":
            raise InstanceCleanupFailed(code) from exc
        if code == "investor_readonly_not_verified":
            raise InvestorAccessNotVerified(code) from exc
        if code == "investor_sync_timeout":
            raise InvestorVerificationTimeout(code) from exc
        if code == "server_identity_mismatch":
            raise ServerIdentityMismatch(code) from exc
        if code == "identity_mismatch":
            raise AccountIdentityMismatch(code) from exc
        if code in {"authorization_failed", "invalid_account"}:
            raise Mt5AuthorizationFailed(code) from exc
        if code == "terminal_start_failed":
            raise TerminalStartFailed(code) from exc
        raise Mt5InitializeFailed(code) from exc
    finally:
        investor_password = ""
        gc.collect()
    verification_observation: ObservedProcessEndpoint | None = None
    if endpoint_observer is not None:
        try:
            verification_observation = endpoint_observer(status.pid)
        except BrokerEndpointObservationError:
            # A transient or ambiguous socket must not block a successful read-only login.
            # Promotion remains fail-closed and is attempted again only when evidence is usable.
            verification_observation = None
    effective_server = (
        status.effective_server
        if isinstance(status.effective_server, str)
        and status.effective_server.strip()
        else str(status.account.get("server", "")).strip()
    )
    if not effective_server:
        effective_server = server
    if any(character in effective_server for character in "\r\n"):
        raise Mt5InitializeFailed("server_identity_invalid")
    try:
        store.write(cid, "mt5_server", effective_server)
    except Exception as exc:
        raise SecretStoreFailed(
            "effective server identity persistence failed"
        ) from exc
    _record_checkpoint(
        api,
        job,
        "terminal_start",
        "completed",
        root=root,
        local_status="terminal_started",
    )
    _record_checkpoint(
        api,
        job,
        "bridge_activation",
        "completed",
        "mql5_file_bridge",
        root=root,
        local_status="bridge_ready",
    )

    try:
        process_factory(state_path).adopt(terminal)
    except (AttributeError, RuntimeError):
        # The runtime owns an already-started, exact terminal path. Adoption is persistence for
        # deprovision/recovery; a test double may intentionally omit real OS process discovery.
        pass
    adapter = Mql5FileMt5Adapter(
        status.files_path,
        cid,
        login,
        effective_server,
        root / "state",
    )
    ingestion_sink = None
    if trading_ingestion_url:
        try:
            ingestion_sink = TradingIngestionSink(
                root, trading_ingestion_url, store.read(cid, "bridge_token")
            )
        except Exception as exc:
            raise SecretStoreFailed("provision ingestion token unavailable") from exc
    try:
        _record_checkpoint(
            api,
            job,
            "investor_verification",
            "started",
            root=root,
            local_status="verifying_read_only",
        )
        _verify_investor_access(adapter)
        _record_checkpoint(
            api,
            job,
            "investor_verification",
            "completed",
            root=root,
            local_status="read_only_verified",
        )
        _record_checkpoint(
            api,
            job,
            "history_sync",
            "started",
            mode,
            root=root,
            local_status="importing_history",
        )
        _require_lease(api, job)
        if mode == "new_only":
            counts = _run_history_sync(adapter, root, mode, from_date)
        else:
            counts = _run_control_plane_history_import(
                adapter,
                root,
                api,
                job,
                mode,
                from_date,
                str(login),
                effective_server,
            )
            _prime_live_state_after_initial_history(adapter, root)
        _record_checkpoint(
            api,
            job,
            "history_sync",
            "completed",
            mode,
            root=root,
            local_status="history_ready",
        )
        _record_checkpoint(
            api,
            job,
            "sync_activation",
            "started",
            root=root,
            local_status="starting_live_sync",
        )
        _require_lease(api, job)
        delivered = _run_live_sync_once(adapter, root, ingestion_sink)
        _record_checkpoint(
            api,
            job,
            "sync_activation",
            "completed",
            root=root,
            local_status="connected",
        )
    except AgentError:
        raise
    except Mt5Error as exc:
        raise _map_mt5_error(exc) from exc
    except Exception as exc:
        raise LiveSyncFailed("file bridge sync failed") from exc
    try:
        InstanceProvisioner(root.parent, store.root).seal_runtime_assets(cid)
    except Exception as exc:
        raise InstanceProvisionFailed("managed runtime asset sealing failed") from exc
    return {
        "imported_deals": counts["deals"],
        "imported_orders": counts["orders"],
        "live_sync_started": True,
        "live_sync_events_delivered": delivered,
        "file_bridge": "mql5-local-json",
        "_verification_pid": status.pid,
        "_verification_observation": verification_observation,
        "effective_server_name": effective_server,
    }


def _authenticate_and_sync(
    job: dict,
    api: Any,
    root: Path,
    cid: str,
    login: int,
    server: str,
    mode: HistoryMode,
    from_date: "datetime | None",
    store: WindowsSecretStore,
    adapter_factory: Callable[..., Any],
    process_factory: Callable[[Path], Any],
    trading_ingestion_url: str,
) -> dict:
    terminal = root / "terminal" / "terminal64.exe"
    state_path = root / "state" / "terminal-process.json"
    _require_lease(api, job)
    _record_checkpoint(
        api,
        job,
        "terminal_start",
        "started",
        root=root,
        local_status="authenticating",
    )
    try:
        investor_password = store.read(cid, "mt5_investor_password")
    except Exception as exc:
        raise SecretStoreFailed("stored credential unavailable") from exc

    _ensure_no_stale_process(terminal, state_path, process_factory)
    process = process_factory(state_path)
    adapter = adapter_factory(terminal, login, server)
    ingestion_sink = None
    if trading_ingestion_url:
        try:
            ingestion_sink = TradingIngestionSink(
                root, trading_ingestion_url, store.read(cid, "bridge_token")
            )
        except Exception as exc:
            raise SecretStoreFailed("provision ingestion token unavailable") from exc
    try:
        with adapter.session(investor_password):
            investor_password = ""
            gc.collect()
            try:
                process.adopt(terminal)
            except (AttributeError, RuntimeError):
                pass
            _record_checkpoint(
                api,
                job,
                "terminal_start",
                "completed",
                root=root,
                local_status="terminal_started",
            )
            _record_checkpoint(
                api,
                job,
                "investor_verification",
                "started",
                root=root,
                local_status="verifying_read_only",
            )
            _verify_investor_access(adapter)
            _record_checkpoint(
                api,
                job,
                "investor_verification",
                "completed",
                root=root,
                local_status="read_only_verified",
            )
            _record_checkpoint(
                api,
                job,
                "history_sync",
                "started",
                mode,
                root=root,
                local_status="importing_history",
            )
            _require_lease(api, job)
            counts = _run_history_sync(
                adapter, root, mode, from_date, ingestion_sink, str(login), server
            )
            _record_checkpoint(
                api,
                job,
                "history_sync",
                "completed",
                mode,
                root=root,
                local_status="history_ready",
            )
            _record_checkpoint(
                api,
                job,
                "sync_activation",
                "started",
                root=root,
                local_status="starting_live_sync",
            )
            _require_lease(api, job)
            try:
                delivered = _run_live_sync_once(adapter, root, ingestion_sink)
            except Exception as exc:
                raise LiveSyncFailed("live sync check failed") from exc
            _record_checkpoint(
                api,
                job,
                "sync_activation",
                "completed",
                root=root,
                local_status="connected",
            )
    except Mt5Error as exc:
        raise _map_mt5_error(exc) from exc

    return {
        "imported_deals": counts["deals"],
        "imported_orders": counts["orders"],
        "live_sync_started": True,
        "live_sync_events_delivered": delivered,
        "_verification_pid": getattr(process, "pid", 0),
    }


def _run_live_sync_once(adapter: Any, root: Path, sink: Callable[[dict], None] | None = None) -> int:
    """A single poll_once(). Historically this ran exactly once per connection, glued to the tail
    of provision()/historical_sync() (this architecture runs one job at a time, see
    docs/windows/architecture.md). Ongoing monitoring is now driven by filesystem notifications in
    ``Mt5EventSupervisor``; this helper remains for provisioning and the retired compatibility
    handler, which can pass an HTTP-forwarding sink instead of the local-only default."""
    dedup = PersistentDedup(root / "state" / "live-dedup.sqlite")
    try:
        live = LiveSync(
            adapter,
            PersistentSnapshot(root / "state" / "live_snapshot.json"),
            dedup,
            sink or LocalEventSink(root / "data" / "live.jsonl"),
            outbox=EventOutbox(str(root / "state" / "live-outbox.json")),
        )
        return live.poll_once()
    finally:
        dedup.close()
