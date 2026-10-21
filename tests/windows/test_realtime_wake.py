from __future__ import annotations

import json
import threading

import pytest
import websocket

from windows_agent.realtime_wake import RealtimeWakeListener

AGENT_TOPIC = "mt5-agent:11111111-1111-4111-8111-111111111111"
SESSION = {
    "api_version": "1",
    "realtime_url": "wss://fixture.supabase.co/realtime/v1/websocket",
    "publishable_key": "publishable-fixture",
    "access_token": "header.payload.signature",
    "expires_at": "2099-01-01T00:00:00.000Z",
    "topics": ["mt5-agent:any", AGENT_TOPIC],
}


def reply(ref, status="ok", topic="realtime:mt5-agent:any"):
    return json.dumps({"topic": topic, "event": "phx_reply", "payload": {"status": status, "response": {}}, "ref": ref})


def broadcast(event="command_available", topic="realtime:mt5-agent:any"):
    return json.dumps({
        "topic": topic,
        "event": "broadcast",
        "payload": {"event": event, "payload": {"job_id": "fixture"}, "type": "broadcast"},
        "ref": None,
    })


TIMEOUT = object()


class ScriptedSocket:
    """Plays the server's frames in order; after the script, calls ``on_exhausted``."""

    def __init__(self, frames, on_exhausted):
        self.frames = list(frames)
        self.on_exhausted = on_exhausted
        self.sent = []
        self.closed = False

    def send(self, value):
        self.sent.append(json.loads(value))

    def recv(self):
        if self.frames:
            frame = self.frames.pop(0)
            if frame is TIMEOUT:
                raise websocket.WebSocketTimeoutException("idle")
            if callable(frame):
                return frame()
            return frame
        return self.on_exhausted()

    def close(self):
        self.closed = True


class SessionApi:
    def __init__(self, session=SESSION):
        self.session_calls = 0
        self._session = session

    def session(self):
        self.session_calls += 1
        return dict(self._session)


def run_listener(listener, stop_event, wake_event=None):
    wake_event = wake_event or threading.Event()
    listener.run(wake_event, stop_event)
    return wake_event


def test_joins_both_private_topics_with_the_session_token_and_wakes_on_command_available():
    stop_event = threading.Event()
    seen = {}
    socket = ScriptedSocket([reply("1"), reply("2"), TIMEOUT, broadcast()], lambda: stop_event.set() or "")

    def connect(url, timeout):
        seen["url"], seen["timeout"] = url, timeout
        return socket

    listener = RealtimeWakeListener(SessionApi(), connect=connect)
    wakes = []
    wake_event = threading.Event()
    original_set = wake_event.set
    wake_event.set = lambda: (wakes.append(listener.joined), original_set())  # type: ignore[method-assign]
    run_listener(listener, stop_event, wake_event)

    assert seen["url"].startswith("wss://fixture.supabase.co/realtime/v1/websocket?")
    assert "apikey=publishable-fixture" in seen["url"] and "vsn=1.0.0" in seen["url"]
    joins = [message for message in socket.sent if message["event"] == "phx_join"]
    assert [message["topic"] for message in joins] == ["realtime:mt5-agent:any", f"realtime:{AGENT_TOPIC}"]
    assert all(message["payload"]["config"]["private"] is True for message in joins)
    assert all(message["payload"]["access_token"] == "header.payload.signature" for message in joins)
    # One recovery drain right after the join, one for the broadcast; both while joined.
    assert wakes == [True, True]
    assert socket.closed
    assert listener.joined is False


def test_ignores_other_events_and_other_topics():
    stop_event = threading.Event()
    frames = [
        reply("1"), reply("2"),
        broadcast(event="something_else"),
        broadcast(topic="realtime:public:noise"),
        json.dumps({"topic": "phoenix", "event": "phx_reply", "payload": {"status": "ok"}, "ref": "hb-9"}),
    ]
    socket = ScriptedSocket(frames, lambda: stop_event.set() or "")
    listener = RealtimeWakeListener(SessionApi(), connect=lambda url, timeout: socket)
    wake_event = threading.Event()
    calls = []
    original_set = wake_event.set
    wake_event.set = lambda: (calls.append("wake"), original_set())  # type: ignore[method-assign]
    run_listener(listener, stop_event, wake_event)

    assert calls == ["wake"]  # only the recovery drain after the join


