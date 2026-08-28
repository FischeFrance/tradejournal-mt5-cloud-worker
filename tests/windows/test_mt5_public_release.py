from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from windows_agent.provisioning.mt5_instance import InstanceProvisioner
from windows_agent.provisioning.mt5_public_release import (
    MAX_INSTALLER_BYTES,
    MT5_STABLE_INSTALLER_URL,
    AuthenticodeIdentity,
    DownloadResult,
    HttpsMt5InstallerDownloader,
    Mt5ProvisionedReleaseInventory,
    Mt5PublicReleaseError,
    Mt5PublicReleaseProbe,
    _run_installer,
)


IDENTITY = AuthenticodeIdentity(
    subject="CN=MetaQuotes Ltd.,O=MetaQuotes Ltd.,C=CY",
    common_name="MetaQuotes Ltd.",
    organizations=("MetaQuotes Ltd.",),
    certificate_sha256="a" * 64,
)


def _build_reader(path: Path) -> int:
    match = re.search(rb"build=(\d+)", path.read_bytes())
    if match is None:
        raise Mt5PublicReleaseError("missing test build")
    return int(match.group(1))


class FakeDownloader:
    def __init__(self, payload: bytes = b"installer;build=6140") -> None:
        self.payload = payload
        self.calls = 0

    def __call__(self, destination: Path) -> DownloadResult:
        self.calls += 1
        destination.write_bytes(self.payload)
        return DownloadResult(MT5_STABLE_INSTALLER_URL, len(self.payload))


class QuietProcesses:
    def __init__(self, quiet: bool = True) -> None:
        self.quiet = quiet
        self.snapshots = 0
        self.waits = 0

    def snapshot(self) -> object:
        self.snapshots += 1
        return frozenset()

    def wait_until_quiet(self, before, installer, destination, timeout) -> bool:
        self.waits += 1
        assert before == frozenset()
        assert installer.name == "mt5setup.exe"
        assert destination.name == "terminal"
        assert timeout > 0
        return self.quiet


def _runner(
    *,
    exit_code: int = 1,
    terminal_build: int = 6140,
    create_terminal: bool = True,
):
    def run(installer: Path, destination: Path, timeout: float) -> int:
        assert installer.read_bytes() == b"installer;build=6140"
        assert timeout > 0
        if create_terminal:
            destination.mkdir()
            (destination / "terminal64.exe").write_bytes(
                f"terminal;build={terminal_build}".encode()
            )
            (destination / "MetaEditor64.exe").write_bytes(
                f"editor;build={terminal_build}".encode()
            )
            (destination / "metatester64.exe").write_bytes(
                f"tester;build={terminal_build}".encode()
            )
        return exit_code

    return run


def _probe(
    tmp_path: Path,
    *,
    downloader: FakeDownloader | None = None,
    runner=None,
    processes: QuietProcesses | None = None,
) -> Mt5PublicReleaseProbe:
    return Mt5PublicReleaseProbe(
        tmp_path / "public-release-state",
        downloader=downloader or FakeDownloader(),
        signature_verifier=lambda _path: IDENTITY,
        build_reader=_build_reader,
        installer_runner=runner or _runner(),
        process_guard=processes or QuietProcesses(),
        acl_restrictor=lambda _path: None,
        clock_ms=lambda: 1_788_000_000_000,
    )


def test_exit_one_is_diagnostic_and_verified_postconditions_publish(
    tmp_path: Path,
) -> None:
    downloader = FakeDownloader()
    processes = QuietProcesses()
    probe = _probe(tmp_path, downloader=downloader, processes=processes)

    release = probe.refresh()

    assert release.build == 6140
    assert release.terminal_sha256 == InstanceProvisioner._sha256(
        release.terminal_root / "terminal64.exe"
    )
    assert release.root == probe.releases_root / release.release_id
    assert release.root.is_dir()
    assert probe.load_current() == release
    metadata = json.loads((release.root / "release.json").read_text())
    assert metadata["installer_exit_code"] == 1
    assert metadata["postconditions"] == {
        "process_quiescent": True,
        "terminal_present": True,
        "signatures_valid": True,
        "builds_equal": True,
    }
    assert downloader.calls == 1
    assert processes.snapshots == processes.waits == 1
    assert not tuple(probe.state_root.glob(".public-mt5-*"))


