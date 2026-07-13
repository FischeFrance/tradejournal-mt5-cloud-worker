"""Entry point del worker: poll periodico, rilevamento eventi, normalizzazione e invio.

Non stampa MAI MT5_PASSWORD o TRADEJOURNAL_BRIDGE_TOKEN (nemmeno mascherati: sono
semplicemente esclusi da ogni log). account_number e server vengono sempre mascherati prima di
finire in un log (vedi event_sender.mask_value).
"""

from __future__ import annotations

import logging
import signal
import sys
import time
from typing import Optional

from bridge_mt5_client import BridgeMt5Client
from config import load_config
from event_detector import detect_events
from event_normalizer import normalize_event
from event_sender import EventSender, mask_value
from mock_mt5_client import MockMt5Client
from mt5_client import Mt5Client, Mt5ConnectionError, RealMt5Client
from snapshot_store import SnapshotStore

HEARTBEAT_FILE = "/tmp/mt5_worker_heartbeat"
SNAPSHOT_FILE_REAL_MODE = "/app/data/snapshot.json"

logger = logging.getLogger("mt5_worker.main")


class ShutdownRequested(Exception):
    pass


def _configure_logging(log_level: str) -> None:
    level = getattr(logging, log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _build_client(config) -> Mt5Client:
    if config.mt5_client_source == "mock":
        logger.info("MT5_CLIENT_SOURCE=mock: nessuna dipendenza da Wine/MT5, uso MockMt5Client.")
        return MockMt5Client()
    if config.mt5_client_source == "bridge":
        logger.info("MT5_CLIENT_SOURCE=bridge: uso mt5-bridge HTTP read-only.")
        return BridgeMt5Client(
            bridge_url=config.mt5_bridge_url,
            bridge_token=config.mt5_bridge_token,
            timeout_seconds=config.mt5_bridge_timeout_seconds,
            deal_lookback_hours=config.mt5_deal_lookback_hours,
        )
    logger.info(
        "MT5_CLIENT_SOURCE=direct: uso RealMt5Client (server=%s). Richiede un terminale MT5 "
        "raggiungibile (Windows o Linux+Wine, vedi README, 'Fase 2: MT5 reale + Wine').",
        mask_value(config.mt5_server),
    )
    logger.warning(
        "Promemoria sicurezza: MT5_PASSWORD deve essere la password INVESTOR (sola lettura) "
        "dell'account, mai la password di trading (vedi .env.example)."
    )
    return RealMt5Client(config.mt5_login, config.mt5_password, config.mt5_server)


def _process_snapshot(current_snapshot, account, snapshot_store: SnapshotStore, sender: EventSender) -> bool:
    """Rileva/invia gli eventi e avanza il checkpoint solo se il poll e' stato consegnato.

    In caso di successo parziale lo snapshot precedente resta su disco: al poll seguente gli
    eventi vengono rigenerati con lo stesso event_id deterministico. L'ingestion API puo' quindi
    deduplicare quelli gia' accettati, mentre un evento fallito non viene perso.
    """
    previous_snapshot = snapshot_store.get()
    raw_events = detect_events(previous_snapshot, current_snapshot)
    all_delivered = True

    for raw_event in raw_events:
        payload = normalize_event(
            raw_event,
            account_number=account.get("login"),
            server=account.get("server"),
        )
        result = sender.send(payload)
        logger.info(
            "Evento %s (ticket=%s) -> esito invio: %s",
            payload["event_type"],
            payload["external_trade_id"],
            result.status,
        )
        if result.status not in ("sent", "dry_run"):
            all_delivered = False

    if all_delivered:
        snapshot_store.update(current_snapshot)
    else:
        logger.warning(
            "Uno o piu' eventi del poll non sono stati consegnati: snapshot non aggiornato, "
            "il prossimo poll li rigenerera' con event_id deterministico."
        )
    return all_delivered


def _write_heartbeat() -> None:
    try:
        with open(HEARTBEAT_FILE, "w", encoding="utf-8") as handle:
            handle.write(str(time.time()))
    except OSError as exc:
        logger.warning("Impossibile scrivere l'heartbeat file %s: %s", HEARTBEAT_FILE, exc)


def _install_signal_handlers() -> None:
    def _handler(signum, _frame):
        logger.info("Ricevuto segnale %s, arresto in corso...", signum)
        raise ShutdownRequested()

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)


def run(env: Optional[dict] = None) -> int:
    config = load_config(env)
    _configure_logging(config.log_level)

    logger.info(
        "Avvio mt5-cloud-worker: mock_mode=%s mt5_client_source=%s dry_run=%s "
        "poll_interval=%ss api_target_configurato=%s",
        config.mock_mode,
        config.mt5_client_source,
        config.dry_run,
        config.poll_interval_seconds,
        config.has_api_target,
    )

    client = _build_client(config)
    snapshot_store = SnapshotStore(
        file_path=None if config.mt5_client_source == "mock" else SNAPSHOT_FILE_REAL_MODE
    )
    sender = EventSender(
        api_url=config.tradejournal_api_url,
        bridge_token=config.tradejournal_bridge_token,
        dry_run=config.dry_run,
    )

    try:
        client.connect()
    except Mt5ConnectionError as exc:
        logger.error("Connessione MT5 iniziale fallita: %s", exc)
        if config.mt5_client_source != "mock":
            return 1

    _install_signal_handlers()
    scenario_completion_logged = False

    try:
        while True:
            client.tick()

            try:
                current_snapshot = client.snapshot()
                account = client.account_info()
            except Mt5ConnectionError as exc:
                logger.error("Errore nel leggere lo stato MT5 (%s), tento la riconnessione...", exc)
                try:
                    client.reconnect()
                except Mt5ConnectionError as reconnect_exc:
                    logger.error("Riconnessione fallita: %s", reconnect_exc)
                _write_heartbeat()
                time.sleep(config.poll_interval_seconds)
                continue

            _process_snapshot(current_snapshot, account, snapshot_store, sender)
            _write_heartbeat()

            if (
                config.mt5_client_source == "mock"
                and getattr(client, "is_scenario_finished", False)
                and not scenario_completion_logged
            ):
                logger.info(
                    "Scenario mock completato: tutti e 7 gli eventi sono stati simulati. "
                    "Il worker continua a girare (nessun nuovo evento) finche' non viene fermato."
                )
                scenario_completion_logged = True

            time.sleep(config.poll_interval_seconds)
    except ShutdownRequested:
        logger.info("Worker arrestato in modo pulito.")
        return 0
    except KeyboardInterrupt:
        logger.info("Worker interrotto da tastiera.")
        return 0


if __name__ == "__main__":
    sys.exit(run())