@pytest.mark.parametrize(
    "frames",
    [
        [reply("1"), reply("2", status="error")],  # a private topic refused
        [reply("1"), reply("2"), json.dumps({"topic": "realtime:mt5-agent:any", "event": "phx_error", "payload": {}, "ref": None})],
        [reply("1"), reply("2"), json.dumps({"topic": f"realtime:{AGENT_TOPIC}", "event": "phx_close", "payload": {}, "ref": None})],
        [reply("1"), reply("2"), json.dumps({"topic": "realtime:mt5-agent:any", "event": "system", "payload": {"status": "error", "message": "Token has expired"}, "ref": None})],
        [reply("1"), reply("2"), ""],  # the server closed the socket
    ],
)
def test_a_dead_channel_clears_joined_and_reconnects_with_a_new_session(frames):
    stop_event = threading.Event()
    api = SessionApi()
    sockets = []
    joined_while_open = []

    def connect(url, timeout):
        if len(sockets) == 1:
            # Second connection: prove the first one released `joined`, then stop.
            joined_while_open.append(listener.joined)
            stop_event.set()
            return ScriptedSocket([], lambda: "")
        socket = ScriptedSocket(frames, lambda: "")
        sockets.append(socket)
        return socket

    listener = RealtimeWakeListener(api, connect=connect, reconnect_max_seconds=0.01)
    run_listener(listener, stop_event)

    assert api.session_calls == 2
    assert joined_while_open == [False]
    assert sockets[0].closed


def test_unanswered_heartbeats_mark_the_channel_dead():
    stop_event = threading.Event()
    clock = {"now": 0.0}

    def advance():
        clock["now"] += 30.0  # each idle receive lasts longer than a heartbeat interval
        raise websocket.WebSocketTimeoutException("idle")

    socket = ScriptedSocket([reply("1"), reply("2")], advance)
    connections = []

    def connect(url, timeout):
        connections.append(url)
        if len(connections) > 1:
            stop_event.set()
        return socket

    listener = RealtimeWakeListener(
        SessionApi(),
        connect=connect,
        heartbeat_seconds=25.0,
        reconnect_max_seconds=0.01,
        monotonic=lambda: clock["now"],
    )
    run_listener(listener, stop_event)

    heartbeats = [message for message in socket.sent if message["event"] == "heartbeat"]
    assert heartbeats and heartbeats[0]["topic"] == "phoenix"
    assert len(connections) == 2  # the silent socket was dropped and a new session requested


def test_answered_heartbeats_keep_the_channel_open():
    stop_event = threading.Event()
    clock = {"now": 0.0}
    state = {"idle": 0}
    socket = None

    def answer_or_stop():
        # Odd calls let a heartbeat interval pass; even calls answer the heartbeat just sent.
        state["idle"] += 1
        if state["idle"] > 12:
            stop_event.set()
            raise websocket.WebSocketTimeoutException("idle")
        if state["idle"] % 2:
            clock["now"] += 30.0
            raise websocket.WebSocketTimeoutException("idle")
        pending = [message for message in socket.sent if message["event"] == "heartbeat"]
        return json.dumps({"topic": "phoenix", "event": "phx_reply", "payload": {"status": "ok"}, "ref": pending[-1]["ref"]})

    socket = ScriptedSocket([reply("1"), reply("2")], answer_or_stop)
    api = SessionApi()
    listener = RealtimeWakeListener(api, connect=lambda url, timeout: socket, heartbeat_seconds=25.0, monotonic=lambda: clock["now"])
    run_listener(listener, stop_event)

    assert api.session_calls == 1
    assert len([message for message in socket.sent if message["event"] == "heartbeat"]) >= 5


def test_refreshes_the_session_before_the_token_expires():
    stop_event = threading.Event()
    api = SessionApi({**SESSION, "expires_at": "2026-10-21T12:00:00.000Z"})
    wall = {"now": 0.0}
    expiry = 1792584000.0  # 2026-10-21T12:00:00Z
    sockets = []

    def connect(url, timeout):
        if sockets:
            stop_event.set()
        wall["now"] = expiry - 301.0
        socket = ScriptedSocket([reply("1"), reply("2")], lambda: wall.__setitem__("now", expiry - 299.0) or (_ for _ in ()).throw(websocket.WebSocketTimeoutException("idle")))
        sockets.append(socket)
        return socket

    listener = RealtimeWakeListener(api, connect=connect, wall_clock=lambda: wall["now"])
    run_listener(listener, stop_event)

    assert api.session_calls == 2  # a planned refresh, not a failure back-off
    assert sockets[0].closed


def test_a_join_that_never_answers_times_out():
    stop_event = threading.Event()
    clock = {"now": 0.0}
    connections = []

    def silent():
        clock["now"] += 5.0
        raise websocket.WebSocketTimeoutException("idle")

    def connect(url, timeout):
        connections.append(url)
        if len(connections) > 1:
            stop_event.set()
        return ScriptedSocket([], silent)

    listener = RealtimeWakeListener(SessionApi(), connect=connect, reconnect_max_seconds=0.01, monotonic=lambda: clock["now"])
    run_listener(listener, stop_event)

    assert len(connections) == 2
    assert listener.joined is False


def test_a_session_failure_backs_off_without_joining():
    stop_event = threading.Event()

    class FailingApi:
        calls = 0

        def session(self):
            FailingApi.calls += 1
            if FailingApi.calls >= 3:
                stop_event.set()
            raise RuntimeError("realtime_not_configured")

    listener = RealtimeWakeListener(FailingApi(), connect=lambda url, timeout: pytest.fail("no session, no socket"), reconnect_max_seconds=0.01)
    wake_event = run_listener(listener, stop_event)

    assert FailingApi.calls == 3
    assert not wake_event.is_set()
    assert listener.joined is False
