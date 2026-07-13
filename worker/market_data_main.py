"""Entry point del market-data-worker: processo separato dal trade-sync worker (main.py).

Deliberatamente NON importa main.py e non condivide il suo loop: sono due processi/container
distinti (vedi docker-compose.research.yml), cosi' un problema nella raccolta dati di mercato non
puo' mai bloccare o rallentare la sincronizzazione dei trade verso TradeJournal, e viceversa.

Non stampa MAI DATABASE_URL (contiene la password Postgres): solo app_mode/simboli/timeframe/
conteggi finiscono nei log. Le eccezioni sollevate da psycopg2 in caso di errore di connessione
non includono la password nel loro messaggio (verificato: libpq riporta host/porta/utente, mai
la password), ma restano comunque loggate solo come stringa dell'eccezione, mai il DSN stesso.
"""

from __future__ import annotations

import logging
import signal
import sys
import time
from pathlib import Path
from typing import Optional

import db_migrate
from config import Config, ConfigError, load_config
from market_data_source import Mt5BridgeError, build_market_data_source
from market_data_store import MarketDataStore

HEARTBEAT_FILE = "/tmp/mt5_market_data_worker_heartbeat"

#: Quante candele richiedere per ciascuna coppia symbol/timeframe al primo avvio (nessun
#: checkpoint ancora salvato). Un limite alto ma finito: evita di caricare in memoria una serie
#: arbitrariamente lunga in un solo colpo, restando comunque generoso per un mock/mt5 che parte
#: da un'epoca fissa.
INITIAL_BACKFILL_LIMIT = 500

#: Quante candele richiedere per ciascuna coppia symbol/timeframe a ogni ciclo di poll
#: successivo al backfill. Deve coprire comodamente il numero di barre che possono essersi
#: formate in un intervallo di MARKET_DATA_POLL_SECONDS anche per il timeframe piu' corto (M1).
POLL_BATCH_LIMIT = 100

logger = logging.getLogger("mt5_worker.market_data_main")


class ShutdownRequested(Exception):
    pass


