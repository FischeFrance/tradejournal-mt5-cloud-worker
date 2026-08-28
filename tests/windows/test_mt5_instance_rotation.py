from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, RLock
from unittest.mock import patch
from uuid import uuid4

import pytest

import windows_agent.provisioning.mt5_instance_rotation as rotation_module
from windows_agent.mt5_lifecycle import Mt5LifecycleCoordinator
from windows_agent.provisioning.mt5_instance import (
    InstanceProvisioner,
    _MANAGED_RUNTIME_ASSETS,
)
from windows_agent.provisioning.mt5_instance_rotation import (
    Mt5InstanceRotationError,
    Mt5InstanceRotator,
    Mt5TemplateRelease,
)
from windows_agent.state_store import read_json
from windows_agent.worker.native_mt5_runtime import NativeMt5Status


class FakeSecrets:
    def read(self, _connection_id: str, name: str) -> str:
        return {"mt5_login": "42", "mt5_server": "Broker-Demo"}[name]


class FakeProcess:
    adopted: list[Path] = []
    cleaned: list[Path] = []

    def __init__(self, _state_path: Path) -> None:
        pass

    def adopt(self, terminal: Path) -> int:
        self.adopted.append(terminal)
        return 123

    def cleanup_path(self, terminal: Path) -> bool:
        self.cleaned.append(terminal)
        return True


class RuntimeController:
    def __init__(self, resume_results: list[object] | None = None) -> None:
        self.resume_results = list(resume_results or [])
        self.stop_results: list[bool] = []
        self.stop_calls = 0
        self.resume_calls: list[dict] = []
        self.callback_calls: list[tuple[object, bool]] = []

    def factory(self, root: Path, connection_id: str):
        owner = self

        class Runtime:
            def __init__(self) -> None:
                self.root = root
                self.connection_id = connection_id

            def stop(self) -> bool:
                owner.stop_calls += 1
                return owner.stop_results.pop(0) if owner.stop_results else True

            def set_verified_vendor_update_callback(
                self,
                callback: object,
                *,
                required: bool,
            ) -> None:
                owner.callback_calls.append((callback, required))

            def resume(self, **kwargs):
                owner.resume_calls.append(kwargs)
                if owner.resume_results:
                    result = owner.resume_results.pop(0)
                    if isinstance(result, BaseException):
                        raise result
                    if callable(result):
                        return result(root, connection_id)
                    return result
                return _healthy_status(root)

        return Runtime()


def _healthy_status(root: Path) -> NativeMt5Status:
    return NativeMt5Status(
        123,
        {"login": "42", "server": "Broker-Demo", "trade_allowed": False},
        {"terminal_connected": True, "account_trade_allowed": False},
        root / "terminal" / "MQL5" / "Files" / "TradeJournal",
    )


def _template(root: Path, terminal_payload: bytes) -> tuple[Path, Path]:
    terminal = root / "terminal64.exe"
    terminal.parent.mkdir(parents=True)
    terminal.write_bytes(terminal_payload)
    (root / "Config").mkdir()
    (root / "MQL5" / "Files").mkdir(parents=True)
    for relative in _MANAGED_RUNTIME_ASSETS:
        asset = root / relative
        asset.parent.mkdir(parents=True, exist_ok=True)
        asset.write_bytes(b"managed-" + relative.as_posix().encode("ascii"))
    expert = root / _MANAGED_RUNTIME_ASSETS[0]
    return terminal, expert


def _instance(
    tmp_path: Path,
    source_terminal: Path,
    connection_id: str,
) -> Path:
    instances = tmp_path / "instances"
    root = InstanceProvisioner(instances, tmp_path / "secrets").provision(
        connection_id,
        source_terminal,
        InstanceProvisioner._sha256(source_terminal),
    )
    config = root / "terminal" / "Config"
    (config / "accounts.dat").write_bytes(b"private-account")
    certificates = config / "certificates"
    certificates.mkdir()
    (certificates / "broker.cer").write_bytes(b"private-certificate")
    files = root / "terminal" / "MQL5" / "Files" / "TradeJournal"
    files.mkdir(parents=True)
    (files / "cursor.json").write_text("cursor-17", encoding="utf-8")
    bases = root / "terminal" / "Bases"
    bases.mkdir()
    (bases / "history.bin").write_bytes(b"large-cache-not-preserved")
    return root


