from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator


class FakeMt5Adapter:
    def __init__(self, snapshots: list[dict[str, Any]] | None = None) -> None:
        self.snapshots = snapshots or [{"positions": {}, "orders": {}, "deals": {}}]
        self.index = 0

    @contextmanager
    def session(self, investor_password: str) -> Iterator["FakeMt5Adapter"]:
        yield self

    def verify_identity(self) -> dict[str, str]:
        return {"login": "0000", "server": "Demo"}

    def snapshot(self, lookback_hours: int = 72) -> dict[str, Any]:
        value = self.snapshots[min(self.index, len(self.snapshots) - 1)]
        self.index += 1
        return value

    def history_orders(self, start: Any, end: Any) -> tuple[Any, ...]:
        return ()

    def history_deals(self, start: Any, end: Any) -> tuple[Any, ...]:
        return ()
