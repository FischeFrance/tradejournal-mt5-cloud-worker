from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..security import safe_child

SUBDIRS = ("terminal", "worker", "secrets", "state", "logs", "data")


@dataclass(frozen=True)
class InstanceLayout:
    root: Path
    connection_id: str

    @property
    def path(self) -> Path:
        return safe_child(self.root, self.connection_id)

    def create(self) -> Path:
        self.path.mkdir(parents=True, exist_ok=True)
        for name in SUBDIRS:
            (self.path / name).mkdir(exist_ok=True)
        return self.path
