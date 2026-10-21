"""Third phase of the nightly maintenance: every account is checked after the updates.

Read only: it never stops, starts or rotates a terminal. For each account assigned to this
worker it checks that exactly one terminal runs, that the Bridge heartbeat is fresh and
connected for the expected account in investor mode, that the Bridge is the pinned one and
that the MT5 build is not older than the golden template.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Any, Callable, Iterable

from .provisioning.mt5_instance import _MANAGED_RUNTIME_ASSETS, InstanceProvisioner
from .security import canonical_uuid
from .state_store import read_json
from .worker.adapter_errors import IdentityMismatch
from .worker.maintenance_mt5_runtime import NativeMt5Runtime
from .worker.mql5_file_adapter import Mql5FileMt5Adapter, Mql5FileStale

logger = logging.getLogger(__name__)

PROBLEMS = frozenset({
    "not_provisioned",
    "not_running",
    "process_ambiguous",
    "heartbeat_stale",
    "disconnected",
    "identity_mismatch",
    "trading_enabled",
    "expert_outdated",
    "mt5_outdated",
    "unverifiable",
})
# A terminal restarted moments ago may still be logging in or writing its first heartbeat.
_TRANSIENT = frozenset({"heartbeat_stale", "disconnected", "unverifiable"})


@dataclass(frozen=True)
class Mt5FleetVerification:
    checked: tuple[str, ...]
    issues: tuple[tuple[str, str], ...]

    @property
    def healthy(self) -> int:
        return len(self.checked) - len(self.issues)


def _running_terminal_pids(root: Path, connection_id: str) -> list[int]:
    return NativeMt5Runtime(root, connection_id)._running_terminal_pids()


class Mt5FleetVerifier:
    def __init__(
        self,
        *,
        instances_root: Path,
        secrets: Any,
        expert_sha256: str,
        template_terminal: Path,
        build_reader: Callable[[Path], int] | None = None,
        connection_ids: Callable[[], Iterable[str]] | None = None,
        adapter_factory: Callable[..., Any] = Mql5FileMt5Adapter,
        terminal_pids: Callable[[Path, str], list[int]] = _running_terminal_pids,
        settle_seconds: float = 90.0,
        poll_seconds: float = 5.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.instances_root = Path(instances_root)
        self.secrets = secrets
        self.expert_sha256 = expert_sha256.strip().lower()
        self.template_terminal = Path(template_terminal)
        self.build_reader = build_reader
        self.connection_ids = connection_ids
        self.adapter_factory = adapter_factory
        self.terminal_pids = terminal_pids
        self.settle_seconds = settle_seconds
        self.poll_seconds = poll_seconds
        self.sleep = sleep
        self.clock = clock

    def _targets(self) -> tuple[str, ...]:
        assigned = set(self.connection_ids() if self.connection_ids is not None else ())
        if not assigned and self.instances_root.is_dir():
            # Without tonight's inventory every provisioned terminal is checked.
            for child in self.instances_root.iterdir():
                try:
                    connection_id = canonical_uuid(child.name)
                except ValueError:
                    continue
                state = read_json(child / "state" / "instance.json", {})
                if state.get("status") == "provisioned":
                    assigned.add(connection_id)
        return tuple(sorted(assigned))

    def _problem(self, connection_id: str) -> str | None:
        root = self.instances_root / connection_id
        state = read_json(root / "state" / "instance.json", {})
        if state.get("connection_id") != connection_id or state.get("status") != "provisioned":
            return "not_provisioned"
        pids = self.terminal_pids(root, connection_id)
        if not pids:
            return "not_running"
        if len(pids) > 1:
            return "process_ambiguous"
        login = int(self.secrets.read(connection_id, "mt5_login"))
        server = self.secrets.read(connection_id, "mt5_server")
        adapter = self.adapter_factory(
            root / "terminal" / "MQL5" / "Files" / "TradeJournal",
            connection_id,
            login,
            server,
            root / "state",
        )
        try:
            connected = adapter.terminal_info().connected
            account = adapter.account_info()
        except Mql5FileStale:
            return "heartbeat_stale"
        except IdentityMismatch:
            return "identity_mismatch"
        if not connected:
            return "disconnected"
        if account.trade_allowed:
            return "trading_enabled"
        if InstanceProvisioner._sha256(root / "terminal" / _MANAGED_RUNTIME_ASSETS[0]) != self.expert_sha256:
            return "expert_outdated"
        if self.build_reader is not None and self.build_reader(
            root / "terminal" / "terminal64.exe"
        ) < self.build_reader(self.template_terminal):
            return "mt5_outdated"
        return None

    def _checked_problem(self, connection_id: str) -> str | None:
        try:
            return self._problem(connection_id)
        except Exception as exc:
            logger.warning(
                "MT5 verification could not read %s (%s)",
                connection_id,
                type(exc).__name__,
            )
            return "unverifiable"

    def verify(self, stop_event: Event) -> Mt5FleetVerification:
        targets = self._targets()
        problems = {connection_id: self._checked_problem(connection_id) for connection_id in targets}
        deadline = self.clock() + self.settle_seconds
        while (
            any(problem in _TRANSIENT for problem in problems.values())
            and self.clock() < deadline
            and not stop_event.is_set()
        ):
            self.sleep(self.poll_seconds)
            for connection_id, problem in problems.items():
                if problem in _TRANSIENT:
                    problems[connection_id] = self._checked_problem(connection_id)
        issues = tuple(
            (connection_id, problem)
            for connection_id, problem in problems.items()
            if problem is not None
        )
        for connection_id, problem in issues:
            logger.warning("MT5 verification: %s %s", connection_id, problem)
        return Mt5FleetVerification(targets, issues)
