from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any
from uuid import uuid4

from windows_agent.provisioning.mt5_instance import InstanceProvisioner
from windows_agent.provisioning.secret_store import WindowsSecretStore
from windows_agent.real_handlers import recover_startup_instances


class FakeRuntime:
    calls: list[dict[str, Any]] = []
    installs: list[dict[str, Any]] = []
    stopped = False

    def __init__(self, root: Path, connection_id: str) -> None:
        self.root = root
        self.connection_id = connection_id

    def resume(self, **kwargs: Any) -> None:
        self.calls.append(
            {
                "connection_id": self.connection_id,
                **kwargs,
            }
        )

    def install_expert(
        self,
        expert_binary: Path,
        history_mode: str,
    ) -> None:
        destination = (
            self.root
            / "terminal"
            / "MQL5"
            / "Experts"
            / "TradeJournal"
            / "TradeJournalBridge.ex5"
        )
        destination.write_bytes(expert_binary.read_bytes())
        self.installs.append(
            {
                "connection_id": self.connection_id,
                "expert_binary": expert_binary,
                "history_mode": history_mode,
            }
        )

    def stop(self) -> None:
        type(self).stopped = True


class FakeProcessManager:
    adopted: list[Path] = []

    def __init__(self, state_path: Path) -> None:
        self.state_path = state_path

    def adopt(self, executable: Path) -> int:
        self.adopted.append(executable)
        return 321


def _published_instance(tmp_path: Path, monkeypatch: Any) -> tuple[str, Path, Path, Path]:
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
        staticmethod(lambda path: None),
    )
    connection_id = str(uuid4())
    instances_root = tmp_path / "instances"
    secrets_root = tmp_path / "secrets"
    template_root = tmp_path / "template"
    terminal = template_root / "terminal64.exe"
    expert = template_root / "MQL5" / "Experts" / "TradeJournal" / "TradeJournalBridge.ex5"
    discovery = template_root / "MQL5" / "Scripts" / "TradeJournal" / "TradeJournalDiscovery.ex5"
    loader = template_root / "MQL5" / "Scripts" / "TradeJournal" / "TradeJournalLoader.ex5"
    terminal.parent.mkdir(parents=True)
    expert.parent.mkdir(parents=True)
    discovery.parent.mkdir(parents=True)
    terminal.write_bytes(b"terminal")
    expert.write_bytes(b"expert")
    discovery.write_bytes(b"discovery")
    loader.write_bytes(b"loader")
    terminal_sha256 = hashlib.sha256(terminal.read_bytes()).hexdigest()
    provisioner = InstanceProvisioner(instances_root, secrets_root)
    provisioner.provision(connection_id, terminal, terminal_sha256)
    provisioner.seal_runtime_assets(connection_id)
    return connection_id, instances_root, secrets_root, expert


def test_startup_recovery_resumes_once_without_reading_password(tmp_path: Path, monkeypatch: Any) -> None:
    connection_id, instances_root, secrets_root, expert = _published_instance(tmp_path, monkeypatch)
    store = WindowsSecretStore(secrets_root)
    store.write(connection_id, "mt5_login", "42")
    store.write(connection_id, "mt5_server", "Demo")
    store.write(connection_id, "bridge_token", "tjmt5_test")
    FakeRuntime.calls = []
    FakeRuntime.installs = []
    FakeRuntime.stopped = False
    FakeProcessManager.adopted = []

    result = recover_startup_instances(
        instances_root,
        secrets_root,
        (connection_id,),
        expert,
        hashlib.sha256(expert.read_bytes()).hexdigest(),
        process_factory=FakeProcessManager,
        process_finder=lambda terminal: [],
        runtime_factory=FakeRuntime,
    )

    assert result.recovered == (connection_id,)
    assert result.failed == ()
    assert FakeRuntime.calls == [
        {
            "connection_id": connection_id,
            "login": 42,
            "server": "Demo",
            "expert_binary": expert,
            "history_mode": "new_only",
        }
    ]
    assert FakeRuntime.installs == [
        {
            "connection_id": connection_id,
            "expert_binary": expert,
            "history_mode": "new_only",
        }
    ]
    assert len(FakeProcessManager.adopted) == 1
    assert FakeRuntime.stopped is False
    assert not (secrets_root / connection_id / "mt5_investor_password.dpapi").exists()


def test_startup_recovery_fails_closed_without_bridge_token(tmp_path: Path, monkeypatch: Any) -> None:
    connection_id, instances_root, secrets_root, expert = _published_instance(tmp_path, monkeypatch)
    store = WindowsSecretStore(secrets_root)
    store.write(connection_id, "mt5_login", "42")
    store.write(connection_id, "mt5_server", "Demo")
    FakeRuntime.calls = []
    FakeRuntime.installs = []
    FakeRuntime.stopped = False
    FakeProcessManager.adopted = []

    result = recover_startup_instances(
        instances_root,
        secrets_root,
        (connection_id,),
        expert,
        hashlib.sha256(expert.read_bytes()).hexdigest(),
        process_factory=FakeProcessManager,
        process_finder=lambda terminal: [],
        runtime_factory=FakeRuntime,
    )

    assert result.recovered == ()
    assert result.failed == (connection_id,)
    assert FakeRuntime.calls == []
    assert FakeRuntime.installs == []
    assert FakeProcessManager.adopted == []
