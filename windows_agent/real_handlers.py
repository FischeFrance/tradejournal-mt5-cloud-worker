from __future__ import annotations

import gc
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .agent_errors import (
    AccountIdentityMismatch,
    AgentError,
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
from .credential_envelope import decrypt_credential_envelope
from .job_runner import LeaseLost
from .provisioning.instance_layout import InstanceLayout
from .provisioning.mt5_instance import InstanceProvisioner
from .provisioning.process_manager import ProcessManager
from .provisioning.secret_store import WindowsSecretStore
from .security import canonical_uuid
from .state_store import atomic_json, read_json
from .worker.dedup import PersistentDedup
from .worker.direct_mt5_adapter import (
    DirectMt5Adapter,
    IdentityMismatch,
    Mt5Error,
    Mt5IpcError,
    Mt5ProcessCrashed,
)
from .worker.history_sync import HistoryMode, HistorySync
from .worker.live_sync import LiveSync
from .worker.local_event_sink import LocalEventSink

logger = logging.getLogger(__name__)

SERVER_PATTERN = re.compile(r"[A-Za-z0-9._ -]{1,128}")

JobHandler = Callable[[dict], dict]


class PersistentSnapshot:
    """Disk-backed replacement for customer_flow.py's in-memory MemorySnapshot. A real handler
    only runs one poll_once() per job (see module docstring below), so this is mostly a
    convenience, but persisting it means a future resumed live-sync check starts from the last
    known state instead of an empty one."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def get(self) -> dict:
        return read_json(self.path, {"positions": {}, "orders": {}, "deals": {}})

    def save(self, value: dict) -> None:
        atomic_json(self.path, value)


def _progress(root: Path, **fields: Any) -> None:
    """Local-only progress tracking for operator visibility and Fase 5 recovery. The contract's
    job status enum (pending/claimed/running/complete/failed/cancelled, see
    contracts/mt5-agent-v1/schema.json) has no "authenticating"/"importing_history"/"connected"
    states -- those are internal granularity, never sent to the control plane."""
    path = root / "state" / "job_progress.json"
    current = read_json(path)
    current.update(fields)
    atomic_json(path, current)


def _require_lease(api: Any, job: dict) -> None:
    heartbeat = api.heartbeat(job["job_id"], job["lease_id"])
    if not heartbeat.get("lease_valid", False):
        raise LeaseLost("lease lost")


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


def _expected_identity(payload: dict) -> tuple[int, str]:
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
    return login, server


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
        # DirectMt5Adapter.verify_identity() does not distinguish which field mismatched (see
        # worker/direct_mt5_adapter.py) -- account_identity_mismatch is used as the primary code;
        # server_identity_mismatch remains reachable (mapped, tested) for callers/adapters that do
        # distinguish (e.g. a future NativeMt5Runtime-backed handler already does, see
        # worker/native_mt5_runtime.py's separate identity_mismatch/server_identity_mismatch codes).
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
    """Recovery safeguard (Fase 5): by this architecture's design nothing keeps MT5 running
    between jobs (see _run_live_sync_once's docstring), so a terminal64.exe already running at
    this instance's path can only be an orphan from a crashed previous attempt, never a
    legitimate concurrent session. Terminating it first prevents a second terminal64.exe fighting
    the same portable data directory."""
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
    adapter_factory: Callable[..., Any] = DirectMt5Adapter,
    process_factory: Callable[[Path], Any] = ProcessManager,
) -> dict[str, JobHandler]:
    """Real provision/historical_sync/deprovision handlers, reusing exactly the same building
    blocks as customer_flow.py (InstanceProvisioner, WindowsSecretStore, DirectMt5Adapter,
    HistorySync, LiveSync) but driven by a job already claimed by the real JobRunner/control
    plane, instead of customer_flow.py's own CLI-oriented LocalControlPlane. See
    CONTROL-PLANE-NEXT-STEPS.txt for why the two could not simply be wired together as-is."""

    store = WindowsSecretStore(secrets_root)

    def provision(job: dict) -> dict:
        cid = canonical_uuid(str(job["connection_id"]))
        payload = job.get("payload") or {}
        _require_lease(api, job)
        password = _decrypt_envelope(payload, secrets_root)
        login, server = _expected_identity(payload)
        mode, from_date = _history_window(job)

        try:
            store.write(cid, "mt5_login", str(login))
            store.write(cid, "mt5_server", server)
            store.write(cid, "mt5_investor_password", password)
        except Exception as exc:
            raise SecretStoreFailed("failed to persist credentials to DPAPI") from exc
        finally:
            del password
            gc.collect()

        layout = InstanceLayout(instances_root, cid)
        try:
            if layout.path.exists():
                root = layout.path
            else:
                if not source_terminal.is_file():
                    raise TerminalStartFailed("golden MT5 terminal template missing")
                root = InstanceProvisioner(instances_root, secrets_root).provision(
                    cid, source_terminal
                )
        except TerminalStartFailed:
            raise
        except Exception as exc:
            raise InstanceProvisionFailed("instance provisioning failed") from exc
        _progress(root, status="provisioned", connection_id=cid)

        result = _authenticate_and_sync(
            job, api, root, cid, login, server, mode, from_date, store, adapter_factory, process_factory
        )
        _progress(root, status="connected")
        return result

    def historical_sync(job: dict) -> dict:
        cid = canonical_uuid(str(job["connection_id"]))
        mode, from_date = _history_window(job)
        layout = InstanceLayout(instances_root, cid)
        root = layout.path
        if not root.exists():
            raise InstanceProvisionFailed("no provisioned instance for connection")
        try:
            login = int(store.read(cid, "mt5_login"))
            server = store.read(cid, "mt5_server")
        except Exception as exc:
            raise SecretStoreFailed("stored identity unavailable") from exc

        _require_lease(api, job)
        _progress(root, status="authenticating")
        try:
            investor_password = store.read(cid, "mt5_investor_password")
        except Exception as exc:
            raise SecretStoreFailed("stored credential unavailable") from exc

        terminal = root / "terminal" / "terminal64.exe"
        state_path = root / "state" / "terminal-process.json"
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
                counts = _run_history_sync(adapter, root, mode, from_date)
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

    return {
        "provision": provision,
        "historical_sync": historical_sync,
        "deprovision": deprovision,
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


def _run_history_sync(adapter: Any, root: Path, mode: HistoryMode, from_date: "datetime | None") -> dict:
    dedup = PersistentDedup(root / "state" / "history-dedup.sqlite")
    sink = _deduped_sink(dedup, LocalEventSink(root / "data" / "history.jsonl"))
    try:
        return HistorySync(adapter, root / "state" / "history.json", sink).run(mode, from_date)
    except Exception as exc:
        raise HistorySyncFailed("history import failed") from exc


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
            counts = _run_history_sync(adapter, root, mode, from_date)
            _progress(root, status="starting_live_sync")
            _require_lease(api, job)
            try:
                delivered = _run_live_sync_once(adapter, root)
            except Exception as exc:
                raise LiveSyncFailed("live sync check failed") from exc
    except Mt5Error as exc:
        raise _map_mt5_error(exc) from exc

    return {
        "imported_deals": counts["deals"],
        "imported_orders": counts["orders"],
        "live_sync_started": True,
        "live_sync_events_delivered": delivered,
    }


def _run_live_sync_once(adapter: Any, root: Path) -> int:
    """A single poll_once(), exactly like customer_flow.py's own live-sync proof step -- this
    architecture runs one job at a time (docs/windows/architecture.md) and the MetaTrader5 Python
    package holds a single global IPC connection per process, so there is no continuous
    background live-sync thread here; ongoing monitoring needs a periodic job/scheduler this
    contract does not define yet (see CONTROL-PLANE-NEXT-STEPS.txt)."""
    live = LiveSync(
        adapter,
        PersistentSnapshot(root / "state" / "live_snapshot.json"),
        PersistentDedup(root / "state" / "live-dedup.sqlite"),
        LocalEventSink(root / "data" / "live.jsonl"),
    )
    return live.poll_once()