def _rotator(
    tmp_path: Path,
    source_terminal: Path,
    expert: Path,
    controller: RuntimeController,
) -> Mt5InstanceRotator:
    FakeProcess.adopted = []
    FakeProcess.cleaned = []
    return Mt5InstanceRotator(
        instances_root=tmp_path / "instances",
        secrets_root=tmp_path / "secrets",
        source_terminal=source_terminal,
        expert_binary=expert,
        expert_sha256=InstanceProvisioner._sha256(expert),
        lifecycle=Mt5LifecycleCoordinator(),
        template_lock=RLock(),
        runtime_factory=controller.factory,
        process_factory=FakeProcess,
        secret_store=FakeSecrets(),
    )


def test_source_terminal_clone_reuses_rotation_dependencies(tmp_path: Path) -> None:
    source_terminal, expert = _template(tmp_path / "source", b"terminal-v1")
    candidate_terminal, _ = _template(tmp_path / "candidate", b"terminal-v2")
    controller = RuntimeController()
    rotator = _rotator(tmp_path, source_terminal, expert, controller)

    candidate = rotator.for_source_terminal(candidate_terminal)

    assert candidate.source_terminal == candidate_terminal.resolve()
    assert candidate.instances_root == rotator.instances_root
    assert candidate.secrets_root == rotator.secrets_root
    assert candidate.secrets is rotator.secrets
    assert candidate.expert_binary == rotator.expert_binary
    assert candidate.lifecycle is rotator.lifecycle
    assert candidate.template_lock is rotator.template_lock
    assert candidate.runtime_factory is rotator.runtime_factory
    assert candidate.process_factory is rotator.process_factory


def test_rotation_preserves_only_required_private_state_and_resumes_new_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_terminal, _ = _template(tmp_path / "old", b"terminal-v1")
    new_terminal, expert = _template(tmp_path / "new", b"terminal-v2")
    connection_id = str(uuid4())
    root = _instance(tmp_path, old_terminal, connection_id)
    controller = RuntimeController()
    rotator = _rotator(tmp_path, new_terminal, expert, controller)
    target = Mt5TemplateRelease.from_template(
        new_terminal,
        InstanceProvisioner._sha256(new_terminal),
    )
    history_from = datetime(2026, 8, 27, 21, 29, tzinfo=timezone.utc)
    monkeypatch.setattr(
        rotation_module,
        "new_only_recovery_from",
        lambda _root: history_from,
    )
    def verified_update_callback(*_args):
        return None

    assert rotator.rotate_one(
        connection_id,
        target,
        verified_update_callback=verified_update_callback,
        verified_update_required=True,
    ) is True

    assert (root / "terminal" / "terminal64.exe").read_bytes() == b"terminal-v2"
    assert (
        root / "terminal" / "Config" / "accounts.dat"
    ).read_bytes() == b"private-account"
    assert (
        root / "terminal" / "Config" / "certificates" / "broker.cer"
    ).read_bytes() == b"private-certificate"
    assert (
        root / "terminal" / "MQL5" / "Files" / "TradeJournal" / "cursor.json"
    ).read_text(encoding="utf-8") == "cursor-17"
    assert not (root / "terminal" / "Bases").exists()
    assert controller.resume_calls == [
        {
            "login": 42,
            "server": "Broker-Demo",
            "expert_binary": expert.resolve(),
            "history_mode": "new_only",
            "history_from": history_from,
        }
    ]
    assert FakeProcess.adopted == [root / "terminal" / "terminal64.exe"]
    assert controller.callback_calls == [(verified_update_callback, True)]
    assert not (root / ".terminal-maintenance-backup").exists()
    assert not (root / "state" / "mt5-rotation.json").exists()


