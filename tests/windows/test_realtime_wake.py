from __future__ import annotations

import json
import threading

from windows_agent.realtime_wake import RealtimeWakeListener


class FakeApi:
    def session(self):
        return {
            "realtime_url": "wss://fixture.supabase.co/realtime/v1/websocket",
            "publishable_key": "publishable-fixture",
            "access_token": "header.payload.signature",
            "expires_at": "2099-01-01T00:00:00.000Z",
            "topics": ["mt5-agent:any", "mt5-agent:11111111-1111-4111-8111-111111111111"],
        }


class FakeSocket:
    def __init__(self, stop_event):
        self.stop_event = stop_event
        self.sent = []
        self.received = 0

    def send(self, value):
        self.sent.append(json.loads(value))

    def recv(self):
        self.received += 1
        if self.received <= 2:
            return json.dumps(
                {"topic": "realtime:fixture", "event": "phx_reply", "payload": {"status": "ok", "response": {}}, "ref": str(self.received)}
            )
        self.stop_event.set()
        return json.dumps(
            {
                "topic": "realtime:mt5-agent:any",
                "event": "broadcast",
                "payload": {"event": "command_available", "payload": {"job_id": "fixture"}},
                "ref": None,
            }
        )

    def close(self):
        pass


def test_private_topics_join_and_broadcast_wakes_claim_drain():
    stop_event = threading.Event()
    wake_event = threading.Event()
    socket = FakeSocket(stop_event)
    captured = {}

    def connect(url, timeout):
        captured["url"] = url
        captured["timeout"] = timeout
        return socket

    listener = RealtimeWakeListener(FakeApi(), connect=connect)
    listener.run(wake_event, stop_event)

    assert wake_event.is_set()
    assert "apikey=publishable-fixture" in captured["url"]
    joins = [message for message in socket.sent if message["event"] == "phx_join"]
    assert [message["topic"] for message in joins] == [
        "realtime:mt5-agent:any",
        "realtime:mt5-agent:11111111-1111-4111-8111-111111111111",
    ]
    assert all(message["payload"]["config"]["private"] is True for message in joins)
    assert all(message["payload"]["access_token"] == "header.payload.signature" for message in joins)
