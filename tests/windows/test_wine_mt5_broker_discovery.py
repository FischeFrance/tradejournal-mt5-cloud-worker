from __future__ import annotations

import ctypes
import inspect
import os
from pathlib import Path
from typing import Sequence

import pytest

from windows_agent.worker.mt5_broker_discovery import (
    BrokerDiscoveryError,
    BrokerDiscoveryResult,
    WindowsMt5BrokerDiscovery,
)
from windows_agent.worker.wine_mt5_broker_discovery import (
    WineWin32Mt5BrokerDiscoveryAdapter,
    _CtypesWineWin32Backend,
    _run_wine_autopid_payload,
    _run_wine_payload,
)


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds


class FakeWineWin32Backend:
    _DISCOVERY_CONTROLS = (
        (1, 10814, "Edit"),
        (2, 10815, "Button"),
        (3, 10729, "SysListView32"),
        (4, 12324, "Button"),
        (5, 2, "Button"),
    )
    _ACCOUNT_CONTROLS = (
        (6, 12323, "Button"),
        (4, 12324, "Button"),
        (5, 2, "Button"),
        (7, 10139, "ComboBox"),
    )

    def __init__(
        self,
        terminal_path: Path,
        *,
        pids: Sequence[int] = (400,),
        windows: Sequence[int] = (900,),
        recognized: bool = True,
        responses: dict[str, Sequence[str]] | None = None,
    ) -> None:
        self.terminal_path = str(terminal_path)
        self.pids = tuple(pids)
        self.windows = tuple(windows)
        if len(self.pids) == len(self.windows):
            self.window_owners = dict(zip(self.windows, self.pids))
        else:
            owner = self.pids[0] if self.pids else 0
            self.window_owners = {window: owner for window in self.windows}
        self.recognized = recognized
        self.responses = responses or {}
        self.page = {window: "discovery" for window in self.windows}
        self.open = {window: True for window in self.windows}
        self.query = {window: "" for window in self.windows}
        self.rows = {window: [] for window in self.windows}
        self.selected = {window: None for window in self.windows}
        self.combo = {window: "" for window in self.windows}
        self.actions: list[tuple[str, object]] = []
        self.next_clicks = 0
        self.override_on_next_number: int | None = None
        self.final_combo_override: str | None = None
        self.timeout_on_find = False
        self.cancel_closes = True
        self.find_disabled_polls = {window: 0 for window in self.windows}
        self._metadata: dict[int, tuple[int, str, int]] = {}
        for window in self.windows:
            for suffix, control_id, class_name in (
                self._DISCOVERY_CONTROLS + self._ACCOUNT_CONTROLS
            ):
                self._metadata[self._child(window, suffix)] = (
                    control_id,
                    class_name,
                    window,
                )

    @staticmethod
    def _child(window: int, suffix: int) -> int:
        return window * 100 + suffix

    def _window_for(self, hwnd: int) -> int:
        return self._metadata[hwnd][2]

    def process_image_path(self, pid: int) -> str | None:
        return self.terminal_path if pid in self.pids else None

    def candidate_window_pids(self, terminal_path: Path) -> Sequence[int]:
        if os.path.normcase(str(terminal_path)) != os.path.normcase(self.terminal_path):
            return ()
        return self.pids

    def window_pid(self, hwnd: int) -> int:
        if hwnd in self.window_owners:
            return self.window_owners[hwnd]
        try:
            window = self._window_for(hwnd)
        except KeyError:
            return 0
        return self.window_owners[window]

    def top_level_windows(self, pid: int) -> Sequence[int]:
        return tuple(
            window
            for window in self.windows
            if self.window_owners[window] == pid
        )

    def descendant_windows(self, hwnd: int) -> Sequence[int]:
        if not self.recognized or not self.open.get(hwnd, False):
            return ()
        controls = (
            self._DISCOVERY_CONTROLS
            if self.page[hwnd] == "discovery"
            else self._ACCOUNT_CONTROLS
        )
        return tuple(self._child(hwnd, suffix) for suffix, _cid, _class in controls)

    def is_window(self, hwnd: int) -> bool:
        if hwnd in self.open:
            return self.open[hwnd]
        try:
            window = self._window_for(hwnd)
        except KeyError:
            return False
        return self.open[window] and hwnd in self.descendant_windows(window)

    def is_visible(self, hwnd: int) -> bool:
        return self.is_window(hwnd)

    def is_enabled(self, hwnd: int) -> bool:
        try:
            control_id, _class_name, window = self._metadata[hwnd]
        except KeyError:
            return self.is_window(hwnd)
        if control_id == 10815 and self.find_disabled_polls[window] > 0:
            self.find_disabled_polls[window] -= 1
            return False
        return self.is_window(hwnd)

    def class_name(self, hwnd: int) -> str:
        return self._metadata[hwnd][1]

    def control_id(self, hwnd: int) -> int:
        return self._metadata[hwnd][0]

    def set_text(self, hwnd: int, value: str, timeout_ms: int) -> None:
        assert timeout_ms > 0
        window = self._window_for(hwnd)
        assert self.control_id(hwnd) == 10814
        self.query[window] = value
        self.actions.append(("set_text", value))

    def click(self, hwnd: int, timeout_ms: int) -> None:
        assert timeout_ms > 0
        window = self._window_for(hwnd)
        control_id = self.control_id(hwnd)
        self.actions.append(("click", control_id))
        if control_id == 10815:
            if self.timeout_on_find:
                raise TimeoutError("private backend detail")
            self.rows[window] = list(self.responses.get(self.query[window], ()))
            self.selected[window] = None
            self.find_disabled_polls[window] = 2
        elif control_id == 12324:
            selected = self.selected[window]
            assert selected is not None
            self.next_clicks += 1
            self.page[window] = "account"
            self.combo[window] = self.rows[window][selected]
            if (
                self.override_on_next_number == self.next_clicks
                and self.final_combo_override is not None
            ):
                self.combo[window] = self.final_combo_override
        elif control_id == 12323:
            self.page[window] = "discovery"
        elif control_id == 2 and self.cancel_closes:
            self.open[window] = False

    def get_text(self, hwnd: int, maximum: int, timeout_ms: int) -> str:
        assert maximum == 256
        assert timeout_ms > 0
        window = self._window_for(hwnd)
        assert self.control_id(hwnd) == 10139
        self.actions.append(("get_text", self.combo[window]))
        return self.combo[window]

    def list_item_count(self, hwnd: int, timeout_ms: int) -> int:
        assert timeout_ms > 0
        window = self._window_for(hwnd)
        assert self.control_id(hwnd) == 10729
        self.actions.append(("list_item_count", len(self.rows[window])))
        return len(self.rows[window])

    def select_list_item(self, hwnd: int, index: int, timeout_ms: int) -> None:
        assert timeout_ms > 0
        window = self._window_for(hwnd)
        assert self.control_id(hwnd) == 10729
        if not 0 <= index < len(self.rows[window]):
            raise IndexError(index)
        self.selected[window] = index
        self.actions.append(("select_list_item", index))


