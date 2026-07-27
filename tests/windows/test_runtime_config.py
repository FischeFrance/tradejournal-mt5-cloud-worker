from __future__ import annotations

import pytest

from windows_agent.agent_secrets import AGENT_SCOPE_ID
from windows_agent.provisioning.secret_store import WindowsSecretStore
from windows_agent.runtime_config import build_api_client, load_agent_token, load_runtime_config

PIN_ENV = {
    "TRADEJOURNAL_MT5_TEMPLATE_SHA256": "1" * 64,
    "TRADEJOURNAL_MT5_EXPERT_SHA256": "2" * 64,
    "TRADEJOURNAL_TRADING_INGESTION_URL": "https://agent.example/trading-mt5-events",
}


def test_load_runtime_config_requires_api_url():
    with pytest.raises(ValueError, match="TRADEJOURNAL_API_URL"):
        load_runtime_config(env={})


def test_load_runtime_config_defaults(tmp_path):
    config = load_runtime_config(
        env={
            "TRADEJOURNAL_API_URL": "https://agent.example/trading-agent",
            **PIN_ENV,
        }
    )
    assert config.base_url == "https://agent.example/trading-agent"
    assert config.poll_seconds == 5.0
    assert config.broker_registry_path.name == "endpoint-registry.json"
    assert config.broker_artifact_manifest.name == "artifact-manifest.json"
    assert config.broker_artifact_root == config.broker_registry_path.parent
    assert config.broker_identity_cache.name == "broker-identity-cache.json"
    assert config.broker_identity_cache_ttl_seconds == 86_400
    assert config.broker_identity_model == "gpt-5.6"
    assert config.broker_wizard_enabled is False
    assert config.mt5_interactive_user == ""


def test_load_runtime_config_overrides(tmp_path):
    registry = tmp_path / "registry.json"
    artifact_root = tmp_path / "artifacts"
    manifest = tmp_path / "manifest.json"
    identity_cache = tmp_path / "identity-cache.json"
    config = load_runtime_config(
        env={
            "TRADEJOURNAL_API_URL": "https://agent.example/trading-agent",
            "TRADEJOURNAL_POLL_SECONDS": "12.5",
            "TRADEJOURNAL_SECRETS_ROOT": str(tmp_path),
            "TRADEJOURNAL_BROKER_REGISTRY": str(registry),
            "TRADEJOURNAL_BROKER_ARTIFACT_ROOT": str(artifact_root),
            "TRADEJOURNAL_BROKER_ARTIFACT_MANIFEST": str(manifest),
            "TRADEJOURNAL_BROKER_IDENTITY_CACHE": str(identity_cache),
            "TRADEJOURNAL_BROKER_IDENTITY_CACHE_TTL_SECONDS": "600",
            "TRADEJOURNAL_BROKER_IDENTITY_MODEL": "gpt-5.6-test",
            "TRADEJOURNAL_MT5_WIZARD_ENABLED": "1",
            "TRADEJOURNAL_MT5_INTERACTIVE_USER": "TradeJournalMT5",
            **PIN_ENV,
        }
    )
    assert config.poll_seconds == 12.5
    assert config.secrets_root == tmp_path
    assert config.broker_registry_path == registry
    assert config.broker_artifact_root == artifact_root
    assert config.broker_artifact_manifest == manifest
    assert config.broker_identity_cache == identity_cache
    assert config.broker_identity_cache_ttl_seconds == 600
    assert config.broker_identity_model == "gpt-5.6-test"
    assert config.broker_wizard_enabled is True
    assert config.mt5_interactive_user == "TradeJournalMT5"


@pytest.mark.parametrize("value", ["0", "59", "2592001", "not-an-int"])
def test_runtime_config_rejects_invalid_broker_identity_ttl(value):
    with pytest.raises(ValueError, match="BROKER_IDENTITY_CACHE_TTL_SECONDS"):
        load_runtime_config(
            env={
                "TRADEJOURNAL_API_URL": "https://agent.example/trading-agent",
                "TRADEJOURNAL_BROKER_IDENTITY_CACHE_TTL_SECONDS": value,
                **PIN_ENV,
            }
        )


@pytest.mark.parametrize("value", ["yes", "true", "2", "-1"])
def test_runtime_config_rejects_invalid_broker_wizard_gate(value):
    with pytest.raises(ValueError, match="TRADEJOURNAL_MT5_WIZARD_ENABLED"):
        load_runtime_config(
            env={
                "TRADEJOURNAL_API_URL": "https://agent.example/trading-agent",
                "TRADEJOURNAL_MT5_WIZARD_ENABLED": value,
                **PIN_ENV,
            }
        )


def test_runtime_config_requires_dedicated_user_when_wizard_is_enabled():
    with pytest.raises(ValueError, match="TRADEJOURNAL_MT5_INTERACTIVE_USER"):
        load_runtime_config(
            env={
                "TRADEJOURNAL_API_URL": "https://agent.example/trading-agent",
                "TRADEJOURNAL_MT5_WIZARD_ENABLED": "1",
                **PIN_ENV,
            }
        )


def test_build_api_client_reads_token_from_dpapi(tmp_path):
    store = WindowsSecretStore(tmp_path)
    store.write(AGENT_SCOPE_ID, "agent_token", "tjagent_fixturevalue")
    config = load_runtime_config(
        env={
            "TRADEJOURNAL_API_URL": "https://agent.example/trading-agent",
            "TRADEJOURNAL_SECRETS_ROOT": str(tmp_path),
            **PIN_ENV,
        }
    )
    assert load_agent_token(config.secrets_root) == "tjagent_fixturevalue"
    client = build_api_client(config)
    assert client.base_url == "https://agent.example/trading-agent/"


def test_runtime_config_rejects_missing_or_invalid_binary_pins():
    with pytest.raises(ValueError, match="TRADEJOURNAL_MT5_TEMPLATE_SHA256"):
        load_runtime_config(
            env={
                "TRADEJOURNAL_API_URL": "https://agent.example/trading-agent",
                "TRADEJOURNAL_TRADING_INGESTION_URL": "https://agent.example/trading-mt5-events",
            }
        )
    with pytest.raises(ValueError, match="TRADEJOURNAL_MT5_EXPERT_SHA256"):
        load_runtime_config(
            env={
                "TRADEJOURNAL_API_URL": "https://agent.example/trading-agent",
                "TRADEJOURNAL_MT5_TEMPLATE_SHA256": "1" * 64,
                "TRADEJOURNAL_TRADING_INGESTION_URL": "https://agent.example/trading-mt5-events",
            }
        )


def test_runtime_config_requires_secure_ingestion_endpoint():
    env = {
        "TRADEJOURNAL_API_URL": "https://agent.example/trading-agent",
        "TRADEJOURNAL_MT5_TEMPLATE_SHA256": "1" * 64,
        "TRADEJOURNAL_MT5_EXPERT_SHA256": "2" * 64,
    }
    with pytest.raises(ValueError, match="TRADEJOURNAL_TRADING_INGESTION_URL"):
        load_runtime_config(env=env)
    with pytest.raises(ValueError, match="HTTPS"):
        load_runtime_config(
            env={
                **env,
                "TRADEJOURNAL_TRADING_INGESTION_URL": "http://agent.example/events",
            }
        )
