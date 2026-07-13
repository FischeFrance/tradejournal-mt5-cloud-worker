"""Lifecycle di arresto del bridge Windows, senza Wine, socket o segnali reali."""

from __future__ import annotations

import signal
import threading
from types import SimpleNamespace

import pytest

from windows import mt5_bridge


class _FakeSession:
    def __init__(self) -> None:
        self.shutdown_calls = 0

    def shutdown(self) -> None:
        self.shutdown_calls += 1


class _SignalDrivenServer:
    """Fake che riproduce il vincolo di ``HTTPServer.shutdown()``.

    ``shutdown()`` attende che ``serve_forever()`` possa terminare. Se il signal
    handler lo chiamasse direttamente nello stesso thread del serve loop, il fake
    rileverebbe il thread errato senza bloccarsi per sempre; il test fallirebbe
    sull'identita' del thread.
    """

    def __init__(
        self,
        registered_handlers: dict[int, object],
        signals_to_deliver: tuple[int, ...],
    ) -> None:
        self._registered_handlers = registered_handlers
        self._signals_to_deliver = signals_to_deliver
        self._serve_loop_can_exit = threading.Event()
        self.shutdown_started = threading.Event()
        self.shutdown_finished = threading.Event()
        self.handler_returned = threading.Event()
        self.serve_forever_returned = threading.Event()
        self.serve_thread_id: int | None = None
        self.shutdown_thread_ids: list[int] = []
        self.shutdown_calls = 0
        self.server_close_calls = 0
        self.server_address = ("127.0.0.1", 8090)

    def serve_forever(self) -> None:
        self.serve_thread_id = threading.get_ident()

        first_handler = self._registered_handlers[self._signals_to_deliver[0]]
        first_handler(self._signals_to_deliver[0], None)
        self.handler_returned.set()

        # Il primo shutdown deve essere gia' partito e deve poter restare in
        # attesa dell'uscita dal serve loop senza bloccare il signal handler.
        assert self.shutdown_started.wait(timeout=2), "shutdown HTTP non avviato dal segnale"

        for signum in self._signals_to_deliver[1:]:
            handler = self._registered_handlers[signum]
            handler(signum, None)

        self._serve_loop_can_exit.set()
        self.serve_forever_returned.set()

    def shutdown(self) -> None:
        self.shutdown_calls += 1
        self.shutdown_thread_ids.append(threading.get_ident())
        self.shutdown_started.set()

        # Una chiamata sincrona dal serve thread e' il bug sotto test. Non la
        # blocchiamo indefinitamente: il thread id registrato rende il fallimento
        # deterministico e mantiene la suite sempre terminabile.
        if threading.get_ident() != self.serve_thread_id:
            if not self._serve_loop_can_exit.wait(timeout=2):
                raise AssertionError("serve_forever non e' terminato dopo il segnale")
        self.shutdown_finished.set()

    def server_close(self) -> None:
        self.server_close_calls += 1


def _capture_signal_handlers(monkeypatch) -> dict[int, object]:
    handlers: dict[int, object] = {}

    def _record(signum, handler):
        handlers[signum] = handler

    monkeypatch.setattr(mt5_bridge.signal, "signal", _record)
    return handlers


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_signal_stops_server_without_running_shutdown_on_serve_thread(monkeypatch, signum):
    handlers = _capture_signal_handlers(monkeypatch)
    session = _FakeSession()
    server = _SignalDrivenServer(handlers, (signum,))

    mt5_bridge._serve_server(server, session)

    assert signal.SIGINT in handlers
    assert signal.SIGTERM in handlers
    assert server.handler_returned.is_set()
    assert server.serve_forever_returned.is_set()
    assert server.shutdown_finished.wait(timeout=2)
    assert server.shutdown_calls == 1
    assert len(server.shutdown_thread_ids) == 1
    assert server.shutdown_thread_ids[0] != server.serve_thread_id
    assert server.server_close_calls == 1
    assert session.shutdown_calls == 1


def test_repeated_signals_start_only_one_shutdown_and_lifecycle_terminates(monkeypatch):
    handlers = _capture_signal_handlers(monkeypatch)
    session = _FakeSession()
    server = _SignalDrivenServer(
        handlers,
        (signal.SIGINT, signal.SIGTERM, signal.SIGINT),
    )

    mt5_bridge._serve_server(server, session)

    assert server.shutdown_finished.wait(timeout=2)
    assert server.shutdown_calls == 1
    assert server.server_close_calls == 1
    assert session.shutdown_calls == 1
    assert server.serve_forever_returned.is_set()


def test_mt5_session_shutdown_is_idempotent():
    fake_mt5 = SimpleNamespace(shutdown_calls=0)

    def _shutdown() -> None:
        fake_mt5.shutdown_calls += 1

    fake_mt5.shutdown = _shutdown
    session = mt5_bridge._Mt5Session(SimpleNamespace())
    session._mt5 = fake_mt5
    session._connected = True

    session.shutdown()
    session.shutdown()

    assert fake_mt5.shutdown_calls == 1
    assert session._connected is False