def test_rotation_expected_source_rejects_changed_release_before_mutation(
    tmp_path: Path,
) -> None:
    old_terminal, _ = _template(tmp_path / "old", b"terminal-v1")
    new_terminal, expert = _template(tmp_path / "new", b"terminal-v2")
    connection_id = str(uuid4())
    root = _instance(tmp_path, old_terminal, connection_id)
    expected_source = Mt5TemplateRelease.from_template(
        old_terminal,
        InstanceProvisioner._sha256(old_terminal),
    )

    # Model another verified writer winning the race after inventory: both
    # files and state are internally consistent, but no longer identify the
    # release the caller authorized as the rotation source.
    live_terminal = root / "terminal" / "terminal64.exe"
    live_terminal.write_bytes(b"terminal-intermediate")
    state_path = root / "state" / "instance.json"
    changed_state = read_json(state_path)
    changed_state["terminal_sha256"] = InstanceProvisioner._sha256(live_terminal)
    changed_state["template_code_manifest_sha256"] = (
        InstanceProvisioner._code_manifest(root / "terminal")
    )
    rotation_module.atomic_json(state_path, changed_state)
    terminal_before = live_terminal.read_bytes()
    state_before = state_path.read_bytes()

    controller = RuntimeController()
    rotator = _rotator(tmp_path, new_terminal, expert, controller)
    target = Mt5TemplateRelease.from_template(
        new_terminal,
        InstanceProvisioner._sha256(new_terminal),
    )

    with pytest.raises(
        Mt5InstanceRotationError,
        match="source release changed",
    ):
        rotator.rotate_one(
            connection_id,
            target,
            expected_source=expected_source,
        )

    assert controller.stop_calls == 0
    assert controller.resume_calls == []
    assert FakeProcess.adopted == []
    assert live_terminal.read_bytes() == terminal_before
    assert state_path.read_bytes() == state_before
    assert not (root / "state" / "mt5-rotation.json").exists()
    assert not (root / ".terminal-maintenance-staging").exists()
    assert not (root / ".terminal-maintenance-backup").exists()


def test_rotation_expected_source_allows_exact_observed_release(
    tmp_path: Path,
) -> None:
    old_terminal, _ = _template(tmp_path / "old", b"terminal-v1")
    new_terminal, expert = _template(tmp_path / "new", b"terminal-v2")
    connection_id = str(uuid4())
    root = _instance(tmp_path, old_terminal, connection_id)
    controller = RuntimeController()
    rotator = _rotator(tmp_path, new_terminal, expert, controller)
    expected_source = Mt5TemplateRelease.from_template(
        old_terminal,
        InstanceProvisioner._sha256(old_terminal),
    )
    target = Mt5TemplateRelease.from_template(
        new_terminal,
        InstanceProvisioner._sha256(new_terminal),
    )

    assert rotator.rotate_one(
        connection_id,
        target,
        expected_source=expected_source,
    ) is True

    assert controller.stop_calls == 1
    assert (root / "terminal" / "terminal64.exe").read_bytes() == b"terminal-v2"


def test_rotation_without_expected_source_remains_backward_compatible(
    tmp_path: Path,
) -> None:
    old_terminal, _ = _template(tmp_path / "old", b"terminal-v1")
    new_terminal, expert = _template(tmp_path / "new", b"terminal-v2")
    connection_id = str(uuid4())
    root = _instance(tmp_path, old_terminal, connection_id)
    controller = RuntimeController()
    rotator = _rotator(tmp_path, new_terminal, expert, controller)
    target = Mt5TemplateRelease.from_template(
        new_terminal,
        InstanceProvisioner._sha256(new_terminal),
    )

    assert rotator.rotate_one(connection_id, target) is True

    assert controller.stop_calls == 1
    assert (root / "terminal" / "terminal64.exe").read_bytes() == b"terminal-v2"


def test_rotation_retries_transient_windows_directory_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_terminal, _ = _template(tmp_path / "old", b"terminal-v1")
    new_terminal, expert = _template(tmp_path / "new", b"terminal-v2")
    connection_id = str(uuid4())
    root = _instance(tmp_path, old_terminal, connection_id)
    rotator = _rotator(tmp_path, new_terminal, expert, RuntimeController())
    target = Mt5TemplateRelease.from_template(
        new_terminal,
        InstanceProvisioner._sha256(new_terminal),
    )
    real_replace = rotation_module.durable_replace
    attempts = 0
    delays: list[float] = []

    def flaky_replace(source: Path, destination: Path) -> None:
        nonlocal attempts
        if source == root / "terminal":
            attempts += 1
            if attempts < 3:
                error = OSError("access denied")
                error.winerror = 5
                raise error
        real_replace(source, destination)

    monkeypatch.setattr(rotation_module, "durable_replace", flaky_replace)
    monkeypatch.setattr(rotation_module.time, "sleep", delays.append)

    assert rotator.rotate_one(connection_id, target) is True

    assert attempts == 3
    assert delays == [0.1, 0.2]
    assert (root / "terminal" / "terminal64.exe").read_bytes() == b"terminal-v2"


