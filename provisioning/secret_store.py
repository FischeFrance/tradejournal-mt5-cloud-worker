"""Gestione dei soli file secret host-side, mai inclusi nei job o nei log."""

from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path
from typing import Dict

from .validation import validate_uuid

SECRET_NAMES = ("mt5_password", "mt5_bridge_token", "tradejournal_bridge_token")


class SecretStoreError(ValueError):
    pass


class SecretStore:
    def __init__(self, root: Path, *, expected_owner_uid: int = 1000) -> None:
        self.root = Path(root)
        self.expected_owner_uid = expected_owner_uid
        if self.root.exists() and (self.root.is_symlink() or not self.root.is_dir()):
            raise SecretStoreError(f"Directory root secret non valida: {self.root}.")
        created = not self.root.exists()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if created:
            self.root.chmod(0o700)
        root_mode = stat.S_IMODE(self.root.lstat().st_mode)
        if root_mode != 0o700:
            raise SecretStoreError(
                f"Permessi root secret non sicuri ({root_mode:04o}); usare esattamente 0700."
            )

    def connection_dir(self, connection_id: str) -> Path:
        canonical = validate_uuid(connection_id, "connection_id")
        return self.root / canonical

    def ensure_connection_dir(self, connection_id: str) -> Path:
        path = self.connection_dir(connection_id)
        if path.exists() and (path.is_symlink() or not path.is_dir()):
            raise SecretStoreError(f"Directory secret non valida: {path}.")
        created = not path.exists()
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        if created:
            path.chmod(0o700)
        mode = stat.S_IMODE(path.lstat().st_mode)
        if mode != 0o700:
            raise SecretStoreError(
                f"Permessi directory secret non sicuri ({mode:04o}); usare esattamente 0700."
            )
        return path

    def path_for(self, connection_id: str, name: str) -> Path:
        if name not in SECRET_NAMES:
            raise SecretStoreError(f"Nome secret non ammesso: {name}.")
        return self.connection_dir(connection_id) / name

    def validate_file(self, path: Path) -> None:
        try:
            file_stat = path.lstat()
        except FileNotFoundError:
            raise SecretStoreError(f"Secret assente o non regolare: {path}.")
        if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode):
            raise SecretStoreError(f"Secret assente o non regolare: {path}.")
        mode = stat.S_IMODE(file_stat.st_mode)
        if mode not in {0o400, 0o600}:
            raise SecretStoreError(
                f"Permessi secret non sicuri ({mode:04o}); usare esattamente 0400 o 0600."
            )
        if file_stat.st_uid != self.expected_owner_uid:
            raise SecretStoreError(
                "Owner UID secret non valido; configurare TJ_SECRET_OWNER_UID o correggere "
                f"l'owner atteso ({self.expected_owner_uid})."
            )
        if file_stat.st_size <= 0:
            raise SecretStoreError(f"Secret vuoto: {path}.")

    def validate_connection(self, connection_id: str) -> Dict[str, Path]:
        directory = self.ensure_connection_dir(connection_id)
        paths = {name: directory / name for name in SECRET_NAMES}
        for path in paths.values():
            self.validate_file(path)
        return paths

    def write_for_local_test(self, connection_id: str, name: str, value: str) -> Path:
        """Helper esplicito per smoke test locale; non viene mai chiamato dal job processor."""

        if not isinstance(value, str) or not value or "\n" in value or "\r" in value:
            raise SecretStoreError("Il valore secret deve essere non vuoto e su una sola riga.")
        self.ensure_connection_dir(connection_id)
        path = self.path_for(connection_id, name)
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        base_flags = os.O_WRONLY | os.O_CLOEXEC | nofollow
        created = False
        try:
            descriptor = os.open(path, base_flags | os.O_CREAT | os.O_EXCL, 0o600)
            created = True
        except FileExistsError:
            try:
                descriptor = os.open(path, base_flags)
            except OSError as exc:
                raise SecretStoreError(f"Secret path non sicuro o non apribile: {path}.") from exc
        try:
            file_stat = os.fstat(descriptor)
            if not stat.S_ISREG(file_stat.st_mode):
                raise SecretStoreError(f"Secret path non regolare: {path}.")
            if file_stat.st_uid != self.expected_owner_uid:
                raise SecretStoreError(
                    f"Owner UID secret non valido; atteso {self.expected_owner_uid}."
                )
            os.fchmod(descriptor, 0o600)
            os.ftruncate(descriptor, 0)
            os.write(descriptor, value.encode("utf-8"))
            os.fsync(descriptor)
        except Exception:
            if created:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            raise
        finally:
            os.close(descriptor)
        return path

    def delete_connection(self, connection_id: str) -> None:
        path = self.connection_dir(connection_id)
        if not path.exists():
            return
        if path.is_symlink() or not path.is_dir():
            raise SecretStoreError(f"Rifiutata rimozione secret path non regolare: {path}.")
        shutil.rmtree(path)