def test_same_release_is_downloaded_and_materialized_again_but_deduplicated(
    tmp_path: Path,
) -> None:
    downloader = FakeDownloader()
    probe = _probe(tmp_path, downloader=downloader)

    first = probe.refresh()
    second = probe.refresh()

    assert first == second
    assert downloader.calls == 2
    assert tuple(path.name for path in probe.releases_root.iterdir()) == (
        first.release_id,
    )


def test_zero_exit_without_terminal_never_publishes(tmp_path: Path) -> None:
    probe = _probe(
        tmp_path,
        runner=_runner(exit_code=0, create_terminal=False),
    )

    with pytest.raises(Mt5PublicReleaseError, match="distribution is incomplete"):
        probe.refresh()

    assert not probe.current_path.exists()
    assert not tuple(probe.releases_root.iterdir())


def test_build_mismatch_never_publishes(tmp_path: Path) -> None:
    probe = _probe(tmp_path, runner=_runner(terminal_build=6139))

    with pytest.raises(Mt5PublicReleaseError, match="builds differ"):
        probe.refresh()

    assert not probe.current_path.exists()


def test_running_installer_descendant_never_publishes_or_deletes_evidence(
    tmp_path: Path,
) -> None:
    probe = _probe(tmp_path, processes=QuietProcesses(quiet=False))

    with pytest.raises(Mt5PublicReleaseError, match="left a process running"):
        probe.refresh()

    assert not probe.current_path.exists()
    assert len(tuple(probe.state_root.glob(".public-mt5-*"))) == 1


def test_non_metaquotes_identity_is_rejected_before_installer_launch(
    tmp_path: Path,
) -> None:
    launched = False

    def runner(*_args):
        nonlocal launched
        launched = True
        return 0

    probe = Mt5PublicReleaseProbe(
        tmp_path / "state",
        downloader=FakeDownloader(),
        signature_verifier=lambda _path: AuthenticodeIdentity(
            subject="CN=MetaQuotes Ltd. Evil,O=MetaQuotes Ltd. Evil",
            common_name="MetaQuotes Ltd. Evil",
            organizations=("MetaQuotes Ltd. Evil",),
            certificate_sha256="b" * 64,
        ),
        build_reader=_build_reader,
        installer_runner=runner,
        process_guard=QuietProcesses(),
        acl_restrictor=lambda _path: None,
    )

    with pytest.raises(Mt5PublicReleaseError, match="signer is not MetaQuotes"):
        probe.refresh()

    assert launched is False


def test_default_runner_uses_auto_and_exact_isolated_path(monkeypatch, tmp_path):
    installer = tmp_path / "mt5setup.exe"
    destination = tmp_path / "path with spaces"
    installer.touch()
    observed = {}

    def run(arguments, **kwargs):
        observed["arguments"] = arguments
        observed["kwargs"] = kwargs
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(subprocess, "run", run)

    assert _run_installer(installer, destination, 12.0) == 1
    assert observed["arguments"] == [
        str(installer),
        "/auto",
        f"/path:{destination}",
    ]
    assert observed["kwargs"]["timeout"] == 12.0
    assert observed["kwargs"]["check"] is False


class FakeResponse:
    def __init__(
        self,
        payload: bytes,
        *,
        final_url: str = MT5_STABLE_INSTALLER_URL,
        declared: str | None = None,
    ) -> None:
        self.payload = payload
        self.offset = 0
        self.status = 200
        self.final_url = final_url
        self.headers = {
            "Content-Encoding": "identity",
            "Content-Length": declared if declared is not None else str(len(payload)),
        }

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def geturl(self):
        return self.final_url

    def read(self, size: int) -> bytes:
        chunk = self.payload[self.offset : self.offset + size]
        self.offset += len(chunk)
        return chunk


class FakeOpener:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.request = None
        self.timeout = None

    def open(self, request, timeout):
        self.request = request
        self.timeout = timeout
        return self.response


