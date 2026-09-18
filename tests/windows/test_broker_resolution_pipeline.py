from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import uuid4

import pytest

from windows_agent import real_handlers
from windows_agent.agent_errors import CredentialEnvelopeInvalid
from windows_agent.broker_endpoint_resolver import (
    BrokerEndpointResolutionError,
    VerifiedBrokerEndpoint,
)
from windows_agent.broker_registry import (
    BrokerRegistry,
    BrokerResolution,
    ResolutionMethod,
)
from windows_agent.broker_resolution import (
    ZeroLicenseBrokerResolver,
    validate_resolution_plan,
)
from windows_agent.broker_wizard import BrokerWizardEvidence


class AcknowledgingApi:
    @staticmethod
    def heartbeat(_job_id: str, _lease_id: str) -> dict[str, bool]:
        return {"lease_valid": True}

    @staticmethod
    def progress(
        _job_id: str,
        _lease_id: str,
        _event_code: str,
        _event_status: str,
        _detail_code: str | None = None,
    ) -> dict[str, bool]:
        return {"event_recorded": True}


def _verified_endpoint(
    *,
    broker_label: str = "Generic Markets",
    server_name: str = "Generic-Live",
) -> VerifiedBrokerEndpoint:
    return VerifiedBrokerEndpoint(
        broker_label=broker_label,
        server_name=server_name,
        host="203.0.113.10",
        port=443,
        protocol="TCP/TLS",
        observed_at_unix_ms=1,
        discovery_method="MT5_MANAGED_INVESTOR_LOGIN",
        verification_pid=123,
        process_creation_time_unix_ms=1,
        verification_session_id="12345678-1234-4234-8234-123456789abc",
        confidence="HIGH",
        artifact_relative_path="artifacts/fixture.json",
        artifact_sha256="1" * 64,
    )


def test_stale_terminal_cleanup_failure_stops_before_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    terminal = tmp_path / "terminal64.exe"
    terminal.write_bytes(b"terminal")
    monkeypatch.setattr(
        real_handlers.ProcessManager,
        "find",
        staticmethod(lambda _terminal: [123]),
    )

    class Process:
        @staticmethod
        def cleanup_path(_terminal: Path) -> bool:
            return False

    with pytest.raises(Exception) as captured:
        real_handlers._ensure_no_stale_process(
            terminal,
            tmp_path / "process.json",
            lambda _state: Process(),
        )

    assert captured.value.error_code == "terminal_start_failed"


def test_known_fpm_profile_keeps_registry_terminal_discovery_plan() -> None:
    plan = ZeroLicenseBrokerResolver().resolve("FPMTrading-Live")

    assert plan.method is ResolutionMethod.TERMINAL_DISCOVERY
    assert plan.expected_server == "FPMTrading-Live"
    assert plan.connection_target is None
    assert "FPM Trading" in plan.discovery_queries
    assert plan.profile_id == "fpm-trading-live"


def test_unknown_valid_server_gets_exact_terminal_discovery_plan() -> None:
    plan = ZeroLicenseBrokerResolver().resolve("NewBroker-Live")

    assert plan.method is ResolutionMethod.TERMINAL_DISCOVERY
    assert plan.requested_server == "NewBroker-Live"
    assert plan.expected_server == "NewBroker-Live"
    assert plan.connection_target is None
    assert plan.discovery_queries == ("NewBroker-Live",)
    assert plan.matched_by == "generic_exact_server"


def test_unknown_server_hint_is_additive_and_never_replaces_exact_server() -> None:
    plan = ZeroLicenseBrokerResolver().resolve(
        "NewBroker-Live", broker_hint="New Broker Ltd."
    )

    assert plan.method is ResolutionMethod.TERMINAL_DISCOVERY
    assert plan.discovery_queries == ("NewBroker-Live", "New Broker Ltd.")
    assert plan.connection_target is None


@pytest.mark.parametrize(
    "hint",
    [
        "",
        " leading",
        "trailing ",
        "../broker",
        "broker\\name",
        "broker\nname",
        "broker\u200bname",
        "127.0.0.1:22",
        "localhost",
        "metadata.google.internal",
        "[::1]",
        42,
    ],
)
def test_generic_broker_hint_is_strictly_validated(hint: object) -> None:
    with pytest.raises(ValueError, match="broker_hint"):
        ZeroLicenseBrokerResolver().resolve("NewBroker-Live", broker_hint=hint)


