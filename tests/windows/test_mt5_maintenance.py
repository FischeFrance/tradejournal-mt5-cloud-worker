from __future__ import annotations

import hashlib
from pathlib import Path
from threading import Event, RLock
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest

from windows_agent.mt5_lifecycle import Mt5LifecycleCoordinator
from windows_agent.mt5_maintenance import (
    Mt5CanaryOnlyReport,
    Mt5MaintenanceCoordinator,
    Mt5MaintenanceError,
)
from windows_agent.provisioning.mt5_instance import InstanceProvisioner
from windows_agent.provisioning.mt5_instance_rotation import (
    Mt5FleetRotationReport,
    Mt5TemplateRelease,
)
from windows_agent.provisioning.mt5_public_release import (
    AuthenticodeIdentity,
    Mt5InstanceReleaseInventory,
    Mt5PublicRelease,
)
from windows_agent.provisioning.mt5_template import PreparedMt5Template
from windows_agent.provisioning.mt5_update_store import Mt5UpdateRelease
from windows_agent.state_store import atomic_json
from windows_agent.worker.native_mt5_runtime import NativeMt5Status


class FakeSecrets:
    def __init__(self, values: dict[str, tuple[int, str]]) -> None:
        self.values = values

    def read(self, connection_id: str, name: str) -> str:
        login, server = self.values[connection_id]
        return str(login) if name == "mt5_login" else server


class FakeTemplateManager:
    def __init__(self, terminal: Path) -> None:
        self.source_terminal = terminal
        self.current_sha256 = InstanceProvisioner._sha256(terminal)
        self.immediate_promotions = 0
        self.commits = 0
        self.discards = 0
        self.candidate_root = terminal.parent.parent / "candidate"
        self.candidate_root.mkdir()
        (self.candidate_root / "terminal64.exe").write_bytes(b"terminal-v2")

    def recover_interrupted_rotation(self) -> str:
        return self.current_sha256

    @staticmethod
    def _release(root: Path) -> Mt5UpdateRelease:
        return Mt5UpdateRelease.from_terminal_root(root)

    @property
    def source_release(self) -> Mt5UpdateRelease:
        return self._release(self.source_terminal.parent)

    @property
    def target_release(self) -> Mt5UpdateRelease:
        return self._release(self.candidate_root)

    def promote_verified_update(self, *_args) -> str:
        self.immediate_promotions += 1
        self.source_terminal.write_bytes(b"terminal-v2")
        self.current_sha256 = InstanceProvisioner._sha256(self.source_terminal)
        return self.current_sha256

    def prepare_verified_update(self, *_args, **kwargs) -> PreparedMt5Template:
        source = self.source_release
        target = self.target_release
        assert kwargs["expected_source_terminal_sha256"] == source.terminal_sha256
        assert (
            kwargs["expected_source_code_manifest_sha256"]
            == source.code_manifest_sha256
        )
        assert kwargs["expected_target_terminal_sha256"] == target.terminal_sha256
        assert (
            kwargs["expected_target_code_manifest_sha256"]
            == target.code_manifest_sha256
        )
        kwargs["cancel_check"]()
        return PreparedMt5Template(
            self.candidate_root,
            source.terminal_sha256,
            source.code_manifest_sha256,
            target.terminal_sha256,
            target.code_manifest_sha256,
            "a" * 64,
            "CN=MetaQuotes Ltd., O=MetaQuotes Ltd.",
        )

    def prepare_verified_distribution(
        self,
        distribution_root: Path,
        **kwargs: object,
    ) -> PreparedMt5Template:
        target = self.target_release
        assert Path(distribution_root) == self.candidate_root
        assert kwargs["expected_terminal_sha256"] == target.terminal_sha256
        assert kwargs["expected_distribution_manifest_sha256"] == (
            InstanceProvisioner._tree_manifest(self.candidate_root)
        )
        cancel = kwargs["cancel_check"]
        assert callable(cancel)
        cancel()
        source = self.source_release
        return PreparedMt5Template(
            self.candidate_root,
            source.terminal_sha256,
            source.code_manifest_sha256,
            target.terminal_sha256,
            target.code_manifest_sha256,
            "a" * 64,
            "CN=MetaQuotes Ltd., O=MetaQuotes Ltd.",
        )

    def commit_prepared_update(self, prepared: PreparedMt5Template) -> str:
        self.commits += 1
        self.source_terminal.write_bytes(
            (prepared.root / "terminal64.exe").read_bytes()
        )
        self.current_sha256 = InstanceProvisioner._sha256(self.source_terminal)
        return self.current_sha256

    def discard_prepared_update(self, _prepared: PreparedMt5Template) -> None:
        self.discards += 1

    @property
    def promotions(self) -> int:
        return self.immediate_promotions + self.commits


class FakePendingStore:
    def __init__(self, manager: FakeTemplateManager) -> None:
        self.manager = manager
        self.receipts: list[SimpleNamespace] = []
        self.captures = 0
        self.quarantined: list[str] = []

    def capture(self, bundle_root: Path, updater: Path, config: Path, signer: str):
        self.captures += 1
        receipt = SimpleNamespace(
            root=bundle_root,
            receipt_id="b" * 64,
            source_release=self.manager.source_release,
            target_release=self.manager.target_release,
            signer_subject=signer,
            updater=updater,
            updater_config=config,
        )
        if not self.receipts:
            self.receipts.append(receipt)
        return receipt

    def pending(self):
        return tuple(self.receipts)

    def complete(self, receipt_id: str) -> None:
        self.receipts = [
            receipt
            for receipt in self.receipts
            if receipt.receipt_id != receipt_id
        ]

    def quarantine(self, receipt_id: str) -> None:
        self.quarantined.append(receipt_id)
        self.complete(receipt_id)


