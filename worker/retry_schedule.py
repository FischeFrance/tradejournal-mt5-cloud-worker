"""Bounded wall-clock retry metadata; active processes use a monotonic deadline."""

from __future__ import annotations

import copy
import math
import time
from typing import Any, Callable


def validate_retry_record(record: Any) -> None:
    if record is None:
        return
    if not isinstance(record, dict) or set(record) != {
        "requested_at", "retry_at", "delay_seconds"
    }:
        raise ValueError("invalid ingestion retry metadata")
    delay = record["delay_seconds"]
    if isinstance(delay, bool) or not isinstance(delay, int) or not 1 <= delay <= 60:
        raise ValueError("invalid ingestion retry delay")
    for key in ("requested_at", "retry_at"):
        value = record[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("invalid ingestion retry timestamp")
        if not math.isfinite(value) or value < 0:
            raise ValueError("invalid ingestion retry timestamp")
    if abs(record["retry_at"] - record["requested_at"] - delay) > 0.001:
        raise ValueError("inconsistent ingestion retry timestamps")


class RetrySchedule:
    """Only numeric deadlines are stored, never URLs, credentials or error responses."""

    def __init__(
        self,
        record: dict | None = None,
        clock_fn: Callable[[], float] = time.time,
        monotonic_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self.clock = clock_fn
        self.monotonic = monotonic_fn
        self.record: dict | None = None
        self._deadline = 0.0
        self.replace(record)

    def replace(self, record: dict | None) -> None:
        validate_retry_record(record)
        if record == self.record:
            return
        self.record = copy.deepcopy(record)
        # Clamp a restart after a wall-clock rollback to the original bounded delay.
        remaining = (
            max(0.0, min(record["delay_seconds"], record["retry_at"] - self.clock()))
            if record else 0.0
        )
        self._deadline = self.monotonic() + remaining

    @property
    def remaining_seconds(self) -> int:
        return math.ceil(max(0.0, self._deadline - self.monotonic()))

    @property
    def retry_at(self) -> float | None:
        return self.record["retry_at"] if self.record else None

    def after(self, delay: int | None) -> dict:
        if isinstance(delay, bool) or not isinstance(delay, int) or not 1 <= delay <= 60:
            delay = 60
        now = self.clock()
        return {"requested_at": now, "retry_at": now + delay, "delay_seconds": delay}