def test_downloader_is_fixed_to_official_https_url_and_streams_bounded(
    tmp_path: Path,
) -> None:
    opener = FakeOpener(FakeResponse(b"signed-installer"))
    handlers = []

    def opener_factory(handler):
        handlers.append(handler)
        return opener

    downloader = HttpsMt5InstallerDownloader(
        max_bytes=32,
        timeout=7,
        opener_factory=opener_factory,
    )
    destination = tmp_path / "mt5setup.exe"

    result = downloader(destination)

    assert result == DownloadResult(MT5_STABLE_INSTALLER_URL, 16)
    assert destination.read_bytes() == b"signed-installer"
    assert opener.request.full_url == MT5_STABLE_INSTALLER_URL
    assert opener.request.get_header("Accept-encoding") == "identity"
    assert opener.timeout == 7
    assert len(handlers) == 1


@pytest.mark.parametrize(
    "final_url",
    (
        "http://download.terminal.free/mt5setup.exe",
        "https://evil.example/mt5setup.exe",
        "https://user@download.terminal.free/mt5setup.exe",
    ),
)
def test_downloader_rejects_redirect_outside_https_allowlist(
    tmp_path: Path,
    final_url: str,
) -> None:
    opener = FakeOpener(FakeResponse(b"payload", final_url=final_url))
    downloader = HttpsMt5InstallerDownloader(
        opener_factory=lambda _handler: opener,
    )
    destination = tmp_path / "mt5setup.exe"

    with pytest.raises(Mt5PublicReleaseError, match="redirected unsafely"):
        downloader(destination)

    assert not destination.exists()


def test_downloader_rejects_declared_oversize_without_creating_file(
    tmp_path: Path,
) -> None:
    opener = FakeOpener(
        FakeResponse(b"payload", declared=str(MAX_INSTALLER_BYTES + 1))
    )
    downloader = HttpsMt5InstallerDownloader(
        opener_factory=lambda _handler: opener,
    )
    destination = tmp_path / "mt5setup.exe"

    with pytest.raises(Mt5PublicReleaseError, match="response size is invalid"):
        downloader(destination)

    assert not destination.exists()


def _write_instance(
    instances: Path,
    content: bytes,
    *,
    mutate_after_state: bool = False,
) -> tuple[str, Path]:
    connection_id = str(uuid4())
    root = instances / connection_id
    terminal = root / "terminal" / "terminal64.exe"
    terminal.parent.mkdir(parents=True)
    terminal.write_bytes(content)
    state = {
        "connection_id": connection_id,
        "status": "provisioned",
        "terminal_sha256": InstanceProvisioner._sha256(terminal),
        "template_code_manifest_sha256": InstanceProvisioner._code_manifest(
            terminal.parent
        ),
    }
    state_path = root / "state" / "instance.json"
    state_path.parent.mkdir()
    state_path.write_text(json.dumps(state), encoding="utf-8")
    if mutate_after_state:
        terminal.write_bytes(content + b";tampered")
    return connection_id, root


def test_read_only_inventory_classifies_build_and_integrity(tmp_path: Path) -> None:
    baseline = _probe(tmp_path / "baseline").refresh()
    instances = tmp_path / "instances"
    instances.mkdir()
    old, _ = _write_instance(instances, b"old;build=6139")
    current, current_root = _write_instance(
        instances,
        (baseline.terminal_root / "terminal64.exe").read_bytes(),
    )
    ahead, _ = _write_instance(instances, b"ahead;build=6141")
    divergent, _ = _write_instance(instances, b"other-signed-binary;build=6140")
    tampered, tampered_root = _write_instance(
        instances,
        b"was-valid;build=6140",
        mutate_after_state=True,
    )
    deprovisioned, deprovisioned_root = _write_instance(
        instances,
        b"gone;build=6130",
    )
    missing_state = str(uuid4())
    (instances / missing_state).mkdir()
    deprovisioned_state = deprovisioned_root / "state" / "instance.json"
    value = json.loads(deprovisioned_state.read_text())
    value["status"] = "deprovisioned"
    deprovisioned_state.write_text(json.dumps(value), encoding="utf-8")
    state_before = {
        path: path.read_bytes()
        for path in instances.glob("*/state/instance.json")
    }

    records = Mt5ProvisionedReleaseInventory(
        instances,
        build_reader=_build_reader,
        signature_verifier=lambda _path: IDENTITY,
    ).scan(baseline)
    by_id = {record.connection_id: record for record in records}

    assert by_id[old].classification == "older"
    assert by_id[current].classification == "current"
    assert by_id[ahead].classification == "ahead"
    assert by_id[divergent].classification == "same_build_divergent"
    assert by_id[tampered].classification == "unverifiable"
    assert by_id[tampered].hash_integrity is False
    assert by_id[tampered].failure == "terminal_hash_mismatch"
    assert by_id[missing_state].classification == "unverifiable"
    assert by_id[missing_state].failure == "state_invalid"
    assert deprovisioned not in by_id
    assert by_id[current].root == current_root
    assert by_id[current].build == baseline.build
    assert by_id[current].state_integrity is True
    assert by_id[current].hash_integrity is True
    assert by_id[current].code_integrity is True
    assert {
        path: path.read_bytes()
        for path in instances.glob("*/state/instance.json")
    } == state_before