def terminal(tmp_path: Path) -> Path:
    path = tmp_path / "Wine Prefix" / "terminal64.exe"
    path.parent.mkdir()
    path.touch()
    return path


def adapter_for(
    backend: FakeWineWin32Backend, clock: FakeClock
) -> WineWin32Mt5BrokerDiscoveryAdapter:
    return WineWin32Mt5BrokerDiscoveryAdapter(
        backend, clock=clock, sleeper=clock.sleep
    )


def test_adapter_contract_has_no_credential_or_network_parameters() -> None:
    parameters = inspect.signature(
        WineWin32Mt5BrokerDiscoveryAdapter.open_session
    ).parameters

    assert set(parameters) == {
        "self",
        "terminal_path",
        "candidate_pids",
        "timeout_seconds",
    }


class _FakeKernel32Memory:
    def __init__(self) -> None:
        self.freed: list[tuple[object, object]] = []
        self.closed: list[object] = []

    def VirtualFreeEx(
        self, process: object, remote: object, _size: int, _operation: int
    ) -> bool:
        self.freed.append((process, remote))
        return True

    def CloseHandle(self, process: object) -> bool:
        self.closed.append(process)
        return True


def _remote_state_backend(*, timeout: bool) -> tuple[
    _CtypesWineWin32Backend, _FakeKernel32Memory, object, ctypes.c_void_p
]:
    backend = object.__new__(_CtypesWineWin32Backend)
    kernel = _FakeKernel32Memory()
    process = object()
    remote = ctypes.c_void_p(0x1234)
    backend._kernel32 = kernel
    backend.window_pid = lambda _hwnd: 400
    backend._write_remote_structure = lambda _pid, _value: (process, remote)

    def send(*_args: object, **_kwargs: object) -> int:
        if timeout:
            raise TimeoutError("receiver still owns the remote pointer")
        return 1

    backend._send_message = send
    return backend, kernel, process, remote


