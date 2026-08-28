from __future__ import annotations

import gc
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from worker.atomic_file import durable_replace

from ..interactive_identity import (
    InteractiveIdentityError,
    verified_interactive_session_present,
    verify_interactive_task_identity,
    verify_local_standard_interactive_user,
)
from ..provisioning.mt5_instance import (
    InstanceProvisioner,
    MT5_GENERATED_EXAMPLE_DIRS,
)
from ..provisioning.mt5_update_store import (
    MAX_UPDATE_BUNDLE_BYTES,
    MAX_UPDATE_BUNDLE_FILE_BYTES,
    MAX_UPDATE_BUNDLE_FILES,
    Mt5PendingUpdateStoreError,
    Mt5UpdateRelease,
    Mt5VerifiedUpdateBundle,
    UPDATER_ONLY_CONFIG_BYTES,
    VENDOR_UPDATE_PAYLOAD_NAME,
    VERIFIED_UPDATE_METADATA_NAME,
    load_verified_update_bundle,
    mark_update_bundle_healthy,
    seal_applied_update_bundle,
    stage_verified_update_bundle,
    write_updater_only_config,
)
from ..provisioning.secret_store import WindowsSecretStore
from ..state_store import atomic_json

logger = logging.getLogger(__name__)


class NativeMt5Error(RuntimeError):
    """Sanitized native-terminal failure; never contains credentials."""


@dataclass(frozen=True)
class NativeMt5Status:
    pid: int
    account: dict[str, Any]
    heartbeat: dict[str, Any]
    files_path: Path
    requested_server: str | None = None
    effective_server: str | None = None


@dataclass(frozen=True)
class NativeMt5UpdateRecovery:
    """Sanitized result of reconciling pre-health LiveUpdate archives."""

    discarded_staged: int = 0
    sealed_applied: int = 0
    pending_health: int = 0


@dataclass(frozen=True)
class _LiveUpdateCandidate:
    pid: int
    executable: Path
    arguments: tuple[str, ...]


@dataclass(frozen=True)
class _AuthFailureMonitor:
    task: str
    pid: int
    creation_time_unix_ms: int
    helper: Path
    request: Path
    result: Path
    launcher: Path


