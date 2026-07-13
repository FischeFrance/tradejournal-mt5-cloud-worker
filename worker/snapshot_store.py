"""Checkpoint persistente e durevole dell'ultimo snapshot MT5 noto.

Lo snapshot viene sostituito atomicamente da un file temporaneo nella stessa directory. File e
directory vengono sincronizzati prima che lo stato in memoria avanzi, cosi' un errore di disco non
puo' far divergere silenziosamente memoria e checkpoint persistente.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
from typing import Any, Dict, Optional


EMPTY_SNAPSHOT: Dict[str, Any] = {"positions": {}, "orders": {}, "deals": {}}
_SNAPSHOT_FIELDS = ("positions", "orders", "deals")
_SECURE_FILE_MODE = 0o600


class SnapshotStoreError(RuntimeError):
    """Il checkpoint non puo' essere letto o reso durevole senza rischio di perdita dati."""


class SnapshotStore:
    def __init__(self, file_path: Optional[str] = None) -> None:
        self.file_path = file_path
        self._snapshot: Dict[str, Any] = self._load_from_disk() if file_path else _empty()

    def get(self) -> Dict[str, Any]:
        return self._snapshot

    def update(self, snapshot: Dict[str, Any]) -> None:
        """Rende durevole ``snapshot`` prima di aggiornare il checkpoint in memoria."""
        if self.file_path:
            self._save_to_disk(snapshot)
        self._snapshot = snapshot

    def _load_from_disk(self) -> Dict[str, Any]:
        assert self.file_path is not None
        if not os.path.lexists(self.file_path):
            return _empty()

        fd = -1
        try:
            path_stat = os.lstat(self.file_path)
            if stat.S_ISLNK(path_stat.st_mode):
                raise SnapshotStoreError("Snapshot persistente non sicuro: i symlink non sono ammessi.")

            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(self.file_path, flags)
            file_stat = os.fstat(fd)
            if not stat.S_ISREG(file_stat.st_mode):
                raise SnapshotStoreError(
                    "Snapshot persistente non valido: il percorso deve essere un file regolare."
                )
            if (path_stat.st_dev, path_stat.st_ino) != (file_stat.st_dev, file_stat.st_ino):
                raise SnapshotStoreError(
                    "Snapshot persistente cambiato durante l'apertura; caricamento rifiutato."
                )

            if stat.S_IMODE(file_stat.st_mode) != _SECURE_FILE_MODE:
                os.fchmod(fd, _SECURE_FILE_MODE)
                os.fsync(fd)

            with os.fdopen(fd, "r", encoding="utf-8") as handle:
                fd = -1
                data = json.load(handle)
        except SnapshotStoreError:
            raise
        except (OSError, json.JSONDecodeError, UnicodeError) as exc:
            raise SnapshotStoreError("Snapshot persistente illeggibile o corrotto.") from exc
        finally:
            if fd >= 0:
                os.close(fd)

        if not isinstance(data, dict) or any(
            not isinstance(data.get(field), dict) for field in _SNAPSHOT_FIELDS
        ):
            raise SnapshotStoreError(
                "Formato snapshot non valido: positions/orders/deals devono essere oggetti."
            )
        return {field: data[field] for field in _SNAPSHOT_FIELDS}

    def _save_to_disk(self, snapshot: Dict[str, Any]) -> None:
        assert self.file_path is not None
        if not isinstance(snapshot, dict) or any(
            not isinstance(snapshot.get(field), dict) for field in _SNAPSHOT_FIELDS
        ):
            raise SnapshotStoreError(
                "Formato snapshot non valido: positions/orders/deals devono essere oggetti."
            )

        directory = os.path.dirname(self.file_path) or "."
        tmp_path: Optional[str] = None
        fd = -1
        try:
            os.makedirs(directory, exist_ok=True)
            self._reject_symlink_target()
            fd, tmp_path = tempfile.mkstemp(
                prefix=f".{os.path.basename(self.file_path)}.", suffix=".tmp", dir=directory
            )
            os.fchmod(fd, _SECURE_FILE_MODE)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                fd = -1
                json.dump(snapshot, handle, sort_keys=True, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())

            self._reject_symlink_target()
            os.replace(tmp_path, self.file_path)
            tmp_path = None
            self._fsync_directory(directory)
        except SnapshotStoreError:
            raise
        except (OSError, TypeError, ValueError) as exc:
            raise SnapshotStoreError("Impossibile rendere durevole lo snapshot persistente.") from exc
        finally:
            if fd >= 0:
                os.close(fd)
            if tmp_path is not None:
                try:
                    os.unlink(tmp_path)
                except FileNotFoundError:
                    pass

    def _reject_symlink_target(self) -> None:
        assert self.file_path is not None
        try:
            mode = os.lstat(self.file_path).st_mode
        except FileNotFoundError:
            return
        if stat.S_ISLNK(mode):
            raise SnapshotStoreError("Snapshot persistente non sicuro: i symlink non sono ammessi.")

    @staticmethod
    def _fsync_directory(directory: str) -> None:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
        directory_fd = os.open(directory, flags)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)


def _empty() -> Dict[str, Any]:
    return {"positions": {}, "orders": {}, "deals": {}}
