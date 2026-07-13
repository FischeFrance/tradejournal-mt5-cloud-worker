"""Test dell'outbox trade-sync: solo filesystem temporaneo e sender fake, nessuna rete."""

from __future__ import annotations

import json
import os
import stat
from unittest.mock import patch

import pytest

from event_outbox import EventOutbox, OutboxError
from event_sender import SendResult


def _payload(event_id="mt5-account-trade_opened-1-digest", event_time="2026-01-01T00:00:00Z"):
    return {
        "event_id": event_id,
        "event_type": "trade_opened",
        "event_time": event_time,
        "account_number": "123456",
        "server": "Broker-Demo",
        "external_trade_id": "1",
    }


class _Sender:
    def __init__(self, results):
        self.results = list(results)
        self.payloads = []

    def send(self, payload):
        self.payloads.append(payload)
        return self.results.pop(0)


def test_enqueue_is_atomic_and_deduplicated_by_event_id(tmp_path):
    path = tmp_path / "event_outbox.json"
    outbox = EventOutbox(str(path))
    first = _payload()

    real_replace = os.replace
    with patch("event_outbox.os.replace", wraps=real_replace) as replace:
        assert outbox.enqueue_many([first]) == 1

    replace.assert_called_once()
    source, destination = replace.call_args.args
    assert os.path.dirname(source) == str(tmp_path)
    assert destination == str(path)
    assert not list(tmp_path.glob("*.tmp"))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    # Dopo un crash tra enqueue e snapshot lo stesso evento viene normalizzato di nuovo. Il
    # timestamp puo' cambiare, ma event_id e' autorevole e la prima versione resta persistita.
    duplicate_after_restart = _payload(event_time="2026-01-01T00:00:01Z")
    restarted = EventOutbox(str(path))
    assert restarted.enqueue_many([duplicate_after_restart]) == 0
    assert restarted.pending_count() == 1
    assert restarted.pending_payloads()[first["event_id"]] == first


def test_batch_is_persisted_together_and_survives_restart(tmp_path):
    path = tmp_path / "event_outbox.json"
    first = _payload("event-1")
    second = _payload("event-2")

    outbox = EventOutbox(str(path))
    assert outbox.enqueue_many([first, second]) == 2

    restarted = EventOutbox(str(path))
    assert restarted.pending_payloads() == {"event-1": first, "event-2": second}


def test_fifo_order_survives_restart_when_event_ids_sort_differently(tmp_path):
    path = tmp_path / "event_outbox.json"
    payloads = [
        _payload("z-trade-opened", "2026-01-01T00:00:00Z"),
        _payload("m-trade-modified", "2026-01-01T00:00:01Z"),
        _payload("a-trade-closed", "2026-01-01T00:00:02Z"),
    ]
    EventOutbox(str(path)).enqueue_many(payloads)

    sender = _Sender([SendResult(status="sent") for _payload_item in payloads])
    result = EventOutbox(str(path)).drain(sender)

    assert result.sent == 3
    assert sender.payloads == payloads


def test_transient_failure_stops_before_later_events_to_preserve_causality(tmp_path):
    path = tmp_path / "event_outbox.json"
    opened = _payload("event-opened")
    closed = _payload("event-closed")
    outbox = EventOutbox(str(path))
    outbox.enqueue_many([opened, closed])
    sender = _Sender(
        [SendResult(status="failed", error="http_503", failure_type="transient")]
    )

    result = outbox.drain(sender)

    assert result.transient_failures == 1
    assert result.pending == 2
    assert sender.payloads == [opened]
    assert list(outbox.pending_payloads()) == ["event-opened", "event-closed"]
    assert outbox.dead_letter_count() == 0


