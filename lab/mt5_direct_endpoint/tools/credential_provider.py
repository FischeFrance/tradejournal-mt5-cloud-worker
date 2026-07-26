"""In-memory, redaction-safe credential handling for the account/worker lab.

Nothing in this module ever writes a secret to disk, logs, JSON, or a
subprocess command line. No real credential source is implemented here;
``CredentialProvider`` documents where a real implementation would plug in.
"""
from __future__ import annotations

import hmac
import json
from dataclasses import dataclass
from typing import Any, Protocol


_REDACTED = "<redacted:SecretString>"


class CredentialLeakError(ValueError):
    """Raised when a secret value would otherwise leak into serialized output."""


class SecretString:
    """A string that never renders, hashes-for-equality, or pickles as plaintext."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        if not isinstance(value, str):
            raise TypeError("SecretString requires a str value")
        self._value = value

    def reveal(self) -> str:
        """Explicit, greppable escape hatch. Callers must never log/serialize the result."""
        return self._value

    def __repr__(self) -> str:
        return _REDACTED

    def __str__(self) -> str:
        return _REDACTED

    def __format__(self, format_spec: str) -> str:
        return _REDACTED

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SecretString):
            return NotImplemented
        return hmac.compare_digest(self._value.encode("utf-8"), other._value.encode("utf-8"))

    def __ne__(self, other: object) -> bool:
        result = self.__eq__(other)
        return result if result is NotImplemented else not result

    def __hash__(self) -> int:
        return hash(SecretString)

    def __bool__(self) -> bool:
        return bool(self._value)

    def __reduce__(self) -> Any:
        raise CredentialLeakError("SecretString cannot be pickled")

    def __getstate__(self) -> Any:
        raise CredentialLeakError("SecretString cannot be serialized")


@dataclass(frozen=True)
class Credentials:
    login: SecretString
    password: SecretString
    investor_password: SecretString | None = None


class CredentialProvider(Protocol):
    """Integration point for a real credential source.

    A real implementation MUST source credentials into memory only (e.g. from
    an already-decrypted, per-request envelope) and MUST NOT persist them to
    disk, environment variables, logs, or process arguments/command lines. No
    real implementation exists in this lab; only ``FakeCredentialProvider`` is
    provided, and only for tests.
    """

    def get(self, account_id: str) -> Credentials: ...


class FakeCredentialProvider:
    """In-memory, test-only credential provider. Never use on a real login path."""

    def __init__(self) -> None:
        self._values: dict[str, Credentials] = {}

    def register(
        self,
        account_id: str,
        *,
        login: str,
        password: str,
        investor_password: str | None = None,
    ) -> None:
        self._values[account_id] = Credentials(
            login=SecretString(login),
            password=SecretString(password),
            investor_password=SecretString(investor_password) if investor_password is not None else None,
        )

    def get(self, account_id: str) -> Credentials:
        return self._values[account_id]


def _scan_for_secrets(value: Any, path: str = "$") -> None:
    if isinstance(value, SecretString):
        raise CredentialLeakError(f"secret value at {path} must not be serialized")
    if isinstance(value, dict):
        for key, child in value.items():
            _scan_for_secrets(child, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _scan_for_secrets(child, f"{path}[{index}]")


def json_dumps_safe(value: Any, **kwargs: Any) -> str:
    """Serialize to JSON, raising ``CredentialLeakError`` instead of leaking a secret.

    Every new module in this lab must use this instead of raw ``json.dumps``
    for any output that could plausibly contain a ``Credentials``/
    ``SecretString`` value: logs, CLI JSON output, worker state files, evidence.
    """
    _scan_for_secrets(value)
    return json.dumps(value, **kwargs)
