import os
import shutil
import subprocess
import sys
import time
import uuid

import pytest

WORKER_DIR = os.path.join(os.path.dirname(__file__), "..", "worker")
sys.path.insert(0, os.path.abspath(WORKER_DIR))


def _wait_for_postgres_ready(database_url: str, timeout_seconds: float = 30.0) -> None:
    import psycopg2

    deadline = time.monotonic() + timeout_seconds
    last_error = None
    while time.monotonic() < deadline:
        try:
            conn = psycopg2.connect(database_url)
            conn.close()
            return
        except psycopg2.OperationalError as exc:
            last_error = exc
            time.sleep(0.5)
    raise RuntimeError(f"Postgres non pronto entro {timeout_seconds}s: {last_error}")


@pytest.fixture(scope="session")
def postgres_database_url():
    """Avvia un Postgres 16 throwaway via Docker per i test di integrazione dello store
    (tests/test_market_data_store_integration.py): nessun mock del database, query reali contro
    un'istanza reale, incluso il rispetto dei CHECK/UNIQUE constraint definiti nelle migration.

    Bind esplicito a 127.0.0.1 con porta assegnata dinamicamente da Docker (mai 0.0.0.0, mai una
    porta fissa che potrebbe collidere con un Postgres gia' in esecuzione sulla macchina). Le
    credenziali qui sono throwaway, valide solo per la durata del container di test, mai referenziate
    altrove: non hanno nulla a che fare con POSTGRES_PASSWORD di docker-compose.research.yml.

    Se Docker non e' disponibile o l'avvio fallisce, i test che dipendono da questa fixture
    vengono saltati (non falliti): l'assenza di Docker nell'ambiente di test non deve rompere la
    suite, ma la copertura "vera" con un database reale resta il punto di questo file.
    """
    if shutil.which("docker") is None:
        pytest.skip("docker non disponibile in questo ambiente: salto i test di integrazione Postgres.")

    container_name = f"mt5-research-test-pg-{uuid.uuid4().hex[:8]}"
    user, password, db_name = "test_user", "test_password_throwaway", "test_db"

    run_cmd = [
        "docker", "run", "--rm", "-d",
        "--name", container_name,
        "-e", f"POSTGRES_USER={user}",
        "-e", f"POSTGRES_PASSWORD={password}",
        "-e", f"POSTGRES_DB={db_name}",
        "-p", "127.0.0.1::5432",
        "postgres:16-alpine",
    ]
    try:
        subprocess.run(run_cmd, check=True, capture_output=True, text=True, timeout=60)
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
        pytest.skip(f"Impossibile avviare Postgres via Docker per i test di integrazione: {exc}")
        return

    try:
        port_output = subprocess.run(
            ["docker", "port", container_name, "5432/tcp"],
            check=True, capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        host_port = int(port_output.rsplit(":", 1)[-1])
        database_url = f"postgresql://{user}:{password}@127.0.0.1:{host_port}/{db_name}"

        _wait_for_postgres_ready(database_url)

        import db_migrate
        import psycopg2

        conn = psycopg2.connect(database_url)
        try:
            db_migrate.run_migrations(conn)
        finally:
            conn.close()

        yield database_url
    finally:
        subprocess.run(["docker", "rm", "-f", container_name], capture_output=True, text=True)


@pytest.fixture
def market_data_store(postgres_database_url):
    """MarketDataStore connesso a Postgres reale, con le tabelle svuotate prima di ogni test:
    ogni test parte da uno stato pulito e non dipende dall'ordine di esecuzione degli altri."""
    import psycopg2
    from market_data_store import MarketDataStore

    reset_conn = psycopg2.connect(postgres_database_url)
    try:
        with reset_conn:
            with reset_conn.cursor() as cur:
                cur.execute("TRUNCATE TABLE market_candles, market_symbols RESTART IDENTITY CASCADE;")
    finally:
        reset_conn.close()

    store = MarketDataStore(postgres_database_url)
    store.connect()
    yield store
    store.close()
