"""Private Supabase Realtime wake-up channel for durable MT5 commands.

Broadcasts are intentionally hints only. The caller always drains the authoritative leased job
table through ``AgentApiClient.claim()`` after startup, reconnect, or ``command_available``.
"""

from __future__ import annotations

import json
import logging
import random
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urlencode

import websocket

logger = logging.getLogger(__name__)


class RealtimeWakeListener:
    def __init__(
        self,
        api: Any,
        connect: Callable[..., Any] = websocket.create_connection,
        heartbeat_seconds: float = 25.0,
        reconnect_max_seconds: float = 30.0,
    ) -> None:
        self._api = api
        self._connect = connect
        self._heartbeat_seconds = heartbeat_seconds
        self._reconnect_max_seconds = reconnect_max_seconds

    @staticmethod
    def _message(topic: str, event: str, payload: dict, ref: str) -> str:
        return json.dumps(
            {"topic": topic, "event": event, "payload": payload, "ref": ref},
            separators=(",", ":"),
        )

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
                )
            )
        return socket

    @staticmethod
    def _await_private_joins(socket: Any, topic_count: int, stop_event: threading.Event) -> None:
        joined: set[str] = set()
        while len(joined) < topic_count and not stop_event.is_set():
            try:
                message = json.loads(socket.recv())
            except websocket.WebSocketTimeoutException:
                continue
            if message.get("event") != "phx_reply":
                continue
            ref = str(message.get("ref", ""))
            payload = message.get("payload")
            if not isinstance(payload, dict) or payload.get("status") != "ok":
                raise ConnectionError("private Realtime topic authorization failed")
            if ref in {str(index) for index in range(1, topic_count + 1)}:
                joined.add(ref)
        if len(joined) != topic_count:
            raise ConnectionError("Realtime stopped before private topics joined")

    def run(self, wake_event: threading.Event, stop_event: threading.Event) -> None:
        failures = 0
        while not stop_event.is_set():
            socket = None
            try:
                session = self._api.session()
                expires_at = datetime.fromisoformat(session["expires_at"].replace("Z", "+00:00"))
                socket = self._connect_session(session)
                self._await_private_joins(socket, len(session["topics"]), stop_event)
                failures = 0
                wake_event.set()  # one recovery claim after every successful (re)connect
                last_heartbeat = time.monotonic()
                while not stop_event.is_set():
                    if datetime.now(timezone.utc).timestamp() >= expires_at.timestamp() - 300:
                        break
                    if time.monotonic() - last_heartbeat >= self._heartbeat_seconds:
                        socket.send(self._message("phoenix", "heartbeat", {}, "heartbeat"))
                        last_heartbeat = time.monotonic()
                    try:
                        raw = socket.recv()
                    except websocket.WebSocketTimeoutException:
                        continue
                    if not raw:
                        raise ConnectionError("Realtime socket closed")
                    message = json.loads(raw)
                    if (
                        message.get("event") == "broadcast"
                        and isinstance(message.get("payload"), dict)
                        and message["payload"].get("event") == "command_available"
                    ):
                        wake_event.set()
            except Exception as exc:
                failures += 1
                logger.warning("Realtime wake channel unavailable (error=%s)", type(exc).__name__)
            finally:
                if socket is not None:
                    try:
                        socket.close()
                    except Exception:
                        pass
            if stop_event.is_set():
                return
            base = min(2 ** min(failures - 1, 5), self._reconnect_max_seconds)
            stop_event.wait(base + random.uniform(0, base * 0.25))
