from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Literal

from ..state_store import atomic_json, read_json

HistoryMode = Literal["new_only", "from_date", "all_available"]


class HistorySync:
    def __init__(
        self,
        adapter: Any,
        checkpoint: Path,
        sink: Callable[[dict[str, Any]], None],
        accounting_sink: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.adapter = adapter
        self.checkpoint = checkpoint
        self.sink = sink
        self.accounting_sink = accounting_sink

    def run(
        self, mode: HistoryMode, from_date: datetime | None = None, chunk_days: int = 7
    ) -> dict[str, int]:
        if (
            mode == "from_date"
            and getattr(self.adapter, "history_time_basis", None)
            == "broker_server_unresolved"
        ):
            raise ValueError("historical_timezone_unavailable")
        now = datetime.now(timezone.utc)
        saved = read_json(self.checkpoint)
        if mode == "new_only":
            start = datetime.fromisoformat(saved.get("through", now.isoformat()))
        elif mode == "from_date" and from_date is not None:
            start = from_date.astimezone(timezone.utc)
        elif mode == "all_available":
            start = datetime(1970, 1, 1, tzinfo=timezone.utc)
        else:
            raise ValueError("invalid history mode/from_date")
        counts = {"orders": 0, "deals": 0, "accounting_deals": 0}
        cursor = start
        while cursor < now:
            # The local EA already publishes one immutable full-history snapshot.
            # Re-scanning that same ledger for thousands of seven-day windows is
            # quadratic; direct MT5 adapters still retain bounded chunking.
            end = (
                now
                if bool(getattr(self.adapter, "history_snapshot_atomic", False))
                else min(cursor + timedelta(days=max(1, min(chunk_days, 31))), now)
            )
            for kind, records in (
                ("orders", self.adapter.history_orders(cursor, end)),
                ("deals", self.adapter.history_deals(cursor, end)),
            ):
                for record in records:
                    self.sink(
                        {
                            "kind": kind,
                            "record": record._asdict()
                            if hasattr(record, "_asdict")
                            else dict(record),
                        }
                    )
                    counts[kind] += 1
            ledger_reader = getattr(self.adapter, "history_accounting_deals", None)
            if callable(ledger_reader) and self.accounting_sink is not None:
                for record in ledger_reader(cursor, end):
                    self.accounting_sink(
                        {
                            "kind": "accounting_deals",
                            "record": record._asdict()
                            if hasattr(record, "_asdict")
                            else dict(record),
                        }
                    )
                    counts["accounting_deals"] += 1
            atomic_json(
                self.checkpoint,
                {
                    "through": end.isoformat(),
                    "orders": counts["orders"],
                    "deals": counts["deals"],
                    "accounting_deals": counts["accounting_deals"],
                },
            )
            cursor = end
        return counts
