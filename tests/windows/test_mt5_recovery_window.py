from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from windows_agent.mt5_recovery_window import new_only_recovery_from


NOW = datetime(2026, 8, 27, 21, 30, tzinfo=timezone.utc)


def _heartbeat(root: Path, generated_at: str) -> None:
    path = (
        root
        / "terminal"
        / "MQL5"
        / "Files"
        / "TradeJournal"
        / "heartbeat.json"
    )
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"generated_at": generated_at}), encoding="utf-8")


def test_recovery_cutoff_overlaps_the_last_durable_heartbeat(tmp_path: Path) -> None:
    _heartbeat(tmp_path, "2026-08-27T21:29:55Z")

    cutoff = new_only_recovery_from(tmp_path, clock=lambda: NOW)

    assert cutoff == datetime(2026, 8, 27, 21, 28, 55, tzinfo=timezone.utc)


def test_missing_or_invalid_heartbeat_uses_a_small_safe_overlap(tmp_path: Path) -> None:
    _heartbeat(tmp_path, "not-a-date")

    cutoff = new_only_recovery_from(tmp_path, clock=lambda: NOW)

    assert cutoff == NOW - timedelta(seconds=60)


def test_stale_heartbeat_cannot_expand_recovery_beyond_six_hours(
    tmp_path: Path,
) -> None:
    _heartbeat(tmp_path, "2025-01-01T00:00:00Z")

    cutoff = new_only_recovery_from(tmp_path, clock=lambda: NOW)

    assert cutoff == NOW - timedelta(hours=6)
