"""E2E trade-sync su socket reali.

La catena sotto usa il fake bridge come vero server HTTP, il client bridge di produzione,
event_detector/event_normalizer reali e EventSender verso una ingestion HTTP finta. Non viene
mockata nessuna delle due connessioni di rete e nessun componente market-data e' coinvolto.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from bridge_mt5_client import BridgeMt5Client
from event_detector import detect_events
from event_normalizer import normalize_event
from event_sender import EventSender
from snapshot_store import SnapshotStore


EXPECTED_EVENT_TYPES = [
    "trade_opened",
    "trade_modified",
    "trade_modified",
    "trade_closed",
    "pending_order_created",
    "pending_order_modified",
    "pending_order_cancelled",
]


class _IngestionServer:
    def __init__(self, token: str) -> None:
        self.token = token
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return None

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0") or "0")
                raw = self.rfile.read(length) if length else b"{}"
                outer.requests.append({
                    "path": self.path,
                    "authorization": self.headers.get("Authorization"),
                    "payload": json.loads(raw.decode("utf-8")),
                })
                body = b'{"ok":true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}/api/mt5-events"

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *_exc):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


def test_full_trade_sync_path_emits_exactly_seven_events(fake_bridge_server, tmp_path):
    bridge_url, bridge_token = fake_bridge_server
    ingestion_token = "test-ingestion-token-distinct-from-bridge"
    snapshot_path = tmp_path / "snapshot.json"

    client = BridgeMt5Client(
        bridge_url=bridge_url,
        bridge_token=bridge_token,
        timeout_seconds=2.0,
        max_retries=2,
        backoff_base_seconds=0.0,
        sleep_fn=lambda _seconds: None,
    )
    client.connect()
    store = SnapshotStore(str(snapshot_path))

    with _IngestionServer(ingestion_token) as ingestion:
        sender = EventSender(
            api_url=ingestion.url,
            bridge_token=ingestion_token,
            dry_run=False,
            max_retries=2,
            backoff_base_seconds=0.0,
            sleep_fn=lambda _seconds: None,
        )

        # Stato 0 iniziale + sette transizioni: il primo snapshot stabilisce la baseline e le
        # successive sette chiamate devono generare esattamente un evento ciascuna.
        for _ in range(8):
            current = client.snapshot()
            account = client.account_info()  # deve usare la cache dello stesso snapshot
            for raw_event in detect_events(store.get(), current):
                payload = normalize_event(raw_event, account["login"], account["server"])
                result = sender.send(payload)
                assert result.status == "sent"
            store.update(current)

    payloads = [request["payload"] for request in ingestion.requests]
    assert [payload["event_type"] for payload in payloads] == EXPECTED_EVENT_TYPES
    assert len(payloads) == 7
    assert len({payload["event_id"] for payload in payloads}) == 7
    assert all(request["path"] == "/api/mt5-events" for request in ingestion.requests)
    assert all(
        request["authorization"] == f"Bearer {ingestion_token}"
        for request in ingestion.requests
    )
    assert all(payload["account_number"] == "123456" for payload in payloads)
    assert all(payload["server"] == "FakeBridge-Demo" for payload in payloads)

    # Il file persistente contiene l'ultimo stato: ricaricarlo simula un riavvio del worker e
    # non deve produrre eventi se il bridge restituisce ancora lo stesso snapshot finale.
    restarted_store = SnapshotStore(str(snapshot_path))
    final_snapshot = client.snapshot()
    assert detect_events(restarted_store.get(), final_snapshot) == []


def test_event_ids_are_stable_when_the_same_transition_is_reprocessed(fake_bridge_server):
    bridge_url, bridge_token = fake_bridge_server
    client = BridgeMt5Client(
        bridge_url=bridge_url,
        bridge_token=bridge_token,
        timeout_seconds=2.0,
        backoff_base_seconds=0.0,
        sleep_fn=lambda _seconds: None,
    )
    client.connect()

    empty = client.snapshot()
    opened = client.snapshot()
    raw_event = detect_events(empty, opened)[0]
    account = client.account_info()

    first = normalize_event(raw_event, account["login"], account["server"])
    second = normalize_event(raw_event, account["login"], account["server"])
    assert first["event_id"] == second["event_id"]