def test_conflicting_hint_for_known_profile_fails_closed() -> None:
    with pytest.raises(ValueError, match="conflicts"):
        ZeroLicenseBrokerResolver().resolve(
            "FPMTrading-Live", broker_hint="Different Broker Ltd"
        )


def test_unknown_server_can_never_be_promoted_to_direct() -> None:
    plan = ZeroLicenseBrokerResolver().resolve(
        "Unknown-Live", broker_hint="Unknown Markets"
    )

    assert plan.method is not ResolutionMethod.DIRECT_ENDPOINT
    assert plan.connection_target is None


@pytest.mark.parametrize(
    "server",
    [
        "127.0.0.1",
        "localhost",
        "metadata.google.internal",
        "10.0.0.1",
        "0x7f000001",
    ],
)
def test_unknown_network_target_is_never_sent_to_terminal(server: str) -> None:
    with pytest.raises(ValueError, match="network target"):
        ZeroLicenseBrokerResolver().resolve(server)


def test_direct_plan_is_available_only_from_validated_registry_profile() -> None:
    registry = BrokerRegistry.from_dict(
        {
            "schema_version": 1,
            "revision": 7,
            "profiles": [
                {
                    "profile_id": "known-live",
                    "broker_id": "known",
                    "broker_name": "Known Markets",
                    "server": "Known-Live",
                    "environment": "live",
                    "aliases": [],
                    "discovery_queries": ["Known Markets"],
                    "source": "unit-test",
                    "connection_target": "mt5.known.example:443",
                    "target_verified_at": "2026-07-22T00:00:00Z",
                }
            ],
        }
    )

    plan = ZeroLicenseBrokerResolver(registry).resolve("Known-Live")

    assert plan.method is ResolutionMethod.DIRECT_ENDPOINT
    assert plan.connection_target == "mt5.known.example:443"


def test_injected_ambiguous_or_inconsistent_plan_is_rejected() -> None:
    inconsistent = BrokerResolution(
        requested_server="Requested-Live",
        method=ResolutionMethod.TERMINAL_DISCOVERY,
        expected_server="Other-Live",
        connection_target="mt5.other.example:443",
        discovery_queries=("Other-Live",),
        profile_id=None,
        broker_id=None,
        broker_name=None,
        environment=None,
        revision=1,
        matched_by="bad-injected-plan",
    )

    with pytest.raises(ValueError):
        validate_resolution_plan(inconsistent, requested_server="Requested-Live")


def test_injected_direct_endpoint_cannot_self_certify_its_target() -> None:
    untrusted = BrokerResolution(
        requested_server="Requested-Live",
        method=ResolutionMethod.DIRECT_ENDPOINT,
        expected_server="Requested-Live",
        connection_target="attacker.invalid:443",
        discovery_queries=("Requested-Live",),
        profile_id="injected",
        broker_id="injected",
        broker_name="Injected",
        environment="live",
        revision=1,
        matched_by="exact",
    )

    with pytest.raises(ValueError, match="untrusted"):
        validate_resolution_plan(untrusted, requested_server="Requested-Live")


def test_injected_discovery_query_cannot_be_a_network_probe() -> None:
    untrusted = BrokerResolution(
        requested_server="Requested-Live",
        method=ResolutionMethod.TERMINAL_DISCOVERY,
        expected_server="Requested-Live",
        connection_target=None,
        discovery_queries=("127.0.0.1:22",),
        profile_id=None,
        broker_id=None,
        broker_name=None,
        environment=None,
        revision=1,
        matched_by="injected",
    )

    with pytest.raises(ValueError, match="network target"):
        validate_resolution_plan(untrusted, requested_server="Requested-Live")


