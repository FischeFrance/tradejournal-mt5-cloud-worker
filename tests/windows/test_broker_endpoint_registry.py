from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from windows_agent.broker_endpoint_registry import (
    BrokerEndpointObservationError,
    BrokerEndpointPublicationError,
    BrokerEndpointRegistryPublisher,
    EndpointInvalidation,
    EndpointPromotion,
    ObservedProcessEndpoint,
    observe_process_endpoint,
)
from windows_agent.broker_endpoint_resolver import (
    BrokerEndpointResolutionError,
    resolve_verified_broker_endpoint,
)
from windows_agent.broker_wizard import (
    BrokerWizardEvidence,
    write_login_verification_artifact,
)


RUN_ID = "12345678-1234-4234-8234-123456789abc"
RUN_ID_2 = "12345678-1234-4234-8234-123456789abd"


def _promotion(
    tmp_path: Path,
    *,
    run_id: str = RUN_ID,
    server_name: str = "PepperstoneUK-Live",
    host: str = "203.0.113.10",
    port: int = 443,
    pid: int = 4800,
    process_created: int = 999_000,
    observed: int = 1_000_000,
) -> EndpointPromotion:
    wizard_artifact = tmp_path / f"wizard-{run_id}.json"
    wizard_artifact.write_text('{"status":"SUCCESS"}\n', encoding="utf-8")
    evidence = BrokerWizardEvidence(
        run_id=run_id,
        expected_server_name=server_name,
        selected_broker_label="Pepperstone",
        censused_server_names=(server_name,),
        terminal_pid=4700,
        completed_at_unix_ms=observed - 1,
        artifact_path=wizard_artifact,
        artifact_sha256=hashlib.sha256(
            wizard_artifact.read_bytes()
        ).hexdigest(),
    )
    observation = ObservedProcessEndpoint(
        host=host,
        port=port,
        pid=pid,
        process_creation_time_unix_ms=process_created,
        observed_at_unix_ms=observed,
    )
    verification, digest = write_login_verification_artifact(
        tmp_path / "state",
        evidence=evidence,
        verification_pid=pid,
        verified_at_unix_ms=observed,
        process_creation_time_unix_ms=process_created,
        remote_host=host,
        remote_port=port,
    )
    return EndpointPromotion(
        broker_label="Pepperstone",
        server_name=server_name,
        verification_session_id=run_id,
        verification_artifact=verification,
        verification_artifact_sha256=digest,
        provenance_artifact=wizard_artifact,
        provenance_artifact_sha256=evidence.artifact_sha256,
        observation=observation,
    )


def _publisher(tmp_path: Path) -> BrokerEndpointRegistryPublisher:
    return BrokerEndpointRegistryPublisher(
        tmp_path / "registry" / "endpoint-registry.json",
        artifact_root=tmp_path / "registry",
        artifact_manifest=tmp_path / "registry" / "artifact-manifest.json",
    )


def test_observer_requires_one_established_endpoint() -> None:
    class Process:
        def create_time(self):
            return 999.0

        def net_connections(self, *, kind):
            assert kind == "tcp"
            return [
                SimpleNamespace(
                    status="ESTABLISHED",
                    raddr=("203.0.113.10", 443),
                ),
                SimpleNamespace(
                    status="LISTEN",
                    raddr=("198.51.100.2", 443),
                ),
            ]

    endpoint = observe_process_endpoint(
        4800,
        process_factory=lambda pid: Process(),
        now_unix_ms=1_000_000,
        sleep=lambda _seconds: None,
    )
    assert endpoint.server_address == "203.0.113.10:443"
    assert endpoint.pid == 4800
    assert endpoint.process_creation_time_unix_ms == 999_000


def test_observer_rejects_ambiguous_or_missing_endpoint() -> None:
    class Process:
        def create_time(self):
            return 999.0

        def net_connections(self, *, kind):
            return [
                SimpleNamespace(
                    status="ESTABLISHED",
                    raddr=("203.0.113.10", 443),
                ),
                SimpleNamespace(
                    status="ESTABLISHED",
                    raddr=("198.51.100.2", 443),
                ),
            ]

    with pytest.raises(
        BrokerEndpointObservationError,
        match="missing or ambiguous",
    ):
        observe_process_endpoint(
            4800,
            process_factory=lambda pid: Process(),
            sleep=lambda _seconds: None,
        )


