from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


class LocalEventSink:
    """Durable append-only JSONL sink for detected/imported events.

    No route exists yet (server- or agent-side) to forward these to the trading-mt5-events
    ingestion endpoint for managed connections (that requires issuing a per-connection bridge
    token during provisioning, which is not part of the control-plane contract today -- see
    CONTROL-PLANE-NEXT-STEPS.txt). Persisting locally, durably, and losslessly is strictly better
    than the previous placeholder (a bare no-op lambda in customer_flow.py) while that gap remains
    open: nothing detected is ever silently discarded.
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
