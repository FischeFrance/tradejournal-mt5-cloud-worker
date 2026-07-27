from __future__ import annotations

from pathlib import Path

from worker.atomic_file import durable_replace


def test_durable_replace_publishes_complete_content(tmp_path: Path) -> None:
    destination = tmp_path / "state.json"
    destination.write_text("old", encoding="utf-8")
    temporary = tmp_path / ".state.json.tmp"
    temporary.write_text("new", encoding="utf-8")

    durable_replace(temporary, destination)

    assert destination.read_text(encoding="utf-8") == "new"
    assert not temporary.exists()
