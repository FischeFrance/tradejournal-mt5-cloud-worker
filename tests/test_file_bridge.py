"""Test del contratto HTTP di bridge/files/file_bridge.py: un vero server HTTP in ascolto su
127.0.0.1 (fixture file_bridge_factory, vedi tests/conftest.py), interrogato con richieste HTTP
reali (requests). Nessun Wine/MT5 richiesto: i file che l'EA (mt5/experts/TradeJournalBridge.mq5)
scriverebbe sono scritti direttamente dai test in una directory temporanea."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import requests


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _write_json(path, payload) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_heartbeat(files_dir, *, age_seconds: float = 0.0, terminal_connected: bool = True) -> None:
    generated_at = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    _write_json(
        files_dir / "heartbeat.json",
        {"generated_at": _iso(generated_at), "sequence": 1, "terminal_connected": terminal_connected},
    )


def _default_account() -> dict:
    return {
        "login": "12345678",
        "server": "Broker-Demo",
        "balance": 10000.0,
        "equity": 10010.0,
        "currency": "EUR",
        "leverage": 100,
    }


def _write_ready_state(files_dir, *, positions=None, orders=None, candles=None) -> None:
    _write_json(files_dir / "account.json", _default_account())
    _write_json(files_dir / "positions.json", positions if positions is not None else [])
    _write_json(files_dir / "orders.json", orders if orders is not None else [])
    _write_json(
        files_dir / "candles.json",
        candles if candles is not None else {"EURUSD": {tf: [] for tf in ("M1", "M5", "M15", "H1", "H4", "D1")}},
    )
    (files_dir / "events.jsonl").touch()
    _write_heartbeat(files_dir)


def _deal_event(
    deal_ticket: str,
    position_ticket: str,
    *,
    close_time: str,
    profit: float = 20.0,
    connection_id: str = "test-connection-id",
    login: str = "12345678",
    server: str = "Broker-Demo",
    timestamp_msc: int = 0,
) -> dict:
    return {
        "event_id": f"{connection_id}|{login}|{server}|DEAL_ADD|{deal_ticket}|{timestamp_msc}|1",
        "connection_id": connection_id,
        "login": login,
        "server": server,
        "timestamp_msc": timestamp_msc,
        "event_type": "DEAL_ADD",
        "ticket": deal_ticket,
        "position_id": position_ticket,
        "order_id": None,
        "deal_id": deal_ticket,
        "symbol": "EURUSD",
        "direction": "sell",
        "volume": 0.1,
        "price": 1.172,
        "stop_loss": 0.0,
        "take_profit": 0.0,
        "profit": profit,
        "commission": -0.5,
        "swap": 0.0,
        "magic": 0,
        "comment": "",
        "entry": "OUT",
        "time": close_time,
    }


def _append_events(files_dir, *events: dict) -> None:
    with open(files_dir / "events.jsonl", "a", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event) + "\n")


# ---------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------


def test_health_before_ea_ever_ran_is_degraded(file_bridge_factory):
    base_url, token, _files_dir = file_bridge_factory()
    response = requests.get(f"{base_url}/health", headers=_headers(token))
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert body["terminal_connected"] is False
    assert body["account_connected"] is False
    assert body["server"] == "<vuoto>"
    assert body["version"] == "file-bridge/1.0"


def test_health_requires_auth(file_bridge_factory):
    base_url, _token, _files_dir = file_bridge_factory()
    response = requests.get(f"{base_url}/health")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_health_ok_once_heartbeat_and_account_are_fresh(file_bridge_factory):
    base_url, token, files_dir = file_bridge_factory()
    _write_ready_state(files_dir)

    response = requests.get(f"{base_url}/health", headers=_headers(token))
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["terminal_connected"] is True
    assert body["account_connected"] is True
    assert body["server"] != "Broker-Demo"  # mascherato in /health, a differenza dello snapshot


def test_health_is_degraded_when_heartbeat_expired(file_bridge_factory):
    base_url, token, files_dir = file_bridge_factory(heartbeat_max_age_seconds=1.0)
    _write_ready_state(files_dir)
    _write_heartbeat(files_dir, age_seconds=5.0)  # ben oltre il max_age di 1s configurato

    response = requests.get(f"{base_url}/health", headers=_headers(token))
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert body["terminal_connected"] is False


# ---------------------------------------------------------------------------
# POST /v1/trading/snapshot
# ---------------------------------------------------------------------------


def test_snapshot_wrong_token_is_401(file_bridge_factory):
    base_url, _token, files_dir = file_bridge_factory()
    _write_ready_state(files_dir)
    response = requests.post(
        f"{base_url}/v1/trading/snapshot",
        json={},
        headers=_headers("wrong-token"),
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_snapshot_returns_503_when_heartbeat_missing(file_bridge_factory):
    base_url, token, _files_dir = file_bridge_factory()
    response = requests.post(f"{base_url}/v1/trading/snapshot", json={}, headers=_headers(token))
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "mt5_not_connected"


def test_snapshot_happy_path_shape(file_bridge_factory):
    base_url, token, files_dir = file_bridge_factory()
    position = {
        "ticket": "10001",
        "symbol": "EURUSD",
        "direction": "buy",
        "volume": 0.1,
        "open_price": 1.17,
        "stop_loss": 1.168,
        "take_profit": 1.174,
        "open_time": "2026-07-13T10:00:00Z",
    }
    _write_ready_state(files_dir, positions=[position])
    now = datetime.now(timezone.utc)
    _append_events(files_dir, _deal_event("30001", "10001", close_time=_iso(now - timedelta(minutes=5))))

    response = requests.post(
        f"{base_url}/v1/trading/snapshot",
        json={"deal_lookback_hours": 24},
        headers=_headers(token),
    )
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"account", "positions", "orders", "deals", "generated_at"}
    assert body["account"]["login"] == "12345678"
    assert body["account"]["server"] == "Broker-Demo"  # non mascherato nello snapshot
    assert body["positions"] == [position]
    assert body["orders"] == []
    assert len(body["deals"]) == 1
    deal = body["deals"][0]
    assert deal["deal_ticket"] == "30001"
    assert deal["position_ticket"] == "10001"
    assert deal["profit"] == 20.0
    assert body["generated_at"].endswith("Z")


def test_snapshot_excludes_deals_outside_lookback_window(file_bridge_factory):
    base_url, token, files_dir = file_bridge_factory()
    _write_ready_state(files_dir)
    now = datetime.now(timezone.utc)
    _append_events(files_dir, _deal_event("40001", "20001", close_time=_iso(now - timedelta(hours=48))))

    response = requests.post(
        f"{base_url}/v1/trading/snapshot",
        json={"deal_lookback_hours": 24},
        headers=_headers(token),
    )
    assert response.status_code == 200
    assert response.json()["deals"] == []


def test_snapshot_ignores_partial_incomplete_account_file(file_bridge_factory):
    base_url, token, files_dir = file_bridge_factory()
    _write_ready_state(files_dir)
    # Simula una scrittura interrotta: JSON troncato, come potrebbe capitare leggendo un file
    # nel mezzo di un aggiornamento non ancora reso atomico (mai il caso reale con
    # WriteJsonAtomic lato EA, ma il bridge deve restare tollerante comunque).
    (files_dir / "account.json").write_text('{"login": "12345678", "serv', encoding="utf-8")

    response = requests.post(f"{base_url}/v1/trading/snapshot", json={}, headers=_headers(token))
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "mt5_not_connected"


def test_snapshot_ignores_stray_tmp_file_never_renamed(file_bridge_factory):
    """Un file '.tmp' lasciato da una scrittura mai completata (rename non ancora avvenuto, o
    processo interrotto a meta') non deve mai essere confuso col file finale: il bridge legge
    solo account.json, mai account.json.tmp."""
    base_url, token, files_dir = file_bridge_factory()
    _write_json(files_dir / "account.json.tmp", _default_account())
    _write_heartbeat(files_dir)

    response = requests.post(f"{base_url}/v1/trading/snapshot", json={}, headers=_headers(token))
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "mt5_not_connected"


def test_snapshot_deduplicates_repeated_deal_events(file_bridge_factory):
    """Lo stesso deal_ticket notificato due volte (es. backfill rieseguito dopo un riavvio
    dell'EA) non deve mai produrre due voci nell'array deals: l'ultima vince."""
    base_url, token, files_dir = file_bridge_factory()
    _write_ready_state(files_dir)
    now = datetime.now(timezone.utc)
    close_time = _iso(now - timedelta(minutes=5))
    _append_events(
        files_dir,
        _deal_event("50001", "60001", close_time=close_time, profit=10.0),
        _deal_event("50001", "60001", close_time=close_time, profit=10.0),
    )

    response = requests.post(
        f"{base_url}/v1/trading/snapshot",
        json={"deal_lookback_hours": 24},
        headers=_headers(token),
    )
    deals = response.json()["deals"]
    assert len(deals) == 1
    assert deals[0]["deal_ticket"] == "50001"


def test_snapshot_cursor_persists_across_new_reader_instances(file_bridge_factory):
    """Il cursore di lettura incrementale (offset + indice deal) e' persistito su disco: un nuovo
    processo bridge puntato sulla stessa directory (qui simulato con un secondo server sulla
    stessa cartella) recupera i deal gia' visti senza dover ri-scansionare events.jsonl da zero
    e senza perdere quelli accumulati in precedenza."""
    base_url, token, files_dir = file_bridge_factory()
    _write_ready_state(files_dir)
    now = datetime.now(timezone.utc)
    _append_events(files_dir, _deal_event("70001", "80001", close_time=_iso(now - timedelta(minutes=1))))

    first = requests.post(
        f"{base_url}/v1/trading/snapshot", json={"deal_lookback_hours": 24}, headers=_headers(token)
    )
    assert len(first.json()["deals"]) == 1
    assert (files_dir / "bridge_cursor.json").exists()

    # Nuovo server (nuova FileSnapshotSource/_EventsCursor) sulla STESSA directory: deve
    # ripartire dal cursore persistito, non da zero.
    base_url_2, token_2, _ = file_bridge_factory(base_dir=files_dir)
    second = requests.post(
        f"{base_url_2}/v1/trading/snapshot", json={"deal_lookback_hours": 24}, headers=_headers(token_2)
    )
    deals = second.json()["deals"]
    assert len(deals) == 1
    assert deals[0]["deal_ticket"] == "70001"


# ---------------------------------------------------------------------------
# POST /v1/candles
# ---------------------------------------------------------------------------


def test_candles_wrong_token_is_401(file_bridge_factory):
    base_url, _token, files_dir = file_bridge_factory()
    _write_ready_state(files_dir)
    response = requests.post(
        f"{base_url}/v1/candles",
        json={"symbol": "EURUSD", "timeframe": "M1", "since": None, "limit": 5},
        headers=_headers("wrong-token"),
    )
    assert response.status_code == 401


def test_candles_happy_path_and_since_exclusive(file_bridge_factory):
    base_url, token, files_dir = file_bridge_factory()
    candle_1 = {
        "open_time": "2026-07-13T10:00:00Z", "open": "1.17000", "high": "1.17050",
        "low": "1.16990", "close": "1.17010", "tick_volume": 100, "spread": 8, "source": "mt5",
    }
    candle_2 = {
        "open_time": "2026-07-13T10:01:00Z", "open": "1.17010", "high": "1.17060",
        "low": "1.17000", "close": "1.17020", "tick_volume": 90, "spread": 8, "source": "mt5",
    }
    _write_ready_state(files_dir, candles={"EURUSD": {"M1": [candle_1, candle_2]}})

    response = requests.post(
        f"{base_url}/v1/candles",
        json={"symbol": "EURUSD", "timeframe": "M1", "since": None, "limit": 10},
        headers={**_headers(token), "X-Mt5-Bridge-Now-Override": "2026-07-13T10:05:00Z"},
    )
    assert response.status_code == 200
    body = response.json()
    assert [c["open_time"] for c in body["candles"]] == [candle_1["open_time"], candle_2["open_time"]]
    assert isinstance(body["candles"][0]["open"], str)

    since_response = requests.post(
        f"{base_url}/v1/candles",
        json={"symbol": "EURUSD", "timeframe": "M1", "since": candle_1["open_time"], "limit": 10},
        headers={**_headers(token), "X-Mt5-Bridge-Now-Override": "2026-07-13T10:05:00Z"},
    )
    assert [c["open_time"] for c in since_response.json()["candles"]] == [candle_2["open_time"]]


def test_candles_excludes_forming_candle(file_bridge_factory):
    base_url, token, files_dir = file_bridge_factory()
    forming = {
        "open_time": "2026-07-13T10:04:30Z", "open": "1.17000", "high": "1.17010",
        "low": "1.16990", "close": "1.17005", "tick_volume": 5, "spread": 8, "source": "mt5",
    }
    _write_ready_state(files_dir, candles={"EURUSD": {"M1": [forming]}})

    response = requests.post(
        f"{base_url}/v1/candles",
        json={"symbol": "EURUSD", "timeframe": "M1", "since": None, "limit": 10},
        headers={**_headers(token), "X-Mt5-Bridge-Now-Override": "2026-07-13T10:04:45Z"},
    )
    assert response.json()["candles"] == []


def test_candles_unsupported_symbol_is_422(file_bridge_factory):
    base_url, token, files_dir = file_bridge_factory(broker_symbol="EURUSD")
    _write_ready_state(files_dir)
    response = requests.post(
        f"{base_url}/v1/candles",
        json={"symbol": "GBPUSD", "timeframe": "M1", "since": None, "limit": 5},
        headers=_headers(token),
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "unsupported_symbol"


def test_candles_not_yet_published_by_ea_is_503(file_bridge_factory):
    base_url, token, files_dir = file_bridge_factory()
    files_dir.mkdir(parents=True, exist_ok=True)
    response = requests.post(
        f"{base_url}/v1/candles",
        json={"symbol": "EURUSD", "timeframe": "M1", "since": None, "limit": 5},
        headers=_headers(token),
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "mt5_not_connected"


# ---------------------------------------------------------------------------
# hardening: verifica obbligatoria dell'identita' dell'account
# ---------------------------------------------------------------------------


def test_health_degraded_when_account_login_does_not_match_expected(file_bridge_factory):
    base_url, token, files_dir = file_bridge_factory(expected_login="99999999")
    _write_ready_state(files_dir)  # account.json ha login "12345678" (default _default_account)

    response = requests.get(f"{base_url}/health", headers=_headers(token))
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert body["account_connected"] is False


def test_health_degraded_when_account_server_does_not_match_expected(file_bridge_factory):
    base_url, token, files_dir = file_bridge_factory(expected_server="Other-Broker")
    _write_ready_state(files_dir)

    response = requests.get(f"{base_url}/health", headers=_headers(token))
    assert response.json()["status"] == "degraded"


def test_snapshot_refuses_on_account_mismatch(file_bridge_factory):
    base_url, token, files_dir = file_bridge_factory(expected_login="99999999")
    _write_ready_state(files_dir)

    response = requests.post(f"{base_url}/v1/trading/snapshot", json={}, headers=_headers(token))
    assert response.status_code == 503
    body = response.json()
    assert body["error"]["code"] == "account_mismatch"
    # Il messaggio non deve mai includere il login reale ne' quello atteso in chiaro.
    assert "12345678" not in response.text
    assert "99999999" not in response.text


def test_snapshot_ok_when_account_matches_expected(file_bridge_factory):
    # Verifica di controllo: gli stessi valori di default della fixture (login/server) devono
    # combaciare, altrimenti tutti gli altri test "happy path" del file sarebbero silenziosamente
    # scorretti dopo l'introduzione della verifica obbligatoria di identita'.
    base_url, token, files_dir = file_bridge_factory()
    _write_ready_state(files_dir)

    response = requests.post(f"{base_url}/v1/trading/snapshot", json={}, headers=_headers(token))
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# hardening: deduplica non piu' basata sul solo deal_ticket
# ---------------------------------------------------------------------------


def test_same_ticket_on_different_connection_ids_is_not_merged(file_bridge_factory):
    """Due connessioni/account diversi (broker differenti che riusano la stessa numerazione
    ticket) non devono mai fondersi in una voce sola: l'evento di una connessione diversa dalla
    propria (test-connection-id, il default della fixture) e' semplicemente escluso, non fuso."""
    base_url, token, files_dir = file_bridge_factory()  # connection_id di default: test-connection-id
    _write_ready_state(files_dir)
    now = datetime.now(timezone.utc)
    close_time = _iso(now - timedelta(minutes=5))
    _append_events(
        files_dir,
        _deal_event("90001", "80001", close_time=close_time, profit=111.0,
                    connection_id="test-connection-id"),
        _deal_event("90001", "80001", close_time=close_time, profit=222.0,
                    connection_id="other-connection-id"),
    )

    response = requests.post(
        f"{base_url}/v1/trading/snapshot", json={"deal_lookback_hours": 24}, headers=_headers(token)
    )
    deals = response.json()["deals"]
    assert len(deals) == 1
    assert deals[0]["deal_ticket"] == "90001"
    assert deals[0]["profit"] == 111.0  # non 222.0: l'evento dell'altra connessione e' escluso


# ---------------------------------------------------------------------------
# hardening: cursore con fingerprint (device+inode), rotazione, troncamento, righe parziali
# ---------------------------------------------------------------------------


def test_rotation_preserves_unread_tail_and_picks_up_new_file(file_bridge_factory):
    base_url, token, files_dir = file_bridge_factory()
    _write_ready_state(files_dir)
    now = datetime.now(timezone.utc)

    # 1) Il bridge legge il deal A e persiste un cursore con l'identita' del file corrente.
    _append_events(files_dir, _deal_event("10001", "1", close_time=_iso(now - timedelta(minutes=30))))
    first = requests.post(
        f"{base_url}/v1/trading/snapshot", json={"deal_lookback_hours": 24}, headers=_headers(token)
    )
    assert {d["deal_ticket"] for d in first.json()["deals"]} == {"10001"}

    # 2) Il deal B viene scritto ma MAI letto dal bridge prima della rotazione (simula l'EA che
    # scrive e ruota events.jsonl fra due poll del bridge).
    _append_events(files_dir, _deal_event("10002", "1", close_time=_iso(now - timedelta(minutes=20))))
    events_path = files_dir / "events.jsonl"
    rotated_path = files_dir / "events.jsonl.1"
    events_path.rename(rotated_path)  # stesso identity (device, inode) del file appena letto
    events_path.touch()
    _append_events(files_dir, _deal_event("10003", "1", close_time=_iso(now - timedelta(minutes=5))))

    # 3) Il prossimo poll deve vedere TUTTI e tre i deal: A (gia' noto), B (coda non letta prima
    # della rotazione, recuperata da events.jsonl.1) e C (nel nuovo events.jsonl).
    second = requests.post(
        f"{base_url}/v1/trading/snapshot", json={"deal_lookback_hours": 24}, headers=_headers(token)
    )
    assert {d["deal_ticket"] for d in second.json()["deals"]} == {"10001", "10002", "10003"}


def test_truncated_events_file_recovers_without_crashing(file_bridge_factory):
    base_url, token, files_dir = file_bridge_factory()
    _write_ready_state(files_dir)
    now = datetime.now(timezone.utc)

    _append_events(files_dir, _deal_event("20001", "1", close_time=_iso(now - timedelta(minutes=30))))
    first = requests.post(
        f"{base_url}/v1/trading/snapshot", json={"deal_lookback_hours": 24}, headers=_headers(token)
    )
    assert {d["deal_ticket"] for d in first.json()["deals"]} == {"20001"}

    # Troncamento sul posto: STESSO file (stesso inode), contenuto piu' corto di prima. Diverso da
    # una rotazione (che rinomina il file esistente creandone uno nuovo con lo stesso nome).
    events_path = files_dir / "events.jsonl"
    events_path.write_text(
        json.dumps(_deal_event("20002", "1", close_time=_iso(now - timedelta(minutes=5)))) + "\n",
        encoding="utf-8",
    )

    second = requests.post(
        f"{base_url}/v1/trading/snapshot", json={"deal_lookback_hours": 24}, headers=_headers(token)
    )
    assert response_has_deal(second, "20002")


def response_has_deal(response, deal_ticket: str) -> bool:
    return deal_ticket in {d["deal_ticket"] for d in response.json()["deals"]}


def test_partial_last_jsonl_line_is_retried_next_cycle(file_bridge_factory):
    base_url, token, files_dir = file_bridge_factory()
    _write_ready_state(files_dir)
    now = datetime.now(timezone.utc)

    complete_event = _deal_event("30001", "1", close_time=_iso(now - timedelta(minutes=10)))
    partial_event = _deal_event("30002", "1", close_time=_iso(now - timedelta(minutes=5)))
    events_path = files_dir / "events.jsonl"
    with open(events_path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(complete_event) + "\n")
        handle.write(json.dumps(partial_event))  # nessun newline finale: riga "in scrittura"

    first = requests.post(
        f"{base_url}/v1/trading/snapshot", json={"deal_lookback_hours": 24}, headers=_headers(token)
    )
    tickets = {d["deal_ticket"] for d in first.json()["deals"]}
    assert "30001" in tickets
    assert "30002" not in tickets  # riga incompleta: non ancora esposta

    # L'EA completa la riga (stesso comportamento di un FileWriteString seguito da FileFlush).
    with open(events_path, "a", encoding="utf-8") as handle:
        handle.write("\n")

    second = requests.post(
        f"{base_url}/v1/trading/snapshot", json={"deal_lookback_hours": 24}, headers=_headers(token)
    )
    assert response_has_deal(second, "30002")


def test_restart_before_cursor_save_reprocesses_idempotently(file_bridge_factory, tmp_path):
    shared_dir = tmp_path / "shared-TradeJournal"
    base_url, token, files_dir = file_bridge_factory(base_dir=shared_dir)
    _write_ready_state(files_dir)
    now = datetime.now(timezone.utc)
    _append_events(files_dir, _deal_event("40001", "1", close_time=_iso(now - timedelta(minutes=10))))

    first = requests.post(
        f"{base_url}/v1/trading/snapshot", json={"deal_lookback_hours": 24}, headers=_headers(token)
    )
    assert response_has_deal(first, "40001")
    cursor_path = files_dir / "bridge_cursor.json"
    assert cursor_path.exists()

    # Simula un crash del processo bridge subito DOPO aver letto il deal (in memoria) ma PRIMA di
    # persistere il cursore aggiornato: il file su disco viene fatto regredire a uno stato
    # antecedente alla lettura (stessa identita' del file, offset/deals pero' come se non fosse
    # mai stato letto).
    stale_cursor = json.loads(cursor_path.read_text(encoding="utf-8"))
    stale_cursor["offset"] = 0
    stale_cursor["deals"] = {}
    cursor_path.write_text(json.dumps(stale_cursor), encoding="utf-8")

    # "Riavvio": una nuova istanza del bridge (nuovo _EventsCursor) sulla STESSA directory deve
    # rileggere da capo e riconvergere allo stesso risultato, senza duplicati ne' errori.
    base_url_2, token_2, _ = file_bridge_factory(base_dir=files_dir)
    second = requests.post(
        f"{base_url_2}/v1/trading/snapshot", json={"deal_lookback_hours": 24}, headers=_headers(token_2)
    )
    deals = second.json()["deals"]
    assert len(deals) == 1
    assert deals[0]["deal_ticket"] == "40001"
