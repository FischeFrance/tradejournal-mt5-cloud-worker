"""Test della validazione di avvio di market_data_main.py, SENZA toccare Postgres.

run() deve rifiutarsi di partire (return code 1, nessun tentativo di connessione) per ogni
configurazione che non abilita esplicitamente la raccolta dati di mercato: questo processo esiste
solo per quello, quindi avviarlo con ENABLE_MARKET_DATA=false (o senza APP_MODE=research) e' un
errore di configurazione, non un caso d'uso legittimo da degradare silenziosamente."""

from __future__ import annotations

import pytest
from config import ConfigError, load_config
from market_data_main import _validate_startup, run


def test_validate_startup_raises_when_market_data_disabled():
    config = load_config({})  # default: APP_MODE=client, ENABLE_MARKET_DATA=false
    with pytest.raises(ConfigError, match="ENABLE_MARKET_DATA"):
        _validate_startup(config)


def test_validate_startup_passes_when_market_data_enabled():
    config = load_config({
        "APP_MODE": "research",
        "ENABLE_MARKET_DATA": "true",
        "DATABASE_URL": "postgresql://user:pass@localhost:5432/db",
    })
    _validate_startup(config)  # non deve sollevare


def test_run_returns_error_code_without_market_data_enabled(capsys):
    exit_code = run(env={})
    assert exit_code == 1


def test_run_returns_error_code_with_client_mode_explicit(capsys):
    exit_code = run(env={"APP_MODE": "client"})
    assert exit_code == 1


def test_run_returns_error_code_with_invalid_app_mode(capsys):
    exit_code = run(env={"APP_MODE": "not-a-real-mode"})
    assert exit_code == 1


def test_run_does_not_log_database_url_on_failed_startup(capsys):
    # Porta 1 su loopback: nessun servizio in ascolto, la connessione viene rifiutata quasi
    # istantaneamente (nessuna attesa di timeout di rete). connect_max_retries=1 evita qualunque
    # backoff: il test resta un unit test veloce, non un test di integrazione mascherato.
    run(
        env={
            "APP_MODE": "research",
            "ENABLE_MARKET_DATA": "true",
            "DATABASE_URL": "postgresql://research_user:supersecretpassword@127.0.0.1:1/db",
        },
        connect_max_retries=1,
    )
    captured = capsys.readouterr()
    assert "supersecretpassword" not in captured.out
    assert "supersecretpassword" not in captured.err