def test_v1_file_is_migrated_without_loss_and_recovers_order_from_event_time(tmp_path):
    path = tmp_path / "event_outbox.json"
    opened = _payload("z-event-opened", "2026-01-01T00:00:00Z")
    closed = _payload("a-event-closed", "2026-01-01T00:00:01Z")
    dead = _payload("dead-event", "2025-12-31T23:59:59Z")
    legacy = {
        "version": 1,
        "pending": {opened["event_id"]: opened, closed["event_id"]: closed},
        "dead_letter": {
            dead["event_id"]: {"payload": dead, "failure_type": "permanent"}
        },
    }
    path.write_text(json.dumps(legacy, sort_keys=True), encoding="utf-8")
    path.chmod(0o644)

    migrated = EventOutbox(str(path))
    persisted = json.loads(path.read_text(encoding="utf-8"))

    assert list(migrated.pending_payloads()) == ["z-event-opened", "a-event-closed"]
    assert migrated.dead_letters()["dead-event"]["payload"] == dead
    assert persisted["version"] == 2
    assert [payload["event_id"] for payload in persisted["pending"]] == [
        "z-event-opened",
        "a-event-closed",
    ]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_transient_failure_stays_pending_and_is_delivered_after_restart(tmp_path):
    path = tmp_path / "event_outbox.json"
    payload = _payload()
    outbox = EventOutbox(str(path))
    outbox.enqueue_many([payload])

    first_sender = _Sender(
        [SendResult(status="failed", error="http_503", failure_type="transient", attempts=3)]
    )
    first_drain = outbox.drain(first_sender)
    assert first_drain.transient_failures == 1
    assert first_drain.pending == 1
    assert outbox.dead_letter_count() == 0

    restarted = EventOutbox(str(path))
    second_sender = _Sender([SendResult(status="sent", http_status=200, attempts=1)])
    second_drain = restarted.drain(second_sender)
    assert second_drain.sent == 1
    assert second_drain.pending == 0
    assert second_sender.payloads == [payload]
    assert EventOutbox(str(path)).pending_count() == 0


def test_permanent_failure_moves_event_to_persistent_dead_letter_once(tmp_path):
    path = tmp_path / "event_outbox.json"
    payload = _payload()
    outbox = EventOutbox(str(path))
    outbox.enqueue_many([payload])
    sender = _Sender([
        SendResult(
            status="failed",
            http_status=422,
            error="rejected_by_api",
            attempts=1,
            failure_type="permanent",
        )
    ])

    result = outbox.drain(sender)

    assert result.dead_lettered == 1
    assert result.pending == 0
    restarted = EventOutbox(str(path))
    assert restarted.pending_count() == 0
    assert restarted.dead_letter_count() == 1
    record = restarted.dead_letters()[payload["event_id"]]
    assert record["payload"] == payload
    assert record["http_status"] == 422

    never_called = _Sender([])
    assert restarted.drain(never_called).pending == 0
    assert never_called.payloads == []
    assert restarted.enqueue_many([payload]) == 0


def test_dry_run_preserves_outbox_for_later_real_delivery(tmp_path):
    path = tmp_path / "event_outbox.json"
    outbox = EventOutbox(str(path))
    outbox.enqueue_many([_payload()])
    sender = _Sender([SendResult(status="dry_run", attempts=0)])

    result = outbox.drain(sender)

    assert result.dry_run == 1
    assert result.pending == 1
    assert EventOutbox(str(path)).pending_count() == 1


def test_invalid_or_truncated_file_fails_closed_instead_of_losing_events(tmp_path):
    path = tmp_path / "event_outbox.json"
    path.write_text('{"pending":', encoding="utf-8")

    with pytest.raises(OutboxError, match="illeggibile"):
        EventOutbox(str(path))


def test_invalid_event_id_is_rejected_without_creating_state_file(tmp_path):
    path = tmp_path / "event_outbox.json"
    outbox = EventOutbox(str(path))

    with pytest.raises(OutboxError, match="event_id"):
        outbox.enqueue_many([{"event_type": "trade_opened"}])

    assert not path.exists()


def test_persisted_format_contains_no_sender_credentials(tmp_path):
    path = tmp_path / "event_outbox.json"
    outbox = EventOutbox(str(path))
    outbox.enqueue_many([_payload()])

    state = json.loads(path.read_text(encoding="utf-8"))

    assert state["version"] == 2
    assert isinstance(state["pending"], list)
    serialized = json.dumps(state)
    assert "password" not in serialized.lower()
    assert "token" not in serialized.lower()
