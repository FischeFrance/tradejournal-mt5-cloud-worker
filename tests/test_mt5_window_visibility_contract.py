from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "windows" / "Set-Mt5WindowVisibility.ps1"
RUNTIME = ROOT / "windows_agent" / "worker" / "native_mt5_runtime.py"


def test_window_visibility_helper_is_identity_bound_and_reversible() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert "Get-Process -Id" in source
    assert "expected_executable" in source
    assert "creation_time_unix_ms" in source
    assert "[StringComparison]::OrdinalIgnoreCase" in source
    assert "WindowsForProcess" in source
    assert "ShowWindowAsync" in source
    assert "SW_HIDE = 0" in source
    assert "SW_SHOWNA = 8" in source
    assert "visible_after" in source
    assert "Stop-Process" not in source
    assert "GetWindowText" not in source


def test_runtime_hides_only_after_verified_heartbeat() -> None:
    source = RUNTIME.read_text(encoding="utf-8")
    heartbeat = source[source.index("def _wait_for_heartbeat"):source.index("def _running_terminal_pids")]
    ready = source[source.index("def _ready_status"):source.index("def _wait_for_heartbeat")]

    assert "heartbeat is None" in heartbeat
    assert "investor_readonly_not_verified" in heartbeat
    assert "set_terminal_window_visibility(pid, visible=False)" in ready
    assert ready.index("_release_interactive_task()") < ready.index(
        "set_terminal_window_visibility"
    )
