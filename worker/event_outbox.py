"""Outbox FIFO persistente per gli eventi trade-sync.

Il formato v2 conserva esplicitamente i pending in una lista ordinata. Ogni mutazione usa un
file temporaneo 0600, ``fsync`` e ``os.replace`` nella stessa directory. I file v1 vengono
migrati senza perdere eventi; quando possibile ``event_time`` ricostruisce l'ordine causale che
la serializzazione ordinata per chiave del vecchio formato poteva avere alterato.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

from event_sender import EventSender

logger = logging.getLogger("mt5_worker.event_outbox")

_FORMAT_VERSION = 2
_LEGACY_FORMAT_VERSION = 1
_SECURE_FILE_MODE = 0o600


class OutboxError(RuntimeError):
    """Errore di consistenza o persistenza dell'outbox."""


@dataclass(frozen=True)
class DrainResult:
    sent: int = 0
    dry_run: int = 0
    dead_lettered: int = 0
    transient_failures: int = 0
    pending: int = 0


class EventOutbox:
    """Outbox JSON FIFO, persistente quando ``file_path`` e' valorizzato."""

    def __init__(self, file_path: Optional[str] = None) -> None:
        self.file_path = file_path
        if not file_path:
            self._state = self._empty_state()
            return

        self._state, migration_required = self._load()
        if migration_required:
            self._persist(self._state)

    @staticmethod
    def _empty_state() -> Dict[str, Any]:
        return {"version": _FORMAT_VERSION, "pending": [], "dead_letter": {}}

    def pending_count(self) -> int:
        return len(self._state["pending"])

    def dead_letter_count(self) -> int:
        return len(self._state["dead_letter"])

    def pending_payloads(self) -> Dict[str, Dict[str, Any]]:
        """Copia indicizzata che mantiene l'ordine FIFO di inserimento."""
        return {
            payload["event_id"]: copy.deepcopy(payload)
            for payload in self._state["pending"]
        }

    def dead_letters(self) -> Dict[str, Dict[str, Any]]:
        return copy.deepcopy(self._state["dead_letter"])

    def enqueue_many(self, payloads: Iterable[Dict[str, Any]]) -> int:
        """Accoda atomicamente un batch, in ordine, deduplicandolo per ``event_id``."""
        batch = [copy.deepcopy(payload) for payload in payloads]
        for payload in batch:
            self._validate_payload(payload)

        new_state = copy.deepcopy(self._state)
        known_ids = {
            payload["event_id"] for payload in new_state["pending"]
        } | set(new_state["dead_letter"])
        added = 0
        for payload in batch:
            event_id = payload["event_id"]
            if event_id in known_ids:
                continue
            new_state["pending"].append(payload)
            known_ids.add(event_id)
            added += 1

        if added:
            self._replace_state(new_state)
        return added

    def drain(self, sender: EventSender) -> DrainResult:
        """Consegna i pending FIFO, fermandosi al primo fallimento transitorio.

        Un evento successivo puo' dipendere dal precedente (open -> modify -> close). Continuare
        dopo un errore transitorio potrebbe quindi trasformare un problema temporaneo in un 4xx
        permanente e perdere causalita'.
        """
        sent = dry_run = dead_lettered = transient_failures = 0
        index = 0

        while index < len(self._state["pending"]):
            payload = copy.deepcopy(self._state["pending"][index])
            event_id = payload["event_id"]
            result = sender.send(payload)

            if result.status == "sent":
                new_state = copy.deepcopy(self._state)
                new_state["pending"].pop(index)
                self._replace_state(new_state)
                sent += 1
                continue

            if result.status == "dry_run":
                # Dry-run prova il percorso di normalizzazione e logging, non costituisce una
                # consegna. Conservare l'evento consente di inviarlo dopo il passaggio a false.
                dry_run += 1
                break

            if result.failure_type == "permanent":
                new_state = copy.deepcopy(self._state)
                stored_payload = new_state["pending"].pop(index)
                new_state["dead_letter"][event_id] = {
                    "payload": stored_payload,
                    "failure_type": "permanent",
                    "http_status": result.http_status,
                    "error": result.error,
                    "attempts": result.attempts,
                }
                self._replace_state(new_state)
                dead_lettered += 1
                logger.error(
                    "Evento spostato in dead-letter dopo un rifiuto permanente "
                    "(http_status=%s).",
                    result.http_status,
                )
                continue

            # Include risultati legacy senza failure_type: conservarli e fermare il drain e' la
            # scelta fail-safe che preserva sia l'evento sia l'ordine dei successivi.
            transient_failures += 1
            break

        return DrainResult(
            sent=sent,
            dry_run=dry_run,
            dead_lettered=dead_lettered,
            transient_failures=transient_failures,
            pending=self.pending_count(),
        )

    def _load(self) -> Tuple[Dict[str, Any], bool]:
        assert self.file_path is not None
        if not os.path.lexists(self.file_path):
            return self._empty_state(), False

        state = self._read_json_file()
        if not isinstance(state, dict):
            raise OutboxError("Formato outbox non valido: la radice JSON deve essere un oggetto.")

        version = state.get("version")
        dead_letter = state.get("dead_letter")
        if not isinstance(dead_letter, dict):
            raise OutboxError("Formato outbox non valido: dead_letter deve essere un oggetto.")

        migration_required = version == _LEGACY_FORMAT_VERSION
        if migration_required:
            legacy_pending = state.get("pending")
            if not isinstance(legacy_pending, dict):
                raise OutboxError("Formato outbox v1 non valido: pending deve essere un oggetto.")
            for event_id, payload in legacy_pending.items():
                self._validate_keyed_payload(event_id, payload, "pending")
            pending = self._order_legacy_pending(list(legacy_pending.values()))
            state = {
                "version": _FORMAT_VERSION,
                "pending": pending,
                "dead_letter": dead_letter,
            }
        elif version == _FORMAT_VERSION:
            pending = state.get("pending")
            if not isinstance(pending, list):
                raise OutboxError("Formato outbox non valido: pending deve essere un array.")
        else:
            raise OutboxError("Versione del formato outbox non supportata.")

        self._validate_state(state)
        return state, migration_required

    def _read_json_file(self) -> Any:
        assert self.file_path is not None
        fd = -1
        try:
            path_stat = os.lstat(self.file_path)
            if stat.S_ISLNK(path_stat.st_mode):
                raise OutboxError("Outbox persistente non sicura: i symlink non sono ammessi.")

            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(self.file_path, flags)
            file_stat = os.fstat(fd)
            if not stat.S_ISREG(file_stat.st_mode):
                raise OutboxError("Outbox persistente non valida: atteso un file regolare.")
            if (path_stat.st_dev, path_stat.st_ino) != (file_stat.st_dev, file_stat.st_ino):
                raise OutboxError("Outbox cambiata durante l'apertura; caricamento rifiutato.")
            if stat.S_IMODE(file_stat.st_mode) != _SECURE_FILE_MODE:
                os.fchmod(fd, _SECURE_FILE_MODE)
                os.fsync(fd)

            with os.fdopen(fd, "r", encoding="utf-8") as handle:
                fd = -1
                return json.load(handle)
        except OutboxError:
            raise
        except (OSError, json.JSONDecodeError, UnicodeError) as exc:
            raise OutboxError("Outbox persistente illeggibile o corrotta.") from exc
        finally:
            if fd >= 0:
                os.close(fd)

    @classmethod
    def _validate_state(cls, state: Dict[str, Any]) -> None:
        pending = state["pending"]
        dead_letter = state["dead_letter"]
        pending_ids = set()
        for payload in pending:
            cls._validate_payload(payload)
            event_id = payload["event_id"]
            if event_id in pending_ids:
                raise OutboxError("Formato outbox non valido: event_id pending duplicato.")
            pending_ids.add(event_id)

        for event_id, record in dead_letter.items():
            if not isinstance(record, dict) or not isinstance(record.get("payload"), dict):
                raise OutboxError("Formato outbox non valido: record dead-letter incoerente.")
            cls._validate_keyed_payload(event_id, record["payload"], "dead-letter")
            if event_id in pending_ids:
                raise OutboxError("Formato outbox non valido: event_id presente in due stati.")

    @staticmethod
    def _validate_payload(payload: Any) -> None:
        if not isinstance(payload, dict):
            raise OutboxError("Ogni evento dell'outbox deve essere un oggetto.")
        event_id = payload.get("event_id")
        if not isinstance(event_id, str) or not event_id:
            raise OutboxError("Ogni evento dell'outbox deve avere un event_id stringa non vuoto.")

    @classmethod
    def _validate_keyed_payload(cls, event_id: Any, payload: Any, section: str) -> None:
        cls._validate_payload(payload)
        if payload["event_id"] != event_id:
            raise OutboxError(f"Formato outbox non valido: event_id {section} incoerente.")

    @staticmethod
    def _order_legacy_pending(payloads: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        parsed_times = []
        for index, payload in enumerate(payloads):
            value = payload.get("event_time")
            if not isinstance(value, str):
                return payloads
            normalized = f"{value[:-1]}+00:00" if value.endswith("Z") else value
            try:
                parsed = datetime.fromisoformat(normalized)
            except ValueError:
                return payloads
            if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
                return payloads
            parsed_times.append((parsed, index, payload))
        return [item[2] for item in sorted(parsed_times, key=lambda item: (item[0], item[1]))]

    def _replace_state(self, new_state: Dict[str, Any]) -> None:
        if self.file_path:
            self._persist(new_state)
        self._state = new_state

    def _persist(self, state: Dict[str, Any]) -> None:
        assert self.file_path is not None
        directory = os.path.dirname(self.file_path) or "."
        os.makedirs(directory, exist_ok=True)
        self._reject_symlink_target()
        fd, tmp_path = tempfile.mkstemp(
            prefix=f".{os.path.basename(self.file_path)}.", suffix=".tmp", dir=directory
        )
        try:
            os.fchmod(fd, _SECURE_FILE_MODE)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                fd = -1
                json.dump(state, handle, sort_keys=True, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            self._reject_symlink_target()
            os.replace(tmp_path, self.file_path)
            tmp_path = ""
            self._fsync_directory(directory)
        except BaseException:
            if fd >= 0:
                os.close(fd)
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except FileNotFoundError:
                    pass
            raise

    def _reject_symlink_target(self) -> None:
        assert self.file_path is not None
        try:
            mode = os.lstat(self.file_path).st_mode
        except FileNotFoundError:
            return
        if stat.S_ISLNK(mode):
            raise OutboxError("Outbox persistente non sicura: i symlink non sono ammessi.")

    @staticmethod
    def _fsync_directory(directory: str) -> None:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
        directory_fd = os.open(directory, flags)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
