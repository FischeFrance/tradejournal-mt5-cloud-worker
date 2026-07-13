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
from event_outbox import EventOutbox, OutboxError
from event_sender import EventSender, mask_value
from mock_mt5_client import MockMt5Client
from mt5_client import Mt5Client, Mt5ConnectionError, RealMt5Client
from snapshot_store import SnapshotStore, SnapshotStoreError

HEARTBEAT_FILE = "/tmp/mt5_worker_heartbeat"
SNAPSHOT_FILE_REAL_MODE = "/app/data/snapshot.json"
EVENT_OUTBOX_FILE_REAL_MODE = "/app/data/event_outbox.json"

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


def _process_snapshot(
    current_snapshot,
    account,
    snapshot_store: SnapshotStore,
    sender: EventSender,
    outbox: Optional[EventOutbox] = None,
) -> bool:
    """Rileva, persiste e prova a consegnare gli eventi del poll.

    L'ordine e' intenzionale: l'intero batch entra nell'outbox prima di avanzare lo snapshot.
    Un crash in mezzo alle due scritture rigenera gli stessi ``event_id`` al riavvio e l'outbox
    li deduplica. Errori transitori restano pendenti, mentre quelli permanenti finiscono nella
    dead-letter senza bloccare per sempre l'avanzamento del checkpoint.

    ``outbox=None`` mantiene la chiamata compatibile per mock/test tramite uno store in memoria;
    il percorso di produzione passa sempre l'istanza persistente creata in ``run``.
    """
    outbox = outbox or EventOutbox()
    previous_snapshot = snapshot_store.get()
    raw_events = detect_events(previous_snapshot, current_snapshot)
    payloads = []
    for raw_event in raw_events:
        payloads.append(
            normalize_event(
                raw_event,
                account_number=account.get("login"),
                server=account.get("server"),
            )
        )

    enqueued = outbox.enqueue_many(payloads)
    snapshot_store.update(current_snapshot)
    drain_result = outbox.drain(sender)

    if enqueued or drain_result.pending:
        logger.info(
            "Outbox trade-sync: nuovi=%s inviati=%s dry_run=%s dead_letter=%s "
            "fallimenti_transitori=%s pendenti=%s",
            enqueued,
            drain_result.sent,
            drain_result.dry_run,
            drain_result.dead_lettered,
            drain_result.transient_failures,
            drain_result.pending,
        )
    if drain_result.pending:
        logger.warning(
            "Uno o piu' eventi restano nell'outbox per dry-run o errori recuperabili; saranno "
            "ritentati in un ciclo successivo."
        )
    return drain_result.pending == 0


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
    sender = EventSender(
        api_url=config.tradejournal_api_url,
        bridge_token=config.tradejournal_bridge_token,
        dry_run=config.dry_run,
    )
    try:
        snapshot_store = SnapshotStore(
            file_path=None if config.mt5_client_source == "mock" else SNAPSHOT_FILE_REAL_MODE
        )
        outbox = EventOutbox(
            file_path=None
            if config.mt5_client_source == "mock"
            else EVENT_OUTBOX_FILE_REAL_MODE
        )
    except (OutboxError, SnapshotStoreError, OSError) as exc:
        logger.error("Impossibile caricare lo stato persistente del worker: %s", exc)
        return 1

    # Gli eventi sopravvissuti a un riavvio non dipendono da una nuova lettura MT5 e possono
    # essere consegnati subito. Un fallimento transitorio li lascia semplicemente su disco.
    try:
        startup_drain = outbox.drain(sender)
    except (OutboxError, OSError) as exc:
        logger.error("Impossibile aggiornare l'outbox persistente: %s", exc)
        return 1
    if startup_drain.pending:
        logger.warning(
            "Outbox ripristinata con %s evento/i ancora pendenti.", startup_drain.pending
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

            try:
                _process_snapshot(current_snapshot, account, snapshot_store, sender, outbox)
            except (OutboxError, SnapshotStoreError, OSError) as exc:
                logger.error("Impossibile aggiornare lo stato persistente del worker: %s", exc)
                return 1
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
