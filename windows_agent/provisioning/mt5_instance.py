from __future__ import annotations

import shutil
from pathlib import Path

from ..state_store import atomic_json
from .instance_layout import InstanceLayout
from .secret_store import WindowsSecretStore


class InstanceProvisioner:
    def __init__(self, instances_root: Path, secrets_root: Path) -> None:
        self.instances_root = instances_root
        self.secrets = WindowsSecretStore(secrets_root)

    def provision(
        self, connection_id: str, source_terminal: Path | None = None
    ) -> Path:
        layout = InstanceLayout(self.instances_root, connection_id)
        root = layout.create()
        terminal = root / "terminal" / "terminal64.exe"
        if source_terminal is not None:
            if (
                source_terminal.resolve().name.lower() != "terminal64.exe"
                or not source_terminal.is_file()
            ):
                raise ValueError("source terminal invalid")
            source_root = source_terminal.resolve().parent
            for source in source_root.iterdir():
                destination = terminal.parent / source.name
                if source.is_dir():
                    shutil.copytree(source, destination, dirs_exist_ok=True)
                else:
                    shutil.copy2(source, destination)
        atomic_json(
            root / "state" / "instance.json",
            {
                "connection_id": connection_id,
                "status": "provisioned",
                "terminal": str(terminal),
            },
        )
        return root

    def deprovision(self, connection_id: str) -> None:
        layout = InstanceLayout(self.instances_root, connection_id)
        self.secrets.delete_connection(connection_id)
        root = layout.path
        if not root.exists():
            return
        atomic_json(
            root / "state" / "instance.json",
            {"connection_id": connection_id, "status": "deprovisioned"},
        )
        for child in (root / "terminal", root / "worker", root / "data"):
            if child.exists():
                shutil.rmtree(child)
