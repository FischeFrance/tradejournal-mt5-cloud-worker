"""Read-only adapter for versioned JSON emitted by ``TradeJournalBridge.ex5``.

This is the Windows agent's default data path.  It deliberately has no dependency on
the ``MetaTrader5`` Python package and opens no socket: the only input is the isolated
terminal's ``MQL5/Files/TradeJournal`` directory.  The legacy direct adapter remains
available for an explicitly selected future fallback.
"""

from __future__ import annotations

import json
import math
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from ..state_store import atomic_json, read_json
from .adapter_errors import IdentityMismatch, Mt5Error
from .history_balance import reconstruct_trade_deals

SCHEMA_VERSION = 1
# The EA refreshes every 2s. Thirty seconds still rejects an interrupted terminal promptly,
# while leaving margin for an initial full-history pass on a busy Windows host.
DEFAULT_HEARTBEAT_MAX_AGE_SECONDS = 30.0
MAX_CHECKPOINT_DEAL_KEYS = 512
# The EA publishes a complete bundle every two seconds.  On Windows, replacing one
# of those files can keep the destination briefly unavailable to another process.
# Cover one complete producer cycle plus margin, while keeping malformed JSON and
# identity/schema failures immediately fail-closed.
_TRANSIENT_READ_ATTEMPTS = 26
_SNAPSHOT_CONSISTENCY_ATTEMPTS = 26
_TRANSIENT_READ_DELAY_SECONDS = 0.1
_EVENT_FILE = re.compile(r"^event-([0-9]+)\.json$")


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

    history_snapshot_atomic = True
    history_time_basis = "broker_server_unresolved"

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
        self.state_dir = Path(state_dir)
        self.checkpoint_path = self.state_dir / "file-adapter-checkpoint.json"
        self.event_checkpoint_path = self.state_dir / "file-event-checkpoint.json"
        self.history_handoff_path = self.state_dir / "history-live-handoff.json"
        self._history_deals_cache: tuple[
            dict[str, Any] | None,
            tuple[dict[str, Any], ...],
            tuple[dict[str, Any], ...],
        ] | None = None

    @staticmethod
    def _parse_time(value: object) -> datetime | None:
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)

    @staticmethod
    def _all_available_window(start: datetime) -> bool:
        return start <= datetime(1970, 1, 1, tzinfo=timezone.utc)

    def _read_path_envelope(self, path: Path, label: str) -> tuple[dict[str, Any], Any]:
        for attempt in range(_TRANSIENT_READ_ATTEMPTS):
            try:
                raw = json.loads(path.read_text(encoding="utf-8-sig"))
                break
            except OSError as exc:
                if attempt + 1 == _TRANSIENT_READ_ATTEMPTS:
                    raise Mql5FileAdapterError(f"{label}_unavailable") from exc
                # The EA publishes files atomically, but Windows can briefly deny a
                # concurrent reader while the replacement is finalized.  Retry only
                # that narrow sharing race; malformed content remains fail-closed.
                time.sleep(_TRANSIENT_READ_DELAY_SECONDS)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise Mql5FileAdapterError(f"{label}_unavailable") from exc
        if not isinstance(raw, dict) or raw.get("schema_version") != SCHEMA_VERSION:
            raise Mql5FileAdapterError(f"{label}_schema_invalid")
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
            raise Mql5FileAdapterError(f"{label}_schema_invalid")
        if identity["login"] != self.expected_login:
            raise Mql5FileIdentityMismatch("account_identity_mismatch")
        if identity["server"].casefold() != self.expected_server.casefold() or server_identity.casefold() != self.expected_server.casefold():
            raise Mql5FileIdentityMismatch("server_identity_mismatch")
        return raw, raw["payload"]

    def _read_envelope(self, name: str) -> tuple[dict[str, Any], Any]:
        return self._read_path_envelope(self.files_dir / name, Path(name).stem)

    def _heartbeat_envelope(self) -> tuple[dict[str, Any], dict[str, Any]]:
        envelope, payload = self._read_envelope("heartbeat.json")
        if not isinstance(payload, dict) or not isinstance(payload.get("terminal_connected"), bool):
            raise Mql5FileAdapterError("heartbeat_schema_invalid")
        generated_at = self._parse_time(envelope["generated_at"])
        assert generated_at is not None
        age = (datetime.now(timezone.utc) - generated_at).total_seconds()
        if age < -2 or age > self.heartbeat_max_age_seconds:
            raise Mql5FileStale("heartbeat_stale")
        return envelope, payload

    def _heartbeat(self) -> dict[str, Any]:
        _, payload = self._heartbeat_envelope()
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
        balance = payload.get("balance")
        equity = payload.get("equity")
        currency = payload.get("currency")
        leverage = payload.get("leverage")
        credit = payload.get("credit", 0)
        if (
            not isinstance(balance, (int, float))
            or isinstance(balance, bool)
            or not math.isfinite(float(balance))
            or not isinstance(equity, (int, float))
            or isinstance(equity, bool)
            or not math.isfinite(float(equity))
            or not isinstance(currency, str)
            or not re.fullmatch(r"[A-Z0-9]{3,12}", currency.upper())
            or not isinstance(leverage, int)
            or isinstance(leverage, bool)
            or leverage < 1
            or leverage > 1_000_000
            or not isinstance(credit, (int, float))
            or isinstance(credit, bool)
            or not math.isfinite(float(credit))
        ):
            raise Mql5FileAdapterError("account_schema_invalid")
        return payload

    def verify_identity(self) -> dict[str, str]:
        self._account()
        return {"login": self.expected_login, "server": self.expected_server}

    def terminal_info(self) -> Any:
        return SimpleNamespace(connected=bool(self._heartbeat()["terminal_connected"]))

    def account_info(self) -> Any:
        account = self._account()
        return SimpleNamespace(
            trade_allowed=bool(account["trade_allowed"]),
            balance=float(account["balance"]),
            equity=float(account["equity"]),
            currency=str(account["currency"]).upper(),
            leverage=int(account["leverage"]),
            credit=float(account.get("credit", 0)),
        )

    def _rows(self, name: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        envelope, payload = self._read_envelope(name)
        if not isinstance(payload, list) or any(not isinstance(row, dict) for row in payload):
            raise Mql5FileAdapterError(f"{Path(name).stem}_schema_invalid")
        return envelope, [dict(row) for row in payload]

    def _require_history_sequence(self, envelope: dict[str, Any]) -> None:
        heartbeat, _ = self._heartbeat_envelope()
        account, _ = self._read_envelope("account.json")
        if len({envelope["sequence"], heartbeat["sequence"], account["sequence"]}) != 1:
            raise Mql5FileAdapterError("history_snapshot_sequence_mismatch")

    def _deal_rows(
        self,
    ) -> tuple[dict[str, Any], dict[str, Any] | None, list[dict[str, Any]]]:
        """Read the V2 anchored deal payload, retaining legacy list compatibility."""
        envelope, payload = self._read_envelope("deals.json")
        if isinstance(payload, list):
            anchor = None
            rows = payload
        elif isinstance(payload, dict):
            anchor = payload.get("anchor")
            rows = payload.get("deals")
            if not isinstance(anchor, dict):
                raise Mql5FileAdapterError("deals_schema_invalid")
        else:
            raise Mql5FileAdapterError("deals_schema_invalid")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise Mql5FileAdapterError("deals_schema_invalid")
        normalized_anchor = dict(anchor) if anchor is not None else None
        normalized_rows = [dict(row) for row in rows]
        if normalized_anchor is not None and normalized_anchor.get("coherent") is True:
            history_indices = [row.get("history_index") for row in normalized_rows]
            last = normalized_rows[-1] if normalized_rows else {}
            if (
                normalized_anchor.get("order_basis") != "mt5_history_index_v1"
                or history_indices != list(range(len(normalized_rows)))
                or normalized_anchor.get("deal_count") != len(normalized_rows)
                or str(normalized_anchor.get("last_deal_ticket", "0"))
                != str(last.get("ticket", "0"))
                or normalized_anchor.get("last_deal_time_msc", 0)
                != last.get("time_msc", 0)
            ):
                normalized_anchor["coherent"] = False
                normalized_anchor["coherence_error"] = "ledger_metadata_mismatch"
        return envelope, normalized_anchor, normalized_rows

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

    @staticmethod
    def _positions_by_stable_id(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """Key positions like MT5 deal history does, with backwards-compatible fallback.

        ``DEAL_POSITION_ID`` refers to ``POSITION_IDENTIFIER``.  A position ticket may change
        after broker-side service operations, while the identifier remains stable for the
        lifetime of the logical trade.  Older EA builds do not publish ``position_id``, so they
        continue to work through the ticket fallback during a controlled rollout.
        """
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            key = row.get("position_id") or row.get("ticket")
            if key is not None:
                result[str(key)] = row
        return result

    def snapshot(self, lookback_hours: int = 72) -> dict[str, dict[str, Any]]:
        del lookback_hours  # freshness is enforced from the producer's generated_at field.
        for attempt in range(_SNAPSHOT_CONSISTENCY_ATTEMPTS):
            heartbeat_before, heartbeat_payload = self._heartbeat_envelope()
            if not heartbeat_payload["terminal_connected"]:
                raise Mql5FileAdapterError("terminal_not_connected")
            account_envelope, account = self._read_envelope("account.json")
            if not isinstance(account, dict) or str(account.get("login")) != self.expected_login:
                raise Mql5FileIdentityMismatch("account_identity_mismatch")
            positions_envelope, positions = self._rows("positions.json")
            orders_envelope, orders = self._rows("orders.json")
            deals_envelope, _, deals = self._deal_rows()
            heartbeat_after, _ = self._heartbeat_envelope()
            sequences = {
                int(heartbeat_before["sequence"]),
                int(heartbeat_after["sequence"]),
                int(account_envelope["sequence"]),
                int(positions_envelope["sequence"]),
                int(orders_envelope["sequence"]),
                int(deals_envelope["sequence"]),
            }
            if len(sequences) == 1:
                mapped_deals = self._dedupe(deals, "ticket")
                self._save_checkpoint(int(deals_envelope["sequence"]), list(mapped_deals))
                return {
                    "positions": self._positions_by_stable_id(positions),
                    "orders": self._dedupe(orders, "ticket"),
                    "deals": mapped_deals,
                }
            if attempt + 1 < _SNAPSHOT_CONSISTENCY_ATTEMPTS:
                time.sleep(_TRANSIENT_READ_DELAY_SECONDS)
        raise Mql5FileAdapterError("snapshot_sequence_mismatch")

    def _history(self, name: str, start: datetime, end: datetime) -> tuple[dict[str, Any], ...]:
        self.verify_identity()
        envelope, rows = self._rows(name)
        self._require_history_sequence(envelope)
        all_available = self._all_available_window(start)
        result = []
        for row in rows:
            moment = self._parse_time(row.get("time", row.get("close_time")))
            if moment is not None and (all_available or start <= moment < end):
                result.append(row)
        return tuple(result)

    def history_orders(self, start: datetime, end: datetime) -> tuple[dict[str, Any], ...]:
        return self._history("history_orders.json", start, end)

    def history_deals(self, start: datetime, end: datetime) -> tuple[dict[str, Any], ...]:
        self.verify_identity()
        _, projected, _ = self._historical_deal_snapshot()
        annotated = any("history_event_type" in row for row in projected)
        all_available = self._all_available_window(start)
        eligible_positions = {
            str(row.get("position_id"))
            for row in projected
            if row.get("project_as_trade", True) is True
            and row.get("history_event_type") == "trade_opened"
            and (moment := self._parse_time(row.get("time"))) is not None
            and (all_available or start <= moment < end)
        }
        return tuple(
            row
            for row in projected
            if row.get("project_as_trade", True) is True
            and (moment := self._parse_time(row.get("time"))) is not None
            and (all_available or moment < end)
            and (
                str(row.get("position_id")) in eligible_positions
                if annotated
                else start <= moment
            )
        )

    def history_accounting_deals(
        self, start: datetime, end: datetime
    ) -> tuple[dict[str, Any], ...]:
        """Return the complete ledger, including deposits, credit and charges.

        These records are persisted in a dedicated local audit stream and are
        intentionally never projected into the journal as trades.
        """
        self.verify_identity()
        _, _, rows = self._historical_deal_snapshot()
        all_available = self._all_available_window(start)
        return tuple(
            row
            for row in rows
            if (moment := self._parse_time(row.get("time"))) is not None
            and (all_available or start <= moment < end)
        )

    def history_anchor(self) -> dict[str, Any] | None:
        self.verify_identity()
        anchor, _, _ = self._historical_deal_snapshot()
        return anchor

    def history_balance_rows(self) -> tuple[dict[str, Any], ...]:
        """Include non-projected lifecycles so the manual report can say N/D."""
        self.verify_identity()
        _, projected, _ = self._historical_deal_snapshot()
        return projected

    def _historical_deal_snapshot(
        self,
    ) -> tuple[
        dict[str, Any] | None,
        tuple[dict[str, Any], ...],
        tuple[dict[str, Any], ...],
    ]:
        if self._history_deals_cache is None:
            envelope, anchor, rows = self._deal_rows()
            self._require_history_sequence(envelope)
            immutable_rows = tuple(dict(row) for row in rows)
            if anchor is None:
                valid_entries = {"0", "1", "2", "3", "IN", "OUT", "INOUT", "OUT_BY"}
                projected = tuple(
                    row
                    for row in immutable_rows
                    if str(row.get("position_id", "")).strip() not in ("", "0")
                    and isinstance(row.get("symbol"), str)
                    and bool(row["symbol"].strip())
                    and str(row.get("entry", "")).strip().upper() in valid_entries
                )
            else:
                projected = reconstruct_trade_deals(immutable_rows, anchor)
            self._history_deals_cache = (
                dict(anchor) if anchor is not None else None,
                projected,
                immutable_rows,
            )
        return self._history_deals_cache

    def pending_events(self) -> tuple[dict[str, Any], ...]:
        checkpoint = read_json(self.event_checkpoint_path, {})
        last_sequence = checkpoint.get("last_sequence", 0)
        if not isinstance(last_sequence, int) or last_sequence < 0:
            raise Mql5FileAdapterError("event_checkpoint_invalid")
        events_dir = self.files_dir / "events"
        if not events_dir.exists():
            return ()
        if events_dir.is_symlink() or not events_dir.is_dir():
            raise Mql5FileAdapterError("events_directory_invalid")
        self._prune_acknowledged_event_files(events_dir, last_sequence)
        handoff_deal_tickets = self._history_handoff_deal_tickets()
        pending: list[tuple[int, dict[str, Any]]] = []
        for path in events_dir.iterdir():
            match = _EVENT_FILE.fullmatch(path.name)
            if not match:
                continue
            if path.is_symlink() or not path.is_file():
                raise Mql5FileAdapterError("event_file_invalid")
            sequence = int(match.group(1))
            if sequence <= last_sequence:
                continue
            envelope, payload = self._read_path_envelope(path, "event")
            if envelope["sequence"] != sequence or not isinstance(payload, dict):
                raise Mql5FileAdapterError("event_schema_invalid")
            event = {"sequence": sequence, **payload}
            native_deal_ticket = event.get("deal_id") or event.get("ticket")
            if (
                str(event.get("event_type", "")).upper() == "DEAL_ADD"
                and native_deal_ticket is not None
                and str(native_deal_ticket) in handoff_deal_tickets
            ):
                event["history_archived"] = True
            pending.append((sequence, event))
        pending.sort(key=lambda item: item[0])
        return tuple(payload for _, payload in pending)

    def _history_handoff_deal_tickets(self) -> set[str]:
        if not self.history_handoff_path.exists():
            return set()
        value = read_json(self.history_handoff_path, {})
        if not isinstance(value, dict):
            raise Mql5FileAdapterError("history_handoff_invalid")
        tickets = value.get("archived_deal_tickets")
        imported_tickets = value.get("imported_deal_tickets")
        if (
            value.get("schema_version") != 2
            or value.get("connection_id") != self.connection_id
            or not isinstance(value.get("job_id"), str)
            or not re.fullmatch(
                r"[0-9a-f]{64}", str(value.get("history_document_sha256", ""))
            )
            or not isinstance(value.get("anchor_sequence"), int)
            or not isinstance(tickets, list)
            or not isinstance(imported_tickets, list)
            or any(
                not isinstance(ticket, str)
                or not re.fullmatch(r"[0-9]{1,32}", ticket)
                for ticket in [*tickets, *imported_tickets]
            )
            or not set(imported_tickets).issubset(set(tickets))
        ):
            raise Mql5FileAdapterError("history_handoff_invalid")
        return set(tickets)

    def acknowledge_events(self, through_sequence: int) -> None:
        if not isinstance(through_sequence, int) or through_sequence < 0:
            raise Mql5FileAdapterError("event_checkpoint_invalid")
        current = read_json(self.event_checkpoint_path, {})
        previous = current.get("last_sequence", 0)
        if not isinstance(previous, int) or previous < 0 or through_sequence < previous:
            raise Mql5FileAdapterError("event_checkpoint_regression")
        atomic_json(
            self.event_checkpoint_path,
            {"connection_id": self.connection_id, "last_sequence": through_sequence},
        )
        events_dir = self.files_dir / "events"
        if events_dir.exists():
            if events_dir.is_symlink() or not events_dir.is_dir():
                raise Mql5FileAdapterError("events_directory_invalid")
            self._prune_acknowledged_event_files(events_dir, through_sequence)

    @staticmethod
    def _prune_acknowledged_event_files(
        events_dir: Path, through_sequence: int
    ) -> None:
        for path in events_dir.iterdir():
            match = _EVENT_FILE.fullmatch(path.name)
            if not match or int(match.group(1)) > through_sequence:
                continue
            if path.is_symlink() or not path.is_file():
                raise Mql5FileAdapterError("event_file_invalid")
            try:
                path.unlink()
            except OSError as exc:
                raise Mql5FileAdapterError("event_cleanup_failed") from exc

    def candles(self, symbol: str, timeframe: str) -> tuple[dict[str, Any], ...]:
        _, payload = self._read_envelope(f"candles/{symbol}-{timeframe}.json")
        if not isinstance(payload, list) or any(not isinstance(row, dict) for row in payload):
            raise Mql5FileAdapterError("candles_schema_invalid")
        return tuple(dict(row) for row in payload)

    def checkpoint(self) -> dict[str, Any]:
        """Expose only sanitized, bounded recovery metadata for diagnostics/tests."""
        value = read_json(self.checkpoint_path, {})
        return value if isinstance(value, dict) else {}
