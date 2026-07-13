"""Invarianti del compose trade-sync HTTP.

Questi test eseguono soltanto ``docker compose config``: non costruiscono immagini, non
avviano container e non effettuano chiamate di rete. Se Docker/Compose non e' disponibile il
modulo viene saltato, come gia' accade per i test di integrazione Postgres in conftest.py.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILE = REPO_ROOT / "docker-compose.trade-sync-fake.yml"


@pytest.fixture(scope="module")
def compose_config():
    if shutil.which("docker") is None:
        pytest.skip("docker non disponibile: salto le invarianti risolte da Docker Compose")

    try:
        subprocess.run(
            ["docker", "compose", "version"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
        pytest.skip(f"plugin docker compose non disponibile: {exc}")

    env = os.environ.copy()
    env.update(
        {
            "MT5_BRIDGE_TOKEN": "test-private-mt5-bridge-token",
            "TRADEJOURNAL_BRIDGE_TOKEN": "test-ingestion-token-distinct",
            "TRADEJOURNAL_API_URL": "http://192.0.2.10:3000/api/mt5-events",
        }
    )
    command = [
        "docker",
        "compose",
        "--env-file",
        "/dev/null",
        "-f",
        str(COMPOSE_FILE),
        "config",
        "--format",
        "json",
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=REPO_ROOT,
            env=env,
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        pytest.fail(f"impossibile eseguire docker compose config: {exc}")
    except subprocess.CalledProcessError as exc:
        pytest.fail(f"docker compose config non valido: {exc.stderr}")
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        pytest.fail(f"docker compose config non ha restituito JSON valido: {exc}")


def test_compose_contains_only_trade_sync_and_fake_bridge(compose_config):
    assert set(compose_config["services"]) == {"mt5-bridge-fake", "trade-sync-worker"}
    assert "postgres" not in compose_config["services"]
    assert "market-data-worker" not in compose_config["services"]


def test_neither_service_publishes_ports(compose_config):
    for service in compose_config["services"].values():
        assert not service.get("ports")
        assert service.get("network_mode") != "host"


def test_worker_uses_linux_bridge_client_and_distinct_tokens(compose_config):
    bridge_env = compose_config["services"]["mt5-bridge-fake"]["environment"]
    worker = compose_config["services"]["trade-sync-worker"]
    worker_env = worker["environment"]

    assert worker["build"]["target"] == "mock"
    assert worker_env["MOCK_MODE"] == "false"
    assert worker_env["MT5_CLIENT_SOURCE"] == "bridge"
    assert worker_env["MT5_BRIDGE_URL"] == "http://mt5-bridge-fake:8080"
    assert worker_env["MT5_BRIDGE_TOKEN"] == bridge_env["MT5_BRIDGE_TOKEN"]
    assert worker_env["MT5_BRIDGE_TOKEN"] != worker_env["TRADEJOURNAL_BRIDGE_TOKEN"]
    assert not {"MT5_LOGIN", "MT5_PASSWORD", "MT5_SERVER"}.intersection(worker_env)


def test_snapshot_volume_is_persistent_and_bridge_is_health_dependency(compose_config):
    worker = compose_config["services"]["trade-sync-worker"]
    mounts = worker["volumes"]

    assert any(
        mount.get("type") == "volume"
        and mount.get("source") == "trade-sync-snapshot"
        and mount.get("target") == "/app/data"
        for mount in mounts
    )
    assert worker["depends_on"]["mt5-bridge-fake"]["condition"] == "service_healthy"


def test_services_apply_basic_container_hardening(compose_config):
    for service in compose_config["services"].values():
        assert service["read_only"] is True
        assert "ALL" in service["cap_drop"]
        assert any(option.startswith("no-new-privileges") for option in service["security_opt"])