def test_observer_discards_transient_endpoint_without_ranking() -> None:
    class Process:
        calls = 0

        def create_time(self):
            return 999.0

        def net_connections(self, *, kind):
            self.calls += 1
            stable = SimpleNamespace(
                status="ESTABLISHED",
                raddr=("203.0.113.10", 443),
            )
            transient = SimpleNamespace(
                status="ESTABLISHED",
                raddr=("198.51.100.2", 443),
            )
            return [stable, transient] if self.calls == 1 else [stable]

    endpoint = observe_process_endpoint(
        4800,
        process_factory=lambda pid: Process(),
        now_unix_ms=1_000_000,
        sleep=lambda _seconds: None,
    )
    assert endpoint.server_address == "203.0.113.10:443"


def test_publisher_creates_v3_registry_and_independent_resolution(
    tmp_path: Path,
) -> None:
    publisher = _publisher(tmp_path)
    published = publisher.publish(_promotion(tmp_path))

    assert published.server_name == "PepperstoneUK-Live"
    assert published.server_address == "203.0.113.10:443"
    registry_path = tmp_path / "registry" / "endpoint-registry.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    assert registry["schema_version"] == 3
    assert "ttl_seconds" not in registry
    assert "password" not in registry_path.read_text(encoding="utf-8").lower()

    resolved = resolve_verified_broker_endpoint(
        registry_path,
        broker_label="Pepperstone",
        server_name="PepperstoneUK-Live",
        artifact_root=tmp_path / "registry",
        artifact_manifest=(
            tmp_path / "registry" / "artifact-manifest.json"
        ),
    )
    assert resolved.server_address == "203.0.113.10:443"


def test_new_endpoint_supersedes_previous_only_for_same_server(
    tmp_path: Path,
) -> None:
    publisher = _publisher(tmp_path)
    publisher.publish(_promotion(tmp_path))
    publisher.publish(
        _promotion(
            tmp_path,
            run_id=RUN_ID_2,
            host="198.51.100.2",
            observed=1_000_010,
        )
    )
    registry = json.loads(
        (
            tmp_path / "registry" / "endpoint-registry.json"
        ).read_text(encoding="utf-8")
    )
    records = registry["brokers"]["Pepperstone"]
    assert [record["status"] for record in records] == [
        "SUPERSEDED",
        "VERIFIED",
    ]
    assert (
        records[0]["invalidation_reason"]
        == "SUPERSEDED_BY_NEW_VERIFICATION"
    )
    resolved = resolve_verified_broker_endpoint(
        tmp_path / "registry" / "endpoint-registry.json",
        broker_label="Pepperstone",
        server_name="PepperstoneUK-Live",
        artifact_root=tmp_path / "registry",
        artifact_manifest=(
            tmp_path / "registry" / "artifact-manifest.json"
        ),
    )
    assert resolved.server_address == "198.51.100.2:443"


def test_different_server_remains_independently_resolvable(
    tmp_path: Path,
) -> None:
    publisher = _publisher(tmp_path)
    publisher.publish(_promotion(tmp_path))
    publisher.publish(
        _promotion(
            tmp_path,
            run_id=RUN_ID_2,
            server_name="Pepperstone-Demo",
            host="198.51.100.2",
            observed=1_000_010,
        )
    )
    for server_name, address in (
        ("PepperstoneUK-Live", "203.0.113.10:443"),
        ("Pepperstone-Demo", "198.51.100.2:443"),
    ):
        resolved = resolve_verified_broker_endpoint(
            tmp_path / "registry" / "endpoint-registry.json",
            broker_label="Pepperstone",
            server_name=server_name,
            artifact_root=tmp_path / "registry",
            artifact_manifest=(
                tmp_path / "registry" / "artifact-manifest.json"
            ),
        )
        assert resolved.server_address == address


