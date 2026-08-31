from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from windows_agent import real_handlers
from windows_agent.agent_errors import CredentialEnvelopeInvalid
from windows_agent.broker_registry import (
    BrokerRegistry,
    BrokerResolution,
    ResolutionMethod,
)
from windows_agent.broker_resolution import (
    ZeroLicenseBrokerResolver,
    validate_resolution_plan,
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

    class Api:
        @staticmethod
        def heartbeat(_job_id: str, _lease_id: str) -> dict[str, bool]:
            return {"lease_valid": True}

    class Resolver:
        @staticmethod
        def resolve(expected_server: str, *, broker_hint: object = None) -> BrokerResolution:
            events.append(f"resolve:{expected_server}:{broker_hint}")
            return ZeroLicenseBrokerResolver().resolve(
                expected_server, broker_hint=broker_hint
            )

    def reject_decrypt(_payload: dict, _secrets_root: Path) -> str:
        events.append("decrypt")
        raise CredentialEnvelopeInvalid("stop after ordering assertion")

    monkeypatch.setattr(real_handlers, "_decrypt_envelope", reject_decrypt)
    handlers = real_handlers.build_real_handlers(
        Api(),
        instances_root=tmp_path / "instances",
        secrets_root=tmp_path / "secrets",
        source_terminal=tmp_path / "terminal64.exe",
        broker_resolver=Resolver(),
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
            "broker_hint": "Generic Markets",
            "credential_envelope": {"opaque": True},
        },
    }

    with pytest.raises(CredentialEnvelopeInvalid):
        handlers["provision"](job)

    assert events == ["resolve:Generic-Live:Generic Markets", "decrypt"]


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
            "broker_hint": "../ambiguous",
            "credential_envelope": {"opaque": True},
        },
    }

    with pytest.raises(CredentialEnvelopeInvalid):
        handlers["provision"](job)

    assert decrypt_called is False


def test_provision_passes_generic_resolution_plan_to_native_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[BrokerResolution] = []

    class Api:
        @staticmethod
        def heartbeat(_job_id: str, _lease_id: str) -> dict[str, bool]:
            return {"lease_valid": True}

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
        plan = args[5]
        assert isinstance(plan, BrokerResolution)
        captured.append(plan)
        return {"live_sync_started": True}

    monkeypatch.setattr(
        real_handlers, "_start_file_bridge_and_sync", fake_native_helper
    )
    source_terminal = tmp_path / "template" / "terminal64.exe"
    source_terminal.parent.mkdir(parents=True)
    source_terminal.write_bytes(b"stub")
    handlers = real_handlers.build_real_handlers(
        Api(),
        instances_root=tmp_path / "instances",
        secrets_root=tmp_path / "secrets",
        source_terminal=source_terminal,
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
            "broker_hint": "Generic Markets",
            "credential_envelope": {"opaque": True},
        },
    }

    result = handlers["provision"](job)

    assert result == {"live_sync_started": True}
    assert len(captured) == 1
    assert captured[0].method is ResolutionMethod.TERMINAL_DISCOVERY
    assert captured[0].expected_server == "Generic-Live"
    assert captured[0].discovery_queries == (
        "Generic-Live",
        "Generic Markets",
    )


def test_native_helper_discovers_before_reading_stored_password(
    tmp_path: Path,
) -> None:
    events: list[str] = []

    class Api:
        @staticmethod
        def heartbeat(_job_id: str, _lease_id: str) -> dict[str, bool]:
            return {"lease_valid": True}

    class Store:
        @staticmethod
        def read(_cid: str, name: str) -> str:
            events.append(f"read:{name}")
            return "opaque-password"

    class Runtime:
        @staticmethod
        def prepare_broker(**kwargs: object) -> None:
            events.append("prepare")
            assert kwargs == {
                "expected_server": "Generic-Live",
                "queries": ("Generic-Live",),
            }

        @staticmethod
        def start(**_kwargs: object) -> None:
            events.append("start")
            raise real_handlers.NativeMt5Error("intentional_stop")

    root = tmp_path / "instance"
    root.mkdir()
    plan = ZeroLicenseBrokerResolver().resolve("Generic-Live")
    job = {"job_id": "job", "lease_id": "lease"}

    with pytest.raises(Exception) as captured:
        real_handlers._start_file_bridge_and_sync(
            job,
            Api(),
            root,
            str(uuid4()),
            42,
            plan,
            "new_only",
            None,
            Store(),  # type: ignore[arg-type]
            lambda _path: object(),
            tmp_path / "bridge.ex5",
            lambda _root, _cid: Runtime(),  # type: ignore[arg-type]
        )

    assert captured.value.error_code == "mt5_initialize_failed"
    assert events == ["prepare", "read:mt5_investor_password", "start"]


def test_native_helper_stops_prepared_terminal_when_lease_is_lost(
    tmp_path: Path,
) -> None:
    events: list[str] = []

    class Api:
        calls = 0

        @classmethod
        def heartbeat(cls, _job_id: str, _lease_id: str) -> dict[str, bool]:
            cls.calls += 1
            return {"lease_valid": cls.calls == 1}

    class Store:
        @staticmethod
        def read(_cid: str, _name: str) -> str:
            events.append("unexpected-read")
            return "opaque-password"

    class Runtime:
        @staticmethod
        def prepare_broker(**_kwargs: object) -> None:
            events.append("prepare")

        @staticmethod
        def stop() -> bool:
            events.append("stop")
            return True

    root = tmp_path / "instance"
    root.mkdir()
    plan = ZeroLicenseBrokerResolver().resolve("Generic-Live")

    with pytest.raises(real_handlers.LeaseLost):
        real_handlers._start_file_bridge_and_sync(
            {"job_id": "job", "lease_id": "lease"},
            Api(),
            root,
            str(uuid4()),
            42,
            plan,
            "new_only",
            None,
            Store(),  # type: ignore[arg-type]
            lambda _path: object(),
            tmp_path / "bridge.ex5",
            lambda _root, _cid: Runtime(),  # type: ignore[arg-type]
        )

    assert events == ["prepare", "stop"]
