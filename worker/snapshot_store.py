"""Persistenza minimale dell'ultimo snapshot noto, per confrontare i poll successivi.

Tiene lo stato in memoria durante l'esecuzione del processo e, opzionalmente, lo salva su un
file JSON locale cosi' che un riavvio del worker non generi eventi "trade_opened" fantasma per
posizioni gia' note prima del riavvio. Nessun dato sensibile (password/token) transita mai da
questo modulo: lo snapshot contiene solo posizioni/ordini/deal.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger("mt5_worker.snapshot_store")

EMPTY_SNAPSHOT: Dict[str, Any] = {"positions": {}, "orders": {}, "deals": {}}


class SnapshotStore:
    def __init__(self, file_path: Optional[str] = None) -> None:
        self.file_path = file_path
        self._snapshot: Dict[str, Any] = self._load_from_disk() if file_path else _empty()

    def get(self) -> Dict[str, Any]:
        return self._snapshot

    def update(self, snapshot: Dict[str, Any]) -> None:
        self._snapshot = snapshot
        if self.file_path:
            self._save_to_disk(snapshot)

    def _load_from_disk(self) -> Dict[str, Any]:
        if not self.file_path or not os.path.exists(self.file_path):
            return _empty()
        try:
            with open(self.file_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if not isinstance(data, dict):
                return _empty()
            return {
                "positions": data.get("positions", {}),
                "orders": data.get("orders", {}),
                "deals": data.get("deals", {}),
            }
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Snapshot su disco illeggibile (%s), riparto da uno stato vuoto.", exc)
            return _empty()

    def _save_to_disk(self, snapshot: Dict[str, Any]) -> None:
        assert self.file_path is not None
        directory = os.path.dirname(self.file_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp_path = f"{self.file_path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(snapshot, handle)
        os.replace(tmp_path, self.file_path)


def _empty() -> Dict[str, Any]:
    return {"positions": {}, "orders": {}, "deals": {}}