def test_endpoint_specific_failure_invalidates_only_exact_record(
    tmp_path: Path,
) -> None:
    publisher = _publisher(tmp_path)
    live = publisher.publish(_promotion(tmp_path))
    publisher.publish(
        _promotion(
            tmp_path,
            run_id=RUN_ID_2,
            server_name="Pepperstone-Demo",
            host="198.51.100.2",
            observed=1_000_010,
        )
    )

    publisher.invalidate(
        EndpointInvalidation(
            endpoint=live,
            reason="ENDPOINT_CONNECTION_FAILED",
            invalidated_at_unix_ms=1_000_020,
            event_id="12345678-1234-4234-8234-123456789abe",
        )
    )

    registry_path = tmp_path / "registry" / "endpoint-registry.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    live_record = registry["brokers"]["Pepperstone"][0]
    assert live_record["status"] == "INVALID"
    assert (
        live_record["invalidation_reason"]
        == "ENDPOINT_CONNECTION_FAILED"
    )
    assert live_record["invalidated_at_unix_ms"] == 1_000_020
    with pytest.raises(
        BrokerEndpointResolutionError,
        match="missing or ambiguous",
    ):
        resolve_verified_broker_endpoint(
            registry_path,
            broker_label="Pepperstone",
            server_name="PepperstoneUK-Live",
            artifact_root=tmp_path / "registry",
            artifact_manifest=(
                tmp_path / "registry" / "artifact-manifest.json"
            ),
        )
    demo = resolve_verified_broker_endpoint(
        registry_path,
        broker_label="Pepperstone",
        server_name="Pepperstone-Demo",
        artifact_root=tmp_path / "registry",
        artifact_manifest=(
            tmp_path / "registry" / "artifact-manifest.json"
        ),
    )
    assert demo.server_address == "198.51.100.2:443"


@pytest.mark.parametrize(
    "reason",
    [
        "AUTHORIZATION_FAILED",
        "INVALID_ACCOUNT",
        "AUTHENTICATION_FAILURE",
        "ENVIRONMENT_FAILURE",
    ],
)
def test_non_endpoint_failure_reason_cannot_invalidate(
    tmp_path: Path,
    reason: str,
) -> None:
    publisher = _publisher(tmp_path)
    endpoint = publisher.publish(_promotion(tmp_path))
    with pytest.raises(
        BrokerEndpointPublicationError,
        match="not endpoint-specific",
    ):
        publisher.invalidate(
            EndpointInvalidation(
                endpoint=endpoint,
                reason=reason,
                invalidated_at_unix_ms=1_000_020,
                event_id=RUN_ID_2,
            )
        )
    resolved = resolve_verified_broker_endpoint(
        tmp_path / "registry" / "endpoint-registry.json",
        broker_label="Pepperstone",
        server_name="PepperstoneUK-Live",
        artifact_root=tmp_path / "registry",
        artifact_manifest=(
            tmp_path / "registry" / "artifact-manifest.json"
        ),
    )
    assert resolved.server_address == "203.0.113.10:443"


def test_tampered_binding_never_publishes_registry(tmp_path: Path) -> None:
    promotion = _promotion(tmp_path)
    evidence = json.loads(
        promotion.verification_artifact.read_text(encoding="utf-8")
    )
    evidence["remote_port"] = 444
    promotion.verification_artifact.write_text(
        json.dumps(evidence),
        encoding="utf-8",
    )
    promotion = EndpointPromotion(
        **{
            **promotion.__dict__,
            "verification_artifact_sha256": hashlib.sha256(
                promotion.verification_artifact.read_bytes()
            ).hexdigest(),
        }
    )
    with pytest.raises(
        BrokerEndpointPublicationError,
        match="binding",
    ):
        _publisher(tmp_path).publish(promotion)
    assert not (
        tmp_path / "registry" / "endpoint-registry.json"
    ).exists()
    assert not (
        tmp_path / "registry" / "artifact-manifest.json"
    ).exists()


