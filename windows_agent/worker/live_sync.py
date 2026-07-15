from __future__ import annotations

import random
import time
from typing import Any, Callable

from worker.event_detector import detect_events
from worker.event_normalizer import normalize_event

from .dedup import PersistentDedup


def detect_windows_events(previous: dict, current: dict) -> list[dict]:
    events = detect_events(previous, current)
    before, after = previous.get("positions", {}), current.get("positions", {})
    for ticket in sorted(before.keys() & after.keys()):
        old, new = before[ticket], after[ticket]
        if old.get("volume") != new.get("volume"):
            events.append(
                {
                    "event_type": "trade_volume_changed",
                    "ticket": ticket,
                    "symbol": new.get("symbol"),
                    "direction": new.get("direction"),
                    "volume": new.get("volume"),
                    "previous_volume": old.get("volume"),
                    "partial_close": (new.get("volume") or 0)
                    < (old.get("volume") or 0),
                }
            )
    for ticket in sorted(
        current.get("deals", {}).keys() - previous.get("deals", {}).keys()
    ):
        deal = current["deals"][ticket]
        events.append({"event_type": "deal_recorded", "ticket": ticket, **deal})
    return events


class LiveSync:
    def __init__(
        self,
        adapter: Any,
        snapshot_store: Any,
        dedup: PersistentDedup,
        sink: Callable[[dict], None],
        poll_seconds: float = 2.0,
    ) -> None:
        self.adapter, self.snapshot_store, self.dedup, self.sink = (
            adapter,
            snapshot_store,
            dedup,
            sink,
        )
        self.poll_seconds, self.stop_requested = poll_seconds, False

    def poll_once(self) -> int:
        account = self.adapter.verify_identity()
        current = self.adapter.snapshot()
        events = detect_windows_events(self.snapshot_store.get(), current)
        delivered = 0
        for event in events:
            payload = normalize_event(event, account["login"], account["server"])
            if not self.dedup.contains(payload["event_id"]):
                self.sink(payload)
                self.dedup.add(payload["event_id"])
                delivered += 1
        self.snapshot_store.save(current)
        return delivered

    def run(self) -> None:
        while not self.stop_requested:
            try:
                self.poll_once()
            except Exception:
                time.sleep(min(self.poll_seconds + random.uniform(0, 0.25), 10))
                continue
            time.sleep(self.poll_seconds)

    def stop(self) -> None:
        self.stop_requested = True