class NativeMt5Runtime:
    """Launch an isolated MT5 terminal with the read-only MQL5 file bridge.

    This route deliberately does not import the MetaTrader5 Python wheel.  It is
    compatible with terminal builds whose Python IPC is temporarily broken.
    """

    _MANAGED_CHART_PROFILE = "TradeJournal"
    _CACHED_SYMBOL_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{2,63}")
    _BOOTSTRAP_BASE_SYMBOLS = (
        "EURUSD",
        "GBPUSD",
        "USDJPY",
        "USDCHF",
        "AUDUSD",
        "USDCAD",
        "NZDUSD",
        "XAUUSD",
    )
    # [StartUp].Script is chart-bound. MT5 does not execute the script when the
    # requested chart symbol is absent from the broker catalogue, so Discovery
    # cannot inspect that catalogue until some valid chart has opened.
    _DISCOVERY_STARTUP_SUFFIXES = (
        "",
        ".x",
        ".raw",
        "m",
        ".m",
        ".a",
        ".pro",
        ".ecn",
        "-ECN",
    )
    _DISCOVERY_RESOLUTIONS = frozenset(
        ("exact", "currency_pair", "name_related", "fallback")
    )
    _LIVE_UPDATE_FREE_SPACE_RESERVE_BYTES = 64 * 1024 * 1024
    _MAX_LIVE_UPDATE_HOPS = 8
    _LIVE_UPDATE_CHAIN_TIMEOUT_SECONDS = 15 * 60.0
    def __init__(
        self,
        instance_root: Path,
        connection_id: str,
        symbol_hint_root: Path | None = None,
    ) -> None:
        self.root = instance_root.resolve()
        self.connection_id = connection_id
        self.terminal_root = self.root / "terminal"
        self.terminal = self.terminal_root / "terminal64.exe"
        self.files = self.terminal_root / "MQL5" / "Files" / "TradeJournal"
        self.state = self.root / "state"
        self.symbol_hint_root = (
            Path(symbol_hint_root).resolve()
            if symbol_hint_root is not None
            else (self.root.parent / ".broker-symbol-hints").resolve()
        )
        self._process: subprocess.Popen[bytes] | None = None
        self._interactive_task: str | None = None
        self._last_symbol: str | None = None
        self._cancel_check: Callable[[], None] | None = None
        self._pending_live_update: _LiveUpdateCandidate | None = None
        self._verified_vendor_update_callback: (
            Callable[[Path, Path, Path, str], str | None] | None
        ) = None
        self._verified_vendor_update_callback_required = False
        self._verified_interactive_user: str | None = None
        self._readiness_not_before: datetime | None = None
        self._pending_verified_vendor_updates: list[
            tuple[Path, Path, Path, str]
        ] = []

    def set_cancel_check(self, check: Callable[[], None] | None) -> None:
        self._cancel_check = check

    def set_verified_vendor_update_callback(
        self,
        callback: Callable[[Path, Path, Path, str], str | None] | None,
        *,
        required: bool = False,
    ) -> None:
        self._verified_vendor_update_callback = callback
        self._verified_vendor_update_callback_required = required

    @staticmethod
    def _vendor_update_pin_matches(
        state: dict[str, Any],
        release: Mt5UpdateRelease,
        signer_subject: str,
    ) -> bool:
        vendor_update = state.get("vendor_update")
        return (
            state.get("terminal_sha256") == release.terminal_sha256
            and state.get("template_code_manifest_sha256")
            == release.code_manifest_sha256
            and isinstance(vendor_update, dict)
            and vendor_update.get("schema_version") == 1
            and vendor_update.get("signer_subject") == signer_subject
            and vendor_update.get("terminal_sha256")
            == release.terminal_sha256
            and vendor_update.get("code_manifest_sha256")
            == release.code_manifest_sha256
            and type(vendor_update.get("verified_at_unix_ms")) is int
            and vendor_update["verified_at_unix_ms"] > 0
        )

    def _validate_staged_update_state(
        self,
        bundle: Mt5VerifiedUpdateBundle,
        actual_release: Mt5UpdateRelease,
    ) -> tuple[dict[str, Any], bool]:
        state_path = self.state / "instance.json"
        if (
            InstanceProvisioner._is_reparse_point(state_path)
            or not state_path.is_file()
        ):
            raise NativeMt5Error("mt5_update_startup_state_invalid")
        state = self._read_json(state_path)
        if (
            state is None
            or state.get("connection_id") != self.connection_id
            or state.get("status") != "provisioned"
        ):
            raise NativeMt5Error("mt5_update_startup_state_invalid")
        try:
            actual_assets = (
                InstanceProvisioner._managed_runtime_assets_manifest(
                    self.terminal_root
                )
            )
        except (OSError, ValueError) as exc:
            raise NativeMt5Error("mt5_update_startup_assets_invalid") from exc
        if actual_assets != bundle.managed_assets_manifest_sha256:
            raise NativeMt5Error("mt5_update_startup_assets_invalid")

        recorded_assets = state.get("runtime_assets_manifest_sha256")
        recorded_assets_version = state.get("runtime_assets_manifest_version")
        if recorded_assets is None and recorded_assets_version is None:
            # A first connection can be interrupted before the normal
            # post-heartbeat asset seal.  The private staged receipt contains
            # the pre-update digest, so atomically establish that write-once
            # pin only after the current managed files match it exactly.
            state["runtime_assets_manifest_sha256"] = actual_assets
            state["runtime_assets_manifest_version"] = 1
            try:
                atomic_json(state_path, state)
            except (OSError, ValueError) as exc:
                raise NativeMt5Error(
                    "mt5_update_startup_state_invalid"
                ) from exc
        elif recorded_assets != actual_assets or recorded_assets_version != 1:
            raise NativeMt5Error("mt5_update_startup_assets_invalid")

        source_pinned = (
            state.get("terminal_sha256")
            == bundle.source_release.terminal_sha256
            and state.get("template_code_manifest_sha256")
            == bundle.source_release.code_manifest_sha256
        )
        target_pinned = self._vendor_update_pin_matches(
            state,
            actual_release,
            bundle.signer_subject,
        )
        if actual_release == bundle.source_release:
            if not source_pinned:
                raise NativeMt5Error("mt5_update_startup_pin_invalid")
            return state, False
        if not source_pinned and not target_pinned:
            raise NativeMt5Error("mt5_update_startup_pin_invalid")
        return state, target_pinned

    def recover_interrupted_live_updates(self) -> NativeMt5UpdateRecovery:
        """Resolve staged archives before startup instance reconciliation.

        A staged archive is written before LiveUpdate mutates the terminal. If
        the service stops after that point, an unchanged source is discarded;
        a changed terminal is accepted only when its MetaQuotes signer and the
        write-once TradeJournal asset pin still match. The resulting applied
        receipt deliberately remains pending until a later heartbeat passes.
        """

        if not self.state.exists():
            return NativeMt5UpdateRecovery()
        if (
            InstanceProvisioner._is_reparse_point(self.state)
            or not self.state.is_dir()
        ):
            raise NativeMt5Error("mt5_update_startup_state_invalid")
        working_sets = self._interrupted_live_update_working_sets()
        if working_sets:
            # The service can die while the copied MetaQuotes updater is still
            # running independently. Freeze that exact executable and any
            # terminal it relaunched before interpreting source/target bytes.
            self._terminate_live_update_working_processes(working_sets)
            if not self.stop(timeout=30.0):
                raise NativeMt5Error("mt5_update_startup_process_stop_failed")
            for working in working_sets:
                try:
                    shutil.rmtree(working)
                except OSError as exc:
                    raise NativeMt5Error(
                        "mt5_update_startup_working_cleanup_failed"
                    ) from exc
        try:
            candidates = sorted(
                self.state.glob("live-update-*"),
                key=lambda path: path.name,
            )
        except OSError as exc:
            raise NativeMt5Error("mt5_update_startup_scan_failed") from exc

        discarded = 0
        sealed = 0
        pending_health = 0
        actual_release: Mt5UpdateRelease | None = None
        for candidate in candidates:
            if (
                InstanceProvisioner._is_reparse_point(candidate)
                or not candidate.is_dir()
            ):
                raise NativeMt5Error("mt5_update_startup_bundle_invalid")
            metadata = candidate / VERIFIED_UPDATE_METADATA_NAME
            if not metadata.is_file():
                # No durable source-release seal was established. Once the
                # exact updater is stopped this directory is unusable and can
                # only be stale private scratch data.
                try:
                    shutil.rmtree(candidate)
                except OSError as exc:
                    raise NativeMt5Error(
                        "mt5_update_startup_bundle_cleanup_failed"
                    ) from exc
                continue
            try:
                bundle = load_verified_update_bundle(candidate)
                if bundle.source_connection_id != self.connection_id:
                    raise NativeMt5Error(
                        "mt5_update_startup_identity_mismatch"
                    )
                if bundle.target_release is not None:
                    if actual_release is None:
                        actual_release = Mt5UpdateRelease.from_terminal_root(
                            self.terminal_root
                        )
                    if bundle.target_release == actual_release:
                        # The account must pass a fresh heartbeat/read-only
                        # gate before this durable receipt can be published.
                        pending_health += 1
                    continue
                actual_release = Mt5UpdateRelease.from_terminal_root(
                    self.terminal_root
                )
                _state, target_pinned = self._validate_staged_update_state(
                    bundle,
                    actual_release,
                )
                signer = bundle.signer_subject
                if not target_pinned:
                    # A changed terminal tree is not proof that the updater
                    # committed atomically: the service may have died between
                    # replacing terminal64.exe and its companion DLLs. Replay
                    # the immutable signed bundle idempotently and require an
                    # explicit zero exit before creating a target receipt.
                    actual_release = self._resume_staged_live_update(bundle)
                else:
                    observed_signer = self._verify_metaquotes_signature(
                        self.terminal
                    )
                    if observed_signer != signer:
                        raise NativeMt5Error(
                            "mt5_update_startup_signer_mismatch"
                        )
                    recorded_digest = InstanceProvisioner._sha256(
                        self.terminal
                    )
                    if recorded_digest != actual_release.terminal_sha256:
                        raise NativeMt5Error(
                            "mt5_update_startup_pin_invalid"
                        )
                confirmed_release = Mt5UpdateRelease.from_terminal_root(
                    self.terminal_root
                )
                confirmed_state = self._read_json(
                    self.state / "instance.json"
                )
                if (
                    confirmed_release != actual_release
                    or confirmed_state is None
                    or not self._vendor_update_pin_matches(
                        confirmed_state,
                        actual_release,
                        signer,
                    )
                ):
                    raise NativeMt5Error("mt5_update_startup_pin_invalid")
                seal_applied_update_bundle(bundle.root, actual_release)
                sealed += 1
                pending_health += 1
            except NativeMt5Error:
                raise
            except (Mt5PendingUpdateStoreError, OSError, ValueError) as exc:
                raise NativeMt5Error(
                    "mt5_update_startup_recovery_failed"
                ) from exc
        return NativeMt5UpdateRecovery(
            discarded_staged=discarded,
            sealed_applied=sealed,
            pending_health=pending_health,
        )

    def _resume_staged_live_update(
        self,
        bundle: Mt5VerifiedUpdateBundle,
    ) -> Mt5UpdateRelease:
        if bundle.target_release is not None or bundle.health_verified:
            raise NativeMt5Error("mt5_update_startup_bundle_invalid")
        if self._verify_metaquotes_signature(bundle.updater) != bundle.signer_subject:
            raise NativeMt5Error("mt5_update_startup_signer_mismatch")
        payload_bytes = sum(int(entry["size"]) for entry in bundle.files)
        try:
            free_bytes = shutil.disk_usage(self.state).free
        except OSError as exc:
            raise NativeMt5Error("mt5_update_storage_unavailable") from exc
        if (
            payload_bytes > MAX_UPDATE_BUNDLE_BYTES
            or free_bytes
            < payload_bytes + self._LIVE_UPDATE_FREE_SPACE_RESERVE_BYTES
        ):
            raise NativeMt5Error("mt5_update_storage_insufficient")

        working: Path | None = None
        try:
            working = Path(
                tempfile.mkdtemp(
                    prefix=".live-update-working-recovery-",
                    dir=self.state,
                )
            )
            WindowsSecretStore.restrict_acl(working)
            for entry in bundle.files:
                name = str(entry["name"])
                self._copy_bounded_verified_file(
                    bundle.root / name,
                    working / name,
                    int(entry["size"]),
                    str(entry["sha256"]),
                )
            (working / "temp").mkdir()
            WindowsSecretStore.restrict_acl(working / "temp")
            InstanceProvisioner._sync_tree(working)
            working_updater = working / bundle.updater.name
            working_config = working / bundle.updater_config.name
            command = [
                str(working_updater),
                "/update",
                f"/path:{self.terminal_root}",
                "/portable",
                f"/config:{working_config}",
            ]
            process = subprocess.Popen(
                command,
                cwd=working,
                close_fds=True,
            )
            deadline = time.monotonic() + 240.0
            try:
                while process.poll() is None and time.monotonic() < deadline:
                    self._check_cancelled()
                    time.sleep(0.5)
                if process.poll() is None:
                    process.kill()
                    process.wait(5)
                    raise NativeMt5Error("mt5_update_startup_timeout")
                if process.returncode not in (0, None):
                    raise NativeMt5Error("mt5_update_startup_failed")
            except Exception:
                if process.poll() is None:
                    process.kill()
                    process.wait(5)
                raise

            if not self.stop(timeout=30.0):
                raise NativeMt5Error(
                    "mt5_update_startup_process_stop_failed"
                )
            self._remove_generated_example_code()
            try:
                managed_assets = (
                    InstanceProvisioner._managed_runtime_assets_manifest(
                        self.terminal_root
                    )
                )
            except (OSError, ValueError) as exc:
                raise NativeMt5Error(
                    "mt5_update_startup_assets_invalid"
                ) from exc
            if managed_assets != bundle.managed_assets_manifest_sha256:
                raise NativeMt5Error("mt5_update_startup_assets_invalid")
            terminal_signer = self._verify_metaquotes_signature(self.terminal)
            if terminal_signer != bundle.signer_subject:
                raise NativeMt5Error(
                    "mt5_update_startup_signer_mismatch"
                )
            try:
                recorded_digest = (
                    InstanceProvisioner.record_verified_vendor_update(
                        self.root,
                        self.connection_id,
                        terminal_signer,
                    )
                )
                target_release = Mt5UpdateRelease.from_terminal_root(
                    self.terminal_root
                )
            except (OSError, ValueError, Mt5PendingUpdateStoreError) as exc:
                raise NativeMt5Error(
                    "mt5_update_startup_pin_invalid"
                ) from exc
            if recorded_digest != target_release.terminal_sha256:
                raise NativeMt5Error("mt5_update_startup_pin_invalid")
            return target_release
        finally:
            if working is not None:
                shutil.rmtree(working, ignore_errors=True)

    def _interrupted_live_update_working_sets(self) -> tuple[Path, ...]:
        try:
            candidates = sorted(
                self.state.glob(".live-update-working-*"),
                key=lambda path: path.name,
            )
        except OSError as exc:
            raise NativeMt5Error("mt5_update_startup_scan_failed") from exc
        for candidate in candidates:
            if (
                candidate.parent != self.state
                or InstanceProvisioner._is_reparse_point(candidate)
                or not candidate.is_dir()
            ):
                raise NativeMt5Error("mt5_update_startup_working_invalid")
        return tuple(candidates)

    def _terminate_live_update_working_processes(
        self,
        working_sets: tuple[Path, ...],
    ) -> None:
        try:
            import psutil
        except ImportError as exc:
            raise NativeMt5Error(
                "mt5_update_startup_process_scan_failed"
            ) from exc
        expected = {
            os.path.normcase(
                os.fspath((working / "terminal64.exe").resolve())
            )
            for working in working_sets
        }
        matches = []
        for process in psutil.process_iter(("pid", "exe")):
            try:
                executable = process.info.get("exe")
                if executable and os.path.normcase(
                    os.fspath(Path(str(executable)).resolve())
                ) in expected:
                    matches.append(process)
            except (psutil.AccessDenied, psutil.NoSuchProcess, OSError, ValueError):
                continue
        try:
            controlled = []
            for process in matches:
                try:
                    process.terminate()
                    controlled.append(process)
                except psutil.NoSuchProcess:
                    continue
            _, alive = psutil.wait_procs(controlled, timeout=15.0)
            killed = []
            for process in alive:
                try:
                    process.kill()
                    killed.append(process)
                except psutil.NoSuchProcess:
                    continue
            if killed:
                _, still_alive = psutil.wait_procs(killed, timeout=5.0)
                if still_alive:
                    raise NativeMt5Error(
                        "mt5_update_startup_process_stop_failed"
                    )
        except NativeMt5Error:
            raise
        except (psutil.AccessDenied, OSError) as exc:
            raise NativeMt5Error(
                "mt5_update_startup_process_stop_failed"
            ) from exc

        for working in working_sets:
            if self._running_executable_pids(working / "terminal64.exe"):
                raise NativeMt5Error("mt5_update_startup_process_stop_failed")

    def _discard_pending_verified_vendor_updates(self) -> None:
        pending, self._pending_verified_vendor_updates = (
            self._pending_verified_vendor_updates,
            [],
        )
        for bundle_root, _updater, _config, _signer in pending:
            try:
                bundle = load_verified_update_bundle(bundle_root)
            except Mt5PendingUpdateStoreError:
                shutil.rmtree(bundle_root, ignore_errors=True)
                continue
            # An applied bundle is durable recovery material.  A later resume
            # must be allowed to prove account health and retry its callback.
            if bundle.target_release is None:
                shutil.rmtree(bundle_root, ignore_errors=True)

    def _recover_pending_verified_vendor_updates(self) -> None:
        """Recover sealed bundles left between update, health and capture."""

        if self._verified_vendor_update_callback is None or not self.state.is_dir():
            return
        known = {
            bundle_root.resolve()
            for bundle_root, _updater, _config, _signer
            in self._pending_verified_vendor_updates
        }
        try:
            candidates = sorted(
                self.state.glob("live-update-*"),
                key=lambda path: path.name,
            )
        except OSError as exc:
            raise NativeMt5Error("mt5_update_orphan_scan_failed") from exc
        healthy: list[Mt5VerifiedUpdateBundle] = []
        awaiting_health: list[Mt5VerifiedUpdateBundle] = []
        for candidate in candidates:
            if candidate.resolve() in known:
                continue
            if (
                InstanceProvisioner._is_reparse_point(candidate)
                or not candidate.is_dir()
            ):
                raise NativeMt5Error("mt5_update_orphan_invalid")
            if not (candidate / VERIFIED_UPDATE_METADATA_NAME).is_file():
                # A process kill can leave a pre-seal working directory. It is
                # not trusted or published, but it must not block the account.
                continue
            try:
                bundle = load_verified_update_bundle(candidate)
            except Mt5PendingUpdateStoreError as exc:
                raise NativeMt5Error("mt5_update_orphan_invalid") from exc
            if bundle.source_connection_id != self.connection_id:
                raise NativeMt5Error("mt5_update_orphan_identity_mismatch")
            if bundle.target_release is None:
                # The service stopped before applying this archive. It cannot
                # pass the health gate and is never surfaced to the callback.
                continue
            if bundle.target_release == bundle.source_release:
                # Compatibility with older agents that sealed a successful
                # updater run even when no release bytes changed. A->A is not
                # a graph edge and must never block or reach promotion.
                try:
                    shutil.rmtree(bundle.root)
                except OSError as exc:
                    raise NativeMt5Error(
                        "mt5_update_orphan_cleanup_failed"
                    ) from exc
                continue
            (healthy if bundle.health_verified else awaiting_health).append(bundle)

        selected: list[Mt5VerifiedUpdateBundle] = list(healthy)
        if awaiting_health:
            try:
                actual_release = Mt5UpdateRelease.from_terminal_root(
                    self.terminal_root
                )
            except Mt5PendingUpdateStoreError as exc:
                raise NativeMt5Error(
                    "mt5_update_orphan_target_invalid"
                ) from exc

            # A hard crash can occur after A->B and B->C were sealed but before
            # the final C heartbeat. Reconstruct the one unambiguous reverse
            # chain ending at the actual release; final account health then
            # blesses every signed hop, preserving the path required by an
            # older golden. Unrelated receipts remain deferred for inspection.
            by_target: dict[
                Mt5UpdateRelease,
                dict[tuple[object, ...], list[Mt5VerifiedUpdateBundle]],
            ] = {}
            for bundle in awaiting_health:
                assert bundle.target_release is not None
                identity = (
                    bundle.receipt_id,
                    bundle.source_release,
                    bundle.target_release,
                    bundle.bundle_manifest_sha256,
                    bundle.managed_assets_manifest_sha256,
                    bundle.signer_subject,
                )
                by_target.setdefault(bundle.target_release, {}).setdefault(
                    identity,
                    [],
                ).append(bundle)
            reverse_chain: list[list[Mt5VerifiedUpdateBundle]] = []
            release = actual_release
            visited = {release}
            while True:
                predecessor_groups = list(by_target.get(release, {}).values())
                if not predecessor_groups:
                    break
                if len(predecessor_groups) != 1:
                    raise NativeMt5Error("mt5_update_orphan_chain_ambiguous")
                predecessors = sorted(
                    predecessor_groups[0],
                    key=lambda value: str(value.root),
                )
                predecessor = predecessors[0]
                reverse_chain.append(predecessors)
                release = predecessor.source_release
                if release in visited:
                    raise NativeMt5Error("mt5_update_orphan_chain_invalid")
                visited.add(release)
            selected.extend(
                bundle
                for duplicate_group in reversed(reverse_chain)
                for bundle in duplicate_group
            )
            selected_roots = {
                bundle.root
                for duplicate_group in reverse_chain
                for bundle in duplicate_group
            }
            for bundle in awaiting_health:
                if bundle.root not in selected_roots:
                    logger.warning(
                        "deferred stale MT5 update orphan for %s",
                        self.connection_id,
                    )

        for bundle in selected:
            self._pending_verified_vendor_updates.append(
                (
                    bundle.root,
                    bundle.updater,
                    bundle.updater_config,
                    bundle.signer_subject,
                )
            )
            known.add(bundle.root)

    def _publish_pending_verified_vendor_updates(self) -> None:
        """Promote only updates whose restarted account passed the full health gate."""

        pending, self._pending_verified_vendor_updates = (
            self._pending_verified_vendor_updates,
            [],
        )
        for bundle_root, updater, config, signer in pending:
            captured = False
            try:
                bundle = load_verified_update_bundle(bundle_root)
                if (
                    updater.resolve() != bundle.updater
                    or config.resolve() != bundle.updater_config
                    or signer != bundle.signer_subject
                ):
                    raise Mt5PendingUpdateStoreError(
                        "MT5 update callback metadata does not match its receipt"
                    )
                if not bundle.health_verified:
                    bundle = mark_update_bundle_healthy(bundle_root)
                if self._verified_vendor_update_callback is not None:
                    self._verified_vendor_update_callback(
                        bundle.root,
                        bundle.updater,
                        bundle.updater_config,
                        bundle.signer_subject,
                    )
                    captured = True
            except Exception as exc:
                if self._verified_vendor_update_callback_required:
                    raise NativeMt5Error(
                        "mt5_update_capture_failed"
                    ) from exc
                logger.exception("verified MT5 update capture failed")
            finally:
                if captured:
                    shutil.rmtree(bundle_root, ignore_errors=True)

    def _check_cancelled(self) -> None:
        if self._cancel_check is not None:
            self._cancel_check()

    def install_expert(
        self,
        expert_binary: Path,
        history_mode: str = "new_only",
        history_from: datetime | None = None,
    ) -> Path:
        if not expert_binary.is_file() or expert_binary.suffix.casefold() != ".ex5":
            raise NativeMt5Error("expert_binary_missing")
        if history_mode not in ("new_only", "from_date", "all_available"):
            raise NativeMt5Error("invalid_history_mode")
        destination = (
            self.terminal_root
            / "MQL5"
            / "Experts"
            / "TradeJournal"
            / "TradeJournalBridge.ex5"
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        source_digest = self._sha256(expert_binary)
        temporary_expert = destination.with_suffix(".ex5.tmp")
        try:
            shutil.copy2(expert_binary, temporary_expert)
            # Windows os.fsync maps to _commit and therefore needs a writable descriptor.
            with temporary_expert.open("r+b") as handle:
                os.fsync(handle.fileno())
            if self._sha256(temporary_expert) != source_digest:
                raise NativeMt5Error("expert_copy_integrity_failed")
            durable_replace(temporary_expert, destination)
        finally:
            temporary_expert.unlink(missing_ok=True)
        discovery = (
            self.terminal_root
            / "MQL5"
            / "Scripts"
            / "TradeJournal"
            / "TradeJournalDiscovery.ex5"
        )
        if not discovery.is_file():
            raise NativeMt5Error("discovery_script_missing")
        self.files.mkdir(parents=True, exist_ok=True)
        connection_tmp = self.files / "connection_id.tmp"
        self._write_text_durable(connection_tmp, self.connection_id, "utf-8")
        durable_replace(connection_tmp, self.files / "connection_id")
        mode_tmp = self.files / "history_mode.tmp"
        self._write_text_durable(mode_tmp, history_mode, "utf-8")
        durable_replace(mode_tmp, self.files / "history_mode")
        from_tmp = self.files / "history_from_unix.tmp"
        from_unix = 0
        if history_mode == "from_date" and history_from is None:
            raise NativeMt5Error("history_from_missing")
        if history_mode in ("from_date", "new_only") and history_from is not None:
            from_unix = int(history_from.timestamp())
            if from_unix <= 0:
                raise NativeMt5Error("history_from_invalid")
        self._write_text_durable(from_tmp, str(from_unix), "utf-8")
        durable_replace(from_tmp, self.files / "history_from_unix")
        return destination

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _bounded_source_identity(path: Path) -> tuple[int, str]:
        try:
            before = path.stat()
            size = before.st_size
            if (
                InstanceProvisioner._is_reparse_point(path)
                or not path.is_file()
                or size <= 0
                or size > MAX_UPDATE_BUNDLE_FILE_BYTES
            ):
                raise NativeMt5Error("mt5_update_source_size_invalid")
            digest = hashlib.sha256()
            observed = 0
            with path.open("rb") as handle:
                while observed < size:
                    chunk = handle.read(min(1024 * 1024, size - observed))
                    if not chunk:
                        raise NativeMt5Error("mt5_update_source_changed")
                    observed += len(chunk)
                    digest.update(chunk)
                if handle.read(1):
                    raise NativeMt5Error("mt5_update_source_changed")
            after = path.stat()
            if (
                after.st_size != size
                or after.st_mtime_ns != before.st_mtime_ns
            ):
                raise NativeMt5Error("mt5_update_source_changed")
            return size, digest.hexdigest()
        except NativeMt5Error:
            raise
        except OSError as exc:
            raise NativeMt5Error("mt5_update_source_unavailable") from exc

    @staticmethod
    def _copy_bounded_verified_file(
        source: Path,
        destination: Path,
        expected_size: int,
        expected_sha256: str,
    ) -> None:
        digest = hashlib.sha256()
        written = 0
        try:
            with source.open("rb") as source_handle, destination.open(
                "xb"
            ) as destination_handle:
                while written < expected_size:
                    chunk = source_handle.read(
                        min(1024 * 1024, expected_size - written)
                    )
                    if not chunk:
                        raise NativeMt5Error("mt5_update_source_changed")
                    destination_handle.write(chunk)
                    digest.update(chunk)
                    written += len(chunk)
                if source_handle.read(1):
                    raise NativeMt5Error("mt5_update_source_changed")
                destination_handle.flush()
                os.fsync(destination_handle.fileno())
            if (
                written != expected_size
                or digest.hexdigest() != expected_sha256
            ):
                raise NativeMt5Error("mt5_update_copy_integrity_failed")
        except Exception:
            destination.unlink(missing_ok=True)
            raise

    @staticmethod
    def _write_text_durable(path: Path, content: str, encoding: str) -> None:
        with path.open("w", encoding=encoding, newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())

    def _install_bridge_template(self, symbol: str) -> Path:
        source = self.terminal_root / "Profiles" / "Templates" / "ADX.tpl"
        try:
            raw = source.read_bytes()
            encoding = "utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
            lines = raw.decode(encoding).splitlines()
        except (OSError, UnicodeDecodeError) as exc:
            raise NativeMt5Error("chart_template_missing") from exc
        if "<chart>" not in lines or "<window>" not in lines or "<expert>" in lines:
            raise NativeMt5Error("chart_template_invalid")
        for index, line in enumerate(lines):
            if line.startswith("symbol="):
                lines[index] = f"symbol={symbol}"
                break
        else:
            raise NativeMt5Error("chart_template_invalid")
        expert = [
            "<expert>",
            "name=TradeJournalBridge",
            r"path=Experts\TradeJournal\TradeJournalBridge.ex5",
            "expertmode=0",
            "<inputs>",
            "InpTimerSeconds=2",
            "InpBackfillHours=168",
            "InpSnapshotHistoryHours=87600",
            "InpCandleBars=200",
            "</inputs>",
            "</expert>",
            "",
        ]
        lines[lines.index("<window>") : lines.index("<window>")] = expert
        self.files.mkdir(parents=True, exist_ok=True)
        destination = self.files / "TradeJournalBridge.tpl"
        temporary = destination.with_suffix(".tpl.tmp")
        # MT5 chart templates are Unicode text files and require a BOM when produced outside the
        # terminal. Python's utf-16 codec writes that BOM deterministically.
        # Disable platform newline translation: on Windows, write_text() would otherwise turn
        # every explicit CRLF into CRCRLF and MT5 would silently skip structured template blocks.
        with temporary.open("w", encoding="utf-16", newline="") as handle:
            handle.write("\r\n".join(lines) + "\r\n")
            handle.flush()
            os.fsync(handle.fileno())
        durable_replace(temporary, destination)
        return destination

    def _write_symbol_preference(self, preferred: str) -> Path:
        if (
            not preferred
            or preferred != preferred.strip()
            or len(preferred) > 64
            or any(ord(character) < 32 for character in preferred)
        ):
            raise NativeMt5Error("invalid_startup_symbol")
        self.files.mkdir(parents=True, exist_ok=True)
        destination = self.files / "symbol-preference.txt"
        temporary = self.files / "symbol-preference.tmp"
        self._write_text_durable(temporary, preferred, "utf-8")
        durable_replace(temporary, destination)
        return destination

    @classmethod
    def _discovery_startup_symbols(
        cls,
        preferred: str,
        cached: str | None,
        shared: str | None = None,
    ) -> tuple[str, ...]:
        candidates: list[str] = []
        seen: set[str] = set()
        for value in (
            cached,
            shared,
            *(preferred + suffix for suffix in cls._DISCOVERY_STARTUP_SUFFIXES),
        ):
            if not value or len(value) > 64 or any(ord(character) < 32 for character in value):
                continue
            folded = value.casefold()
            if folded in seen:
                continue
            seen.add(folded)
            candidates.append(value)
        return tuple(candidates)

    @staticmethod
    def _valid_symbol_hint(value: object) -> bool:
        return (
            isinstance(value, str)
            and bool(value)
            and value == value.strip()
            and len(value) <= 64
            and not any(ord(character) < 32 for character in value)
        )

    def _broker_symbol_hint_path(self, server: str) -> Path:
        digest = hashlib.sha256(server.casefold().encode("utf-8")).hexdigest()
        return self.symbol_hint_root / f"{digest}.json"

    def _broker_symbol_hint(self, server: str) -> str | None:
        root = self.symbol_hint_root
        if not root.exists():
            return None
        if self._is_reparse_point(root) or not root.is_dir():
            raise NativeMt5Error("broker_symbol_hint_invalid")
        path = self._broker_symbol_hint_path(server)
        if not path.exists():
            return None
        if self._is_reparse_point(path) or not path.is_file():
            raise NativeMt5Error("broker_symbol_hint_invalid")
        try:
            if path.stat().st_size <= 0 or path.stat().st_size > 4096:
                raise NativeMt5Error("broker_symbol_hint_invalid")
            record = json.loads(path.read_text(encoding="utf-8"))
        except NativeMt5Error:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise NativeMt5Error("broker_symbol_hint_invalid") from exc
        if (
            not isinstance(record, dict)
            or set(record) != {
                "schema_version",
                "server",
                "symbol",
                "terminal_build",
                "source_connection_id",
                "observed_at_unix_ms",
            }
            or record.get("schema_version") != 1
            or not isinstance(record.get("server"), str)
            or record["server"].casefold() != server.casefold()
            or not self._valid_symbol_hint(record.get("symbol"))
            or type(record.get("terminal_build")) is not int
            or record["terminal_build"] <= 0
            or not isinstance(record.get("source_connection_id"), str)
            or not record["source_connection_id"]
            or type(record.get("observed_at_unix_ms")) is not int
            or record["observed_at_unix_ms"] <= 0
        ):
            raise NativeMt5Error("broker_symbol_hint_invalid")
        return record["symbol"]

    def _publish_broker_symbol_hint(
        self,
        server: str,
        symbol: str,
        terminal_build: int,
    ) -> Path:
        if not self._valid_symbol_hint(symbol) or terminal_build <= 0:
            raise NativeMt5Error("broker_symbol_hint_invalid")
        root = self.symbol_hint_root
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise NativeMt5Error("broker_symbol_hint_write_failed") from exc
        if self._is_reparse_point(root) or not root.is_dir():
            raise NativeMt5Error("broker_symbol_hint_invalid")
        path = self._broker_symbol_hint_path(server)
        temporary = root / f".{path.stem}.{self.connection_id}.tmp"
        record = {
            "schema_version": 1,
            "server": server,
            "symbol": symbol,
            "terminal_build": terminal_build,
            "source_connection_id": self.connection_id,
            "observed_at_unix_ms": int(time.time() * 1000),
        }
        try:
            self._write_text_durable(
                temporary,
                json.dumps(record, sort_keys=True, separators=(",", ":")),
                "utf-8",
            )
            durable_replace(temporary, path)
            WindowsSecretStore.restrict_shared_service_acl(root)
            WindowsSecretStore.restrict_shared_service_acl(path)
        except Exception as exc:
            temporary.unlink(missing_ok=True)
            if isinstance(exc, NativeMt5Error):
                raise
            raise NativeMt5Error("broker_symbol_hint_write_failed") from exc
        return path

    def _wait_for_discovery_start(self, expected_symbol: str, timeout: float) -> bool:
        output = self.files / "discovery-started.json"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._check_cancelled()
            if output.is_file():
                try:
                    record = json.loads(output.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise NativeMt5Error("broker_symbol_start_invalid") from exc
                if (
                    not isinstance(record, dict)
                    or set(record) != {
                        "schema_version",
                        "connection_id",
                        "chart_symbol",
                        "terminal_build",
                    }
                    or record.get("schema_version") != 1
                    or record.get("connection_id") != self.connection_id
                    or not isinstance(record.get("chart_symbol"), str)
                    or record["chart_symbol"].casefold() != expected_symbol.casefold()
                    or type(record.get("terminal_build")) is not int
                    or record["terminal_build"] <= 0
                ):
                    raise NativeMt5Error("broker_symbol_start_invalid")
                return True
            if self._process is not None and self._process.poll() is not None:
                pids = self._running_terminal_pids()
                if not pids:
                    raise NativeMt5Error("mt5_process_crashed")
            time.sleep(0.25)
        return False

    def _probe_broker_symbol(
        self,
        preferred: str,
        login: int,
        server: str,
        timeout: float,
    ) -> str:
        if (
            not isinstance(preferred, str)
            or not preferred
            or preferred != preferred.strip()
            or type(login) is not int
            or login <= 0
            or not isinstance(server, str)
            or not server
            or server != server.strip()
            or timeout <= 0
        ):
            raise NativeMt5Error("broker_symbol_probe_invalid")
        output = self.files / "discovered-symbol.json"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._check_cancelled()
            if output.is_file():
                try:
                    record = json.loads(output.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise NativeMt5Error("broker_symbol_probe_invalid") from exc
                if not isinstance(record, dict):
                    raise NativeMt5Error("broker_symbol_probe_invalid")
                symbol = record.get("symbol")
                if (
                    record.get("schema_version") != 1
                    or record.get("connection_id") != self.connection_id
                    or type(record.get("login")) is not int
                    or record.get("login") != login
                    or not isinstance(record.get("server"), str)
                    or record["server"].casefold() != server.casefold()
                    or record.get("requested_symbol") != preferred
                    or record.get("resolution") not in self._DISCOVERY_RESOLUTIONS
                    or type(record.get("catalog_total")) is not int
                    or record["catalog_total"] <= 0
                    or record.get("synchronized") is not True
                    or record.get("terminal_connected") is not True
                    or type(record.get("account_trade_allowed")) is not bool
                    or type(record.get("terminal_build")) is not int
                    or record["terminal_build"] <= 0
                    or not isinstance(symbol, str)
                    or not symbol
                    or symbol != symbol.strip()
                    or len(symbol) > 64
                    or any(ord(character) < 32 for character in symbol)
                ):
                    raise NativeMt5Error("broker_symbol_probe_invalid")
                if record["account_trade_allowed"]:
                    raise NativeMt5Error("investor_readonly_not_verified")
                self._publish_broker_symbol_hint(
                    server,
                    symbol,
                    record["terminal_build"],
                )
                return symbol
            if self._process is not None and self._process.poll() is not None:
                raise NativeMt5Error("mt5_process_crashed")
            time.sleep(0.25)
        raise NativeMt5Error("broker_symbol_probe_failed")

    def _cached_broker_symbol(
        self,
        login: int,
        server: str,
        preferred: str,
    ) -> str | None:
        """Read an exact chart symbol from MT5's broker-scoped account cache.

        The first authenticated terminal phase may create ``selected-<login>.dat``.
        This private, broker-dependent cache is used only as a best-effort chart
        hint.  A missing/unrecognizable token is not a failure: the caller falls
        back to the configured symbol and the in-terminal Discovery script is
        always the authoritative verifier before the bridge is installed.
        """
        if not isinstance(login, int) or isinstance(login, bool) or login <= 0:
            raise NativeMt5Error("invalid_login")
        if (
            not server
            or server != server.strip()
            or len(server) > 128
            or any(character in server for character in "\\/\r\n\0")
        ):
            raise NativeMt5Error("invalid_server")
        if (
            not preferred
            or preferred != preferred.strip()
            or len(preferred) > 64
            or any(ord(character) < 32 for character in preferred)
        ):
            raise NativeMt5Error("invalid_startup_symbol")

        bases = self.terminal_root / "Bases"
        if not bases.exists():
            return None
        if self._is_reparse_point(bases) or not bases.is_dir():
            raise NativeMt5Error("broker_symbol_cache_invalid")
        try:
            matching = [
                entry
                for entry in bases.iterdir()
                if entry.name.casefold() == server.casefold()
            ]
        except OSError as exc:
            raise NativeMt5Error("broker_symbol_cache_invalid") from exc
        if len(matching) != 1:
            return None

        broker_root = matching[0]
        symbols = broker_root / "symbols"
        selected = symbols / f"selected-{login}.dat"
        for path in (broker_root, symbols):
            if not path.exists():
                return None
            if self._is_reparse_point(path) or not path.is_dir():
                raise NativeMt5Error("broker_symbol_cache_invalid")
        if not selected.exists():
            return None
        if self._is_reparse_point(selected) or not selected.is_file():
            raise NativeMt5Error("broker_symbol_cache_invalid")
        try:
            size = selected.stat().st_size
            if size <= 0 or size > 32 * 1024 * 1024:
                raise NativeMt5Error("broker_symbol_cache_invalid")
            payload = selected.read_bytes()
        except NativeMt5Error:
            raise
        except OSError as exc:
            raise NativeMt5Error("broker_symbol_cache_invalid") from exc

        decoded = payload.decode("utf-16-le", errors="ignore")
        tokens = set(self._CACHED_SYMBOL_TOKEN.findall(decoded))
        base_symbols = tuple(
            dict.fromkeys((preferred.upper(), *self._BOOTSTRAP_BASE_SYMBOLS))
        )
        candidates: list[tuple[int, int, int, str, str]] = []
        for token in tokens:
            folded = token.upper()
            for priority, base in enumerate(base_symbols):
                if base not in folded:
                    continue
                candidates.append(
                    (
                        priority,
                        0 if folded == base else 1,
                        abs(len(folded) - len(base)),
                        folded,
                        token,
                    )
                )
                break
        if not candidates:
            return None
        return min(candidates)[-1]

    def _publish_bridge_handoff(self) -> Path:
        template = self.files / "TradeJournalBridge.tpl"
        if not template.is_file():
            raise NativeMt5Error("bridge_template_invalid")
        destination = self.files / "bridge-ready"
        temporary = self.files / "bridge-ready.tmp"
        self._write_text_durable(temporary, "ready\n", "ascii")
        durable_replace(temporary, destination)
        return destination

    def _bridge_template_symbol(self) -> str:
        path = self.files / "TradeJournalBridge.tpl"
        try:
            raw = path.read_bytes()
            encoding = "utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
            lines = raw.decode(encoding).splitlines()
        except (OSError, UnicodeDecodeError) as exc:
            raise NativeMt5Error("bridge_template_invalid") from exc
        symbols = [line[len("symbol=") :] for line in lines if line.startswith("symbol=")]
        if (
            len(symbols) != 1
            or not symbols[0]
            or len(symbols[0]) > 64
            or any(character in symbols[0] for character in "\r\n")
        ):
            raise NativeMt5Error("bridge_template_invalid")
        return symbols[0]

    def _reset_managed_chart_profile(self) -> int:
        """Keep the dedicated MT5 profile empty while the isolated terminal is stopped."""
        if self._running_terminal_pids():
            raise NativeMt5Error("chart_profile_in_use")
        profiles = self.terminal_root / "Profiles" / "Charts"
        try:
            profiles_stat = os.lstat(profiles)
            if (
                profiles.is_symlink()
                or not profiles.is_dir()
                or bool(getattr(profiles_stat, "st_file_attributes", 0) & 0x400)
            ):
                raise NativeMt5Error("chart_profile_invalid")
            profile = profiles / self._MANAGED_CHART_PROFILE
            if not profile.exists():
                profile.mkdir()
            profile_stat = os.lstat(profile)
            if (
                profile.is_symlink()
                or not profile.is_dir()
                or bool(getattr(profile_stat, "st_file_attributes", 0) & 0x400)
            ):
                raise NativeMt5Error("chart_profile_invalid")
            entries = tuple(profile.iterdir())
        except NativeMt5Error:
            raise
        except OSError as exc:
            raise NativeMt5Error("chart_profile_invalid") from exc

        removable: list[Path] = []
        for entry in entries:
            if entry.name.casefold() != "order.wnd" and entry.suffix.casefold() != ".chr":
                continue
            try:
                entry_stat = os.lstat(entry)
                if (
                    entry.is_symlink()
                    or not entry.is_file()
                    or bool(getattr(entry_stat, "st_file_attributes", 0) & 0x400)
                ):
                    raise NativeMt5Error("chart_profile_invalid")
            except NativeMt5Error:
                raise
            except OSError as exc:
                raise NativeMt5Error("chart_profile_invalid") from exc
            removable.append(entry)

        try:
            for entry in removable:
                entry.unlink()
            # MT5 ignores a profile directory with no order.wnd and silently falls
            # back to the bundled Default profile (five generic, black charts on
            # suffix-only brokers). A BOM-only order file is a valid empty chart
            # order and keeps the dedicated profile selected until StartUp opens
            # the single broker-backed chart.
            order = profile / "order.wnd"
            temporary = profile / "order.wnd.tmp"
            with temporary.open("wb") as handle:
                handle.write(b"\xff\xfe")
                handle.flush()
                os.fsync(handle.fileno())
            durable_replace(temporary, order)
        except OSError as exc:
            raise NativeMt5Error("chart_profile_cleanup_failed") from exc
        return len(removable)

    @staticmethod
    def _is_reparse_point(path: Path) -> bool:
        try:
            stat_result = os.lstat(path)
        except OSError:
            return True
        return path.is_symlink() or bool(
            getattr(stat_result, "st_file_attributes", 0) & 0x400
        )

    def _remove_generated_example_code(self) -> tuple[str, ...]:
        """Remove only MT5's known generated examples while the terminal is stopped.

        The first authenticated MT5 start can materialize bundled examples.  Leaving
        them in the isolated instance makes the following Loader start perform a full
        recompilation before the bridge can run.  Every target is fixed, contained in
        this instance, and rejected if it is a reparse point.
        """
        targets: list[tuple[Path, Path]] = []
        for relative in MT5_GENERATED_EXAMPLE_DIRS:
            target = self.terminal_root / relative
            if not target.exists():
                continue
            if self._is_reparse_point(target) or not target.is_dir():
                raise NativeMt5Error("generated_example_cleanup_invalid")
            for directory, names, files in os.walk(target, followlinks=False):
                directory_path = Path(directory)
                if self._is_reparse_point(directory_path):
                    raise NativeMt5Error("generated_example_cleanup_invalid")
                if any(
                    self._is_reparse_point(directory_path / name)
                    for name in (*names, *files)
                ):
                    raise NativeMt5Error("generated_example_cleanup_invalid")
            targets.append((relative, target))
        if not targets:
            return ()
        if self._running_terminal_pids():
            raise NativeMt5Error("generated_example_cleanup_in_use")
        removed: list[str] = []
        for relative, target in targets:
            shutil.rmtree(target)
            removed.append(relative.as_posix())
        return tuple(removed)

    def _generated_example_code_present(self) -> bool:
        return any(
            (self.terminal_root / relative).exists()
            for relative in MT5_GENERATED_EXAMPLE_DIRS
        )

    def _write_startup_config(
        self,
        login: int | None,
        server: str | None,
        password: str | None,
        symbol: str,
        *,
        keep_private: bool = False,
        start_expert: bool = True,
        open_chart: bool = False,
        expert_name: str = "TradeJournal\\TradeJournalBridge",
        script_name: str | None = None,
        filename: str = "startup.ini",
    ) -> Path:
        values = (server or "") + (password or "") + symbol + expert_name + (script_name or "")
        if (
            any(c in values for c in "\r\n")
            or Path(filename).name != filename
            or (login is None) != (server is None)
            or (password is not None and (login is None or not password))
            or (start_expert and script_name is not None)
        ):
            raise NativeMt5Error("invalid_startup_value")
        path = self.state / filename
        self.state.mkdir(parents=True, exist_ok=True)
        common = ["[Common]"]
        if login is not None and server:
            common.extend((f"Login={login}", f"Server={server}"))
        if password is not None:
            common.append(f"Password={password}")
        common.extend((f"KeepPrivate={int(keep_private)}", "NewsEnable=0", ""))
        charts = [
            "[Charts]",
            f"ProfileLast={self._MANAGED_CHART_PROFILE}",
            "PreloadCharts=0",
            "",
        ]
        if script_name is not None:
            # A script receives OnStart even while MT5 is completing the account switch. It waits
            # for the authorized session and then attaches the real EA through a chart template.
            sections = [
                *charts,
                "[Experts]",
                "Enabled=1",
                "AllowLiveTrading=0",
                "AllowDllImport=0",
                "Account=0",
                "Profile=0",
                "Chart=0",
                "",
                "[StartUp]",
                f"Script={script_name}",
                f"Symbol={symbol}",
                "Period=M1",
                "ShutdownTerminal=0",
                "",
            ]
        elif start_expert:
            # MetaTrader resolves StartUp.Expert from its own MQL5/Experts directory.
            # Passing an absolute EX5 path leaves the chart open but does not reliably attach
            # the EA in portable installations.
            sections = [
                *charts,
                "[Experts]",
                "Enabled=1",
                "AllowLiveTrading=0",
                "AllowDllImport=0",
                "Account=0",
                "Profile=0",
                "Chart=0",
                "",
                "[StartUp]",
                f"Expert={expert_name}",
                f"Symbol={symbol}",
                "Period=M1",
                "",
            ]
        elif open_chart:
            # A chart forces MT5 to hydrate the broker/account caches, but no Expert is attached
            # during this warm-up phase.
            sections = [
                *charts,
                "[Experts]",
                "Enabled=0",
                "AllowLiveTrading=0",
                "AllowDllImport=0",
                "",
                "[StartUp]",
                f"Symbol={symbol}",
                "Period=M1",
                "",
            ]
        else:
            # The first phase only persists the investor credential.  Loading charts or the EA
            # here reintroduces the build-6032 first-start hang that this two-phase bootstrap
            # deliberately avoids.
            sections = [
                *charts,
                "[Experts]",
                "Enabled=0",
                "AllowLiveTrading=0",
                "AllowDllImport=0",
                "",
            ]
        content = "\n".join((*common, *sections))
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\r\n") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            content = ""
        try:
            self._restrict_startup_acl(path)
        except Exception:
            self._secure_delete_config(path)
            raise
        return path

    @staticmethod
    def _verify_local_standard_interactive_user(interactive_user: str) -> None:
        try:
            verify_local_standard_interactive_user(interactive_user)
        except InteractiveIdentityError as exc:
            raise NativeMt5Error(exc.code) from exc

    @staticmethod
    def _verify_interactive_task_identity(interactive_user: str) -> None:
        try:
            verify_interactive_task_identity(interactive_user)
        except InteractiveIdentityError as exc:
            raise NativeMt5Error(exc.code) from exc

    def _interactive_user(self) -> str:
        interactive_user = self._setting("TRADEJOURNAL_MT5_INTERACTIVE_USER")
        # MT5 needs an interactive desktop for reliable chart/script initialization, but it must
        # never share the operator's Administrator desktop: every terminal window would otherwise
        # be visible during provisioning and live sync. Built-in service identities do not provide
        # the required desktop either. A dedicated, least-privilege local account is mandatory
        # whenever the scheduled-task launch path is configured.
        if interactive_user and interactive_user.casefold() != "tradejournalmt5":
            raise NativeMt5Error("interactive_user_not_dedicated")
        if (
            interactive_user
            and os.name == "nt"
            and self._verified_interactive_user != interactive_user
        ):
            self._verify_local_standard_interactive_user(interactive_user)
            self._verified_interactive_user = interactive_user
        return interactive_user

    def _restrict_private_acl(self, path: Path, interactive_access: str) -> None:
        # The worker normally runs as LocalSystem while MT5 must run in the active desktop
        # session. Keep private artifacts restricted to SYSTEM plus that one configured identity.
        WindowsSecretStore.restrict_acl(path)
        interactive_user = self._interactive_user()
        if not interactive_user:
            return
        completed = subprocess.run(
            [
                "icacls",
                str(path),
                "/grant",
                f"{interactive_user}:{interactive_access}",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise NativeMt5Error("private_artifact_acl_failed")

    def _restrict_startup_acl(self, path: Path) -> None:
        # Startup configuration is read once, then securely removed.
        self._restrict_private_acl(path, "(R)")

    @staticmethod
    def _grant_interactive_acl(
        path: Path,
        interactive_user: str,
        access: str,
    ) -> None:
        completed = subprocess.run(
            [
                "icacls",
                str(path),
                "/grant:r",
                f"{interactive_user}:{access}",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise NativeMt5Error("interactive_runtime_acl_failed")

    def _prepare_interactive_runtime_acl(self, interactive_user: str) -> None:
        # The service publishes instances with a SYSTEM-only DACL. The dedicated desktop
        # identity needs just enough access to traverse the instance, execute the one-shot
        # launcher, and let MT5 mutate its own portable terminal tree. Deliberately do not grant
        # access to instance\secrets, data, worker, or logs.
        self._grant_interactive_acl(self.root, interactive_user, "(RX)")
        self._grant_interactive_acl(self.state, interactive_user, "(RX)")
        self._grant_interactive_acl(
            self.terminal_root,
            interactive_user,
            "(OI)(CI)(M)",
        )

    @staticmethod
    def _secure_delete_config(path: Path | None) -> None:
        if path is None:
            return
        try:
            size = max(path.stat().st_size, 1024)
            with path.open("r+b") as handle:
                handle.seek(0)
                handle.write(b"x" * size)
                handle.truncate(size)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError:
            pass
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    @staticmethod
    def _setting(name: str) -> str:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
            ) as key:
                return str(winreg.QueryValueEx(key, name)[0]).strip()
        except (ImportError, OSError):
            return ""

    @staticmethod
    def _startup_symbol(default: str) -> str:
        # Incident (2026-07-17): an operator set this machine-wide to a broker-specific variant
        # ("EURUSD.raw") while debugging a different connection. That symbol didn't exist in the
        # terminal's Market Watch, so the [StartUp] chart never opened, the EA never attached, and
        # EVERY provision -- including the credential-free no-login smoke test, on a terminal that
        # never even logs into a broker -- failed with terminal_not_ready. There is no reliable way
        # to validate a symbol before the terminal opens (that's the very step this unblocks), so
        # if this override is ever set again, prefer a plain, unsuffixed major-pair name and prove
        # it against `run-no-login-file-bridge-smoke.ps1` before touching any real connection.
        symbol = NativeMt5Runtime._setting("TRADEJOURNAL_MT5_STARTUP_SYMBOL") or default
        if not symbol or any(c in symbol for c in "\r\n"):
            raise NativeMt5Error("invalid_startup_symbol")
        return symbol

    @staticmethod
    def _payload(record: dict[str, Any], name: str) -> dict[str, Any] | None:
        if record.get("schema_version") != 1 or not isinstance(record.get("payload"), dict):
            return None
        return record["payload"]

    def _journal_checkpoint(self) -> dict[Path, int]:
        logs = self.terminal_root / "logs"
        if not logs.is_dir():
            return {}
        checkpoint: dict[Path, int] = {}
        for path in logs.glob("*.log"):
            try:
                checkpoint[path] = path.stat().st_size
            except OSError:
                continue
        return checkpoint

    def _journal_lines_since(self, checkpoint: dict[Path, int]) -> list[str]:
        logs = self.terminal_root / "logs"
        if not logs.is_dir():
            return []
        lines: list[str] = []
        for path in sorted(logs.glob("*.log")):
            try:
                size = path.stat().st_size
                offset = checkpoint.get(path, 0)
                if size < offset:
                    offset = 0
                elif size == offset:
                    continue
                with path.open("rb") as handle:
                    handle.seek(offset)
                    payload = handle.read()
            except OSError:
                continue
            lines.extend(payload.decode("utf-16-le", errors="replace").splitlines())
        return lines

    @staticmethod
    def _same_path(left: Path, right: Path) -> bool:
        return os.path.normcase(os.fspath(left.resolve())) == os.path.normcase(
            os.fspath(right.resolve())
        )

    @staticmethod
    def _command_argument(arguments: tuple[str, ...], name: str) -> str | None:
        prefix = f"/{name}:".casefold()
        for argument in arguments:
            value = str(argument).strip()
            if value.casefold().startswith(prefix):
                return value[len(prefix) :].strip().strip('"')
        return None

    @staticmethod
    def _live_update_marker_executable(line: str) -> Path | None:
        match = re.search(
            r'(?:^|\t)LiveUpdate\tstart\s+"([^"\r\n]+)"([^\r\n]*)$',
            line,
            re.IGNORECASE,
        )
        if match is None or re.search(
            r"(?:^|\s)/update(?:\s|$)",
            match.group(2),
            re.IGNORECASE,
        ) is None:
            return None
        try:
            return Path(match.group(1)).resolve()
        except (OSError, ValueError):
            return None

    def _live_update_candidates(self, config: Path) -> list[_LiveUpdateCandidate]:
        try:
            import psutil
        except ImportError:
            return []
        candidates: list[_LiveUpdateCandidate] = []
        for process in psutil.process_iter(("pid", "exe", "cmdline")):
            try:
                executable_raw = process.info.get("exe")
                command_raw = process.info.get("cmdline")
                if not executable_raw or not isinstance(command_raw, (list, tuple)):
                    continue
                executable = Path(str(executable_raw)).resolve()
                arguments = tuple(str(value) for value in command_raw)
                parts = tuple(part.casefold() for part in executable.parts)
                if (
                    executable.name.casefold() != "terminal64.exe"
                    or len(parts) < 7
                    or parts[-2] != "liveupdate"
                    or not re.fullmatch(r"[0-9a-f]{32}", parts[-3])
                    or parts[-4] != "terminal"
                    or parts[-5] != "metaquotes"
                    or parts[-6] != "roaming"
                    or parts[-7] != "appdata"
                    or not any(value.casefold() == "/update" for value in arguments)
                ):
                    continue
                target = self._command_argument(arguments, "path")
                requested_config = self._command_argument(arguments, "config")
                if (
                    not target
                    or not requested_config
                    or not self._same_path(Path(target), self.terminal_root)
                    or not self._same_path(Path(requested_config), config)
                ):
                    continue
                unsafe = False
                current = executable
                for _ in range(4):
                    if InstanceProvisioner._is_reparse_point(current):
                        unsafe = True
                        break
                    current = current.parent
                if unsafe:
                    continue
                candidates.append(
                    _LiveUpdateCandidate(
                        pid=int(process.info["pid"]),
                        executable=executable,
                        arguments=arguments,
                    )
                )
            except (psutil.AccessDenied, psutil.NoSuchProcess, OSError, ValueError):
                continue
        return candidates

    def _cached_live_update_candidates(
        self,
        config: Path,
    ) -> list[_LiveUpdateCandidate]:
        """Find a downloaded MetaQuotes update before MT5 can request UAC."""
        if os.name != "nt":
            return []
        interactive_user = self._interactive_user()
        if not interactive_user:
            return []
        system_drive = (os.environ.get("SystemDrive") or "C:").rstrip("\\/")
        cache_root = (
            Path(f"{system_drive}\\Users")
            / interactive_user
            / "AppData"
            / "Roaming"
            / "MetaQuotes"
            / "Terminal"
        )
        if (
            InstanceProvisioner._is_reparse_point(cache_root)
            or not cache_root.is_dir()
        ):
            return []
        candidates: list[_LiveUpdateCandidate] = []
        for executable in cache_root.glob("*/liveupdate/terminal64.exe"):
            try:
                executable = executable.resolve()
                if not re.fullmatch(
                    r"[0-9a-f]{32}",
                    executable.parent.parent.name,
                    re.IGNORECASE,
                ):
                    continue
                unsafe = False
                current = executable
                for _ in range(4):
                    if InstanceProvisioner._is_reparse_point(current):
                        unsafe = True
                        break
                    current = current.parent
                if unsafe:
                    continue
                candidates.append(
                    _LiveUpdateCandidate(
                        pid=0,
                        executable=executable,
                        arguments=(
                            str(executable),
                            "/update",
                            f"/path:{self.terminal_root}",
                            "/portable",
                            f"/config:{config}",
                        ),
                    )
                )
            except (OSError, ValueError):
                continue
        return candidates

    @staticmethod
    def _verify_metaquotes_signature(path: Path) -> str:
        script = (
            "$s=Get-AuthenticodeSignature -LiteralPath "
            "$env:TRADEJOURNAL_SIGNATURE_PATH;"
            "[pscustomobject]@{Status=[string]$s.Status;"
            "Subject=[string]$s.SignerCertificate.Subject}|ConvertTo-Json -Compress"
        )
        environment = os.environ.copy()
        environment["TRADEJOURNAL_SIGNATURE_PATH"] = str(path)
        completed = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                script,
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=environment,
        )
        try:
            identity = json.loads(completed.stdout.strip())
        except (json.JSONDecodeError, AttributeError) as exc:
            raise NativeMt5Error("mt5_update_signature_invalid") from exc
        subject = identity.get("Subject") if isinstance(identity, dict) else None
        if (
            completed.returncode != 0
            or not isinstance(identity, dict)
            or identity.get("Status") != "Valid"
            or not isinstance(subject, str)
            or "CN=MetaQuotes Ltd." not in subject
            or "O=MetaQuotes Ltd." not in subject
        ):
            raise NativeMt5Error("mt5_update_signature_invalid")
        return subject

    @staticmethod
    def _require_elevated_service() -> None:
        if os.name != "nt":
            raise NativeMt5Error("mt5_update_elevation_unavailable")
        try:
            import ctypes

            elevated = bool(ctypes.windll.shell32.IsUserAnAdmin())
        except (AttributeError, OSError) as exc:
            raise NativeMt5Error("mt5_update_elevation_unavailable") from exc
        if not elevated:
            raise NativeMt5Error("mt5_update_elevation_unavailable")

    @staticmethod
    def _terminate_candidate(candidate: _LiveUpdateCandidate) -> None:
        if candidate.pid <= 0:
            return
        try:
            import psutil

            process = psutil.Process(candidate.pid)
            if Path(process.exe()).resolve() != candidate.executable:
                raise NativeMt5Error("mt5_update_process_identity_mismatch")
            process.terminate()
            try:
                process.wait(15)
            except psutil.TimeoutExpired:
                process.kill()
                process.wait(5)
        except psutil.NoSuchProcess:
            return
        except (psutil.AccessDenied, OSError, ValueError) as exc:
            raise NativeMt5Error("mt5_update_process_stop_failed") from exc

    def _complete_live_update(
        self,
        config: Path,
        *,
        timeout: float = 240.0,
    ) -> None:
        candidate = self._pending_live_update
        self._pending_live_update = None
        if candidate is None:
            candidates = self._live_update_candidates(config)
            if len(candidates) != 1:
                raise NativeMt5Error("mt5_update_process_missing")
            candidate = candidates[0]
        self._require_elevated_service()
        archive: Path | None = None
        working: Path | None = None
        preserve_staged_archive = False
        try:
            archive = Path(
                tempfile.mkdtemp(prefix="live-update-", dir=self.state)
            )
            working = Path(
                tempfile.mkdtemp(prefix=".live-update-working-", dir=self.state)
            )
            WindowsSecretStore.restrict_acl(archive)
            WindowsSecretStore.restrict_acl(working)
            source_directory = candidate.executable.parent
            if (
                InstanceProvisioner._is_reparse_point(source_directory)
                or not source_directory.is_dir()
            ):
                raise NativeMt5Error("mt5_update_source_unsafe")

            # MetaQuotes stores the signed updater beside one or more opaque,
            # build-suffixed payloads (for example ``mt5onnx64.6090``).  The
            # updater cannot apply those components if only terminal64.exe is
            # staged.  Copy only that narrowly shaped vendor payload -- never
            # adjacent DLL/EXE files -- into the SYSTEM-only directory.
            payload_sources = [candidate.executable]
            for source in source_directory.iterdir():
                if source == candidate.executable:
                    continue
                if (
                    source.is_file()
                    and not InstanceProvisioner._is_reparse_point(source)
                    and VENDOR_UPDATE_PAYLOAD_NAME.fullmatch(source.name)
                ):
                    payload_sources.append(source)
            payload_sources.sort(key=lambda value: value.name.casefold())
            if len(payload_sources) + 1 > MAX_UPDATE_BUNDLE_FILES:
                raise NativeMt5Error("mt5_update_source_size_invalid")
            payload_identities = {
                source: self._bounded_source_identity(source)
                for source in payload_sources
            }
            payload_bytes = sum(
                size for size, _digest in payload_identities.values()
            ) + len(UPDATER_ONLY_CONFIG_BYTES)
            if payload_bytes > MAX_UPDATE_BUNDLE_BYTES:
                raise NativeMt5Error("mt5_update_source_size_invalid")
            try:
                free_bytes = shutil.disk_usage(self.state).free
            except OSError as exc:
                raise NativeMt5Error(
                    "mt5_update_storage_unavailable"
                ) from exc
            required_bytes = (
                payload_bytes * 2
                + self._LIVE_UPDATE_FREE_SPACE_RESERVE_BYTES
            )
            if free_bytes < required_bytes:
                raise NativeMt5Error("mt5_update_storage_insufficient")
            for source in payload_sources:
                size, digest = payload_identities[source]
                for destination_root in (archive, working):
                    destination = destination_root / source.name
                    self._copy_bounded_verified_file(
                        source,
                        destination,
                        size,
                        digest,
                    )
            (archive / "temp").mkdir()
            (working / "temp").mkdir()
            archived_updater = archive / candidate.executable.name
            working_updater = working / candidate.executable.name
            updater_config = write_updater_only_config(
                archive / "tradejournal-update.ini"
            )
            working_config = write_updater_only_config(
                working / "tradejournal-update.ini"
            )
            updater_signer = self._verify_metaquotes_signature(archived_updater)
            # A cached update is discovered with pid=0. It can start between the
            # cache scan and this point, so bind it to the one exact updater for
            # this terminal/config before touching its files.
            if candidate.pid <= 0:
                active_candidates = [
                    active
                    for active in self._live_update_candidates(config)
                    if self._same_path(active.executable, candidate.executable)
                ]
                if len(active_candidates) > 1:
                    raise NativeMt5Error("mt5_update_process_ambiguous")
                if active_candidates:
                    candidate = active_candidates[0]
            self._terminate_candidate(candidate)
            for source, identity in payload_identities.items():
                if self._bounded_source_identity(source) != identity:
                    raise NativeMt5Error("mt5_update_source_changed")
            if not self.stop():
                raise NativeMt5Error("terminal_stop_failed")
            self._remove_generated_example_code()
            try:
                source_release = Mt5UpdateRelease.from_terminal_root(
                    self.terminal_root
                )
                managed_assets_before = (
                    InstanceProvisioner._managed_runtime_assets_manifest(
                        self.terminal_root
                    )
                )
                stage_verified_update_bundle(
                    archive,
                    source_connection_id=self.connection_id,
                    source_release=source_release,
                    managed_assets_manifest_sha256=managed_assets_before,
                    signer_subject=updater_signer,
                    updater=archived_updater,
                    updater_config=updater_config,
                )
                # The source cache is deleted immediately below. Flush every
                # copied payload and the metadata directory first so a power
                # loss cannot leave a durable receipt pointing at non-durable
                # updater bytes with no remaining vendor source.
                InstanceProvisioner._sync_tree(archive)
                preserve_staged_archive = True
            except (Mt5PendingUpdateStoreError, OSError, ValueError) as exc:
                raise NativeMt5Error("mt5_update_archive_invalid") from exc

            # Delete only after two hash-verified copies and the durable source
            # release sidecar exist.  The archive is never used as subprocess
            # working space, so a vendor updater may consume its working payload
            # without destroying the receipt needed by nightly maintenance.
            for source in payload_sources:
                source.unlink()

            command = [
                str(working_updater),
                "/update",
                f"/path:{self.terminal_root}",
                "/portable",
                f"/config:{working_config}",
            ]
            process = subprocess.Popen(
                command,
                cwd=working,
                close_fds=True,
            )
            deadline = time.monotonic() + max(1.0, min(timeout, 240.0))
            try:
                while process.poll() is None and time.monotonic() < deadline:
                    self._check_cancelled()
                    time.sleep(0.5)
                if process.poll() is None:
                    process.kill()
                    process.wait(5)
                    raise NativeMt5Error("mt5_update_timeout")
                if process.returncode not in (0, None):
                    raise NativeMt5Error("mt5_update_failed")
            except Exception:
                if process.poll() is None:
                    process.kill()
                    process.wait(5)
                raise

            # LiveUpdate can relaunch the target terminal in session 0.  Stop that
            # copy and start it again through the dedicated interactive task.
            if not self.stop(timeout=30.0):
                raise NativeMt5Error("mt5_update_restart_cleanup_failed")
            self._remove_generated_example_code()
            try:
                managed_assets_after = (
                    InstanceProvisioner._managed_runtime_assets_manifest(
                        self.terminal_root
                    )
                )
            except ValueError as exc:
                raise NativeMt5Error("mt5_update_integrity_failed") from exc
            if managed_assets_after != managed_assets_before:
                raise NativeMt5Error("mt5_update_integrity_failed")
            terminal_signer = self._verify_metaquotes_signature(self.terminal)
            if terminal_signer != updater_signer:
                raise NativeMt5Error("mt5_update_signer_mismatch")
            try:
                target_release = Mt5UpdateRelease.from_terminal_root(
                    self.terminal_root
                )
                if target_release == source_release:
                    # MetaQuotes can leave a stale, already-applied cache. It
                    # is safe to consume once, but it is not an update edge:
                    # do not mutate the vendor pin or create A->A recovery
                    # receipts that would form a cycle after a crash.
                    preserve_staged_archive = False
                    logger.info(
                        "discarded no-op MetaQuotes LiveUpdate for %s",
                        self.connection_id,
                    )
                    return
                recorded_digest = InstanceProvisioner.record_verified_vendor_update(
                    self.root,
                    self.connection_id,
                    terminal_signer,
                )
                if recorded_digest != target_release.terminal_sha256:
                    raise ValueError("recorded terminal digest mismatch")
                sealed = seal_applied_update_bundle(archive, target_release)
            except (Mt5PendingUpdateStoreError, OSError, ValueError) as exc:
                raise NativeMt5Error("mt5_update_integrity_failed") from exc
            if self._verified_vendor_update_callback is not None:
                self._pending_verified_vendor_updates.append(
                    (
                        sealed.root,
                        sealed.updater,
                        sealed.updater_config,
                        sealed.signer_subject,
                    )
                )
                # Keep the SYSTEM-only verified bundle until the restarted
                # account passes login, heartbeat and investor-only checks.
                archive = None
            else:
                # Without a control-plane capture callback the fully applied
                # receipt has no consumer. Only failed STAGED receipts are
                # recovery-critical and must survive this call.
                preserve_staged_archive = False
            logger.info("completed verified MetaQuotes LiveUpdate for %s", self.connection_id)
        finally:
            if working is not None:
                shutil.rmtree(working, ignore_errors=True)
            if archive is not None and not preserve_staged_archive:
                shutil.rmtree(archive, ignore_errors=True)

    def _start_and_wait_for_authorization(
        self,
        config: Path,
        login: int,
        server: str,
        timeout: float,
        connection_endpoint: str | None = None,
    ) -> tuple[dict[Path, int], str]:
        # A vendor release can require more than one signed hop (A -> B -> C).
        # Bound both hop count and elapsed time, and prove that every successful
        # updater advances the exact terminal/code release (apart from one
        # harmless stale-cache no-op). All receipts remain private until the
        # final account heartbeat/read-only gate publishes the complete chain.
        chain_deadline = time.monotonic() + max(
            timeout,
            self._LIVE_UPDATE_CHAIN_TIMEOUT_SECONDS,
        )
        try:
            current_release = Mt5UpdateRelease.from_terminal_root(
                self.terminal_root
            )
        except Mt5PendingUpdateStoreError as exc:
            raise NativeMt5Error("mt5_update_progress_invalid") from exc
        visited_releases = {current_release}
        no_progress_hops = 0
        update_hops = 0
        for _attempt in range(self._MAX_LIVE_UPDATE_HOPS + 1):
            remaining = chain_deadline - time.monotonic()
            if remaining <= 0:
                raise NativeMt5Error("mt5_update_chain_timeout")
            checkpoint = self._journal_checkpoint()
            self._start_process(config)
            attempt_timeout = min(timeout, remaining)
            monitor = self._start_auth_failure_monitor(attempt_timeout)
            try:
                observed = self._wait_for_authorization(
                    checkpoint,
                    login,
                    server,
                    attempt_timeout,
                    connection_endpoint,
                    update_config=config,
                    auth_monitor=monitor,
                )
                return checkpoint, observed
            except NativeMt5Error as exc:
                if str(exc) != "mt5_live_update_required":
                    raise
                if update_hops >= self._MAX_LIVE_UPDATE_HOPS:
                    raise NativeMt5Error("mt5_update_loop") from exc
                remaining = chain_deadline - time.monotonic()
                if remaining <= 0:
                    raise NativeMt5Error("mt5_update_chain_timeout") from exc
                self._complete_live_update(
                    config,
                    timeout=min(240.0, remaining),
                )
                try:
                    next_release = Mt5UpdateRelease.from_terminal_root(
                        self.terminal_root
                    )
                except Mt5PendingUpdateStoreError as release_exc:
                    raise NativeMt5Error(
                        "mt5_update_progress_invalid"
                    ) from release_exc
                if next_release == current_release:
                    no_progress_hops += 1
                    if no_progress_hops > 1:
                        raise NativeMt5Error("mt5_update_no_progress")
                else:
                    if next_release in visited_releases:
                        raise NativeMt5Error("mt5_update_cycle")
                    visited_releases.add(next_release)
                    current_release = next_release
                update_hops += 1
            finally:
                self._stop_auth_failure_monitor(monitor)
        raise NativeMt5Error("mt5_update_loop")

    def _wait_for_authorization(
        self,
        checkpoint: dict[Path, int],
        login: int,
        server: str,
        timeout: float,
        connection_endpoint: str | None = None,
        update_config: Path | None = None,
        auth_monitor: _AuthFailureMonitor | None = None,
    ) -> str:
        deadline = time.monotonic() + timeout
        seen_process = False
        expected_login = f"'{login}'"
        expected_endpoint = (connection_endpoint or "").strip().casefold()
        announced_updaters: list[Path] = []
        while time.monotonic() < deadline:
            self._check_cancelled()
            lines = self._journal_lines_since(checkpoint)
            for line in lines:
                folded = line.casefold()
                announced_updater = self._live_update_marker_executable(line)
                if announced_updater is not None and not any(
                    self._same_path(announced_updater, existing)
                    for existing in announced_updaters
                ):
                    announced_updaters.append(announced_updater)
                if "invalid account" in folded:
                    raise NativeMt5Error("authorization_failed")
                if expected_endpoint and expected_endpoint in folded:
                    if (
                        "connection to" in folded
                        and "failed" in folded
                    ):
                        raise NativeMt5Error("endpoint_connection_failed")
                    if (
                        "connection refused" in folded
                        or "actively refused" in folded
                    ):
                        raise NativeMt5Error("endpoint_connection_refused")
                    if (
                        "protocol mismatch" in folded
                        or "unsupported protocol" in folded
                    ):
                        raise NativeMt5Error(
                            "endpoint_protocol_incompatible"
                        )
                    if (
                        "server not found" in folded
                        or "unknown server" in folded
                    ):
                        raise NativeMt5Error(
                            "endpoint_server_unrecognized"
                        )
                if "authorized on" in folded and expected_login in line:
                    marker = folded.find("authorized on")
                    reported_server = line[
                        marker + len("authorized on") :
                    ].strip()
                    through = reported_server.casefold().find(" through ")
                    if through >= 0:
                        reported_server = reported_server[:through].strip()
                    if not reported_server:
                        continue
                    self._release_interactive_task()
                    return reported_server

            pids = self._running_terminal_pids()
            if pids:
                seen_process = True
            if update_config is not None:
                candidates = self._live_update_candidates(update_config)
                if len(candidates) > 1:
                    raise NativeMt5Error("mt5_update_process_ambiguous")
                if len(candidates) == 1:
                    self._pending_live_update = candidates[0]
                    raise NativeMt5Error("mt5_live_update_required")
                if len(announced_updaters) > 1:
                    raise NativeMt5Error("mt5_update_process_ambiguous")
                if announced_updaters:
                    cached_candidates = [
                        candidate
                        for candidate in self._cached_live_update_candidates(
                            update_config
                        )
                        if self._same_path(
                            candidate.executable,
                            announced_updaters[0],
                        )
                    ]
                    if len(cached_candidates) > 1:
                        raise NativeMt5Error(
                            "mt5_update_process_ambiguous"
                        )
                    if len(cached_candidates) == 1:
                        self._pending_live_update = cached_candidates[0]
                        raise NativeMt5Error("mt5_live_update_required")
                    # The journal marker can precede process creation. Keep
                    # polling until the exact updater or exact cached path
                    # appears, or until timeout wins.
                    time.sleep(0.1)
                    continue
            if self._authentication_failure_detected(auth_monitor):
                raise NativeMt5Error("authorization_failed")
            if self._process is not None:
                if self._process.poll() is None:
                    seen_process = True
                elif seen_process and not pids:
                    raise NativeMt5Error("mt5_process_crashed")
            elif seen_process and not pids:
                raise NativeMt5Error("mt5_process_crashed")
            time.sleep(0.5)
        raise NativeMt5Error("authorization_timeout")

    def _start_auth_failure_monitor(
        self,
        timeout: float,
    ) -> _AuthFailureMonitor | None:
        interactive_user = self._interactive_user()
        if os.name != "nt" or not interactive_user:
            return None
        deadline = time.monotonic() + min(timeout, 15.0)
        pids: list[int] = []
        while time.monotonic() < deadline:
            self._check_cancelled()
            pids = self._running_terminal_pids()
            if len(pids) == 1:
                break
            if len(pids) > 1:
                raise NativeMt5Error("terminal_process_ambiguous")
            time.sleep(0.1)
        if len(pids) != 1:
            return None

        pid = pids[0]
        executable, creation_time_unix_ms = self._terminal_process_identity(pid)
        source = (
            Path(__file__).resolve().parents[2]
            / "scripts"
            / "windows"
            / "Detect-Mt5AuthenticationFailure.ps1"
        )
        if not source.is_file():
            return None

        helper = self.state / "detect-mt5-authentication-failure.ps1"
        helper_temporary = helper.with_suffix(".ps1.tmp")
        request = self.state / "authentication-monitor-request.json"
        request_temporary = request.with_suffix(".json.tmp")
        result = self.files / "authentication-monitor-result.json"
        launcher = self.state / "detect-mt5-authentication-failure.cmd"
        task = f"TradeJournalMT5-Auth-{self.connection_id}"
        try:
            source_digest = self._sha256(source)
            shutil.copy2(source, helper_temporary)
            with helper_temporary.open("r+b") as handle:
                os.fsync(handle.fileno())
            if self._sha256(helper_temporary) != source_digest:
                raise NativeMt5Error("authentication_monitor_integrity_failed")
            durable_replace(helper_temporary, helper)
            self._grant_interactive_acl(helper, interactive_user, "(RX)")
            self._write_text_durable(
                request_temporary,
                json.dumps(
                    {
                        "schema_version": 1,
                        "process_id": pid,
                        "creation_time_unix_ms": creation_time_unix_ms,
                        "expected_executable": str(executable),
                        "timeout_seconds": max(1, min(300, int(timeout))),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "utf-8",
            )
            durable_replace(request_temporary, request)
            self._restrict_private_acl(request, "(R)")
            result.unlink(missing_ok=True)
            self._write_text_durable(
                launcher,
                "@echo off\r\n"
                "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass "
                f'-File "{helper}" -RequestPath "{request}" -ResultPath "{result}"\r\n'
                "exit /b %ERRORLEVEL%\r\n",
                "utf-8",
            )
            self._grant_interactive_acl(launcher, interactive_user, "(RX)")
            self._verify_interactive_task_identity(interactive_user)
            create = [
                "schtasks", "/Create", "/TN", task, "/SC", "ONCE", "/ST", "23:59",
                "/RU", interactive_user, "/IT", "/RL", "LIMITED", "/TR", str(launcher), "/F",
            ]
            completed = subprocess.run(create, capture_output=True, text=True, check=False)
            if completed.returncode != 0:
                raise NativeMt5Error("authentication_monitor_task_create_failed")
            self._verify_interactive_task_identity(interactive_user)
            completed = subprocess.run(
                ["schtasks", "/Run", "/TN", task],
                capture_output=True,
                text=True,
                check=False,
            )
            if completed.returncode != 0:
                raise NativeMt5Error("authentication_monitor_task_run_failed")
            return _AuthFailureMonitor(
                task,
                pid,
                creation_time_unix_ms,
                helper,
                request,
                result,
                launcher,
            )
        except Exception:
            subprocess.run(
                ["schtasks", "/Delete", "/TN", task, "/F"],
                capture_output=True,
                text=True,
                check=False,
            )
            for path in (helper_temporary, helper, request_temporary, request, result, launcher):
                path.unlink(missing_ok=True)
            raise

    def _authentication_failure_detected(
        self,
        monitor: _AuthFailureMonitor | None,
    ) -> bool:
        if monitor is None or not monitor.result.is_file():
            return False
        record = self._read_json(monitor.result)
        if (
            record is None
            or set(record) != {
                "schema_version",
                "success",
                "detected",
                "error_code",
                "process_id",
                "creation_time_unix_ms",
            }
            or record.get("schema_version") != 1
            or record.get("process_id") != monitor.pid
            or record.get("creation_time_unix_ms") != monitor.creation_time_unix_ms
        ):
            raise NativeMt5Error("authentication_monitor_result_invalid")
        if record.get("success") is not True:
            raise NativeMt5Error("authentication_monitor_failed")
        return (
            record.get("detected") is True
            and record.get("error_code") == "authorization_failed"
        )

    @staticmethod
    def _stop_auth_failure_monitor(monitor: _AuthFailureMonitor | None) -> None:
        if monitor is None:
            return
        subprocess.run(
            ["schtasks", "/End", "/TN", monitor.task],
            capture_output=True,
            text=True,
            check=False,
        )
        subprocess.run(
            ["schtasks", "/Delete", "/TN", monitor.task, "/F"],
            capture_output=True,
            text=True,
            check=False,
        )
        for path in (monitor.helper, monitor.request, monitor.result, monitor.launcher):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logger.warning("authentication monitor cleanup is incomplete")

    def _wait_for_account_database(self, timeout: float) -> None:
        accounts = self.terminal_root / "Config" / "accounts.dat"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._check_cancelled()
            try:
                if accounts.is_file() and accounts.stat().st_size > 0:
                    # accounts.dat contains MT5's encrypted account material. MT5 must retain
                    # modify access, but no unrelated local identity should inherit access.
                    self._restrict_private_acl(accounts, "(M)")
                    return
            except OSError:
                pass
            time.sleep(0.25)
        raise NativeMt5Error("account_persistence_failed")

    def _wait_for_investor_sync(
        self,
        checkpoint: dict[Path, int],
        login: int,
        timeout: float,
    ) -> None:
        deadline = time.monotonic() + timeout
        expected_login = f"'{login}'"
        synchronized = False
        investor_only = False
        seen_process = False
        while time.monotonic() < deadline:
            self._check_cancelled()
            for line in self._journal_lines_since(checkpoint):
                if expected_login not in line:
                    continue
                folded = line.casefold()
                if "trading has been enabled" in folded:
                    raise NativeMt5Error("investor_readonly_not_verified")
                if "terminal synchronized with" in folded:
                    synchronized = True
                if "trading has been disabled - investor mode" in folded:
                    investor_only = True
            if synchronized and investor_only:
                self._release_interactive_task()
                return

            pids = self._running_terminal_pids()
            if pids:
                seen_process = True
            if seen_process and not pids:
                raise NativeMt5Error("mt5_process_crashed")
            time.sleep(0.5)
        raise NativeMt5Error("investor_sync_timeout")

    def _remove_readiness_files(self) -> None:
        cleanup_failed = False
        for name in (
            "account.json",
            "heartbeat.json",
            "discovered-symbol.json",
            "discovered-symbol.tmp",
            "discovery-started.json",
            "discovery-started.tmp",
            "bridge-ready",
            "bridge-ready.tmp",
            "symbol-preference.tmp",
        ):
            try:
                (self.files / name).unlink(missing_ok=True)
            except OSError:
                cleanup_failed = True
        if cleanup_failed:
            raise NativeMt5Error("readiness_cleanup_failed")
        # MQL5 timestamps have one-second precision. The two-second allowance
        # covers rounding while still binding the next account/heartbeat pair
        # to this launch; successful deletion is the primary stale-data gate.
        self._readiness_not_before = datetime.now(timezone.utc) - timedelta(
            seconds=2
        )

    def _start_process(
        self,
        config: Path,
        login_hint: int | None = None,
    ) -> subprocess.Popen[bytes] | None:
        login_argument = f" /login:{login_hint}" if login_hint is not None else ""
        interactive_user = self._interactive_user()
        if os.name == "nt" and not interactive_user:
            raise NativeMt5Error("dedicated_interactive_user_required")
        if interactive_user:
            self._wait_for_interactive_session(interactive_user)
            self._prepare_interactive_runtime_acl(interactive_user)
            launcher = self.state / "launch-terminal.cmd"
            launcher_content = (
                "@echo off\r\n"
                f'start "" /b "{self.terminal}" /portable{login_argument} '
                f'/profile:{self._MANAGED_CHART_PROFILE} /config:"{config}"\r\n'
            )
            launcher.write_text(launcher_content, encoding="utf-8")
            self._grant_interactive_acl(
                launcher,
                interactive_user,
                "(RX)",
            )
            task = f"TradeJournalMT5-{self.connection_id}"
            command = str(launcher)
            self._verify_interactive_task_identity(interactive_user)
            create = [
                "schtasks", "/Create", "/TN", task, "/SC", "ONCE", "/ST", "23:59",
                "/RU", interactive_user, "/IT", "/RL", "LIMITED", "/TR", command, "/F",
            ]
            completed = subprocess.run(create, capture_output=True, text=True, check=False)
            if completed.returncode != 0:
                raise NativeMt5Error("interactive_task_create_failed")
            # Own the task immediately after Create succeeds.  The token can
            # change between Create and Run (for example after a session
            # reconnect), and every failure from this point must remove the
            # otherwise still-runnable ONCE task.
            self._interactive_task = task
            try:
                self._verify_interactive_task_identity(interactive_user)
                completed = subprocess.run(
                    ["schtasks", "/Run", "/TN", task],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if completed.returncode != 0:
                    raise NativeMt5Error("interactive_task_run_failed")
            except BaseException:
                self._cancel_interactive_task()
                raise
            return None
        arguments = [
            str(self.terminal),
            "/portable",
            f"/profile:{self._MANAGED_CHART_PROFILE}",
        ]
        if login_hint is not None:
            arguments.append(f"/login:{login_hint}")
        arguments.append(f"/config:{config}")
        self._process = subprocess.Popen(
            arguments,
            cwd=self.terminal_root,
            close_fds=True,
        )
        return self._process

    @staticmethod
    def _interactive_session_present(interactive_user: str) -> bool:
        try:
            return verified_interactive_session_present(interactive_user)
        except InteractiveIdentityError as exc:
            raise NativeMt5Error(exc.code) from exc

    def _wait_for_interactive_session(
        self,
        interactive_user: str,
        timeout: float = 90.0,
    ) -> None:
        """Allow secure Windows autologon to finish before a reboot recovery launch."""

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._check_cancelled()
            try:
                self._verify_interactive_task_identity(interactive_user)
                return
            except NativeMt5Error as exc:
                if str(exc) != "interactive_session_unavailable":
                    raise
            time.sleep(0.5)
        raise NativeMt5Error("interactive_session_unavailable")

    def _release_interactive_task(self) -> None:
        """Delete a successful one-shot launcher without stopping its MT5 child."""
        task = self._interactive_task
        if not task:
            return
        completed = subprocess.run(
            ["schtasks", "/Delete", "/TN", task, "/F"],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            # Keep the task name so stop() can still terminate/delete it on the failure path.
            raise NativeMt5Error("interactive_task_cleanup_failed")
        self._interactive_task = None
        try:
            (self.state / "launch-terminal.cmd").unlink(missing_ok=True)
        except OSError:
            pass

    def _cancel_interactive_task(self) -> None:
        """End and delete an owned task after a failed create/run sequence."""

        task = self._interactive_task
        if not task:
            return
        subprocess.run(
            ["schtasks", "/End", "/TN", task],
            capture_output=True,
            text=True,
            check=False,
        )
        completed = subprocess.run(
            ["schtasks", "/Delete", "/TN", task, "/F"],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            # Retain ownership so stop() can retry cleanup.  No arbitrary
            # command output is included in the sanitized error.
            raise NativeMt5Error("interactive_task_cleanup_failed")
        self._interactive_task = None
        try:
            (self.state / "launch-terminal.cmd").unlink(missing_ok=True)
        except OSError:
            pass

    def _terminal_process_identity(self, pid: int) -> tuple[Path, int]:
        try:
            import psutil
        except ImportError as exc:
            raise NativeMt5Error("terminal_window_identity_failed") from exc
        try:
            process = psutil.Process(pid)
            executable = Path(process.exe()).resolve()
            creation_time_unix_ms = int(process.create_time() * 1000)
        except (psutil.Error, OSError, ValueError) as exc:
            raise NativeMt5Error("terminal_window_identity_failed") from exc
        if executable != self.terminal.resolve() or creation_time_unix_ms <= 0:
            raise NativeMt5Error("terminal_window_identity_failed")
        return executable, creation_time_unix_ms

    def set_terminal_window_visibility(
        self,
        pid: int,
        *,
        visible: bool,
        timeout: float = 15.0,
    ) -> dict[str, Any]:
        """Change only this terminal's windows in its dedicated interactive session."""
        interactive_user = self._interactive_user()
        if not interactive_user:
            raise NativeMt5Error("dedicated_interactive_user_required")
        executable, creation_time_unix_ms = self._terminal_process_identity(pid)
        source = (
            Path(__file__).resolve().parents[2]
            / "scripts"
            / "windows"
            / "Set-Mt5WindowVisibility.ps1"
        )
        if not source.is_file():
            raise NativeMt5Error("terminal_window_helper_missing")

        self.state.mkdir(parents=True, exist_ok=True)
        self.files.mkdir(parents=True, exist_ok=True)
        helper = self.state / "set-mt5-window-visibility.ps1"
        helper_temporary = helper.with_suffix(".ps1.tmp")
        request = self.state / "window-visibility-request.json"
        request_temporary = request.with_suffix(".json.tmp")
        result = self.files / "window-visibility-result.json"
        result_temporary = result.with_suffix(".json.tmp")
        launcher = self.state / "set-window-visibility.cmd"
        task = f"TradeJournalMT5-Window-{self.connection_id}"
        action = "show" if visible else "hide"
        task_created = False
        cleanup_failed = False
        try:
            source_digest = self._sha256(source)
            shutil.copy2(source, helper_temporary)
            with helper_temporary.open("r+b") as handle:
                os.fsync(handle.fileno())
            if self._sha256(helper_temporary) != source_digest:
                raise NativeMt5Error("terminal_window_helper_integrity_failed")
            durable_replace(helper_temporary, helper)
            self._grant_interactive_acl(helper, interactive_user, "(RX)")

            payload = {
                "schema_version": 1,
                "process_id": pid,
                "creation_time_unix_ms": creation_time_unix_ms,
                "expected_executable": str(executable),
                "action": action,
            }
            self._write_text_durable(
                request_temporary,
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
                "utf-8",
            )
            durable_replace(request_temporary, request)
            self._restrict_private_acl(request, "(R)")
            result.unlink(missing_ok=True)
            result_temporary.unlink(missing_ok=True)

            launcher_content = (
                "@echo off\r\n"
                "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass "
                f'-File "{helper}" -RequestPath "{request}" -ResultPath "{result}"\r\n'
                "exit /b %ERRORLEVEL%\r\n"
            )
            self._write_text_durable(launcher, launcher_content, "utf-8")
            self._grant_interactive_acl(launcher, interactive_user, "(RX)")

            self._verify_interactive_task_identity(interactive_user)
            create = [
                "schtasks",
                "/Create",
                "/TN",
                task,
                "/SC",
                "ONCE",
                "/ST",
                "23:59",
                "/RU",
                interactive_user,
                "/IT",
                "/RL",
                "LIMITED",
                "/TR",
                str(launcher),
                "/F",
            ]
            completed = subprocess.run(
                create, capture_output=True, text=True, check=False
            )
            if completed.returncode != 0:
                raise NativeMt5Error("terminal_window_task_create_failed")
            task_created = True
            self._verify_interactive_task_identity(interactive_user)
            completed = subprocess.run(
                ["schtasks", "/Run", "/TN", task],
                capture_output=True,
                text=True,
                check=False,
            )
            if completed.returncode != 0:
                raise NativeMt5Error("terminal_window_task_run_failed")

            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline and not result.is_file():
                self._check_cancelled()
                time.sleep(0.1)
            record = self._read_json(result) if result.is_file() else None
            if (
                record is None
                or record.get("schema_version") != 1
                or record.get("success") is not True
                or record.get("action") != action
                or record.get("process_id") != pid
                or record.get("creation_time_unix_ms")
                != creation_time_unix_ms
                or not isinstance(record.get("windows_matched"), int)
                or record["windows_matched"] < 1
                or not isinstance(record.get("visible_after"), int)
                or (visible and record["visible_after"] < 1)
                or (not visible and record["visible_after"] != 0)
            ):
                raise NativeMt5Error("terminal_window_visibility_failed")
            return record
        finally:
            if task_created:
                subprocess.run(
                    ["schtasks", "/End", "/TN", task],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                completed = subprocess.run(
                    ["schtasks", "/Delete", "/TN", task, "/F"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                cleanup_failed = completed.returncode != 0
            for path in (
                helper_temporary,
                helper,
                request_temporary,
                request,
                result_temporary,
                result,
                launcher,
            ):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    cleanup_failed = True
            if cleanup_failed and sys.exc_info()[0] is None:
                raise NativeMt5Error("terminal_window_cleanup_failed")

    def _ready_status(
        self,
        pid: int,
        account: dict[str, Any],
        heartbeat: dict[str, Any],
    ) -> NativeMt5Status:
        self._release_interactive_task()
        self.set_terminal_window_visibility(pid, visible=False)
        return NativeMt5Status(pid, account, heartbeat, self.files)

    def _wait_for_heartbeat(
        self, timeout: float, login: int | None = None, server: str | None = None
    ) -> NativeMt5Status:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._check_cancelled()
            pid = 0
            if self._process is not None:
                pid = self._process.pid
            else:
                # The one-shot scheduled task is deleted immediately after MT5
                # authorizes, while its terminal child intentionally remains alive.
                # Evidence must bind that exact running child rather than returning
                # PID 0 after the task handle has been released.
                pids = self._running_terminal_pids()
                pid = pids[0] if pids else 0
            if self._process is not None and self._process.poll() is not None:
                # MT5 may detach from the short-lived launcher process after reading /config.
                # Accept only the exact executable inside this isolated instance, never an
                # arbitrary terminal64.exe elsewhere on the host.
                pids = self._running_terminal_pids()
                if not pids:
                    raise NativeMt5Error("mt5_process_crashed")
                pid = pids[0]
            account_raw = self._read_json(self.files / "account.json")
            heartbeat_raw = self._read_json(self.files / "heartbeat.json")
            account = self._payload(account_raw, "account") if account_raw else None
            heartbeat = self._payload(heartbeat_raw, "heartbeat") if heartbeat_raw else None
            if self._readiness_not_before is not None:
                account_generated = self._envelope_generated_at(account_raw)
                heartbeat_generated = self._envelope_generated_at(
                    heartbeat_raw
                )
                account_sequence = (
                    account_raw.get("sequence") if account_raw else None
                )
                heartbeat_sequence = (
                    heartbeat_raw.get("sequence")
                    if heartbeat_raw
                    else None
                )
                if (
                    account_generated is None
                    or heartbeat_generated is None
                    or account_generated < self._readiness_not_before
                    or heartbeat_generated < self._readiness_not_before
                    or type(account_sequence) is not int
                    or account_sequence <= 0
                    or heartbeat_sequence != account_sequence
                ):
                    time.sleep(1)
                    continue
            if heartbeat is None:
                time.sleep(1)
                continue
            if login is None:
                return self._ready_status(pid, account or {}, heartbeat)
            if account is None:
                time.sleep(1)
                continue
            observed_login = str(account.get("login", ""))
            if observed_login in ("", "0"):
                time.sleep(1)
                continue
            if observed_login != str(login):
                raise NativeMt5Error("identity_mismatch")
            if str(account.get("server", "")).casefold() != str(server).casefold():
                raise NativeMt5Error("server_identity_mismatch")
            account_identity = (
                account_raw.get("account_identity")
                if account_raw
                else None
            )
            heartbeat_identity = (
                heartbeat_raw.get("account_identity")
                if heartbeat_raw
                else None
            )
            expected_identity = {
                "login": str(login),
                "server": str(server),
            }
            if (
                account_identity != expected_identity
                or heartbeat_identity != expected_identity
                or str(account_raw.get("server_identity", "")).casefold()
                != str(server).casefold()
                or str(heartbeat_raw.get("server_identity", "")).casefold()
                != str(server).casefold()
            ):
                raise NativeMt5Error("server_identity_mismatch")
            if not heartbeat.get("terminal_connected", False):
                time.sleep(1)
                continue
            if bool(account.get("trade_allowed", True)):
                raise NativeMt5Error("investor_readonly_not_verified")
            return self._ready_status(pid, account, heartbeat)
        logger.error(
            "native MT5 runtime: heartbeat.json never appeared within %.0fs "
            "(connection_id=%s, symbol=%s) -- check whether that symbol exists in this "
            "terminal's Market Watch (see TRADEJOURNAL_MT5_STARTUP_SYMBOL)",
            timeout,
            self.connection_id,
            self._last_symbol,
        )
        raise NativeMt5Error("terminal_not_ready")

    @staticmethod
    def _envelope_generated_at(
        record: dict[str, Any] | None,
    ) -> datetime | None:
        if record is None:
            return None
        value = record.get("generated_at")
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc)

    def _running_terminal_pids(self) -> list[int]:
        return self._running_executable_pids(self.terminal)

    def _running_metaeditor_pids(self) -> list[int]:
        return self._running_executable_pids(self.terminal_root / "MetaEditor64.exe")

    @staticmethod
    def _running_executable_pids(executable: Path) -> list[int]:
        try:
            import psutil
        except ImportError:
            return []
        result = []
        for process in psutil.process_iter(("pid", "exe")):
            try:
                if (
                    process.info["exe"]
                    and Path(process.info["exe"]).resolve() == executable.resolve()
                ):
                    result.append(int(process.info["pid"]))
            except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
                continue
        return result

    def start(
        self,
        *,
        login: int,
        server: str,
        connection_endpoint: str | None = None,
        investor_password: str,
        expert_binary: Path,
        history_mode: str = "new_only",
        history_from: datetime | None = None,
        symbol: str = "EURUSD",
        # The bounded investor-proof window starts only after authentication succeeded.
        # It is intentionally separate from short boot/readiness checks: a broker may take
        # time to emit its journal state, but we never start the EA without that proof.
        timeout: float = 300.0,
    ) -> NativeMt5Status:
        if not self.terminal.is_file():
            raise NativeMt5Error("terminal_start_failed")
        self._recover_pending_verified_vendor_updates()
        symbol = self._startup_symbol(symbol)
        self._last_symbol = symbol
        self.install_expert(expert_binary, history_mode, history_from)
        bootstrap: Path | None = None
        startup: Path | None = None
        try:
            startup_server = connection_endpoint or server
            # Phase 1: authenticate with the supplied investor password and ask MT5 to persist it
            # in Config/accounts.dat.  No chart or EA is opened during this first-start window.
            bootstrap = self._write_startup_config(
                login,
                startup_server,
                investor_password,
                symbol,
                keep_private=True,
                start_expert=False,
                filename="login-bootstrap.ini",
            )
            checkpoint, observed_server = self._start_and_wait_for_authorization(
                bootstrap,
                login,
                server,
                min(timeout, 120.0),
                startup_server,
            )
            effective_server = (
                observed_server.strip()
                if isinstance(observed_server, str)
                and observed_server.strip()
                else server
            )
            self._wait_for_account_database(min(timeout, 15.0))
            # A broker cache token can improve the initial chart choice for suffix-only
            # catalogues, but it is deliberately non-blocking.  If the private .dat format is
            # absent or opaque (as observed with FTMO), start Discovery with the configured
            # symbol and let the official in-terminal MQL5 catalogue resolve the final value.
            cached_symbol = self._cached_broker_symbol(login, effective_server, symbol)
            shared_symbol = self._broker_symbol_hint(effective_server)
            bootstrap_symbols = self._discovery_startup_symbols(
                symbol,
                cached_symbol,
                shared_symbol,
            )
            self._secure_delete_config(bootstrap)
            bootstrap = None
            if not self.stop():
                raise NativeMt5Error("terminal_stop_failed")
            self._remove_generated_example_code()

            # Phase 2: [StartUp].Script is chart-bound. A broker that exposes only
            # EURUSD.x (or another suffix) leaves a generic EURUSD chart black and
            # never calls OnStart. Discovery writes an identity-bound marker as its
            # first action; a missing marker lets us rotate through a bounded set of
            # non-trading chart aliases before granting the full synchronization window.
            started = False
            for bootstrap_symbol in bootstrap_symbols:
                self._reset_managed_chart_profile()
                self._remove_readiness_files()
                self._write_symbol_preference(symbol)
                self._last_symbol = bootstrap_symbol
                startup = self._write_startup_config(
                    login,
                    effective_server,
                    investor_password,
                    bootstrap_symbol,
                    keep_private=True,
                    start_expert=False,
                    script_name="TradeJournal\\TradeJournalDiscovery",
                    filename="startup.ini",
                )
                self._start_and_wait_for_authorization(
                    startup,
                    login,
                    effective_server,
                    min(timeout, 120.0),
                )
                if self._wait_for_discovery_start(
                    bootstrap_symbol,
                    min(timeout, 3.0),
                ):
                    started = True
                    break
                self._secure_delete_config(startup)
                startup = None
                if not self.stop(timeout=3.0):
                    raise NativeMt5Error("terminal_stop_failed")
            if not started:
                raise NativeMt5Error("broker_symbol_probe_failed")

            investor_password = ""
            gc.collect()
            resolved_symbol = self._probe_broker_symbol(
                symbol,
                login,
                effective_server,
                min(timeout, 120.0),
            )
            self._last_symbol = resolved_symbol
            self._install_bridge_template(resolved_symbol)
            self._publish_bridge_handoff()
            status = self._wait_for_heartbeat(
                min(timeout, 90.0),
                login,
                effective_server,
            )
            self._publish_pending_verified_vendor_updates()
            if effective_server.casefold() == server.casefold():
                return status
            return replace(
                status,
                requested_server=server,
                effective_server=effective_server,
            )
        except Exception:
            # A failed bootstrap has no consumer yet, so its isolated terminal must not be
            # retained. Successful starts deliberately remain alive for history/live sync.
            self.stop()
            self._discard_pending_verified_vendor_updates()
            raise
        finally:
            investor_password = ""
            gc.collect()
            self._secure_delete_config(bootstrap)
            self._secure_delete_config(startup)

    def resume(
        self,
        *,
        login: int,
        server: str,
        expert_binary: Path,
        history_mode: str = "new_only",
        history_from: datetime | None = None,
        timeout: float = 120.0,
    ) -> NativeMt5Status:
        """Resume a previously provisioned account without reusing a plaintext password."""
        if not self.terminal.is_file():
            raise NativeMt5Error("terminal_start_failed")
        self._recover_pending_verified_vendor_updates()
        symbol = self._bridge_template_symbol()
        self._last_symbol = symbol
        self.install_expert(expert_binary, history_mode, history_from)
        self._remove_readiness_files()
        self._reset_managed_chart_profile()
        config = self._write_startup_config(
            login,
            server,
            None,
            symbol,
            keep_private=True,
            start_expert=True,
            filename="resume.ini",
        )
        try:
            self._start_and_wait_for_authorization(
                config,
                login,
                server,
                min(timeout, 90.0),
            )
            status = self._wait_for_heartbeat(min(timeout, 60.0), login, server)
            # A freshly materialized MetaQuotes distribution can recreate its
            # bundled example EX5 directories during the first authenticated
            # launch.  Those files are intentionally absent from the sealed
            # template, so leave the terminal stopped only long enough to
            # remove the fixed vendor-example paths and perform one clean
            # restart.  The second launch uses the now-initialized private
            # profile and must preserve the target code manifest.
            if self._generated_example_code_present():
                if not self.stop():
                    raise NativeMt5Error(
                        "generated_example_cleanup_stop_failed"
                    )
                self._remove_generated_example_code()
                self._remove_readiness_files()
                self._reset_managed_chart_profile()
                self._start_and_wait_for_authorization(
                    config,
                    login,
                    server,
                    min(timeout, 90.0),
                )
                status = self._wait_for_heartbeat(
                    min(timeout, 60.0),
                    login,
                    server,
                )
            self._publish_pending_verified_vendor_updates()
            return status
        except Exception:
            self.stop()
            self._discard_pending_verified_vendor_updates()
            raise
        finally:
            self._secure_delete_config(config)

    def start_no_login(
        self,
        *,
        expert_binary: Path,
        symbol: str = "EURUSD",
        timeout: float = 90.0,
    ) -> NativeMt5Status:
        """Verify that a generic terminal loads the EA without credentials or MT5 login."""
        if not self.terminal.is_file():
            raise NativeMt5Error("terminal_start_failed")
        symbol = self._startup_symbol(symbol)
        self._last_symbol = symbol
        self.install_expert(expert_binary, "new_only")
        self._install_bridge_template(symbol)
        self._reset_managed_chart_profile()
        self._remove_readiness_files()
        config = self._write_startup_config(None, None, None, symbol)
        try:
            self._start_process(config)
            return self._wait_for_heartbeat(timeout)
        except Exception:
            self.stop()
            raise
        finally:
            self._secure_delete_config(config)

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any] | None:
        try:
            value = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def stop(self, timeout: float = 15.0) -> bool:
        if self._interactive_task:
            subprocess.run(["schtasks", "/End", "/TN", self._interactive_task], capture_output=True, check=False)
            subprocess.run(["schtasks", "/Delete", "/TN", self._interactive_task, "/F"], capture_output=True, check=False)
            self._interactive_task = None
        try:
            (self.state / "launch-terminal.cmd").unlink(missing_ok=True)
        except OSError:
            pass
        process = self._process
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(5)
        self._process = None
        pids = list(
            dict.fromkeys(
                (*self._running_terminal_pids(), *self._running_metaeditor_pids())
            )
        )
        if not pids:
            return True
        try:
            import psutil
        except ImportError:
            return False
        try:
            # A terminal can naturally disappear between the executable scan above and
            # Process(pid).  That is already a successful cleanup, not a failure.
            candidates = []
            for pid in pids:
                try:
                    candidates.append(psutil.Process(pid))
                except psutil.NoSuchProcess:
                    continue

            for candidate in candidates:
                try:
                    candidate.terminate()
                except psutil.NoSuchProcess:
                    continue

            _, alive = psutil.wait_procs(candidates, timeout=timeout)
            kill_candidates = []
            for candidate in alive:
                try:
                    candidate.kill()
                    kill_candidates.append(candidate)
                except psutil.NoSuchProcess:
                    continue

            # kill() is asynchronous on Windows.  Do not immediately rescan and turn
            # that normal termination race into terminal_stop_failed.
            if kill_candidates:
                _, still_alive = psutil.wait_procs(
                    kill_candidates,
                    timeout=min(5.0, timeout),
                )
                if still_alive:
                    return False
            return not self._running_terminal_pids() and not self._running_metaeditor_pids()
        except (psutil.AccessDenied, OSError):
            # Fail closed only when the exact instance process cannot be controlled.
            return False
