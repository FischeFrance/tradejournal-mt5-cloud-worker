"""Bounded Linux launcher for the credential-free Wine/Win32 discovery CLI.

The launcher does not start MT5 and never maps a Unix PID to a Windows PID.  It
expects an already-running, credential-free terminal in its first-start wizard,
then invokes 64-bit Windows Python inside the same ``WINEPREFIX`` and ``DISPLAY``.
The Windows helper enumerates and verifies Win32 process identities itself.

This module is intentionally not wired into the current Docker entrypoint.  That
entrypoint reads the account password before starting MT5 and its golden-template
contract explicitly excludes Windows Python; both lifecycle contracts must be
changed and smoke-tested before unattended discovery can safely become a startup
phase.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any, Callable, Mapping, Sequence

try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - module is imported defensively on Windows
    _fcntl = None

from ..state_store import atomic_json
from .mt5_broker_discovery import (
    BrokerDiscoveryError,
    BrokerDiscoveryResult,
    normalize_server_name,
)


__all__ = [
    "LinuxWineBrokerDiscoveryLauncher",
    "LinuxWineDiscoveryError",
]


_SAFE_MESSAGES = {
    "invalid_request": "Wine broker discovery request is invalid",
    "runtime_unavailable": "Wine broker discovery runtime is unavailable",
    "busy": "Wine broker discovery is already running for this prefix",
    "helper_failed": "Wine broker discovery helper failed",
    "terminal_not_found": "eligible terminal window was not found",
    "terminal_ambiguous": "multiple eligible terminal windows were found",
    "ui_unknown": "terminal user interface is not recognized",
    "no_exact_match": "expected server was not found",
    "ambiguous_exact_match": "expected server result is ambiguous",
    "identity_mismatch": "broker discovery identity did not match",
    "timeout": "Wine broker discovery timed out",
    "cleanup_failed": "Wine broker discovery cleanup failed",
    "internal_error": "Wine broker discovery failed",
}
_HELPER_ERROR_CODES = {
    "invalid_request",
    "terminal_not_found",
    "terminal_ambiguous",
    "ui_unknown",
    "no_exact_match",
    "ambiguous_exact_match",
    "timeout",
    "internal_error",
}
_HELPER_EXIT_CODES = {
    "invalid_request": 2,
    "no_exact_match": 3,
    "ambiguous_exact_match": 4,
    "timeout": 5,
    "terminal_not_found": 6,
    "terminal_ambiguous": 6,
    "ui_unknown": 6,
    "internal_error": 6,
}
_SENSITIVE_ENVIRONMENT_MARKERS = (
    "account",
    "authorization",
    "credential",
    "login",
    "password",
    "secret",
    "token",
)
_WINDOWS_MODULE = "windows_agent.worker.wine_mt5_broker_discovery"
_MAX_RESULT_BYTES = 4096


class LinuxWineDiscoveryError(RuntimeError):
    """A Linux orchestration error whose message is safe to log."""

    def __init__(self, code: str) -> None:
        if code not in _SAFE_MESSAGES:
            code = "internal_error"
        self.code = code
        super().__init__(_SAFE_MESSAGES[code])


def _validated_text(value: str, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise LinuxWineDiscoveryError("invalid_request")
    if any(unicodedata.category(character).startswith("C") for character in value):
        raise LinuxWineDiscoveryError("invalid_request")
    normalized = " ".join(unicodedata.normalize("NFKC", value).split())
    if not normalized or len(normalized) > maximum:
        raise LinuxWineDiscoveryError("invalid_request")
    return normalized


def _validated_windows_c_path(value: str, *, filename: str) -> str:
    value = _validated_text(value, maximum=512)
    path = PureWindowsPath(value)
    if (
        not path.is_absolute()
        or path.drive.casefold() != "c:"
        or path.name.casefold() != filename.casefold()
        or any(part in {".", ".."} for part in path.parts)
    ):
        raise LinuxWineDiscoveryError("invalid_request")
    return str(path)


def _validated_timeout(value: float) -> float:
    if isinstance(value, bool):
        raise LinuxWineDiscoveryError("invalid_request")
    try:
        timeout = float(value)
    except (TypeError, ValueError):
        raise LinuxWineDiscoveryError("invalid_request") from None
    if not 1.0 <= timeout <= 300.0:
        raise LinuxWineDiscoveryError("invalid_request")
    return timeout


def _validated_queries(values: Sequence[str]) -> tuple[str, ...]:
    if not isinstance(values, (tuple, list)):
        raise LinuxWineDiscoveryError("invalid_request")
    queries: list[str] = []
    seen: set[str] = set()
    for value in values:
        query = _validated_text(value, maximum=256)
        key = normalize_server_name(query)
        if key not in seen:
            seen.add(key)
            queries.append(query)
    if not queries or len(queries) > 16:
        raise LinuxWineDiscoveryError("invalid_request")
    return tuple(queries)


def _validated_directory(path: Path, *, private: bool = False) -> Path:
    raw = Path(path)
    if not raw.is_absolute() or raw.is_symlink() or not raw.is_dir():
        raise LinuxWineDiscoveryError("runtime_unavailable")
    resolved = raw.resolve()
    if private:
        info = resolved.stat()
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise LinuxWineDiscoveryError("runtime_unavailable")
    return resolved


def _prefix_file(prefix: Path, windows_path: str) -> Path:
    parsed = PureWindowsPath(windows_path)
    candidate = prefix / "drive_c"
    for part in parsed.parts[1:]:
        candidate = candidate / part
        if candidate.is_symlink():
            raise LinuxWineDiscoveryError("runtime_unavailable")
    if not candidate.is_file():
        raise LinuxWineDiscoveryError("runtime_unavailable")
    drive_c = (prefix / "drive_c").resolve()
    try:
        candidate.resolve().relative_to(drive_c)
    except ValueError:
        raise LinuxWineDiscoveryError("runtime_unavailable") from None
    return candidate


def _wine_c_path_for_prefix_path(prefix: Path, path: Path) -> str:
    drive_c = prefix / "drive_c"
    if drive_c.is_symlink() or not drive_c.is_dir():
        raise LinuxWineDiscoveryError("runtime_unavailable")
    drive_c = drive_c.resolve()
    resolved = path.resolve(strict=False)
    try:
        relative = resolved.relative_to(drive_c)
    except ValueError:
        raise LinuxWineDiscoveryError("runtime_unavailable") from None
    cursor = drive_c
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise LinuxWineDiscoveryError("runtime_unavailable")
    return str(PureWindowsPath("C:/", *relative.parts))


@dataclass(frozen=True)
class _ValidatedRuntime:
    wine_binary: Path
    windows_python: str
    wineprefix: Path
    display: str
    repository_root: Path
    exchange_root: Path


class LinuxWineBrokerDiscoveryLauncher:
    """Execute the Windows helper with private files and a sanitized environment."""

    def __init__(
        self,
        *,
        wine_binary: Path,
        windows_python: str,
        wineprefix: Path,
        display: str,
        repository_root: Path,
        exchange_root: Path,
        source_environment: Mapping[str, str] | None = None,
        popen_factory: Callable[..., Any] = subprocess.Popen,
        kill_process_group: Callable[[int, int], None] = os.killpg,
    ) -> None:
        self._runtime = self._validate_runtime(
            wine_binary=wine_binary,
            windows_python=windows_python,
            wineprefix=wineprefix,
            display=display,
            repository_root=repository_root,
            exchange_root=exchange_root,
        )
        source = dict(
            os.environ if source_environment is None else source_environment
        )
        if any(
            value
            and any(marker in name.casefold() for marker in _SENSITIVE_ENVIRONMENT_MARKERS)
            for name, value in source.items()
        ):
            raise LinuxWineDiscoveryError("runtime_unavailable")
        secret_root = Path("/run/secrets")
        if secret_root.is_dir():
            try:
                if next(secret_root.iterdir(), None) is not None:
                    raise LinuxWineDiscoveryError("runtime_unavailable")
            except OSError:
                raise LinuxWineDiscoveryError("runtime_unavailable") from None
        self._popen = popen_factory
        self._kill_process_group = kill_process_group

    @staticmethod
    def _validate_runtime(
        *,
        wine_binary: Path,
        windows_python: str,
        wineprefix: Path,
        display: str,
        repository_root: Path,
        exchange_root: Path,
    ) -> _ValidatedRuntime:
        raw_wine = Path(wine_binary)
        if not raw_wine.is_absolute() or not raw_wine.exists():
            raise LinuxWineDiscoveryError("runtime_unavailable")
        resolved_wine = raw_wine.resolve()
        if not resolved_wine.is_file() or not os.access(resolved_wine, os.X_OK):
            raise LinuxWineDiscoveryError("runtime_unavailable")

        prefix = _validated_directory(wineprefix, private=True)
        drive_c = prefix / "drive_c"
        if drive_c.is_symlink() or not drive_c.is_dir():
            raise LinuxWineDiscoveryError("runtime_unavailable")
        dosdevices = prefix / "dosdevices"
        if dosdevices.is_symlink() or not dosdevices.is_dir():
            raise LinuxWineDiscoveryError("runtime_unavailable")
        c_drive = dosdevices / "c:"
        if not c_drive.is_symlink() or c_drive.resolve() != drive_c.resolve():
            raise LinuxWineDiscoveryError("runtime_unavailable")
        python_path = _validated_windows_c_path(
            windows_python, filename="python.exe"
        )
        _prefix_file(prefix, python_path)

        # Z: -> / would expose /run/secrets and /proc to the helper and MT5.
        z_drive = dosdevices / "z:"
        if z_drive.exists() or z_drive.is_symlink():
            raise LinuxWineDiscoveryError("runtime_unavailable")

        display = _validated_text(display, maximum=32)
        if not display.startswith(":") or not display[1:].isdigit():
            raise LinuxWineDiscoveryError("invalid_request")

        repository = _validated_directory(repository_root)
        helper = repository / "windows_agent" / "worker" / (
            "wine_mt5_broker_discovery.py"
        )
        if helper.is_symlink() or not helper.is_file():
            raise LinuxWineDiscoveryError("runtime_unavailable")
        exchange = _validated_directory(exchange_root, private=True)
        _wine_c_path_for_prefix_path(prefix, repository)
        _wine_c_path_for_prefix_path(prefix, exchange)
        return _ValidatedRuntime(
            wine_binary=resolved_wine,
            windows_python=python_path,
            wineprefix=prefix,
            display=display,
            repository_root=repository,
            exchange_root=exchange,
        )

    def _child_environment(self) -> dict[str, str]:
        return {
            "DISPLAY": self._runtime.display,
            "HOME": "/home/runtime",
            "LANG": "C.UTF-8",
            "LOGNAME": "runtime",
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUTF8": "1",
            "USER": "runtime",
            "WINEARCH": "win64",
            "WINEDEBUG": "-all",
            "WINEPREFIX": str(self._runtime.wineprefix),
        }

    def _acquire_prefix_lock(self) -> int:
        if _fcntl is None:
            raise LinuxWineDiscoveryError("runtime_unavailable")
        path = self._runtime.wineprefix / ".broker-discovery.lock"
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags, 0o600)
        except OSError:
            raise LinuxWineDiscoveryError("runtime_unavailable") from None
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) & 0o077
            ):
                raise LinuxWineDiscoveryError("runtime_unavailable")
            try:
                _fcntl.flock(
                    descriptor, _fcntl.LOCK_EX | _fcntl.LOCK_NB
                )
            except BlockingIOError:
                raise LinuxWineDiscoveryError("busy") from None
            except OSError:
                raise LinuxWineDiscoveryError("runtime_unavailable") from None
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def _terminate_helper(self, process: Any) -> None:
        if process.poll() is not None:
            return
        try:
            self._kill_process_group(process.pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass
        try:
            process.wait(timeout=2.0)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            self._kill_process_group(process.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            raise LinuxWineDiscoveryError("cleanup_failed") from None

    def _run_helper(
        self, request: Path, result: Path, timeout_seconds: float
    ) -> int:
        command = [
            str(self._runtime.wine_binary),
            self._runtime.windows_python,
            "-m",
            _WINDOWS_MODULE,
            "--request",
            _wine_c_path_for_prefix_path(self._runtime.wineprefix, request),
            "--result",
            _wine_c_path_for_prefix_path(self._runtime.wineprefix, result),
        ]
        try:
            process = self._popen(
                command,
                cwd=self._runtime.repository_root,
                env=self._child_environment(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except (OSError, ValueError):
            raise LinuxWineDiscoveryError("helper_failed") from None
        try:
            return int(process.wait(timeout=timeout_seconds + 10.0))
        except subprocess.TimeoutExpired:
            self._terminate_helper(process)
            raise LinuxWineDiscoveryError("timeout") from None
        except BaseException:
            self._terminate_helper(process)
            raise

    @staticmethod
    def _read_result(
        path: Path, *, expected_server: str, return_code: int
    ) -> BrokerDiscoveryResult:
        descriptor = -1
        try:
            descriptor = os.open(
                path,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            )
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or not 0 < info.st_size <= _MAX_RESULT_BYTES
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) & 0o077
            ):
                raise LinuxWineDiscoveryError("helper_failed")
            encoded = os.read(descriptor, _MAX_RESULT_BYTES + 1)
            if len(encoded) != info.st_size:
                raise LinuxWineDiscoveryError("helper_failed")
            payload = json.loads(encoded.decode("utf-8"))
        except LinuxWineDiscoveryError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise LinuxWineDiscoveryError("helper_failed") from None
        finally:
            if descriptor >= 0:
                os.close(descriptor)

        if not isinstance(payload, dict):
            raise LinuxWineDiscoveryError("helper_failed")
        if payload.get("ok") is True:
            if (
                set(payload) != {"ok", "code", "server", "query_index"}
                or payload.get("code") != "ok"
                or return_code != 0
                or not isinstance(payload.get("server"), str)
                or isinstance(payload.get("query_index"), bool)
                or not isinstance(payload.get("query_index"), int)
                or not 0 <= payload["query_index"] < 16
            ):
                raise LinuxWineDiscoveryError("helper_failed")
            try:
                matches = normalize_server_name(
                    payload["server"]
                ) == normalize_server_name(expected_server)
            except BrokerDiscoveryError:
                raise LinuxWineDiscoveryError("helper_failed") from None
            if not matches:
                raise LinuxWineDiscoveryError("identity_mismatch")
            return BrokerDiscoveryResult(
                server=expected_server, query_index=payload["query_index"]
            )

        if set(payload) != {"ok", "code", "message"} or payload.get("ok") is not False:
            raise LinuxWineDiscoveryError("helper_failed")
        code = payload.get("code")
        if not isinstance(code, str) or code not in _HELPER_ERROR_CODES:
            raise LinuxWineDiscoveryError("helper_failed")
        if return_code != _HELPER_EXIT_CODES[code]:
            raise LinuxWineDiscoveryError("helper_failed")
        raise LinuxWineDiscoveryError(code)

    def discover(
        self,
        *,
        terminal_path: str,
        expected_server: str,
        queries: Sequence[str],
        timeout_seconds: float = 60.0,
    ) -> BrokerDiscoveryResult:
        lock_descriptor = self._acquire_prefix_lock()
        try:
            terminal_path = _validated_windows_c_path(
                terminal_path, filename="terminal64.exe"
            )
            _prefix_file(self._runtime.wineprefix, terminal_path)
            expected_server = _validated_text(expected_server, maximum=128)
            queries = _validated_queries(queries)
            timeout_seconds = _validated_timeout(timeout_seconds)

            exchange = Path(
                tempfile.mkdtemp(
                    prefix="mt5-broker-discovery-", dir=self._runtime.exchange_root
                )
            )
            exchange.chmod(0o700)
            request = exchange / "request.json"
            result = exchange / "result.json"
            try:
                atomic_json(
                    request,
                    {
                        "terminal_path": terminal_path,
                        "expected_server": expected_server,
                        "queries": list(queries),
                        "timeout_seconds": timeout_seconds,
                    },
                )
                request.chmod(0o600)
                return_code = self._run_helper(request, result, timeout_seconds)
                return self._read_result(
                    result,
                    expected_server=expected_server,
                    return_code=return_code,
                )
            finally:
                active_error = sys.exc_info()[0] is not None
                try:
                    shutil.rmtree(exchange)
                except OSError:
                    if not active_error:
                        raise LinuxWineDiscoveryError("cleanup_failed") from None
        finally:
            os.close(lock_descriptor)