class FakePool:
    target_size = 2

    def __init__(self) -> None:
        self.calls: list[tuple[str, bool]] = []
        self.ready = 0

    def accept_template_rotation(
        self,
        digest: str,
        *,
        replenish: bool,
        stop_event: Event,
    ) -> int:
        assert not stop_event.is_set()
        self.calls.append((digest, replenish))
        self.ready = self.target_size
        return 2 if len(self.calls) == 1 else 0

    def ready_count(self) -> int:
        return self.ready


class FakeProcess:
    adopted: list[Path] = []

    def __init__(self, _state: Path) -> None:
        pass

    def adopt(self, terminal: Path) -> int:
        self.adopted.append(terminal)
        return 88


class FakeRotator:
    def __init__(
        self,
        instances_root: Path,
        secrets: FakeSecrets,
        *,
        recovery_failed: tuple[str, ...] = (),
    ) -> None:
        self.instances_root = instances_root
        self.secrets = secrets
        self.recovery_failed = recovery_failed
        self.current: set[str] = set()
        self.rotate_one_calls: list[tuple[str, bool]] = []
        self.rotate_one_history_from: list[object] = []
        self.rotate_all_calls: list[Mt5TemplateRelease] = []
        self.candidate_sources: list[Path] = []
        self.candidate_fail = False
        self.candidate_fail_after: int | None = None
        self.candidate_calls = 0

    def _instance_root(self, connection_id: str) -> Path:
        return self.instances_root / connection_id

    def matches_target(self, connection_id: str, _target: Mt5TemplateRelease) -> bool:
        return connection_id in self.current

    def recover_incomplete(self, **_kwargs):
        return SimpleNamespace(recovered=(), failed=self.recovery_failed)

    def rotate_one(
        self,
        connection_id: str,
        _target: Mt5TemplateRelease,
        *,
        force: bool = False,
        **_kwargs,
    ) -> bool:
        self.rotate_one_history_from.append(_kwargs.get("history_from"))
        if self.candidate_sources:
            self.candidate_calls += 1
            if self.candidate_fail or (
                self.candidate_fail_after is not None
                and self.candidate_calls == self.candidate_fail_after
            ):
                raise RuntimeError("candidate failed")
        self.rotate_one_calls.append((connection_id, force))
        self.current.add(connection_id)
        return True

    def for_source_terminal(self, source_terminal: Path):
        self.candidate_sources.append(source_terminal)
        return self

    def rotate_all(
        self,
        target: Mt5TemplateRelease,
        _stop_event: Event,
        **_kwargs,
    ):
        self.rotate_all_calls.append(target)
        provisioned = tuple(
            child.name for child in self.instances_root.iterdir() if child.is_dir()
        )
        migrated = tuple(cid for cid in provisioned if cid not in self.current)
        self.current.update(provisioned)
        return Mt5FleetRotationReport(target, migrated, ())


class RuntimeController:
    def __init__(
        self, *, publish_update: bool = False, fail_resume: bool = False
    ) -> None:
        self.publish_update = publish_update
        self.fail_resume = fail_resume
        self.callbacks: list[tuple[object, bool]] = []
        self.cancel_checks: list[object] = []
        self.factory_calls: list[tuple[Path, str]] = []
        self.resume_calls: list[dict] = []
        self.stop_calls = 0

    def factory(self, root: Path, connection_id: str):
        owner = self
        owner.factory_calls.append((root, connection_id))

        class Runtime:
            def __init__(self) -> None:
                self.callback = None

            def set_verified_vendor_update_callback(self, callback, *, required: bool):
                self.callback = callback
                owner.callbacks.append((callback, required))

            def set_cancel_check(self, check):
                owner.cancel_checks.append(check)

            def stop(self) -> bool:
                owner.stop_calls += 1
                return True

            def resume(self, **kwargs):
                owner.resume_calls.append(kwargs)
                if owner.fail_resume:
                    raise RuntimeError("probe failed")
                if owner.publish_update:
                    owner.publish_update = False
                    assert self.callback is not None
                    self.callback(
                        root / "bundle",
                        root / "bundle" / "terminal64.exe",
                        root / "state" / "resume.ini",
                        "CN=MetaQuotes Ltd., O=MetaQuotes Ltd.",
                    )
                return NativeMt5Status(
                    88,
                    {"trade_allowed": False},
                    {
                        "terminal_connected": True,
                        "account_trade_allowed": False,
                    },
                    root / "terminal" / "MQL5" / "Files" / "TradeJournal",
                )

        return Runtime()


