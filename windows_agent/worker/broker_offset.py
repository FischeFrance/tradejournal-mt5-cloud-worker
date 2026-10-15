"""Broker server time minus UTC, measured from the Expert's own clocks.

MT5 deals and events carry the broker server wall clock labelled as UTC. Every file the Expert
writes has the real UTC time in its envelope (TimeGMT); the heartbeat payload has the server
clock: ``generated_at`` is TimeCurrent, the time of the last quote, and ``server_time`` (when
the Expert provides it) is TimeTradeServer, which keeps running while the market is closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..state_store import atomic_json, read_json

QUARTER_HOUR_SECONDS = 900
MAX_OFFSET_SECONDS = 14 * 3600
# A fresh last quote is seconds old while the market trades; an older one means a stopped clock.
MAX_SERVER_CLOCK_LAG_SECONDS = 120
# A stored measurement covers a closed market or a restart, not a daylight-saving change.
MAX_MEASUREMENT_AGE = timedelta(days=3)
_CLOCK_SKEW = timedelta(minutes=2)


@dataclass(frozen=True)
class BrokerOffset:
    seconds: int
    observed_at: datetime

    def event_fields(self) -> dict[str, Any]:
        observed = self.observed_at.astimezone(timezone.utc).isoformat(timespec="seconds")
        return {
            "broker_utc_offset_seconds": self.seconds,
            "broker_offset_observed_at": observed.replace("+00:00", "Z"),
        }


def measure_broker_offset(
    utc_clock: datetime, server_clock: datetime, *, terminal_connected: bool
) -> BrokerOffset | None:
    """Server clock minus UTC in whole quarter hours, or None when it cannot be trusted."""
    if not terminal_connected:
        return None
    raw = (server_clock - utc_clock).total_seconds()
    rounded = round(raw / QUARTER_HOUR_SECONDS) * QUARTER_HOUR_SECONDS
    if abs(rounded) > MAX_OFFSET_SECONDS or abs(raw - rounded) > MAX_SERVER_CLOCK_LAG_SECONDS:
        return None
    return BrokerOffset(int(rounded), utc_clock.astimezone(timezone.utc))


class BrokerOffsetStore:
    """Last trusted measurement of one connection, kept across restarts."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def save(self, offset: BrokerOffset) -> None:
        atomic_json(self.path, {"seconds": offset.seconds, "observed_at": offset.observed_at.isoformat()})

    def recent(self, now: datetime) -> BrokerOffset | None:
        try:
            state = read_json(self.path)
            seconds = state["seconds"]
            observed_at = datetime.fromisoformat(state["observed_at"])
        except (OSError, ValueError, KeyError, TypeError):
            return None
        if (
            not isinstance(seconds, int)
            or isinstance(seconds, bool)
            or seconds % QUARTER_HOUR_SECONDS != 0
            or abs(seconds) > MAX_OFFSET_SECONDS
            or observed_at.tzinfo is None
        ):
            return None
        age = now - observed_at
        if age < -_CLOCK_SKEW or age > MAX_MEASUREMENT_AGE:
            return None
        return BrokerOffset(seconds, observed_at.astimezone(timezone.utc))