def test_certificate_reparse_point_is_rejected_before_terminal_stop(
    tmp_path: Path,
) -> None:
    old_terminal, _ = _template(tmp_path / "old", b"terminal-v1")
    new_terminal, expert = _template(tmp_path / "new", b"terminal-v2")
    connection_id = str(uuid4())
    root = _instance(tmp_path, old_terminal, connection_id)
    certificates = root / "terminal" / "Config" / "certificates"
    shutil.rmtree(certificates)
    outside = tmp_path / "outside-certificates"
    outside.mkdir()
    marker = outside / "must-survive.cer"
    marker.write_bytes(b"safe")
    certificates.symlink_to(outside, target_is_directory=True)
    controller = RuntimeController()
    rotator = _rotator(tmp_path, new_terminal, expert, controller)
    target = Mt5TemplateRelease.from_template(
        new_terminal,
        InstanceProvisioner._sha256(new_terminal),
    )

    with pytest.raises(Mt5InstanceRotationError, match="release integrity"):
        rotator.rotate_one(connection_id, target)

    assert controller.stop_calls == 0
    assert marker.read_bytes() == b"safe"


def test_failed_new_release_rolls_back_and_adopts_restarted_old_process(
    tmp_path: Path,
) -> None:
    old_terminal, _ = _template(tmp_path / "old", b"terminal-v1")
    new_terminal, expert = _template(tmp_path / "new", b"terminal-v2")
    connection_id = str(uuid4())
    root = _instance(tmp_path, old_terminal, connection_id)
    previous = read_json(root / "state" / "instance.json")
    controller = RuntimeController(
        [RuntimeError("new build failed"), _healthy_status(root)]
    )
    rotator = _rotator(tmp_path, new_terminal, expert, controller)
    target = Mt5TemplateRelease.from_template(
        new_terminal,
        InstanceProvisioner._sha256(new_terminal),
    )

    with pytest.raises(Mt5InstanceRotationError, match="was rolled back"):
        rotator.rotate_one(connection_id, target)

    assert (root / "terminal" / "terminal64.exe").read_bytes() == b"terminal-v1"
    assert read_json(root / "state" / "instance.json") == previous
    assert [call["history_mode"] for call in controller.resume_calls] == [
        "new_only",
        "new_only",
    ]
    assert FakeProcess.adopted == [root / "terminal" / "terminal64.exe"]
    assert not (root / "state" / "mt5-rotation.json").exists()


def test_rollback_accepts_health_verified_vendor_update_with_required_capture(
    tmp_path: Path,
) -> None:
    old_terminal, _ = _template(tmp_path / "old", b"terminal-v1")
    new_terminal, expert = _template(tmp_path / "new", b"terminal-v2")
    connection_id = str(uuid4())
    root = _instance(tmp_path, old_terminal, connection_id)

    def apply_vendor_update(
        instance_root: Path,
        observed_connection_id: str,
    ) -> NativeMt5Status:
        (instance_root / "terminal" / "terminal64.exe").write_bytes(
            b"terminal-v3"
        )
        InstanceProvisioner.record_verified_vendor_update(
            instance_root,
            observed_connection_id,
            "CN=MetaQuotes Ltd., O=MetaQuotes Ltd.",
        )
        return _healthy_status(instance_root)

    controller = RuntimeController(
        [RuntimeError("new build failed"), apply_vendor_update]
    )
    rotator = _rotator(tmp_path, new_terminal, expert, controller)
    target = Mt5TemplateRelease.from_template(
        new_terminal,
        InstanceProvisioner._sha256(new_terminal),
    )

    def capture(*_args: object) -> str:
        return "receipt"

    with pytest.raises(Mt5InstanceRotationError, match="was rolled back"):
        rotator.rotate_one(
            connection_id,
            target,
            verified_update_callback=capture,
            verified_update_required=True,
        )

    assert (
        root / "terminal" / "terminal64.exe"
    ).read_bytes() == b"terminal-v3"
    assert not (root / "state" / "mt5-rotation.json").exists()


