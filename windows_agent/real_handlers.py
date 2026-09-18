from __future__ import annotations

import gc
import hashlib
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional
from uuid import uuid4

from worker.atomic_file import durable_replace
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
from .credential_envelope import decrypt_credential_envelope
from .event_supervisor import connection_sync_lock
from .job_runner import LeaseLost
from .provisioning.instance_layout import InstanceLayout
from .provisioning.mt5_instance import InstanceProvisioner
from .provisioning.mt5_instance_pool import Mt5InstancePool
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
from .worker.history_archive import (
    build_history_document,
    history_document_bytes,
    history_document_sha256,
    load_or_create_history_document,
    validate_history_document,
)
from .worker.history_balance import build_balance_backfill_report
from .worker.history_sync import HistoryMode, HistorySync
from .worker.live_sync import LiveSync
from .worker.local_event_sink import LocalEventSink
from .worker.mql5_file_adapter import Mql5FileMt5Adapter
from .worker.native_mt5_runtime import NativeMt5Error, NativeMt5Runtime
from .worker.trading_ingestion_sink import TradingIngestionSink

logger = logging.getLogger(__name__)

SERVER_PATTERN = re.compile(r"[A-Za-z0-9._ -]{1,128}")
BROKER_PATTERN = re.compile(r"[A-Za-z0-9 .,&'()+_/-]{1,128}")
DEFAULT_EXPERT_BINARY = Path(r"C:\TradeJournal\mt5-template\MQL5\Experts\TradeJournal\TradeJournalBridge.ex5")
FAILED_PROVISION_LOCAL_STATUSES = frozenset(
    {
        "censusing_broker",
        "broker_censused",
        "broker_discovery_failed",
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
        return tuple(candidate.server_address for candidate in result.candidates)
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
    )


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
        if not process_factory(state_path).cleanup_path(terminal):
            raise TerminalStartFailed("stale terminal cleanup failed")


@dataclass(frozen=True)
class StartupReconciliation:
    """Sanitized result of reconciling isolated terminals after an Agent restart."""

    adopted: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    terminated: tuple[str, ...] = ()
    blocked: tuple[str, ...] = ()


def reconcile_startup_instances(
    instances_root: Path,
    secrets_root: Path,
    process_factory: Callable[[Path], Any] = ProcessManager,
) -> StartupReconciliation:
    """Adopt valid running terminals and clean only demonstrably unsafe or ambiguous ones.

    Restarting the Windows service must not interrupt a healthy MT5 process.  Every terminal is
    isolated under its connection UUID, so one exact process plus a valid published instance is
    safe to adopt into fresh local PID state.  A missing process is deliberately left for the
    recurring ``live_sync`` job, which performs the passwordless reboot recovery.  Invalid
    publications and duplicate exact-path processes remain fail-closed and are terminated.
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
) -> dict[str, JobHandler]:
    """Real provision/historical_sync/deprovision handlers.

    Jobs are claimed by the managed JobRunner/control plane. Without an injected
    ``adapter_factory`` the Windows-native MQL5 file path is always used; the injection point
    exists only for isolated tests.
    """

    store = WindowsSecretStore(secrets_root)

    def _provision_instance(connection_id: str) -> Path:
        if instance_pool is not None:
            pooled_root = instance_pool.claim(connection_id)
            if pooled_root is not None:
                return pooled_root
        return InstanceProvisioner(
            instances_root,
            secrets_root,
        ).provision(
            connection_id,
            source_terminal,
            terminal_sha256,
        )

    def _ensure_current_managed_expert(
        job: dict,
        cid: str,
        root: Path,
        login: int,
        server: str,
        expected_terminal_sha256: str | None,
        history_mode: HistoryMode = "new_only",
    ) -> tuple[Path, Path]:
        """Rotate a stale per-instance EA before any managed sync reads its files.

        Event-driven accounts no longer receive recurring ``live_sync`` jobs.  History sync is
        therefore also the supported rollout boundary for an already provisioned terminal: it
        stops only the affected instance, atomically reseals the managed assets, and resumes the
        same authenticated terminal without reading the investor password.
        """
        terminal = root / "terminal" / "terminal64.exe"
        state_path = root / "state" / "terminal-process.json"
        _verify_binary_pin(terminal, expected_terminal_sha256)
        _verify_binary_pin(expert_binary, expert_sha256)
        terminal_is_running = terminal.is_file() and ProcessManager.find(terminal)
        provisioner = InstanceProvisioner(instances_root, secrets_root)
        try:
            provisioner.validate_runtime_assets(cid)
        except ValueError as exc:
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

        instance_expert = (
            root
            / "terminal"
            / "MQL5"
            / "Experts"
            / "TradeJournal"
            / "TradeJournalBridge.ex5"
        )
        expert_needs_rotation = False
        if expert_sha256 is not None:
            try:
                current_expert_sha256 = provisioner._sha256(instance_expert)
            except OSError as exc:
                raise InstanceProvisionFailed(
                    "provisioned managed expert unavailable"
                ) from exc
            expert_needs_rotation = current_expert_sha256 != expert_sha256

        history_mode_path = (
            root / "terminal" / "MQL5" / "Files" / "TradeJournal" / "history_mode"
        )
        try:
            active_history_mode = history_mode_path.read_text(encoding="utf-8").strip()
        except OSError:
            active_history_mode = ""
        restart_required = expert_needs_rotation or active_history_mode != history_mode
        if restart_required:
            try:
                process = process_factory(state_path)
                if terminal_is_running:
                    process.stop()
                    if not process.cleanup_path(terminal):
                        raise RuntimeError(
                            "instance process survived managed runtime restart"
                        )
                if expert_needs_rotation:
                    provisioner.rotate_managed_expert(
                        cid,
                        expert_binary,
                        expert_sha256,
                    )
                terminal_is_running = False
            except Exception as exc:
                message = (
                    "managed expert rotation failed"
                    if expert_needs_rotation
                    else "managed history mode restart failed"
                )
                raise InstanceProvisionFailed(message) from exc

        if not terminal_is_running:
            try:
                provisioner.validate(cid, verify_code=False)
            except Exception as exc:
                raise InstanceProvisionFailed(
                    "provisioned instance code integrity validation failed"
                ) from exc
            try:
                runtime = runtime_factory(root, cid)
                set_cancel_check = getattr(runtime, "set_cancel_check", None)
                if callable(set_cancel_check):
                    set_cancel_check(job.get("_lease_guard"))
                runtime.resume(
                    login=login,
                    server=server,
                    expert_binary=expert_binary,
                    history_mode=history_mode,
                )
            except NativeMt5Error as exc:
                code = str(exc)
                if code in ("identity_mismatch", "server_identity_mismatch"):
                    raise AccountIdentityMismatch(code) from exc
                if code == "terminal_start_failed":
                    raise TerminalStartFailed(code) from exc
                raise Mt5InitializeFailed(code) from exc
            try:
                process_factory(state_path).adopt(terminal)
            except (AttributeError, RuntimeError):
                pass

        return terminal, state_path

    def _provision_once(job: dict) -> dict:
        cid = canonical_uuid(str(job["connection_id"]))
        payload = job.get("payload") or {}
        _require_lease(api, job)
        _verify_binary_pin(source_terminal, terminal_sha256)
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
                    mtapi_candidate_endpoints = tuple(
                        candidate.server_address for candidate in mtapi_result.candidates
                    )
                    mtapi_candidates_by_endpoint = {
                        candidate.server_address: candidate
                        for candidate in mtapi_result.candidates
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
                        )
                else:
                    result = _start_file_bridge_and_sync(
                        job, api, root, cid, login, server, connection_endpoint,
                        mode, from_date, store, process_factory, expert_binary,
                        runtime_factory, trading_ingestion_url,
                        endpoint_observer=endpoint_observer,
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
        # Withdraw an existing account from the background supervisor before any reprovision
        # work begins.  It becomes visible again only when the full history/live handoff writes
        # `connected`; an interrupted attempt therefore fails closed across service restarts.
        if root.exists() and protected_at_start:
            _progress(
                root,
                status="provisioning",
                connection_id=cid,
            )
        stale_secrets = secrets_root / cid
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
        except Exception as original_exc:
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
                cid, terminal_sha256
            )
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
        _verify_binary_pin(terminal, terminal_sha256)
        state_path = root / "state" / "terminal-process.json"
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
            # The default adapter consumes only the EA's files.  A later history job must
            # therefore not read, decrypt or retain the investor password at all.
            _require_lease(api, job)
            with connection_sync_lock(cid):
                _ensure_current_managed_expert(
                    job,
                    cid,
                    root,
                    login,
                    server,
                    terminal_sha256,
                    history_mode=mode,
                )
                try:
                    _progress(root, status="importing_history")
                    adapter = Mql5FileMt5Adapter(root / "terminal" / "MQL5" / "Files" / "TradeJournal", cid, login, server, root / "state")
                    _verify_investor_access(adapter)
                    # Full history is delivered only through the current job's lease-bound
                    # archive route. The live supervisor stays outside this locked window.
                    counts = _run_history_sync(
                        adapter,
                        root,
                        mode,
                        from_date,
                        api,
                        job,
                        cid,
                        str(login),
                        server,
                    )
                    if mode != "new_only":
                        _prepare_history_to_live_handoff(adapter, root)
                finally:
                    if mode != "new_only":
                        runtime = runtime_factory(root, cid)
                        set_cancel_check = getattr(runtime, "set_cancel_check", None)
                        if callable(set_cancel_check):
                            set_cancel_check(job.get("_lease_guard"))
                        _require_lease(api, job)
                        try:
                            runtime.switch_to_new_only()
                        except NativeMt5Error as exc:
                            raise Mt5InitializeFailed(str(exc)) from exc
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
                    counts = _run_history_sync(
                        adapter,
                        root,
                        mode,
                        from_date,
                        api,
                        job,
                        cid,
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
        return {"imported_deals": counts["deals"], "imported_orders": counts["orders"]}

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
            process = process_factory(root / "state" / "terminal-process.json")
            process.stop()
            terminal = root / "terminal" / "terminal64.exe"
            if root.exists() and not process.cleanup_path(terminal):
                raise RuntimeError("instance processes survived cleanup")
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
        """Recurring job (self-chained server-side, see transition_mt5_provisioning_job in
        20260720080000_mt5_managed_live_sync.sql): reuses the terminal already left running by
        provision() -- no re-launch, no re-login, just cheap local file reads -- unless the
        process has actually died (crash/host reboot), in which case it self-heals by relaunching
        once before giving up. Every cycle also sends a liveness heartbeat regardless of whether
        any new trade was detected, so trading_connections.last_seen_at/status stay fresh in real
        time even on a perfectly quiet account."""
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

        terminal, state_path = _ensure_current_managed_expert(
            job,
            cid,
            root,
            login,
            server,
            recorded_terminal_sha256,
        )

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

    return {
        "provision": provision,
        "historical_sync": historical_sync,
        "deprovision": deprovision,
        "live_sync": live_sync,
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
        "native_deal_ticket": record.get("ticket"),
        "time_msc": record.get("time_msc"),
        "time_basis": "broker_server_unresolved",
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
        projected_type = record.get("history_event_type")
        if projected_type == "trade_opened" or (
            projected_type is None and normalized_entry in ("0", "IN")
        ):
            raw = {
                **base,
                "event_type": "trade_opened",
                "open_price": record.get("price", record.get("open_price")),
                "profit": record.get("profit"),
                "commission": record.get("commission"),
                "fee": record.get("fee"),
                "swap": record.get("swap"),
                "balance_before_open": record.get("balance_before_open"),
                "open_time": event_time,
                "commission": record.get("commission"),
                "swap": record.get("swap"),
            }
        elif projected_type == "trade_volume_changed":
            raw = {
                **base,
                "event_type": "trade_volume_changed",
                "previous_volume": record.get("previous_volume"),
                "partial_close": False,
                "open_price": record.get("open_price"),
                "profit": record.get("profit"),
                "commission": record.get("commission"),
                "fee": record.get("fee"),
                "swap": record.get("swap"),
            }
        elif projected_type in ("trade_closed", "trade_partial_closed") or (
            projected_type is None
            and normalized_entry in ("1", "2", "3", "OUT", "INOUT", "OUT_BY")
        ):
            raw = {
                **base,
                "event_type": projected_type or "trade_closed",
                "close_price": record.get("price", record.get("close_price")),
                "profit": record.get("profit"),
                "commission": record.get("commission"),
                "fee": record.get("fee"),
                "total_commission": record.get("total_commission"),
                "commission_complete": record.get("commission_complete"),
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
                "fee": record.get("fee"),
                "swap": record.get("swap"),
                "close_time": event_time,
            }
    return normalize_event(raw, login, server)


def _rematerialize_canonical_history_stage(path: Path, document: dict[str, Any]) -> None:
    """Repair a semantically verified legacy stage from its committed pending payload."""

    try:
        persisted = path.read_bytes()
    except OSError as exc:
        raise HistorySyncFailed("history staged archive unavailable") from exc
    if persisted != history_document_bytes(document):
        # The pending payload was durably committed before the stage and is authoritative.  Keep
        # the SHA check strict; replace legacy CRLF/non-canonical bytes, then verify normally.
        atomic_json(path, document)


def _run_history_sync(
    adapter: Any,
    root: Path,
    mode: HistoryMode,
    from_date: "datetime | None",
    api: Any | None = None,
    job: dict | None = None,
    connection_id: str | None = None,
    login: str | None = None,
    server: str | None = None,
) -> dict:
    if mode != "new_only":
        if api is None or job is None or connection_id is None or login is None or server is None:
            raise HistorySyncFailed("lease-bound history delivery context missing")
        try:
            resumed = _resume_committed_history_delivery(
                adapter,
                root,
                mode=mode,
                from_date=from_date,
                api=api,
                job=job,
                connection_id=connection_id,
                login=login,
                server=server,
            )
        except Exception as exc:
            if isinstance(exc, (HistorySyncFailed, LeaseLost)):
                raise
            raise HistorySyncFailed("history import failed") from exc
        if resumed is not None:
            return resumed

    dedup = PersistentDedup(root / "state" / "history-dedup.sqlite")
    local_sink = LocalEventSink(root / "data" / "history.jsonl")
    accounting_sink = LocalEventSink(root / "data" / "history-accounting.jsonl")
    history_events: list[dict[str, Any]] = []
    history_event_ids: set[str] = set()
    projected_deals: list[dict[str, Any]] = []
    local_persist = _deduped_sink(dedup, local_sink)
    local_accounting_persist = _deduped_sink(dedup, accounting_sink)

    def persist(entry: dict) -> None:
        local_persist(entry)
        record = dict(entry.get("record") or {})
        if entry.get("kind") == "deals":
            projected_deals.append(record)
        # The history endpoint accepts journal trade events, not the raw MT5 order
        # stream. Only rows explicitly approved by the ledger projector may leave
        # the instance; ambiguous reversals remain in the local audit artifacts.
        # Scale-ins are first-class immutable events because their native deal
        # identity and opening costs are required for exact journal economics.
        if (
            entry.get("kind") == "deals"
            and record.get("project_as_trade") is True
            and login is not None
            and server is not None
        ):
            event = _history_event(entry, login, server)
            event_id = str(event.get("event_id", ""))
            if event_id not in history_event_ids:
                history_event_ids.add(event_id)
                history_events.append(event)

    try:
        counts = HistorySync(
            adapter,
            root / "state" / "history.json",
            persist,
            local_accounting_persist,
        ).run(mode, from_date)
        anchor_reader = getattr(adapter, "history_anchor", None)
        anchor = anchor_reader() if callable(anchor_reader) else None
        report_rows_reader = getattr(adapter, "history_balance_rows", None)
        report_rows = (
            list(report_rows_reader())
            if callable(report_rows_reader)
            else projected_deals
        )
        if login is not None and server is not None and connection_id is not None:
            report = build_balance_backfill_report(
                report_rows,
                connection_id=connection_id,
                account_number=login,
                server=server,
                anchor=anchor,
            )
            atomic_json(root / "data" / "history-balance-backfill.json", report)
        if mode != "new_only":
            if api is None or job is None or connection_id is None or login is None or server is None:
                raise HistorySyncFailed("lease-bound history delivery context missing")
            archive_key = hashlib.sha256(str(job["job_id"]).encode("utf-8")).hexdigest()[:16]
            archive_path = root / "state" / f"history-import-{archive_key}.json"
            pending_path = root / "state" / "history-handoff-pending.json"
            job_id = str(job["job_id"])
            archive_preexisting = archive_path.is_file()
            if archive_preexisting:
                document = load_or_create_history_document(
                    archive_path,
                    job_id=job_id,
                    connection_id=connection_id,
                    account_number=login,
                    server=server,
                    history_mode=mode,
                    from_date=from_date,
                    events=history_events,
                )
                _persist_history_handoff_artifact(
                    adapter,
                    root,
                    job_id=job_id,
                    connection_id=connection_id,
                    archive_path=archive_path,
                    archive_preexisting=True,
                    document=document,
                )
            else:
                # The pending bundle is the first durable commit: it contains both the exact
                # document and the frozen history/live boundary. Only after that commit do we
                # materialize staging and promote it to the immutable uploader name. Therefore
                # no crash point can leave archive bytes without the baseline they require.
                staged_archive_path = archive_path.with_name(
                    f".{archive_path.name}.staged"
                )
                staged_preexisting = staged_archive_path.is_file()
                pending = read_json(pending_path, {})
                pending_for_job = (
                    pending.get("job_id") == job_id
                    and pending.get("connection_id") == connection_id
                    and pending.get("history_document") == archive_path.name
                )
                if pending_for_job:
                    document = pending.get("history_document_payload")
                    if not isinstance(document, dict):
                        raise HistorySyncFailed(
                            "history handoff prepared archive unavailable"
                        )
                    validate_history_document(
                        document,
                        job_id=job_id,
                        connection_id=connection_id,
                        account_number=login,
                        server=server,
                        history_mode=mode,
                        from_date=from_date,
                    )
                    if staged_preexisting:
                        staged_document = load_or_create_history_document(
                            staged_archive_path,
                            job_id=job_id,
                            connection_id=connection_id,
                            account_number=login,
                            server=server,
                            history_mode=mode,
                            from_date=from_date,
                            events=(),
                        )
                        if staged_document != document:
                            raise HistorySyncFailed(
                                "history handoff staged archive mismatched"
                            )
                        _rematerialize_canonical_history_stage(
                            staged_archive_path, document
                        )
                    handoff_persisted = _persist_history_handoff_artifact(
                        adapter,
                        root,
                        job_id=job_id,
                        connection_id=connection_id,
                        archive_path=archive_path,
                        archive_content_path=(
                            staged_archive_path if staged_preexisting else None
                        ),
                        archive_preexisting=True,
                        document=document,
                    )
                    if not handoff_persisted:
                        raise HistorySyncFailed(
                            "history handoff adapter support changed"
                        )
                else:
                    if staged_preexisting:
                        # Stage-only is not a committed boundary. It may come from the old
                        # stage-first protocol; replace it together with a newly captured
                        # pending bundle. Once pending exists, retries never recapture.
                        staged_preexisting = False
                    document = build_history_document(
                        job_id=job_id,
                        connection_id=connection_id,
                        account_number=login,
                        server=server,
                        history_mode=mode,
                        from_date=from_date,
                        events=history_events,
                    )
                    handoff_persisted = _persist_history_handoff_artifact(
                        adapter,
                        root,
                        job_id=job_id,
                        connection_id=connection_id,
                        archive_path=archive_path,
                        archive_preexisting=False,
                        document=document,
                        history_counts=counts,
                    )
                if not staged_preexisting:
                    atomic_json(staged_archive_path, document)
                try:
                    staged_digest = hashlib.sha256(
                        staged_archive_path.read_bytes()
                    ).hexdigest()
                except OSError as exc:
                    raise HistorySyncFailed(
                        "history staged archive unavailable"
                    ) from exc
                if handoff_persisted:
                    committed_pending = read_json(pending_path, {})
                    expected_digest = committed_pending.get(
                        "history_document_sha256"
                    )
                    if (
                        committed_pending.get("job_id") != job_id
                        or committed_pending.get("connection_id") != connection_id
                        or not isinstance(expected_digest, str)
                        or staged_digest != expected_digest
                    ):
                        raise HistorySyncFailed("history staged archive digest mismatch")
                elif staged_digest != history_document_sha256(document):
                    raise HistorySyncFailed("history staged archive digest mismatch")
                try:
                    durable_replace(staged_archive_path, archive_path)
                except OSError as exc:
                    raise HistorySyncFailed("history archive publication failed") from exc
            _deliver_history_document(api, job, document)
        return counts
    except Exception as exc:
        if isinstance(exc, (HistorySyncFailed, LeaseLost)):
            raise
        raise HistorySyncFailed("history import failed") from exc
    finally:
        dedup.close()


def _history_document_deal_tickets(document: dict[str, Any]) -> list[str]:
    """Return the exact native deal membership committed by one history document."""

    trades = document.get("trades")
    if not isinstance(trades, list):
        raise HistorySyncFailed("history archive trades invalid")
    tickets: set[str] = set()
    for group in trades:
        if not isinstance(group, dict) or not isinstance(group.get("events"), list):
            raise HistorySyncFailed("history archive events invalid")
        for event in group["events"]:
            if not isinstance(event, dict):
                raise HistorySyncFailed("history archive event invalid")
            ticket = event.get("native_deal_ticket")
            if ticket is None:
                raise HistorySyncFailed("history archive native deal identity missing")
            ticket_text = str(ticket)
            if not re.fullmatch(r"[0-9]{1,32}", ticket_text):
                raise HistorySyncFailed("history archive native deal identity invalid")
            tickets.add(ticket_text)
    return sorted(tickets, key=lambda value: (len(value), value))


def _history_document_event_count(document: dict[str, Any]) -> int:
    trades = document.get("trades")
    if not isinstance(trades, list):
        raise HistorySyncFailed("history archive trades invalid")
    count = 0
    for group in trades:
        if not isinstance(group, dict) or not isinstance(group.get("events"), list):
            raise HistorySyncFailed("history archive events invalid")
        if any(not isinstance(event, dict) for event in group["events"]):
            raise HistorySyncFailed("history archive event invalid")
        count += len(group["events"])
    return count


def _validated_history_counts(
    value: object, document: dict[str, Any]
) -> dict[str, int]:
    event_count = _history_document_event_count(document)
    if value is None:
        # Compatibility for injected/non-file adapters and direct helper tests. Production file
        # handoffs always persist the exact HistorySync counters in the committed bundle.
        return {"orders": 0, "deals": event_count, "accounting_deals": 0}
    if (
        not isinstance(value, dict)
        or set(value) != {"orders", "deals", "accounting_deals"}
        or any(
            not isinstance(value.get(field), int)
            or isinstance(value.get(field), bool)
            or value[field] < 0
            for field in ("orders", "deals", "accounting_deals")
        )
        or value["deals"] != event_count
    ):
        raise HistorySyncFailed("history handoff counters invalid")
    return {
        "orders": value["orders"],
        "deals": value["deals"],
        "accounting_deals": value["accounting_deals"],
    }


def _deliver_history_document(api: Any, job: dict, document: dict[str, Any]) -> None:
    importer = getattr(api, "import_history_file", None)
    if not callable(importer):
        raise HistorySyncFailed("lease-bound history route unavailable")
    result = importer(str(job["job_id"]), str(job["lease_id"]), document)
    if not isinstance(result, dict):
        raise HistorySyncFailed("history archive acknowledgement invalid")
    if result.get("error_code") == "lease_lost":
        raise LeaseLost("history import lease is lost")
    if result.get("accepted") != _history_document_event_count(document):
        raise HistorySyncFailed("history archive acknowledgement mismatch")


def _resume_committed_history_delivery(
    adapter: Any,
    root: Path,
    *,
    mode: HistoryMode,
    from_date: "datetime | None",
    api: Any,
    job: dict,
    connection_id: str,
    login: str,
    server: str,
) -> dict[str, int] | None:
    """Finish an already committed handoff without reading mutable MT5 history again."""

    job_id = str(job["job_id"])
    archive_key = hashlib.sha256(job_id.encode("utf-8")).hexdigest()[:16]
    archive_path = root / "state" / f"history-import-{archive_key}.json"
    staged_archive_path = archive_path.with_name(f".{archive_path.name}.staged")
    pending_path = root / "state" / "history-handoff-pending.json"
    pending = read_json(pending_path, {})
    pending_for_job = (
        isinstance(pending, dict)
        and pending.get("job_id") == job_id
        and pending.get("connection_id") == connection_id
        and pending.get("history_document") == archive_path.name
    )

    if not archive_path.is_file() and not pending_for_job:
        # A stage without its pending commit is deliberately not recoverable: the snapshot and
        # checkpoint that belonged to those bytes were never made durable. The caller will take
        # one new coherent boundary and overwrite that uncommitted stage.
        return None

    if archive_path.is_file():
        document = load_or_create_history_document(
            archive_path,
            job_id=job_id,
            connection_id=connection_id,
            account_number=login,
            server=server,
            history_mode=mode,
            from_date=from_date,
            events=(),
        )
        handoff_persisted = _persist_history_handoff_artifact(
            adapter,
            root,
            job_id=job_id,
            connection_id=connection_id,
            archive_path=archive_path,
            archive_preexisting=True,
            document=document,
        )
        counts = _validated_history_counts(
            pending.get("history_counts") if handoff_persisted else None,
            document,
        )
    else:
        document = pending.get("history_document_payload")
        if not isinstance(document, dict):
            raise HistorySyncFailed("history handoff prepared archive unavailable")
        validate_history_document(
            document,
            job_id=job_id,
            connection_id=connection_id,
            account_number=login,
            server=server,
            history_mode=mode,
            from_date=from_date,
        )
        staged_preexisting = staged_archive_path.is_file()
        if staged_preexisting:
            staged_document = load_or_create_history_document(
                staged_archive_path,
                job_id=job_id,
                connection_id=connection_id,
                account_number=login,
                server=server,
                history_mode=mode,
                from_date=from_date,
                events=(),
            )
            if staged_document != document:
                raise HistorySyncFailed("history handoff staged archive mismatched")
            _rematerialize_canonical_history_stage(staged_archive_path, document)
        if not _persist_history_handoff_artifact(
            adapter,
            root,
            job_id=job_id,
            connection_id=connection_id,
            archive_path=archive_path,
            archive_content_path=(
                staged_archive_path if staged_preexisting else None
            ),
            archive_preexisting=True,
            document=document,
        ):
            raise HistorySyncFailed("history handoff adapter support changed")
        counts = _validated_history_counts(pending.get("history_counts"), document)
        if not staged_preexisting:
            atomic_json(staged_archive_path, document)
        try:
            staged_digest = hashlib.sha256(staged_archive_path.read_bytes()).hexdigest()
        except OSError as exc:
            raise HistorySyncFailed("history staged archive unavailable") from exc
        if staged_digest != pending.get("history_document_sha256"):
            raise HistorySyncFailed("history staged archive digest mismatch")
        try:
            durable_replace(staged_archive_path, archive_path)
        except OSError as exc:
            raise HistorySyncFailed("history archive publication failed") from exc

    _deliver_history_document(api, job, document)
    return counts


def _persist_history_handoff_artifact(
    adapter: Any,
    root: Path,
    *,
    job_id: str,
    connection_id: str,
    archive_path: Path,
    archive_content_path: Path | None = None,
    archive_preexisting: bool,
    document: dict[str, Any],
    history_counts: dict[str, int] | None = None,
) -> bool:
    """Bind the frozen position baseline to the immutable uploaded history bytes.

    The file adapter exposes one atomic/frozen history bundle. Other injected adapters do not
    participate in the file-bridge handoff, so they intentionally keep their legacy flow.
    Once an archive exists, its original handoff artifact is mandatory: recapturing a newer
    snapshot during retry could acknowledge or suppress deals that were never in that archive.
    """

    snapshot_reader = getattr(adapter, "snapshot", None)
    checkpoint_reader = getattr(adapter, "checkpoint", None)
    acknowledge = getattr(adapter, "acknowledge_events", None)
    supports_file_handoff = all(
        callable(value) for value in (snapshot_reader, checkpoint_reader, acknowledge)
    )
    if not supports_file_handoff:
        return False

    digest_path = archive_content_path
    if digest_path is None and archive_path.is_file():
        digest_path = archive_path
    if digest_path is None:
        archive_digest = history_document_sha256(document)
    else:
        try:
            archive_digest = hashlib.sha256(
                digest_path.read_bytes()
            ).hexdigest()
        except OSError as exc:
            raise HistorySyncFailed("history archive digest unavailable") from exc
    pending_path = root / "state" / "history-handoff-pending.json"
    if archive_preexisting:
        artifact = read_json(pending_path, {})
        expected_archive_name = archive_path.name
        artifact_counts = _validated_history_counts(
            artifact.get("history_counts"), document
        )
        expected_counts = (
            _validated_history_counts(history_counts, document)
            if history_counts is not None
            else artifact_counts
        )
        if (
            artifact.get("schema_version") != 1
            or artifact.get("job_id") != job_id
            or artifact.get("connection_id") != connection_id
            or artifact.get("history_document") != expected_archive_name
            or artifact.get("history_document_sha256") != archive_digest
            or "history_counts" not in artifact
            or artifact_counts != expected_counts
            or (
                artifact.get("history_document_payload") is not None
                and artifact.get("history_document_payload") != document
            )
        ):
            raise HistorySyncFailed("history handoff artifact missing or mismatched")
        return True

    normalized_counts = _validated_history_counts(history_counts, document)
    snapshot = snapshot_reader()
    checkpoint = checkpoint_reader()
    sequence = checkpoint.get("sequence") if isinstance(checkpoint, dict) else None
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
        raise HistorySyncFailed("history handoff checkpoint invalid")
    if not isinstance(snapshot, dict):
        raise HistorySyncFailed("history handoff snapshot invalid")
    positions = snapshot.get("positions")
    orders = snapshot.get("orders")
    deals = snapshot.get("deals")
    if not all(isinstance(value, dict) for value in (positions, orders, deals)):
        raise HistorySyncFailed("history handoff snapshot invalid")

    imported_deal_tickets = _history_document_deal_tickets(document)
    anchored_deal_tickets = sorted(
        (str(ticket) for ticket in deals), key=lambda value: (len(value), value)
    )
    if any(
        not re.fullmatch(r"[0-9]{1,32}", ticket)
        for ticket in anchored_deal_tickets
    ):
        raise HistorySyncFailed("history handoff ledger identity invalid")
    if not set(imported_deal_tickets).issubset(set(anchored_deal_tickets)):
        raise HistorySyncFailed("history handoff ledger membership mismatch")
    atomic_json(
        pending_path,
        {
            "schema_version": 1,
            "job_id": job_id,
            "connection_id": connection_id,
            "history_document": archive_path.name,
            "history_document_sha256": archive_digest,
            # The document and the frozen baseline form one atomic recovery bundle. Keeping the
            # payload here lets a retry materialize missing staging bytes without querying MT5
            # again or moving the history/live boundary.
            "history_document_payload": document,
            "history_counts": normalized_counts,
            "anchor_sequence": sequence,
            # Every deal in the frozen ledger belongs to the history side of the boundary,
            # including accounting rows and deliberately non-projected reversals. A delayed
            # native callback for any of them must never escape later as a live trade.
            "archived_deal_tickets": anchored_deal_tickets,
            "imported_deal_tickets": imported_deal_tickets,
            "snapshot": {"positions": positions, "orders": orders, "deals": {}},
        },
    )
    return True


def _prepare_history_to_live_handoff(adapter: Any, root: Path) -> int:
    """Activate the baseline captured with the exact immutable history archive.

    The pending artifact is created before remote delivery and is reused byte-for-byte on a
    retry. No current MT5 state is recaptured here. Deal membership remains persistent after
    activation so even a delayed ``OnTradeTransaction`` callback is never imported twice.
    """

    artifact = read_json(root / "state" / "history-handoff-pending.json", {})
    acknowledge = getattr(adapter, "acknowledge_events", None)
    archive_name = artifact.get("history_document")
    archive_digest = artifact.get("history_document_sha256")
    sequence = artifact.get("anchor_sequence")
    snapshot = artifact.get("snapshot")
    tickets = artifact.get("archived_deal_tickets")
    imported_tickets = artifact.get("imported_deal_tickets")
    if (
        artifact.get("schema_version") != 1
        or artifact.get("connection_id") != root.name
        or not isinstance(artifact.get("job_id"), str)
        or not re.fullmatch(r"history-import-[0-9a-f]{16}\.json", str(archive_name))
        or not isinstance(archive_digest, str)
        or not re.fullmatch(r"[0-9a-f]{64}", archive_digest)
        or not isinstance(sequence, int)
        or isinstance(sequence, bool)
        or sequence < 0
        or not isinstance(snapshot, dict)
        or not isinstance(tickets, list)
        or not isinstance(imported_tickets, list)
        or any(
            not isinstance(ticket, str)
            or not re.fullmatch(r"[0-9]{1,32}", ticket)
            for ticket in [*tickets, *imported_tickets]
        )
        or not set(imported_tickets).issubset(set(tickets))
        or not callable(acknowledge)
    ):
        raise HistorySyncFailed("history handoff artifact invalid")
    positions = snapshot.get("positions")
    orders = snapshot.get("orders")
    deals = snapshot.get("deals")
    if not all(isinstance(value, dict) for value in (positions, orders, deals)) or deals:
        raise HistorySyncFailed("history handoff baseline invalid")
    archive_path = root / "state" / str(archive_name)
    try:
        current_digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    except OSError as exc:
        raise HistorySyncFailed("history handoff archive unavailable") from exc
    if current_digest != archive_digest:
        raise HistorySyncFailed("history handoff archive digest mismatch")

    active_path = root / "state" / "history-live-handoff.json"
    active = read_json(active_path, {})
    if active:
        active_matches = (
            active.get("schema_version") == 2
            and active.get("job_id") == artifact["job_id"]
            and active.get("connection_id") == root.name
            and active.get("history_document_sha256") == archive_digest
            and active.get("anchor_sequence") == sequence
            and active.get("archived_deal_tickets") == tickets
            and active.get("imported_deal_tickets") == imported_tickets
        )
        if active_matches:
            # Activation is the local commit record. It is written only after the baseline and
            # event checkpoint are durable. A reclaimed/completion retry must not rewind either
            # one after LiveSync has already advanced beyond this anchor.
            return sequence
        if active.get("job_id") == artifact["job_id"]:
            raise HistorySyncFailed("active history handoff conflicts with archive")

    PersistentSnapshot(root / "state" / "live_snapshot.json").save(snapshot)
    acknowledge(sequence)
    # This marker is the activation commit and must therefore be written last. Native ticket
    # membership remains durable across every later live poll; the next history job replaces it.
    atomic_json(
        active_path,
        {
            "schema_version": 2,
            "job_id": artifact["job_id"],
            "connection_id": root.name,
            "history_document_sha256": archive_digest,
            "anchor_sequence": sequence,
            "archived_deal_tickets": tickets,
            "imported_deal_tickets": imported_tickets,
        },
    )
    return sequence


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
) -> dict:
    """Serialize the complete history-to-live publication for one connection."""
    with connection_sync_lock(cid):
        return _start_file_bridge_and_sync_locked(
            job,
            api,
            root,
            cid,
            login,
            server,
            connection_endpoint,
            mode,
            from_date,
            store,
            process_factory,
            expert_binary,
            runtime_factory,
            trading_ingestion_url,
            endpoint_observer,
        )


def _start_file_bridge_and_sync_locked(
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
        counts = _run_history_sync(
            adapter,
            root,
            mode,
            from_date,
            api,
            job,
            cid,
            str(login),
            effective_server,
        )
        if mode != "new_only":
            _prepare_history_to_live_handoff(adapter, root)
            # Switch the already-running EA in place. OnTradeTransaction remains active while
            # the history archive is uploaded and during this handoff, eliminating the blind
            # interval in which a fully opened-and-closed trade used to disappear on restart.
            _require_lease(api, job)
            try:
                runtime.switch_to_new_only()
            except NativeMt5Error as exc:
                code = str(exc)
                raise Mt5InitializeFailed(code) from exc
            adapter = Mql5FileMt5Adapter(
                status.files_path,
                cid,
                login,
                effective_server,
                root / "state",
            )
            _verify_investor_access(adapter)
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
                adapter,
                root,
                mode,
                from_date,
                api,
                job,
                cid,
                str(login),
                server,
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
    """Run one serialized local file-bridge cycle.

    Provisioning/history can invoke this while the background event supervisor is active. The
    shared per-connection lock keeps snapshot, checkpoint, dedup and outbox updates atomic.
    """
    with connection_sync_lock(root.name):
        dedup = PersistentDedup(root / "state" / "live-dedup.sqlite")
        try:
            live = LiveSync(
                adapter,
                PersistentSnapshot(root / "state" / "live_snapshot.json"),
                dedup,
                sink or LocalEventSink(root / "data" / "live.jsonl"),
                outbox=EventOutbox(str(root / "state" / "live-outbox.json")),
                excursion_store=PersistentSnapshot(root / "state" / "position-excursions.json"),
            )
            return live.poll_once()
        finally:
            dedup.close()
