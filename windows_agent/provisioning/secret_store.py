from __future__ import annotations

import os
import tempfile
from pathlib import Path

from ..security import safe_child

ALLOWED = frozenset(
    (
        "mt5_investor_password",
        "mt5_login",
        "mt5_server",
        "ingestion_token",
        "agent_token",
        "worker_token",
        "mt5_provisioning_key",
    )
)


class WindowsSecretStore:
    """Current-user DPAPI blobs. Only the same Windows identity can decrypt them."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _path(self, connection_id: str, name: str) -> Path:
        if name not in ALLOWED:
            raise ValueError("unsupported secret name")
        return safe_child(self.root, connection_id) / f"{name}.dpapi"

    @staticmethod
    def _crypt_protect(data: bytes) -> bytes:
        import win32crypt

        return win32crypt.CryptProtectData(data, "TradeJournal", None, None, None, 0)

    @staticmethod
    def _crypt_unprotect(data: bytes) -> bytes:
        import win32crypt

        return win32crypt.CryptUnprotectData(data, None, None, None, 0)[1]

    def write(self, connection_id: str, name: str, value: str) -> Path:
        if not value or "\n" in value or "\r" in value:
            raise ValueError("secret must be non-empty and single-line")
        path = self._path(connection_id, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        blob = self._crypt_protect(value.encode("utf-8"))
        fd, temporary = tempfile.mkstemp(prefix=".secret.", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(blob)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
        self.restrict_acl(path.parent)
        self.restrict_acl(path)
        return path

    def read(self, connection_id: str, name: str) -> str:
        return self._crypt_unprotect(
            self._path(connection_id, name).read_bytes()
        ).decode("utf-8")

    def delete_connection(self, connection_id: str) -> None:
        directory = safe_child(self.root, connection_id)
        if not directory.exists():
            return
        for path in directory.glob("*.dpapi"):
            path.unlink()
        try:
            directory.rmdir()
        except OSError:
            pass

    @staticmethod
    def restrict_acl(path: Path) -> None:
        import win32api
        import win32con
        import win32security

        user = win32api.GetUserNameEx(win32api.NameSamCompatible)
        sid, _, _ = win32security.LookupAccountName(None, user)
        descriptor = win32security.SECURITY_DESCRIPTOR()
        acl = win32security.ACL()
        acl.AddAccessAllowedAce(win32security.ACL_REVISION, win32con.GENERIC_ALL, sid)
        descriptor.SetSecurityDescriptorDacl(1, acl, 0)
        win32security.SetFileSecurity(
            str(path), win32security.DACL_SECURITY_INFORMATION, descriptor
        )
