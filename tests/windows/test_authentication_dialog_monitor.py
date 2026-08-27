from pathlib import Path


SCRIPT = Path("scripts/windows/Detect-Mt5AuthenticationFailure.ps1")


def test_monitor_is_process_bound_and_never_persists_window_text() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert "GetWindowThreadProcessId" in source
    assert "creation_time_unix_ms" in source
    assert "expected_executable" in source
    assert "Invalid account".casefold() in source.casefold()
    assert "error_code = 'authorization_failed'" in source
    assert "values" not in source[source.index("Write-AuthenticationResult") :]