def _fixture(tmp_path: Path, servers: tuple[str, ...] = ("Broker-A",)):
    template = tmp_path / "template" / "terminal64.exe"
    template.parent.mkdir()
    template.write_bytes(b"terminal-v1")
    expert = tmp_path / "expert.ex5"
    expert.write_bytes(b"expert")
    instances = tmp_path / "instances"
    instances.mkdir()
    values: dict[str, tuple[int, str]] = {}
    ids: list[str] = []
    for index, server in enumerate(servers, start=1):
        connection_id = str(uuid4())
        ids.append(connection_id)
        root = instances / connection_id
        (root / "state").mkdir(parents=True)
        (root / "terminal").mkdir()
        instance_terminal = root / "terminal" / "terminal64.exe"
        instance_terminal.write_bytes(b"instance")
        terminal_sha256 = InstanceProvisioner._sha256(instance_terminal)
        code_manifest_sha256 = InstanceProvisioner._code_manifest(
            instance_terminal.parent
        )
        release_id = hashlib.sha256(
            f"{terminal_sha256}:{code_manifest_sha256}".encode("ascii")
        ).hexdigest()
        atomic_json(
            root / "state" / "instance.json",
            {
                "connection_id": connection_id,
                "status": "provisioned",
                "terminal_sha256": terminal_sha256,
                "template_code_manifest_sha256": code_manifest_sha256,
                "template_release_id": release_id,
            },
        )
        values[connection_id] = (40 + index, server)
    secrets = FakeSecrets(values)
    manager = FakeTemplateManager(template)
    rotator = FakeRotator(instances, secrets)
    return ids, instances, expert, manager, rotator, secrets


def _coordinator(
    instances: Path,
    expert: Path,
    manager: FakeTemplateManager,
    rotator: FakeRotator,
    secrets: FakeSecrets,
    runtime: RuntimeController,
    pool: FakePool,
    pending_store: FakePendingStore | None = None,
    public_probe: object | None = None,
    public_inventory: object | None = None,
) -> Mt5MaintenanceCoordinator:
    lifecycle = Mt5LifecycleCoordinator()
    return Mt5MaintenanceCoordinator(
        instances_root=instances,
        expert_binary=expert,
        template_manager=manager,  # type: ignore[arg-type]
        rotator=rotator,  # type: ignore[arg-type]
        lifecycle=lifecycle,
        template_lock=RLock(),
        instance_pool=pool,  # type: ignore[arg-type]
        pending_update_store=pending_store,  # type: ignore[arg-type]
        runtime_factory=runtime.factory,
        process_factory=FakeProcess,
        secret_store=secrets,
        public_release_probe=public_probe,
        public_release_inventory=public_inventory,
    )


def _public_release(manager: FakeTemplateManager) -> Mt5PublicRelease:
    target = manager.target_release
    signer = AuthenticodeIdentity(
        "CN=MetaQuotes Ltd., O=MetaQuotes Ltd.",
        "MetaQuotes Ltd.",
        ("MetaQuotes Ltd.",),
        "d" * 64,
    )
    return Mt5PublicRelease(
        release_id="e" * 64,
        root=manager.candidate_root,
        installer=manager.candidate_root / "mt5setup.exe",
        terminal_root=manager.candidate_root,
        build=6140,
        installer_sha256="f" * 64,
        terminal_sha256=target.terminal_sha256,
        code_manifest_sha256=target.code_manifest_sha256,
        distribution_manifest_sha256=InstanceProvisioner._tree_manifest(
            manager.candidate_root
        ),
        installer_signer=signer,
        terminal_signer=signer,
        published_at_unix_ms=1,
    )


class FakePublicProbe:
    def __init__(self, release: Mt5PublicRelease) -> None:
        self.release = release
        self.refresh_calls = 0
        self.build_reader = self._build

    @staticmethod
    def _build(path: Path) -> int:
        return 6140 if Path(path).read_bytes() == b"terminal-v2" else 6090

    def refresh(self) -> Mt5PublicRelease:
        self.refresh_calls += 1
        return self.release

    def load_current(self) -> Mt5PublicRelease:
        return self.release


class FakePublicInventory:
    def __init__(self, instances: Path, rotator: FakeRotator) -> None:
        self.instances = instances
        self.rotator = rotator
        self.calls = 0

    def scan(
        self,
        baseline: Mt5PublicRelease,
    ) -> tuple[Mt5InstanceReleaseInventory, ...]:
        self.calls += 1
        records: list[Mt5InstanceReleaseInventory] = []
        for root in sorted(self.instances.iterdir(), key=lambda value: value.name):
            connection_id = root.name
            is_current = connection_id in self.rotator.current
            records.append(
                Mt5InstanceReleaseInventory(
                    connection_id=connection_id,
                    root=root,
                    build=baseline.build if is_current else 6090,
                    terminal_sha256=(
                        baseline.terminal_sha256
                        if is_current
                        else "1" * 64
                    ),
                    recorded_terminal_sha256=(
                        baseline.terminal_sha256
                        if is_current
                        else "1" * 64
                    ),
                    state_integrity=True,
                    hash_integrity=True,
                    code_integrity=True,
                    signature_valid=True,
                    classification="current" if is_current else "older",
                    failure=None,
                )
            )
        return tuple(records)


def test_canary_only_targets_exact_fpm_account_without_shared_mutations(
    tmp_path: Path,
) -> None:
    ids, instances, expert, manager, rotator, secrets = _fixture(
        tmp_path,
        ("FPMTrading-Live", "FPMTrading-Live", "Other-Broker"),
    )
    runtime = RuntimeController()
    pool = FakePool()
    pending_store = FakePendingStore(manager)
    coordinator = _coordinator(
        instances,
        expert,
        manager,
        rotator,
        secrets,
        runtime,
        pool,
        pending_store,
    )
    golden_before = manager.source_terminal.read_bytes()

    report = coordinator.run_canary_only(
        ids[1],
        "fpmtrading-live",
        Event(),
    )

    assert report == Mt5CanaryOnlyReport(ids[1], "FPMTrading-Live", ())
    assert report.update_captured is False
    assert runtime.factory_calls == [(instances / ids[1], ids[1])]
    assert len(runtime.resume_calls) == 1
    assert runtime.resume_calls[0]["server"] == "FPMTrading-Live"
    assert runtime.resume_calls[0]["history_mode"] == "new_only"
    assert runtime.resume_calls[0]["history_from"] is not None
    assert runtime.stop_calls == 1
    assert manager.source_terminal.read_bytes() == golden_before
    assert manager.promotions == 0
    assert manager.discards == 0
    assert pool.calls == []
    assert rotator.rotate_one_calls == []
    assert rotator.rotate_all_calls == []


