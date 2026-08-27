from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock, call, patch

import pytest

from windows_agent.provisioning.mt5_instance import (
    InstanceProvisioner,
    _MANAGED_RUNTIME_ASSETS,
)
from windows_agent.state_store import atomic_json, read_json
from windows_agent.worker.native_mt5_runtime import (
    NativeMt5Error,
    NativeMt5Runtime,
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
    update.assert_called_once_with(config)


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
        patch(
            "windows_agent.worker.native_mt5_runtime.WindowsSecretStore.restrict_acl"
        ),
        patch("subprocess.Popen", return_value=process),
        patch.object(InstanceProvisioner, "record_verified_vendor_update"),
    ):
        runtime._complete_live_update(config)

    terminate.assert_called_once_with(running)


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


def test_live_update_runs_only_staged_verified_copy(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    config = runtime.state / "login-bootstrap.ini"
    config.write_text("temporary", encoding="utf-8")
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
    promote = Mock(return_value="f" * 64)
    runtime.set_verified_vendor_update_callback(promote)
    process = Mock(returncode=0)
    process.poll.return_value = 0
    staged_names: set[str] = set()

    def launch(command: list[str], **_kwargs: object) -> Mock:
        staged_names.update(path.name for path in Path(command[0]).parent.iterdir())
        return process

    with (
        patch.object(runtime, "_require_elevated_service"),
        patch.object(runtime, "_verify_metaquotes_signature", return_value=SIGNER) as verify,
        patch.object(runtime, "_terminate_candidate") as terminate,
        patch.object(runtime, "stop", return_value=True) as stop,
        patch(
            "windows_agent.worker.native_mt5_runtime.WindowsSecretStore.restrict_acl"
        ),
        patch("subprocess.Popen", side_effect=launch) as popen,
        patch.object(InstanceProvisioner, "record_verified_vendor_update") as record,
    ):
        runtime._complete_live_update(config)

    launched = Path(popen.call_args.args[0][0])
    assert launched != source
    assert launched.name == "terminal64.exe"
    assert not launched.exists()
    assert staged_names == {"terminal64.exe", "mt5onnx64.6090", "temp"}
    assert not source.exists()
    assert not payload.exists()
    assert unrelated.is_file()
    assert verify.call_args_list[0].args[0] != source
    assert verify.call_args_list[1] == call(runtime.terminal)
    terminate.assert_called_once_with(candidate)
    assert stop.call_count == 2
    record.assert_called_once_with(runtime.root, CONNECTION_ID, SIGNER)
    promote.assert_called_once()
    assert promote.call_args.args[0].name.startswith("live-update-")
    assert promote.call_args.args[1].name == "terminal64.exe"
    assert promote.call_args.args[2] == config
    assert promote.call_args.args[3] == SIGNER