def test_cleanup_failure_after_commit_never_rolls_back_new_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_terminal, _ = _template(tmp_path / "old", b"terminal-v1")
    new_terminal, expert = _template(tmp_path / "new", b"terminal-v2")
    connection_id = str(uuid4())
    root = _instance(tmp_path, old_terminal, connection_id)
    controller = RuntimeController()
    rotator = _rotator(tmp_path, new_terminal, expert, controller)
    target = Mt5TemplateRelease.from_template(
        new_terminal,
        InstanceProvisioner._sha256(new_terminal),
    )
    original_remove = rotator._remove_tree

    def fail_backup_cleanup(path: Path) -> None:
        if path.name == ".terminal-maintenance-backup":
            raise OSError("sharing violation")
        original_remove(path)

    monkeypatch.setattr(rotator, "_remove_tree", fail_backup_cleanup)
    assert rotator.rotate_one(connection_id, target) is True
    assert (root / "terminal" / "terminal64.exe").read_bytes() == b"terminal-v2"
    assert read_json(root / "state" / "mt5-rotation.json")["phase"] == "committed"

    monkeypatch.setattr(rotator, "_remove_tree", original_remove)
    recovery = rotator.recover_incomplete()
    assert recovery.recovered == (connection_id,)
    assert recovery.failed == ()
    assert (root / "terminal" / "terminal64.exe").read_bytes() == b"terminal-v2"
    assert not (root / ".terminal-maintenance-backup").exists()


def test_retry_does_not_overwrite_unfinished_rotation_journal(
    tmp_path: Path,
) -> None:
    old_terminal, _ = _template(tmp_path / "old", b"terminal-v1")
    new_terminal, expert = _template(tmp_path / "new", b"terminal-v2")
    connection_id = str(uuid4())
    root = _instance(tmp_path, old_terminal, connection_id)
    rotator = _rotator(tmp_path, new_terminal, expert, RuntimeController())
    target = Mt5TemplateRelease.from_template(
        new_terminal,
        InstanceProvisioner._sha256(new_terminal),
    )
    previous = read_json(root / "state" / "instance.json")
    journal = root / "state" / "mt5-rotation.json"
    rotator._write_journal(
        journal,
        connection_id=connection_id,
        phase="rollback_failed",
        previous_state=previous,
        target=target,
    )
    before = journal.read_bytes()

    with pytest.raises(Mt5InstanceRotationError, match="requires recovery"):
        rotator.rotate_one(connection_id, target)

    assert journal.read_bytes() == before


def test_tampered_expert_is_rejected_before_terminal_stop(tmp_path: Path) -> None:
    old_terminal, _ = _template(tmp_path / "old", b"terminal-v1")
    new_terminal, expert = _template(tmp_path / "new", b"terminal-v2")
    connection_id = str(uuid4())
    root = _instance(tmp_path, old_terminal, connection_id)
    controller = RuntimeController()
    rotator = _rotator(tmp_path, new_terminal, expert, controller)
    target = Mt5TemplateRelease.from_template(
        new_terminal,
        InstanceProvisioner._sha256(new_terminal),
    )
    expert.write_bytes(b"tampered")

    with pytest.raises(Mt5InstanceRotationError, match="expert digest"):
        rotator.rotate_one(connection_id, target)

    assert controller.stop_calls == 0
    assert (root / "terminal" / "terminal64.exe").read_bytes() == b"terminal-v1"
    assert not (root / "state" / "mt5-rotation.json").exists()


def test_uuid_shaped_symlink_is_rejected_without_touching_its_target(
    tmp_path: Path,
) -> None:
    new_terminal, expert = _template(tmp_path / "new", b"terminal-v2")
    connection_id = str(uuid4())
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "must-survive"
    marker.write_text("safe", encoding="utf-8")
    instances = tmp_path / "instances"
    instances.mkdir()
    (instances / connection_id).symlink_to(outside, target_is_directory=True)
    rotator = _rotator(
        tmp_path,
        new_terminal,
        expert,
        RuntimeController(),
    )
    target = Mt5TemplateRelease.from_template(
        new_terminal,
        InstanceProvisioner._sha256(new_terminal),
    )

    with pytest.raises(Mt5InstanceRotationError, match="root is unsafe"):
        rotator.rotate_one(connection_id, target)

    assert marker.read_text(encoding="utf-8") == "safe"