def test_provision_resolves_before_decrypting_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []

    def resolve_endpoint(
        broker_label: str | None,
        server: str,
    ) -> VerifiedBrokerEndpoint:
        events.append(f"resolve:{broker_label}:{server}")
        return _verified_endpoint()

    def reject_decrypt(_payload: dict, _secrets_root: Path) -> str:
        events.append("decrypt")
        raise CredentialEnvelopeInvalid("stop after ordering assertion")

    monkeypatch.setattr(real_handlers, "_decrypt_envelope", reject_decrypt)
    handlers = real_handlers.build_real_handlers(
        AcknowledgingApi(),
        instances_root=tmp_path / "instances",
        secrets_root=tmp_path / "secrets",
        source_terminal=tmp_path / "terminal64.exe",
        endpoint_resolver=resolve_endpoint,
    )
    job = {
        "job_id": "ordering",
        "job_type": "provision",
        "connection_id": str(uuid4()),
        "lease_id": "lease",
        "history_mode": "new_only",
        "payload": {
            "expected_login": 42,
            "expected_server": "Generic-Live",
            "broker_label": "Generic Markets",
            "credential_envelope": {"opaque": True},
        },
    }

    with pytest.raises(CredentialEnvelopeInvalid):
        handlers["provision"](job)

    assert events == ["resolve:Generic Markets:Generic-Live", "decrypt"]


def test_invalid_hint_stops_before_decrypting_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    decrypt_called = False

    def unexpected_decrypt(_payload: dict, _secrets_root: Path) -> str:
        nonlocal decrypt_called
        decrypt_called = True
        return "should-not-be-read"

    class Api:
        @staticmethod
        def heartbeat(_job_id: str, _lease_id: str) -> dict[str, bool]:
            return {"lease_valid": True}

    monkeypatch.setattr(real_handlers, "_decrypt_envelope", unexpected_decrypt)
    handlers = real_handlers.build_real_handlers(
        Api(),
        instances_root=tmp_path / "instances",
        secrets_root=tmp_path / "secrets",
        source_terminal=tmp_path / "terminal64.exe",
    )
    job = {
        "job_id": "invalid-hint",
        "job_type": "provision",
        "connection_id": str(uuid4()),
        "lease_id": "lease",
        "history_mode": "new_only",
        "payload": {
            "expected_login": 42,
            "expected_server": "Generic-Live",
            "broker_label": "bad\nlabel",
            "credential_envelope": {"opaque": True},
        },
    }

    with pytest.raises(CredentialEnvelopeInvalid):
        handlers["provision"](job)

    assert decrypt_called is False


def test_provision_passes_verified_endpoint_to_native_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[tuple[str, str]] = []
    resolution_calls: list[tuple[str | None, str]] = []

    monkeypatch.setattr(
        real_handlers,
        "_decrypt_envelope",
        lambda _payload, _secrets_root: "opaque-test-password",
    )
    monkeypatch.setattr(
        real_handlers.WindowsSecretStore,
        "write",
        lambda _self, _connection_id, _name, _value: None,
    )

    def fake_native_helper(*args: object, **_kwargs: object) -> dict[str, object]:
        server = args[5]
        endpoint = args[6]
        assert isinstance(server, str)
        assert isinstance(endpoint, str)
        captured.append((server, endpoint))
        return {"live_sync_started": True}

    def resolve_endpoint(
        broker_label: str | None,
        server: str,
    ) -> VerifiedBrokerEndpoint:
        resolution_calls.append((broker_label, server))
        return _verified_endpoint()

    monkeypatch.setattr(
        real_handlers, "_start_file_bridge_and_sync", fake_native_helper
    )
    source_terminal = tmp_path / "template" / "terminal64.exe"
    source_terminal.parent.mkdir(parents=True)
    source_terminal.write_bytes(b"stub")
    handlers = real_handlers.build_real_handlers(
        AcknowledgingApi(),
        instances_root=tmp_path / "instances",
        secrets_root=tmp_path / "secrets",
        source_terminal=source_terminal,
        endpoint_resolver=resolve_endpoint,
    )
    job = {
        "job_id": "generic-plan",
        "job_type": "provision",
        "connection_id": str(uuid4()),
        "lease_id": "lease",
        "history_mode": "new_only",
        "payload": {
            "expected_login": 42,
            "expected_server": "Generic-Live",
            "broker_label": "Generic Markets",
            "credential_envelope": {"opaque": True},
        },
    }

    result = handlers["provision"](job)

    assert result["live_sync_started"] is True
    assert result["verified_server_name"] == "Generic-Live"
    assert result["verified_broker_label"] == "Generic Markets"
    assert resolution_calls == [("Generic Markets", "Generic-Live")]
    assert captured == [("Generic-Live", "203.0.113.10:443")]


