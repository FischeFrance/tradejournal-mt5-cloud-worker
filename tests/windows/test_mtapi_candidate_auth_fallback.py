from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from windows_agent import real_handlers
from windows_agent.agent_errors import Mt5AuthorizationFailed
from windows_agent.agent_secrets import AGENT_SCOPE_ID, PROVISIONING_KEY_SECRET_NAME
from windows_agent.mtapi_search import MtApiEndpointCandidate, MtApiSearchResult
from windows_agent.provisioning.secret_store import WindowsSecretStore
from windows_agent.worker.native_mt5_runtime import NativeMt5Error


ENCRYPTION_KEY = base64.b64encode(b"0" * 32).decode("ascii")
ENDPOINTS = ("77.76.9.186:443", "82.118.228.39:443")


@pytest.fixture(autouse=True)
def fake_windows_secret_primitives(monkeypatch) -> None:
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


class FakeApi:
    def heartbeat(self, _job_id: str, _lease_id: str) -> dict:
        return {"lease_valid": True}

    def transition(
        self,
        _job_id: str,
        _lease_id: str,
        _status: str,
        _result: dict | None = None,
    ) -> dict:
        return {}

    def progress(
        self,
        _job_id: str,
        _lease_id: str,
        _event_code: str,
        _event_status: str,
        _detail_code: str | None,
    ) -> dict:
        return {"event_recorded": True}


class FakeProcessManager:
    def __init__(self, _state_path: Path) -> None:
        pass

    def stop(self) -> bool:
        return True

    def cleanup_path(self, _terminal: Path) -> bool:
        return True


def _envelope(password: str = "investor-pw") -> dict:
    key = base64.b64decode(ENCRYPTION_KEY)
    iv = os.urandom(12)
    ciphertext = AESGCM(key).encrypt(
        iv,
        json.dumps({"investor_password": password}).encode("utf-8"),
        None,
    )
    return {
        "alg": "aes-256-gcm-v1",
        "iv": base64.b64encode(iv).decode("ascii"),
        "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
    }


def _environment(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    instances_root = tmp_path / "instances"
    secrets_root = tmp_path / "secrets"
    terminal = tmp_path / "golden" / "terminal64.exe"
    terminal.parent.mkdir(parents=True)
    terminal.write_bytes(b"terminal")
    expert = terminal.parent / "TradeJournalBridge.ex5"
    expert.write_bytes(b"expert")
    WindowsSecretStore(secrets_root).write(
        AGENT_SCOPE_ID,
        PROVISIONING_KEY_SECRET_NAME,
        ENCRYPTION_KEY,
    )
    return instances_root, secrets_root, terminal, expert


def _search_result() -> MtApiSearchResult:
    return MtApiSearchResult(
        outcome="EXACT_MATCH",
        candidates=(
            MtApiEndpointCandidate(
                "Goat Funded Ltd.",
                "GoatFunded-Server",
                "77.76.9.186",
                443,
            ),
            MtApiEndpointCandidate(
                "Goat Funded Ltd.",
                "GoatFunded-Server",
                "82.118.228.39",
                443,
            ),
            MtApiEndpointCandidate(
                "Goat Funded Ltd.",
                "GoatFunded-Server",
                "203.0.113.10",
                443,
            ),
        ),
        company_names=("Goat Funded Ltd.",),
        fetched_at_unix_ms=1,
    )


def _job(connection_id: str) -> dict:
    return {
        "job_id": "job-provision",
        "job_type": "provision",
        "connection_id": connection_id,
        "lease_id": "lease-1",
        "history_mode": "new_only",
        "from_date": None,
        "payload": {
            "credential_envelope": _envelope(),
            "expected_login": 314638447,
            "expected_server": "GoatFunded-Server",
        },
    }


def _handlers(tmp_path: Path):
    instances, secrets, terminal, expert = _environment(tmp_path)
    return real_handlers.build_real_handlers(
        FakeApi(),
        instances_root=instances,
        secrets_root=secrets,
        source_terminal=terminal,
        process_factory=FakeProcessManager,
        expert_binary=expert,
        mtapi_search=lambda _server: _search_result(),
    )


def test_tries_second_exact_server_access_point_after_auth_rejection(
    tmp_path: Path,
) -> None:
    attempted: list[str] = []

    def start_candidate(*args, **_kwargs):
        attempted.append(args[6])
        if len(attempted) == 1:
            raise Mt5AuthorizationFailed("authorization_failed")
        return {
            "_verification_pid": 123,
            "effective_server_name": "GoatFunded-Server",
            "live_sync_started": True,
        }

    handlers = _handlers(tmp_path)
    with patch.object(
        real_handlers,
        "_start_file_bridge_and_sync",
        side_effect=start_candidate,
    ):
        result = handlers["provision"](_job(str(uuid4())))

    assert attempted == list(ENDPOINTS)
    assert result["live_sync_started"] is True


def test_stops_after_two_access_point_auth_rejections(tmp_path: Path) -> None:
    attempted: list[str] = []

    def reject_candidate(*args, **_kwargs):
        attempted.append(args[6])
        raise Mt5AuthorizationFailed("authorization_failed")

    handlers = _handlers(tmp_path)
    with (
        patch.object(
            real_handlers,
            "_start_file_bridge_and_sync",
            side_effect=reject_candidate,
        ),
        pytest.raises(Mt5AuthorizationFailed),
    ):
        handlers["provision"](_job(str(uuid4())))

    assert attempted == list(ENDPOINTS)


def test_auth_rejection_discards_incomplete_instance_for_corrected_retry(
    tmp_path: Path,
) -> None:
    connection_id = str(uuid4())

    def reject_candidate(*_args, **_kwargs):
        raise Mt5AuthorizationFailed("authorization_failed")

    handlers = _handlers(tmp_path)
    with (
        patch.object(
            real_handlers,
            "_start_file_bridge_and_sync",
            side_effect=reject_candidate,
        ),
        pytest.raises(Mt5AuthorizationFailed),
    ):
        handlers["provision"](_job(connection_id))

    assert not (tmp_path / "instances" / connection_id).exists()
    assert not (tmp_path / "secrets" / connection_id).exists()


def test_native_authorization_error_has_precise_public_taxonomy(
    tmp_path: Path,
) -> None:
    instances, secrets, terminal, expert = _environment(tmp_path)
    connection_id = str(uuid4())
    root = instances / connection_id
    (root / "terminal").mkdir(parents=True)
    (root / "state").mkdir()
    (root / "terminal" / "terminal64.exe").write_bytes(terminal.read_bytes())
    store = WindowsSecretStore(secrets)
    store.write(connection_id, "mt5_investor_password", "investor-pw")

    class RejectingRuntime:
        def set_cancel_check(self, _check) -> None:
            pass

        def start(self, **_kwargs):
            raise NativeMt5Error("authorization_failed")

    with pytest.raises(Mt5AuthorizationFailed):
        real_handlers._start_file_bridge_and_sync(
            _job(connection_id),
            FakeApi(),
            root,
            connection_id,
            314638447,
            "GoatFunded-Server",
            ENDPOINTS[0],
            "new_only",
            None,
            store,
            FakeProcessManager,
            expert,
            lambda _root, _cid: RejectingRuntime(),
            "",
        )