def _configure_logging(log_level: str) -> None:
    level = getattr(logging, log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _validate_startup(config: Config) -> None:
    """Vincolo specifico di questo processo, in aggiunta alla validazione di config.py:
    un market-data-worker con ENABLE_MARKET_DATA=false e' un errore di deployment, perche'
    questo processo esiste esclusivamente per la raccolta dati di mercato."""
    if not config.enable_market_data:
        raise ConfigError(
            "market_data_main avviato con ENABLE_MARKET_DATA=false. Questo processo serve solo "
            "alla raccolta dati di mercato in modalita' research: verificare che APP_MODE=research "
            "e ENABLE_MARKET_DATA=true siano impostati (vedi docker-compose.research.yml)."
        )


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


def _connect_with_retry(
    database_url: str,
    migrations_dir: Path,
    max_retries: int = 5,
    retry_delay_seconds: float = 2.0,
    sleep_fn=time.sleep,
) -> None:
    """Applica le migration con un numero limitato di retry, per assorbire il breve intervallo
    in cui Postgres e' 'healthy' per Docker ma non ancora pronto ad accettare connessioni."""
    last_error: Optional[BaseException] = None
    for attempt in range(1, max_retries + 1):
        try:
            db_migrate.apply_migrations(database_url, migrations_dir)
            return
        except Exception as exc:  # noqa: BLE001 - qualunque errore di connessione/migration
            last_error = exc
            logger.warning(
                "Tentativo %s/%s di connessione/migration Postgres fallito: %s",
                attempt,
                max_retries,
                exc,
            )
            if attempt < max_retries:
                sleep_fn(retry_delay_seconds * attempt)
    raise RuntimeError(f"Connessione/migration Postgres fallita dopo {max_retries} tentativi: {last_error}")


def _broker_symbol(config: Config, canonical_symbol: str) -> str:
    """Mapping canonical -> broker symbol. Solo EURUSD e' nello scope di questa fase (vedi
    README): usa EURUSD_BROKER_SYMBOL. Qualunque altro simbolo canonico (mock, MARKET_SYMBOLS
    personalizzata) usa se stesso come broker symbol, nessuna traduzione -- da estendere se in
    futuro verranno aggiunti altri asset con un broker symbol diverso dal canonico."""
    if canonical_symbol == "EURUSD":
        return config.eurusd_broker_symbol
    return canonical_symbol


def _sync_symbol_timeframe(store: MarketDataStore, source, symbol_id: int, broker_symbol: str, timeframe: str, limit: int) -> int:
    checkpoint = store.get_checkpoint(symbol_id, timeframe)
    try:
        candles = source.get_candles(broker_symbol, timeframe, since=checkpoint, limit=limit)
    except Mt5BridgeError as exc:
        # Non facciamo mai crashare l'intero worker per un singolo fallimento del bridge
        # (transitorio o permanente che sia): logghiamo e ritentiamo al prossimo ciclo di poll,
        # stesso principio gia' usato da main.py per Mt5ConnectionError sul trade-sync worker.
        logger.error("Sync %s/%s fallita (mt5-bridge): %s", broker_symbol, timeframe, exc)
        return 0
    if not candles:
        return 0
    return store.upsert_candles(symbol_id, candles)


def run(
    env: Optional[dict] = None,
    migrations_dir: Path = db_migrate.DEFAULT_MIGRATIONS_DIR,
    connect_max_retries: int = 5,
    connect_retry_delay_seconds: float = 2.0,
) -> int:
    try:
        config = load_config(env)
        _validate_startup(config)
    except ConfigError as exc:
        logging.basicConfig(level=logging.ERROR)
        logger.error("Configurazione non valida, market-data-worker non avviato: %s", exc)
        return 1

    _configure_logging(config.log_level)
    logger.info(
        "Avvio market-data-worker: app_mode=%s market_data_source=%s symbols=%s timeframes=%s "
        "poll_interval=%ss%s",
        config.app_mode,
        config.market_data_source,
        list(config.market_symbols),
        list(config.market_timeframes),
        config.market_data_poll_seconds,
        f" mt5_bridge_url={config.mt5_bridge_url}" if config.market_data_source == "mt5" else "",
    )

    try:
        _connect_with_retry(
            config.database_url,
            migrations_dir,
            max_retries=connect_max_retries,
            retry_delay_seconds=connect_retry_delay_seconds,
        )
    except RuntimeError as exc:
        logger.error("%s", exc)
        return 1

    source = build_market_data_source(
        config.market_data_source,
        mt5_bridge_url=config.mt5_bridge_url,
        mt5_bridge_token=config.mt5_bridge_token,
        mt5_bridge_timeout_seconds=config.mt5_bridge_timeout_seconds,
    )

    health_check = getattr(source, "health", None)
    if callable(health_check):
        try:
            status = health_check()
            logger.info(
                "mt5-bridge raggiungibile: terminal_connected=%s account_connected=%s server=%s",
                status.get("terminal_connected"), status.get("account_connected"), status.get("server"),
            )
        except Mt5BridgeError as exc:
            logger.warning(
                "mt5-bridge non raggiungibile all'avvio (si continuera' comunque a ritentare a "
                "ogni ciclo di sync): %s", exc,
            )

    store = MarketDataStore(config.database_url)
    store.connect()

    try:
        broker_symbols = {symbol: _broker_symbol(config, symbol) for symbol in config.market_symbols}
        symbol_ids = {
            symbol: store.ensure_symbol(
                canonical_symbol=symbol, broker_symbol=broker_symbols[symbol], source=config.market_data_source
            )
            for symbol in config.market_symbols
        }

        _install_signal_handlers()

        logger.info("Backfill iniziale...")
        for symbol in config.market_symbols:
            for timeframe in config.market_timeframes:
                count = _sync_symbol_timeframe(
                    store, source, symbol_ids[symbol], broker_symbols[symbol], timeframe, INITIAL_BACKFILL_LIMIT
                )
                logger.info("Backfill %s/%s: %s candele.", symbol, timeframe, count)
        _write_heartbeat()

        while True:
            time.sleep(config.market_data_poll_seconds)
            for symbol in config.market_symbols:
                for timeframe in config.market_timeframes:
                    count = _sync_symbol_timeframe(
                        store, source, symbol_ids[symbol], broker_symbols[symbol], timeframe, POLL_BATCH_LIMIT
                    )
                    if count:
                        logger.info("Sync %s/%s: %s nuove candele.", symbol, timeframe, count)
            _write_heartbeat()
    except ShutdownRequested:
        logger.info("market-data-worker arrestato in modo pulito.")
        return 0
    except KeyboardInterrupt:
        logger.info("market-data-worker interrotto da tastiera.")
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(run())
