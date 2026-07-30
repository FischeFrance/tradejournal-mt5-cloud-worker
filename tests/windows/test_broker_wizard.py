from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest

from windows_agent.broker_wizard import (
    BrokerWizardError,
    BrokerWizardEvidence,
    _default_python_executable,
    load_wizard_evidence,
    write_login_verification_artifact,
)


def _wizard_document(run_id: str) -> dict:
    return {
        "schema_version": 2,
        "run_id": run_id,
        "status": "SUCCESS",
        "failure_reason": None,
        "expected_server_name": "GoatFunded-Server3",
        "selected_broker_label": "Goat Funded Trader",
        "selected_broker_labels": ["Goat Funded Trader", "GoatFunded"],
        "censused_server_names": [
            "GoatFunded-Server",
            "GoatFunded-Server2",
            "GoatFunded-Server3",
        ],
        "terminal_pid": 4321,
        "completed_at_unix_ms": 1_785_190_000_000,
    }


def test_load_wizard_evidence_accepts_exact_server_and_verifies_digest(tmp_path):
    run_id = str(uuid4())
    path = tmp_path / "wizard-result.json"
    path.write_text(
        json.dumps(_wizard_document(run_id), sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )

    evidence = load_wizard_evidence(
        path,
        expected_run_id=run_id,
        expected_server_name="GoatFunded-Server3",
    )

    assert evidence.selected_broker_label == "Goat Funded Trader"
    assert evidence.selected_broker_labels == ("Goat Funded Trader", "GoatFunded")
    assert evidence.terminal_pid == 4321
    assert evidence.artifact_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda value: value.update(
                status="FAILED",
                failure_reason="server_not_found",
            ),
            "wizard did not succeed",
        ),
        (
            lambda value: value.update(
                censused_server_names=["GoatFunded-Server2"]
            ),
            "expected server",
        ),
        (
            lambda value: value.update(selected_broker_label="bad\nbroker"),
            "broker label",
        ),
        (
            lambda value: value.update(password="must-not-be-accepted"),
            "fields",
        ),
    ],
)
def test_load_wizard_evidence_rejects_invalid_or_secret_bearing_output(
    tmp_path, mutation, message
):
    run_id = str(uuid4())
    document = _wizard_document(run_id)
    mutation(document)
    path = tmp_path / "wizard-result.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(BrokerWizardError, match=message):
        load_wizard_evidence(
            path,
            expected_run_id=run_id,
            expected_server_name="GoatFunded-Server3",
        )


def test_failed_wizard_evidence_preserves_only_sanitized_reason(tmp_path):
    run_id = str(uuid4())
    document = _wizard_document(run_id)
    document.update(
        status="FAILED",
        failure_reason="server_not_found",
        selected_broker_label=None,
        censused_server_names=[],
    )
    path = tmp_path / "wizard-result.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(BrokerWizardError) as exc_info:
        load_wizard_evidence(
            path,
            expected_run_id=run_id,
            expected_server_name="GoatFunded-Server3",
        )

    assert str(exc_info.value) == "wizard did not succeed"
    assert exc_info.value.failure_reason == "server_not_found"
    assert exc_info.value.detail_code == "wizard_server_not_found"


def test_write_login_verification_artifact_is_atomic_and_contains_no_secret(tmp_path):
    wizard_path = tmp_path / "wizard-result.json"
    wizard_path.write_text("{}", encoding="utf-8")
    evidence = BrokerWizardEvidence(
        run_id=str(uuid4()),
        expected_server_name="GoatFunded-Server3",
        selected_broker_label="Goat Funded Trader",
        censused_server_names=("GoatFunded-Server3",),
        terminal_pid=4321,
        completed_at_unix_ms=1_785_190_000_000,
        artifact_path=wizard_path,
        artifact_sha256=hashlib.sha256(wizard_path.read_bytes()).hexdigest(),
    )

    artifact_path, digest = write_login_verification_artifact(
        tmp_path,
        evidence=evidence,
        verification_pid=9876,
        verified_at_unix_ms=1_785_190_100_000,
    )

    payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert payload["login_verified"] is True
    assert payload["investor_read_only_verified"] is True
    assert payload["verification_pid"] == 9876
    assert payload["provenance_kind"] == "BROKER_WIZARD"
    assert payload["provenance_artifact_sha256"] == evidence.artifact_sha256
    assert digest == hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    serialized = artifact_path.read_text(encoding="utf-8").lower()
    assert "password" not in serialized
    assert "credential" not in serialized
    assert not list(tmp_path.glob("*.tmp"))


def test_default_python_executable_supports_pywin32_service_host(
    tmp_path, monkeypatch
):
    venv = tmp_path / ".venv"
    interpreter = venv / "Scripts" / "python.exe"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_bytes(b"test interpreter")
    monkeypatch.setattr(
        "windows_agent.broker_wizard.sys.executable",
        str(venv / "pythonservice.exe"),
    )
    monkeypatch.setattr(
        "windows_agent.broker_wizard.sys.prefix",
        str(tmp_path / "base-python"),
    )

    assert _default_python_executable() == interpreter
