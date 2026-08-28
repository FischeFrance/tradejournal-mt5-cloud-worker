from __future__ import annotations

from datetime import time

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
    assert config.mt5_maintenance_enabled is False
    assert config.mt5_maintenance_local_time == time(23, 30)
    assert config.mt5_maintenance_timezone == "Europe/Rome"
    assert config.mt5_maintenance_grace_minutes == 120


def test_load_runtime_config_accepts_nightly_maintenance_settings(tmp_path):
    state_path = tmp_path / "maintenance.json"
    config = load_runtime_config(
        env=_runtime_env(
            TRADEJOURNAL_MT5_MAINTENANCE_ENABLED="1",
            TRADEJOURNAL_MT5_MAINTENANCE_LOCAL_TIME="22:45",
            TRADEJOURNAL_MT5_MAINTENANCE_TIMEZONE="UTC",
            TRADEJOURNAL_MT5_MAINTENANCE_GRACE_MINUTES="90",
            TRADEJOURNAL_MT5_MAINTENANCE_STATE_PATH=str(state_path),
            TRADEJOURNAL_MT5_INTERACTIVE_USER="TradeJournalMT5",
        )
    )
    assert config.mt5_maintenance_enabled is True
    assert config.mt5_maintenance_local_time == time(22, 45)
    assert config.mt5_maintenance_timezone == "UTC"
    assert config.mt5_maintenance_grace_minutes == 90
    assert config.mt5_maintenance_state_path == state_path
    assert config.mt5_interactive_user == "TradeJournalMT5"


@pytest.mark.parametrize(
    "interactive_user",
    ("", "Administrator", "SYSTEM", "Alice", "TradeJournalAgent"),
)
def test_nightly_maintenance_requires_dedicated_interactive_user(
    interactive_user: str,
) -> None:
    with pytest.raises(ValueError, match="dedicated local user"):
        load_runtime_config(
            env=_runtime_env(
                TRADEJOURNAL_MT5_MAINTENANCE_ENABLED="1",
                TRADEJOURNAL_MT5_INTERACTIVE_USER=interactive_user,
            )
        )


@pytest.mark.parametrize(
    ("name", "value"),
    (
        ("TRADEJOURNAL_MT5_MAINTENANCE_ENABLED", "yes"),
        ("TRADEJOURNAL_MT5_MAINTENANCE_LOCAL_TIME", "23:7"),
        ("TRADEJOURNAL_MT5_MAINTENANCE_TIMEZONE", "Invalid/Timezone"),
        ("TRADEJOURNAL_MT5_MAINTENANCE_GRACE_MINUTES", "4"),
    ),
)
def test_invalid_maintenance_settings_are_rejected(name: str, value: str):
    with pytest.raises(ValueError):
        load_runtime_config(env=_runtime_env(**{name: value}))


def test_legacy_poll_setting_is_ignored(tmp_path):
    config = load_runtime_config(
        env=_runtime_env(
            TRADEJOURNAL_POLL_SECONDS="12.5",
            TRADEJOURNAL_SECRETS_ROOT=str(tmp_path),
        )
    )
    assert not hasattr(config, "poll_seconds")
    assert config.secrets_root == tmp_path


def test_build_api_client_reads_token_from_dpapi(tmp_path, monkeypatch):
    monkeypatch.setattr(
        WindowsSecretStore,
        "_crypt_protect",
        staticmethod(lambda value: value),
    )
    monkeypatch.setattr(
        WindowsSecretStore,
        "_crypt_unprotect",
        staticmethod(lambda value: value),
    )
    monkeypatch.setattr(
        WindowsSecretStore,
        "restrict_acl",
        staticmethod(lambda _path: None),
    )
    store = WindowsSecretStore(tmp_path)
    store.write(AGENT_SCOPE_ID, "agent_token", "tjagent_fixturevalue")
    config = load_runtime_config(env=_runtime_env(TRADEJOURNAL_SECRETS_ROOT=str(tmp_path)))
    assert load_agent_token(config.secrets_root) == "tjagent_fixturevalue"
    client = build_api_client(config)
    assert client.base_url == "https://agent.example/trading-agent/"
