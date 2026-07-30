from __future__ import annotations

"""Prebuilt, credential-free MT5 instance pool.

Pool entries are complete filesystem copies, never running processes.  A slot
is built and verified outside the user request path, then claimed with an
atomic directory move.  Claimed or failed slots are never returned to READY.
"""

import logging
import os
import shutil
import time
from pathlib import Path
from threading import Event
from uuid import UUID, uuid4

from worker.atomic_file import durable_replace, fsync_directory

from ..state_store import atomic_json, read_json
from ..security import canonical_uuid
from .instance_layout import SUBDIRS, InstanceLayout
from .mt5_instance import InstanceProvisioner


logger = logging.getLogger(__name__)

POOL_SCHEMA_VERSION = 1
DEFAULT_REPLENISH_INTERVAL_SECONDS = 5.0

_PRIVATE_DIRECTORIES = (
    Path("terminal/Bases"),
    Path("terminal/Logs"),
    Path("terminal/MQL5/Files"),
    Path("terminal/MQL5/Logs"),
    Path("terminal/Tester/cache"),
    Path("terminal/Tester/logs"),
)
_PRIVATE_CONFIG_NAMES = frozenset(
    {
        "accounts.dat",
        "accounts.ini",
        "community.ini",
        "signals.ini",
    }
)


class InstancePoolError(RuntimeError):
    """Sanitized pool failure; paths and template contents are not exposed."""


