from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import urlparse

from .agent_secrets import AGENT_SCOPE_ID, AGENT_TOKEN_SECRET_NAME
from .api_client import AgentApiClient
from .provisioning.secret_store import WindowsSecretStore

DEFAULT_SECRETS_ROOT = Path(r"C:\TradeJournal\secrets")
DEFAULT_INSTANCES_ROOT = Path(r"C:\TradeJournal\instances")
DEFAULT_SOURCE_TERMINAL = Path(r"C:\TradeJournal\mt5-template\terminal64.exe")
DEFAULT_EXPERT_BINARY = Path(r"C:\TradeJournal\mt5-template\MQL5\Experts\TradeJournal\TradeJournalBridge.ex5")
DEFAULT_BROKER_REGISTRY_ROOT = Path(r"C:\TradeJournal\broker-registry")
DEFAULT_BROKER_REGISTRY = DEFAULT_BROKER_REGISTRY_ROOT / "endpoint-registry.json"
DEFAULT_BROKER_ARTIFACT_MANIFEST = (
    DEFAULT_BROKER_REGISTRY_ROOT / "artifact-manifest.json"
)
DEFAULT_BROKER_IDENTITY_CACHE = (
    DEFAULT_BROKER_REGISTRY_ROOT / "broker-identity-cache.json"
)
DEFAULT_BROKER_IDENTITY_CACHE_TTL_SECONDS = 24 * 60 * 60
DEFAULT_BROKER_IDENTITY_MODEL = "gpt-5.6"
DEFAULT_POLL_SECONDS = 5.0


@dataclass(frozen=True)
class AgentRuntimeConfig:
    base_url: str
    poll_seconds: float
    secrets_root: Path
    instances_root: Path = DEFAULT_INSTANCES_ROOT
    source_terminal: Path = DEFAULT_SOURCE_TERMINAL
    expert_binary: Path = DEFAULT_EXPERT_BINARY
    terminal_sha256: str = ""
    expert_sha256: str = ""
    trading_ingestion_url: str = ""
    broker_registry_path: Path = DEFAULT_BROKER_REGISTRY
    broker_artifact_root: Path = DEFAULT_BROKER_REGISTRY_ROOT
    broker_artifact_manifest: Path = DEFAULT_BROKER_ARTIFACT_MANIFEST
    broker_identity_cache: Path = DEFAULT_BROKER_IDENTITY_CACHE
    broker_identity_cache_ttl_seconds: int = DEFAULT_BROKER_IDENTITY_CACHE_TTL_SECONDS
    broker_identity_model: str = DEFAULT_BROKER_IDENTITY_MODEL


def _required_sha256(source: Mapping[str, str], name: str) -> str:
    value = source.get(name, "").strip().lower()
    if (
        len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{name} is required and must be a SHA-256 digest")
    return value


def _required_endpoint(source: Mapping[str, str], name: str) -> str:
    value = source.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"{name} is invalid") from exc
    if (
        not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (parsed.scheme != "https" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"))
        or port is not None
        and not 1 <= port <= 65535
    ):
        raise ValueError(f"{name} must be an HTTPS endpoint without credentials")
    return value.rstrip("/")


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
    broker_registry_path = Path(
        source.get("TRADEJOURNAL_BROKER_REGISTRY", "").strip()
        or DEFAULT_BROKER_REGISTRY
    )
    broker_artifact_root = Path(
        source.get("TRADEJOURNAL_BROKER_ARTIFACT_ROOT", "").strip()
        or DEFAULT_BROKER_REGISTRY_ROOT
    )
    broker_artifact_manifest = Path(
        source.get("TRADEJOURNAL_BROKER_ARTIFACT_MANIFEST", "").strip()
        or DEFAULT_BROKER_ARTIFACT_MANIFEST
    )
    broker_identity_cache = Path(
        source.get("TRADEJOURNAL_BROKER_IDENTITY_CACHE", "").strip()
        or DEFAULT_BROKER_IDENTITY_CACHE
    )
    identity_ttl_raw = source.get(
        "TRADEJOURNAL_BROKER_IDENTITY_CACHE_TTL_SECONDS", ""
    ).strip()
    try:
        broker_identity_cache_ttl_seconds = (
            int(identity_ttl_raw)
            if identity_ttl_raw
            else DEFAULT_BROKER_IDENTITY_CACHE_TTL_SECONDS
        )
    except ValueError as exc:
        raise ValueError(
            "TRADEJOURNAL_BROKER_IDENTITY_CACHE_TTL_SECONDS is invalid"
        ) from exc
    if not 60 <= broker_identity_cache_ttl_seconds <= 30 * 24 * 60 * 60:
        raise ValueError(
            "TRADEJOURNAL_BROKER_IDENTITY_CACHE_TTL_SECONDS is invalid"
        )
    broker_identity_model = source.get(
        "TRADEJOURNAL_BROKER_IDENTITY_MODEL", ""
    ).strip() or DEFAULT_BROKER_IDENTITY_MODEL
    terminal_sha256 = _required_sha256(
        source, "TRADEJOURNAL_MT5_TEMPLATE_SHA256"
    )
    expert_sha256 = _required_sha256(
        source, "TRADEJOURNAL_MT5_EXPERT_SHA256"
    )
    # Distinct from base_url (the trading-agent control plane): live_sync is scheduled
    # automatically after provisioning, so accepting a daemon without its ingestion endpoint
    # would create a connection that looks provisioned but can never publish events.
    trading_ingestion_url = _required_endpoint(
        source, "TRADEJOURNAL_TRADING_INGESTION_URL"
    )
    return AgentRuntimeConfig(
        base_url=base_url,
        poll_seconds=poll_seconds,
        secrets_root=secrets_root,
        instances_root=instances_root,
        source_terminal=source_terminal,
        expert_binary=expert_binary,
        terminal_sha256=terminal_sha256,
        expert_sha256=expert_sha256,
        trading_ingestion_url=trading_ingestion_url,
        broker_registry_path=broker_registry_path,
        broker_artifact_root=broker_artifact_root,
        broker_artifact_manifest=broker_artifact_manifest,
        broker_identity_cache=broker_identity_cache,
        broker_identity_cache_ttl_seconds=broker_identity_cache_ttl_seconds,
        broker_identity_model=broker_identity_model,
    )


def load_agent_token(secrets_root: Path) -> str:
    """Reads the Agent's own bearer token from DPAPI storage -- see
    scripts/windows/receive-agent-token.ps1 for how it gets there."""
    store = WindowsSecretStore(secrets_root)
    return store.read(AGENT_SCOPE_ID, AGENT_TOKEN_SECRET_NAME)


def build_api_client(config: AgentRuntimeConfig) -> AgentApiClient:
    token = load_agent_token(config.secrets_root)
    return AgentApiClient(config.base_url, token)