def test_publication_failure_rolls_back_manifest_and_artifact(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from windows_agent import broker_endpoint_registry as registry_module

    publisher = _publisher(tmp_path)
    promotion = _promotion(tmp_path)
    real_atomic_json = registry_module.atomic_json

    def fail_registry_write(path, payload):
        if Path(path).name == "endpoint-registry.json":
            raise OSError("fixture write failure")
        real_atomic_json(path, payload)

    monkeypatch.setattr(
        registry_module,
        "atomic_json",
        fail_registry_write,
    )
    with pytest.raises(
        BrokerEndpointPublicationError,
        match="publication failed",
    ):
        publisher.publish(promotion)

    registry_root = tmp_path / "registry"
    assert not (registry_root / "endpoint-registry.json").exists()
    assert not (registry_root / "artifact-manifest.json").exists()
    assert not list(registry_root.glob("artifacts/**/*.json"))


def test_v1_records_are_preserved_but_invalidated_during_migration(
    tmp_path: Path,
) -> None:
    registry_root = tmp_path / "registry"
    registry_root.mkdir()
    legacy_artifact = registry_root / "legacy.json"
    legacy_artifact.write_text('{"sanitized":true}\n', encoding="utf-8")
    legacy_digest = hashlib.sha256(legacy_artifact.read_bytes()).hexdigest()
    (registry_root / "endpoint-registry.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "updated_at_unix_ms": 900_000,
                "ttl_seconds": 100,
                "brokers": {
                    "Legacy Broker": [
                        {
                            "host": "192.0.2.10",
                            "port": 443,
                            "protocol": "TCP/TLS",
                            "status": "VERIFIED",
                            "observed_at_unix_ms": 900_000,
                            "discovery_method": "MT5_LOGIN_DIALOG_IP",
                            "verification_pid": 4000,
                            "verification_session_id": RUN_ID,
                            "confidence": "MEDIUM",
                            "artifact_relative_path": "legacy.json",
                            "artifact_sha256": legacy_digest,
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    (registry_root / "artifact-manifest.json").write_text(
        json.dumps(
            {
                "files": [
                    {
                        "relative_path": "legacy.json",
                        "sha256": legacy_digest,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    _publisher(tmp_path).publish(_promotion(tmp_path))
    registry = json.loads(
        (registry_root / "endpoint-registry.json").read_text(
            encoding="utf-8"
        )
    )
    assert registry["schema_version"] == 3
    assert registry["brokers"]["Legacy Broker"][0]["status"] == "INVALID"
    assert (
        registry["brokers"]["Legacy Broker"][0]["invalidation_reason"]
        == "LEGACY_PROVENANCE_INSUFFICIENT"
    )
    assert (
        registry["brokers"]["Legacy Broker"][0]["server_name"] is None
    )
    with pytest.raises(
        BrokerEndpointResolutionError,
        match="missing or ambiguous",
    ):
        resolve_verified_broker_endpoint(
            registry_root / "endpoint-registry.json",
            broker_label="Legacy Broker",
            server_name="Legacy-Live",
            artifact_root=registry_root,
            artifact_manifest=registry_root / "artifact-manifest.json",
        )


def test_v2_verified_record_migrates_without_time_expiry(
    tmp_path: Path,
) -> None:
    publisher = _publisher(tmp_path)
    published = publisher.publish(_promotion(tmp_path))
    registry_path = tmp_path / "registry" / "endpoint-registry.json"
    v3 = json.loads(registry_path.read_text(encoding="utf-8"))
    record = dict(v3["brokers"]["Pepperstone"][0])
    record.pop("invalidated_at_unix_ms")
    record.pop("invalidation_reason")
    record.pop("invalidation_event_id")
    registry_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "updated_at_unix_ms": 1,
                "ttl_seconds": 1,
                "brokers": {"Pepperstone": [record]},
            }
        ),
        encoding="utf-8",
    )

    publisher.invalidate(
        EndpointInvalidation(
            endpoint=published,
            reason="SERVER_IDENTITY_MISMATCH",
            invalidated_at_unix_ms=9_999_999_999_999,
            event_id=RUN_ID_2,
        )
    )
    migrated = json.loads(registry_path.read_text(encoding="utf-8"))
    assert migrated["schema_version"] == 3
    assert "ttl_seconds" not in migrated
    assert migrated["brokers"]["Pepperstone"][0]["status"] == "INVALID"
