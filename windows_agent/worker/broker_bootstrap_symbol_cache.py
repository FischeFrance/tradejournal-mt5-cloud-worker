from __future__ import annotations

import hashlib
import json
import stat
import time
from pathlib import Path
from typing import Callable

from ..provisioning.secret_store import WindowsSecretStore
from ..state_store import atomic_json


class BrokerBootstrapSymbolCache:
    """Non-authoritative chart hints learned only after a verified Native start.

    One atomic file per server/build/preference avoids shared-map lost updates. The
    five-field record deliberately contains no connection, account or credential data.
    A hint selects the initial chart; Discovery and investor/producer guards still
    determine whether a connection is usable.
    """

    MAX_AGE_SECONDS = 7 * 24 * 60 * 60
    MAX_FUTURE_SECONDS = 5 * 60
    _FIELDS = frozenset((
        "canonical_server", "terminal_build", "preferred_base", "verified_symbol", "verified_at",
    ))

    def __init__(self, root: Path, *, now: Callable[[], float] | None = None) -> None:
        self.root = Path(root).absolute()
        self._now = now or time.time

    @staticmethod
    def _text(value: object, limit: int) -> bool:
        return (
            isinstance(value, str) and 0 < len(value) <= limit
            and value == value.strip() and not any(ord(char) < 32 for char in value)
        )

    @classmethod
    def _key(cls, server: str, build: int, preferred: str) -> tuple[str, int, str] | None:
        if (
            not cls._text(server, 128) or any(char in server for char in "\\/")
            or type(build) is not int or build <= 0 or not cls._text(preferred, 64)
        ):
            return None
        return server.casefold(), build, preferred

    def _path(self, key: tuple[str, int, str]) -> Path:
        digest = hashlib.sha256(json.dumps(key, ensure_ascii=True).encode("ascii")).hexdigest()
        return self.root / f"{digest}.json"

    @staticmethod
    def _regular(path: Path, *, directory: bool = False) -> bool:
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or (
            getattr(metadata, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        ):
            return False
        return stat.S_ISDIR(metadata.st_mode) if directory else stat.S_ISREG(metadata.st_mode)

    def _safe_root(self) -> bool:
        for path in (self.root, *self.root.parents):
            try:
                if not self._regular(path, directory=True):
                    return False
            except FileNotFoundError:
                continue
        return True

    def lookup(self, *, canonical_server: str, terminal_build: int, preferred_base: str) -> str | None:
        key = self._key(canonical_server, terminal_build, preferred_base)
        if key is None:
            return None
        try:
            if not self._safe_root():
                return None
            path = self._path(key)
            if not self._regular(path) or not 0 < path.stat().st_size <= 4096:
                return None
            record = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(record, dict) or set(record) != self._FIELDS:
                return None
            verified_at = record.get("verified_at")
            age = (
                self._now() - verified_at
                if type(verified_at) is int and 0 < verified_at <= 2**53 else None
            )
            if (
                record.get("canonical_server") != key[0]
                or type(record.get("terminal_build")) is not int
                or record.get("terminal_build") != key[1]
                or record.get("preferred_base") != key[2]
                or not self._text(record.get("verified_symbol"), 64)
                or age is None or not -self.MAX_FUTURE_SECONDS <= age <= self.MAX_AGE_SECONDS
            ):
                return None
            return record["verified_symbol"]
        except (OSError, ValueError, UnicodeError, RecursionError):
            return None

    def store_verified(
        self, *, canonical_server: str, terminal_build: int, preferred_base: str, verified_symbol: str,
    ) -> bool:
        """Publish a hint after the caller has completed all account/producer guards."""
        key = self._key(canonical_server, terminal_build, preferred_base)
        if key is None or not self._text(verified_symbol, 64):
            return False
        try:
            if not self._safe_root():
                return False
            self.root.mkdir(parents=True, exist_ok=True)
            if not self._safe_root():
                return False
            path = self._path(key)
            if path.exists() or path.is_symlink():
                if not self._regular(path):
                    return False
            try:
                WindowsSecretStore.restrict_shared_service_acl(self.root)
            except Exception:
                # pywin32 ACL errors are not necessarily OSError subclasses.
                return False
            atomic_json(path, {
                "canonical_server": key[0], "terminal_build": key[1], "preferred_base": key[2],
                "verified_symbol": verified_symbol, "verified_at": int(self._now()),
            })
            try:
                WindowsSecretStore.restrict_shared_service_acl(path)
            except Exception:
                return False
            return True
        except (OSError, ValueError, ImportError):
            # This cache is an optimization. A failed publication cannot fail a healthy start.
            return False
