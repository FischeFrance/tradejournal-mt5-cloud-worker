"""Credential-free MT5 broker discovery through native Win32 messages.

This adapter targets a Windows Python process running inside the same Wine
prefix/session as ``terminal64.exe``.  It deliberately avoids UI Automation,
pywinauto, screen coordinates, OCR, and custom MetaQuotes network traffic.

The control identifiers in this module are a versioned compatibility boundary.
An unknown window hierarchy, duplicate control, changed identifier, or canonical
server mismatch fails closed through :class:`BrokerDiscoveryError`.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import time
import unicodedata
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence

from ..state_store import atomic_json
from .mt5_broker_discovery import (
    BrokerDiscoveryError,
    BrokerDiscoveryRequest,
    BrokerDiscoveryUiSession,
    WindowsMt5BrokerDiscovery,
    _run_payload,
    normalize_server_name,
)


__all__ = [
    "WineWin32Backend",
    "WineWin32Mt5BrokerDiscoveryAdapter",
    "main",
]


_WM_SETTEXT = 0x000C
_WM_GETTEXT = 0x000D
_WM_GETTEXTLENGTH = 0x000E
_BM_CLICK = 0x00F5
_LVM_FIRST = 0x1000
_LVM_GETITEMCOUNT = _LVM_FIRST + 4
_LVM_SETITEMSTATE = _LVM_FIRST + 43
_LVM_ENSUREVISIBLE = _LVM_FIRST + 19
_LVIS_FOCUSED = 0x0001
_LVIS_SELECTED = 0x0002
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_PROCESS_VM_OPERATION = 0x0008
_PROCESS_VM_WRITE = 0x0020
_MEM_COMMIT = 0x1000
_MEM_RESERVE = 0x2000
_MEM_RELEASE = 0x8000
_PAGE_READWRITE = 0x04
_SMTO_BLOCK = 0x0001
_SMTO_ABORTIFHUNG = 0x0002
_SMTO_ERRORONEXIT = 0x0020
_ERROR_TIMEOUT = 1460
_MAX_RESULT_ROWS = 512
_MAX_CONTROL_TEXT = 256


@dataclass(frozen=True)
class _ControlSpec:
    control_id: int
    class_name: str


_SEARCH_EDIT = _ControlSpec(10814, "Edit")
_FIND_BUTTON = _ControlSpec(10815, "Button")
_RESULT_LIST = _ControlSpec(10729, "SysListView32")
_BACK_BUTTON = _ControlSpec(12323, "Button")
_NEXT_BUTTON = _ControlSpec(12324, "Button")
_CANCEL_BUTTON = _ControlSpec(2, "Button")
_SERVER_COMBO = _ControlSpec(10139, "ComboBox")
_DISCOVERY_SIGNATURE = (
    _SEARCH_EDIT,
    _FIND_BUTTON,
    _RESULT_LIST,
    _NEXT_BUTTON,
    _CANCEL_BUTTON,
)


class WineWin32Backend(Protocol):
    """Small injectable boundary around the Win32 calls used by the adapter."""

    def process_image_path(self, pid: int) -> str | None: ...

    def candidate_window_pids(self, terminal_path: Path) -> Sequence[int]: ...

    def window_pid(self, hwnd: int) -> int: ...

    def top_level_windows(self, pid: int) -> Sequence[int]: ...

    def descendant_windows(self, hwnd: int) -> Sequence[int]: ...

    def is_window(self, hwnd: int) -> bool: ...

    def is_visible(self, hwnd: int) -> bool: ...

    def is_enabled(self, hwnd: int) -> bool: ...

    def class_name(self, hwnd: int) -> str: ...

    def control_id(self, hwnd: int) -> int: ...

    def set_text(self, hwnd: int, value: str, timeout_ms: int) -> None: ...

    def click(self, hwnd: int, timeout_ms: int) -> None: ...

    def get_text(self, hwnd: int, maximum: int, timeout_ms: int) -> str: ...

    def list_item_count(self, hwnd: int, timeout_ms: int) -> int: ...

    def select_list_item(self, hwnd: int, index: int, timeout_ms: int) -> None: ...


class _LVITEMW(ctypes.Structure):
    """64/32-bit-safe LVITEMW layout used by LVM_SETITEMSTATE."""

    _fields_ = [
        ("mask", wintypes.UINT),
        ("iItem", ctypes.c_int),
        ("iSubItem", ctypes.c_int),
        ("state", wintypes.UINT),
        ("stateMask", wintypes.UINT),
        ("pszText", ctypes.c_void_p),
        ("cchTextMax", ctypes.c_int),
        ("iImage", ctypes.c_int),
        ("lParam", ctypes.c_ssize_t),
        ("iIndent", ctypes.c_int),
        ("iGroupId", ctypes.c_int),
        ("cColumns", wintypes.UINT),
        ("puColumns", ctypes.c_void_p),
        ("piColFmt", ctypes.c_void_p),
        ("iGroup", ctypes.c_int),
    ]


class _CtypesWineWin32Backend:
    """Direct ctypes implementation; imported safely on non-Windows hosts."""

    def __init__(self) -> None:
        win_dll = getattr(ctypes, "WinDLL", None)
        callback_factory = getattr(ctypes, "WINFUNCTYPE", None)
        if (
            win_dll is None
            or callback_factory is None
            or os.name != "nt"
            or ctypes.sizeof(ctypes.c_void_p) != 8
        ):
            raise BrokerDiscoveryError("ui_unknown")
        try:
            self._kernel32 = win_dll("kernel32", use_last_error=True)
            self._user32 = win_dll("user32", use_last_error=True)
        except (OSError, AttributeError):
            raise BrokerDiscoveryError("ui_unknown") from None
        self._enum_callback = callback_factory(
            wintypes.BOOL, wintypes.HWND, ctypes.c_ssize_t
        )
        self._configure_signatures()

    def _configure_signatures(self) -> None:
        self._kernel32.OpenProcess.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        self._kernel32.OpenProcess.restype = wintypes.HANDLE
        self._kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self._kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32.CloseHandle.restype = wintypes.BOOL
        self._kernel32.VirtualAllocEx.argtypes = [
            wintypes.HANDLE,
            ctypes.c_void_p,
            ctypes.c_size_t,
            wintypes.DWORD,
            wintypes.DWORD,
        ]
        self._kernel32.VirtualAllocEx.restype = ctypes.c_void_p
        self._kernel32.VirtualFreeEx.argtypes = [
            wintypes.HANDLE,
            ctypes.c_void_p,
            ctypes.c_size_t,
            wintypes.DWORD,
        ]
        self._kernel32.VirtualFreeEx.restype = wintypes.BOOL
        self._kernel32.WriteProcessMemory.argtypes = [
            wintypes.HANDLE,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        self._kernel32.WriteProcessMemory.restype = wintypes.BOOL

        self._user32.EnumWindows.argtypes = [self._enum_callback, ctypes.c_ssize_t]
        self._user32.EnumWindows.restype = wintypes.BOOL
        self._user32.EnumChildWindows.argtypes = [
            wintypes.HWND,
            self._enum_callback,
            ctypes.c_ssize_t,
        ]
        self._user32.EnumChildWindows.restype = wintypes.BOOL
        self._user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self._user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        self._user32.IsWindow.argtypes = [wintypes.HWND]
        self._user32.IsWindow.restype = wintypes.BOOL
        self._user32.IsWindowVisible.argtypes = [wintypes.HWND]
        self._user32.IsWindowVisible.restype = wintypes.BOOL
        self._user32.IsWindowEnabled.argtypes = [wintypes.HWND]
        self._user32.IsWindowEnabled.restype = wintypes.BOOL
        self._user32.GetDlgCtrlID.argtypes = [wintypes.HWND]
        self._user32.GetDlgCtrlID.restype = ctypes.c_int
        self._user32.GetClassNameW.argtypes = [
            wintypes.HWND,
            wintypes.LPWSTR,
            ctypes.c_int,
        ]
        self._user32.GetClassNameW.restype = ctypes.c_int
        self._user32.SendMessageTimeoutW.argtypes = [
            wintypes.HWND,
            wintypes.UINT,
            ctypes.c_size_t,
            ctypes.c_ssize_t,
            wintypes.UINT,
            wintypes.UINT,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        self._user32.SendMessageTimeoutW.restype = ctypes.c_size_t

    @staticmethod
    def _handle_value(handle: object) -> int:
        value = ctypes.cast(handle, ctypes.c_void_p).value
        return int(value or 0)

    def _enum(self, parent: int | None = None) -> tuple[int, ...]:
        handles: list[int] = []

        @self._enum_callback
        def callback(hwnd: wintypes.HWND, _parameter: int) -> bool:
            handles.append(self._handle_value(hwnd))
            return True

        ctypes.set_last_error(0)
        if parent is None:
            ok = self._user32.EnumWindows(callback, 0)
        else:
            ok = self._user32.EnumChildWindows(parent, callback, 0)
        if not ok:
            raise OSError(ctypes.get_last_error(), "window enumeration failed")
        # Be defensive against Wine/user32 shims that surface the same HWND more
        # than once. Duplicate callbacks must never manufacture an ambiguity.
        return tuple(dict.fromkeys(handles))

    def window_pid(self, hwnd: int) -> int:
        pid = wintypes.DWORD()
        self._user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value)

    def _open_process(self, pid: int, access: int) -> object:
        handle = self._kernel32.OpenProcess(access, False, pid)
        if not handle:
            raise OSError(ctypes.get_last_error(), "process open failed")
        return handle

    def _send_message(
        self,
        hwnd: int,
        message: int,
        *,
        wparam: int = 0,
        lparam: int = 0,
        timeout_ms: int,
    ) -> int:
        timeout_ms = max(1, min(int(timeout_ms), 300_000))
        if not self._user32.IsWindow(hwnd):
            raise OSError("control window is no longer available")
        result = ctypes.c_size_t()
        ctypes.set_last_error(0)
        ok = self._user32.SendMessageTimeoutW(
            hwnd,
            message,
            ctypes.c_size_t(wparam).value,
            ctypes.c_ssize_t(lparam).value,
            _SMTO_BLOCK | _SMTO_ABORTIFHUNG | _SMTO_ERRORONEXIT,
            timeout_ms,
            ctypes.byref(result),
        )
        if not ok:
            if ctypes.get_last_error() not in (0, _ERROR_TIMEOUT):
                raise OSError(ctypes.get_last_error(), "Win32 control message failed")
            raise TimeoutError("Win32 control message timed out")
        return int(result.value)

    def process_image_path(self, pid: int) -> str | None:
        try:
            process = self._open_process(pid, _PROCESS_QUERY_LIMITED_INFORMATION)
        except OSError:
            return None
        try:
            capacity = wintypes.DWORD(32_768)
            buffer = ctypes.create_unicode_buffer(capacity.value)
            if not self._kernel32.QueryFullProcessImageNameW(
                process, 0, buffer, ctypes.byref(capacity)
            ):
                return None
            return buffer.value
        finally:
            self._kernel32.CloseHandle(process)

    def candidate_window_pids(self, terminal_path: Path) -> Sequence[int]:
        """Resolve Win32 PIDs inside Wine without trusting Unix process IDs."""

        expected = _normalized_path(terminal_path)
        candidates: list[int] = []
        for hwnd in self._enum():
            if not self.is_window(hwnd) or not self.is_visible(hwnd):
                continue
            pid = self.window_pid(hwnd)
            if pid <= 0 or pid in candidates:
                continue
            actual = self.process_image_path(pid)
            if actual is not None and _normalized_path(actual) == expected:
                candidates.append(pid)
        return tuple(candidates)

    def top_level_windows(self, pid: int) -> Sequence[int]:
        return tuple(hwnd for hwnd in self._enum() if self.window_pid(hwnd) == pid)

    def descendant_windows(self, hwnd: int) -> Sequence[int]:
        return self._enum(hwnd)

    def is_window(self, hwnd: int) -> bool:
        return bool(self._user32.IsWindow(hwnd))

    def is_visible(self, hwnd: int) -> bool:
        return bool(self._user32.IsWindowVisible(hwnd))

    def is_enabled(self, hwnd: int) -> bool:
        return bool(self._user32.IsWindowEnabled(hwnd))

    def class_name(self, hwnd: int) -> str:
        buffer = ctypes.create_unicode_buffer(256)
        copied = self._user32.GetClassNameW(hwnd, buffer, len(buffer))
        if copied <= 0:
            raise OSError(ctypes.get_last_error(), "window class read failed")
        return buffer.value

    def control_id(self, hwnd: int) -> int:
        return int(self._user32.GetDlgCtrlID(hwnd))

    def set_text(self, hwnd: int, value: str, timeout_ms: int) -> None:
        buffer = ctypes.create_unicode_buffer(value)
        result = self._send_message(
            hwnd,
            _WM_SETTEXT,
            lparam=ctypes.addressof(buffer),
            timeout_ms=timeout_ms,
        )
        if result == 0:
            raise OSError("WM_SETTEXT failed")

    def click(self, hwnd: int, timeout_ms: int) -> None:
        self._send_message(hwnd, _BM_CLICK, timeout_ms=timeout_ms)

    def get_text(self, hwnd: int, maximum: int, timeout_ms: int) -> str:
        length = self._send_message(
            hwnd, _WM_GETTEXTLENGTH, timeout_ms=timeout_ms
        )
        if length < 0 or length > maximum:
            raise OSError("control text has invalid length")
        buffer = ctypes.create_unicode_buffer(maximum + 1)
        self._send_message(
            hwnd,
            _WM_GETTEXT,
            wparam=maximum + 1,
            lparam=ctypes.addressof(buffer),
            timeout_ms=timeout_ms,
        )
        return buffer.value

    def list_item_count(self, hwnd: int, timeout_ms: int) -> int:
        return self._send_message(
            hwnd, _LVM_GETITEMCOUNT, timeout_ms=timeout_ms
        )

    def _write_remote_structure(self, pid: int, value: ctypes.Structure) -> tuple:
        process = self._open_process(
            pid, _PROCESS_VM_OPERATION | _PROCESS_VM_WRITE
        )
        size = ctypes.sizeof(value)
        remote = self._kernel32.VirtualAllocEx(
            process,
            None,
            size,
            _MEM_COMMIT | _MEM_RESERVE,
            _PAGE_READWRITE,
        )
        if not remote:
            self._kernel32.CloseHandle(process)
            raise OSError(ctypes.get_last_error(), "remote allocation failed")
        written = ctypes.c_size_t()
        if not self._kernel32.WriteProcessMemory(
            process,
            remote,
            ctypes.byref(value),
            size,
            ctypes.byref(written),
        ) or written.value != size:
            self._kernel32.VirtualFreeEx(process, remote, 0, _MEM_RELEASE)
            self._kernel32.CloseHandle(process)
            raise OSError(ctypes.get_last_error(), "remote write failed")
        return process, remote

    def _set_list_state(
        self, hwnd: int, index: int, state: int, timeout_ms: int
    ) -> None:
        item = _LVITEMW()
        item.stateMask = _LVIS_SELECTED | _LVIS_FOCUSED
        item.state = state
        pid = self.window_pid(hwnd)
        process, remote = self._write_remote_structure(pid, item)
        release_remote = True
        try:
            result = self._send_message(
                hwnd,
                _LVM_SETITEMSTATE,
                wparam=ctypes.c_size_t(index).value,
                lparam=self._handle_value(remote),
                timeout_ms=timeout_ms,
            )
            if result == 0:
                raise OSError("LVM_SETITEMSTATE failed")
        except TimeoutError:
            # LVM_SETITEMSTATE is a process-private message carrying a pointer.
            # SendMessageTimeout may return while a hung receiver still owns that
            # pointer, so freeing it here could make MT5 dereference released
            # memory if its UI thread resumes. Keep the tiny allocation alive;
            # the failed discovery's terminal teardown will reclaim it.
            release_remote = False
            raise
        finally:
            if release_remote:
                self._kernel32.VirtualFreeEx(process, remote, 0, _MEM_RELEASE)
            self._kernel32.CloseHandle(process)

    def select_list_item(self, hwnd: int, index: int, timeout_ms: int) -> None:
        if index < 0:
            raise OSError("invalid list item index")
        self._set_list_state(hwnd, -1, 0, timeout_ms)
        self._set_list_state(
            hwnd, index, _LVIS_SELECTED | _LVIS_FOCUSED, timeout_ms
        )
        self._send_message(
            hwnd,
            _LVM_ENSUREVISIBLE,
            wparam=index,
            lparam=0,
            timeout_ms=timeout_ms,
        )


def _normalized_path(value: str | Path) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(str(value))))


def _matching_controls(
    backend: WineWin32Backend,
    parent: int,
    spec: _ControlSpec,
    *,
    expected_pid: int,
) -> tuple[int, ...]:
    if backend.window_pid(parent) != expected_pid:
        raise BrokerDiscoveryError("ui_unknown")
    matches = []
    for hwnd in backend.descendant_windows(parent):
        if backend.window_pid(hwnd) != expected_pid:
            raise BrokerDiscoveryError("ui_unknown")
        if not backend.is_window(hwnd) or not backend.is_visible(hwnd):
            continue
        if backend.control_id(hwnd) != spec.control_id:
            continue
        if backend.class_name(hwnd).casefold() != spec.class_name.casefold():
            continue
        matches.append(hwnd)
    return tuple(matches)


def _has_discovery_signature(
    backend: WineWin32Backend, parent: int, *, expected_pid: int
) -> bool:
    return all(
        len(
            _matching_controls(
                backend, parent, spec, expected_pid=expected_pid
            )
        )
        == 1
        for spec in _DISCOVERY_SIGNATURE
    )


def _canonical_server_label(value: str) -> str:
    try:
        normalized = normalize_server_name(value)
    except BrokerDiscoveryError:
        raise BrokerDiscoveryError("ui_unknown") from None
    canonical = " ".join(unicodedata.normalize("NFKC", value).split())
    if not normalized or not canonical or len(canonical) > _MAX_CONTROL_TEXT:
        raise BrokerDiscoveryError("ui_unknown")
    return canonical


class WineWin32Mt5BrokerDiscoveryAdapter:
    """Bind broker discovery to one exact Wine/Win32 terminal process."""

    def __init__(
        self,
        backend: WineWin32Backend | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._backend = backend
        self._clock = clock
        self._sleep = sleeper

    def open_session(
        self,
        *,
        terminal_path: Path,
        candidate_pids: Sequence[int],
        timeout_seconds: float,
    ) -> BrokerDiscoveryUiSession:
        backend = self._backend or _CtypesWineWin32Backend()
        expected_path = _normalized_path(terminal_path)
        deadline = self._clock() + timeout_seconds
        exact_process_seen = False
        terminal_window_seen = False
        ambiguous = False

        while True:
            eligible: list[tuple[int, int]] = []
            eligible_seen: set[tuple[int, int]] = set()
            for pid in candidate_pids:
                try:
                    actual_path = backend.process_image_path(pid)
                    if actual_path is None or _normalized_path(actual_path) != expected_path:
                        continue
                    exact_process_seen = True
                    for hwnd in backend.top_level_windows(pid):
                        if not backend.is_window(hwnd) or not backend.is_visible(hwnd):
                            continue
                        if backend.window_pid(hwnd) != pid:
                            raise BrokerDiscoveryError("ui_unknown")
                        terminal_window_seen = True
                        if _has_discovery_signature(
                            backend, hwnd, expected_pid=pid
                        ):
                            identity = (pid, hwnd)
                            if identity not in eligible_seen:
                                eligible_seen.add(identity)
                                eligible.append(identity)
                except BrokerDiscoveryError:
                    raise
                except Exception:
                    continue

            if len(eligible) == 1:
                pid, dialog = eligible[0]
                return _WineWin32Session(
                    backend=backend,
                    pid=pid,
                    dialog=dialog,
                    expected_path=expected_path,
                    deadline=deadline,
                    clock=self._clock,
                    sleeper=self._sleep,
                )
            if len(eligible) > 1:
                ambiguous = True
                break
            remaining = deadline - self._clock()
            if remaining <= 0:
                break
            self._sleep(min(0.05, remaining))

        if ambiguous:
            raise BrokerDiscoveryError("terminal_ambiguous")
        if exact_process_seen and terminal_window_seen:
            raise BrokerDiscoveryError("ui_unknown")
        raise BrokerDiscoveryError("terminal_not_found")


class _WineWin32Session:
    def __init__(
        self,
        *,
        backend: WineWin32Backend,
        pid: int,
        dialog: int,
        expected_path: str,
        deadline: float,
        clock: Callable[[], float],
        sleeper: Callable[[float], None],
    ) -> None:
        self._backend = backend
        self._pid = pid
        self._dialog = dialog
        self._expected_path = expected_path
        self._deadline = deadline
        self._clock = clock
        self._sleep = sleeper
        self._candidate_rows: list[int] = []
        self._candidate_servers: list[str] = []
        self._selected_candidate: int | None = None
        self._accepted = False

    def _remaining(self) -> float:
        remaining = self._deadline - self._clock()
        if remaining <= 0:
            raise BrokerDiscoveryError("timeout")
        return remaining

    def _timeout_ms(self) -> int:
        return max(1, min(int(self._remaining() * 1000), 300_000))

    def _controls(self, spec: _ControlSpec) -> tuple[int, ...]:
        try:
            self._assert_process_binding()
            return _matching_controls(
                self._backend,
                self._dialog,
                spec,
                expected_pid=self._pid,
            )
        except BrokerDiscoveryError:
            raise
        except Exception:
            raise BrokerDiscoveryError("ui_unknown") from None

    def _assert_process_binding(self) -> None:
        if (
            not self._backend.is_window(self._dialog)
            or self._backend.window_pid(self._dialog) != self._pid
        ):
            raise BrokerDiscoveryError("ui_unknown")
        actual_path = self._backend.process_image_path(self._pid)
        if actual_path is None or _normalized_path(actual_path) != self._expected_path:
            raise BrokerDiscoveryError("ui_unknown")

    def _assert_control_binding(self, hwnd: int) -> None:
        self._assert_process_binding()
        if (
            not self._backend.is_window(hwnd)
            or self._backend.window_pid(hwnd) != self._pid
        ):
            raise BrokerDiscoveryError("ui_unknown")

    def _unique_control(self, spec: _ControlSpec) -> int:
        controls = self._controls(spec)
        if len(controls) != 1:
            raise BrokerDiscoveryError("ui_unknown")
        return controls[0]

    def _wait_unique_control(self, spec: _ControlSpec) -> int:
        while True:
            controls = self._controls(spec)
            if len(controls) == 1:
                return controls[0]
            if len(controls) > 1:
                raise BrokerDiscoveryError("ui_unknown")
            self._sleep(min(0.05, self._remaining()))

    def _wait_discovery_page(self) -> None:
        while True:
            try:
                self._assert_process_binding()
                if _has_discovery_signature(
                    self._backend, self._dialog, expected_pid=self._pid
                ):
                    return
            except Exception:
                raise BrokerDiscoveryError("ui_unknown") from None
            self._sleep(min(0.05, self._remaining()))

    def _click(self, spec: _ControlSpec) -> None:
        control = self._unique_control(spec)
        self._assert_control_binding(control)
        if not self._backend.is_enabled(control):
            raise BrokerDiscoveryError("ui_unknown")
        try:
            self._backend.click(control, self._timeout_ms())
        except TimeoutError:
            raise BrokerDiscoveryError("timeout") from None
        except Exception:
            raise BrokerDiscoveryError("ui_unknown") from None

    def _select_row(self, row: int) -> None:
        result_list = self._unique_control(_RESULT_LIST)
        self._assert_control_binding(result_list)
        try:
            self._backend.select_list_item(
                result_list, row, self._timeout_ms()
            )
        except TimeoutError:
            raise BrokerDiscoveryError("timeout") from None
        except Exception:
            raise BrokerDiscoveryError("ui_unknown") from None

    def _read_server_combo(self) -> str:
        combo = self._wait_unique_control(_SERVER_COMBO)
        self._assert_control_binding(combo)
        try:
            value = self._backend.get_text(
                combo, _MAX_CONTROL_TEXT, self._timeout_ms()
            )
        except TimeoutError:
            raise BrokerDiscoveryError("timeout") from None
        except Exception:
            raise BrokerDiscoveryError("ui_unknown") from None
        return _canonical_server_label(value)

    def _probe_row(self, row: int) -> str:
        self._select_row(row)
        self._click(_NEXT_BUTTON)
        server = self._read_server_combo()
        self._click(_BACK_BUTTON)
        self._wait_discovery_page()
        return server

    def open_account(self) -> None:
        # The isolated Wine instance must already show the first-start account
        # wizard.  Opening localized menus without UIA or coordinates is outside
        # this strict adapter's compatibility boundary.
        try:
            self._assert_process_binding()
            if not _has_discovery_signature(
                self._backend, self._dialog, expected_pid=self._pid
            ):
                raise BrokerDiscoveryError("ui_unknown")
        except BrokerDiscoveryError:
            raise
        except Exception:
            raise BrokerDiscoveryError("ui_unknown") from None

    def _wait_find_complete(self, find: int, result_list: int) -> int:
        disabled_seen = False
        stable_count: int | None = None
        stable_since: float | None = None
        while True:
            self._assert_control_binding(find)
            self._assert_control_binding(result_list)
            enabled = self._backend.is_enabled(find)
            if not enabled:
                disabled_seen = True
                stable_count = None
                stable_since = None
            else:
                try:
                    count = self._backend.list_item_count(
                        result_list, self._timeout_ms()
                    )
                except TimeoutError:
                    raise BrokerDiscoveryError("timeout") from None
                except Exception:
                    raise BrokerDiscoveryError("ui_unknown") from None
                if count < 0 or count > _MAX_RESULT_ROWS:
                    raise BrokerDiscoveryError("ui_unknown")
                if count != stable_count:
                    stable_count = count
                    stable_since = self._clock()
                settled = (
                    stable_since is not None
                    and self._clock() - stable_since >= 0.15
                )
                if settled and disabled_seen:
                    return count
            self._sleep(min(0.05, self._remaining()))

    def search(self, query: str) -> Sequence[str]:
        edit = self._unique_control(_SEARCH_EDIT)
        find = self._unique_control(_FIND_BUTTON)
        result_list = self._unique_control(_RESULT_LIST)
        if not self._backend.is_enabled(find):
            raise BrokerDiscoveryError("ui_unknown")
        try:
            self._assert_control_binding(edit)
            self._assert_control_binding(find)
            self._backend.set_text(edit, query, self._timeout_ms())
            self._backend.click(find, self._timeout_ms())
        except TimeoutError:
            raise BrokerDiscoveryError("timeout") from None
        except Exception:
            raise BrokerDiscoveryError("ui_unknown") from None

        count = self._wait_find_complete(find, result_list)
        rows: list[int] = []
        servers: list[str] = []
        for row in range(count):
            servers.append(self._probe_row(row))
            rows.append(row)
        self._candidate_rows = rows
        self._candidate_servers = servers
        self._selected_candidate = None
        self._accepted = False
        return tuple(servers)

    def select_server(self, index: int) -> None:
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise BrokerDiscoveryError("ui_unknown")
        try:
            row = self._candidate_rows[index]
            self._candidate_servers[index]
        except IndexError:
            raise BrokerDiscoveryError("ui_unknown") from None
        self._select_row(row)
        self._selected_candidate = index

    def accept(self) -> None:
        if self._selected_candidate is None:
            raise BrokerDiscoveryError("ui_unknown")
        expected = self._candidate_servers[self._selected_candidate]
        self._click(_NEXT_BUTTON)
        actual = self._read_server_combo()
        if normalize_server_name(actual) != normalize_server_name(expected):
            raise BrokerDiscoveryError("ui_unknown")
        self._accepted = True

    def close(self) -> None:
        if not self._backend.is_window(self._dialog):
            return
        self._assert_process_binding()
        if not self._backend.is_visible(self._dialog):
            return
        self._click(_CANCEL_BUTTON)
        while self._backend.is_window(self._dialog) and self._backend.is_visible(
            self._dialog
        ):
            self._sleep(min(0.05, self._remaining()))


def _run_wine_payload(
    payload: dict[str, Any],
    *,
    adapter: WineWin32Mt5BrokerDiscoveryAdapter | None = None,
) -> tuple[dict[str, Any], int]:
    discovery = WindowsMt5BrokerDiscovery(
        adapter or WineWin32Mt5BrokerDiscoveryAdapter()
    )
    return _run_payload(payload, discovery=discovery)


def _discovery_error_payload(error: BrokerDiscoveryError) -> tuple[dict[str, Any], int]:
    exit_code = {
        "invalid_request": 2,
        "no_exact_match": 3,
        "ambiguous_exact_match": 4,
        "timeout": 5,
    }.get(error.code, 6)
    return {
        "ok": False,
        "code": error.code,
        "message": str(error),
    }, exit_code


def _run_wine_autopid_payload(
    payload: dict[str, Any],
    *,
    backend: WineWin32Backend | None = None,
    adapter: WineWin32Mt5BrokerDiscoveryAdapter | None = None,
) -> tuple[dict[str, Any], int]:
    """Validate a Linux request and resolve candidate PIDs inside Win32/Wine."""

    allowed = {
        "terminal_path",
        "expected_server",
        "queries",
        "timeout_seconds",
    }
    required = allowed - {"timeout_seconds"}
    if (
        not isinstance(payload, dict)
        or set(payload) - allowed
        or not required <= set(payload)
    ):
        return _discovery_error_payload(BrokerDiscoveryError("invalid_request"))

    # Reuse the shared strict request validator with a non-authoritative placeholder.
    # The placeholder is discarded before discovery and can never reach the adapter.
    provisional = dict(payload)
    provisional["candidate_pids"] = [1]
    try:
        request = BrokerDiscoveryRequest.from_dict(provisional)
        selected_backend = backend or _CtypesWineWin32Backend()
        candidate_pids = tuple(
            dict.fromkeys(selected_backend.candidate_window_pids(request.terminal_path))
        )
        if not candidate_pids:
            raise BrokerDiscoveryError("terminal_not_found")
        if len(candidate_pids) != 1:
            raise BrokerDiscoveryError("terminal_ambiguous")
        if any(
            isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0
            for pid in candidate_pids
        ):
            raise BrokerDiscoveryError("ui_unknown")
    except BrokerDiscoveryError as error:
        return _discovery_error_payload(error)
    except Exception:
        return _discovery_error_payload(BrokerDiscoveryError("ui_unknown"))

    final_payload = dict(payload)
    final_payload["candidate_pids"] = list(candidate_pids)
    selected_adapter = adapter or WineWin32Mt5BrokerDiscoveryAdapter(selected_backend)
    return _run_wine_payload(final_payload, adapter=selected_adapter)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the adapter under 64-bit Windows Python in the Wine desktop."""

    parser = argparse.ArgumentParser(
        description="Credential-free Wine/Win32 MT5 broker discovery"
    )
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    args = parser.parse_args(argv)

    try:
        payload = json.loads(args.request.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise BrokerDiscoveryError("invalid_request")
        response, exit_code = _run_wine_autopid_payload(payload)
    except (BrokerDiscoveryError, json.JSONDecodeError, OSError):
        error = BrokerDiscoveryError("invalid_request")
        response = {
            "ok": False,
            "code": error.code,
            "message": str(error),
        }
        exit_code = 2
    atomic_json(args.result, response)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
