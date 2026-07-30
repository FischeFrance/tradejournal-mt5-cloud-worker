from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import windows_agent.broker_wizard_ui
from windows_agent.broker_wizard_ui import (
    _broker_result_labels,
    _click_broker_result,
    _click_wizard_button,
    _enable_existing_account_server_menu,
    _ordered_broker_result_indices,
    _read_request,
    _recover_broker_results,
    _resume_broker_results,
    _search_broker_results,
    _server_names_from_dialog,
    _set_search_text,
    _wait_for_broker_page,
    _wait_for_server_page,
)


def test_helper_supports_direct_script_execution() -> None:
    helper = Path(windows_agent.broker_wizard_ui.__file__).resolve()

    completed = subprocess.run(
        [sys.executable, str(helper)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 2
    assert "ImportError" not in completed.stderr


class _RecordingEdit:
    def __init__(self) -> None:
        self.values: list[str] = []

    def set_edit_text(self, value: str) -> None:
        self.values.append(value)

    def type_keys(self, *_args: object, **_kwargs: object) -> None:
        raise AssertionError("keyboard synthesis must not be used")

    def is_visible(self) -> bool:
        return True

    def is_enabled(self) -> bool:
        return True


def test_broker_search_writes_native_edit_without_keyboard_synthesis() -> None:
    edit = _RecordingEdit()

    _set_search_text(edit, "Pepperstone")

    assert edit.values == ["Pepperstone"]


@dataclass(frozen=True)
class _Rectangle:
    left: int
    top: int
    right: int
    bottom: int


class _ListItem:
    def __init__(self, text: str = "") -> None:
        self._text = text

    def rectangle(self) -> _Rectangle:
        return _Rectangle(left=10, top=20, right=110, bottom=60)

    def text(self) -> str:
        return self._text

    def select(self) -> None:
        raise AssertionError("owner-drawn row must be clicked by rectangle")


class _BrokerList:
    def __init__(self) -> None:
        self.clicks: list[tuple[str, tuple[int, int]]] = []

    def get_item(
        self,
        index: int,
        _subitem_index: int = 0,
    ) -> _ListItem:
        assert index == 7
        return _ListItem()

    def click(self, *, button: str, coords: tuple[int, int]) -> None:
        self.clicks.append((button, coords))

    def is_visible(self) -> bool:
        return True

    def is_enabled(self) -> bool:
        return True


def test_owner_drawn_broker_result_is_clicked_by_item_rectangle() -> None:
    broker_list = _BrokerList()

    _click_broker_result(broker_list, 7)

    assert broker_list.clicks == [("left", (60, 40))]


class _LabeledBrokerList:
    def __init__(self, rows: list[tuple[str, str]]) -> None:
        self.rows = rows

    def get_item(
        self,
        index: int,
        subitem_index: int = 0,
    ) -> _ListItem:
        return _ListItem(self.rows[index][subitem_index])


def test_broker_result_labels_read_both_visible_columns() -> None:
    broker_list = _LabeledBrokerList(
        [("Pepperstone", "PepperstoneUK")]
    )

    assert _broker_result_labels(broker_list, 0) == (
        "Pepperstone",
        "PepperstoneUK",
    )


def test_broker_rows_keep_native_order_and_are_not_scanned_twice() -> None:
    tested: set[tuple[str, ...]] = set()
    first_results = _LabeledBrokerList(
        [
            ("Pepperstone", "PepperstoneEU"),
            ("Pepperstone", "PepperstoneUK"),
            ("Pepperstone", "PepperstoneSC"),
        ]
    )

    first_order, first_readable = _ordered_broker_result_indices(
        first_results,
        3,
        tested_brokers=tested,
    )
    repeated_order, repeated_readable = _ordered_broker_result_indices(
        first_results,
        3,
        tested_brokers=tested,
    )

    assert first_order == [0, 1, 2]
    assert first_readable is True
    assert repeated_order == []
    assert repeated_readable is True


def test_unreadable_broker_rows_disable_safe_cross_query_deduplication() -> None:
    tested: set[tuple[str, ...]] = set()
    broker_list = _LabeledBrokerList(
        [
            ("", ""),
            ("Pepperstone", "PepperstoneUK"),
        ]
    )

    order, all_rows_readable = _ordered_broker_result_indices(
        broker_list,
        2,
        tested_brokers=tested,
    )

    assert order == [0, 1]
    assert all_rows_readable is False


def test_request_deduplicates_equivalent_search_terms(tmp_path) -> None:
    terminal = tmp_path / "terminal64.exe"
    terminal.write_bytes(b"MZ")
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "run_id": "12345678-1234-4234-8234-123456789abc",
                "terminal_path": str(terminal),
                "expected_server_name": "PepperstoneUK-Live",
                "suggested_broker_label": "Pepperstone",
                "search_terms": [
                    "PepperstoneUK",
                    "Pepperstone UK",
                    "Pepperstone",
                ],
                "timeout_seconds": 180,
            }
        ),
        encoding="utf-8",
    )

    assert _read_request(request)["search_terms"] == [
        "PepperstoneUK",
        "Pepperstone",
    ]


