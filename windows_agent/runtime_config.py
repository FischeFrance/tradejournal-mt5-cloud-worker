from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .agent_secrets import AGENT_SCOPE_ID, AGENT_TOKEN_SECRET_NAME
from .api_client import AgentApiClient
from .provisioning.secret_store import WindowsSecretStore

DEFAULT_SECRETS_ROOT = Path(r"C:\TradeJournal\secrets")
DEFAULT_INSTANCES_ROOT = Path(r"C:\TradeJournal\instances")
DEFAULT_SOURCE_TERMINAL = Path(r"C:\TradeJournal\mt5-template\terminal64.exe")
DEFAULT_EXPERT_BINARY = Path(r"C:\TradeJournal\mt5-template\MQL5\Experts\TradeJournal\TradeJournalBridge.ex5")
DEFAULT_POLL_SECONDS = 5.0


@dataclass(frozen=True)
class AgentRuntimeConfig:
    base_url: str
    poll_seconds: float
    secrets_root: Path
    instances_root: Path = DEFAULT_INSTANCES_ROOT
    source_terminal: Path = DEFAULT_SOURCE_TERMINAL
    expert_binary: Path = DEFAULT_EXPERT_BINARY
    trading_ingestion_url: str = ""


def load_runtime_config(env: dict[str, str] | None = None) -> AgentRuntimeConfig:
    """Reads the control-plane base URL from TRADEJOURNAL_API_URL (required) and the poll
    interval from TRADEJOURNAL_POLL_SECONDS (optional). The agent bearer token is deliberately
    NOT read here -- it comes only from DPAPI via build_api_client(), never from an environment
    variable, so it can never leak into a process listing or a crash dump's env snapshot."""
    source = env if env is not None else os.environ
    base_url = source.get("TRADEJOURNAL_API_URL", "").strip()
    if not base_url:
        raise ValueError("TRADEJOURNAL_API_URL is required (e.g. https://<project-ref>.functions.supabase.co/trading-agent)")
    poll_raw = source.get("TRADEJOURNAL_POLL_SECONDS", "").strip()
    poll_seconds = float(poll_raw) if poll_raw else DEFAULT_POLL_SECONDS
    secrets_root = Path(source.get("TRADEJOURNAL_SECRETS_ROOT", "").strip() or DEFAULT_SECRETS_ROOT)
    instances_root = Path(source.get("TRADEJOURNAL_INSTANCES_ROOT", "").strip() or DEFAULT_INSTANCES_ROOT)
    source_terminal = Path(source.get("TRADEJOURNAL_SOURCE_TERMINAL", "").strip() or DEFAULT_SOURCE_TERMINAL)
    expert_binary = Path(source.get("TRADEJOURNAL_EXPERT_BINARY", "").strip() or DEFAULT_EXPERT_BINARY)
    # Distinct from base_url (the trading-agent job control-plane): this is the trading-mt5-events
    # ingestion endpoint the live_sync job posts detected trades/heartbeats to, bridge-token
    # authenticated, exactly like the manual EA/self-hosted worker connectors already do. Left
    # optional here (not raised at startup like base_url) so provision/deprovision/historical_sync
    # keep working unattended even before an operator sets this; live_sync itself raises a clear
    # config error if it's missing when actually needed.
    trading_ingestion_url = source.get("TRADEJOURNAL_TRADING_INGESTION_URL", "").strip()
    return AgentRuntimeConfig(
        base_url=base_url,
        poll_seconds=poll_seconds,
        secrets_root=secrets_root,
        instances_root=instances_root,
        source_terminal=source_terminal,
        expert_binary=expert_binary,
        trading_ingestion_url=trading_ingestion_url,
    )


def load_agent_token(secrets_root: Path) -> str:
    """Reads the Agent's own bearer token from DPAPI storage -- see
    scripts/windows/receive-agent-token.ps1 for how it gets there."""
    store = WindowsSecretStore(secrets_root)
    return store.read(AGENT_SCOPE_ID, AGENT_TOKEN_SECRET_NAME)


def build_api_client(config: AgentRuntimeConfig) -> AgentApiClient:
    token = load_agent_token(config.secrets_root)
    return AgentApiClient(config.base_url, token)
