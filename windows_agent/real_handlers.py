from __future__ import annotations

import gc
import hashlib
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

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
    InstanceProvisionFailed,
    InvestorAccessNotVerified,
    LiveSyncFailed,
    Mt5AuthorizationFailed,
    Mt5InitializeFailed,
    SecretStoreFailed,
    TerminalStartFailed,
)
from .agent_secrets import AGENT_SCOPE_ID, PROVISIONING_KEY_SECRET_NAME
from .broker_endpoint_resolver import (
    BrokerEndpointResolutionError,
    VerifiedBrokerEndpoint,
)
from .broker_identity import (
    BrokerIdentityError,
    BrokerIdentitySuggestion,
)
from .broker_wizard import (
    BrokerWizardError,
    BrokerWizardEvidence,
    write_login_verification_artifact,
)
from .credential_envelope import decrypt_credential_envelope
from .job_runner import LeaseLost
from .provisioning.instance_layout import InstanceLayout
from .provisioning.mt5_instance import InstanceProvisioner
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
from .worker.live_sync import LiveSync
from .worker.local_event_sink import LocalEventSink
from .worker.mql5_file_adapter import Mql5FileMt5Adapter
from .worker.native_mt5_runtime import NativeMt5Error, NativeMt5Runtime
from .worker.trading_ingestion_sink import TradingIngestionSink

logger = logging.getLogger(__name__)

SERVER_PATTERN = re.compile(r"[A-Za-z0-9._ -]{1,128}")
BROKER_PATTERN = re.compile(r"[A-Za-z0-9 .,&'()+_/-]{1,128}")
DEFAULT_EXPERT_BINARY = Path(r"C:\TradeJournal\mt5-template\MQL5\Experts\TradeJournal\TradeJournalBridge.ex5")

JobHandler = Callable[[dict], dict]
BrokerWizard = Callable[
    [Path, str, str, str, Optional[Callable[[], None]]],
    BrokerWizardEvidence,
]


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
    if "authorization" in text:
        return Mt5AuthorizationFailed(str(exc))
    return Mt5InitializeFailed(str(exc))


def _ensure_no_stale_process(
    terminal: Path, state_path: Path, process_factory: Callable[[Path], Any]
) -> None:
    """Remove only an orphan using this isolated terminal path before a new job."""
    if terminal.is_file() and ProcessManager.find(terminal):
        process_factory(state_path).cleanup_path(terminal)


