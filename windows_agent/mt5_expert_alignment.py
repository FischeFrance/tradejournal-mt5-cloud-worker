"""First phase of the nightly maintenance: the pinned Bridge goes into the golden template.

Every terminal the maintenance rotates is a copy of the golden template, and every restart
installs the Bridge the service pins. After a Bridge release the template still carried the
previous one, so each rotated terminal changed under its own seal during the restart and the
rotation rolled back. The template therefore takes the pinned Bridge before any MT5 work,
through the template manager's own reseal, which proves that only the Bridge changed.

A journal makes the swap crash safe: at startup, before the template is validated, a swap
that never received its new seal gets the previous Bridge back.
"""
from __future__ import annotations

import logging
from pathlib import Path
from threading import Event, RLock
from typing import Any

from worker.atomic_file import (
    durable_copy_contents,
    durable_replace,
    fsync_directory,
    make_regular_file_writable,
    unlink_readonly_file,
)

from .provisioning.mt5_instance import _MANAGED_RUNTIME_ASSETS, InstanceProvisioner
from .provisioning.mt5_template import _MARKER_NAME, Mt5TemplateManager
from .state_store import atomic_json, read_json

logger = logging.getLogger(__name__)

JOURNAL_NAME = "template-expert.json"


class Mt5TemplateExpertError(RuntimeError):
    """A sanitized failure while putting the pinned Bridge into the template."""


def copy_verified(source: Path, destination: Path, digest: str) -> None:
    """Replace ``destination`` with ``source`` only once the copy proves ``digest``."""
    staged = destination.with_name(destination.name + ".staged")
    unlink_readonly_file(staged)
    try:
        durable_copy_contents(source, staged)
        if InstanceProvisioner._sha256(staged) != digest:
            raise Mt5TemplateExpertError("MT5 expert copy is invalid")
        if destination.exists():
            make_regular_file_writable(destination)
        durable_replace(staged, destination)
    finally:
        unlink_readonly_file(staged)
    if InstanceProvisioner._sha256(destination) != digest:
        raise Mt5TemplateExpertError("MT5 expert copy is invalid")
    fsync_directory(destination.parent)


class Mt5TemplateExpertSync:
    def __init__(
        self,
        template_manager: Mt5TemplateManager,
        *,
        template_lock: RLock,
        expert_binary: Path,
        expert_sha256: str,
        backup_root: Path,
        instance_pool: Any | None = None,
    ) -> None:
        self.template_manager = template_manager
        self.template_lock = template_lock
        self.expert_binary = Path(expert_binary)
        self.expert_sha256 = expert_sha256.strip().lower()
        self.backup_root = Path(backup_root)
        self.instance_pool = instance_pool
        self.template_root = Path(template_manager.source_terminal).parent
        self.template_bridge = self.template_root / _MANAGED_RUNTIME_ASSETS[0]
        self.journal = self.backup_root / JOURNAL_NAME

    def _backup(self, digest: str) -> Path:
        return self.backup_root / f"TradeJournalBridge-{digest}.ex5"

    def _template_sealed(self) -> bool:
        """False only when a schema-2 seal no longer matches the template's executables."""
        marker_path = self.template_root / _MARKER_NAME
        marker = read_json(marker_path, {}) if marker_path.is_file() else {}
        if marker.get("schema_version") != 2:
            return True
        return marker.get("code_manifest_sha256") == InstanceProvisioner._code_manifest(
            self.template_root
        )

    def _put_back(self, digest: str) -> None:
        backup = self._backup(digest)
        if not backup.is_file() or InstanceProvisioner._sha256(backup) != digest:
            raise Mt5TemplateExpertError("MT5 template expert backup is invalid")
        copy_verified(backup, self.template_bridge, digest)

    def recover_interrupted(self) -> None:
        """Undo a swap that never received its seal; never raises at startup."""
        with self.template_lock:
            if not self.journal.is_file():
                return
            try:
                journal = read_json(self.journal, {})
                previous = journal.get("previous_expert_sha256")
                current = InstanceProvisioner._sha256(self.template_bridge)
                if current not in (previous, journal.get("expert_sha256")):
                    raise Mt5TemplateExpertError("MT5 template expert is unknown")
                if current != previous and not self._template_sealed():
                    self._put_back(previous)
                    logger.warning(
                        "restored the previous Bridge in the MT5 template after an interrupted update"
                    )
                self.journal.unlink()
            except Exception:
                # The journal stays, so the next maintenance stops at phase one and reports it.
                logger.exception("MT5 template expert recovery needs attention")

    def align(self, stop_event: Event) -> bool:
        """Put the pinned Bridge into the template; return whether it changed."""
        with self.template_lock:
            if self.journal.exists():
                self.recover_interrupted()
                if self.journal.exists():
                    raise Mt5TemplateExpertError("MT5 template expert update needs attention")
            self.template_manager.recover_interrupted_rotation()
            previous = InstanceProvisioner._sha256(self.template_bridge)
            if previous == self.expert_sha256:
                return False
            if stop_event.is_set():
                raise Mt5TemplateExpertError("MT5 maintenance was interrupted")
            if InstanceProvisioner._sha256(self.expert_binary) != self.expert_sha256:
                raise Mt5TemplateExpertError("MT5 pinned expert is invalid")
            self.backup_root.mkdir(parents=True, exist_ok=True)
            backup = self._backup(previous)
            if not backup.is_file() or InstanceProvisioner._sha256(backup) != previous:
                copy_verified(self.template_bridge, backup, previous)
            atomic_json(
                self.journal,
                {
                    "schema_version": 1,
                    "previous_expert_sha256": previous,
                    "expert_sha256": self.expert_sha256,
                },
            )
            try:
                copy_verified(self.expert_binary, self.template_bridge, self.expert_sha256)
                self.template_manager.reseal_managed_code_deployment(
                    backup,
                    previous,
                    self.expert_sha256,
                )
            except Exception as exc:
                try:
                    if (
                        InstanceProvisioner._sha256(self.template_bridge) != previous
                        and not self._template_sealed()
                    ):
                        self._put_back(previous)
                    self.journal.unlink()
                except Exception:
                    logger.exception("MT5 template expert rollback is incomplete")
                raise Mt5TemplateExpertError("MT5 template expert update failed") from exc
            self.template_manager.recover_interrupted_rotation()
            self.journal.unlink()
            logger.info(
                "MT5 template now carries the pinned Bridge (%s, was %s)",
                self.expert_sha256[:12],
                previous[:12],
            )
            if self.instance_pool is not None:
                # Slots copied from the previous template no longer match its seal: replace them
                # now, so no new account starts from the previous Bridge.
                try:
                    self.instance_pool.accept_template_rotation(
                        self.template_manager.current_sha256,
                        replenish=True,
                        stop_event=stop_event,
                    )
                except Exception:
                    if stop_event.is_set():
                        raise
                    logger.exception("MT5 pool rebuild failed after the template Bridge update")
            return True
