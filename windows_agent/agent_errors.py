from __future__ import annotations

"""Sanitized error taxonomy for the real provision/historical_sync/deprovision handlers.

Every exception here carries an explicit `error_code` class attribute matching
`^[a-z0-9_]{1,64}$` (see contracts/mt5-agent-v1/schema.json's errorCode def). JobRunner.run_once
reads this attribute (falling back to the exception class name for anything unexpected, e.g. a
bare NotImplementedError) before sending it to the control plane -- never the exception message,
which may contain interpolated detail meant only for local logs.
"""


class AgentError(RuntimeError):
    error_code = "internal_agent_error"


class CredentialEnvelopeInvalid(AgentError):
    error_code = "credential_envelope_invalid"


class CredentialDecryptionFailed(AgentError):
    error_code = "credential_decryption_failed"


class SecretStoreFailed(AgentError):
    error_code = "secret_store_failed"


class InstanceProvisionFailed(AgentError):
    error_code = "instance_provision_failed"


class TerminalStartFailed(AgentError):
    error_code = "terminal_start_failed"


class Mt5InitializeFailed(AgentError):
    error_code = "mt5_initialize_failed"


class Mt5AuthorizationFailed(AgentError):
    error_code = "mt5_authorization_failed"


class AccountIdentityMismatch(AgentError):
    error_code = "account_identity_mismatch"


class ServerIdentityMismatch(AgentError):
    error_code = "server_identity_mismatch"


class InvestorAccessNotVerified(AgentError):
    error_code = "investor_access_not_verified"


class HistorySyncFailed(AgentError):
    error_code = "history_sync_failed"


class LiveSyncFailed(AgentError):
    error_code = "live_sync_failed"


class DeprovisionFailed(AgentError):
    error_code = "deprovision_failed"


class InternalAgentError(AgentError):
    error_code = "internal_agent_error"