def test_canary_only_captures_pending_update_without_consuming_or_promoting(
    tmp_path: Path,
) -> None:
    ids, instances, expert, manager, rotator, secrets = _fixture(
        tmp_path,
        ("FPMTrading-Live", "Other-Broker"),
    )
    runtime = RuntimeController(publish_update=True)
    pool = FakePool()
    pending_store = FakePendingStore(manager)
    coordinator = _coordinator(
        instances,
        expert,
        manager,
        rotator,
        secrets,
        runtime,
        pool,
        pending_store,
    )
    golden_before = manager.source_terminal.read_bytes()

    report = coordinator.run_canary_only(
        ids[0],
        "FPMTrading-Live",
        Event(),
    )

    assert report == Mt5CanaryOnlyReport(
        ids[0],
        "FPMTrading-Live",
        ("b" * 64,),
    )
    assert report.update_captured is True
    assert pending_store.captures == 1
    assert tuple(receipt.receipt_id for receipt in pending_store.pending()) == (
        "b" * 64,
    )
    assert pending_store.quarantined == []
    assert manager.source_terminal.read_bytes() == golden_before
    assert manager.promotions == 0
    assert manager.discards == 0
    assert pool.calls == []
    assert rotator.rotate_one_calls == []
    assert rotator.rotate_all_calls == []


def test_canary_only_requires_pending_store_before_stopping_terminal(
    tmp_path: Path,
) -> None:
    ids, instances, expert, manager, rotator, secrets = _fixture(
        tmp_path,
        ("FPMTrading-Live",),
    )
    runtime = RuntimeController(publish_update=True)
    pool = FakePool()
    coordinator = _coordinator(
        instances,
        expert,
        manager,
        rotator,
        secrets,
        runtime,
        pool,
    )

    with pytest.raises(Mt5MaintenanceError, match="pending update store is disabled"):
        coordinator.run_canary_only(
            ids[0],
            "FPMTrading-Live",
            Event(),
        )

    assert runtime.factory_calls == []
    assert runtime.stop_calls == 0
    assert manager.promotions == 0
    assert pool.calls == []
    assert rotator.rotate_one_calls == []
    assert rotator.rotate_all_calls == []


def test_canary_only_rejects_wrong_server_before_stopping_terminal(
    tmp_path: Path,
) -> None:
    ids, instances, expert, manager, rotator, secrets = _fixture(
        tmp_path,
        ("Other-Broker",),
    )
    runtime = RuntimeController()
    pool = FakePool()
    coordinator = _coordinator(
        instances,
        expert,
        manager,
        rotator,
        secrets,
        runtime,
        pool,
        FakePendingStore(manager),
    )

    with pytest.raises(Mt5MaintenanceError, match="server does not match"):
        coordinator.run_canary_only(
            ids[0],
            "FPMTrading-Live",
            Event(),
        )

    assert runtime.factory_calls == []
    assert runtime.stop_calls == 0
    assert manager.promotions == 0
    assert pool.calls == []
    assert rotator.rotate_one_calls == []
    assert rotator.rotate_all_calls == []


def test_canary_only_rejects_noncanonical_connection_before_mutation(
    tmp_path: Path,
) -> None:
    _ids, instances, expert, manager, rotator, secrets = _fixture(
        tmp_path,
        ("FPMTrading-Live",),
    )
    runtime = RuntimeController()
    pool = FakePool()
    coordinator = _coordinator(
        instances,
        expert,
        manager,
        rotator,
        secrets,
        runtime,
        pool,
        FakePendingStore(manager),
    )

    with pytest.raises(Mt5MaintenanceError, match="connection is invalid"):
        coordinator.run_canary_only(
            "NOT-A-CANONICAL-UUID",
            "FPMTrading-Live",
            Event(),
        )

    assert runtime.factory_calls == []
    assert runtime.stop_calls == 0
    assert manager.promotions == 0
    assert pool.calls == []
    assert rotator.rotate_one_calls == []
    assert rotator.rotate_all_calls == []


def test_canary_only_rejects_pre_cancelled_request_without_mutation(
    tmp_path: Path,
) -> None:
    ids, instances, expert, manager, rotator, secrets = _fixture(
        tmp_path,
        ("FPMTrading-Live",),
    )
    runtime = RuntimeController()
    pool = FakePool()
    coordinator = _coordinator(
        instances,
        expert,
        manager,
        rotator,
        secrets,
        runtime,
        pool,
        FakePendingStore(manager),
    )
    stop_event = Event()
    stop_event.set()

    with pytest.raises(Mt5MaintenanceError, match="was interrupted"):
        coordinator.run_canary_only(
            ids[0],
            "FPMTrading-Live",
            stop_event,
        )

    assert runtime.factory_calls == []
    assert runtime.stop_calls == 0
    assert manager.promotions == 0
    assert pool.calls == []
    assert rotator.rotate_one_calls == []
    assert rotator.rotate_all_calls == []