class _Combo:
    def __init__(self, values: list[object], *, enabled: bool = True) -> None:
        self.values = values
        self.enabled = enabled
        self.dropped = False
        self.drop_transitions: list[bool] = []

    def item_texts(self) -> list[object]:
        return self.values

    def is_visible(self) -> bool:
        return True

    def is_enabled(self) -> bool:
        return self.enabled

    def send_message(
        self,
        message: int,
        wparam: int,
        _lparam: int,
    ) -> int:
        if message == 0x014F:
            self.dropped = bool(wparam)
            self.drop_transitions.append(self.dropped)
            return 1
        assert message == 0x0157
        return int(self.dropped)


class _ExistingAccountButton:
    def __init__(self, combo: _Combo) -> None:
        self.combo = combo
        self.click_count = 0

    def click(self) -> None:
        self.click_count += 1
        self.combo.enabled = True

    def is_visible(self) -> bool:
        return True

    def is_enabled(self) -> bool:
        return True


class _ServerDialog:
    def __init__(self, *, enabled: bool = True) -> None:
        self.combo = _Combo(
            [
                "PepperstoneUK-Live",
                "",
                "../unsafe",
                "PepperstoneUK-Demo",
                "pepperstoneuk-live",
                123,
            ],
            enabled=enabled,
        )
        self.existing_account = _ExistingAccountButton(self.combo)

    def child_window(self, **criteria: object) -> _ControlSpecification:
        if criteria == {"control_id": 10469, "class_name": "Button"}:
            return _ControlSpecification(self.existing_account)
        assert criteria == {
            "control_id": 10139,
            "class_name": "ComboBox",
        }
        return _ControlSpecification(self.combo)


def test_server_names_are_validated_deduplicated_and_sorted() -> None:
    dialog = _ServerDialog()

    assert _server_names_from_dialog(dialog) == [
        "PepperstoneUK-Demo",
        "PepperstoneUK-Live",
    ]
    assert dialog.combo.drop_transitions == [True, False]


def test_existing_account_mode_enables_server_menu_before_census(
    monkeypatch: object,
) -> None:
    monkeypatch.setattr(
        "windows_agent.broker_wizard_ui.time.sleep",
        lambda _seconds: None,
    )
    dialog = _ServerDialog(enabled=False)

    _enable_existing_account_server_menu(dialog, 10)

    assert dialog.existing_account.click_count == 1
    assert dialog.combo.is_enabled()
    assert _server_names_from_dialog(dialog) == [
        "PepperstoneUK-Demo",
        "PepperstoneUK-Live",
    ]


class _ControlSpecification:
    def __init__(self, wrapper: object) -> None:
        self.wrapper = wrapper
        self.wait_calls: list[tuple[str, float]] = []

    def wait(self, state: str, *, timeout: float) -> None:
        self.wait_calls.append((state, timeout))

    def wrapper_object(self) -> object:
        return self.wrapper


class _Button:
    def __init__(self) -> None:
        self.click_count = 0

    def click(self) -> None:
        self.click_count += 1

    def is_visible(self) -> bool:
        return True

    def is_enabled(self) -> bool:
        return True


