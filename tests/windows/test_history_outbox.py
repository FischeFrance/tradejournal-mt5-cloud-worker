from __future__ import annotations

import pytest

from worker.event_outbox import EventOutbox
from worker.event_sender import SendResult
from windows_agent import real_handlers
from windows_agent.agent_errors import HistorySyncFailed


class _Sender:
    def __init__(self, results: list[SendResult]) -> None:
        self.results = list(results)
        self.payloads: list[dict] = []

    def send(self, payload: dict) -> SendResult:
        self.payloads.append(payload)
        return self.results.pop(0)


class _HistorySyncWithClose:
    def __init__(self, _adapter, _checkpoint, sink) -> None:
        self.sink = sink

    def run(self, _mode, _from_date) -> dict[str, int]:
        self.sink(
            {
                "kind": "deals",
                "record": {
                    "ticket": "9001",
                    "position_id": "1001",
                    "symbol": "DAX40",
                    "direction": "sell",
                    "volume": 1.0,
                    "price": 23456.7,
                    "entry": "OUT",
                    "profit": -26.48,
                    "commission": -1.0,
                    "swap": 0.0,
                    "time": "2026-09-23T16:42:47Z",
                },
            }
        )
        return {"orders": 0, "deals": 1}


def _payload(event_id: str, event_type: str) -> dict:
    return {"event_id": event_id, "event_type": event_type}


def test_history_dead_letter_does_not_block_later_trade_close(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "instance"
    outbox_path = root / "state" / "history-outbox.json"
    rejected = _payload("pending-order-filled-rejected", "pending_order_filled")
    outbox = EventOutbox(str(outbox_path))
    outbox.enqueue_many([rejected])
    rejected_sender = _Sender([
        SendResult(
            status="failed",
            http_status=422,
            error="rejected_by_api",
            attempts=1,
            failure_type="permanent",
        )
    ])
    assert outbox.drain(rejected_sender).dead_lettered == 1
    monkeypatch.setattr(real_handlers, "HistorySync", _HistorySyncWithClose)

    sender = _Sender([SendResult(status="sent", http_status=200, attempts=1)])
    result = real_handlers._run_history_sync(
        object(),
        root,
        "new_only",
        None,
        ingestion_sink=sender,
        login="42",
        server="Demo",
    )

    assert result == {"orders": 0, "deals": 1}
    assert [payload["event_type"] for payload in sender.payloads] == ["trade_closed"]
    assert sender.payloads[0]["external_trade_id"] == "1001"
    assert sender.payloads[0]["close_time"] == "2026-09-23T16:42:47Z"
    persisted_outbox = EventOutbox(str(outbox_path))
    assert persisted_outbox.pending_count() == 0
    assert persisted_outbox.dead_letter_count() == 1


def test_history_transient_delivery_remains_fail_closed(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(real_handlers, "HistorySync", _HistorySyncWithClose)
    root = tmp_path / "instance"
    sender = _Sender([
        SendResult(
            status="failed",
            error="http_503",
            attempts=3,
            failure_type="transient",
        )
    ])

    with pytest.raises(HistorySyncFailed, match="pending=1"):
        real_handlers._run_history_sync(
            object(),
            root,
            "new_only",
            None,
            ingestion_sink=sender,
            login="42",
            server="Demo",
        )

    outbox = EventOutbox(str(root / "state" / "history-outbox.json"))
    assert outbox.pending_count() == 1
    assert outbox.dead_letter_count() == 0
