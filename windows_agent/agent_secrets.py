from __future__ import annotations

"""Well-known DPAPI storage scope for secrets that belong to this Agent/VPS as a whole
(agent bearer token, MT5 provisioning encryption key) rather than to any single customer
connection. WindowsSecretStore is keyed by a canonical UUID "connection_id" -- this sentinel
reuses that same mechanism (no new DPAPI code path) for the agent-level scope. Never allocate
a real customer connection with this id."""

AGENT_SCOPE_ID = "00000000-0000-0000-0000-000000000000"
AGENT_TOKEN_SECRET_NAME = "agent_token"
PROVISIONING_KEY_SECRET_NAME = "mt5_provisioning_key"