def test_canary_only_cursor_failure_does_not_attempt_recovery_with_unbound_state(
    tmp_path: Path,
) -> None:
    ids, instances, expert, manager, rotator, secrets = _fixture(
        tmp_path,
        ("FPMTrading-Live",),
    )
    runtime = RuntimeController()
    pool = FakePool()
    coordinator = _coordinator(
        instances,
        expert,
        manager,
        rotator,
        secrets,
        runtime,
        pool,
        FakePendingStore(manager),
    )

    with (
        patch(
            "windows_agent.mt5_maintenance.new_only_recovery_from",
            side_effect=OSError("unavailable"),
        ),
        pytest.raises(Mt5MaintenanceError, match="cursor is unavailable"),
    ):
        coordinator.run_canary_only(
            ids[0],
            "FPMTrading-Live",
            Event(),
        )

    assert runtime.stop_calls == 0
    assert rotator.rotate_one_calls == []
    assert rotator.rotate_all_calls == []
    assert manager.promotions == 0
    assert pool.calls == []


def test_failed_canary_only_probe_restores_only_requested_account(
    tmp_path: Path,
) -> None:
    ids, instances, expert, manager, rotator, secrets = _fixture(
        tmp_path,
        ("FPMTrading-Live", "Other-Broker"),
    )
    runtime = RuntimeController(fail_resume=True)
    pool = FakePool()
    coordinator = _coordinator(
        instances,
        expert,
        manager,
        rotator,
        secrets,
        runtime,
        pool,
        FakePendingStore(manager),
    )

    with pytest.raises(Mt5MaintenanceError, match="was recovered"):
        coordinator.run_canary_only(
            ids[0],
            "FPMTrading-Live",
            Event(),
        )

    assert runtime.factory_calls == [(instances / ids[0], ids[0])]
    assert runtime.stop_calls == 2
    assert rotator.rotate_one_calls == [(ids[0], True)]
    assert rotator.rotate_one_history_from[-1] == runtime.resume_calls[0][
        "history_from"
    ]
    assert ids[1] not in rotator.current
    assert manager.promotions == 0
    assert pool.calls == []
    assert rotator.rotate_all_calls == []


def test_public_canary_refreshes_baseline_and_updates_only_fpm(
    tmp_path: Path,
) -> None:
    ids, instances, expert, manager, rotator, secrets = _fixture(
        tmp_path,
        ("FPMTrading-Live", "FPMTrading-Live", "Other-Broker"),
    )
    public = _public_release(manager)
    public_probe = FakePublicProbe(public)
    public_inventory = FakePublicInventory(instances, rotator)
    pool = FakePool()
    coordinator = _coordinator(
        instances,
        expert,
        manager,
        rotator,
        secrets,
        RuntimeController(),
        pool,
        FakePendingStore(manager),
        public_probe,
        public_inventory,
    )

    with patch.object(
        InstanceProvisioner,
        "record_verified_vendor_update",
        return_value="a" * 64,
    ) as seal:
        report = coordinator.run_public_canary_only(
            ids[1],
            "FPMTrading-Live",
            Event(),
        )

    assert public_probe.refresh_calls == 1
    assert report.updated is True
    assert report.public_build == 6140
    assert report.observed_build_before == 6090
    assert report.observed_build_after == 6140
    assert report.classification_before == "older"
    assert report.classification_after == "current"
    assert report.inventory_counts == (
        ("older", 3),
        ("current", 0),
        ("ahead", 0),
        ("same_build_divergent", 0),
        ("unverifiable", 0),
    )
    assert rotator.rotate_one_calls == [(ids[1], True)]
    assert ids[0] not in rotator.current
    assert ids[2] not in rotator.current
    assert manager.commits == 0
    assert manager.discards == 1
    assert pool.calls == []
    assert rotator.rotate_all_calls == []
    seal.assert_called_once()


def test_nightly_public_baseline_promotes_pool_then_only_older_instances(
    tmp_path: Path,
) -> None:
    ids, instances, expert, manager, rotator, secrets = _fixture(
        tmp_path,
        ("Broker-A", "Broker-A", "Broker-B"),
    )
    public = _public_release(manager)
    public_probe = FakePublicProbe(public)
    public_inventory = FakePublicInventory(instances, rotator)
    pool = FakePool()
    coordinator = _coordinator(
        instances,
        expert,
        manager,
        rotator,
        secrets,
        RuntimeController(),
        pool,
        None,
        public_probe,
        public_inventory,
    )

    report = coordinator.run_once(Event())

    assert public_probe.refresh_calls == 1
    assert manager.commits == 1
    assert manager.source_terminal.read_bytes() == b"terminal-v2"
    assert pool.calls == [(report.current_release.terminal_sha256, True)]
    assert rotator.rotate_all_calls == []
    assert rotator.current == set(ids)
    assert len(report.checked_connections) == 2
    assert len(report.migrated_connections) == 1
    assert report.release_changed is True