class Mt5InstancePool:
    def __init__(
        self,
        *,
        pool_root: Path,
        instances_root: Path,
        secrets_root: Path,
        source_terminal: Path,
        expected_terminal_sha256: str | None,
        target_size: int = 2,
        max_size: int = 3,
    ) -> None:
        if (
            not isinstance(target_size, int)
            or isinstance(target_size, bool)
            or not 0 <= target_size <= 8
            or not isinstance(max_size, int)
            or isinstance(max_size, bool)
            or not max(1, target_size) <= max_size <= 8
        ):
            raise ValueError("MT5 instance pool size is invalid")
        self.pool_root = Path(pool_root).resolve()
        self.instances_root = Path(instances_root).resolve()
        self.secrets_root = Path(secrets_root).resolve()
        self.source_terminal = Path(source_terminal).resolve()
        self.expected_terminal_sha256 = expected_terminal_sha256
        self.target_size = target_size
        self.max_size = max_size
        self.building_root = self.pool_root / "building"
        self.ready_root = self.pool_root / "ready"
        self.claimed_root = self.pool_root / "claimed"
        self.reservations_root = self.pool_root / "reservations"
        self._validate_root_relationships()
        self._ensure_roots()

    def _validate_root_relationships(self) -> None:
        pool = os.path.normcase(os.fspath(self.pool_root))
        instances = os.path.normcase(os.fspath(self.instances_root))
        try:
            common = os.path.commonpath((pool, instances))
        except ValueError as exc:
            raise ValueError(
                "pool and instance roots must share one filesystem"
            ) from exc
        if common in (pool, instances):
            raise ValueError(
                "pool and instance roots must be independent"
            )

    def _ensure_roots(self) -> None:
        self.instances_root.mkdir(parents=True, exist_ok=True)
        self.pool_root.mkdir(parents=True, exist_ok=True)
        for root in (
            self.building_root,
            self.ready_root,
            self.claimed_root,
            self.reservations_root,
        ):
            root.mkdir(exist_ok=True)
        try:
            if self.pool_root.stat().st_dev != self.instances_root.stat().st_dev:
                raise ValueError(
                    "pool and instance roots must share one filesystem"
                )
        except OSError as exc:
            raise ValueError("pool filesystem cannot be verified") from exc
        for root in (
            self.pool_root,
            self.building_root,
            self.ready_root,
            self.claimed_root,
            self.reservations_root,
            self.instances_root,
        ):
            if (
                InstanceProvisioner._is_reparse_point(root)
                or not root.is_dir()
            ):
                raise ValueError("MT5 instance pool root is unsafe")

    @staticmethod
    def _uuid(value: str) -> str:
        try:
            parsed = UUID(value)
        except (AttributeError, TypeError, ValueError) as exc:
            raise InstancePoolError("pool slot identity is invalid") from exc
        if parsed.version != 4 or str(parsed) != value:
            raise InstancePoolError("pool slot identity is invalid")
        return value

    @staticmethod
    def _remove_tree(path: Path) -> None:
        if not path.exists():
            return
        if InstanceProvisioner._is_reparse_point(path) or not path.is_dir():
            raise InstancePoolError("pool cleanup target is unsafe")
        shutil.rmtree(path)

    def recover_incomplete(self) -> None:
        """Remove only never-published pool work left by an interrupted agent."""

        self._ensure_roots()
        for root in (self.building_root, self.claimed_root):
            for child in tuple(root.iterdir()):
                self._remove_tree(child)
        for reservation in tuple(self.reservations_root.iterdir()):
            if (
                InstanceProvisioner._is_reparse_point(reservation)
                or not reservation.is_file()
            ):
                raise InstancePoolError("pool reservation is unsafe")
            reservation.unlink()

    @staticmethod
    def _empty_directory(path: Path) -> None:
        if not path.exists():
            return
        if InstanceProvisioner._is_reparse_point(path) or not path.is_dir():
            raise InstancePoolError("private pool directory is unsafe")
        shutil.rmtree(path)

    def _sanitize_slot(self, root: Path) -> None:
        for relative in _PRIVATE_DIRECTORIES:
            self._empty_directory(root / relative)
        config = root / "terminal" / "Config"
        if config.exists():
            if (
                InstanceProvisioner._is_reparse_point(config)
                or not config.is_dir()
            ):
                raise InstancePoolError("terminal config is unsafe")
            for child in tuple(config.iterdir()):
                if child.name.casefold() not in _PRIVATE_CONFIG_NAMES:
                    continue
                if child.is_dir():
                    self._remove_tree(child)
                elif (
                    child.is_file()
                    and not InstanceProvisioner._is_reparse_point(child)
                ):
                    child.unlink()
                else:
                    raise InstancePoolError(
                        "private terminal config is unsafe"
                    )
        for name in ("worker", "secrets", "logs", "data"):
            directory = root / name
            if (
                not directory.is_dir()
                or InstanceProvisioner._is_reparse_point(directory)
            ):
                raise InstancePoolError("pool slot layout is unsafe")
            for child in tuple(directory.iterdir()):
                if child.is_dir():
                    self._remove_tree(child)
                elif (
                    child.is_file()
                    and not InstanceProvisioner._is_reparse_point(child)
                ):
                    child.unlink()
                else:
                    raise InstancePoolError("pool slot contains private data")

    def _assert_sanitized(self, root: Path) -> None:
        InstanceProvisioner._validate_source_tree(root)
        for relative in _PRIVATE_DIRECTORIES:
            if (root / relative).exists():
                raise InstancePoolError("pool slot contains private runtime data")
        config = root / "terminal" / "Config"
        if config.is_dir() and any(
            child.name.casefold() in _PRIVATE_CONFIG_NAMES
            for child in config.iterdir()
        ):
            raise InstancePoolError("pool slot contains private account data")
        for name in ("worker", "secrets", "logs", "data"):
            directory = root / name
            if not directory.is_dir() or any(directory.iterdir()):
                raise InstancePoolError("pool slot is not anonymous")

    def _pool_record(self, root: Path) -> dict:
        try:
            value = read_json(root / "state" / "pool.json")
        except (OSError, ValueError) as exc:
            raise InstancePoolError("pool slot metadata is invalid") from exc
        expected_fields = {
            "schema_version",
            "slot_id",
            "status",
            "created_at_unix_ms",
            "terminal_sha256",
            "template_manifest_sha256",
            "code_manifest_sha256",
        }
        if (
            set(value) != expected_fields
            or value.get("schema_version") != POOL_SCHEMA_VERSION
            or value.get("status") != "READY"
            or not isinstance(value.get("created_at_unix_ms"), int)
            or isinstance(value.get("created_at_unix_ms"), bool)
            or value["created_at_unix_ms"] <= 0
        ):
            raise InstancePoolError("pool slot metadata is invalid")
        self._uuid(value.get("slot_id"))
        return value

    def _validate_slot(self, root: Path) -> dict:
        if (
            InstanceProvisioner._is_reparse_point(root)
            or not root.is_dir()
        ):
            raise InstancePoolError("pool slot is unsafe")
        record = self._pool_record(root)
        try:
            instance = read_json(root / "state" / "instance.json")
            if (
                instance.get("connection_id") != record["slot_id"]
                or instance.get("status") != "provisioned"
                or instance.get("terminal_sha256")
                != record["terminal_sha256"]
                or instance.get("template_manifest_sha256")
                != record["template_manifest_sha256"]
                or instance.get("template_code_manifest_sha256")
                != record["code_manifest_sha256"]
            ):
                raise InstancePoolError("pool slot binding is invalid")
            if (
                InstanceProvisioner._tree_manifest(root / "terminal")
                != record["template_manifest_sha256"]
            ):
                raise InstancePoolError("pool slot manifest mismatch")
            InstanceProvisioner._validate_published_instance(
                root,
                instance,
                self.expected_terminal_sha256,
            )
            self._assert_sanitized(root)
        except InstancePoolError:
            raise
        except (OSError, ValueError) as exc:
            raise InstancePoolError("pool slot validation failed") from exc
        return instance

    def build_one(self) -> Path:
        """Build, sanitize and atomically publish one independent READY slot."""

        self._ensure_roots()
        if self.ready_count() >= self.max_size:
            raise InstancePoolError("MT5 instance pool is already full")
        slot_id = str(uuid4())
        builder = InstanceProvisioner(
            self.building_root,
            self.secrets_root,
        )
        root: Path | None = None
        destination = self.ready_root / slot_id
        try:
            root = builder.provision(
                slot_id,
                self.source_terminal,
                self.expected_terminal_sha256,
            )
            self._sanitize_slot(root)
            instance = read_json(root / "state" / "instance.json")
            terminal_root = root / "terminal"
            instance["template_manifest_sha256"] = (
                InstanceProvisioner._tree_manifest(terminal_root)
            )
            instance["template_code_manifest_sha256"] = (
                InstanceProvisioner._code_manifest(terminal_root)
            )
            atomic_json(root / "state" / "instance.json", instance)
            atomic_json(
                root / "state" / "pool.json",
                {
                    "schema_version": POOL_SCHEMA_VERSION,
                    "slot_id": slot_id,
                    "status": "READY",
                    "created_at_unix_ms": int(time.time() * 1000),
                    "terminal_sha256": instance["terminal_sha256"],
                    "template_manifest_sha256": instance[
                        "template_manifest_sha256"
                    ],
                    "code_manifest_sha256": instance[
                        "template_code_manifest_sha256"
                    ],
                },
            )
            InstanceProvisioner._sync_tree(root)
            self._validate_slot(root)
            if destination.exists():
                raise InstancePoolError("pool slot collision")
            durable_replace(root, destination)
            root = None
            return destination
        finally:
            if root is not None and root.exists():
                self._remove_tree(root)

    def ready_slots(self) -> tuple[Path, ...]:
        self._ensure_roots()
        slots: list[Path] = []
        for child in sorted(
            self.ready_root.iterdir(),
            key=lambda value: value.name,
        ):
            try:
                self._uuid(child.name)
            except InstancePoolError:
                continue
            if (
                child.is_dir()
                and not InstanceProvisioner._is_reparse_point(child)
            ):
                slots.append(child)
        return tuple(slots)

    def ready_count(self) -> int:
        return len(self.ready_slots())

    def maintain_once(self, stop_event: Event | None = None) -> int:
        """Replenish sequentially up to target_size and return READY count."""

        while self.ready_count() < self.target_size:
            if stop_event is not None and stop_event.is_set():
                break
            self.build_one()
        return self.ready_count()

    def replenish_forever(
        self,
        stop_event: Event,
        interval_seconds: float = DEFAULT_REPLENISH_INTERVAL_SECONDS,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("pool replenish interval is invalid")
        while not stop_event.is_set():
            try:
                self.maintain_once(stop_event)
            except Exception:
                logger.exception("MT5 instance pool replenishment failed")
            stop_event.wait(interval_seconds)

    def _reserve(self, connection_id: str) -> Path:
        try:
            connection_id = canonical_uuid(connection_id)
        except (AttributeError, TypeError, ValueError) as exc:
            raise InstancePoolError(
                "connection identity is invalid"
            ) from exc
        reservation = self.reservations_root / connection_id
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        try:
            descriptor = os.open(reservation, flags, 0o600)
        except FileExistsError as exc:
            raise InstancePoolError(
                "connection already has an in-flight pool claim"
            ) from exc
        try:
            os.write(descriptor, b"reserved\n")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        fsync_directory(self.reservations_root)
        return reservation

    def claim(self, connection_id: str) -> Path | None:
        """Atomically consume one READY slot for a canonical connection UUID.

        ``None`` means the pool was empty; the caller may use the existing
        direct-copy fallback. Invalid slots are destroyed rather than reused.
        """

        try:
            connection_id = canonical_uuid(connection_id)
        except (AttributeError, TypeError, ValueError) as exc:
            raise InstancePoolError(
                "connection identity is invalid"
            ) from exc
        destination = InstanceLayout(
            self.instances_root,
            connection_id,
        ).path
        if destination.exists():
            return InstanceProvisioner(
                self.instances_root,
                self.secrets_root,
            ).validate(connection_id, self.expected_terminal_sha256)
        self._ensure_roots()
        reservation = self._reserve(connection_id)
        claimed: Path | None = None
        published = False
        try:
            if destination.exists():
                return InstanceProvisioner(
                    self.instances_root,
                    self.secrets_root,
                ).validate(
                    connection_id,
                    self.expected_terminal_sha256,
                )
            for slot in self.ready_slots():
                candidate = self.claimed_root / connection_id
                try:
                    durable_replace(slot, candidate)
                except FileExistsError:
                    raise InstancePoolError(
                        "connection claim target already exists"
                    )
                except OSError as exc:
                    if not slot.exists():
                        continue
                    raise InstancePoolError(
                        "pool slot claim failed"
                    ) from exc
                claimed = candidate
                try:
                    instance = self._validate_slot(claimed)
                except InstancePoolError:
                    self._remove_tree(claimed)
                    claimed = None
                    continue
                state = {
                    **instance,
                    "connection_id": connection_id,
                    "status": "provisioned",
                    "terminal": str(
                        destination / "terminal" / "terminal64.exe"
                    ),
                    "pool_slot_id": slot.name,
                    "pool_claimed_at_unix_ms": int(time.time() * 1000),
                }
                atomic_json(
                    claimed / "state" / "instance.json",
                    state,
                )
                (claimed / "state" / "pool.json").unlink()
                if destination.exists():
                    raise InstancePoolError(
                        "connection publication already exists"
                    )
                durable_replace(claimed, destination)
                claimed = None
                published = True
                return InstanceProvisioner(
                    self.instances_root,
                    self.secrets_root,
                ).validate(
                    connection_id,
                    self.expected_terminal_sha256,
                )
            return None
        except Exception:
            if published and destination.exists():
                self._remove_tree(destination)
            raise
        finally:
            if claimed is not None and claimed.exists():
                self._remove_tree(claimed)
            reservation.unlink(missing_ok=True)
            fsync_directory(self.reservations_root)