def test_broker_wizard_runs_before_credential_decryption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    def reject_endpoint(
        _broker_label: str | None,
        _server: str,
    ) -> VerifiedBrokerEndpoint:
        raise BrokerEndpointResolutionError("fixture registry miss")

    def run_wizard(
        root: Path,
        search_text: str,
        suggested_broker_label: str,
        expected_server: str,
        cancel_check: object,
    ) -> BrokerWizardEvidence:
        events.append("wizard")
        assert search_text == suggested_broker_label == "Generic Markets"
        assert expected_server == "Generic-Live"
        assert cancel_check is None
        artifact = root / "state" / "broker-wizard-result.json"
        artifact.write_text('{"status":"SUCCESS"}', encoding="utf-8")
        return BrokerWizardEvidence(
            run_id="12345678-1234-4234-8234-123456789abc",
            expected_server_name=expected_server,
            selected_broker_label=suggested_broker_label,
            censused_server_names=(expected_server,),
            terminal_pid=123,
            completed_at_unix_ms=1,
            artifact_path=artifact,
            artifact_sha256=hashlib.sha256(artifact.read_bytes()).hexdigest(),
        )

    def reject_decrypt(_payload: dict, _secrets_root: Path) -> str:
        events.append("decrypt")
        raise CredentialEnvelopeInvalid("stop after ordering assertion")

    monkeypatch.setattr(real_handlers, "_decrypt_envelope", reject_decrypt)
    source_terminal = tmp_path / "template" / "terminal64.exe"
    source_terminal.parent.mkdir(parents=True)
    source_terminal.write_bytes(b"stub")
    handlers = real_handlers.build_real_handlers(
        AcknowledgingApi(),
        instances_root=tmp_path / "instances",
        secrets_root=tmp_path / "secrets",
        source_terminal=source_terminal,
        endpoint_resolver=reject_endpoint,
        broker_wizard=run_wizard,
    )

    with pytest.raises(CredentialEnvelopeInvalid):
        handlers["provision"](
            {
                "job_id": "job",
                "job_type": "provision",
                "connection_id": str(uuid4()),
                "lease_id": "lease",
                "history_mode": "new_only",
                "payload": {
                    "expected_login": 42,
                    "expected_server": "Generic-Live",
                    "broker_label": "Generic Markets",
                    "credential_envelope": {"opaque": True},
                },
            }
        )

    assert events == ["wizard", "decrypt"]


def test_broker_wizard_lease_guard_aborts_before_credential_decryption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    guard_calls = 0

    def lease_guard() -> None:
        nonlocal guard_calls
        guard_calls += 1
        events.append(f"guard:{guard_calls}")
        if guard_calls == 2:
            raise real_handlers.LeaseLost("fixture lease lost")

    def reject_endpoint(
        _broker_label: str | None,
        _server: str,
    ) -> VerifiedBrokerEndpoint:
        raise BrokerEndpointResolutionError("fixture registry miss")

    def run_wizard(
        _root: Path,
        _search_text: str,
        _suggested_broker_label: str,
        _expected_server: str,
        cancel_check: object,
    ) -> BrokerWizardEvidence:
        events.append("wizard")
        assert callable(cancel_check)
        cancel_check()
        raise AssertionError("lease loss must abort the wizard")

    def unexpected_decrypt(_payload: dict, _secrets_root: Path) -> str:
        events.append("unexpected-decrypt")
        return "opaque-password"

    monkeypatch.setattr(real_handlers, "_decrypt_envelope", unexpected_decrypt)
    source_terminal = tmp_path / "template" / "terminal64.exe"
    source_terminal.parent.mkdir(parents=True)
    source_terminal.write_bytes(b"stub")
    handlers = real_handlers.build_real_handlers(
        AcknowledgingApi(),
        instances_root=tmp_path / "instances",
        secrets_root=tmp_path / "secrets",
        source_terminal=source_terminal,
        endpoint_resolver=reject_endpoint,
        broker_wizard=run_wizard,
    )
    job = {
        "job_id": "job",
        "job_type": "provision",
        "connection_id": str(uuid4()),
        "lease_id": "lease",
        "history_mode": "new_only",
        "payload": {
            "expected_login": 42,
            "expected_server": "Generic-Live",
            "broker_label": "Generic Markets",
            "credential_envelope": {"opaque": True},
        },
        "_lease_guard": lease_guard,
    }

    with pytest.raises(real_handlers.LeaseLost):
        handlers["provision"](job)

    assert events == ["guard:1", "wizard", "guard:2"]
