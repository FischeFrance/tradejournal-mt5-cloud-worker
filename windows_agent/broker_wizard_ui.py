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
    return {
        "run_id": run_id,
        "terminal": terminal,
        "expected_server_name": expected,
        "suggested_broker_label": suggested,
        "search_terms": list(dict.fromkeys(term.strip() for term in terms)),
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
    import psutil

    if app is not None:
        try:
            app.top_window().close()
        except Exception:
            pass
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        matches = _run_terminal_processes(terminal, launched_at)
        if not matches:
            return True
        time.sleep(0.25)
    matches = _run_terminal_processes(terminal, launched_at)
    if not matches and initial_process.poll() is None:
        matches = [initial_process]
    for process in matches:
        try:
            process.terminate()
        except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
            continue
    _, alive = psutil.wait_procs(matches, timeout=5)
    for process in alive:
        try:
            process.kill()
        except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
            continue
    if alive:
        _, alive = psutil.wait_procs(alive, timeout=5)
    return not alive and not _run_terminal_processes(terminal, launched_at)


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
    selected_broker = suggested
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

        edit = dialog.child_window(class_name="Edit").wrapper_object()
        find_button = dialog.child_window(
            title="Find your company",
            class_name="Button",
        ).wrapper_object()
        for term in request["search_terms"]:
            attempt_started = time.monotonic()
            edit.set_edit_text("")
            edit.type_keys(term, with_spaces=True)
            find_button.click()
            time.sleep(4)
            next_button = dialog.child_window(
                title="&Next >",
                class_name="Button",
            ).wrapper_object()
            next_button.click()
            server_dialog = application.window(title_re=r"Open an Account.*")
            try:
                server_dialog.wait("exists ready", timeout=min(timeout, 30))
            except PwaTimeoutError as exc:
                raise WizardUiError("driver_failure") from exc
            try:
                combo = server_dialog.child_window(
                    class_name="ComboBox"
                ).wrapper_object()
                items = [
                    item.strip()
                    for item in combo.item_texts()
                    if isinstance(item, str) and _SERVER.fullmatch(item.strip())
                ]
            except Exception as exc:
                raise WizardUiError("driver_failure") from exc
            if any(item.casefold() == expected.casefold() for item in items):
                selected_broker = suggested
                censused_servers = sorted(set(items), key=str.casefold)
                try:
                    server_dialog.child_window(
                        title="Cancel",
                        class_name="Button",
                    ).wrapper_object().click()
                except Exception:
                    pass
                break
            try:
                server_dialog.child_window(
                    title="< &Back",
                    class_name="Button",
                ).wrapper_object().click()
                time.sleep(1)
                dialog = application.window(title_re=r"Open an Account.*")
            except Exception as exc:
                raise WizardUiError("driver_failure") from exc
            if time.monotonic() - attempt_started > timeout:
                raise WizardUiError("timeout")
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
        "schema_version": 1,
        "run_id": request["run_id"],
        "status": "SUCCESS" if success else "FAILED",
        "failure_reason": failure_reason,
        "expected_server_name": expected,
        "selected_broker_label": selected_broker if success else None,
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
