from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


class LocalEventSink:
    """Durable append-only JSONL sink for detected/imported events.

    The managed path also forwards through the persistent EventOutbox to
    ``trading-mt5-events``. This local stream is the independent, durable attempt audit: it is not
    used as delivery acknowledgement and may therefore contain repeated attempts with the same
    event_id.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def __call__(self, payload: dict[str, Any]) -> None:
        line = json.dumps(payload, sort_keys=True, default=str) + "\n"
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            with os.fdopen(fd, "a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            pass
