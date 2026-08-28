from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

import windows_agent.mt5_adhoc_probe as probe
from windows_agent.provisioning.secret_store import WindowsSecretStore
from windows_agent.state_store import read_json


def test_run_probe_targets_exact_connection_without_a_time_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection_id = probe.FPM_TEST_CONNECTION_ID
    nonce = str(uuid4())
    calls: list[object] = []
    config = object()

    monkeypatch.setattr(
        probe,
        "_assert_local_system",
        lambda: calls.append("local_system"),
    )
    monkeypatch.setattr(
        probe,
        "_assert_service_stopped",
        lambda: calls.append("service_stopped"),
    )
    monkeypatch.setattr(
        probe,
        "_validate_release",
        lambda revision: calls.append(("release", revision)),
    )
    monkeypatch.setattr(probe, "_load_service_config", lambda: config)

    class Coordinator:
        def run_canary_only(self, cid, server, stop_event):
            calls.append((cid, server, stop_event.is_set()))
            return SimpleNamespace(
                connection_id=cid,
                server="FPMTrading-Live",
                update_captured=True,
                pending_update_receipt_ids=("a" * 64,),
            )

    monkeypatch.setattr(
        probe,
        "_build_coordinator",
        lambda observed: Coordinator()
        if observed is config
        else pytest.fail("unexpected config"),
    )

    result = probe.run_probe(
        revision="b" * 40,
        nonce=nonce,
        connection_id=connection_id,
        expected_server="FPMTrading-Live",
    )

    assert calls == [
        "local_system",
        "service_stopped",
        ("release", "b" * 40),
        (connection_id, "FPMTrading-Live", False),
    ]
    assert result == {
        "connection_id": connection_id,
        "server": "FPMTrading-Live",
        "update_captured": True,
        "pending_update_receipt_ids": ["a" * 64],
    }


@pytest.mark.parametrize(
    ("connection_id", "expected_server", "code"),
    [
        ("not-a-uuid", "FPMTrading-Live", "connection_id_invalid"),
        (
            probe.FPM_TEST_CONNECTION_ID,
            " FPMTrading-Live",
            "expected_server_invalid",
        ),
        (
            probe.FPM_TEST_CONNECTION_ID,
            "FPMTrading-Live\n",
            "expected_server_invalid",
        ),
        (str(uuid4()), probe.FPM_TEST_SERVER, "canary_scope_restricted"),
        (
            probe.FPM_TEST_CONNECTION_ID,
            "Other-Live",
            "canary_scope_restricted",
        ),
    ],
)
def test_run_probe_rejects_unbound_target_before_building_coordinator(
    monkeypatch: pytest.MonkeyPatch,
    connection_id: str,
    expected_server: str,
    code: str,
) -> None:
    monkeypatch.setattr(probe, "_assert_local_system", lambda: None)
    monkeypatch.setattr(probe, "_assert_service_stopped", lambda: None)
    monkeypatch.setattr(probe, "_validate_release", lambda _revision: None)
    monkeypatch.setattr(
        probe,
        "_build_coordinator",
        lambda _config: pytest.fail("coordinator must not be built"),
    )

    with pytest.raises(probe.Mt5AdHocProbeError, match=code):
        probe.run_probe(
            revision="b" * 40,
            nonce=str(uuid4()),
            connection_id=connection_id,
            expected_server=expected_server,
        )


def test_build_coordinator_explicitly_disables_pool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, object] = {}

    class Manager:
        def __init__(self, source, digest, *, lock):
            calls["manager"] = (source, digest, lock)

        def validate_current_quiesced(self):
            calls["validated"] = True

    pending = object()
    rotator = object()
    result = object()

    monkeypatch.setattr(probe, "Mt5TemplateManager", Manager)
    monkeypatch.setattr(
        probe,
        "verify_interactive_task_identity",
        lambda user: calls.setdefault("interactive_user", user),
    )
    def pending_store(root):
        calls["pending_root"] = root
        return pending

    def build_rotator(**kwargs):
        calls["rotator_kwargs"] = kwargs
        return rotator

    monkeypatch.setattr(probe, "Mt5PendingUpdateStore", pending_store)
    monkeypatch.setattr(probe, "Mt5InstanceRotator", build_rotator)

    def coordinator(**kwargs):
        calls["coordinator_kwargs"] = kwargs
        return result

    monkeypatch.setattr(probe, "Mt5MaintenanceCoordinator", coordinator)
    config = SimpleNamespace(
        source_terminal=tmp_path / "template" / "terminal64.exe",
        terminal_sha256="1" * 64,
        mt5_interactive_user="TradeJournalMT5",
        mt5_maintenance_state_path=tmp_path / "state" / "maintenance.json",
        instances_root=tmp_path / "instances",
        secrets_root=tmp_path / "secrets",
        expert_binary=tmp_path / "expert.ex5",
        expert_sha256="2" * 64,
    )

    assert probe._build_coordinator(config) is result
    kwargs = calls["coordinator_kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["instance_pool"] is None
    assert kwargs["pending_update_store"] is pending
    assert calls["validated"] is True
    assert calls["interactive_user"] == "TradeJournalMT5"


def test_main_publishes_sanitized_success_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nonce = str(uuid4())
    connection_id = str(uuid4())
    monkeypatch.setattr(probe, "RESULT_ROOT", tmp_path)
    monkeypatch.setattr(
        WindowsSecretStore,
        "restrict_shared_service_acl",
        staticmethod(lambda _path: None),
    )
    monkeypatch.setattr(
        probe,
        "run_probe",
        lambda **_kwargs: {
            "connection_id": connection_id,
            "server": "FPMTrading-Live",
            "update_captured": False,
            "pending_update_receipt_ids": [],
        },
    )

    exit_code = probe.main(
        [
            "--revision",
            "b" * 40,
            "--nonce",
            nonce,
            "--connection-id",
            connection_id,
            "--expected-server",
            "FPMTrading-Live",
        ]
    )

    assert exit_code == 0
    result = read_json(tmp_path / f"{nonce}.json")
    assert result["success"] is True
    assert result["code"] == "ok"
    assert result["mode"] == "canary_only"
    assert result["details"]["update_captured"] is False


def test_main_never_publishes_exception_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    nonce = str(uuid4())
    connection_id = str(uuid4())
    marker = "password=must-not-leak"
    monkeypatch.setattr(probe, "RESULT_ROOT", tmp_path)
    monkeypatch.setattr(
        WindowsSecretStore,
        "restrict_shared_service_acl",
        staticmethod(lambda _path: None),
    )

    def fail(**_kwargs):
        raise RuntimeError(marker)

    monkeypatch.setattr(probe, "run_probe", fail)

    exit_code = probe.main(
        [
            "--revision",
            "b" * 40,
            "--nonce",
            nonce,
            "--connection-id",
            connection_id,
            "--expected-server",
            "FPMTrading-Live",
        ]
    )

    assert exit_code == 1
    raw = (tmp_path / f"{nonce}.json").read_text(encoding="utf-8")
    assert marker not in raw
    assert marker not in caplog.text
    result = read_json(tmp_path / f"{nonce}.json")
    assert result["success"] is False
    assert result["code"] == "mt5_adhoc_probe_failed"
    assert result["details"] == {}


def test_ad_hoc_entrypoint_has_no_cascade_or_scheduler_path() -> None:
    source = Path(probe.__file__).read_text(encoding="utf-8")

    for forbidden in (
        "_maintenance_window",
        ".run_once(",
        "rotate_all(",
        "Mt5InstancePool",
        "commit_prepared_update",
        "accept_template_rotation",
    ):
        assert forbidden not in source
