"""Real sender/outbox/live-poll paths, mocked HTTP and clocks; no broker or provider access."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
import requests

from windows_agent.api_client import AgentApiClient
from windows_agent.job_runner import JobRunner, LeaseLost
from windows_agent.worker.dedup import PersistentDedup
from windows_agent.worker.live_sync import LiveSync, LiveSyncDeliveryError
from windows_agent.worker.trading_ingestion_sink import TradingIngestionSink
from worker.event_outbox import EventOutbox, OutboxError
from worker.event_sender import EventSender, retry_after_seconds


class Clock:
    def __init__(self):
        self.wall = 1_000.0
        self.mono = 10.0

    def advance(self, seconds):
        self.wall += seconds
        self.mono += seconds

    def options(self):
        return {"clock_fn": lambda: self.wall, "monotonic_fn": lambda: self.mono}


def response(status, delay=None):
    return SimpleNamespace(status_code=status, headers={"Retry-After": delay},
                           json=lambda: {"status": "ok"})


def sender():
    return EventSender("https://example.invalid/ingestion", "never-persist-token", False,
                       max_retries=1, sleep_fn=lambda _: pytest.fail("429 must not sleep"))


def payload(event_id):
    return {"event_id": event_id, "event_type": "trade_closed", "profit": -1.5}


def outbox(path, clock):
    return EventOutbox(str(path), **clock.options())


@pytest.mark.parametrize("value,expected", [("1", 1), (" 17 ", 17), ("60", 60), ("01", 1)])
def test_rate_hint_is_bounded_numeric_and_sender_never_immediately_retries(value, expected):
    with patch("requests.post", return_value=response(429, value)) as post:
        result = sender().send(payload("first"))
    assert result.retry_after_seconds == expected
    assert result.status == "failed" and result.retryable and result.attempts == 1
    post.assert_called_once()


@pytest.mark.parametrize("value", [None, "", "0", "61", "999", "-1", "1.5", "NaN",
                                  "Sun, 12 Oct 2026 11:00:00 GMT", "20\r\nsecret", 20, True])
def test_untrusted_rate_hint_cannot_set_an_unbounded_or_zero_deadline(value):
    assert retry_after_seconds({"Retry-After": value}) == 60


def test_fifo_delay_survives_restart_and_enqueue_without_reset(tmp_path):
    clock, path = Clock(), tmp_path / "connection-a.json"
    events = [payload("opened"), payload("modified"), payload("closed")]
    queue = outbox(path, clock)
    queue.enqueue_many(events)
    with patch("requests.post", side_effect=[response(200), response(429, "17")]) as post:
        first = queue.drain(sender())
    assert first.sent == 1 and first.pending == 2 and first.rate_limited
    assert first.retry_after_seconds == 17
    assert post.call_args.kwargs["json"] == events[1]
    deadline = queue.retry_at
    clock.advance(8)
    queue.enqueue_many([payload("newer")])
    assert queue.retry_at == deadline
    restarted = outbox(path, clock)
    with patch("requests.post") as post:
        waiting = restarted.drain(sender())
    post.assert_not_called()
    assert waiting.pending == 3 and waiting.retry_after_seconds == 9
    clock.advance(9)
    with patch("requests.post", return_value=response(200)) as post:
        resumed = restarted.drain(sender())
    assert resumed.sent == 3 and resumed.pending == 0
    assert [call.kwargs["json"] for call in post.call_args_list] == events[1:] + [payload("newer")]
    assert restarted.retry_at is None and "retry" not in json.loads(path.read_text())


def test_backpressure_is_scoped_to_the_connection_outbox(tmp_path):
    clock = Clock()
    capped = outbox(tmp_path / "account-a.json", clock)
    other = outbox(tmp_path / "account-b.json", clock)
    capped.enqueue_many([payload("first")])
    other.enqueue_many([payload("other")])
    with patch("requests.post", side_effect=[response(429, "60"), response(200)]) as post:
        assert capped.drain(sender()).rate_limited
        assert other.drain(sender()).sent == 1
    assert post.call_count == 2


def test_lost_success_response_replays_identical_id_payload_and_only_then_removes(tmp_path):
    clock, path = Clock(), tmp_path / "outbox.json"
    event = payload("immutable-close-id")
    queue = outbox(path, clock)
    queue.enqueue_many([event])
    remote_saved = set()
    observed = []

    def simulated_ingestion(_url, **kwargs):
        observed.append(kwargs["json"])
        event_id = kwargs["json"]["event_id"]
        if event_id not in remote_saved:
            remote_saved.add(event_id)
            raise requests.Timeout("response lost after durable commit")
        return response(200)  # Duplicate acknowledgement, no second economic operation.

    with patch("requests.post", side_effect=simulated_ingestion):
        assert queue.drain(sender()).pending == 1
        assert outbox(path, clock).drain(sender()).sent == 1
    assert observed == [event, event] and remote_saved == {event["event_id"]}
    assert outbox(path, clock).pending_count() == 0


def test_crash_after_ack_before_local_delete_keeps_original_event_for_duplicate_retry(tmp_path):
    clock, path = Clock(), tmp_path / "outbox.json"
    event = payload("original-id")
    queue = outbox(path, clock)
    queue.enqueue_many([event])
    with patch("requests.post", return_value=response(200)), patch.object(
        queue, "_persist", side_effect=OSError("simulated disk failure")
    ):
        with pytest.raises(OSError):
            queue.drain(sender())
    assert outbox(path, clock).pending_payloads() == {event["event_id"]: event}
    with patch("requests.post", return_value=response(200)) as post:
        assert outbox(path, clock).drain(sender()).sent == 1
    assert post.call_args.kwargs["json"] == event


def test_live_cycle_does_not_read_new_source_events_while_the_fifo_is_waiting(tmp_path):
    clock, path = Clock(), tmp_path / "outbox.json"
    queue = outbox(path, clock)
    queue.enqueue_many([payload("prior-event")])
    with patch("requests.post", return_value=response(429, "30")):
        assert queue.drain(sender()).rate_limited
    adapter = SimpleNamespace(verify_identity=lambda: pytest.fail("must keep causal prefix"))
    dedup = PersistentDedup(tmp_path / "dedup.sqlite")
    try:
        live = LiveSync(adapter, None, dedup, sender(), outbox=outbox(path, clock))
        with patch("requests.post") as post:
            assert live.poll_once() == 0
        post.assert_not_called()
    finally:
        dedup.close()
    assert outbox(path, clock).pending_count() == 1


@pytest.mark.parametrize("status", [401, 403, 503])
def test_non_rate_errors_do_not_become_successful_deferred_lifecycle_cycles(tmp_path, status):
    clock = Clock()
    queue = outbox(tmp_path / "outbox.json", clock)
    queue.enqueue_many([payload("event")])
    dedup = PersistentDedup(tmp_path / "dedup.sqlite")
    adapter = SimpleNamespace(verify_identity=lambda: pytest.fail("pending prefix must stop source"))
    try:
        live = LiveSync(adapter, None, dedup, sender(), outbox=queue)
        with patch("requests.post", return_value=response(status)):
            with pytest.raises(LiveSyncDeliveryError):
                live.poll_once()
    finally:
        dedup.close()
    assert queue.retry_at is None
    assert queue.pending_count() == 1


def test_heartbeat_coalesces_latest_snapshot_retaining_retry_across_restart(tmp_path):
    clock = Clock()
    account = SimpleNamespace(balance=100.0, equity=101.0, currency="EUR", leverage=100)
    sink = TradingIngestionSink(tmp_path, "https://example.invalid", "never-persist-token",
                                **clock.options())
    with patch("requests.post", return_value=response(429, "30")) as post:
        assert sink.send_heartbeat(account) is False
        assert sink.heartbeat_deferred and sink.heartbeat_retry_at == 1_030
        account.balance = 105.0
        assert sink.heartbeat_may_continue(account) is True
        assert post.call_count == 1
    clock.advance(10)
    restarted = TradingIngestionSink(tmp_path, "https://example.invalid", "rotated-token",
                                     **clock.options())
    with patch("requests.post") as post:
        assert restarted.heartbeat_may_continue(account) is True
        assert restarted.heartbeat_deferred and restarted.heartbeat_retry_at == 1_030
    post.assert_not_called()
    state = (tmp_path / "state" / "heartbeat-outbox.json").read_text()
    assert "token" not in state and "https" not in state and len(json.loads(state)["pending"]) == 1
    clock.advance(20)
    account.balance = 110.0
    with patch("requests.post", return_value=response(200)) as post:
        assert restarted.send_heartbeat(account) is True
    assert post.call_args.kwargs["json"] == {
        "event_type": "heartbeat", "balance": 110.0, "equity": 101.0,
        "currency": "EUR", "leverage": 100,
    }
    assert not restarted.heartbeat_deferred and restarted.heartbeat_retry_at is None


@pytest.mark.parametrize("status", [401, 403, 409, 422])
def test_heartbeat_cycle_decision_does_not_mask_auth_or_other_rejections(tmp_path, status):
    sink = TradingIngestionSink(tmp_path, "https://example.invalid", "token")
    with patch("requests.post", return_value=response(status)):
        assert sink.heartbeat_may_continue() is False
    assert not sink.heartbeat_deferred and sink.heartbeat_retry_at is None


def test_presence_backpressure_does_not_block_trade_delivery(tmp_path):
    clock = Clock()
    sink = TradingIngestionSink(tmp_path, "https://example.invalid", "token", **clock.options())
    with patch("requests.post", side_effect=[response(429, "60"), response(200)]) as post:
        assert sink.heartbeat_may_continue() is True
        assert sink.send(payload("trade-close")).status == "sent"
    assert post.call_count == 2


def test_clock_changes_cannot_trigger_early_send_or_unbounded_wait_after_restart(tmp_path):
    clock, path = Clock(), tmp_path / "outbox.json"
    queue = outbox(path, clock)
    queue.enqueue_many([payload("id")])
    with patch("requests.post", return_value=response(429, "60")):
        queue.drain(sender())
    clock.wall += 100_000  # Running instance follows its monotonic deadline.
    assert queue.retry_after_seconds == 60
    clock.wall = 500  # Restart after rollback is bounded by the original 60-second delay.
    restarted = outbox(path, clock)
    assert restarted.retry_after_seconds == 60
    clock.advance(60)
    with patch("requests.post", return_value=response(200)):
        assert restarted.drain(sender()).sent == 1


@pytest.mark.parametrize("retry", [{"retry_at": 2},
                                   {"requested_at": 1000, "retry_at": 1060, "delay_seconds": True},
                                   {"requested_at": 1000, "retry_at": 1060, "delay_seconds": 0},
                                   {"requested_at": 1000, "retry_at": float("nan"), "delay_seconds": 60}])
def test_corrupt_retry_metadata_fails_closed_without_discarding_events(tmp_path, retry):
    path = tmp_path / "outbox.json"
    path.write_text(json.dumps({"version": 2, "pending": [payload("id")],
                                "dead_letter": {}, "retry": retry}))
    with pytest.raises(OutboxError, match="retry"):
        EventOutbox(str(path))
    assert json.loads(path.read_text())["pending"][0]["event_id"] == "id"


def test_heartbeat_coalescing_cannot_overwrite_a_trade_queue(tmp_path):
    queue = EventOutbox(str(tmp_path / "outbox.json"))
    queue.enqueue_many([payload("id")])
    with pytest.raises(OutboxError, match="Heartbeat coalescing"):
        queue.coalesce_heartbeat({"event_id": "presence", "event_type": "heartbeat"})
    assert queue.pending_count() == 1


@pytest.mark.parametrize("status,body", [(200, {"status": "deferred"}), (200, {"error": "secret"}),
                                      (200, None), (200, []), (204, None)])
def test_2xx_without_explicit_durable_ack_keeps_the_event(tmp_path, status, body, caplog):
    queue = EventOutbox(str(tmp_path / "outbox.json"))
    event = payload("stable-id")
    queue.enqueue_many([event])
    reply = response(status)
    reply.json = lambda: body
    with patch("requests.post", return_value=reply) as post:
        delivery = queue.drain(sender())
    assert delivery.sent == 0 and delivery.pending == 1 and delivery.transient_failures == 1
    assert queue.pending_payloads() == {event["event_id"]: event}
    assert "secret" not in caplog.text and post.call_args.kwargs["allow_redirects"] is False


def test_html_login_response_cannot_delete_an_event_or_echo_body(tmp_path, caplog):
    queue = EventOutbox(str(tmp_path / "outbox.json"))
    queue.enqueue_many([payload("event")])
    reply = response(200)

    def html_instead_of_json():
        raise ValueError("<html>never-log-response-secret</html>")

    reply.json = html_instead_of_json
    with patch("requests.post", return_value=reply):
        delivery = queue.drain(sender())
    assert delivery.pending == 1 and delivery.sent == 0
    assert "never-log-response-secret" not in caplog.text


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_redirect_never_forwards_the_token_or_acks_a_login_page(tmp_path, status):
    queue = EventOutbox(str(tmp_path / "outbox.json"))
    queue.enqueue_many([payload("event")])
    reply = response(status)
    reply.headers["Location"] = "https://untrusted.invalid/login"
    with patch("requests.post", return_value=reply) as post:
        delivery = queue.drain(sender())
    assert delivery.pending == 1 and delivery.sent == 0 and queue.dead_letter_count() == 0
    post.assert_called_once()
    assert post.call_args.kwargs["allow_redirects"] is False


@pytest.mark.parametrize("ack", ["ok", "duplicate"])
def test_only_a_valid_durable_receipt_removes_the_event(tmp_path, ack):
    queue = EventOutbox(str(tmp_path / "outbox.json"))
    queue.enqueue_many([payload("event")])
    reply = response(200)
    reply.json = lambda: {"status": ack, "imported_trade_id": None}
    with patch("requests.post", return_value=reply):
        assert queue.drain(sender()).sent == 1
    assert queue.pending_count() == 0


def test_dry_run_of_an_existing_real_outbox_never_acknowledges_or_deletes(tmp_path):
    path = tmp_path / "outbox.json"
    event = payload("previous-real-event")
    EventOutbox(str(path)).enqueue_many([event])
    dry_sender = EventSender(None, None, True)
    with patch("requests.post") as post:
        delivery = EventOutbox(str(path)).drain(dry_sender)
    post.assert_not_called()
    assert delivery.dry_run == 1 and delivery.sent == 0 and delivery.pending == 1
    assert EventOutbox(str(path)).pending_payloads() == {event["event_id"]: event}


def test_new_stream_batch_429_is_durable_before_source_ack_and_resumes_without_job_failure(tmp_path):
    clock, path = Clock(), tmp_path / "outbox.json"
    record = {
        "sequence": 18, "event_type": "DEAL_ADD", "entry": "IN", "ticket": "901",
        "deal_id": "901", "position_id": "100", "order_id": "700", "symbol": "EURUSD",
        "direction": "buy", "volume": 0.1, "price": 1.2, "time": "2026-10-12T09:00:00Z",
        "timestamp_msc": 1_791_794_800_000,
    }

    class Snapshot:
        value = {"positions": {}, "deals": {}, "orders": {}}

        def get(self):
            return self.value

        def save(self, value):
            self.value = value

    class Adapter:
        acknowledged = 0
        records = (record,)

        def verify_identity(self):
            return {"login": "42", "server": "Demo"}

        def snapshot(self):
            return {"positions": {"100": {"ticket": "100", "symbol": "EURUSD",
                                           "volume": 0.1, "open_price": 1.2}},
                    "deals": {}, "orders": {}}

        def pending_events(self):
            return self.records

        def acknowledge_events(self, sequence):
            assert EventOutbox(str(path)).pending_count() == 1  # fsync + atomic rename precedes ack.
            self.acknowledged = sequence
            self.records = ()

    adapter, snapshot = Adapter(), Snapshot()
    dedup = PersistentDedup(tmp_path / "dedup.sqlite")
    try:
        live = LiveSync(adapter, snapshot, dedup, sender(), outbox=outbox(path, clock))
        with patch("requests.post", return_value=response(429, "10")) as post:
            assert live.poll_once() == 0
        original = post.call_args.kwargs["json"]
        assert adapter.acknowledged == 18 and outbox(path, clock).pending_count() == 1
        clock.advance(10)
        with patch("requests.post", return_value=response(200)) as post:
            assert live.poll_once() == 1
        assert post.call_args.kwargs["json"] == original
    finally:
        dedup.close()


def test_sink_failure_log_does_not_reintroduce_login_from_the_event_id(tmp_path, caplog):
    sink = TradingIngestionSink(tmp_path, "https://example.invalid", "never-log-token")
    account = "123456789"
    event = {**payload(f"mt5-{account}-closed-native-id"), "account_number": account,
             "server": "NeverLogRealBroker-Live"}
    with patch("requests.post", return_value=response(429, "10")):
        assert sink.send(event).http_status == 429
    assert account not in caplog.text and "never-log-token" not in caplog.text
    assert "NeverLogRealBroker-Live" not in caplog.text


def test_ingestion_409_retains_existing_quarantine_semantics_not_rate_deferral(tmp_path):
    queue = EventOutbox(str(tmp_path / "outbox.json"))
    queue.enqueue_many([payload("id")])
    with patch("requests.post", return_value=response(409)):
        delivery = queue.drain(sender())
    assert delivery.dead_lettered == 1 and not delivery.rate_limited
    assert queue.retry_at is None and queue.dead_letters()["id"]["payload"] == payload("id")


@pytest.mark.parametrize("status", [401, 403, 429])
def test_control_plane_heartbeat_errors_remain_fail_closed_and_never_presence_deferral(status):
    calls = []

    def lease_endpoint(request):
        calls.append(request)
        return httpx.Response(status, json={"error_code": "unauthorized"})

    client = AgentApiClient("https://example.invalid", "token", httpx.MockTransport(lease_endpoint))
    try:
        with pytest.raises(httpx.HTTPStatusError):
            client.heartbeat("job", "lease")
    finally:
        client.client.close()
        client._upload_client.close()
    assert len(calls) == 1  # Ingestion's Retry-After handling does not alter lease ownership.


def test_control_plane_409_still_yields_lease_lost_never_job_completion():
    body = {"api_version": "1", "lease_valid": False, "error_code": "lease_lost"}
    client = AgentApiClient("https://example.invalid", "token",
                            httpx.MockTransport(lambda _: httpx.Response(409, json=body)))
    try:
        assert client.heartbeat("job", "lease") == body
        with pytest.raises(LeaseLost):
            JobRunner._require_transition({"error_code": "lease_lost"}, "complete")
    finally:
        client.client.close()
        client._upload_client.close()
