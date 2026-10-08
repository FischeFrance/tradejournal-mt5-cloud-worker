"""Bounded history overlap used only to bridge an MT5 restart gap."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable


DEFAULT_OVERLAP = timedelta(seconds=60)
MAX_RECOVERY_WINDOW = timedelta(hours=6)


def new_only_recovery_from(
    instance_root: Path,
    *,
    clock: Callable[[], datetime] | None = None,
) -> datetime:
    """Return a safe UTC cutoff for a ``new_only`` restart.

    The bridge heartbeat is the last durable proof that live events were being
    observed before the stop.  Its timestamp is overlapped by one minute and
    clamped to six hours, so malformed/stale local data can never turn a rolling
    restart into a full historical import.  Downstream event IDs are
    deterministic, making the deliberate overlap idempotent.
    """

    now = (clock or (lambda: datetime.now(timezone.utc)))()
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("MT5 recovery clock must return an aware datetime")
    now = now.astimezone(timezone.utc)
    observed: datetime | None = None
    heartbeat = (
        Path(instance_root)
        / "terminal"
        / "MQL5"
        / "Files"
        / "TradeJournal"
        / "heartbeat.json"
    )
    try:
        if heartbeat.is_file() and heartbeat.stat().st_size <= 256 * 1024:
            value = json.loads(heartbeat.read_text(encoding="utf-8"))
            generated_at = value.get("generated_at") if isinstance(value, dict) else None
            if isinstance(generated_at, str):
                parsed = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
                if parsed.tzinfo is not None:
                    observed = parsed.astimezone(timezone.utc)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        observed = None

    if observed is None or observed > now:
        observed = now
    cutoff = observed - DEFAULT_OVERLAP
    floor = now - MAX_RECOVERY_WINDOW
    if cutoff < floor:
        cutoff = floor
    return cutoff
