"""Strict, credential-free MT5 broker discovery through Windows UI Automation.

The public models and selection helpers are platform independent.  ``pywinauto``
is imported only when the default Windows adapter is instantiated, which keeps
the policy and orchestration code testable on non-Windows development hosts.
"""

from __future__ import annotations

import argparse
import json
import os
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, Sequence

from ..broker_resolution import reject_network_target
from ..state_store import atomic_json


__all__ = [
    "BrokerDiscoveryError",
    "BrokerDiscoveryRequest",
    "BrokerDiscoveryResult",
    "BrokerDiscoveryUiAdapter",
    "BrokerDiscoveryUiSession",
    "WindowsMt5BrokerDiscovery",
    "normalize_server_name",
    "select_exact_server",
]


_SAFE_MESSAGES = {
    "invalid_request": "broker discovery request is invalid",
    "terminal_not_found": "eligible terminal window was not found",
    "terminal_ambiguous": "multiple eligible terminal windows were found",
    "ui_unknown": "terminal user interface is not recognized",
    "no_exact_match": "expected server was not found",
    "ambiguous_exact_match": "expected server result is ambiguous",
    "timeout": "broker discovery timed out",
    "internal_error": "broker discovery failed",
}


class BrokerDiscoveryError(RuntimeError):
    """A discovery failure whose text is safe to persist or return from the CLI."""

    def __init__(self, code: str) -> None:
        if code not in _SAFE_MESSAGES:
            code = "internal_error"
        self.code = code
        super().__init__(_SAFE_MESSAGES[code])


