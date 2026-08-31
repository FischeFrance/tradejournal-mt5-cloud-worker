from __future__ import annotations

from pathlib import Path

from worker.atomic_file import (
    _MOVEFILE_REPLACE_EXISTING,
    _MOVEFILE_WRITE_THROUGH,
    _windows_move_flags,
    durable_replace,
)


def test_durable_replace_publishes_complete_content(tmp_path: Path) -> None:
    destination = tmp_path / "state.json"
    destination.write_text("old", encoding="utf-8")
    temporary = tmp_path / ".state.json.tmp"
    temporary.write_text("new", encoding="utf-8")

    durable_replace(temporary, destination)

    assert destination.read_text(encoding="utf-8") == "new"
    assert not temporary.exists()


def test_windows_move_flags_replace_regular_files(tmp_path: Path) -> None:
    source = tmp_path / "event.tmp"
    source.write_text("complete", encoding="utf-8")

    assert _windows_move_flags(str(source)) == (
        _MOVEFILE_REPLACE_EXISTING | _MOVEFILE_WRITE_THROUGH
    )


def test_windows_move_flags_do_not_replace_directories(tmp_path: Path) -> None:
    source = tmp_path / "pool-slot"
    source.mkdir()

    assert _windows_move_flags(str(source)) == _MOVEFILE_WRITE_THROUGH
