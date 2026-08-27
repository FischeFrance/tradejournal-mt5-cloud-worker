"""Shared read-only adapter errors for the Windows MT5 pipeline."""

from __future__ import annotations


class Mt5Error(RuntimeError):
    pass


class IdentityMismatch(Mt5Error):
    pass


class Mt5IpcError(Mt5Error):
    pass


class Mt5ProcessCrashed(Mt5Error):
    pass
