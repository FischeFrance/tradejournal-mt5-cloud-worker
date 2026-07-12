"""Migration runner minimale per lo schema Postgres della modalita' research.

Nessuna DDL sparsa nel codice Python: ogni cambiamento di schema e' un file .sql numerato in
db/migrations/, applicato una sola volta (tracciato nella tabella schema_migrations) e in ordine
di nome file. Deliberatamente senza un framework esterno (Alembic, ecc.): lo schema di questa
fase e' piccolo (due tabelle) e un runner di poche righe e' piu' semplice da verificare a vista
di una dipendenza aggiuntiva. Se lo schema crescera' in complessita', rivalutare.

Usato solo dal market-data-worker (market_data_main.py) e dai test di integrazione: il
trade-sync worker non importa mai questo modulo.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List

logger = logging.getLogger("mt5_worker.db_migrate")

DEFAULT_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "db" / "migrations"

_CREATE_TRACKING_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     TEXT PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def _list_migration_files(migrations_dir: Path) -> List[Path]:
    if not migrations_dir.is_dir():
        raise FileNotFoundError(f"Cartella migration non trovata: {migrations_dir}")
    return sorted(migrations_dir.glob("*.sql"), key=lambda p: p.name)


def run_migrations(conn, migrations_dir: Path = DEFAULT_MIGRATIONS_DIR) -> List[str]:
    """Applica, in ordine e in modo idempotente, le migration non ancora applicate.

    `conn` e' una connessione psycopg2 gia' aperta (dependency injection: rende il modulo
    testabile senza legarlo a un modo particolare di ottenere la connessione). Restituisce la
    lista delle versioni applicate in questa chiamata (vuota se lo schema era gia' aggiornato).
    Rieseguire questa funzione su uno schema gia' aggiornato e' un no-op sicuro.
    """
    with conn:
        with conn.cursor() as cur:
            cur.execute(_CREATE_TRACKING_TABLE)

    applied: List[str] = []
    for path in _list_migration_files(migrations_dir):
        version = path.stem
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM schema_migrations WHERE version = %s", (version,))
                if cur.fetchone() is not None:
                    continue
                sql = path.read_text(encoding="utf-8")
                logger.info("Applico migration %s...", version)
                cur.execute(sql)
                cur.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (version,))
        applied.append(version)
    return applied


def apply_migrations(database_url: str, migrations_dir: Path = DEFAULT_MIGRATIONS_DIR) -> List[str]:
    """Apre una connessione da DATABASE_URL, applica le migration, chiude la connessione.

    Punto d'ingresso usato da market_data_main.py all'avvio. L'import di psycopg2 e' lazy (come
    _import_mt5 in mt5_client.py) cosi' che i moduli che non fanno mai I/O su Postgres (trade-sync
    worker, test unitari puri) non richiedano il pacchetto come dipendenza rigida.
    """
    try:
        import psycopg2  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError(
            "Pacchetto 'psycopg2' non disponibile: richiesto solo per il market-data-worker "
            "(vedi requirements-research.txt)."
        ) from exc

    conn = psycopg2.connect(database_url)
    try:
        return run_migrations(conn, migrations_dir)
    finally:
        conn.close()