def test_remote_list_state_is_freed_after_completed_message() -> None:
    backend, kernel, process, remote = _remote_state_backend(timeout=False)

    backend._set_list_state(100, 0, 3, 1000)

    assert kernel.freed == [(process, remote)]
    assert kernel.closed == [process]


def test_remote_list_state_stays_allocated_when_message_times_out() -> None:
    backend, kernel, process, _remote = _remote_state_backend(timeout=True)

    with pytest.raises(TimeoutError):
        backend._set_list_state(100, 0, 3, 1000)

    assert kernel.freed == []
    assert kernel.closed == [process]


def test_cli_payload_rejects_credentials_before_opening_backend(
    tmp_path: Path,
) -> None:
    payload = {
        "terminal_path": str(terminal(tmp_path)),
        "candidate_pids": [400],
        "expected_server": "Broker-Live",
        "queries": ["broker"],
        "password": "must-not-be-accepted",
    }

    response, exit_code = _run_wine_payload(payload)

    assert exit_code == 2
    assert response == {
        "ok": False,
        "code": "invalid_request",
        "message": "broker discovery request is invalid",
    }


def test_linux_payload_resolves_windows_pids_inside_backend(tmp_path: Path) -> None:
    terminal_path = terminal(tmp_path)
    backend = FakeWineWin32Backend(
        terminal_path,
        pids=(710,),
        responses={"broker": ("Broker-Live",)},
    )
    clock = FakeClock()
    payload = {
        "terminal_path": str(terminal_path),
        "expected_server": "Broker-Live",
        "queries": ["broker"],
        "timeout_seconds": 10,
    }

    response, exit_code = _run_wine_autopid_payload(
        payload,
        backend=backend,
        adapter=adapter_for(backend, clock),
    )

    assert exit_code == 0
    assert response == {
        "ok": True,
        "code": "ok",
        "server": "Broker-Live",
        "query_index": 0,
    }


def test_linux_payload_rejects_external_candidate_pids(tmp_path: Path) -> None:
    terminal_path = terminal(tmp_path)
    backend = FakeWineWin32Backend(terminal_path)
    payload = {
        "terminal_path": str(terminal_path),
        "candidate_pids": [400],
        "expected_server": "Broker-Live",
        "queries": ["broker"],
    }

    response, exit_code = _run_wine_autopid_payload(payload, backend=backend)

    assert exit_code == 2
    assert response["code"] == "invalid_request"


def test_linux_payload_fails_when_win32_finds_no_exact_terminal(
    tmp_path: Path,
) -> None:
    terminal_path = terminal(tmp_path)
    backend = FakeWineWin32Backend(terminal_path, pids=())
    payload = {
        "terminal_path": str(terminal_path),
        "expected_server": "Broker-Live",
        "queries": ["broker"],
    }

    response, exit_code = _run_wine_autopid_payload(payload, backend=backend)

    assert exit_code == 6
    assert response["code"] == "terminal_not_found"


def test_linux_payload_fails_on_multiple_exact_win32_processes(
    tmp_path: Path,
) -> None:
    terminal_path = terminal(tmp_path)
    backend = FakeWineWin32Backend(
        terminal_path, pids=(710, 711), windows=(900, 901)
    )
    payload = {
        "terminal_path": str(terminal_path),
        "expected_server": "Broker-Live",
        "queries": ["broker"],
    }

    response, exit_code = _run_wine_autopid_payload(payload, backend=backend)

    assert exit_code == 6
    assert response["code"] == "terminal_ambiguous"


def test_owner_data_rows_are_probed_by_next_combo_and_back(
    tmp_path: Path,
) -> None:
    terminal_path = terminal(tmp_path)
    backend = FakeWineWin32Backend(
        terminal_path,
        responses={
            "first": ("OtherBroker-Live",),
            "second": ("FPMTrading-Demo", "FPMTrading-Live"),
        },
    )
    clock = FakeClock()

    result = WindowsMt5BrokerDiscovery(adapter_for(backend, clock)).discover(
        terminal_path=terminal_path,
        candidate_pids=(400,),
        expected_server="FPMTrading-Live",
        queries=("first", "second"),
        timeout_seconds=10,
    )

    assert result == BrokerDiscoveryResult(
        server="FPMTrading-Live", query_index=1
    )
    assert [value for action, value in backend.actions if action == "select_list_item"] == [
        0,
        0,
        1,
        1,
    ]
    assert [value for action, value in backend.actions if action == "get_text"] == [
        "OtherBroker-Live",
        "FPMTrading-Demo",
        "FPMTrading-Live",
        "FPMTrading-Live",
    ]
    assert backend.open[900] is False
    assert backend.actions[-1] == ("click", 2)