def test_new_release_is_verified_then_rebuilds_pool_and_rotates_fleet(
    tmp_path: Path,
) -> None:
    ids, instances, expert, manager, rotator, secrets = _fixture(
        tmp_path,
        ("Broker-A", "Broker-A", "Broker-B"),
    )
    rotator.current.add(ids[1])
    runtime = RuntimeController(publish_update=True)
    pool = FakePool()
    pending_store = FakePendingStore(manager)
    coordinator = _coordinator(
        instances,
        expert,
        manager,
        rotator,
        secrets,
        runtime,
        pool,
        pending_store,
    )

    report = coordinator.run_once(Event())

    assert report.release_changed is True
    assert len(report.checked_connections) == 2
    assert ids[1] in report.checked_connections
    assert manager.immediate_promotions == 0
    assert manager.commits == 1
    assert pending_store.captures == 1
    assert pending_store.pending() == ()
    assert all(required is True for _callback, required in runtime.callbacks)
    assert all(call["history_mode"] == "new_only" for call in runtime.resume_calls)
    assert all(call["history_from"] is not None for call in runtime.resume_calls)
    assert len(pool.calls) == 1
    assert all(replenish is True for _digest, replenish in pool.calls)
    assert rotator.rotate_all_calls == [report.current_release]
    assert report.ready_pool_slots == pool.target_size


def test_no_release_does_not_promote_but_still_checks_pool_postcondition(
    tmp_path: Path,
) -> None:
    ids, instances, expert, manager, rotator, secrets = _fixture(tmp_path)
    rotator.current.add(ids[0])
    runtime = RuntimeController()
    pool = FakePool()
    coordinator = _coordinator(
        instances, expert, manager, rotator, secrets, runtime, pool
    )

    report = coordinator.run_once(Event())

    assert report.release_changed is False
    assert manager.promotions == 0
    assert len(pool.calls) == 1
    assert report.migrated_connections == ()


def test_bad_canary_identity_does_not_hide_other_healthy_canaries(
    tmp_path: Path,
) -> None:
    ids, instances, expert, manager, rotator, secrets = _fixture(
        tmp_path,
        ("Broken-Broker", "Healthy-Broker"),
    )

    class PartiallyUnavailableSecrets(FakeSecrets):
        def read(self, connection_id: str, name: str) -> str:
            if connection_id == ids[0]:
                raise RuntimeError("secret unavailable")
            return super().read(connection_id, name)

    partial = PartiallyUnavailableSecrets(secrets.values)
    coordinator = _coordinator(
        instances,
        expert,
        manager,
        rotator,
        partial,
        RuntimeController(),
        FakePool(),
    )

    canaries = coordinator._canaries(coordinator._current_release())

    assert [canary.connection_id for canary in canaries] == [ids[1]]


def test_stale_receipt_after_managed_bridge_deploy_is_quarantined(
    tmp_path: Path,
) -> None:
    ids, instances, expert, manager, rotator, secrets = _fixture(tmp_path)
    rotator.current.update(ids)
    runtime = RuntimeController()
    pool = FakePool()
    pending_store = FakePendingStore(manager)
    receipt_id = "c" * 64
    pending_store.receipts.append(
        SimpleNamespace(
            root=tmp_path / "stale",
            receipt_id=receipt_id,
            source_release=Mt5UpdateRelease(
                manager.source_release.terminal_sha256,
                "d" * 64,
                "e" * 64,
            ),
            target_release=manager.target_release,
            signer_subject="CN=MetaQuotes Ltd., O=MetaQuotes Ltd.",
            updater=tmp_path / "stale" / "terminal64.exe",
            updater_config=tmp_path / "stale" / "tradejournal-update.ini",
        )
    )
    coordinator = _coordinator(
        instances,
        expert,
        manager,
        rotator,
        secrets,
        runtime,
        pool,
        pending_store,
    )

    report = coordinator.run_once(Event())

    assert report.release_changed is False
    assert pending_store.quarantined == [receipt_id]
    assert pending_store.pending() == ()
    assert manager.commits == 0


def test_pending_release_chain_is_selected_in_order_without_quarantine(
    tmp_path: Path,
) -> None:
    _ids, instances, expert, manager, rotator, secrets = _fixture(tmp_path)
    pending_store = FakePendingStore(manager)
    runtime = RuntimeController()
    coordinator = _coordinator(
        instances,
        expert,
        manager,
        rotator,
        secrets,
        runtime,
        FakePool(),
        pending_store,
    )
    release_a = Mt5TemplateRelease("a" * 64, "1" * 64, "4" * 64)
    release_b = Mt5TemplateRelease("b" * 64, "2" * 64, "5" * 64)
    release_c = Mt5TemplateRelease("c" * 64, "3" * 64, "6" * 64)
    edge_ab = SimpleNamespace(
        receipt_id="a" * 64,
        source_release=coordinator._store_release(release_a),
        target_release=coordinator._store_release(release_b),
    )
    edge_bc = SimpleNamespace(
        receipt_id="b" * 64,
        source_release=coordinator._store_release(release_b),
        target_release=coordinator._store_release(release_c),
    )
    pending_store.receipts[:] = [edge_ab, edge_bc]

    assert coordinator._select_pending_receipt(release_a) is edge_ab
    assert pending_store.quarantined == []
    pending_store.complete(edge_ab.receipt_id)
    assert coordinator._select_pending_receipt(release_b) is edge_bc
    assert pending_store.quarantined == []


