from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import Mock, call, patch

import pytest

from windows_agent.provisioning.mt5_instance import (
    InstanceProvisioner,
    _MANAGED_RUNTIME_ASSETS,
)
from windows_agent.provisioning.mt5_update_store import (
    MAX_UPDATE_BUNDLE_FILE_BYTES,
    Mt5PendingUpdateStore,
    Mt5PendingUpdateStoreError,
    Mt5UpdateRelease,
    load_verified_update_bundle,
    seal_applied_update_bundle,
    stage_verified_update_bundle,
    write_updater_only_config,
)
from windows_agent.state_store import atomic_json, read_json
from windows_agent.worker.native_mt5_runtime import (
    NativeMt5Error,
    NativeMt5Runtime,
    NativeMt5Status,
    _LiveUpdateCandidate,
)


CONNECTION_ID = "00000000-0000-4000-8000-000000000001"
SIGNER = "CN=MetaQuotes Ltd., O=MetaQuotes Ltd., S=Lemesos, C=CY"


def _runtime(tmp_path: Path) -> NativeMt5Runtime:
    terminal = tmp_path / "terminal" / "terminal64.exe"
    terminal.parent.mkdir(parents=True)
    terminal.write_bytes(b"terminal-v1")
    for relative in _MANAGED_RUNTIME_ASSETS:
        asset = terminal.parent / relative
        asset.parent.mkdir(parents=True, exist_ok=True)
        asset.write_bytes(relative.as_posix().encode("utf-8"))
    (tmp_path / "state").mkdir()
    return NativeMt5Runtime(tmp_path, CONNECTION_ID)


def _applied_orphan(runtime: NativeMt5Runtime, tmp_path: Path) -> Path:
    source_root = tmp_path / "source-release"
    (source_root / "MQL5" / "Experts").mkdir(parents=True)
    (source_root / "terminal64.exe").write_bytes(b"terminal-v0")
    (source_root / "MQL5" / "Experts" / "vendor.ex5").write_bytes(b"vendor")
    source_release = Mt5UpdateRelease.from_terminal_root(source_root)
    target_release = Mt5UpdateRelease.from_terminal_root(runtime.terminal_root)
    bundle = runtime.state / "live-update-crash-fixture"
    bundle.mkdir()
    updater = bundle / "terminal64.exe"
    updater.write_bytes(b"signed-updater")
    (bundle / "mt5onnx64.6090").write_bytes(b"opaque-payload")
    (bundle / "temp").mkdir()
    updater_config = write_updater_only_config(
        bundle / "tradejournal-update.ini"
    )
    stage_verified_update_bundle(
        bundle,
        source_connection_id=CONNECTION_ID,
        source_release=source_release,
        managed_assets_manifest_sha256=(
            InstanceProvisioner._managed_runtime_assets_manifest(
                runtime.terminal_root
            )
        ),
        signer_subject=SIGNER,
        updater=updater,
        updater_config=updater_config,
    )
    seal_applied_update_bundle(bundle, target_release)
    return bundle


def _staged_recovery_runtime(
    tmp_path: Path,
) -> tuple[NativeMt5Runtime, Path, Mt5UpdateRelease]:
    root = tmp_path / "instances" / CONNECTION_ID
    terminal = root / "terminal" / "terminal64.exe"
    terminal.parent.mkdir(parents=True)
    terminal.write_bytes(b"terminal-v1")
    for relative in _MANAGED_RUNTIME_ASSETS:
        asset = terminal.parent / relative
        asset.parent.mkdir(parents=True, exist_ok=True)
        asset.write_bytes(relative.as_posix().encode("utf-8"))
    source_release = Mt5UpdateRelease.from_terminal_root(terminal.parent)
    managed_assets = InstanceProvisioner._managed_runtime_assets_manifest(
        terminal.parent
    )
    atomic_json(
        root / "state" / "instance.json",
        {
            "connection_id": CONNECTION_ID,
            "status": "provisioned",
            "terminal": str(terminal),
            "terminal_sha256": source_release.terminal_sha256,
            "template_manifest_sha256": "0" * 64,
            "template_code_manifest_sha256": (
                source_release.code_manifest_sha256
            ),
            "runtime_assets_manifest_sha256": managed_assets,
            "runtime_assets_manifest_version": 1,
        },
    )
    runtime = NativeMt5Runtime(root, CONNECTION_ID)
    bundle = runtime.state / "live-update-staged-crash"
    bundle.mkdir()
    updater = bundle / "terminal64.exe"
    updater.write_bytes(b"signed-updater")
    (bundle / "mt5onnx64.6090").write_bytes(b"opaque-payload")
    (bundle / "temp").mkdir()
    config = write_updater_only_config(bundle / "tradejournal-update.ini")
    stage_verified_update_bundle(
        bundle,
        source_connection_id=CONNECTION_ID,
        source_release=source_release,
        managed_assets_manifest_sha256=managed_assets,
        signer_subject=SIGNER,
        updater=updater,
        updater_config=config,
    )
    return runtime, bundle, source_release