def test_matching_is_exact_after_safe_unicode_normalization(
    tmp_path: Path,
) -> None:
    terminal_path = terminal(tmp_path)
    backend = FakeWineWin32Backend(
        terminal_path,
        responses={"FPM": ("  ＦＰＭTrading-Live  ",)},
    )
    clock = FakeClock()

    result = WindowsMt5BrokerDiscovery(adapter_for(backend, clock)).discover(
        terminal_path=terminal_path,
        candidate_pids=(400,),
        expected_server="FPMTrading-Live",
        queries=("FPM",),
        timeout_seconds=10,
    )

    assert result.server == "FPMTrading-Live"
    assert backend.open[900] is False


def test_duplicate_canonical_server_rows_fail_closed(tmp_path: Path) -> None:
    terminal_path = terminal(tmp_path)
    backend = FakeWineWin32Backend(
        terminal_path,
        responses={"broker": ("Broker-Live", "broker-live")},
    )
    clock = FakeClock()

    with pytest.raises(BrokerDiscoveryError) as captured:
        WindowsMt5BrokerDiscovery(adapter_for(backend, clock)).discover(
            terminal_path=terminal_path,
            candidate_pids=(400,),
            expected_server="Broker-Live",
            queries=("broker",),
            timeout_seconds=10,
        )

    assert captured.value.code == "ambiguous_exact_match"
    assert "Broker" not in str(captured.value)
    assert backend.open[900] is False


def test_no_exact_server_fails_closed_and_cancels_wizard(tmp_path: Path) -> None:
    terminal_path = terminal(tmp_path)
    backend = FakeWineWin32Backend(
        terminal_path,
        responses={"broker": ("Broker-Demo",)},
    )
    clock = FakeClock()

    with pytest.raises(BrokerDiscoveryError) as captured:
        WindowsMt5BrokerDiscovery(adapter_for(backend, clock)).discover(
            terminal_path=terminal_path,
            candidate_pids=(400,),
            expected_server="Broker-Live",
            queries=("broker",),
            timeout_seconds=10,
        )

    assert captured.value.code == "no_exact_match"
    assert backend.open[900] is False


def test_final_combo_is_reverified_before_success(tmp_path: Path) -> None:
    terminal_path = terminal(tmp_path)
    backend = FakeWineWin32Backend(
        terminal_path,
        responses={"broker": ("Broker-Live",)},
    )
    backend.override_on_next_number = 2
    backend.final_combo_override = "Broker-Demo"
    clock = FakeClock()

    with pytest.raises(BrokerDiscoveryError) as captured:
        WindowsMt5BrokerDiscovery(adapter_for(backend, clock)).discover(
            terminal_path=terminal_path,
            candidate_pids=(400,),
            expected_server="Broker-Live",
            queries=("broker",),
            timeout_seconds=10,
        )

    assert captured.value.code == "ui_unknown"
    assert "Broker" not in str(captured.value)
    assert backend.open[900] is False


def test_process_image_must_match_terminal_path_exactly(tmp_path: Path) -> None:
    terminal_path = terminal(tmp_path)
    other_path = tmp_path / "Other" / "terminal64.exe"
    backend = FakeWineWin32Backend(other_path)
    clock = FakeClock()

    with pytest.raises(BrokerDiscoveryError) as captured:
        adapter_for(backend, clock).open_session(
            terminal_path=terminal_path,
            candidate_pids=(400,),
            timeout_seconds=1,
        )

    assert captured.value.code == "terminal_not_found"
    assert str(terminal_path) not in str(captured.value)


def test_exact_process_with_unknown_controls_fails_as_unknown_ui(
    tmp_path: Path,
) -> None:
    terminal_path = terminal(tmp_path)
    backend = FakeWineWin32Backend(terminal_path, recognized=False)
    clock = FakeClock()

    with pytest.raises(BrokerDiscoveryError) as captured:
        adapter_for(backend, clock).open_session(
            terminal_path=terminal_path,
            candidate_pids=(400,),
            timeout_seconds=1,
        )

    assert captured.value.code == "ui_unknown"


