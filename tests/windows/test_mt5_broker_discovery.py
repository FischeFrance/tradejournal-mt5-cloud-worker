from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Sequence

import pytest

from windows_agent.worker.mt5_broker_discovery import (
    BrokerDiscoveryError,
    BrokerDiscoveryRequest,
    BrokerDiscoveryResult,
    PywinautoMt5BrokerDiscoveryAdapter,
    WindowsMt5BrokerDiscovery,
    _PywinautoSession,
    _run_payload,
    main,
    normalize_server_name,
    select_exact_server,
)


class FakeSession:
    def __init__(self, responses: dict[str, Sequence[str]]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, object]] = []
        self.closed = 0

    def open_account(self) -> None:
        self.calls.append(("open_account", None))

    def search(self, query: str) -> Sequence[str]:
        self.calls.append(("search", query))
        return self.responses.get(query, ())

    def select_server(self, index: int) -> None:
        self.calls.append(("select_server", index))

    def accept(self) -> None:
        self.calls.append(("accept", None))

    def close(self) -> None:
        self.closed += 1


class FakeAdapter:
    def __init__(self, session: FakeSession) -> None:
        self.session = session
        self.opened_with: dict[str, object] | None = None

    def open_session(
        self,
        *,
        terminal_path: Path,
        candidate_pids: Sequence[int],
        timeout_seconds: float,
    ) -> FakeSession:
        self.opened_with = {
            "terminal_path": terminal_path,
            "candidate_pids": tuple(candidate_pids),
            "timeout_seconds": timeout_seconds,
        }
        return self.session


def terminal(tmp_path: Path) -> Path:
    path = tmp_path / "MT5 isolated" / "terminal64.exe"
    path.parent.mkdir(exist_ok=True)
    path.touch()
    return path


def test_module_does_not_import_pywinauto_on_non_windows_host() -> None:
    assert "pywinauto" not in sys.modules


def test_worker_package_exports_public_discovery_contract() -> None:
    from windows_agent.worker import (
        BrokerDiscoveryError as ExportedError,
        BrokerDiscoveryRequest as ExportedRequest,
        WindowsMt5BrokerDiscovery as ExportedDiscovery,
    )

    assert ExportedError is BrokerDiscoveryError
    assert ExportedRequest is BrokerDiscoveryRequest
    assert ExportedDiscovery is WindowsMt5BrokerDiscovery


