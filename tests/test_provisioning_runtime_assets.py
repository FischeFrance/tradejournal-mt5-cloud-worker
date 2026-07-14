"""Static and Compose-resolved release gates for per-account runtime assets."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILE = REPO_ROOT / "deploy" / "instance" / "compose.yaml"
RUNTIME_ENTRYPOINT = REPO_ROOT / "deploy" / "instance" / "entrypoint-runtime.sh"
RUNTIME_HEALTHCHECK = REPO_ROOT / "deploy" / "instance" / "healthcheck-runtime.sh"
RUNTIME_DOCKERFILE = REPO_ROOT / "deploy" / "instance" / "Dockerfile.runtime"
WORKER_DOCKERFILE = REPO_ROOT / "docker" / "Dockerfile"
SYSTEMD_UNIT = (
    REPO_ROOT / "deploy" / "systemd" / "tradejournal-provisioning-agent.service"
)


@pytest.fixture(scope="module")
def resolved_compose(tmp_path_factory):
    if shutil.which("docker") is None:
        pytest.skip("docker is unavailable")
    try:
        subprocess.run(
            ["docker", "compose", "version"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        pytest.skip(f"docker compose is unavailable: {exc}")

    secret_dir = tmp_path_factory.mktemp("runtime-compose-secrets")
    for name in ("mt5_password", "mt5_bridge_token", "tradejournal_bridge_token"):
        path = secret_dir / name
        path.write_text("local-test-placeholder", encoding="utf-8")
        path.chmod(0o400)

    env = os.environ.copy()
    env.update(
        {
            "TJ_PROJECT_NAME": "tjmt5-123456781234123412341234567890ab",
            "TJ_CONNECTION_ID": "12345678-1234-1234-1234-1234567890ab",
            "REPOSITORY_ROOT": str(REPO_ROOT),
            "MT5_RUNTIME_TARGET": "mock",
            "MT5_LOGIN": "123456",
            "MT5_SERVER": "Broker-Demo",
            "TJ_EXPECTED_MT5_LOGIN": "123456",
            "TJ_EXPECTED_MT5_SERVER": "Broker-Demo",
            "TRADEJOURNAL_API_URL": "https://example.test/api/mt5-events",
            "TJ_SECRET_DIR": str(secret_dir),
            "MT5_TEMPLATE_ARCHIVE": "/dev/null",
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
    except subprocess.CalledProcessError as exc:
        pytest.fail(f"instance Compose is invalid: {exc.stderr}")
    return json.loads(completed.stdout)


def test_compose_keeps_services_private_and_per_instance(resolved_compose):
    assert set(resolved_compose["services"]) == {"mt5-runtime", "worker"}
    assert resolved_compose["networks"]["instance-internal"]["internal"] is True
    for service in resolved_compose["services"].values():
        assert not service.get("ports")
        assert not service.get("privileged", False)
        assert service.get("network_mode") != "host"
        assert "ALL" in service["cap_drop"]
        assert service["read_only"] is True
        assert service["restart"] == "unless-stopped"
        assert service["stop_grace_period"] == "45s"
        assert not any("docker.sock" in str(mount) for mount in service.get("volumes", []))


def test_runtime_has_only_bootstrap_capabilities(resolved_compose):
    runtime = resolved_compose["services"]["mt5-runtime"]
    worker = resolved_compose["services"]["worker"]
    assert set(runtime["cap_add"]) == {"CHOWN", "SETGID", "SETUID"}
    assert not worker.get("cap_add")
    for service in (runtime, worker):
        assert any(
            option.startswith("no-new-privileges") for option in service["security_opt"]
        )


def test_file_secret_contract_is_separated_without_ignored_metadata(resolved_compose):
    runtime = resolved_compose["services"]["mt5-runtime"]
    worker = resolved_compose["services"]["worker"]
    runtime_secrets = {item["source"] for item in runtime["secrets"]}
    worker_secrets = {item["source"] for item in worker["secrets"]}

    assert runtime_secrets == {"mt5_password", "mt5_bridge_token"}
    assert worker_secrets == {"mt5_bridge_token", "tradejournal_bridge_token"}
    assert "mt5_password" not in worker_secrets
    assert "tradejournal_bridge_token" not in runtime_secrets
    for item in runtime["secrets"] + worker["secrets"]:
        assert not {"uid", "gid", "mode"}.intersection(item)

    source = COMPOSE_FILE.read_text(encoding="utf-8")
    assert "preserve host ownership/mode" in source
    assert "Release gate" in source


def test_compose_has_healthchecks_limits_and_persistence(resolved_compose):
    runtime = resolved_compose["services"]["mt5-runtime"]
    worker = resolved_compose["services"]["worker"]
    assert runtime["healthcheck"]["test"][0] == "CMD"
    assert worker["healthcheck"]["test"][0] == "CMD"
    assert runtime["cpus"] > 0 and worker["cpus"] > 0
    assert int(runtime["mem_limit"]) > 0 and int(worker["mem_limit"]) > 0
    assert runtime["pids_limit"] == 512
    assert worker["pids_limit"] == 128
    assert any(mount["target"] == "/var/lib/tradejournal/wine-prefix" for mount in runtime["volumes"])
    assert any(mount["target"] == "/app/data" for mount in worker["volumes"])


def test_runtime_healthcheck_parses_status_without_exposing_response():
    source = RUNTIME_HEALTHCHECK.read_text(encoding="utf-8")
    dockerfile = RUNTIME_DOCKERFILE.read_text(encoding="utf-8")
    assert 'exec gosu runtime:runtime "$0" "$@"' in source
    assert "jq -e" in source
    assert '.status == "ok"' in source
    assert "show-error" not in source
    assert "runtime is not healthy" in source
    assert dockerfile.count("jq") >= 2


def test_mock_base_images_pin_python_patch_and_distribution():
    runtime = RUNTIME_DOCKERFILE.read_text(encoding="utf-8")
    worker = WORKER_DOCKERFILE.read_text(encoding="utf-8")
    assert "FROM python:3.11.9-slim-bookworm AS mock" in runtime
    assert "FROM python:3.11.9-slim-bookworm AS base" in worker


def test_runtime_supervises_children_and_bounds_ordered_cleanup():
    source = RUNTIME_ENTRYPOINT.read_text(encoding="utf-8")
    assert 'wait -n -p EXITED_CHILD "$BRIDGE_PID" "$TERMINAL_PID" "$XVFB_PID"' in source
    bridge = source.index('terminate_child "$BRIDGE_PID"')
    terminal = source.index('terminate_child "$TERMINAL_PID"')
    wineserver = source.index('wineserver -k', bridge)
    xvfb = source.index('terminate_child "$XVFB_PID"', wineserver)
    assert bridge < terminal < wineserver < xvfb
    assert "kill -KILL" in source
    assert "WINESERVER_STOP_TIMEOUT_SECONDS" in source


def test_systemd_unit_is_bootstrap_compatible_and_hardened():
    source = SYSTEMD_UNIT.read_text(encoding="utf-8")
    assert "EnvironmentFile=/etc/tradejournal/provisioning-agent.env" in source
    assert "EnvironmentFile=-" not in source
    assert "ProtectSystem=full" in source
    assert "ReadOnlyPaths=/opt/tradejournal/tradejournal-mt5-cloud-worker" in source
    assert "TimeoutStopSec=330s" in source
    assert "KillMode=mixed" in source
    for directive in (
        "NoNewPrivileges=true",
        "CapabilityBoundingSet=",
        "AmbientCapabilities=",
        "PrivateDevices=true",
        "ProtectProc=invisible",
        "RestrictNamespaces=true",
    ):
        assert directive in source
