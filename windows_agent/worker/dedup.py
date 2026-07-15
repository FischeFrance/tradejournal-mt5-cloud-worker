from __future__ import annotations

import sqlite3
from pathlib import Path


class PersistentDedup:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS delivered (event_id TEXT PRIMARY KEY, at TEXT DEFAULT CURRENT_TIMESTAMP)"
        )

    def contains(self, event_id: str) -> bool:
        return (
            self.connection.execute(
                "SELECT 1 FROM delivered WHERE event_id=?", (event_id,)
            ).fetchone()
            is not None
        )

    def add(self, event_id: str) -> bool:
        cursor = self.connection.execute(
            "INSERT OR IGNORE INTO delivered(event_id) VALUES (?)", (event_id,)
        )
        self.connection.commit()
        return cursor.rowcount == 1
