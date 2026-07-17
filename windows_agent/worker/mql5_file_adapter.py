"""Read-only adapter for versioned JSON emitted by ``TradeJournalBridge.ex5``.

This is the Windows agent's default data path.  It deliberately has no dependency on
the ``MetaTrader5`` Python package and opens no socket: the only input is the isolated
terminal's ``MQL5/Files/TradeJournal`` directory.  The legacy direct adapter remains
available for an explicitly selected future fallback.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from ..state_store import atomic_json, read_json
from .direct_mt5_adapter import IdentityMismatch, Mt5Error

SCHEMA_VERSION = 1
# The EA refreshes every 2s. Thirty seconds still rejects an interrupted terminal promptly,
# while leaving margin for an initial full-history pass on a busy Windows host.
DEFAULT_HEARTBEAT_MAX_AGE_SECONDS = 30.0
MAX_CHECKPOINT_DEAL_KEYS = 512


class Mql5FileAdapterError(Mt5Error):
    """Sanitized local file bridge failure; never carries snapshot payloads."""


class Mql5FileStale(Mql5FileAdapterError):
    pass


class Mql5FileIdentityMismatch(IdentityMismatch):
    pass


class Mql5FileMt5Adapter:
    """Expose the read methods consumed by ``HistorySync`` and ``LiveSync``.

    A snapshot is accepted only when its envelope and heartbeat are valid for the
    expected account.  A temporary/corrupt file is treated as not ready and retried
    by the daemon on its next job/poll rather than being forwarded as partial data.
    """

    def __init__(
        self,
        files_dir: Path,
        connection_id: str,
        expected_login: int,
        expected_server: str,
        state_dir: Path,
        heartbeat_max_age_seconds: float = DEFAULT_HEARTBEAT_MAX_AGE_SECONDS,
    ) -> None:
        if heartbeat_max_age_seconds <= 0:
            raise ValueError("heartbeat_max_age_seconds must be positive")
        self.files_dir = Path(files_dir)
        self.connection_id = str(connection_id)
        self.expected_login = str(int(expected_login))
        self.expected_server = str(expected_server)
        self.heartbeat_max_age_seconds = heartbeat_max_age_seconds
        self.checkpoint_path = Path(state_dir) / "file-adapter-checkpoint.json"

    @staticmethod
    def _parse_time(value: object) -> datetime | None:
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)

    def _read_envelope(self, name: str) -> tuple[dict[str, Any], Any]:
        path = self.files_dir / name
        try:
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise Mql5FileAdapterError(f"{Path(name).stem}_unavailable") from exc
        if not isinstance(raw, dict) or raw.get("schema_version") != SCHEMA_VERSION:
            raise Mql5FileAdapterError(f"{Path(name).stem}_schema_invalid")
        generated_at = self._parse_time(raw.get("generated_at"))
        sequence = raw.get("sequence")
        identity = raw.get("account_identity")
        server_identity = raw.get("server_identity")
        if (
            generated_at is None
            or not isinstance(sequence, int)
            or sequence < 0
            or not isinstance(identity, dict)
            or not isinstance(identity.get("login"), str)
            or not isinstance(identity.get("server"), str)
            or not isinstance(server_identity, str)
            or "payload" not in raw
        ):
            raise Mql5FileAdapterError(f"{Path(name).stem}_schema_invalid")
        if identity["login"] != self.expected_login:
            raise Mql5FileIdentityMismatch("account_identity_mismatch")
        if identity["server"].casefold() != self.expected_server.casefold() or server_identity.casefold() != self.expected_server.casefold():
            raise Mql5FileIdentityMismatch("server_identity_mismatch")
        return raw, raw["payload"]

    def _heartbeat(self) -> dict[str, Any]:
        envelope, payload = self._read_envelope("heartbeat.json")
        if not isinstance(payload, dict) or not isinstance(payload.get("terminal_connected"), bool):
            raise Mql5FileAdapterError("heartbeat_schema_invalid")
        generated_at = self._parse_time(envelope["generated_at"])
        assert generated_at is not None
        age = (datetime.now(timezone.utc) - generated_at).total_seconds()
        if age < -2 or age > self.heartbeat_max_age_seconds:
            raise Mql5FileStale("heartbeat_stale")
        return payload

    def _account(self) -> dict[str, Any]:
        self._heartbeat()
        _, payload = self._read_envelope("account.json")
        if not isinstance(payload, dict):
            raise Mql5FileAdapterError("account_schema_invalid")
        if str(payload.get("login")) != self.expected_login:
            raise Mql5FileIdentityMismatch("account_identity_mismatch")
        server = payload.get("server")
        if not isinstance(server, str) or server.casefold() != self.expected_server.casefold():
            raise Mql5FileIdentityMismatch("server_identity_mismatch")
        if not isinstance(payload.get("trade_allowed"), bool):
            raise Mql5FileAdapterError("account_schema_invalid")
        return payload

    def verify_identity(self) -> dict[str, str]:
        self._account()
        return {"login": self.expected_login, "server": self.expected_server}

    def terminal_info(self) -> Any:
        return SimpleNamespace(connected=bool(self._heartbeat()["terminal_connected"]))

    def account_info(self) -> Any:
        account = self._account()
        return SimpleNamespace(trade_allowed=bool(account["trade_allowed"]))

    def _rows(self, name: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        envelope, payload = self._read_envelope(name)
        if not isinstance(payload, list) or any(not isinstance(row, dict) for row in payload):
            raise Mql5FileAdapterError(f"{Path(name).stem}_schema_invalid")
        return envelope, [dict(row) for row in payload]

    @staticmethod
    def _dedupe(rows: list[dict[str, Any]], field: str) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            key = str(row.get(field, ""))
            if key:
                result[key] = row
        return result

    def _save_checkpoint(self, sequence: int, deal_keys: list[str]) -> None:
        self.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(
            self.checkpoint_path,
            {
                "connection_id": self.connection_id,
                "sequence": sequence,
                "recent_deal_keys": deal_keys[-MAX_CHECKPOINT_DEAL_KEYS:],
            },
        )

    def snapshot(self, lookback_hours: int = 72) -> dict[str, dict[str, Any]]:
        del lookback_hours  # freshness is enforced from the producer's generated_at field.
        self.verify_identity()
        _, positions = self._rows("positions.json")
        _, orders = self._rows("orders.json")
        deals_envelope, deals = self._rows("deals.json")
        mapped_deals = self._dedupe(deals, "ticket")
        self._save_checkpoint(int(deals_envelope["sequence"]), list(mapped_deals))
        return {
            "positions": self._dedupe(positions, "ticket"),
            "orders": self._dedupe(orders, "ticket"),
            "deals": mapped_deals,
        }

    def _history(self, name: str, start: datetime, end: datetime) -> tuple[dict[str, Any], ...]:
        self.verify_identity()
        _, rows = self._rows(name)
        result = []
        for row in rows:
            moment = self._parse_time(row.get("time", row.get("close_time")))
            if moment is not None and start <= moment < end:
                result.append(row)
        return tuple(result)

    def history_orders(self, start: datetime, end: datetime) -> tuple[dict[str, Any], ...]:
        return self._history("history_orders.json", start, end)

    def history_deals(self, start: datetime, end: datetime) -> tuple[dict[str, Any], ...]:
        return self._history("deals.json", start, end)

    def candles(self, symbol: str, timeframe: str) -> tuple[dict[str, Any], ...]:
        _, payload = self._read_envelope(f"candles/{symbol}-{timeframe}.json")
        if not isinstance(payload, list) or any(not isinstance(row, dict) for row in payload):
            raise Mql5FileAdapterError("candles_schema_invalid")
        return tuple(dict(row) for row in payload)

    def checkpoint(self) -> dict[str, Any]:
        """Expose only sanitized, bounded recovery metadata for diagnostics/tests."""
        value = read_json(self.checkpoint_path, {})
        return value if isinstance(value, dict) else {}
