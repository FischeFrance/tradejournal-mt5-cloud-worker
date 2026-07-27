from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from windows_agent.broker_endpoint_resolver import (
    BrokerEndpointResolutionError,
    resolve_verified_broker_endpoint,
)


RUN_ID = "12345678-1234-4234-8234-123456789abc"


def _fixture(tmp_path: Path, *, status: str = "VERIFIED") -> tuple[Path, Path, Path]:
    artifact = tmp_path / "events.jsonl"
    artifact.write_text('{"sanitized":true}\n', encoding="utf-8")
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    manifest = tmp_path / "artifact-manifest.json"
    manifest.write_text(
        json.dumps(
            {"files": [{"relative_path": artifact.name, "sha256": digest}]},
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    registry = tmp_path / "endpoint-registry.json"
    registry.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "updated_at_unix_ms": 1_000_000,
                "ttl_seconds": 100,
                "brokers": {
                    "FPMTrading": [
                        {
                            "host": "188.42.136.4",
                            "port": 443,
                            "protocol": "TCP/TLS",
                            "status": status,
                            "observed_at_unix_ms": 1_000_000,
                            "discovery_method": "MT5_LOGIN_DIALOG_IP",
                            "verification_pid": 4800,
                            "verification_session_id": RUN_ID,
                            "confidence": "MEDIUM",
                            "artifact_relative_path": artifact.name,
                            "artifact_sha256": digest,
                        }
                    ]
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return registry, manifest, artifact


def _resolve(tmp_path: Path, registry: Path, manifest: Path):
    return resolve_verified_broker_endpoint(
        registry,
        broker_label="FPM Trading",
        artifact_root=tmp_path,
        artifact_manifest=manifest,
        now_unix_ms=1_000_001,
    )


def test_resolves_one_verified_endpoint_with_independent_provenance(tmp_path: Path) -> None:
    registry, manifest, _ = _fixture(tmp_path)
    endpoint = _resolve(tmp_path, registry, manifest)
    assert endpoint.broker_label == "FPMTrading"
    assert endpoint.server_address == "188.42.136.4:443"
    assert endpoint.confidence == "MEDIUM"


@pytest.mark.parametrize("status", ["CANDIDATE", "METAQUOTES_CDN", "EXPIRED"])
def test_non_verified_status_never_resolves(tmp_path: Path, status: str) -> None:
    registry, manifest, _ = _fixture(tmp_path, status=status)
    with pytest.raises(BrokerEndpointResolutionError, match="missing or ambiguous"):
        _resolve(tmp_path, registry, manifest)


def test_expired_or_multiple_verified_endpoint_is_rejected(tmp_path: Path) -> None:
    registry, manifest, _ = _fixture(tmp_path)
    with pytest.raises(BrokerEndpointResolutionError, match="missing or ambiguous"):
        resolve_verified_broker_endpoint(
            registry,
            broker_label="FPMTrading",
            artifact_root=tmp_path,
            artifact_manifest=manifest,
            now_unix_ms=1_100_001,
        )
    payload = json.loads(registry.read_text(encoding="utf-8"))
    payload["brokers"]["FPMTrading"].append(
        {**payload["brokers"]["FPMTrading"][0], "host": "203.0.113.10"}
    )
    registry.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(BrokerEndpointResolutionError, match="missing or ambiguous"):
        _resolve(tmp_path, registry, manifest)


def test_tampered_or_missing_artifact_is_rejected(tmp_path: Path) -> None:
    registry, manifest, artifact = _fixture(tmp_path)
    artifact.write_text("tampered\n", encoding="utf-8")
    with pytest.raises(BrokerEndpointResolutionError, match="digest"):
        _resolve(tmp_path, registry, manifest)
    artifact.unlink()
    with pytest.raises(BrokerEndpointResolutionError, match="digest"):
        _resolve(tmp_path, registry, manifest)


def test_corrupt_registry_and_secret_fields_are_rejected(tmp_path: Path) -> None:
    registry, manifest, _ = _fixture(tmp_path)
    registry.write_text("{", encoding="utf-8")
    with pytest.raises(BrokerEndpointResolutionError, match="cannot be read"):
        _resolve(tmp_path, registry, manifest)
    registry, manifest, _ = _fixture(tmp_path)
    payload = json.loads(registry.read_text(encoding="utf-8"))
    payload["password"] = "forbidden"
    registry.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(BrokerEndpointResolutionError, match="secret-bearing"):
        _resolve(tmp_path, registry, manifest)


def test_malformed_unrelated_broker_record_is_rejected(tmp_path: Path) -> None:
    registry, manifest, _ = _fixture(tmp_path)
    payload = json.loads(registry.read_text(encoding="utf-8"))
    payload["brokers"]["OtherBroker"] = [{"status": "CANDIDATE"}]
    registry.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(BrokerEndpointResolutionError, match="record fields"):
        _resolve(tmp_path, registry, manifest)


def test_unsafe_artifact_path_and_symlink_are_rejected(tmp_path: Path) -> None:
    registry, manifest, artifact = _fixture(tmp_path)
    payload = json.loads(registry.read_text(encoding="utf-8"))
    payload["brokers"]["FPMTrading"][0]["artifact_relative_path"] = "../events.jsonl"
    registry.write_text(json.dumps(payload), encoding="utf-8")
    manifest_payload = json.loads(manifest.read_text(encoding="utf-8"))
    manifest_payload["files"][0]["relative_path"] = "../events.jsonl"
    manifest.write_text(json.dumps(manifest_payload), encoding="utf-8")
    with pytest.raises(BrokerEndpointResolutionError, match="unsafe"):
        _resolve(tmp_path, registry, manifest)

    registry, manifest, artifact = _fixture(tmp_path)
    link = tmp_path / "linked-events.jsonl"
    try:
        link.symlink_to(artifact)
    except OSError:
        pytest.skip("symlink creation is not available")
    payload = json.loads(registry.read_text(encoding="utf-8"))
    payload["brokers"]["FPMTrading"][0]["artifact_relative_path"] = link.name
    payload["brokers"]["FPMTrading"][0]["artifact_sha256"] = hashlib.sha256(
        artifact.read_bytes()
    ).hexdigest()
    registry.write_text(json.dumps(payload), encoding="utf-8")
    manifest.write_text(
        json.dumps(
            {
                "files": [
                    {
                        "relative_path": link.name,
                        "sha256": payload["brokers"]["FPMTrading"][0][
                            "artifact_sha256"
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(BrokerEndpointResolutionError, match="reparse"):
        _resolve(tmp_path, registry, manifest)


def test_symlink_artifact_root_is_rejected(tmp_path: Path) -> None:
    actual = tmp_path / "actual"
    actual.mkdir()
    registry, manifest, _ = _fixture(actual)
    linked_root = tmp_path / "linked-root"
    try:
        linked_root.symlink_to(actual, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is not available")
    with pytest.raises(BrokerEndpointResolutionError, match="root is unsafe"):
        resolve_verified_broker_endpoint(
            registry,
            broker_label="FPM Trading",
            artifact_root=linked_root,
            artifact_manifest=manifest,
            now_unix_ms=1_000_001,
        )