class _WizardPage:
    def __init__(self) -> None:
        self.broker_list = _BrokerList()
        self.brokers = _ControlSpecification(self.broker_list)
        self.button = _Button()
        self.button_spec = _ControlSpecification(self.button)
        self.edit = _RecordingEdit()
        self.edit_spec = _ControlSpecification(self.edit)
        self.server_dialog = _ServerDialog()

    def child_window(self, **criteria: object) -> _ControlSpecification:
        if criteria in (
            {"control_id": 12323, "class_name": "Button"},
            {"control_id": 12324, "class_name": "Button"},
        ):
            return self.button_spec
        if criteria == {"control_id": 10815, "class_name": "Button"}:
            return self.button_spec
        if criteria == {"control_id": 10814, "class_name": "Edit"}:
            return self.edit_spec
        if criteria == {"control_id": 10469, "class_name": "Button"}:
            return _ControlSpecification(
                self.server_dialog.existing_account
            )
        if criteria == {"control_id": 10139, "class_name": "ComboBox"}:
            return _ControlSpecification(self.server_dialog.combo)
        assert criteria == {
            "control_id": 10729,
            "class_name": "SysListView32",
        }
        return self.brokers


class _Application:
    def __init__(self) -> None:
        self.page = _WizardPage()

    def window(self, *, title_re: str) -> _WizardPage:
        assert title_re == r"Open an Account.*"
        return self.page


def test_wizard_waits_for_controls_unique_to_each_page(
    monkeypatch: object,
) -> None:
    monkeypatch.setattr(
        "windows_agent.broker_wizard_ui.time.sleep",
        lambda _seconds: None,
    )
    application = _Application()

    server_page = _wait_for_server_page(application, 12)
    broker_page, broker_list = _wait_for_broker_page(application, 13)
    _click_wizard_button(application, 12324, 14)
    searched_page, searched_list = _search_broker_results(
        application,
        "Pepperstone",
        15,
    )

    assert server_page is application.page
    assert broker_page is application.page
    assert broker_list is application.page.broker_list
    assert searched_page is application.page
    assert searched_list is application.page.broker_list
    assert application.page.edit.values == ["", "Pepperstone"]
    assert application.page.brokers.wait_calls == []
    assert application.page.button_spec.wait_calls == []
    assert application.page.button.click_count == 2


def test_candidate_recovery_reuses_visible_results_before_next_index(
    monkeypatch: object,
) -> None:
    monkeypatch.setattr(
        "windows_agent.broker_wizard_ui.time.sleep",
        lambda _seconds: None,
    )
    application = _Application()

    page, broker_list = _recover_broker_results(
        application,
        "Pepperstone",
        15,
    )

    assert page is application.page
    assert broker_list is application.page.broker_list
    assert application.page.button.click_count == 1
    assert application.page.edit.values == []


def test_candidate_recovery_retries_back_only_until_results_are_visible(
    monkeypatch: object,
) -> None:
    monkeypatch.setattr(
        "windows_agent.broker_wizard_ui.time.sleep",
        lambda _seconds: None,
    )
    application = _Application()
    waits = {"count": 0}

    def delayed_results(
        _application: object,
        _timeout: float,
    ) -> tuple[object, object]:
        waits["count"] += 1
        if waits["count"] == 1:
            from windows_agent.broker_wizard_ui import WizardUiError

            raise WizardUiError("driver_failure")
        return application.page, application.page.broker_list

    monkeypatch.setattr(
        "windows_agent.broker_wizard_ui._wait_for_broker_page",
        delayed_results,
    )

    page, broker_list = _recover_broker_results(
        application,
        "Pepperstone",
        15,
    )

    assert page is application.page
    assert broker_list is application.page.broker_list
    assert application.page.button.click_count == 2
    assert waits["count"] == 2


def test_unavailable_candidate_reuses_visible_results_without_new_search(
    monkeypatch: object,
) -> None:
    monkeypatch.setattr(
        "windows_agent.broker_wizard_ui.time.sleep",
        lambda _seconds: None,
    )
    application = _Application()

    page, broker_list = _resume_broker_results(
        application,
        "Pepperstone",
        15,
    )

    assert page is application.page
    assert broker_list is application.page.broker_list
    assert application.page.button.click_count == 0
    assert application.page.edit.values == []
