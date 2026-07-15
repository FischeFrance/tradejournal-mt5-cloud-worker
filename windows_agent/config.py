from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .security import canonical_uuid, safe_child


@dataclass(frozen=True)
class AgentConfig:
    connection_id: str
    instances_root: Path = Path(r"C:\TradeJournal\instances")
    poll_seconds: float = 2.0
    research_enabled: bool = False

    def __post_init__(self) -> None:
        canonical_uuid(self.connection_id)
        if not 0.25 <= self.poll_seconds <= 300:
            raise ValueError("poll_seconds outside safe range")
        if self.research_enabled:
            raise ValueError("research cannot be enabled by local/client configuration")

    @property
    def instance_root(self) -> Path:
        return safe_child(self.instances_root, self.connection_id)