def _write_trusted_template(root: Path, build: int) -> None:
    root.mkdir(parents=True)
    (root / "terminal64.exe").write_bytes(f"terminal;build={build}".encode())
    (root / "MetaEditor64.exe").write_bytes(f"editor;build={build}".encode())
    (root / "metatester64.exe").write_bytes(f"tester;build={build}".encode())
    for relative, content in (
        (
            Path("MQL5/Experts/TradeJournal/TradeJournalBridge.ex5"),
            b"bridge",
        ),
        (
            Path("MQL5/Scripts/TradeJournal/TradeJournalDiscovery.ex5"),
            b"discovery",
        ),
        (
            Path("MQL5/Scripts/TradeJournal/TradeJournalLoader.ex5"),
            b"loader",
        ),
    ):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


def test_inventory_accepts_only_exact_signed_public_auxiliary_partial(
    tmp_path: Path,
) -> None:
    baseline = _probe(tmp_path / "baseline").refresh()
    trusted = tmp_path / "trusted"
    _write_trusted_template(trusted, 6139)
    instances = tmp_path / "instances"
    instances.mkdir()
    connection_id = str(uuid4())
    root = instances / connection_id
    terminal_root = root / "terminal"
    shutil.copytree(trusted, terminal_root)
    for name in ("MetaEditor64.exe", "metatester64.exe"):
        shutil.copy2(baseline.terminal_root / name, terminal_root / name)
    state_path = root / "state" / "instance.json"
    state_path.parent.mkdir()
    state_path.write_text(
        json.dumps(
            {
                "connection_id": connection_id,
                "status": "provisioned",
                "terminal_sha256": InstanceProvisioner._sha256(
                    trusted / "terminal64.exe"
                ),
                "template_code_manifest_sha256": (
                    InstanceProvisioner._code_manifest(trusted)
                ),
                "runtime_assets_manifest_sha256": (
                    InstanceProvisioner._managed_runtime_assets_manifest(
                        trusted
                    )
                ),
            }
        ),
        encoding="utf-8",
    )
    # A later bridge deployment may legitimately make the current golden's
    # managed assets differ from this older instance.  The instance's own
    # recorded managed-assets digest remains the authority for those files.
    (
        trusted
        / "MQL5/Experts/TradeJournal/TradeJournalBridge.ex5"
    ).write_bytes(b"current-bridge")
    inventory = Mt5ProvisionedReleaseInventory(
        instances,
        build_reader=_build_reader,
        signature_verifier=lambda _path: IDENTITY,
        trusted_template_root=trusted,
    )

    record = inventory.scan(baseline)[0]

    assert record.classification == "older"
    assert record.code_integrity is True
    assert record.state_reseal_required is True
    assert record.verified_partial_signer == IDENTITY.subject
    assert record.actual_code_manifest_sha256 == (
        InstanceProvisioner._code_manifest(terminal_root)
    )

    (terminal_root / "MetaEditor64.exe").write_bytes(b"evil;build=6140")
    blocked = inventory.scan(baseline)[0]
    assert blocked.classification == "unverifiable"
    assert blocked.failure == "code_manifest_mismatch"
    assert blocked.state_reseal_required is False


def test_inventory_rejects_reparse_instances_root(tmp_path: Path) -> None:
    target = tmp_path / "real"
    target.mkdir()
    link = tmp_path / "instances"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    baseline = _probe(tmp_path / "baseline").refresh()

    with pytest.raises(Mt5PublicReleaseError, match="instances root is unsafe"):
        Mt5ProvisionedReleaseInventory(
            link,
            build_reader=_build_reader,
            signature_verifier=lambda _path: IDENTITY,
        ).scan(baseline)