def test_authorization_wait_reports_announced_live_update(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    config = runtime.state / "login-bootstrap.ini"
    config.write_text("temporary", encoding="utf-8")
    updater = (
        tmp_path
        / "profile"
        / "AppData"
        / "Roaming"
        / "MetaQuotes"
        / "Terminal"
        / ("A" * 32)
        / "liveupdate"
        / "terminal64.exe"
    )
    candidate = _LiveUpdateCandidate(
        pid=42,
        executable=updater,
        arguments=(
            str(updater),
            "/update",
            f"/path:{runtime.terminal_root}",
            f"/config:{config}",
        ),
    )
    line = (
        'AA\t0\t00:00:00\tLiveUpdate\tstart "'
        f'{updater}" /update /path:"{runtime.terminal_root}"'
    )
    with (
        patch.object(runtime, "_journal_lines_since", return_value=[line]),
        patch.object(runtime, "_running_terminal_pids", return_value=[]),
        patch.object(runtime, "_live_update_candidates", return_value=[candidate]),
    ):
        with pytest.raises(NativeMt5Error, match="mt5_live_update_required"):
            runtime._wait_for_authorization(
                {},
                42,
                "Demo",
                1,
                update_config=config,
            )
    assert runtime._pending_live_update == candidate


def test_authorization_wait_binds_exact_announced_cached_update(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    config = runtime.state / "login-bootstrap.ini"
    config.write_text("temporary", encoding="utf-8")
    updater = (
        tmp_path
        / "profile"
        / "AppData"
        / "Roaming"
        / "MetaQuotes"
        / "Terminal"
        / ("A" * 32)
        / "liveupdate"
        / "terminal64.exe"
    )
    candidate = _LiveUpdateCandidate(
        pid=0,
        executable=updater,
        arguments=(str(updater), "/update"),
    )
    line = (
        'AA\t0\t00:00:00\tLiveUpdate\tstart "'
        f'{updater}" /update /path:"{runtime.terminal_root}"'
    )
    with (
        patch.object(runtime, "_journal_lines_since", return_value=[line]),
        patch.object(runtime, "_running_terminal_pids", return_value=[]),
        patch.object(runtime, "_live_update_candidates", return_value=[]),
        patch.object(
            runtime,
            "_cached_live_update_candidates",
            return_value=[candidate],
        ) as cached,
        pytest.raises(NativeMt5Error, match="mt5_live_update_required"),
    ):
        runtime._wait_for_authorization(
            {},
            42,
            "Demo",
            1,
            update_config=config,
        )

    assert runtime._pending_live_update == candidate
    cached.assert_called_once_with(config)


def test_authorization_wait_ignores_unmatched_cached_update(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    config = runtime.state / "login-bootstrap.ini"
    config.write_text("temporary", encoding="utf-8")
    announced = tmp_path / "announced" / "terminal64.exe"
    stale = _LiveUpdateCandidate(
        pid=0,
        executable=tmp_path / "stale" / "terminal64.exe",
        arguments=("terminal64.exe", "/update"),
    )
    marker = (
        'AA\t0\t00:00:00\tLiveUpdate\tstart "'
        f'{announced}" /update /path:"{runtime.terminal_root}"'
    )
    authorized = (
        "BB\t0\t00:00:01\tNetwork\t'42': authorized on Demo "
        "through Access Point"
    )
    with (
        patch.object(
            runtime,
            "_journal_lines_since",
            side_effect=[[marker], [authorized]],
        ),
        patch.object(runtime, "_running_terminal_pids", return_value=[]),
        patch.object(runtime, "_live_update_candidates", return_value=[]),
        patch.object(
            runtime,
            "_cached_live_update_candidates",
            return_value=[stale],
        ) as cached,
    ):
        observed = runtime._wait_for_authorization(
            {},
            42,
            "Demo",
            1,
            update_config=config,
        )

    assert observed == "Demo"
    assert runtime._pending_live_update is None
    cached.assert_called_once_with(config)


def test_authorization_wait_rejects_ambiguous_announced_cached_update(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    config = runtime.state / "login-bootstrap.ini"
    config.write_text("temporary", encoding="utf-8")
    updater = tmp_path / "announced" / "terminal64.exe"
    candidates = [
        _LiveUpdateCandidate(
            pid=0,
            executable=updater,
            arguments=(str(updater), "/update"),
        ),
        _LiveUpdateCandidate(
            pid=0,
            executable=updater,
            arguments=(str(updater), "/update"),
        ),
    ]
    marker = (
        'AA\t0\t00:00:00\tLiveUpdate\tstart "'
        f'{updater}" /update /path:"{runtime.terminal_root}"'
    )
    with (
        patch.object(runtime, "_journal_lines_since", return_value=[marker]),
        patch.object(runtime, "_running_terminal_pids", return_value=[]),
        patch.object(runtime, "_live_update_candidates", return_value=[]),
        patch.object(
            runtime,
            "_cached_live_update_candidates",
            return_value=candidates,
        ),
        pytest.raises(
            NativeMt5Error,
            match="mt5_update_process_ambiguous",
        ),
    ):
        runtime._wait_for_authorization(
            {},
            42,
            "Demo",
            1,
            update_config=config,
        )


def test_authorization_phase_completes_one_update_then_retries(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    config = runtime.state / "login-bootstrap.ini"
    config.write_text("temporary", encoding="utf-8")
    with (
        patch.object(runtime, "_journal_checkpoint", side_effect=[{"a": 1}, {"b": 2}]),
        patch.object(runtime, "_start_process") as start_process,
        patch.object(
            runtime,
            "_wait_for_authorization",
            side_effect=[NativeMt5Error("mt5_live_update_required"), "Demo"],
        ) as wait,
        patch.object(runtime, "_complete_live_update") as update,
    ):
        checkpoint, observed = runtime._start_and_wait_for_authorization(
            config,
            42,
            "Demo",
            30,
        )
    assert checkpoint == {"b": 2}
    assert observed == "Demo"
    assert start_process.call_args_list == [call(config), call(config)]
    assert wait.call_count == 2
    update.assert_called_once_with(config, timeout=240.0)


def test_authorization_phase_converges_across_multiple_signed_update_hops(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    config = runtime.state / "login-bootstrap.ini"
    config.write_text("temporary", encoding="utf-8")
    releases = iter((b"terminal-v2", b"terminal-v3"))

    def complete(*_args: object, **_kwargs: object) -> None:
        runtime.terminal.write_bytes(next(releases))

    with (
        patch.object(
            runtime,
            "_journal_checkpoint",
            side_effect=[{"a": 1}, {"b": 2}, {"c": 3}],
        ),
        patch.object(runtime, "_start_process") as start_process,
        patch.object(
            runtime,
            "_wait_for_authorization",
            side_effect=[
                NativeMt5Error("mt5_live_update_required"),
                NativeMt5Error("mt5_live_update_required"),
                "Demo",
            ],
        ) as wait,
        patch.object(
            runtime,
            "_complete_live_update",
            side_effect=complete,
        ) as update,
    ):
        checkpoint, observed = runtime._start_and_wait_for_authorization(
            config,
            42,
            "Demo",
            30,
        )

    assert checkpoint == {"c": 3}
    assert observed == "Demo"
    assert start_process.call_count == 3
    assert wait.call_count == 3
    assert update.call_count == 2
    assert all(call.kwargs["timeout"] == 240.0 for call in update.call_args_list)


def test_authorization_phase_rejects_repeated_no_progress_updates(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    config = runtime.state / "login-bootstrap.ini"
    config.write_text("temporary", encoding="utf-8")

    with (
        patch.object(runtime, "_journal_checkpoint", return_value={}),
        patch.object(runtime, "_start_process"),
        patch.object(
            runtime,
            "_wait_for_authorization",
            side_effect=NativeMt5Error("mt5_live_update_required"),
        ),
        patch.object(runtime, "_complete_live_update") as update,
        pytest.raises(NativeMt5Error, match="mt5_update_no_progress"),
    ):
        runtime._start_and_wait_for_authorization(
            config,
            42,
            "Demo",
            30,
        )

    assert update.call_count == 2


def test_unbound_cached_update_is_not_applied_before_terminal_start(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    config = runtime.state / "login-bootstrap.ini"
    config.write_text("temporary", encoding="utf-8")
    candidate = _LiveUpdateCandidate(
        pid=0,
        executable=tmp_path / "profile" / "liveupdate" / "terminal64.exe",
        arguments=("terminal64.exe", "/update"),
    )
    with (
        patch.object(runtime, "_cached_live_update_candidates", return_value=[candidate]),
        patch.object(runtime, "_complete_live_update") as complete,
        patch.object(runtime, "_journal_checkpoint", return_value={}),
        patch.object(runtime, "_start_process") as start,
        patch.object(runtime, "_wait_for_authorization", return_value="Demo"),
    ):
        _, observed = runtime._start_and_wait_for_authorization(
            config,
            42,
            "Demo",
            30,
        )

    assert observed == "Demo"
    assert runtime._pending_live_update is None
    start.assert_called_once_with(config)
    complete.assert_not_called()


def test_cached_update_is_bound_to_running_updater_before_cleanup(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    config = runtime.state / "login-bootstrap.ini"
    config.write_text("temporary", encoding="utf-8")
    source = tmp_path / "profile" / "liveupdate" / "terminal64.exe"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"signed-updater")
    cached = _LiveUpdateCandidate(
        pid=0,
        executable=source,
        arguments=(str(source), "/update"),
    )
    running = _LiveUpdateCandidate(
        pid=42,
        executable=source,
        arguments=(str(source), "/update"),
    )
    runtime._pending_live_update = cached
    process = Mock(returncode=0)
    process.poll.return_value = 0

    with (
        patch.object(runtime, "_require_elevated_service"),
        patch.object(runtime, "_live_update_candidates", return_value=[running]),
        patch.object(runtime, "_verify_metaquotes_signature", return_value=SIGNER),
        patch.object(runtime, "_terminate_candidate") as terminate,
        patch.object(runtime, "stop", return_value=True),
        patch.object(runtime, "_remove_generated_example_code"),
        patch(
            "windows_agent.worker.native_mt5_runtime.WindowsSecretStore.restrict_acl"
        ),
        patch("subprocess.Popen", return_value=process),
        patch.object(
            InstanceProvisioner,
            "record_verified_vendor_update",
            return_value=InstanceProvisioner._sha256(runtime.terminal),
        ) as record,
    ):
        runtime._complete_live_update(config)

    terminate.assert_called_once_with(running)
    record.assert_not_called()
    assert runtime._pending_verified_vendor_updates == []
    assert not source.exists()


def test_running_live_update_wins_over_stale_auth_monitor_result(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    config = runtime.state / "login-bootstrap.ini"
    config.write_text("temporary", encoding="utf-8")
    updater = tmp_path / "profile" / "liveupdate" / "terminal64.exe"
    candidate = _LiveUpdateCandidate(
        pid=42,
        executable=updater,
        arguments=(str(updater), "/update"),
    )

    with (
        patch.object(runtime, "_journal_lines_since", return_value=[]),
        patch.object(runtime, "_running_terminal_pids", return_value=[]),
        patch.object(runtime, "_live_update_candidates", return_value=[candidate]),
        patch.object(
            runtime,
            "_authentication_failure_detected",
            side_effect=NativeMt5Error("authentication_monitor_result_invalid"),
        ) as authentication_failure,
        pytest.raises(NativeMt5Error, match="mt5_live_update_required"),
    ):
        runtime._wait_for_authorization(
            {},
            42,
            "Demo",
            1,
            update_config=config,
        )

    assert runtime._pending_live_update == candidate
    authentication_failure.assert_not_called()


def test_verified_vendor_update_rotates_instance_integrity_pin(tmp_path: Path) -> None:
    root = tmp_path / CONNECTION_ID
    terminal_root = root / "terminal"
    terminal_root.mkdir(parents=True)
    terminal = terminal_root / "terminal64.exe"
    terminal.write_bytes(b"terminal-v1")
    for relative in _MANAGED_RUNTIME_ASSETS:
        asset = terminal_root / relative
        asset.parent.mkdir(parents=True, exist_ok=True)
        asset.write_bytes(relative.as_posix().encode("utf-8"))
    old_terminal_sha = InstanceProvisioner._sha256(terminal)
    old_code_manifest = InstanceProvisioner._code_manifest(terminal_root)
    runtime_assets = InstanceProvisioner._managed_runtime_assets_manifest(terminal_root)
    atomic_json(
        root / "state" / "instance.json",
        {
            "connection_id": CONNECTION_ID,
            "status": "provisioned",
            "terminal": str(terminal),
            "terminal_sha256": old_terminal_sha,
            "template_manifest_sha256": "0" * 64,
            "template_code_manifest_sha256": old_code_manifest,
            "runtime_assets_manifest_sha256": runtime_assets,
            "runtime_assets_manifest_version": 1,
        },
    )

    terminal.write_bytes(b"terminal-v2")
    new_digest = InstanceProvisioner.record_verified_vendor_update(
        root,
        CONNECTION_ID,
        SIGNER,
    )
    state = read_json(root / "state" / "instance.json")
    assert new_digest != old_terminal_sha
    assert state["terminal_sha256"] == new_digest
    assert state["vendor_update"]["terminal_sha256"] == new_digest
    InstanceProvisioner._validate_published_instance(
        root,
        state,
        old_terminal_sha,
    )


def test_verified_vendor_update_rejects_changed_expected_binding(
    tmp_path: Path,
) -> None:
    root = tmp_path / CONNECTION_ID
    terminal_root = root / "terminal"
    terminal_root.mkdir(parents=True)
    terminal = terminal_root / "terminal64.exe"
    terminal.write_bytes(b"terminal-v1")
    for relative in _MANAGED_RUNTIME_ASSETS:
        asset = terminal_root / relative
        asset.parent.mkdir(parents=True, exist_ok=True)
        asset.write_bytes(relative.as_posix().encode("utf-8"))
    state_path = root / "state" / "instance.json"
    atomic_json(
        state_path,
        {
            "connection_id": CONNECTION_ID,
            "status": "provisioned",
            "terminal_sha256": InstanceProvisioner._sha256(terminal),
            "template_code_manifest_sha256": (
                InstanceProvisioner._code_manifest(terminal_root)
            ),
            "runtime_assets_manifest_sha256": (
                InstanceProvisioner._managed_runtime_assets_manifest(
                    terminal_root
                )
            ),
        },
    )
    before = state_path.read_bytes()
    terminal.write_bytes(b"terminal-v2")

    with pytest.raises(ValueError, match="binding changed"):
        InstanceProvisioner.record_verified_vendor_update(
            root,
            CONNECTION_ID,
            SIGNER,
            expected_terminal_sha256="0" * 64,
            expected_code_manifest_sha256=(
                InstanceProvisioner._code_manifest(terminal_root)
            ),
        )

    assert state_path.read_bytes() == before


def test_live_update_runs_only_staged_verified_copy(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    config = runtime.state / "login-bootstrap.ini"
    config.write_text("Password=investor-secret", encoding="utf-8")
    source = tmp_path / "profile" / "liveupdate" / "terminal64.exe"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"signed-updater")
    payload = source.parent / "mt5onnx64.6090"
    payload.write_bytes(b"signed-vendor-payload")
    unrelated = source.parent / "untrusted.dll"
    unrelated.write_bytes(b"must-not-run")
    candidate = _LiveUpdateCandidate(
        pid=42,
        executable=source,
        arguments=(str(source), "/update"),
    )
    runtime._pending_live_update = candidate
    captured: dict[str, object] = {}

    def capture(bundle: Path, updater: Path, config: Path, signer: str) -> str:
        captured["bundle"] = bundle
        captured["updater"] = updater
        captured["config"] = config
        captured["config_content"] = config.read_text(encoding="utf-8")
        captured["signer"] = signer
        captured["names"] = {path.name for path in bundle.iterdir()}
        return "f" * 64

    promote = Mock(side_effect=capture)
    runtime.set_verified_vendor_update_callback(promote)
    process = Mock(returncode=0)
    process.poll.return_value = 0
    staged_names: set[str] = set()

    def launch(command: list[str], **_kwargs: object) -> Mock:
        working = Path(command[0]).parent
        staged_names.update(path.name for path in working.iterdir())
        config_argument = next(
            argument for argument in command if argument.startswith("/config:")
        )
        launched_config = Path(config_argument.removeprefix("/config:"))
        captured["working_config"] = launched_config
        captured["working_config_content"] = launched_config.read_text(
            encoding="utf-8"
        )
        (working / "mt5onnx64.6090").unlink()
        runtime.terminal.write_bytes(b"terminal-v2")
        return process

    with (
        patch.object(runtime, "_require_elevated_service"),
        patch.object(runtime, "_verify_metaquotes_signature", return_value=SIGNER) as verify,
        patch.object(runtime, "_terminate_candidate") as terminate,
        patch.object(runtime, "stop", return_value=True) as stop,
        patch.object(runtime, "_remove_generated_example_code"),
        patch(
            "windows_agent.worker.native_mt5_runtime.WindowsSecretStore.restrict_acl"
        ),
        patch("subprocess.Popen", side_effect=launch) as popen,
        patch.object(
            InstanceProvisioner,
            "record_verified_vendor_update",
            side_effect=lambda *_args: InstanceProvisioner._sha256(runtime.terminal),
        ) as record,
    ):
        runtime._complete_live_update(config)

    launched = Path(popen.call_args.args[0][0])
    assert launched != source
    assert launched.name == "terminal64.exe"
    assert not launched.exists()
    assert not promote.called
    archived_bundle, archived_updater, archived_config, _ = (
        runtime._pending_verified_vendor_updates[0]
    )
    assert archived_updater.is_file()
    assert (archived_bundle / "mt5onnx64.6090").is_file()
    assert archived_config != config
    assert "Password=" not in archived_config.read_text(encoding="utf-8")
    config.unlink()
    runtime._publish_pending_verified_vendor_updates()
    assert not archived_bundle.exists()
    assert staged_names == {
        "terminal64.exe",
        "mt5onnx64.6090",
        "temp",
        "tradejournal-update.ini",
    }
    assert captured["working_config"] != config
    assert "Password=" not in str(captured["working_config_content"])
    assert not source.exists()
    assert not payload.exists()
    assert unrelated.is_file()
    assert verify.call_args_list[0].args[0] != source
    assert verify.call_args_list[1] == call(runtime.terminal)
    terminate.assert_called_once_with(candidate)
    assert stop.call_count == 2
    record.assert_called_once_with(runtime.root, CONNECTION_ID, SIGNER)
    promote.assert_called_once()
    assert Path(captured["bundle"]).name.startswith("live-update-")
    assert Path(captured["updater"]).name == "terminal64.exe"
    assert Path(captured["config"]).name == "tradejournal-update.ini"
    assert "Password=" not in str(captured["config_content"])
    assert captured["signer"] == SIGNER
    assert captured["names"] == {
        "terminal64.exe",
        "mt5onnx64.6090",
        "temp",
        "tradejournal-update.ini",
        "verified-update.json",
    }


def test_failed_live_update_preserves_staged_bundle_for_startup_replay(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    source_release = Mt5UpdateRelease.from_terminal_root(runtime.terminal_root)
    managed_assets = InstanceProvisioner._managed_runtime_assets_manifest(
        runtime.terminal_root
    )
    atomic_json(
        runtime.state / "instance.json",
        {
            "connection_id": CONNECTION_ID,
            "status": "provisioned",
            "terminal": str(runtime.terminal),
            "terminal_sha256": source_release.terminal_sha256,
            "template_manifest_sha256": "0" * 64,
            "template_code_manifest_sha256": (
                source_release.code_manifest_sha256
            ),
            "runtime_assets_manifest_sha256": managed_assets,
            "runtime_assets_manifest_version": 1,
        },
    )
    config = runtime.state / "login-bootstrap.ini"
    config.write_text("Password=investor-secret", encoding="utf-8")
    source = tmp_path / "profile" / "liveupdate" / "terminal64.exe"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"signed-updater")
    (source.parent / "mt5onnx64.6090").write_bytes(b"vendor-payload")
    candidate = _LiveUpdateCandidate(
        pid=42,
        executable=source,
        arguments=(str(source), "/update"),
    )
    runtime._pending_live_update = candidate
    failed = Mock(returncode=17)
    failed.poll.return_value = 17

    with (
        patch.object(runtime, "_require_elevated_service"),
        patch.object(runtime, "_verify_metaquotes_signature", return_value=SIGNER),
        patch.object(runtime, "_terminate_candidate"),
        patch.object(runtime, "stop", return_value=True),
        patch.object(runtime, "_remove_generated_example_code"),
        patch(
            "windows_agent.worker.native_mt5_runtime.WindowsSecretStore.restrict_acl"
        ),
        patch("subprocess.Popen", return_value=failed),
        pytest.raises(NativeMt5Error, match="mt5_update_failed"),
    ):
        runtime._complete_live_update(config)

    bundles = tuple(runtime.state.glob("live-update-*"))
    assert len(bundles) == 1
    staged = load_verified_update_bundle(bundles[0])
    assert staged.target_release is None
    assert not source.exists()
    assert not tuple(runtime.state.glob(".live-update-working-*"))

    recovered = Mock(returncode=0)
    recovered.poll.return_value = 0

    def replay(*_args: object, **_kwargs: object) -> Mock:
        runtime.terminal.write_bytes(b"terminal-v2")
        return recovered

    with (
        patch.object(runtime, "_verify_metaquotes_signature", return_value=SIGNER),
        patch.object(runtime, "stop", return_value=True),
        patch.object(runtime, "_remove_generated_example_code"),
        patch(
            "windows_agent.worker.native_mt5_runtime.WindowsSecretStore.restrict_acl"
        ),
        patch("subprocess.Popen", side_effect=replay),
    ):
        report = runtime.recover_interrupted_live_updates()

    assert report.sealed_applied == 1
    assert report.pending_health == 1
    assert load_verified_update_bundle(bundles[0]).target_release is not None


def test_live_update_rejects_oversized_payload_before_stopping_terminal(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    config = runtime.state / "login-bootstrap.ini"
    config.write_text("[Common]\n", encoding="utf-8")
    source = tmp_path / "profile" / "liveupdate" / "terminal64.exe"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"signed-updater")
    payload = source.parent / "mt5onnx64.6090"
    with payload.open("wb") as handle:
        handle.truncate(MAX_UPDATE_BUNDLE_FILE_BYTES + 1)
    candidate = _LiveUpdateCandidate(
        pid=42,
        executable=source,
        arguments=(str(source), "/update"),
    )
    runtime._pending_live_update = candidate

    with (
        patch.object(runtime, "_require_elevated_service"),
        patch.object(runtime, "_terminate_candidate") as terminate,
        patch.object(runtime, "stop") as stop,
        patch(
            "windows_agent.worker.native_mt5_runtime.WindowsSecretStore.restrict_acl"
        ),
        pytest.raises(NativeMt5Error, match="mt5_update_source_size_invalid"),
    ):
        runtime._complete_live_update(config)

    terminate.assert_not_called()
    stop.assert_not_called()
    assert source.is_file()
    assert payload.is_file()
    assert not tuple(runtime.state.glob("live-update-*"))
    assert not tuple(runtime.state.glob(".live-update-working-*"))


def test_live_update_checks_free_space_before_copy_or_terminal_stop(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    config = runtime.state / "login-bootstrap.ini"
    config.write_text("[Common]\n", encoding="utf-8")
    source = tmp_path / "profile" / "liveupdate" / "terminal64.exe"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"signed-updater")
    candidate = _LiveUpdateCandidate(
        pid=42,
        executable=source,
        arguments=(str(source), "/update"),
    )
    runtime._pending_live_update = candidate

    with (
        patch.object(runtime, "_require_elevated_service"),
        patch.object(runtime, "_copy_bounded_verified_file") as copy_file,
        patch.object(runtime, "_terminate_candidate") as terminate,
        patch.object(runtime, "stop") as stop,
        patch(
            "windows_agent.worker.native_mt5_runtime.WindowsSecretStore.restrict_acl"
        ),
        patch(
            "windows_agent.worker.native_mt5_runtime.shutil.disk_usage",
            return_value=Mock(free=0),
        ),
        pytest.raises(NativeMt5Error, match="mt5_update_storage_insufficient"),
    ):
        runtime._complete_live_update(config)

    copy_file.assert_not_called()
    terminate.assert_not_called()
    stop.assert_not_called()
    assert source.is_file()
    assert not tuple(runtime.state.glob("live-update-*"))
    assert not tuple(runtime.state.glob(".live-update-working-*"))


def test_startup_preflight_seals_update_after_crash_before_vendor_pin(
    tmp_path: Path,
) -> None:
    runtime, bundle, source_release = _staged_recovery_runtime(tmp_path)
    runtime.terminal.write_bytes(b"terminal-v2")
    target_release = Mt5UpdateRelease.from_terminal_root(runtime.terminal_root)
    process = Mock(returncode=0)
    process.poll.return_value = 0

    with (
        patch.object(
            runtime,
            "_verify_metaquotes_signature",
            return_value=SIGNER,
        ),
        patch.object(runtime, "stop", return_value=True),
        patch.object(runtime, "_remove_generated_example_code"),
        patch(
            "windows_agent.worker.native_mt5_runtime."
            "WindowsSecretStore.restrict_acl"
        ),
        patch("subprocess.Popen", return_value=process),
    ):
        report = runtime.recover_interrupted_live_updates()

    assert report.discarded_staged == 0
    assert report.sealed_applied == 1
    assert report.pending_health == 1
    metadata = load_verified_update_bundle(bundle)
    assert metadata.source_release == source_release
    assert metadata.target_release == target_release
    assert not metadata.health_verified
    state = read_json(runtime.state / "instance.json")
    assert state["terminal_sha256"] == target_release.terminal_sha256
    assert (
        state["template_code_manifest_sha256"]
        == target_release.code_manifest_sha256
    )
    assert state["vendor_update"]["signer_subject"] == SIGNER
    repeated = runtime.recover_interrupted_live_updates()
    assert repeated.sealed_applied == 0
    assert repeated.pending_health == 1


def test_startup_preflight_seals_update_after_crash_after_vendor_pin(
    tmp_path: Path,
) -> None:
    runtime, bundle, _source_release = _staged_recovery_runtime(tmp_path)
    runtime.terminal.write_bytes(b"terminal-v2")
    target_release = Mt5UpdateRelease.from_terminal_root(runtime.terminal_root)
    InstanceProvisioner.record_verified_vendor_update(
        runtime.root,
        CONNECTION_ID,
        SIGNER,
    )

    with (
        patch.object(
            runtime,
            "_verify_metaquotes_signature",
            return_value=SIGNER,
        ),
        patch.object(
            InstanceProvisioner,
            "record_verified_vendor_update",
            side_effect=AssertionError("vendor pin must not be rewritten"),
        ),
    ):
        report = runtime.recover_interrupted_live_updates()

    assert report.sealed_applied == 1
    assert report.pending_health == 1
    assert load_verified_update_bundle(bundle).target_release == target_release


def test_startup_preflight_replays_staged_archive_when_update_never_ran(
    tmp_path: Path,
) -> None:
    runtime, bundle, _source_release = _staged_recovery_runtime(tmp_path)
    process = Mock(returncode=0)
    process.poll.return_value = 0

    def launch(*_args: object, **_kwargs: object) -> Mock:
        runtime.terminal.write_bytes(b"terminal-v2")
        return process

    with (
        patch.object(
            runtime,
            "_verify_metaquotes_signature",
            return_value=SIGNER,
        ),
        patch.object(runtime, "stop", return_value=True),
        patch.object(runtime, "_remove_generated_example_code"),
        patch(
            "windows_agent.worker.native_mt5_runtime."
            "WindowsSecretStore.restrict_acl"
        ),
        patch("subprocess.Popen", side_effect=launch),
    ):
        report = runtime.recover_interrupted_live_updates()

    assert report.discarded_staged == 0
    assert report.sealed_applied == 1
    assert report.pending_health == 1
    assert bundle.exists()
    assert load_verified_update_bundle(bundle).target_release is not None


def test_startup_preflight_never_seals_partial_tree_without_zero_exit(
    tmp_path: Path,
) -> None:
    runtime, bundle, _source_release = _staged_recovery_runtime(tmp_path)
    (runtime.terminal_root / "vendor.dll").write_bytes(b"partial-update")
    process = Mock(returncode=1)
    process.poll.return_value = 1

    with (
        patch.object(
            runtime,
            "_verify_metaquotes_signature",
            return_value=SIGNER,
        ),
        patch(
            "windows_agent.worker.native_mt5_runtime."
            "WindowsSecretStore.restrict_acl"
        ),
        patch("subprocess.Popen", return_value=process),
        pytest.raises(
            NativeMt5Error,
            match="mt5_update_startup_failed",
        ),
    ):
        runtime.recover_interrupted_live_updates()

    metadata = load_verified_update_bundle(bundle)
    assert metadata.target_release is None
    state = read_json(runtime.state / "instance.json")
    assert "vendor_update" not in state


def test_startup_preflight_stops_orphan_updater_before_recovery(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    working = runtime.state / ".live-update-working-crash"
    working.mkdir()
    (working / "terminal64.exe").write_bytes(b"copied-updater")
    calls: list[str] = []

    def terminate(_working_sets: tuple[Path, ...]) -> None:
        calls.append("updater")

    def stop(*_args: object, **_kwargs: object) -> bool:
        calls.append("terminal")
        return True

    with (
        patch.object(
            runtime,
            "_terminate_live_update_working_processes",
            side_effect=terminate,
        ),
        patch.object(runtime, "stop", side_effect=stop),
    ):
        report = runtime.recover_interrupted_live_updates()

    assert report == type(report)()
    assert calls == ["updater", "terminal"]
    assert not working.exists()


def test_startup_preflight_keeps_orphan_working_set_when_stop_fails(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    working = runtime.state / ".live-update-working-crash"
    working.mkdir()
    (working / "terminal64.exe").write_bytes(b"copied-updater")

    with (
        patch.object(runtime, "_terminate_live_update_working_processes"),
        patch.object(runtime, "stop", return_value=False),
        pytest.raises(
            NativeMt5Error,
            match="mt5_update_startup_process_stop_failed",
        ),
    ):
        runtime.recover_interrupted_live_updates()

    assert working.exists()


def test_startup_preflight_terminates_only_exact_working_updater(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    working = runtime.state / ".live-update-working-crash"
    working.mkdir()
    updater = working / "terminal64.exe"
    updater.write_bytes(b"copied-updater")
    process = Mock(info={"pid": 42, "exe": str(updater)})

    with (
        patch(
            "psutil.process_iter",
            side_effect=[[process], []],
        ),
        patch(
            "psutil.wait_procs",
            return_value=([process], []),
        ) as wait_procs,
    ):
        runtime._terminate_live_update_working_processes((working,))

    process.terminate.assert_called_once_with()
    process.kill.assert_not_called()
    wait_procs.assert_called_once_with([process], timeout=15.0)


def test_resume_recovers_applied_orphan_and_required_callback_is_retryable(
    tmp_path: Path,
) -> None:
    first = _runtime(tmp_path)
    orphan = _applied_orphan(first, tmp_path)
    store = Mt5PendingUpdateStore(tmp_path / "pending-updates")
    with pytest.raises(Mt5PendingUpdateStoreError, match="phase"):
        store.capture(orphan)

    def resume(runtime: NativeMt5Runtime) -> NativeMt5Status:
        config = runtime.state / "resume.ini"

        def write_config(*_args: object, **_kwargs: object) -> Path:
            config.write_text("[Common]\nKeepPrivate=1\n", encoding="utf-8")
            return config

        status = NativeMt5Status(
            88,
            {"trade_allowed": False},
            {
                "terminal_connected": True,
                "account_trade_allowed": False,
            },
            runtime.files,
        )
        with (
            patch.object(runtime, "_bridge_template_symbol", return_value="EURUSD"),
            patch.object(runtime, "install_expert"),
            patch.object(runtime, "_remove_readiness_files"),
            patch.object(runtime, "_reset_managed_chart_profile"),
            patch.object(runtime, "_write_startup_config", side_effect=write_config),
            patch.object(runtime, "_start_and_wait_for_authorization"),
            patch.object(runtime, "_wait_for_heartbeat", return_value=status),
            patch.object(runtime, "stop", return_value=True),
        ):
            return runtime.resume(
                login=42,
                server="Demo",
                expert_binary=tmp_path / "unused.ex5",
            )

    first.set_verified_vendor_update_callback(
        Mock(side_effect=RuntimeError("store unavailable")),
        required=True,
    )
    with pytest.raises(NativeMt5Error, match="mt5_update_capture_failed"):
        resume(first)

    assert orphan.is_dir()
    assert load_verified_update_bundle(orphan, require_healthy=True).health_verified
    assert store.pending() == ()

    restarted = NativeMt5Runtime(tmp_path, CONNECTION_ID)
    restarted.set_verified_vendor_update_callback(store.capture, required=True)
    status = resume(restarted)

    assert status.pid == 88
    assert not orphan.exists()
    receipts = store.pending()
    assert len(receipts) == 1
    assert receipts[0].target_release == Mt5UpdateRelease.from_terminal_root(
        restarted.terminal_root
    )


def test_startup_recovery_accepts_duplicate_identical_applied_receipts(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    orphan = _applied_orphan(runtime, tmp_path)
    duplicate = runtime.state / "live-update-crash-duplicate"
    shutil.copytree(orphan, duplicate)
    runtime.set_verified_vendor_update_callback(Mock(), required=True)

    runtime._recover_pending_verified_vendor_updates()

    recovered_roots = [
        bundle_root
        for bundle_root, _updater, _config, _signer
        in runtime._pending_verified_vendor_updates
    ]
    assert len(recovered_roots) == 2
    assert set(recovered_roots) == {orphan, duplicate}
