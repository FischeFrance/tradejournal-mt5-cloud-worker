from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from worker.atomic_file import durable_replace

from ..security import safe_child

ALLOWED = frozenset(
    (
        "mt5_investor_password",
        "mt5_login",
        "mt5_server",
        "mt5_endpoint",
        "mt5_broker_label",
        "ingestion_token",
        "agent_token",
        "worker_token",
        "mt5_provisioning_key",
        "bridge_token",
        "grafana_loki_url",
        "grafana_loki_user",
        "grafana_loki_token",
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
        import win32cryptcon

        # CRYPTPROTECT_UI_FORBIDDEN: without it, DPAPI can attempt to show a Windows UI prompt
        # in some certificate/policy configurations. Over a headless SSH session (no attached
        # interactive desktop) that prompt is invisible and unreachable, and the call blocks
        # forever instead of failing. Forbidding UI makes an unexpected DPAPI requirement fail
        # fast and loud, never hang silently.
        #
        # CRYPTPROTECT_LOCAL_MACHINE: secrets here are provisioned by whichever identity runs
        # this tool (interactively, over SSH, as an administrator) but must be readable by the
        # Windows Service, which runs as LocalSystem -- a different Windows identity with its
        # own separate per-user DPAPI key. Per-user protection (the default) ties the blob to
        # the encrypting identity only, so a service running as a different identity can never
        # decrypt it. Machine-scoped protection ties the blob to this machine instead, so any
        # identity on it can decrypt -- NTFS ACLs (restrict_acl) remain the access boundary.
        flags = win32cryptcon.CRYPTPROTECT_UI_FORBIDDEN | win32cryptcon.CRYPTPROTECT_LOCAL_MACHINE
        return win32crypt.CryptProtectData(data, "TradeJournal", None, None, None, flags)

    @staticmethod
    def _crypt_unprotect(data: bytes) -> bytes:
        import win32crypt
        import win32cryptcon

        flags = win32cryptcon.CRYPTPROTECT_UI_FORBIDDEN | win32cryptcon.CRYPTPROTECT_LOCAL_MACHINE
        return win32crypt.CryptUnprotectData(data, None, None, None, flags)[1]

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
            durable_replace(temporary, path)
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
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError("connection secret directory is unsafe")
        shutil.rmtree(directory)
        if directory.exists():
            raise OSError("connection secret cleanup is incomplete")

    @staticmethod
    def restrict_acl(path: Path) -> None:
        import win32api
        import win32con
        import win32security

        # Services running as LocalSystem do not have a SAM-compatible user name that can be
        # resolved with LookupAccountName.  The access token always contains the effective SID,
        # so use it directly and keep the DPAPI blob readable only by the identity that wrote it.
        token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
        try:
            sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
        finally:
            token.Close()  # type: ignore[attr-defined]  # pywin32 stub types the handle as int; PyHANDLE has Close() at runtime
        descriptor = win32security.SECURITY_DESCRIPTOR()
        acl = win32security.ACL()
        acl.AddAccessAllowedAce(win32security.ACL_REVISION, win32con.GENERIC_ALL, sid)
        descriptor.SetSecurityDescriptorDacl(1, acl, 0)
        win32security.SetFileSecurity(
            str(path), win32security.DACL_SECURITY_INFORMATION, descriptor
        )