def normalize_server_name(value: str) -> str:
    """Normalize formatting only; never perform fuzzy or substring matching."""

    if not isinstance(value, str):
        raise BrokerDiscoveryError("invalid_request")
    if any(unicodedata.category(character).startswith("C") for character in value):
        raise BrokerDiscoveryError("invalid_request")
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def _validated_text(value: str, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise BrokerDiscoveryError("invalid_request")
    if any(unicodedata.category(character).startswith("C") for character in value):
        raise BrokerDiscoveryError("invalid_request")
    normalized = " ".join(unicodedata.normalize("NFKC", value).split())
    if not normalized or len(normalized) > maximum:
        raise BrokerDiscoveryError("invalid_request")
    return normalized


def select_exact_server(candidates: Sequence[str], expected_server: str) -> int | None:
    """Return the unique canonical exact match, or ``None`` when it is absent."""

    expected = normalize_server_name(_validated_text(expected_server, maximum=128))
    matches = []
    for index, candidate in enumerate(candidates):
        try:
            normalized_candidate = normalize_server_name(
                _validated_text(candidate, maximum=256)
            )
        except BrokerDiscoveryError:
            raise BrokerDiscoveryError("ui_unknown") from None
        if normalized_candidate == expected:
            matches.append(index)
    if len(matches) > 1:
        raise BrokerDiscoveryError("ambiguous_exact_match")
    return matches[0] if matches else None


@dataclass(frozen=True)
class BrokerDiscoveryRequest:
    terminal_path: Path
    candidate_pids: tuple[int, ...]
    expected_server: str
    queries: tuple[str, ...]
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        terminal_path = Path(self.terminal_path)
        if (
            not terminal_path.is_absolute()
            or terminal_path.name.casefold() != "terminal64.exe"
        ):
            raise BrokerDiscoveryError("invalid_request")

        if not isinstance(self.candidate_pids, (tuple, list)) or any(
            isinstance(pid, bool) or not isinstance(pid, int)
            for pid in self.candidate_pids
        ):
            raise BrokerDiscoveryError("invalid_request")
        candidate_pids = tuple(dict.fromkeys(self.candidate_pids))
        if (
            not candidate_pids
            or len(candidate_pids) > 32
            or any(pid <= 0 for pid in candidate_pids)
        ):
            raise BrokerDiscoveryError("invalid_request")

        expected_server = _validated_text(self.expected_server, maximum=128)
        try:
            reject_network_target(expected_server, field="expected_server")
        except ValueError:
            raise BrokerDiscoveryError("invalid_request") from None
        if not isinstance(self.queries, (tuple, list)):
            raise BrokerDiscoveryError("invalid_request")
        queries: list[str] = []
        seen: set[str] = set()
        for raw_query in self.queries:
            query = _validated_text(raw_query, maximum=256)
            try:
                reject_network_target(query, field="discovery_query")
            except ValueError:
                raise BrokerDiscoveryError("invalid_request") from None
            key = normalize_server_name(query)
            if key not in seen:
                seen.add(key)
                queries.append(query)
        if not queries or len(queries) > 16:
            raise BrokerDiscoveryError("invalid_request")

        if isinstance(self.timeout_seconds, bool):
            raise BrokerDiscoveryError("invalid_request")
        try:
            timeout_seconds = float(self.timeout_seconds)
        except (TypeError, ValueError):
            raise BrokerDiscoveryError("invalid_request") from None
        if not 1.0 <= timeout_seconds <= 300.0:
            raise BrokerDiscoveryError("invalid_request")

        object.__setattr__(self, "terminal_path", terminal_path)
        object.__setattr__(self, "candidate_pids", candidate_pids)
        object.__setattr__(self, "expected_server", expected_server)
        object.__setattr__(self, "queries", tuple(queries))
        object.__setattr__(self, "timeout_seconds", timeout_seconds)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "BrokerDiscoveryRequest":
        allowed = {
            "terminal_path",
            "candidate_pids",
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
            raise BrokerDiscoveryError("invalid_request")
        if not isinstance(payload["candidate_pids"], list):
            raise BrokerDiscoveryError("invalid_request")
        if not isinstance(payload["queries"], list):
            raise BrokerDiscoveryError("invalid_request")
        if not isinstance(payload["terminal_path"], str):
            raise BrokerDiscoveryError("invalid_request")
        if not isinstance(payload["expected_server"], str):
            raise BrokerDiscoveryError("invalid_request")
        return cls(
            terminal_path=Path(payload["terminal_path"]),
            candidate_pids=tuple(payload["candidate_pids"]),
            expected_server=payload["expected_server"],
            queries=tuple(payload["queries"]),
            timeout_seconds=payload.get("timeout_seconds", 30.0),
        )


@dataclass(frozen=True)
class BrokerDiscoveryResult:
    server: str
    query_index: int


class BrokerDiscoveryUiSession(Protocol):
    def open_account(self) -> None: ...

    def search(self, query: str) -> Sequence[str]: ...

    def select_server(self, index: int) -> None: ...

    def accept(self) -> None: ...

    def close(self) -> None: ...


class BrokerDiscoveryUiAdapter(Protocol):
    def open_session(
        self,
        *,
        terminal_path: Path,
        candidate_pids: Sequence[int],
        timeout_seconds: float,
    ) -> BrokerDiscoveryUiSession: ...


class WindowsMt5BrokerDiscovery:
    """Drive one terminal instance without ever receiving account credentials."""

    def __init__(self, adapter: BrokerDiscoveryUiAdapter | None = None) -> None:
        self._adapter = adapter

    def discover(
        self,
        *,
        terminal_path: Path,
        candidate_pids: Sequence[int],
        expected_server: str,
        queries: Sequence[str],
        timeout_seconds: float = 30.0,
    ) -> BrokerDiscoveryResult:
        request = BrokerDiscoveryRequest(
            terminal_path=terminal_path,
            candidate_pids=tuple(candidate_pids),
            expected_server=expected_server,
            queries=tuple(queries),
            timeout_seconds=timeout_seconds,
        )
        adapter = self._adapter or PywinautoMt5BrokerDiscoveryAdapter()
        deadline = time.monotonic() + request.timeout_seconds
        session: BrokerDiscoveryUiSession | None = None
        try:
            session = adapter.open_session(
                terminal_path=request.terminal_path,
                candidate_pids=request.candidate_pids,
                timeout_seconds=request.timeout_seconds,
            )
            session.open_account()
            for query_index, query in enumerate(request.queries):
                if time.monotonic() >= deadline:
                    raise BrokerDiscoveryError("timeout")
                candidates = tuple(session.search(query))
                match = select_exact_server(candidates, request.expected_server)
                if match is None:
                    continue
                session.select_server(match)
                session.accept()
                result = BrokerDiscoveryResult(
                    server=request.expected_server,
                    query_index=query_index,
                )
                # A modal wizard left open can block the subsequent credential
                # bootstrap.  Cleanup is therefore mandatory on success and only
                # best-effort on an already-failed discovery.
                session.close()
                session = None
                return result
            raise BrokerDiscoveryError("no_exact_match")
        except BrokerDiscoveryError:
            raise
        except TimeoutError:
            raise BrokerDiscoveryError("timeout") from None
        except Exception:
            raise BrokerDiscoveryError("ui_unknown") from None
        finally:
            if session is not None:
                try:
                    session.close()
                except Exception:
                    pass


class PywinautoMt5BrokerDiscoveryAdapter:
    """English MT5 UIA adapter. Unknown layouts fail closed."""

    def __init__(self) -> None:
        try:
            import psutil
            from pywinauto import Application, Desktop
        except ImportError as error:
            raise BrokerDiscoveryError("ui_unknown") from error
        self._psutil = psutil
        self._application = Application
        self._desktop = Desktop

    def open_session(
        self,
        *,
        terminal_path: Path,
        candidate_pids: Sequence[int],
        timeout_seconds: float,
    ) -> BrokerDiscoveryUiSession:
        expected = os.path.normcase(os.path.abspath(str(terminal_path)))
        deadline = time.monotonic() + timeout_seconds
        ambiguous = False
        while True:
            eligible: list[int] = []
            for pid in candidate_pids:
                try:
                    actual = os.path.normcase(
                        os.path.abspath(self._psutil.Process(pid).exe())
                    )
                except (self._psutil.NoSuchProcess, self._psutil.AccessDenied, OSError):
                    continue
                if actual == expected:
                    eligible.append(pid)

            windows: list[tuple[int, Any]] = []
            for pid in eligible:
                try:
                    app = self._application(backend="uia").connect(process=pid)
                    window = app.top_window()
                    if window.is_visible() and window.is_enabled():
                        windows.append((pid, window))
                except Exception:
                    continue
            if len(windows) == 1:
                pid, window = windows[0]
                return _PywinautoSession(
                    desktop=self._desktop(backend="uia"),
                    pid=pid,
                    main_window=window,
                    deadline=deadline,
                )
            ambiguous = len(windows) > 1
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(0.1, remaining))
        raise BrokerDiscoveryError(
            "terminal_ambiguous" if ambiguous else "terminal_not_found"
        )


class _PywinautoSession:
    _RESULT_TYPES = {"DataItem", "ListItem", "TreeItem"}

    def __init__(
        self, *, desktop: Any, pid: int, main_window: Any, deadline: float
    ) -> None:
        self._desktop = desktop
        self._pid = pid
        self._main_window = main_window
        self._deadline = deadline
        self._dialog: Any | None = None
        self._candidate_controls: list[Any] = []
        self._accepted = False

    def _remaining(self) -> float:
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise BrokerDiscoveryError("timeout")
        return remaining

    @staticmethod
    def _control_type(control: Any) -> str:
        return str(getattr(control.element_info, "control_type", ""))

    @staticmethod
    def _visible_enabled(control: Any) -> bool:
        try:
            return bool(control.is_visible() and control.is_enabled())
        except Exception:
            return False

    def _wait_for_dialog(self) -> Any:
        while self._remaining() > 0:
            windows = self._desktop.windows(process=self._pid, visible_only=True)
            matches = [
                window
                for window in windows
                if normalize_server_name(window.window_text()) == "open an account"
            ]
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                raise BrokerDiscoveryError("ui_unknown")
            time.sleep(min(0.1, self._remaining()))
        raise BrokerDiscoveryError("timeout")

    def open_account(self) -> None:
        # A clean generic installation often opens this wizard automatically on
        # first start.  Reuse that exact dialog instead of trying to invoke a
        # menu on a modal window that does not expose one.
        try:
            title = normalize_server_name(self._main_window.window_text())
        except Exception:
            title = ""
        if title == "open an account":
            self._dialog = self._main_window
            return
        # The Insert shortcut is context-sensitive in MT5 (it is documented for
        # the Accounts/Navigator control), so a generic focused window is unsafe.
        # Invoke the named application menu and fail closed if the build does not
        # expose it through UIA.
        try:
            self._main_window.menu_select("File->Open an Account", exact=True)
        except Exception:
            raise BrokerDiscoveryError("ui_unknown") from None
        self._dialog = self._wait_for_dialog()

    def _descendants(self) -> list[Any]:
        if self._dialog is None:
            raise BrokerDiscoveryError("ui_unknown")
        return [
            control
            for control in self._dialog.descendants()
            if self._visible_enabled(control)
        ]

    def _unique_control(
        self, *, control_type: str, titles: set[str] | None = None
    ) -> Any:
        matches = []
        for control in self._descendants():
            if self._control_type(control) != control_type:
                continue
            if (
                titles is not None
                and normalize_server_name(control.window_text()) not in titles
            ):
                continue
            matches.append(control)
        if len(matches) != 1:
            raise BrokerDiscoveryError("ui_unknown")
        return matches[0]

    @staticmethod
    def _labels(control: Any) -> list[str]:
        values = [control.window_text()]
        try:
            values.extend(child.window_text() for child in control.descendants())
        except Exception:
            pass
        labels: list[str] = []
        seen: set[str] = set()
        for value in values:
            try:
                label = _validated_text(value, maximum=256)
            except BrokerDiscoveryError:
                continue
            key = normalize_server_name(label)
            if key not in seen:
                seen.add(key)
                labels.append(label)
        return labels

    def _leaf_result_controls(self) -> list[Any]:
        """Return selectable result rows without also flattening their parents.

        MT5 commonly exposes ``broker -> Demo/Live`` as nested TreeItems.  Reading
        every descendant label from both the broker parent and each server child
        would manufacture duplicate server candidates and turn one real match into
        a false ambiguity.  Only leaf result rows can identify a concrete server.
        """

        controls = [
            control
            for control in self._descendants()
            if self._control_type(control) in self._RESULT_TYPES
        ]
        leaves: list[Any] = []
        for control in controls:
            try:
                nested_results = any(
                    self._control_type(child) in self._RESULT_TYPES
                    for child in control.descendants()
                )
            except Exception:
                raise BrokerDiscoveryError("ui_unknown") from None
            if not nested_results:
                leaves.append(control)
        return leaves

    def search(self, query: str) -> Sequence[str]:
        edit = self._unique_control(control_type="Edit")
        find = self._unique_control(
            control_type="Button",
            titles={"find", "find your broker"},
        )
        edit.set_edit_text(query)
        find.invoke()

        # Search is complete only when its button is enabled again.  A short stable
        # interval also covers builds that never expose the disabled state to UIA.
        stable_since: float | None = None
        previous: tuple[str, ...] | None = None
        controls: list[Any] = []
        labels: list[str] = []
        while self._remaining() > 0:
            if find.is_enabled():
                current_controls: list[Any] = []
                current_labels: list[str] = []
                for control in self._leaf_result_controls():
                    for label in self._labels(control):
                        current_controls.append(control)
                        current_labels.append(label)
                snapshot = tuple(current_labels)
                if snapshot == previous:
                    stable_since = stable_since or time.monotonic()
                    if time.monotonic() - stable_since >= 0.25:
                        controls, labels = current_controls, current_labels
                        break
                else:
                    previous = snapshot
                    stable_since = time.monotonic()
            time.sleep(min(0.1, self._remaining()))
        else:
            raise BrokerDiscoveryError("timeout")
        self._candidate_controls = controls
        return tuple(labels)

    def select_server(self, index: int) -> None:
        try:
            control = self._candidate_controls[index]
        except IndexError:
            raise BrokerDiscoveryError("ui_unknown") from None
        try:
            control.select()
        except Exception:
            try:
                control.invoke()
            except Exception:
                raise BrokerDiscoveryError("ui_unknown") from None

    def accept(self) -> None:
        next_button = self._unique_control(
            control_type="Button", titles={"next", "next >", "next>"}
        )
        next_button.invoke()
        self._accepted = True

    def close(self) -> None:
        if self._dialog is not None and self._dialog.exists():
            cancel = self._unique_control(control_type="Button", titles={"cancel"})
            cancel.invoke()
            while self._dialog.exists() and self._remaining() > 0:
                time.sleep(min(0.1, self._remaining()))


def _run_payload(
    payload: dict[str, Any],
    *,
    discovery: WindowsMt5BrokerDiscovery | None = None,
) -> tuple[dict[str, Any], int]:
    try:
        request = BrokerDiscoveryRequest.from_dict(payload)
        worker = discovery or WindowsMt5BrokerDiscovery()
        result = worker.discover(
            terminal_path=request.terminal_path,
            candidate_pids=request.candidate_pids,
            expected_server=request.expected_server,
            queries=request.queries,
            timeout_seconds=request.timeout_seconds,
        )
        return {
            "ok": True,
            "code": "ok",
            "server": result.server,
            "query_index": result.query_index,
        }, 0
    except BrokerDiscoveryError as error:
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
    except Exception:
        error = BrokerDiscoveryError("internal_error")
        return {
            "ok": False,
            "code": error.code,
            "message": str(error),
        }, 6


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Credential-free MT5 broker discovery")
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    args = parser.parse_args(argv)

    try:
        payload = json.loads(args.request.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise BrokerDiscoveryError("invalid_request")
        response, exit_code = _run_payload(payload)
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