def test_tampered_backup_is_never_restored_during_recovery(tmp_path: Path) -> None:
    old_terminal, _ = _template(tmp_path / "old", b"terminal-v1")
    new_terminal, expert = _template(tmp_path / "new", b"terminal-v2")
    connection_id = str(uuid4())
    root = _instance(tmp_path, old_terminal, connection_id)
    rotator = _rotator(tmp_path, new_terminal, expert, RuntimeController())
    target = Mt5TemplateRelease.from_template(
        new_terminal,
        InstanceProvisioner._sha256(new_terminal),
    )
    previous = read_json(root / "state" / "instance.json")
    backup = root / ".terminal-maintenance-backup"
    (root / "terminal").rename(backup)
    shutil.copytree(new_terminal.parent, root / "terminal")
    (backup / "terminal64.exe").write_bytes(b"tampered-old")
    rotator._write_journal(
        root / "state" / "mt5-rotation.json",
        connection_id=connection_id,
        phase="swapped",
        previous_state=previous,
        target=target,
    )

    recovery = rotator.recover_incomplete()

    assert recovery.recovered == ()
    assert recovery.failed == (connection_id,)
    assert (root / "terminal" / "terminal64.exe").read_bytes() == b"terminal-v2"
    assert (root / "state" / "mt5-rotation.json").exists()


def test_failed_rollback_stop_keeps_backup_and_journal_for_recovery(
    tmp_path: Path,
) -> None:
    old_terminal, _ = _template(tmp_path / "old", b"terminal-v1")
    new_terminal, expert = _template(tmp_path / "new", b"terminal-v2")
    connection_id = str(uuid4())
    root = _instance(tmp_path, old_terminal, connection_id)
    controller = RuntimeController([RuntimeError("new build failed")])
    controller.stop_results = [True, False]
    rotator = _rotator(tmp_path, new_terminal, expert, controller)
    target = Mt5TemplateRelease.from_template(
        new_terminal,
        InstanceProvisioner._sha256(new_terminal),
    )

    with pytest.raises(Mt5InstanceRotationError, match="and rollback failed"):
        rotator.rotate_one(connection_id, target)

    assert (
        root / ".terminal-maintenance-backup" / "terminal64.exe"
    ).read_bytes() == b"terminal-v1"
    journal = read_json(root / "state" / "mt5-rotation.json")
    assert journal["phase"] == "rollback_failed"
    persisted_cutoff = datetime.fromtimestamp(
        journal["new_only_recovery_from_unix"],
        timezone.utc,
    )

    recovery_controller = RuntimeController()
    recovery_rotator = _rotator(
        tmp_path,
        new_terminal,
        expert,
        recovery_controller,
    )

    def verified_update_callback(*_args):
        return None

    recovery = recovery_rotator.recover_incomplete(
        verified_update_callback=verified_update_callback,
        verified_update_required=True,
    )

    assert recovery.recovered == (connection_id,)
    assert recovery.failed == ()
    assert recovery_controller.resume_calls[0]["history_mode"] == "new_only"
    assert recovery_controller.resume_calls[0]["history_from"] == persisted_cutoff
    assert recovery_controller.callback_calls == [
        (verified_update_callback, True)
    ]


def test_fleet_rotation_continues_after_middle_instance_failure(
    tmp_path: Path,
) -> None:
    old_terminal, _ = _template(tmp_path / "old", b"terminal-v1")
    new_terminal, expert = _template(tmp_path / "new", b"terminal-v2")
    connection_ids = tuple(sorted(str(uuid4()) for _ in range(3)))
    for connection_id in connection_ids:
        _instance(tmp_path, old_terminal, connection_id)
    rotator = _rotator(
        tmp_path,
        new_terminal,
        expert,
        RuntimeController(),
    )
    target = Mt5TemplateRelease.from_template(
        new_terminal,
        InstanceProvisioner._sha256(new_terminal),
    )
    attempted: list[str] = []

    def rotate_one(connection_id: str, *_args: object, **_kwargs: object) -> bool:
        attempted.append(connection_id)
        if connection_id == connection_ids[1]:
            raise Mt5InstanceRotationError("simulated account failure")
        return True

    with (
        patch.object(rotator, "_matches_target", return_value=False),
        patch.object(rotator, "rotate_one", side_effect=rotate_one),
        pytest.raises(Mt5InstanceRotationError) as failure,
    ):
        rotator.rotate_all(target, Event())

    assert attempted == list(connection_ids)
    assert failure.value.failed == (connection_ids[1],)