def test_pending_self_loop_is_quarantined_without_hiding_real_edge(
    tmp_path: Path,
) -> None:
    _ids, instances, expert, manager, rotator, secrets = _fixture(tmp_path)
    pending_store = FakePendingStore(manager)
    coordinator = _coordinator(
        instances,
        expert,
        manager,
        rotator,
        secrets,
        RuntimeController(),
        FakePool(),
        pending_store,
    )
    current = coordinator._current_release()
    source = coordinator._store_release(current)
    loop = SimpleNamespace(
        receipt_id="0" * 64,
        source_release=source,
        target_release=source,
    )
    edge = SimpleNamespace(
        receipt_id="1" * 64,
        source_release=source,
        target_release=manager.target_release,
    )
    pending_store.receipts[:] = [loop, edge]

    assert coordinator._pending_receipt_chain(current) == (edge,)
    assert pending_store.quarantined == [loop.receipt_id]


def test_release_chain_is_composed_and_committed_only_once(
    tmp_path: Path,
) -> None:
    ids, instances, expert, manager, rotator, secrets = _fixture(tmp_path)
    rotator.current.update(ids)
    pending_store = FakePendingStore(manager)
    pool = FakePool()
    coordinator = _coordinator(
        instances,
        expert,
        manager,
        rotator,
        secrets,
        RuntimeController(),
        pool,
        pending_store,
    )
    release_a = manager.source_release
    release_b = manager.target_release
    final_root = tmp_path / "candidate-final"
    final_root.mkdir()
    (final_root / "terminal64.exe").write_bytes(b"terminal-v3")
    release_c = Mt5UpdateRelease.from_terminal_root(final_root)
    edge_ab = SimpleNamespace(
        root=tmp_path / "edge-ab",
        receipt_id="a" * 64,
        source_release=release_a,
        target_release=release_b,
        signer_subject="CN=MetaQuotes Ltd., O=MetaQuotes Ltd.",
        updater=tmp_path / "edge-ab" / "terminal64.exe",
        updater_config=tmp_path / "edge-ab" / "tradejournal-update.ini",
    )
    edge_bc = SimpleNamespace(
        root=tmp_path / "edge-bc",
        receipt_id="b" * 64,
        source_release=release_b,
        target_release=release_c,
        signer_subject="CN=MetaQuotes Ltd., O=MetaQuotes Ltd.",
        updater=tmp_path / "edge-bc" / "terminal64.exe",
        updater_config=tmp_path / "edge-bc" / "tradejournal-update.ini",
    )
    pending_store.receipts[:] = [edge_ab, edge_bc]

    def advance(
        prepared: PreparedMt5Template,
        *_args: object,
        **kwargs: object,
    ) -> PreparedMt5Template:
        assert kwargs["expected_source_terminal_sha256"] == (
            release_b.terminal_sha256
        )
        assert kwargs["expected_target_terminal_sha256"] == (
            release_c.terminal_sha256
        )
        (prepared.root / "terminal64.exe").write_bytes(b"terminal-v3")
        return PreparedMt5Template(
            prepared.root,
            prepared.source_terminal_sha256,
            prepared.source_code_manifest_sha256,
            release_c.terminal_sha256,
            release_c.code_manifest_sha256,
            "c" * 64,
            edge_bc.signer_subject,
        )

    with patch.object(
        manager,
        "advance_prepared_update",
        side_effect=advance,
        create=True,
    ) as advance_update:
        current, _discarded, synchronized = (
            coordinator._promote_pending_updates(
                coordinator._canaries(coordinator._current_release()),
                Event(),
            )
        )

    assert current.terminal_sha256 == release_c.terminal_sha256
    assert manager.commits == 1
    assert advance_update.call_count == 1
    assert len(pool.calls) == 1
    assert synchronized is True
    assert pending_store.pending() == ()


def test_canary_already_on_final_chain_target_is_not_downgraded(
    tmp_path: Path,
) -> None:
    ids, instances, expert, manager, rotator, secrets = _fixture(tmp_path)
    pending_store = FakePendingStore(manager)
    coordinator = _coordinator(
        instances,
        expert,
        manager,
        rotator,
        secrets,
        RuntimeController(),
        FakePool(),
        pending_store,
    )
    release_a = manager.source_release
    release_b = manager.target_release
    release_c = Mt5UpdateRelease("c" * 64, "d" * 64, "e" * 64)
    pending_store.receipts[:] = [
        SimpleNamespace(
            receipt_id="a" * 64,
            source_release=release_a,
            target_release=release_b,
        ),
        SimpleNamespace(
            receipt_id="b" * 64,
            source_release=release_b,
            target_release=release_c,
        ),
    ]
    canary = coordinator._canaries(coordinator._current_release())[0]

    with patch.object(
        rotator,
        "matches_target",
        side_effect=lambda _connection_id, target: (
            target.terminal_sha256 == release_c.terminal_sha256
        ),
    ) as matches:
        assert coordinator._matches_applicable_pending_target(
            canary,
            coordinator._current_release(),
        )

    assert [call.args[1].terminal_sha256 for call in matches.call_args_list] == [
        release_b.terminal_sha256,
        release_c.terminal_sha256,
    ]


def test_post_commit_store_io_failure_reconciles_fleet_to_new_golden(
    tmp_path: Path,
) -> None:
    ids, instances, expert, manager, rotator, secrets = _fixture(
        tmp_path,
        ("Broker-A", "Broker-A"),
    )
    runtime = RuntimeController(publish_update=True)
    pool = FakePool()

    class FailingCompleteStore(FakePendingStore):
        def complete(self, receipt_id: str) -> None:
            raise OSError(f"cannot retire {receipt_id}")

    pending_store = FailingCompleteStore(manager)
    coordinator = _coordinator(
        instances,
        expert,
        manager,
        rotator,
        secrets,
        runtime,
        pool,
        pending_store,
    )

    with pytest.raises(
        Mt5MaintenanceError,
        match="pending update store is unavailable",
    ):
        coordinator.run_once(Event())

    assert manager.commits == 1
    assert len(rotator.rotate_all_calls) == 1
    assert rotator.rotate_all_calls[0] == coordinator._current_release()
    assert set(ids).issubset(rotator.current)
    assert len(pool.calls) == 2


