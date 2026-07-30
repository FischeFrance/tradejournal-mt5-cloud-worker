from __future__ import annotations

"""Interactive-session helper for the official MT5 “Open an Account” wizard.

The request contains no credentials.  Output is a small sanitized JSON record;
no screenshots, UI tree, command line, account number, token or password are
persisted.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping
from uuid import UUID

if __package__:
    from .provisioning.process_manager import ProcessManager
else:
    # The scheduled task executes this file by path rather than with ``-m``.
    # Add only the repository/release root so the same package import works in
    # both service tests and the dedicated interactive Windows session.
    package_root = str(Path(__file__).resolve().parent.parent)
    if package_root not in sys.path:
        sys.path.insert(0, package_root)
    from windows_agent.provisioning.process_manager import ProcessManager


_LABEL = re.compile(r"^[A-Za-z0-9&'()._ /+-]{1,128}$")
_SERVER = re.compile(r"^[A-Za-z0-9._ -]{1,128}$")
_REQUEST_FIELDS = {
    "schema_version",
    "run_id",
    "terminal_path",
    "expected_server_name",
    "suggested_broker_label",
    "search_terms",
    "timeout_seconds",
}
_MAX_BROKER_RESULTS = 50
_BACK_BUTTON_ID = 12323
_CB_GETDROPPEDSTATE = 0x0157
_CB_SHOWDROPDOWN = 0x014F
_EXISTING_ACCOUNT_BUTTON_ID = 10469
_FIND_BUTTON_ID = 10815
_NEXT_BUTTON_ID = 12324
_SEARCH_EDIT_ID = 10814
_SERVER_COMBO_ID = 10139


class WizardUiError(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _uuid4(value: Any) -> str:
    if not isinstance(value, str):
        raise WizardUiError("invalid_request")
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError) as exc:
        raise WizardUiError("invalid_request") from exc
    if parsed.version != 4 or str(parsed) != value.lower():
        raise WizardUiError("invalid_request")
    return str(parsed)


def _label_key(value: str) -> str:
    return "".join(
        character
        for character in value.casefold()
        if character.isalnum()
    )


def _read_request(path: Path) -> dict[str, Any]:
    try:
        if (
            path.is_symlink()
            or not path.is_file()
            or path.stat().st_size <= 0
            or path.stat().st_size > 32 * 1024
        ):
            raise WizardUiError("invalid_request")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WizardUiError("invalid_request") from exc
    if not isinstance(value, Mapping) or set(value) != _REQUEST_FIELDS:
        raise WizardUiError("invalid_request")
    if value["schema_version"] != 1:
        raise WizardUiError("invalid_request")
    run_id = _uuid4(value["run_id"])
    terminal = Path(str(value["terminal_path"])).resolve()
    expected = value["expected_server_name"]
    suggested = value["suggested_broker_label"]
    terms = value["search_terms"]
    timeout = value["timeout_seconds"]
    if (
        terminal.name.casefold() != "terminal64.exe"
        or terminal.is_symlink()
        or not terminal.is_file()
        or not isinstance(expected, str)
        or not _SERVER.fullmatch(expected)
        or not isinstance(suggested, str)
        or not _LABEL.fullmatch(suggested)
        or not isinstance(terms, list)
        or not 1 <= len(terms) <= 3
        or any(not isinstance(term, str) or not _LABEL.fullmatch(term) for term in terms)
        or not isinstance(timeout, int)
        or isinstance(timeout, bool)
        or not 30 <= timeout <= 300
    ):
        raise WizardUiError("invalid_request")
    unique_terms: list[str] = []
    term_keys: set[str] = set()
    for term in terms:
        normalized = term.strip()
        key = _label_key(normalized)
        if key not in term_keys:
            unique_terms.append(normalized)
            term_keys.add(key)
    return {
        "run_id": run_id,
        "terminal": terminal,
        "expected_server_name": expected,
        "suggested_broker_label": suggested,
        "search_terms": unique_terms,
        "timeout_seconds": timeout,
    }


def _write_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _same_executable(left: str | None, right: Path) -> bool:
    if not left:
        return False
    try:
        return Path(left).resolve() == right.resolve()
    except OSError:
        return False


def _set_search_text(edit: Any, value: str) -> None:
    """Set the broker query without synthesizing keyboard input.

    Scheduled interactive tasks can retain a disconnected desktop where
    ``type_keys`` fails even though the native Edit control is writable.
    ``set_edit_text`` targets that control directly and does not depend on
    keyboard focus or the active RDP client.
    """

    edit.set_edit_text(value)


def _click_broker_result(broker_list: Any, index: int) -> None:
    """Choose an owner-drawn broker row without relying on readable text.

    MT5 exposes the broker results as a native ListView whose row labels can
    be blank to accessibility clients.  Clicking the row by its native item
    rectangle still sends the control's normal selection notification.
    """

    rectangle = broker_list.get_item(index).rectangle()
    if rectangle.right <= rectangle.left or rectangle.bottom <= rectangle.top:
        raise WizardUiError("driver_failure")
    broker_list.click(
        button="left",
        coords=(
            (rectangle.left + rectangle.right) // 2,
            (rectangle.top + rectangle.bottom) // 2,
        ),
    )


def _broker_result_labels(
    broker_list: Any,
    index: int,
) -> tuple[str, ...]:
    """Read the two visible native ListView labels for one broker row."""

    labels: list[str] = []
    keys: set[str] = set()
    for subitem_index in range(2):
        try:
            raw = broker_list.get_item(index, subitem_index).text()
        except Exception:
            continue
        if not isinstance(raw, str):
            continue
        normalized = raw.strip()
        if not normalized or not _LABEL.fullmatch(normalized):
            continue
        key = _label_key(normalized)
        if key and key not in keys:
            labels.append(normalized)
            keys.add(key)
    return tuple(labels)


def _ordered_broker_result_indices(
    broker_list: Any,
    result_count: int,
    *,
    tested_brokers: set[tuple[str, ...]],
) -> tuple[list[int], bool]:
    """Return unread rows in their native MT5 order.

    The returned boolean is false when at least one row has no readable native
    labels. In that situation another broad query cannot be deduplicated
    safely, so the caller must not repeat the complete scan.
    """

    ordered: list[int] = []
    all_rows_readable = True
    for index in range(min(result_count, _MAX_BROKER_RESULTS)):
        labels = _broker_result_labels(broker_list, index)
        fingerprint = tuple(_label_key(label) for label in labels)
        if not fingerprint:
            all_rows_readable = False
            ordered.append(index)
            continue
        if fingerprint in tested_brokers:
            continue
        tested_brokers.add(fingerprint)
        ordered.append(index)
    return ordered, all_rows_readable


def _selected_broker_label(
    labels: tuple[str, ...],
    suggested_label: str,
) -> str:
    """Persist an observed UI label, never the AI suggestion by itself."""

    if not labels:
        raise WizardUiError("driver_failure")
    suggested_key = _label_key(suggested_label)
    for label in labels:
        if _label_key(label) == suggested_key:
            return label
    return labels[0]


def _set_server_menu_open(
    combo: Any,
    *,
    opened: bool,
    timeout: float,
) -> None:
    """Change and verify the native ComboBox dropped state."""

    try:
        combo.send_message(
            _CB_SHOWDROPDOWN,
            1 if opened else 0,
            0,
        )
    except Exception as exc:
        raise WizardUiError("driver_failure") from exc
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            is_open = bool(
                combo.send_message(_CB_GETDROPPEDSTATE, 0, 0)
            )
            if is_open == opened:
                return
        except Exception:
            pass
        time.sleep(0.1)
    raise WizardUiError("driver_failure")


def _server_names_from_dialog(
    server_dialog: Any,
    timeout: float = 5,
) -> list[str]:
    """Open the enabled Server menu and return its validated entries."""

    try:
        combo = server_dialog.child_window(
            control_id=_SERVER_COMBO_ID,
            class_name="ComboBox",
        ).wrapper_object()
        if not combo.is_visible() or not combo.is_enabled():
            raise WizardUiError("driver_failure")
        menu_opened = False
        try:
            _set_server_menu_open(combo, opened=True, timeout=timeout)
            menu_opened = True
            previous_items: tuple[object, ...] | None = None
            deadline = time.monotonic() + timeout
            items: tuple[object, ...] | None = None
            while time.monotonic() < deadline:
                current_items = tuple(combo.item_texts())
                if current_items == previous_items:
                    items = current_items
                    break
                previous_items = current_items
                time.sleep(0.1)
            if items is None:
                raise WizardUiError("driver_failure")
        finally:
            if menu_opened:
                _set_server_menu_open(
                    combo,
                    opened=False,
                    timeout=timeout,
                )
        names: dict[str, str] = {}
        for item in items:
            if isinstance(item, str) and _SERVER.fullmatch(item.strip()):
                normalized = item.strip()
                names.setdefault(normalized.casefold(), normalized)
    except WizardUiError:
        raise
    except Exception as exc:
        raise WizardUiError("driver_failure") from exc
    return sorted(names.values(), key=str.casefold)


def _enable_existing_account_server_menu(
    server_dialog: Any,
    timeout: float,
) -> None:
    """Select existing-account login and wait for its server menu.

    MT5 initially shows the Server ComboBox while leaving it disabled.  Its
    contents must not be treated as a usable census until the official
    "Connect with an existing trade account" radio button has been selected
    and the ComboBox has become enabled.
    """

    deadline = time.monotonic() + timeout
    clicked = False
    while time.monotonic() < deadline:
        try:
            existing_account = server_dialog.child_window(
                control_id=_EXISTING_ACCOUNT_BUTTON_ID,
                class_name="Button",
            ).wrapper_object()
            combo = server_dialog.child_window(
                control_id=_SERVER_COMBO_ID,
                class_name="ComboBox",
            ).wrapper_object()
            if (
                not clicked
                and existing_account.is_visible()
                and existing_account.is_enabled()
            ):
                existing_account.click()
                clicked = True
            if combo.is_visible() and combo.is_enabled():
                return
        except Exception:
            pass
        time.sleep(0.1)
    raise WizardUiError("driver_failure")


def _wait_for_server_page(
    application: Any,
    timeout: float,
) -> Any:
    """Wait for a control unique to the server-selection wizard page.

    Every page of the MT5 wizard keeps the same window title.  Waiting only
    for that window therefore races the page transition after ``Next``.
    """

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        dialog = application.window(title_re=r"Open an Account.*")
        try:
            existing_account = dialog.child_window(
                control_id=_EXISTING_ACCOUNT_BUTTON_ID,
                class_name="Button",
            ).wrapper_object()
            combo = dialog.child_window(
                control_id=_SERVER_COMBO_ID,
                class_name="ComboBox",
            ).wrapper_object()
            if (
                existing_account.is_visible()
                and existing_account.is_enabled()
                and combo.is_visible()
            ):
                return dialog
        except Exception:
            pass
        time.sleep(0.1)
    raise WizardUiError("driver_failure")


def _wait_for_broker_page(
    application: Any,
    timeout: float,
) -> tuple[Any, Any]:
    """Wait for the visible broker page, ignoring retained stale controls."""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        dialog = application.window(title_re=r"Open an Account.*")
        try:
            edit = dialog.child_window(
                control_id=_SEARCH_EDIT_ID,
                class_name="Edit",
            ).wrapper_object()
            broker_list = dialog.child_window(
                control_id=10729,
                class_name="SysListView32",
            ).wrapper_object()
            if (
                edit.is_visible()
                and edit.is_enabled()
                and broker_list.is_visible()
                and broker_list.is_enabled()
            ):
                return dialog, broker_list
        except Exception:
            pass
        time.sleep(0.1)
    raise WizardUiError("driver_failure")


def _search_broker_results(
    application: Any,
    term: str,
    timeout: float,
) -> tuple[Any, Any]:
    """Submit a fresh broker query and return its rebuilt result list."""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        dialog = application.window(title_re=r"Open an Account.*")
        try:
            edit = dialog.child_window(
                control_id=_SEARCH_EDIT_ID,
                class_name="Edit",
            ).wrapper_object()
            find_button = dialog.child_window(
                control_id=_FIND_BUTTON_ID,
                class_name="Button",
            ).wrapper_object()
            if (
                edit.is_visible()
                and edit.is_enabled()
                and find_button.is_visible()
                and find_button.is_enabled()
            ):
                _set_search_text(edit, "")
                _set_search_text(edit, term)
                find_button.click()
                time.sleep(4)
                return _wait_for_broker_page(
                    application,
                    max(0.1, deadline - time.monotonic()),
                )
        except Exception:
            pass
        time.sleep(0.1)
    raise WizardUiError("driver_failure")


def _recover_broker_results(
    application: Any,
    term: str,
    timeout: float,
    server_dialog: Any | None = None,
) -> tuple[Any, Any]:
    """Return to the visible result list, rebuilding it only as fallback."""

    for _attempt in range(2):
        try:
            if server_dialog is None:
                _click_wizard_button(
                    application,
                    _BACK_BUTTON_ID,
                    min(timeout, 5),
                )
            else:
                _click_dialog_button(
                    server_dialog,
                    _BACK_BUTTON_ID,
                    min(timeout, 5),
                )
        except WizardUiError:
            pass
        try:
            return _wait_for_broker_page(
                application,
                min(timeout, 5),
            )
        except WizardUiError:
            continue
    return _search_broker_results(application, term, timeout)


def _resume_broker_results(
    application: Any,
    term: str,
    timeout: float,
) -> tuple[Any, Any]:
    """Reuse visible results or rebuild them if MT5 discarded the page."""

    try:
        return _wait_for_broker_page(
            application,
            min(timeout, 10),
        )
    except WizardUiError:
        return _search_broker_results(application, term, timeout)


def _click_dialog_button(
    dialog: Any,
    control_id: int,
    timeout: float,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            button = dialog.child_window(
                control_id=control_id,
                class_name="Button",
            ).wrapper_object()
            if button.is_visible() and button.is_enabled():
                button.click()
                return
        except Exception:
            pass
        time.sleep(0.1)
    raise WizardUiError("driver_failure")


def _click_wizard_button(
    application: Any,
    control_id: int,
    timeout: float,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        dialog = application.window(title_re=r"Open an Account.*")
        try:
            button = dialog.child_window(
                control_id=control_id,
                class_name="Button",
            ).wrapper_object()
            if button.is_visible() and button.is_enabled():
                button.click()
                return
        except Exception:
            pass
        time.sleep(0.1)
    raise WizardUiError("driver_failure")


def _find_exact_terminal_pid(
    terminal: Path,
    launched_at: float,
    initial_pid: int,
    timeout: float,
) -> int:
    import psutil

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        matches: list[int] = []
        for process in psutil.process_iter(("pid", "exe", "create_time")):
            try:
                if (
                    _same_executable(process.info["exe"], terminal)
                    and float(process.info["create_time"] or 0) >= launched_at - 2
                ):
                    matches.append(int(process.info["pid"]))
            except (psutil.AccessDenied, psutil.NoSuchProcess, OSError, ValueError):
                continue
        matches = sorted(set(matches))
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise WizardUiError("terminal_process_ambiguous")
        try:
            process = psutil.Process(initial_pid)
            if _same_executable(process.exe(), terminal):
                return initial_pid
        except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
            pass
        time.sleep(0.25)
    raise WizardUiError("terminal_start_failed")


def _close_run_terminals(
    app: Any,
    terminal: Path,
    launched_at: float,
    initial_process: subprocess.Popen[bytes],
) -> bool:
    if app is not None:
        try:
            app.top_window().close()
        except Exception:
            pass
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        matches = _run_terminal_processes(terminal, launched_at)
        if not matches:
            break
        time.sleep(0.25)
    try:
        return ProcessManager.cleanup_path(terminal, timeout=15)
    except (OSError, RuntimeError, ValueError):
        return False


def _run_terminal_processes(
    terminal: Path,
    launched_at: float,
) -> list[Any]:
    import psutil

    matches: list[Any] = []
    for process in psutil.process_iter(("exe", "create_time")):
        try:
            if (
                _same_executable(process.info["exe"], terminal)
                and float(process.info["create_time"] or 0) >= launched_at - 2
            ):
                matches.append(process)
        except (psutil.AccessDenied, psutil.NoSuchProcess, OSError, ValueError):
            continue
    return matches


def _run(request: Mapping[str, Any]) -> dict[str, Any]:
    from pywinauto.application import Application
    from pywinauto.timings import TimeoutError as PwaTimeoutError

    terminal: Path = request["terminal"]
    expected: str = request["expected_server_name"]
    suggested: str = request["suggested_broker_label"]
    timeout = int(request["timeout_seconds"])
    launched_at = time.time()
    process = subprocess.Popen(
        [str(terminal), "/portable"],
        cwd=terminal.parent,
        close_fds=True,
    )
    pid = 0
    application = None
    failure_reason: str | None = None
    selected_broker: str | None = None
    selected_broker_labels: tuple[str, ...] = ()
    censused_servers: list[str] = []
    try:
        pid = _find_exact_terminal_pid(
            terminal,
            launched_at,
            process.pid,
            min(timeout, 30),
        )
        application = Application(backend="win32").connect(
            process=pid,
            timeout=min(timeout, 30),
        )
        try:
            dialog = application.window(title_re=r"Open an Account.*")
            dialog.wait("exists ready", timeout=min(timeout, 30))
        except PwaTimeoutError:
            main_window = application.top_window()
            try:
                main_window.menu_select("File->Open an Account")
            except Exception:
                main_window.set_focus()
                main_window.type_keys("%f", pause=0.2)
            dialog = application.window(title_re=r"Open an Account.*")
            try:
                dialog.wait("exists ready", timeout=min(timeout, 30))
            except PwaTimeoutError as exc:
                raise WizardUiError("wizard_not_found") from exc

        tested_brokers: set[tuple[str, ...]] = set()
        allow_fallback_search = True
        for term_index, term in enumerate(request["search_terms"]):
            if term_index > 0 and not allow_fallback_search:
                break
            attempt_started = time.monotonic()
            try:
                dialog, broker_list = _search_broker_results(
                    application,
                    term,
                    min(timeout, 30),
                )
            except Exception as exc:
                raise WizardUiError("driver_failure") from exc

            result_count = broker_list.item_count()
            if result_count < 0:
                raise WizardUiError("driver_failure")
            indices, all_rows_readable = _ordered_broker_result_indices(
                broker_list,
                result_count,
                tested_brokers=tested_brokers,
            )
            if result_count > 0 and not all_rows_readable:
                allow_fallback_search = False
            for index in indices:
                if time.monotonic() - attempt_started > timeout:
                    raise WizardUiError("timeout")
                server_dialog: Any | None = None
                try:
                    row_labels = _broker_result_labels(broker_list, index)
                    _click_broker_result(broker_list, index)
                except WizardUiError:
                    raise
                except Exception as exc:
                    raise WizardUiError("driver_failure") from exc
                try:
                    _click_wizard_button(
                        application,
                        _NEXT_BUTTON_ID,
                        min(timeout, 5),
                    )
                except WizardUiError:
                    dialog, broker_list = _resume_broker_results(
                        application,
                        term,
                        min(timeout, 30),
                    )
                    continue
                try:
                    # MT5 keeps stale controls from the broker page alive
                    # briefly after the native Next notification.  Let the
                    # page transition settle, select existing-account login,
                    # then read only the server menu enabled by that choice.
                    time.sleep(2)
                    server_dialog = _wait_for_server_page(
                        application,
                        min(timeout, 30),
                    )
                    _enable_existing_account_server_menu(
                        server_dialog,
                        min(timeout, 10),
                    )
                    items = _server_names_from_dialog(server_dialog)
                except PwaTimeoutError as exc:
                    raise WizardUiError("driver_failure") from exc
                except WizardUiError:
                    dialog, broker_list = _recover_broker_results(
                        application,
                        term,
                        min(timeout, 30),
                        server_dialog,
                    )
                    continue
                except Exception as exc:
                    raise WizardUiError("driver_failure") from exc

                if any(
                    item.casefold() == expected.casefold()
                    for item in items
                ):
                    selected_broker = _selected_broker_label(
                        row_labels,
                        suggested,
                    )
                    selected_broker_labels = row_labels
                    censused_servers = items
                    try:
                        server_dialog.child_window(
                            title="Cancel",
                            class_name="Button",
                        ).wrapper_object().click()
                    except Exception:
                        pass
                    break

                try:
                    dialog, broker_list = _recover_broker_results(
                        application,
                        term,
                        min(timeout, 30),
                        server_dialog,
                    )
                except PwaTimeoutError as exc:
                    raise WizardUiError("driver_failure") from exc
                except Exception as exc:
                    raise WizardUiError("driver_failure") from exc
            if censused_servers:
                break
        if not censused_servers:
            raise WizardUiError("server_not_found")
    except WizardUiError as exc:
        failure_reason = exc.reason
    except Exception:
        failure_reason = "driver_failure"
    finally:
        cleaned = _close_run_terminals(
            application,
            terminal,
            launched_at,
            process,
        )
        if not cleaned:
            failure_reason = "cleanup_failed"

    success = failure_reason is None
    return {
        "schema_version": 2,
        "run_id": request["run_id"],
        "status": "SUCCESS" if success else "FAILED",
        "failure_reason": failure_reason,
        "expected_server_name": expected,
        "selected_broker_label": selected_broker if success else None,
        "selected_broker_labels": list(selected_broker_labels) if success else [],
        "censused_server_names": censused_servers if success else [],
        "terminal_pid": pid if pid > 0 else 1,
        "completed_at_unix_ms": int(time.time() * 1000),
    }


def main() -> int:
    if len(sys.argv) != 3:
        return 2
    request_path = Path(sys.argv[1])
    result_path = Path(sys.argv[2])
    try:
        request = _read_request(request_path)
        result = _run(request)
        _write_atomic(result_path, result)
        return 0 if result["status"] == "SUCCESS" else 1
    except WizardUiError:
        return 2
    except Exception:
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
