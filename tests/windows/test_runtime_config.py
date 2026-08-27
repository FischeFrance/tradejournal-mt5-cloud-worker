from __future__ import annotations

import pytest

from windows_agent.agent_secrets import AGENT_SCOPE_ID
from windows_agent.provisioning.secret_store import WindowsSecretStore
from windows_agent.runtime_config import build_api_client, load_agent_token, load_runtime_config


def _runtime_env(**overrides: str) -> dict[str, str]:
    return {
        "TRADEJOURNAL_API_URL": "https://agent.example/trading-agent",
        "TRADEJOURNAL_MT5_TEMPLATE_SHA256": "a" * 64,
        "TRADEJOURNAL_MT5_EXPERT_SHA256": "b" * 64,
        "TRADEJOURNAL_TRADING_INGESTION_URL": "https://agent.example/trading-mt5-events",
        **overrides,
    }


def test_load_runtime_config_requires_api_url():
    with pytest.raises(ValueError, match="TRADEJOURNAL_API_URL"):
        load_runtime_config(env={})


def test_load_runtime_config_defaults(tmp_path):
    config = load_runtime_config(env=_runtime_env())
    assert config.base_url == "https://agent.example/trading-agent"
    assert not hasattr(config, "poll_seconds")


def test_legacy_poll_setting_is_ignored(tmp_path):
    config = load_runtime_config(
        env=_runtime_env(
            TRADEJOURNAL_POLL_SECONDS="12.5",
            TRADEJOURNAL_SECRETS_ROOT=str(tmp_path),
        )
    )
    assert not hasattr(config, "poll_seconds")
    assert config.secrets_root == tmp_path


def test_build_api_client_reads_token_from_dpapi(tmp_path):
    store = WindowsSecretStore(tmp_path)
    store.write(AGENT_SCOPE_ID, "agent_token", "tjagent_fixturevalue")
    config = load_runtime_config(env=_runtime_env(TRADEJOURNAL_SECRETS_ROOT=str(tmp_path)))
    assert load_agent_token(config.secrets_root) == "tjagent_fixturevalue"
    client = build_api_client(config)
    assert client.base_url == "https://agent.example/trading-agent/"