def test_persistent_pool_failure_does_not_block_fleet_convergence(
    tmp_path: Path,
) -> None:
    ids, instances, expert, manager, rotator, secrets = _fixture(
        tmp_path,
        ("Broker-A", "Broker-A"),
    )

    class FailingPool(FakePool):
        def accept_template_rotation(
            self,
            digest: str,
            *,
            replenish: bool,
            stop_event: Event,
        ) -> int:
            self.calls.append((digest, replenish))
            raise OSError("pool storage unavailable")

    pool = FailingPool()
    pending_store = FakePendingStore(manager)
    coordinator = _coordinator(
        instances,
        expert,
        manager,
        rotator,
        secrets,
        RuntimeController(publish_update=True),
        pool,
        pending_store,
    )

    with pytest.raises(Mt5MaintenanceError, match="pool postcondition"):
        coordinator.run_once(Event())

    assert manager.commits == 1
    assert len(pool.calls) == 2
    assert len(rotator.rotate_all_calls) == 1
    assert rotator.rotate_all_calls[0] == coordinator._current_release()
    assert set(ids).issubset(rotator.current)


def test_probe_installs_a_stop_event_cancellation_check(tmp_path: Path) -> None:
    _ids, instances, expert, manager, rotator, secrets = _fixture(tmp_path)
    runtime = RuntimeController()
    pool = FakePool()
    coordinator = _coordinator(
        instances, expert, manager, rotator, secrets, runtime, pool
    )
    stop_event = Event()

    coordinator.run_once(stop_event)

    assert len(runtime.cancel_checks) == 1
    stop_event.set()
    cancel_check = runtime.cancel_checks[0]
    assert callable(cancel_check)
    with pytest.raises(Mt5MaintenanceError, match="was interrupted"):
        cancel_check()


def test_failed_probe_rebases_canary_to_current_golden_and_aborts_cascade(
    tmp_path: Path,
) -> None:
    ids, instances, expert, manager, rotator, secrets = _fixture(tmp_path)
    runtime = RuntimeController(fail_resume=True)
    pool = FakePool()
    coordinator = _coordinator(
        instances, expert, manager, rotator, secrets, runtime, pool
    )

    with pytest.raises(Mt5MaintenanceError, match="was recovered"):
        coordinator.run_once(Event())

    assert rotator.rotate_one_calls == [(ids[0], True), (ids[0], True)]
    assert rotator.rotate_one_history_from[-1] == runtime.resume_calls[0][
        "history_from"
    ]
    assert rotator.rotate_all_calls == []
    assert pool.calls == []


def test_failed_candidate_never_publishes_golden_or_rebuilds_pool(
    tmp_path: Path,
) -> None:
    _ids, instances, expert, manager, rotator, secrets = _fixture(tmp_path)
    rotator.current.update(child.name for child in instances.iterdir())
    rotator.candidate_fail = True
    runtime = RuntimeController(publish_update=True)
    pool = FakePool()
    pending_store = FakePendingStore(manager)
    coordinator = _coordinator(
        instances,
        expert,
        manager,
        rotator,
        secrets,
        runtime,
        pool,
        pending_store,
    )

    with pytest.raises(Mt5MaintenanceError, match="candidate verification"):
        coordinator.run_once(Event())

    assert manager.immediate_promotions == 0
    assert manager.commits == 0
    assert manager.discards == 1
    assert len(pending_store.pending()) == 1
    assert pool.calls == []
    assert rotator.rotate_all_calls == []


def test_failed_later_candidate_restores_previously_validated_canary(
    tmp_path: Path,
) -> None:
    ids, instances, expert, manager, rotator, secrets = _fixture(
        tmp_path,
        ("Broker-A", "Broker-B"),
    )
    rotator.current.update(ids)
    rotator.candidate_fail_after = 2
    runtime = RuntimeController(publish_update=True)
    pool = FakePool()
    pending_store = FakePendingStore(manager)
    coordinator = _coordinator(
        instances,
        expert,
        manager,
        rotator,
        secrets,
        runtime,
        pool,
        pending_store,
    )

    with pytest.raises(Mt5MaintenanceError, match="candidate verification"):
        coordinator.run_once(Event())

    assert rotator.rotate_one_calls[-1] == (min(ids), True)
    assert manager.commits == 0
    assert manager.discards == 1
    assert pool.calls == []


def test_failed_rotation_recovery_blocks_all_maintenance_mutations(
    tmp_path: Path,
) -> None:
    _ids, instances, expert, manager, rotator, secrets = _fixture(tmp_path)
    rotator.recovery_failed = ("broken",)
    runtime = RuntimeController(publish_update=True)
    pool = FakePool()
    coordinator = _coordinator(
        instances, expert, manager, rotator, secrets, runtime, pool
    )

    with pytest.raises(Mt5MaintenanceError, match="recovery is incomplete"):
        coordinator.run_once(Event())

    assert runtime.stop_calls == 0
    assert manager.promotions == 0
    assert pool.calls == []