def test_open_account_uses_named_menu_not_context_sensitive_insert() -> None:
    class MainWindow:
        def __init__(self) -> None:
            self.menu_calls: list[tuple[str, bool]] = []

        def menu_select(self, path: str, exact: bool) -> None:
            self.menu_calls.append((path, exact))

        def type_keys(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("generic Insert must not be used")

    class Dialog:
        @staticmethod
        def window_text() -> str:
            return "Open an Account"

    class Desktop:
        @staticmethod
        def windows(**_kwargs: object) -> list[Dialog]:
            return [Dialog()]

    main_window = MainWindow()
    session = _PywinautoSession(
        desktop=Desktop(),
        pid=123,
        main_window=main_window,
        deadline=10**12,
    )

    session.open_account()

    assert main_window.menu_calls == [("File->Open an Account", True)]


def test_open_account_reuses_first_start_wizard_without_invoking_menu() -> None:
    class Dialog:
        @staticmethod
        def window_text() -> str:
            return "Open an Account"

        @staticmethod
        def menu_select(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("an already-open wizard must be reused")

    dialog = Dialog()
    session = _PywinautoSession(
        desktop=object(),
        pid=123,
        main_window=dialog,
        deadline=10**12,
    )

    session.open_account()

    assert session._dialog is dialog


def test_close_invokes_named_cancel_without_native_escape() -> None:
    class ElementInfo:
        control_type = "Button"

    class Dialog:
        visible = True

        def exists(self) -> bool:
            return self.visible

        def descendants(self) -> list["Cancel"]:
            return [Cancel(self)]

        @staticmethod
        def type_keys(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("native Escape must not be used")

    class Cancel:
        element_info = ElementInfo()

        def __init__(self, dialog: Dialog) -> None:
            self.dialog = dialog

        @staticmethod
        def is_visible() -> bool:
            return True

        @staticmethod
        def is_enabled() -> bool:
            return True

        @staticmethod
        def window_text() -> str:
            return "Cancel"

        def invoke(self) -> None:
            self.dialog.visible = False

    dialog = Dialog()
    session = _PywinautoSession(
        desktop=object(),
        pid=123,
        main_window=object(),
        deadline=10**12,
    )
    session._dialog = dialog

    session.close()

    assert dialog.visible is False


def test_default_adapter_retries_until_terminal_window_is_visible(
    tmp_path: Path,
) -> None:
    terminal_path = terminal(tmp_path)

    class Process:
        @staticmethod
        def exe() -> str:
            return str(terminal_path)

    class Psutil:
        class NoSuchProcess(Exception):
            pass

        class AccessDenied(Exception):
            pass

        @staticmethod
        def Process(_pid: int) -> Process:
            return Process()

    class Window:
        @staticmethod
        def is_visible() -> bool:
            return True

        @staticmethod
        def is_enabled() -> bool:
            return True

    class App:
        @staticmethod
        def top_window() -> Window:
            return Window()

    class ApplicationFactory:
        def __init__(self) -> None:
            self.attempts = 0

        def __call__(self, **_kwargs: object) -> "ApplicationFactory":
            return self

        def connect(self, **_kwargs: object) -> App:
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError("window not ready")
            return App()

    class DesktopFactory:
        def __call__(self, **_kwargs: object) -> object:
            return object()

    factory = ApplicationFactory()
    adapter = object.__new__(PywinautoMt5BrokerDiscoveryAdapter)
    adapter._psutil = Psutil()  # type: ignore[attr-defined]
    adapter._application = factory  # type: ignore[attr-defined]
    adapter._desktop = DesktopFactory()  # type: ignore[attr-defined]

    session = adapter.open_session(
        terminal_path=terminal_path,
        candidate_pids=(123,),
        timeout_seconds=1,
    )

    assert isinstance(session, _PywinautoSession)
    assert factory.attempts == 2


def test_normalization_is_canonical_but_selection_is_not_fuzzy() -> None:
    assert normalize_server_name("  FPMTrading-Live  ") == "fpmtrading-live"
    assert select_exact_server(
        ["FPMTrading-Demo", "fpmtrading-live"], "FPMTrading-Live"
    ) == 1
    assert select_exact_server(["prefix-FPMTrading-Live"], "FPMTrading-Live") is None


def test_duplicate_exact_server_is_ambiguous() -> None:
    with pytest.raises(BrokerDiscoveryError) as captured:
        select_exact_server(
            ["FPMTrading-Live", "fpmtrading-live"], "FPMTrading-Live"
        )
    assert captured.value.code == "ambiguous_exact_match"
    assert "FPM" not in str(captured.value)


def test_nested_broker_tree_uses_only_leaf_server_rows() -> None:
    class ElementInfo:
        def __init__(self, control_type: str) -> None:
            self.control_type = control_type

    class Control:
        def __init__(
            self,
            text: str,
            control_type: str,
            children: Sequence["Control"] = (),
        ) -> None:
            self._text = text
            self.element_info = ElementInfo(control_type)
            self._children = tuple(children)

        @staticmethod
        def is_visible() -> bool:
            return True

        @staticmethod
        def is_enabled() -> bool:
            return True

        def window_text(self) -> str:
            return self._text

        def descendants(self) -> list["Control"]:
            descendants: list[Control] = []
            for child in self._children:
                descendants.append(child)
                descendants.extend(child.descendants())
            return descendants

    live = Control("FPMTrading-Live", "TreeItem")
    demo = Control("FPMTrading-Demo", "TreeItem")
    broker = Control("FPM Trading Ltd.", "TreeItem", (demo, live))

    class Dialog:
        @staticmethod
        def descendants() -> list[Control]:
            return [broker, demo, live]

    session = _PywinautoSession(
        desktop=object(),
        pid=123,
        main_window=object(),
        deadline=10**12,
    )
    session._dialog = Dialog()

    controls = session._leaf_result_controls()
    labels = [label for control in controls for label in session._labels(control)]

    assert controls == [demo, live]
    assert labels == ["FPMTrading-Demo", "FPMTrading-Live"]
    assert select_exact_server(labels, "FPMTrading-Live") == 1


def test_request_normalizes_and_deduplicates_queries(tmp_path: Path) -> None:
    request = BrokerDiscoveryRequest(
        terminal_path=terminal(tmp_path),
        candidate_pids=(44, 44, 45),
        expected_server=" FPMTrading-Live ",
        queries=("FPM Trading", " fpm  trading ", "FPMTrading"),
    )

    assert request.candidate_pids == (44, 45)
    assert request.expected_server == "FPMTrading-Live"
    assert request.queries == ("FPM Trading", "FPMTrading")


@pytest.mark.parametrize(
    "overrides",
    [
        {"terminal_path": Path("other.exe")},
        {"candidate_pids": ()},
        {"candidate_pids": (0,)},
        {"candidate_pids": (True,)},
        {"candidate_pids": ("123",)},
        {"expected_server": "bad\nserver"},
        {"expected_server": "bad\rserver"},
        {"queries": ()},
        {"queries": ("bad\nquery",)},
        {"queries": ("bad\x00query",)},
        {"timeout_seconds": 0.5},
        {"timeout_seconds": True},
    ],
)
def test_request_rejects_unsafe_or_incomplete_fields(
    tmp_path: Path, overrides: dict[str, object]
) -> None:
    values: dict[str, object] = {
        "terminal_path": terminal(tmp_path),
        "candidate_pids": (42,),
        "expected_server": "FPMTrading-Live",
        "queries": ("FPM Trading",),
        "timeout_seconds": 30.0,
    }
    values.update(overrides)
    with pytest.raises(BrokerDiscoveryError) as captured:
        BrokerDiscoveryRequest(**values)  # type: ignore[arg-type]
    assert captured.value.code == "invalid_request"


@pytest.mark.parametrize(
    "network_target",
    (
        "127.0.0.1",
        "169.254.169.254",
        "localhost",
        "metadata.internal",
        "broker.example",
        "[::1]",
        "0x7f000001",
    ),
)
def test_request_rejects_network_targets_before_ui(
    tmp_path: Path, network_target: str
) -> None:
    with pytest.raises(BrokerDiscoveryError) as captured:
        BrokerDiscoveryRequest(
            terminal_path=terminal(tmp_path),
            candidate_pids=(100,),
            expected_server="Broker-Live",
            queries=(network_target,),
        )

    assert captured.value.code == "invalid_request"


def test_request_rejects_network_target_as_expected_server(tmp_path: Path) -> None:
    with pytest.raises(BrokerDiscoveryError) as captured:
        BrokerDiscoveryRequest(
            terminal_path=terminal(tmp_path),
            candidate_pids=(100,),
            expected_server="127.0.0.1",
            queries=("Broker",),
        )

    assert captured.value.code == "invalid_request"


def test_queries_run_in_order_until_unique_exact_server(tmp_path: Path) -> None:
    session = FakeSession(
        {
            "first": ["OtherBroker-Live"],
            "second": ["FPMTrading-Demo", "FPMTrading-Live"],
        }
    )
    adapter = FakeAdapter(session)

    result = WindowsMt5BrokerDiscovery(adapter).discover(
        terminal_path=terminal(tmp_path),
        candidate_pids=(100, 101),
        expected_server="FPMTrading-Live",
        queries=("first", "second", "unused"),
        timeout_seconds=10,
    )

    assert result == BrokerDiscoveryResult(server="FPMTrading-Live", query_index=1)
    assert session.calls == [
        ("open_account", None),
        ("search", "first"),
        ("search", "second"),
        ("select_server", 1),
        ("accept", None),
    ]
    assert session.closed == 1
    assert adapter.opened_with == {
        "terminal_path": terminal(tmp_path),
        "candidate_pids": (100, 101),
        "timeout_seconds": 10.0,
    }


def test_zero_matches_fails_closed_and_cleans_up(tmp_path: Path) -> None:
    session = FakeSession({"one": ["FPMTrading-Demo"], "two": []})
    with pytest.raises(BrokerDiscoveryError) as captured:
        WindowsMt5BrokerDiscovery(FakeAdapter(session)).discover(
            terminal_path=terminal(tmp_path),
            candidate_pids=(100,),
            expected_server="FPMTrading-Live",
            queries=("one", "two"),
        )

    assert captured.value.code == "no_exact_match"
    assert session.closed == 1
    assert "FPM" not in str(captured.value)


def test_ambiguous_match_never_selects_and_cleans_up(tmp_path: Path) -> None:
    session = FakeSession(
        {"one": ["FPMTrading-Live", "fpmtrading-live"]}
    )
    with pytest.raises(BrokerDiscoveryError) as captured:
        WindowsMt5BrokerDiscovery(FakeAdapter(session)).discover(
            terminal_path=terminal(tmp_path),
            candidate_pids=(100,),
            expected_server="FPMTrading-Live",
            queries=("one",),
        )

    assert captured.value.code == "ambiguous_exact_match"
    assert not any(call[0] == "select_server" for call in session.calls)
    assert session.closed == 1


class UnknownUiSession(FakeSession):
    def open_account(self) -> None:
        raise RuntimeError("window title and local path must not escape")


def test_unknown_ui_exception_is_sanitized(tmp_path: Path) -> None:
    session = UnknownUiSession({})
    with pytest.raises(BrokerDiscoveryError) as captured:
        WindowsMt5BrokerDiscovery(FakeAdapter(session)).discover(
            terminal_path=terminal(tmp_path),
            candidate_pids=(100,),
            expected_server="FPMTrading-Live",
            queries=("FPM Trading",),
        )
    assert captured.value.code == "ui_unknown"
    assert str(captured.value) == "terminal user interface is not recognized"
    assert session.closed == 1


def test_cli_payload_rejects_secret_or_unknown_fields(tmp_path: Path) -> None:
    request_path = tmp_path / "request.json"
    result_path = tmp_path / "result.json"
    request_path.write_text(
        json.dumps(
            {
                "terminal_path": str(terminal(tmp_path)),
                "candidate_pids": [100],
                "expected_server": "FPMTrading-Live",
                "queries": ["FPM Trading"],
                "password": "must-not-be-accepted",
            }
        ),
        encoding="utf-8",
    )

    assert main(["--request", str(request_path), "--result", str(result_path)]) == 2
    response = json.loads(result_path.read_text(encoding="utf-8"))
    assert response == {
        "code": "invalid_request",
        "message": "broker discovery request is invalid",
        "ok": False,
    }
    assert "password" not in result_path.read_text(encoding="utf-8").lower()


def test_cli_contract_returns_only_safe_success_fields(tmp_path: Path) -> None:
    session = FakeSession({"FPM Trading": ["FPMTrading-Live"]})
    payload = {
        "terminal_path": str(terminal(tmp_path)),
        "candidate_pids": [100],
        "expected_server": "FPMTrading-Live",
        "queries": ["FPM Trading"],
    }

    response, exit_code = _run_payload(
        payload,
        discovery=WindowsMt5BrokerDiscovery(FakeAdapter(session)),
    )

    assert exit_code == 0
    assert response == {
        "ok": True,
        "code": "ok",
        "server": "FPMTrading-Live",
        "query_index": 0,
    }
