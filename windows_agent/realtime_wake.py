"""Private Supabase Realtime wake-up channel for durable MT5 commands.

A broadcast is only a hint: the daemon still drains the leased job table through
``AgentApiClient.claim()`` after every successful (re)connect and every ``command_available``.
``joined`` tells the daemon whether the hint can be trusted. While it is false (Realtime down, a
private topic refused, a heartbeat unanswered) the daemon keeps its short claim poll, so an
outage of the channel never delays a job.
"""

from __future__ import annotations

import json
import logging
import random
import threading
import time
from datetime import datetime
from typing import Any, Callable
from urllib.parse import urlencode

import websocket

logger = logging.getLogger(__name__)

# A new session is requested this long before the one-hour Realtime JWT expires.
SESSION_REFRESH_MARGIN_SECONDS = 300.0
JOIN_TIMEOUT_SECONDS = 15.0


def _session_expiry(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


class RealtimeWakeListener:
    def __init__(
        self,
        api: Any,
        connect: Callable[..., Any] = websocket.create_connection,
        heartbeat_seconds: float = 25.0,
        reconnect_max_seconds: float = 300.0,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._api = api
        self._connect = connect
        self._heartbeat_seconds = heartbeat_seconds
        self._reconnect_max_seconds = reconnect_max_seconds
        self._monotonic = monotonic
        self._wall_clock = wall_clock
        self._joined = threading.Event()

    @property
    def joined(self) -> bool:
        """Both private topics are joined and the socket answers heartbeats."""
        return self._joined.is_set()

    @staticmethod
    def _message(topic: str, event: str, payload: dict, ref: str, join_ref: str | None = None) -> str:
        message: dict[str, Any] = {"topic": topic, "event": event, "payload": payload, "ref": ref}
        if join_ref is not None:
            message["join_ref"] = join_ref
        return json.dumps(message, separators=(",", ":"))

    def _connect_session(self, session: dict) -> Any:
        query = urlencode({"apikey": session["publishable_key"], "vsn": "1.0.0"})
        socket = self._connect(f"{session['realtime_url']}?{query}", timeout=1.0)
        for index, topic in enumerate(session["topics"], start=1):
            socket.send(
                self._message(
                    f"realtime:{topic}",
                    "phx_join",
                    {
                        "config": {
                            "private": True,
                            "broadcast": {"ack": False, "self": False},
                            "presence": {"enabled": False},
                        },
                        "access_token": session["access_token"],
                    },
                    str(index),
                    join_ref=str(index),
                )
            )
        return socket

    def _await_private_joins(self, socket: Any, topic_count: int, stop_event: threading.Event) -> None:
        expected = {str(index) for index in range(1, topic_count + 1)}
        joined: set[str] = set()
        deadline = self._monotonic() + JOIN_TIMEOUT_SECONDS
        while joined != expected and not stop_event.is_set():
            if self._monotonic() >= deadline:
                raise TimeoutError("private Realtime topics did not answer the join")
            try:
                raw = socket.recv()
            except websocket.WebSocketTimeoutException:
                continue
            if not raw:
                raise ConnectionError("Realtime socket closed during join")
            message = json.loads(raw)
            ref = str(message.get("ref", ""))
            if message.get("event") != "phx_reply" or ref not in expected:
                continue
            payload = message.get("payload")
            if not isinstance(payload, dict) or payload.get("status") != "ok":
                raise ConnectionError("private Realtime topic authorization failed")
            joined.add(ref)

    def _listen(
        self,
        socket: Any,
        topics: set[str],
        expires_at: float,
        wake_event: threading.Event,
        stop_event: threading.Event,
    ) -> None:
        """Wake on ``command_available`` until the session nears expiry. Raises on any sign of a
        dead channel so ``joined`` never claims a subscription that no longer delivers."""
        heartbeat_count = 0
        last_heartbeat = self._monotonic()
        unanswered_ref: str | None = None
        unanswered_since = 0.0
        while not stop_event.is_set():
            if self._wall_clock() >= expires_at - SESSION_REFRESH_MARGIN_SECONDS:
                return
            now = self._monotonic()
            if unanswered_ref is not None and now - unanswered_since >= 2 * self._heartbeat_seconds:
                raise TimeoutError("Realtime heartbeat unanswered")
            if now - last_heartbeat >= self._heartbeat_seconds:
                heartbeat_count += 1
                ref = f"hb-{heartbeat_count}"
                socket.send(self._message("phoenix", "heartbeat", {}, ref))
                last_heartbeat = now
                if unanswered_ref is None:
                    unanswered_ref, unanswered_since = ref, now
            try:
                raw = socket.recv()
            except websocket.WebSocketTimeoutException:
                continue
            if not raw:
                raise ConnectionError("Realtime socket closed")
            message = json.loads(raw)
            event = message.get("event")
            topic = message.get("topic")
            payload = message.get("payload")
            if event == "phx_reply" and topic == "phoenix":
                if unanswered_ref is not None and str(message.get("ref")) == unanswered_ref:
                    unanswered_ref = None
                continue
            if topic in topics and event in ("phx_error", "phx_close"):
                raise ConnectionError(f"Realtime channel {event}")
            if (
                topic in topics
                and event == "system"
                and isinstance(payload, dict)
                and payload.get("status") == "error"
            ):
                raise ConnectionError("Realtime channel reported an error")
            if (
                topic in topics
                and event == "broadcast"
                and isinstance(payload, dict)
                and payload.get("event") == "command_available"
            ):
                wake_event.set()

    def run(self, wake_event: threading.Event, stop_event: threading.Event) -> None:
        failures = 0
        while not stop_event.is_set():
            socket = None
            try:
                session = self._api.session()
                expires_at = _session_expiry(session["expires_at"])
                topics = [str(topic) for topic in session["topics"]]
                socket = self._connect_session(session)
                self._await_private_joins(socket, len(topics), stop_event)
                if stop_event.is_set():
                    return
                failures = 0
                self._joined.set()
                logger.info("Realtime wake channel joined (%d private topics)", len(topics))
                wake_event.set()  # one recovery drain after every successful (re)connect
                self._listen(
                    socket,
                    {f"realtime:{topic}" for topic in topics},
                    expires_at,
                    wake_event,
                    stop_event,
                )
            except Exception as exc:
                failures += 1
                logger.warning("Realtime wake channel unavailable (error=%s)", type(exc).__name__)
            finally:
                self._joined.clear()
                if socket is not None:
                    try:
                        socket.close()
                    except Exception:
                        pass
            if stop_event.is_set():
                return
            # A planned session refresh reconnects at once; failures back off up to five minutes,
            # so a broken channel costs a few session requests an hour while polling covers jobs.
            delay = 0.0 if failures == 0 else min(2.0 ** min(failures - 1, 9), self._reconnect_max_seconds)
            stop_event.wait(delay + random.uniform(0, max(delay, 1.0) * 0.25))