def sweep_stale_instances(
    instances_root: Path, process_factory: Callable[[Path], Any] = ProcessManager
) -> list[str]:
    """Called once when the daemon (re)starts (see agent_daemon.build_runner). No terminal64.exe
    should ever be running between jobs in this architecture -- anything found is an orphan left
    by a crash (service restart, process kill, host reboot) and is terminated so the next claimed
    job for that connection starts clean instead of silently racing an old process."""
    swept: list[str] = []
    if not instances_root.exists():
        return swept
    for entry in instances_root.iterdir():
        if not entry.is_dir():
            continue
        terminal = entry / "terminal" / "terminal64.exe"
        if terminal.is_file() and ProcessManager.find(terminal):
            process_factory(entry / "state" / "terminal-process.json").cleanup_path(terminal)
            swept.append(entry.name)
    return swept


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
    endpoint_resolver: Callable[[str], VerifiedBrokerEndpoint] | None = None,
    broker_identity_resolver: Callable[[str], BrokerIdentitySuggestion] | None = None,
    broker_wizard: BrokerWizard | None = None,
) -> dict[str, JobHandler]:
    """Real provision/historical_sync/deprovision handlers.

    Jobs are claimed by the managed JobRunner/control plane. Without an injected
    ``adapter_factory`` the Windows-native MQL5 file path is always used; the injection point
    exists only for isolated tests.
    """

    store = WindowsSecretStore(secrets_root)

    def provision(job: dict) -> dict:
        cid = canonical_uuid(str(job["connection_id"]))
        payload = job.get("payload") or {}
        _require_lease(api, job)
        _verify_binary_pin(source_terminal, terminal_sha256)
        _verify_binary_pin(expert_binary, expert_sha256)
        login, server, broker_label = _expected_identity(payload)
        payload_broker_label = broker_label
        mode, from_date = _history_window(job)
        broker_search_text = broker_label
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
            if not BROKER_PATTERN.fullmatch(broker_label):
                raise BrokerIdentityUnavailable(
                    "broker identity suggestion is invalid"
                )
        verified_endpoint: VerifiedBrokerEndpoint | None = None
        wizard_evidence: BrokerWizardEvidence | None = None
        if endpoint_resolver is None:
            connection_endpoint = server
        else:
            try:
                verified_endpoint = endpoint_resolver(broker_label)
                connection_endpoint = verified_endpoint.server_address
            except BrokerEndpointResolutionError as exc:
                if broker_wizard is None:
                    raise BrokerEndpointUnavailable(
                        "verified broker endpoint unavailable"
                    ) from exc
                try:
                    root = InstanceProvisioner(
                        instances_root, secrets_root
                    ).provision(cid, source_terminal, terminal_sha256)
                except Exception as provision_exc:
                    raise InstanceProvisionFailed(
                        "broker census instance provisioning failed"
                    ) from provision_exc
                _progress(
                    root,
                    status="censusing_broker",
                    connection_id=cid,
                )
                try:
                    wizard_evidence = broker_wizard(
                        root,
                        broker_search_text or broker_label,
                        broker_label,
                        server,
                        job.get("_lease_guard"),
                    )
                except BrokerWizardError as wizard_exc:
                    raise BrokerDiscoveryFailed(
                        "broker wizard failed closed"
                    ) from wizard_exc
                selected_broker = wizard_evidence.selected_broker_label
                if (
                    not BROKER_PATTERN.fullmatch(selected_broker)
                    or (
                        payload_broker_label is not None
                        and _broker_key(payload_broker_label)
                        != _broker_key(selected_broker)
                    )
                ):
                    raise BrokerIdentityUnavailable(
                        "broker wizard identity conflicts with the request"
                    )
                broker_label = selected_broker
                connection_endpoint = server
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
        try:
            store.write(cid, "mt5_login", str(login))
            store.write(cid, "mt5_server", server)
            store.write(cid, "mt5_broker_label", broker_label)
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
            root = InstanceProvisioner(instances_root, secrets_root).provision(
                cid, source_terminal, terminal_sha256
            )
        except TerminalStartFailed:
            raise
        except Exception as exc:
            raise InstanceProvisionFailed("instance provisioning failed") from exc
        _progress(root, status="provisioned", connection_id=cid)

        if adapter_factory is None:
            result = _start_file_bridge_and_sync(
                job, api, root, cid, login, server, connection_endpoint, mode, from_date, store,
                process_factory, expert_binary, runtime_factory, trading_ingestion_url,
            )
        else:
            result = _authenticate_and_sync(
                job, api, root, cid, login, server, mode, from_date, store,
                adapter_factory, process_factory, trading_ingestion_url,
            )
        verification_pid = result.pop("_verification_pid", None)
        if verified_endpoint is not None:
            result.update(
                {
                    "verified_server_name": server,
                    "verified_broker_label": broker_label,
                    "verification_method": "managed_investor_login",
                    "endpoint_protocol": verified_endpoint.protocol,
                    "endpoint_artifact_sha256": verified_endpoint.artifact_sha256,
                    "endpoint_verification_session_id": (
                        verified_endpoint.verification_session_id
                    ),
                }
            )
        elif wizard_evidence is not None:
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
            try:
                _, artifact_digest = write_login_verification_artifact(
                    root / "state",
                    evidence=wizard_evidence,
                    verification_pid=verification_pid,
                )
            except BrokerWizardError as exc:
                process_factory(
                    root / "state" / "terminal-process.json"
                ).stop()
                raise BrokerDiscoveryFailed(
                    "login verification evidence publication failed"
                ) from exc
            result.update(
                {
                    "verified_server_name": server,
                    "verified_broker_label": broker_label,
                    "verification_method": "managed_investor_login",
                    "endpoint_protocol": "TCP/TLS",
                    "endpoint_artifact_sha256": artifact_digest,
                    "endpoint_verification_session_id": wizard_evidence.run_id,
                }
            )
        _progress(root, status="connected")
        return result

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
        ingestion_sink = None
        if trading_ingestion_url:
            try:
                ingestion_sink = TradingIngestionSink(
                    root, trading_ingestion_url, store.read(cid, "bridge_token")
                )
            except Exception as exc:
                raise SecretStoreFailed("history ingestion token unavailable") from exc
        if adapter_factory is None:
            # The default adapter consumes only the EA's files.  A later history job must
            # therefore not read, decrypt or retain the investor password at all.
            _require_lease(api, job)
            _progress(root, status="importing_history")
            adapter = Mql5FileMt5Adapter(root / "terminal" / "MQL5" / "Files" / "TradeJournal", cid, login, server, root / "state")
            _verify_investor_access(adapter)
            # The native EA emits an authoritative event file for every backfilled transaction.
            # Do not synthesize a second event stream from snapshots; consume the files below.
            counts = _run_history_sync(adapter, root, mode, from_date)
            if ingestion_sink is not None:
                _run_live_sync_once(adapter, root, ingestion_sink)
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
                        adapter, root, mode, from_date, ingestion_sink, str(login), server
                    )
            except Mt5Error as exc:
                raise _map_mt5_error(exc) from exc
        _progress(root, status="connected")
        return {"imported_deals": counts["deals"], "imported_orders": counts["orders"]}

    def deprovision(job: dict) -> dict:
        cid = canonical_uuid(str(job["connection_id"]))
        layout = InstanceLayout(instances_root, cid)
        root = layout.path
        _progress_if_exists(root, status="disconnecting")
        try:
            # "ferma live sync"/"ferma history sync": no background thread survives a single job
            # in this architecture (see module docstring on job-at-a-time V1 design), so the only
            # real cleanup step is stopping the terminal process itself, idempotently -- safe to
            # call even if the instance was never fully provisioned (process.stop() is a no-op
            # when its state file is absent, and InstanceProvisioner.deprovision() is itself
            # idempotent, see test_fake_provision_deprovision_idempotent).
            process_factory(root / "state" / "terminal-process.json").stop()
            InstanceProvisioner(instances_root, secrets_root).deprovision(cid)
        except Exception as exc:
            raise DeprovisionFailed("deprovision failed") from exc
        _progress_if_exists(root, status="disconnected")
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
                cid, terminal_sha256, verify_code=False
            )
        except Exception as exc:
            raise InstanceProvisionFailed(
                "provisioned instance integrity validation failed"
            ) from exc
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
        if not (terminal.is_file() and ProcessManager.find(terminal)):
            try:
                InstanceProvisioner(instances_root, secrets_root).validate(
                    cid, terminal_sha256
                )
            except Exception as exc:
                raise InstanceProvisionFailed(
                    "provisioned instance code integrity validation failed"
                ) from exc
            _verify_binary_pin(terminal, terminal_sha256)
            _verify_binary_pin(expert_binary, expert_sha256)
            try:
                runtime = runtime_factory(root, cid)
                set_cancel_check = getattr(runtime, "set_cancel_check", None)
                if callable(set_cancel_check):
                    set_cancel_check(job.get("_lease_guard"))
                runtime.resume(
                    login=login,
                    server=server,
                    expert_binary=expert_binary,
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

        adapter = Mql5FileMt5Adapter(root / "terminal" / "MQL5" / "Files" / "TradeJournal", cid, login, server, root / "state")
        try:
            _verify_investor_access(adapter)
            sink = TradingIngestionSink(root, trading_ingestion_url, bridge_token)
            if not sink.send_heartbeat():
                raise LiveSyncFailed("live sync heartbeat was not acknowledged")
            delivered = _run_live_sync_once(adapter, root, sink)
        except AgentError:
            raise
        except Mt5Error as exc:
            raise _map_mt5_error(exc) from exc
        except Exception as exc:
            raise LiveSyncFailed("live sync check failed") from exc
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


def _verify_investor_access(adapter: Any) -> None:
    account = adapter.account_info()
    terminal_info = adapter.terminal_info()
    if not bool(getattr(terminal_info, "connected", False)):
        raise Mt5InitializeFailed("terminal not connected")
    if bool(getattr(account, "trade_allowed", True)):
        raise InvestorAccessNotVerified("account is not read-only/investor")


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
) -> dict:
    """Launch MT5 once, then consume only EA-produced local files.

    No Python MT5 IPC session is created here. The password is only supplied to the protected
    startup config inside ``NativeMt5Runtime`` and is cleared before file parsing/history sync.
    """
    terminal = root / "terminal" / "terminal64.exe"
    state_path = root / "state" / "terminal-process.json"
    _ensure_no_stale_process(terminal, state_path, process_factory)
    _require_lease(api, job)
    _progress(root, status="starting_native_file_bridge")
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
        if code == "investor_readonly_not_verified":
            raise InvestorAccessNotVerified(code) from exc
        if code in ("identity_mismatch", "server_identity_mismatch"):
            raise AccountIdentityMismatch(code) from exc
        if code == "terminal_start_failed":
            raise TerminalStartFailed(code) from exc
        raise Mt5InitializeFailed(code) from exc
    finally:
        investor_password = ""
        gc.collect()

    try:
        process_factory(state_path).adopt(terminal)
    except (AttributeError, RuntimeError):
        # The runtime owns an already-started, exact terminal path. Adoption is persistence for
        # deprovision/recovery; a test double may intentionally omit real OS process discovery.
        pass
    adapter = Mql5FileMt5Adapter(status.files_path, cid, login, server, root / "state")
    ingestion_sink = None
    if trading_ingestion_url:
        try:
            ingestion_sink = TradingIngestionSink(
                root, trading_ingestion_url, store.read(cid, "bridge_token")
            )
        except Exception as exc:
            raise SecretStoreFailed("provision ingestion token unavailable") from exc
    try:
        _verify_investor_access(adapter)
        _progress(root, status="importing_history")
        _require_lease(api, job)
        # Native backfill is delivered by events/event-*.json in the following live poll.
        counts = _run_history_sync(adapter, root, mode, from_date)
        _progress(root, status="starting_live_sync")
        _require_lease(api, job)
        delivered = _run_live_sync_once(adapter, root, ingestion_sink)
    except AgentError:
        raise
    except Mt5Error as exc:
        raise _map_mt5_error(exc) from exc
    except Exception as exc:
        raise LiveSyncFailed("file bridge sync failed") from exc
    return {
        "imported_deals": counts["deals"],
        "imported_orders": counts["orders"],
        "live_sync_started": True,
        "live_sync_events_delivered": delivered,
        "file_bridge": "mql5-local-json",
        "_verification_pid": status.pid,
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
    _progress(root, status="authenticating")
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
            _verify_investor_access(adapter)
            _progress(root, status="importing_history")
            _require_lease(api, job)
            counts = _run_history_sync(
                adapter, root, mode, from_date, ingestion_sink, str(login), server
            )
            _progress(root, status="starting_live_sync")
            _require_lease(api, job)
            try:
                delivered = _run_live_sync_once(adapter, root, ingestion_sink)
            except Exception as exc:
                raise LiveSyncFailed("live sync check failed") from exc
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
    docs/windows/architecture.md) -- ongoing monitoring is now driven by the recurring live_sync
    job (see build_real_handlers.live_sync), which passes its own HTTP-forwarding sink instead of
    the local-only default used here for the one-shot provisioning-time check."""
    live = LiveSync(
        adapter,
        PersistentSnapshot(root / "state" / "live_snapshot.json"),
        PersistentDedup(root / "state" / "live-dedup.sqlite"),
        sink or LocalEventSink(root / "data" / "live.jsonl"),
        outbox=EventOutbox(str(root / "state" / "live-outbox.json")),
    )
    return live.poll_once()
