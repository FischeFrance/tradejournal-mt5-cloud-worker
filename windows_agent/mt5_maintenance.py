"""Nightly MT5 release probe, golden promotion, pool rebuild and fleet rollout."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import Event, RLock
from typing import Any, Callable

from .mt5_lifecycle import Mt5LifecycleCoordinator
from .mt5_recovery_window import new_only_recovery_from
from .provisioning.mt5_instance_pool import Mt5InstancePool
from .provisioning.mt5_instance_rotation import (
    Mt5FleetRotationReport,
    Mt5InstanceRotationError,
    Mt5InstanceRotator,
    Mt5TemplateRelease,
)
from .provisioning.mt5_template import (
    Mt5TemplateManager,
    Mt5TemplateRecoveryRequired,
    PreparedMt5Template,
)
from .provisioning.mt5_update_store import (
    Mt5PendingUpdateReceipt,
    Mt5PendingUpdateStore,
    Mt5PendingUpdateStoreError,
    Mt5UpdateRelease,
)
from .provisioning.process_manager import ProcessManager
from .security import canonical_uuid
from .state_store import read_json
from .worker.native_mt5_runtime import NativeMt5Runtime, NativeMt5Status

logger = logging.getLogger(__name__)


class Mt5MaintenanceError(RuntimeError):
    """A sanitized nightly maintenance failure."""


@dataclass(frozen=True)
class Mt5MaintenanceReport:
    previous_release: Mt5TemplateRelease
    current_release: Mt5TemplateRelease
    checked_connections: tuple[str, ...]
    migrated_connections: tuple[str, ...]
    already_current_connections: tuple[str, ...]
    discarded_pool_slots: int
    ready_pool_slots: int

    @property
    def release_changed(self) -> bool:
        return self.previous_release != self.current_release


@dataclass(frozen=True)
class Mt5CanaryOnlyReport:
    """Sanitized result of one explicitly targeted MT5 canary probe."""

    connection_id: str
    server: str
    pending_update_receipt_ids: tuple[str, ...]

    @property
    def update_captured(self) -> bool:
        return bool(self.pending_update_receipt_ids)


@dataclass(frozen=True)
class _Canary:
    connection_id: str
    root: Path
    login: int
    server: str


class Mt5MaintenanceCoordinator:
    """Perform one idempotent maintenance pass on the daemon's main thread.

    There is no anonymous MT5 release endpoint.  One already-provisioned,
    credentialed account per server is therefore restarted briefly to let the
    official terminal negotiate LiveUpdate.  It uses MT5's persisted
    ``accounts.dat`` and never reads an investor password.  A release is
    promoted only after that canary has logged in, emitted a connected
    heartbeat, and proved that trading remains disabled.
    """

    def __init__(
        self,
        *,
        instances_root: Path,
        expert_binary: Path,
        template_manager: Mt5TemplateManager,
        rotator: Mt5InstanceRotator,
        lifecycle: Mt5LifecycleCoordinator,
        template_lock: RLock,
        instance_pool: Mt5InstancePool | None = None,
        pending_update_store: Mt5PendingUpdateStore | None = None,
        runtime_factory: Callable[[Path, str], Any] = NativeMt5Runtime,
        process_factory: Callable[[Path], Any] = ProcessManager,
        secret_store: Any | None = None,
    ) -> None:
        self.instances_root = Path(instances_root).resolve()
        self.expert_binary = Path(expert_binary).resolve()
        self.template_manager = template_manager
        self.rotator = rotator
        self.lifecycle = lifecycle
        self.template_lock = template_lock
        self.instance_pool = instance_pool
        self.pending_update_store = pending_update_store
        self.runtime_factory = runtime_factory
        self.process_factory = process_factory
        self.secrets = secret_store or rotator.secrets

    def _current_release(self) -> Mt5TemplateRelease:
        with self.template_lock:
            return Mt5TemplateRelease.from_template(
                self.template_manager.source_terminal,
                self.template_manager.current_sha256,
            )

    @staticmethod
    def _validate_status(status: NativeMt5Status) -> None:
        if (
            status.pid <= 0
            or status.heartbeat.get("terminal_connected") is not True
            or status.heartbeat.get("account_trade_allowed") is not False
        ):
            raise Mt5MaintenanceError("MT5 canary health check failed")

    def _canaries(self, target: Mt5TemplateRelease) -> tuple[_Canary, ...]:
        by_server: dict[str, list[_Canary]] = {}
        if not self.instances_root.exists():
            return ()
        for candidate in sorted(
            self.instances_root.iterdir(), key=lambda value: value.name
        ):
            try:
                connection_id = canonical_uuid(candidate.name)
            except ValueError:
                continue
            root = self.rotator._instance_root(connection_id)
            state = read_json(root / "state" / "instance.json", {})
            if state.get("status") != "provisioned":
                continue
            try:
                login = int(self.secrets.read(connection_id, "mt5_login"))
                server = self.secrets.read(connection_id, "mt5_server")
            except Exception:
                logger.warning(
                    "skipping MT5 canary with unavailable identity (connection=%s)",
                    connection_id,
                )
                continue
            if login <= 0 or not server or server != server.strip():
                logger.warning(
                    "skipping MT5 canary with invalid identity (connection=%s)",
                    connection_id,
                )
                continue
            by_server.setdefault(server.casefold(), []).append(
                _Canary(connection_id, root, login, server)
            )

        selected: list[_Canary] = []
        for values in by_server.values():
            # Prefer an instance already on the golden release.  A stale one is
            # still a valid fallback and will be rebased in the final rollout.
            values.sort(
                key=lambda value: (
                    not self.rotator.matches_target(
                        value.connection_id,
                        target,
                    ),
                    value.connection_id,
                )
            )
            selected.append(values[0])
        return tuple(sorted(selected, key=lambda value: value.connection_id))

    def _capture_callback(
        self,
        stop_event: Event,
        *,
        on_captured: Callable[[str], None] | None = None,
        require_pending_store: bool = False,
    ) -> Callable[..., str | None]:
        pending_update_store = self.pending_update_store
        if require_pending_store and pending_update_store is None:
            raise Mt5MaintenanceError("MT5 pending update store is disabled")

        def capture(
            bundle_root: Path,
            updater: Path,
            config: Path,
            signer_subject: str,
        ) -> str | None:
            if stop_event.is_set():
                raise Mt5MaintenanceError("MT5 maintenance was interrupted")
            if pending_update_store is not None:
                receipt = pending_update_store.capture(
                    bundle_root,
                    updater,
                    config,
                    signer_subject,
                )
                if on_captured is not None:
                    on_captured(receipt.receipt_id)
                return receipt.receipt_id
            if require_pending_store:
                raise Mt5MaintenanceError("MT5 pending update store is disabled")

            # Compatibility for deployments that explicitly disable scheduled
            # maintenance. Production wiring always supplies the durable store,
            # so no normal connection can publish a golden build during the day.
            with self.template_lock:
                return self.template_manager.promote_verified_update(
                    bundle_root,
                    updater,
                    config,
                    signer_subject,
                )

        return capture

    def _adopt(self, canary: _Canary) -> None:
        try:
            self.process_factory(canary.root / "state" / "terminal-process.json").adopt(
                canary.root / "terminal" / "terminal64.exe"
            )
        except AttributeError:
            return
        except RuntimeError as exc:
            raise Mt5MaintenanceError("MT5 canary process adoption failed") from exc

    def _restore_canary(
        self,
        canary: _Canary,
        runtime: Any,
        stop_event: Event,
        recovery_from: datetime,
        capture_callback: Callable[..., str | None] | None = None,
    ) -> None:
        if not runtime.stop():
            raise Mt5MaintenanceError("MT5 canary recovery could not stop terminal")
        target = self._current_release()
        self.rotator.rotate_one(
            canary.connection_id,
            target,
            force=True,
            history_from=recovery_from,
            verified_update_callback=(
                capture_callback or self._capture_callback(stop_event)
            ),
            verified_update_required=self.pending_update_store is not None,
        )

    def _probe(
        self,
        canary: _Canary,
        stop_event: Event,
        *,
        capture_callback: Callable[..., str | None] | None = None,
    ) -> None:
        with self.lifecycle.connection(canary.connection_id):
            self._require_not_stopped(stop_event)
            runtime = self.runtime_factory(canary.root, canary.connection_id)
            set_cancel_check = getattr(runtime, "set_cancel_check", None)
            if callable(set_cancel_check):

                def require_not_stopped() -> None:
                    if stop_event.is_set():
                        raise Mt5MaintenanceError("MT5 maintenance was interrupted")

                set_cancel_check(require_not_stopped)
            runtime.set_verified_vendor_update_callback(
                capture_callback or self._capture_callback(stop_event),
                required=self.pending_update_store is not None,
            )
            try:
                recovery_from = new_only_recovery_from(canary.root)
            except Exception as exc:
                raise Mt5MaintenanceError(
                    "MT5 canary recovery cursor is unavailable"
                ) from exc
            try:
                if not runtime.stop():
                    raise Mt5MaintenanceError("MT5 canary stop failed")
                status = runtime.resume(
                    login=canary.login,
                    server=canary.server,
                    expert_binary=self.expert_binary,
                    history_mode="new_only",
                    history_from=recovery_from,
                )
                self._validate_status(status)
                self._adopt(canary)
            except Exception as exc:
                try:
                    self._restore_canary(
                        canary,
                        runtime,
                        stop_event,
                        recovery_from,
                        capture_callback,
                    )
                except Exception as recovery_exc:
                    raise Mt5MaintenanceError(
                        "MT5 canary probe and recovery failed"
                    ) from recovery_exc
                raise Mt5MaintenanceError(
                    "MT5 canary probe failed and was recovered"
                ) from exc

    def run_canary_only(
        self,
        connection_id: str,
        expected_server: str,
        stop_event: Event,
    ) -> Mt5CanaryOnlyReport:
        """Probe exactly one account without publishing or cascading an update.

        A healthy signed LiveUpdate is retained only as a durable pending
        receipt.  The ordinary scheduled maintenance pass remains the sole
        path that may promote the golden template, rebuild the pool, or rotate
        the fleet.
        """

        self._require_not_stopped(stop_event)
        if self.pending_update_store is None:
            raise Mt5MaintenanceError("MT5 pending update store is disabled")
        if (
            not isinstance(expected_server, str)
            or not expected_server
            or expected_server != expected_server.strip()
        ):
            raise Mt5MaintenanceError("MT5 canary expected server is invalid")
        try:
            connection_id = canonical_uuid(connection_id)
        except (TypeError, ValueError) as exc:
            raise Mt5MaintenanceError("MT5 canary connection is invalid") from exc
        try:
            root = self.rotator._instance_root(connection_id)
            state = read_json(root / "state" / "instance.json", {})
        except Exception as exc:
            raise Mt5MaintenanceError("MT5 canary instance is invalid") from exc
        if (
            state.get("connection_id") != connection_id
            or state.get("status") != "provisioned"
        ):
            raise Mt5MaintenanceError("MT5 canary instance is invalid")
        try:
            login = int(self.secrets.read(connection_id, "mt5_login"))
            server = self.secrets.read(connection_id, "mt5_server")
        except Exception as exc:
            raise Mt5MaintenanceError("MT5 canary identity is unavailable") from exc
        if (
            login <= 0
            or not isinstance(server, str)
            or not server
            or server != server.strip()
        ):
            raise Mt5MaintenanceError("MT5 canary identity is invalid")
        if server.casefold() != expected_server.casefold():
            raise Mt5MaintenanceError("MT5 canary server does not match")

        captured: list[str] = []

        def record_capture(receipt_id: str) -> None:
            if receipt_id not in captured:
                captured.append(receipt_id)

        self._require_not_stopped(stop_event)
        self._probe(
            _Canary(connection_id, root, login, server),
            stop_event,
            capture_callback=self._capture_callback(
                stop_event,
                on_captured=record_capture,
                require_pending_store=True,
            ),
        )
        return Mt5CanaryOnlyReport(
            connection_id,
            server,
            tuple(captured),
        )

    @staticmethod
    def _rotation_release(release: Mt5UpdateRelease) -> Mt5TemplateRelease:
        return Mt5TemplateRelease(
            release.terminal_sha256,
            release.code_manifest_sha256,
            release.release_id,
        )

    @staticmethod
    def _store_release(release: Mt5TemplateRelease) -> Mt5UpdateRelease:
        return Mt5UpdateRelease(
            release.terminal_sha256,
            release.code_manifest_sha256,
            release.release_id,
        )

    @staticmethod
    def _require_not_stopped(stop_event: Event) -> None:
        if stop_event.is_set():
            raise Mt5MaintenanceError("MT5 maintenance was interrupted")

    def _store_pending(self) -> tuple[Mt5PendingUpdateReceipt, ...]:
        store = self.pending_update_store
        if store is None:
            return ()
        try:
            return store.pending()
        except Mt5PendingUpdateStoreError:
            raise
        except OSError as exc:
            raise Mt5PendingUpdateStoreError(
                "MT5 pending update store is unavailable"
            ) from exc

    def _store_complete(self, receipt_id: str) -> None:
        store = self.pending_update_store
        if store is None:
            raise Mt5MaintenanceError("MT5 pending update store is disabled")
        try:
            store.complete(receipt_id)
        except Mt5PendingUpdateStoreError:
            raise
        except OSError as exc:
            raise Mt5PendingUpdateStoreError(
                "MT5 pending update store is unavailable"
            ) from exc

    def _store_quarantine(self, receipt_id: str) -> None:
        store = self.pending_update_store
        if store is None:
            raise Mt5MaintenanceError("MT5 pending update store is disabled")
        try:
            store.quarantine(receipt_id)
        except Mt5PendingUpdateStoreError:
            raise
        except OSError as exc:
            raise Mt5PendingUpdateStoreError(
                "MT5 pending update store is unavailable"
            ) from exc

    def _resolve_pending_receipt_chain(
        self,
        current: Mt5TemplateRelease,
        pending: tuple[Mt5PendingUpdateReceipt, ...],
    ) -> tuple[Mt5PendingUpdateReceipt, ...]:
        """Return one unique, acyclic release path without mutating the store."""

        node = self._store_release(current)
        visited = {node}
        chain: list[Mt5PendingUpdateReceipt] = []
        while True:
            applicable = [
                receipt
                for receipt in pending
                if receipt.source_release == node
                and receipt.target_release != receipt.source_release
            ]
            if not applicable:
                return tuple(chain)
            targets = {receipt.target_release for receipt in applicable}
            if len(targets) != 1:
                raise Mt5MaintenanceError(
                    "MT5 pending updates disagree on target release"
                )
            target = next(iter(targets))
            if target in visited:
                raise Mt5MaintenanceError("MT5 pending update chain is cyclic")
            # Multiple independently captured bundles for the same exact edge
            # are equivalent. Deterministically consume one; all duplicate
            # receipts are retired only after the final atomic commit.
            receipt = min(applicable, key=lambda value: value.receipt_id)
            chain.append(receipt)
            visited.add(target)
            node = target

    def _pending_receipt_chain(
        self,
        current: Mt5TemplateRelease,
    ) -> tuple[Mt5PendingUpdateReceipt, ...]:
        if self.pending_update_store is None:
            return ()
        current_store_release = self._store_release(current)
        pending = self._store_pending()

        # Older agents could persist a READY A->A receipt when an updater ran
        # successfully but changed no release bytes. It is evidence, not an
        # update edge, and must never conflict with a legitimate A->B bundle.
        for receipt in pending:
            if receipt.source_release == receipt.target_release:
                self._store_quarantine(receipt.receipt_id)
        pending = self._store_pending()

        # A crash after publication but before receipt cleanup is idempotent.
        for receipt in pending:
            if receipt.target_release == current_store_release:
                self._store_complete(receipt.receipt_id)
        pending = self._store_pending()
        chain = self._resolve_pending_receipt_chain(current, pending)
        if chain:
            return chain

        # No edge leaves the current golden: everything else belongs to an
        # obsolete managed-code anchor or an unreachable release graph.
        for receipt in pending:
            self._store_quarantine(receipt.receipt_id)
        if pending:
            logger.warning(
                "quarantined %s MT5 update receipt(s) for obsolete source releases",
                len(pending),
            )
        return ()

    def _select_pending_receipt(
        self,
        current: Mt5TemplateRelease,
    ) -> Mt5PendingUpdateReceipt | None:
        chain = self._pending_receipt_chain(current)
        return chain[0] if chain else None

    def _matches_applicable_pending_target(
        self,
        canary: _Canary,
        current: Mt5TemplateRelease,
    ) -> bool:
        if self.pending_update_store is None:
            return False
        chain = self._resolve_pending_receipt_chain(
            current,
            self._store_pending(),
        )
        for receipt in chain:
            if self.rotator.matches_target(
                canary.connection_id,
                self._rotation_release(receipt.target_release),
            ):
                return True
        return False

    def _prepare_candidate(
        self,
        receipt: Mt5PendingUpdateReceipt,
        stop_event: Event,
    ) -> PreparedMt5Template:
        return self.template_manager.prepare_verified_update(
            receipt.root,
            receipt.updater,
            receipt.updater_config,
            receipt.signer_subject,
            expected_source_terminal_sha256=(
                receipt.source_release.terminal_sha256
            ),
            expected_source_code_manifest_sha256=(
                receipt.source_release.code_manifest_sha256
            ),
            expected_target_terminal_sha256=(
                receipt.target_release.terminal_sha256
            ),
            expected_target_code_manifest_sha256=(
                receipt.target_release.code_manifest_sha256
            ),
            cancel_check=lambda: self._require_not_stopped(stop_event),
        )

    def _advance_candidate(
        self,
        prepared: PreparedMt5Template,
        receipt: Mt5PendingUpdateReceipt,
        stop_event: Event,
    ) -> PreparedMt5Template:
        return self.template_manager.advance_prepared_update(
            prepared,
            receipt.root,
            receipt.updater,
            receipt.updater_config,
            receipt.signer_subject,
            expected_source_terminal_sha256=(
                receipt.source_release.terminal_sha256
            ),
            expected_source_code_manifest_sha256=(
                receipt.source_release.code_manifest_sha256
            ),
            expected_target_terminal_sha256=(
                receipt.target_release.terminal_sha256
            ),
            expected_target_code_manifest_sha256=(
                receipt.target_release.code_manifest_sha256
            ),
            cancel_check=lambda: self._require_not_stopped(stop_event),
        )

    def _validate_candidate_on_canaries(
        self,
        prepared: PreparedMt5Template,
        canaries: tuple[_Canary, ...],
        stop_event: Event,
    ) -> None:
        if not canaries:
            raise Mt5MaintenanceError(
                "MT5 update cannot be promoted without a broker canary"
            )
        candidate_release = Mt5TemplateRelease(
            prepared.target_terminal_sha256,
            prepared.target_code_manifest_sha256,
            hashlib.sha256(
                (
                    f"{prepared.target_terminal_sha256}:"
                    f"{prepared.target_code_manifest_sha256}"
                ).encode("ascii")
            ).hexdigest(),
        )
        candidate_rotator = self.rotator.for_source_terminal(
            prepared.root / "terminal64.exe"
        )
        capture = self._capture_callback(stop_event)
        golden_release = self._current_release()
        validated: list[_Canary] = []
        try:
            for canary in canaries:
                self._require_not_stopped(stop_event)
                candidate_rotator.rotate_one(
                    canary.connection_id,
                    candidate_release,
                    force=True,
                    verified_update_callback=capture,
                    verified_update_required=self.pending_update_store is not None,
                )
                if not candidate_rotator.matches_target(
                    canary.connection_id,
                    candidate_release,
                ):
                    raise Mt5MaintenanceError(
                        "MT5 candidate canary release changed"
                    )
                validated.append(canary)
        except Exception as exc:
            rollback_failed = False
            for canary in reversed(validated):
                try:
                    self.rotator.rotate_one(
                        canary.connection_id,
                        golden_release,
                        force=True,
                        verified_update_callback=capture,
                        verified_update_required=(
                            self.pending_update_store is not None
                        ),
                    )
                    if not self.rotator.matches_target(
                        canary.connection_id,
                        golden_release,
                    ):
                        raise Mt5MaintenanceError(
                            "MT5 candidate rollback release changed"
                        )
                except Exception:
                    rollback_failed = True
                    logger.error(
                        "MT5 candidate rollback failed (connection=%s)",
                        canary.connection_id,
                    )
            if rollback_failed:
                raise Mt5MaintenanceError(
                    "MT5 candidate verification and rollback failed"
                ) from exc
            raise

    def _promote_pending_updates(
        self,
        canaries: tuple[_Canary, ...],
        stop_event: Event,
    ) -> tuple[Mt5TemplateRelease, int, bool]:
        current = self._current_release()
        discarded_pool_slots = 0
        pool_synchronized = False
        while True:
            receipts = self._pending_receipt_chain(current)
            if not receipts:
                return current, discarded_pool_slots, pool_synchronized
            prepared: PreparedMt5Template | None = None
            committed = False
            try:
                prepared = self._prepare_candidate(receipts[0], stop_event)
                for receipt in receipts[1:]:
                    prepared = self._advance_candidate(
                        prepared,
                        receipt,
                        stop_event,
                    )
                self._validate_candidate_on_canaries(
                    prepared,
                    canaries,
                    stop_event,
                )
                self._require_not_stopped(stop_event)
                # Keep the shared template lock from golden publication through
                # synchronous pool replacement. A concurrent provision can see
                # either the complete old pair or the complete new pair, never
                # a new golden beside a claimable slot from the prior release.
                with self.template_lock:
                    digest = self.template_manager.commit_prepared_update(prepared)
                    committed = True
                    if self.instance_pool is not None:
                        try:
                            discarded_pool_slots += (
                                self.instance_pool.accept_template_rotation(
                                    digest,
                                    replenish=True,
                                    stop_event=stop_event,
                                )
                            )
                            pool_synchronized = True
                        except Exception:
                            if stop_event.is_set():
                                raise
                            # Golden publication is the point of no return.
                            # Fleet convergence must continue even if pool
                            # replenishment is temporarily unavailable.
                            logger.exception(
                                "MT5 pool rebuild failed after golden commit"
                            )
                            pool_synchronized = False
            except Mt5TemplateRecoveryRequired as exc:
                # The candidate/updater roots may still be live or the golden
                # publication may be between atomic moves. Only the manager's
                # next recovery pass is allowed to inspect or delete them.
                raise Mt5MaintenanceError(
                    "MT5 template rotation requires recovery"
                ) from exc
            except Exception as exc:
                if not committed and prepared is not None:
                    try:
                        self.template_manager.discard_prepared_update(prepared)
                    except Exception:
                        logger.exception("MT5 candidate cleanup is incomplete")
                if isinstance(exc, Mt5MaintenanceError):
                    raise
                raise Mt5MaintenanceError(
                    "MT5 candidate verification failed"
                ) from exc
            # Do not rescan the entire store after golden publication. A
            # newly corrupt, unrelated receipt must not strand the fleet on
            # the old release. Duplicate equivalent receipts are idempotently
            # retired on the next healthy pass.
            for receipt in receipts:
                self._store_complete(receipt.receipt_id)
            current = self._current_release()

    def _require_fleet_release(self, target: Mt5TemplateRelease) -> None:
        if not self.instances_root.exists():
            return
        for candidate in self.instances_root.iterdir():
            try:
                connection_id = canonical_uuid(candidate.name)
            except ValueError:
                continue
            root = self.rotator._instance_root(connection_id)
            state = read_json(root / "state" / "instance.json", {})
            if state.get("status") != "provisioned":
                continue
            if not self.rotator.matches_target(connection_id, target):
                raise Mt5MaintenanceError("MT5 fleet release postcondition failed")

    def _reconcile_after_pending_store_failure(
        self,
        stop_event: Event,
    ) -> None:
        """Converge pool/fleet to an already valid golden without reading receipts."""

        self._require_not_stopped(stop_event)
        current = self._current_release()
        pool_failed = False
        if self.instance_pool is not None:
            try:
                self.instance_pool.accept_template_rotation(
                    current.terminal_sha256,
                    replenish=True,
                    stop_event=stop_event,
                )
                pool_failed = (
                    self.instance_pool.ready_count()
                    < self.instance_pool.target_size
                )
            except Exception:
                if stop_event.is_set():
                    raise
                logger.exception(
                    "MT5 pool recovery failed while reconciling golden fleet"
                )
                pool_failed = True
        self.rotator.rotate_all(
            current,
            stop_event,
            verified_update_callback=self._capture_callback(stop_event),
            verified_update_required=self.pending_update_store is not None,
        )
        self._require_fleet_release(current)
        if pool_failed:
            raise Mt5MaintenanceError("MT5 pool postcondition failed")

    def run_once(self, stop_event: Event) -> Mt5MaintenanceReport:
        try:
            return self._run_once(stop_event)
        except Mt5PendingUpdateStoreError as exc:
            try:
                self._reconcile_after_pending_store_failure(stop_event)
            except Exception as recovery_exc:
                raise Mt5MaintenanceError(
                    "MT5 pending store and fleet recovery failed"
                ) from recovery_exc
            raise Mt5MaintenanceError(
                "MT5 pending update store is unavailable"
            ) from exc

    def _run_once(self, stop_event: Event) -> Mt5MaintenanceReport:
        self.template_manager.recover_interrupted_rotation()
        recovery = self.rotator.recover_incomplete(
            verified_update_callback=self._capture_callback(stop_event),
            verified_update_required=self.pending_update_store is not None,
        )
        if recovery.failed:
            raise Mt5MaintenanceError("MT5 rotation recovery is incomplete")
        if stop_event.is_set():
            raise Mt5MaintenanceError("MT5 maintenance was interrupted")

        previous = self._current_release()
        checked: list[str] = []
        canaries = self._canaries(previous)
        if not canaries:
            logger.warning(
                "MT5 release probe skipped: no provisioned credentialed account"
            )
        for canary in canaries:
            if stop_event.is_set():
                raise Mt5MaintenanceError("MT5 maintenance was interrupted")
            if not self.rotator.matches_target(canary.connection_id, previous):
                # A daytime LiveUpdate may already have moved this canary to a
                # durably captured target. Do not downgrade it just to probe the
                # same release again; the candidate pass below performs the
                # full restart/login/read-only gate on every broker.
                if self._matches_applicable_pending_target(canary, previous):
                    checked.append(canary.connection_id)
                    continue
                self.rotator.rotate_one(
                    canary.connection_id,
                    previous,
                    force=True,
                    verified_update_callback=self._capture_callback(stop_event),
                    verified_update_required=self.pending_update_store is not None,
                )
            self._probe(canary, stop_event)
            checked.append(canary.connection_id)

        current, discarded, pool_synchronized = self._promote_pending_updates(
            canaries,
            stop_event,
        )
        ready = 0
        pool_failed = False
        if self.instance_pool is not None:
            try:
                if not pool_synchronized:
                    discarded += self.instance_pool.accept_template_rotation(
                        current.terminal_sha256,
                        replenish=True,
                        stop_event=stop_event,
                    )
                ready = self.instance_pool.ready_count()
                pool_failed = ready < self.instance_pool.target_size
            except Exception:
                if stop_event.is_set():
                    raise Mt5MaintenanceError(
                        "MT5 maintenance was interrupted"
                    )
                logger.exception("MT5 pool convergence failed")
                pool_failed = True

        try:
            fleet: Mt5FleetRotationReport = self.rotator.rotate_all(
                current,
                stop_event,
                verified_update_callback=self._capture_callback(stop_event),
                verified_update_required=self.pending_update_store is not None,
            )
        except Mt5InstanceRotationError as exc:
            raise Mt5MaintenanceError("MT5 fleet rotation failed") from exc
        self._require_fleet_release(current)
        if pool_failed:
            raise Mt5MaintenanceError("MT5 pool postcondition failed")
        logger.info(
            "MT5 maintenance pass complete (changed=%s, checked=%s, migrated=%s, pool_ready=%s)",
            previous != current,
            len(checked),
            len(fleet.migrated),
            ready,
        )
        return Mt5MaintenanceReport(
            previous,
            current,
            tuple(checked),
            fleet.migrated,
            fleet.already_current,
            discarded,
            ready,
        )