def test_cross_process_child_control_fails_closed(tmp_path: Path) -> None:
    terminal_path = terminal(tmp_path)

    class CrossProcessControlBackend(FakeWineWin32Backend):
        def window_pid(self, hwnd: int) -> int:
            if hwnd == self._child(900, 1):
                return 999
            return super().window_pid(hwnd)

    backend = CrossProcessControlBackend(terminal_path)
    clock = FakeClock()

    with pytest.raises(BrokerDiscoveryError) as captured:
        adapter_for(backend, clock).open_session(
            terminal_path=terminal_path,
            candidate_pids=(400,),
            timeout_seconds=1,
        )

    assert captured.value.code == "ui_unknown"


def test_process_image_is_rechecked_before_control_actions(tmp_path: Path) -> None:
    terminal_path = terminal(tmp_path)
    backend = FakeWineWin32Backend(terminal_path)
    clock = FakeClock()
    session = adapter_for(backend, clock).open_session(
        terminal_path=terminal_path,
        candidate_pids=(400,),
        timeout_seconds=1,
    )
    backend.terminal_path = str(tmp_path / "replaced" / "terminal64.exe")

    with pytest.raises(BrokerDiscoveryError) as captured:
        session.open_account()

    assert captured.value.code == "ui_unknown"


def test_multiple_exact_discovery_windows_are_ambiguous(tmp_path: Path) -> None:
    terminal_path = terminal(tmp_path)
    backend = FakeWineWin32Backend(terminal_path, windows=(900, 901))
    clock = FakeClock()

    with pytest.raises(BrokerDiscoveryError) as captured:
        adapter_for(backend, clock).open_session(
            terminal_path=terminal_path,
            candidate_pids=(400,),
            timeout_seconds=1,
        )

    assert captured.value.code == "terminal_ambiguous"


def test_duplicate_enumeration_of_same_hwnd_is_not_ambiguous(tmp_path: Path) -> None:
    terminal_path = terminal(tmp_path)

    class DuplicateWindowBackend(FakeWineWin32Backend):
        def top_level_windows(self, pid: int) -> Sequence[int]:
            windows = tuple(super().top_level_windows(pid))
            return windows + windows

    backend = DuplicateWindowBackend(
        terminal_path,
        responses={"broker": ("Broker-Live",)},
    )
    clock = FakeClock()

    result = WindowsMt5BrokerDiscovery(adapter_for(backend, clock)).discover(
        terminal_path=terminal_path,
        candidate_pids=(400,),
        expected_server="Broker-Live",
        queries=("broker",),
        timeout_seconds=10,
    )

    assert result.server == "Broker-Live"


def test_hung_find_message_maps_to_sanitized_timeout(tmp_path: Path) -> None:
    terminal_path = terminal(tmp_path)
    backend = FakeWineWin32Backend(
        terminal_path,
        responses={"private-query": ("Broker-Live",)},
    )
    backend.timeout_on_find = True
    clock = FakeClock()

    with pytest.raises(BrokerDiscoveryError) as captured:
        WindowsMt5BrokerDiscovery(adapter_for(backend, clock)).discover(
            terminal_path=terminal_path,
            candidate_pids=(400,),
            expected_server="Broker-Live",
            queries=("private-query",),
            timeout_seconds=10,
        )

    assert captured.value.code == "timeout"
    assert "private" not in str(captured.value)
    assert backend.open[900] is False


def test_cancel_must_close_the_wizard_deterministically(tmp_path: Path) -> None:
    terminal_path = terminal(tmp_path)
    backend = FakeWineWin32Backend(
        terminal_path,
        responses={"broker": ("Broker-Live",)},
    )
    backend.cancel_closes = False
    clock = FakeClock()

    with pytest.raises(BrokerDiscoveryError) as captured:
        WindowsMt5BrokerDiscovery(adapter_for(backend, clock)).discover(
            terminal_path=terminal_path,
            candidate_pids=(400,),
            expected_server="Broker-Live",
            queries=("broker",),
            timeout_seconds=1,
        )

    assert captured.value.code == "timeout"
    assert backend.open[900] is True


@pytest.mark.skipif(os.name == "nt", reason="non-Windows guard only")
def test_default_backend_fails_safely_outside_windows(tmp_path: Path) -> None:
    terminal_path = terminal(tmp_path)

    with pytest.raises(BrokerDiscoveryError) as captured:
        WineWin32Mt5BrokerDiscoveryAdapter().open_session(
            terminal_path=terminal_path,
            candidate_pids=(400,),
            timeout_seconds=1,
        )

    assert captured.value.code == "ui_unknown"
