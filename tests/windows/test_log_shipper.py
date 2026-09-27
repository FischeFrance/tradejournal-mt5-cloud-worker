from __future__ import annotations

import logging
from pathlib import Path

from windows_agent.observability.log_shipper import (
    LokiLogHandler,
    _stream_labels,
    install_loki_handler,
    load_loki_credentials,
)
from windows_agent.provisioning.secret_store import WindowsSecretStore


class _FakeResponse:
    def __init__(self, status_code: int = 204) -> None:
        self.status_code = status_code


class _FakeSession:
    def __init__(self, status_code: int = 204, exception: Exception | None = None) -> None:
        self.status_code = status_code
        self.exception = exception
        self.calls: list[dict] = []

    def post(self, url, *, data, headers, auth, timeout):
        self.calls.append(
            {"url": url, "data": data, "headers": headers, "auth": auth, "timeout": timeout}
        )
        if self.exception is not None:
            raise self.exception
        return _FakeResponse(self.status_code)


def _record(message: str, connection_id: str | None = None) -> logging.LogRecord:
    record = logging.LogRecord(
        name="mt5_worker.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )
    if connection_id is not None:
        record.connection_id = connection_id
    return record


def _handler_without_thread(session: _FakeSession) -> LokiLogHandler:
    # The background thread is irrelevant to these tests -- they call _send() directly to stay
    # deterministic. Stop the thread immediately so it never races with the assertions below.
    handler = LokiLogHandler("https://logs.example/", "12345", "secret-token", session=session)
    handler.close()
    return handler


def test_stream_labels_include_connection_id_only_when_present():
    with_connection = _stream_labels(_record("hello", connection_id="conn-1"))
    without_connection = _stream_labels(_record("hello"))

    assert with_connection["connection_id"] == "conn-1"
    assert "connection_id" not in without_connection
    assert with_connection["service"] == "tradejournal-mt5-agent"
    assert with_connection["level"] == "info"


def test_send_posts_batched_streams_grouped_by_label_set():
    session = _FakeSession()
    handler = _handler_without_thread(session)
    handler.setFormatter(logging.Formatter("%(message)s"))

    handler._send(
        [
            _record("first", connection_id="conn-1"),
            _record("second", connection_id="conn-1"),
            _record("third", connection_id="conn-2"),
        ]
    )

    assert len(session.calls) == 1
    call = session.calls[0]
    assert call["url"] == "https://logs.example/loki/api/v1/push"
    assert call["auth"] == ("12345", "secret-token")
    import json

    payload = json.loads(call["data"])
    streams = {tuple(sorted(s["stream"].items())): s["values"] for s in payload["streams"]}
    conn1_key = next(key for key in streams if ("connection_id", "conn-1") in key)
    conn2_key = next(key for key in streams if ("connection_id", "conn-2") in key)
    assert [v[1] for v in streams[conn1_key]] == ["first", "second"]
    assert [v[1] for v in streams[conn2_key]] == ["third"]


def test_send_swallows_http_errors_without_raising():
    session = _FakeSession(exception=ConnectionError("unreachable"))
    handler = _handler_without_thread(session)
    handler.setFormatter(logging.Formatter("%(message)s"))

    # Must not raise: a Loki outage can never propagate into the caller.
    handler._send([_record("hello")])


def test_send_logs_but_does_not_raise_on_non_2xx_status():
    session = _FakeSession(status_code=429)
    handler = _handler_without_thread(session)
    handler.setFormatter(logging.Formatter("%(message)s"))

    handler._send([_record("hello")])


def test_emit_drops_records_when_queue_is_full_instead_of_blocking():
    session = _FakeSession()
    handler = LokiLogHandler(
        "https://logs.example/", "12345", "secret-token", session=session
    )
    handler.close()  # stop the background thread; we only exercise emit()/the queue here.
    for _ in range(6000):
        handler.emit(_record("spam"))
    # Must not have blocked or raised despite exceeding _MAX_QUEUE_SIZE (5000).
    assert handler._queue.full()


def test_load_loki_credentials_returns_none_when_never_provisioned(tmp_path: Path):
    assert load_loki_credentials(tmp_path) is None


def test_load_loki_credentials_reads_all_three_secrets(tmp_path: Path, monkeypatch):
    stored: dict[tuple[str, str], str] = {}

    def fake_read(self, connection_id, name):
        try:
            return stored[(connection_id, name)]
        except KeyError:
            raise FileNotFoundError(name)

    monkeypatch.setattr(WindowsSecretStore, "read", fake_read)
    from windows_agent.agent_secrets import AGENT_SCOPE_ID

    stored[(AGENT_SCOPE_ID, "grafana_loki_url")] = "https://logs.example/"
    stored[(AGENT_SCOPE_ID, "grafana_loki_user")] = "12345"
    stored[(AGENT_SCOPE_ID, "grafana_loki_token")] = "secret-token"

    result = load_loki_credentials(tmp_path)

    assert result == ("https://logs.example/", "12345", "secret-token")


def test_install_loki_handler_is_a_noop_when_unconfigured(tmp_path: Path):
    root_logger = logging.getLogger()
    before = list(root_logger.handlers)

    handler = install_loki_handler(tmp_path)

    assert handler is None
    assert root_logger.handlers == before


def test_install_loki_handler_attaches_and_returns_handler(tmp_path: Path, monkeypatch):
    def fake_read(self, connection_id, name):
        return {
            "grafana_loki_url": "https://logs.example/",
            "grafana_loki_user": "12345",
            "grafana_loki_token": "secret-token",
        }[name]

    monkeypatch.setattr(WindowsSecretStore, "read", fake_read)

    handler = install_loki_handler(tmp_path)
    try:
        assert handler is not None
        assert handler in logging.getLogger().handlers
    finally:
        if handler is not None:
            logging.getLogger().removeHandler(handler)
            handler.close()
