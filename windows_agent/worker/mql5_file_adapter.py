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
# v1 had one shared EA/Windows marker.  A replacement immediately before the
# EA's cleanup could be deleted as if it were the EA's own old marker.  The v2
# protocol gives each writer an owned pathname instead: Windows only replaces
# the request, while the EA only writes state/ack files.
_SOURCE_RECOVERY_LEGACY_MARKER = "source-recovery-required.json"
_SOURCE_RECOVERY_REQUEST_V2 = "source-recovery-request-v2.json"
_SOURCE_RECOVERY_ACK_V2 = "source-recovery-ack-v2.json"
_SOURCE_RECOVERY_PROTOCOL_VERSION = 2
_SOURCE_RECOVERY_GENERATION_STATE_SCHEMA_VERSION = 1
_SOURCE_RECOVERY_GENERATION_STATE = "source-recovery-request-v2-state.json"
_MAX_SOURCE_RECOVERY_GENERATION = (1 << 63) - 1


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

    # The EA commits a full historical ledger only with an aligned account and
    # heartbeat sequence.  Consumers can therefore distinguish it from legacy
    # windowed history without guessing at its broker time zone.
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
        self.history_high_water_path = self.state_dir / "history-ledger-high-water.json"

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

    @staticmethod
    def _is_source_recovery_generation(value: object, *, minimum: int = 0) -> bool:
        return (
            isinstance(value, int)
            and not isinstance(value, bool)
            and minimum <= value <= _MAX_SOURCE_RECOVERY_GENERATION
        )

    @staticmethod
    def _is_source_recovery_protocol_version(value: object) -> bool:
        return (
            isinstance(value, int)
            and not isinstance(value, bool)
            and value == _SOURCE_RECOVERY_PROTOCOL_VERSION
        )

    @staticmethod
    def _is_full_history_recovery_cutoff(value: object) -> bool:
        return isinstance(value, int) and not isinstance(value, bool) and value == 0

    def _read_optional_control_object(
        self, name: str, label: str
    ) -> dict[str, Any] | None:
        """Read one non-envelope recovery control file without trusting a race.

        Control files are atomically replaced by their respective owner.  A
        short sharing/replace race is retried, but a malformed object, a
        symlink, or a persistent read error is never interpreted as "no
        recovery request".
        """

        path = self.files_dir / name
        for attempt in range(_TRANSIENT_READ_ATTEMPTS):
            try:
                if path.is_symlink():
                    raise Mql5FileAdapterError(f"{label}_invalid")
                if not path.exists():
                    # An absent optional control file is the normal bootstrap
                    # state.  Do not turn every healthy heartbeat into a
                    # multi-second polling delay merely to chase a later
                    # independent writer; the next watcher/poll observes it.
                    return None
                if not path.is_file():
                    raise Mql5FileAdapterError(f"{label}_invalid")
                raw = json.loads(path.read_text(encoding="utf-8-sig"))
                break
            except Mql5FileAdapterError:
                raise
            except FileNotFoundError:
                # The EA and Windows both publish by atomic replacement.  A
                # reader can observe the narrow replace boundary even though
                # the final state is valid.
                if attempt + 1 == _TRANSIENT_READ_ATTEMPTS:
                    return None
                time.sleep(_TRANSIENT_READ_DELAY_SECONDS)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise Mql5FileAdapterError(f"{label}_invalid") from exc
            except OSError as exc:
                if attempt + 1 == _TRANSIENT_READ_ATTEMPTS:
                    raise Mql5FileAdapterError(f"{label}_unavailable") from exc
                time.sleep(_TRANSIENT_READ_DELAY_SECONDS)
        else:  # pragma: no cover - loop always returns or breaks
            return None
        if not isinstance(raw, dict):
            raise Mql5FileAdapterError(f"{label}_invalid")
        return raw

    def _read_source_recovery_request_v2(self) -> dict[str, int] | None:
        payload = self._read_optional_control_object(
            _SOURCE_RECOVERY_REQUEST_V2, "source_recovery_request"
        )
        if payload is None:
            return None
        generation = payload.get("generation")
        if (
            not self._is_source_recovery_protocol_version(
                payload.get("protocol_version")
            )
            or not self._is_source_recovery_generation(generation, minimum=1)
            # An inferred cutoff can omit the predecessor of a delayed close.
            # The only Windows-initiated request is deliberately full ledger.
            or not self._is_full_history_recovery_cutoff(payload.get("from_unix"))
        ):
            raise Mql5FileAdapterError("source_recovery_request_invalid")
        assert isinstance(generation, int)
        return {"generation": generation}

    def _read_source_recovery_ack_v2(self) -> dict[str, int] | None:
        payload = self._read_optional_control_object(
            _SOURCE_RECOVERY_ACK_V2, "source_recovery_ack"
        )
        if payload is None:
            return None
        generation = payload.get("generation")
        if (
            not self._is_source_recovery_protocol_version(
                payload.get("protocol_version")
            )
            or not self._is_source_recovery_generation(generation)
            or not self._is_full_history_recovery_cutoff(payload.get("from_unix"))
        ):
            raise Mql5FileAdapterError("source_recovery_ack_invalid")
        assert isinstance(generation, int)
        return {"generation": generation}

    def _read_legacy_source_recovery_marker(self) -> dict[str, int] | None:
        """Validate, but never mutate, an inherited v1 EA-owned marker."""

        payload = self._read_optional_control_object(
            _SOURCE_RECOVERY_LEGACY_MARKER, "source_recovery_marker"
        )
        if payload is None:
            return None
        from_unix = payload.get("from_unix")
        if (
            not isinstance(payload.get("continuity_version"), int)
            or isinstance(payload.get("continuity_version"), bool)
            or payload.get("continuity_version") != 1
            or not self._is_source_recovery_generation(from_unix)
        ):
            raise Mql5FileAdapterError("source_recovery_marker_invalid")
        assert isinstance(from_unix, int)
        return {"from_unix": from_unix}

    def _source_recovery_heartbeat_v2(
        self, payload: dict[str, Any], *, require_v2: bool = False
    ) -> tuple[int, int, int] | None:
        """Return the v2 heartbeat capability and generations when advertised.

        A wholly absent triplet is the pre-v2 EA and remains readable during a
        staged rollout.  A partial or malformed triplet is not a compatible
        heartbeat: accepting it could turn an unacknowledged full-history
        request into a false ``connected`` state.
        """

        fields = (
            "source_recovery_protocol_version",
            "source_recovery_request_generation",
            "source_recovery_ack_generation",
        )
        present = tuple(field in payload for field in fields)
        if not any(present):
            if require_v2:
                raise Mql5FileAdapterError("source_recovery_protocol_unsupported")
            return None
        if not all(present):
            raise Mql5FileAdapterError("heartbeat_schema_invalid")
        version = payload[fields[0]]
        request_generation = payload[fields[1]]
        ack_generation = payload[fields[2]]
        if (
            not self._is_source_recovery_protocol_version(version)
            or not self._is_source_recovery_generation(request_generation)
            or not self._is_source_recovery_generation(ack_generation)
        ):
            raise Mql5FileAdapterError("source_recovery_protocol_unsupported")
        assert isinstance(request_generation, int)
        assert isinstance(ack_generation, int)
        return int(version), request_generation, ack_generation

    def _source_recovery_connection_status(
        self, payload: dict[str, Any]
    ) -> tuple[bool, int, int, int]:
        """Return recovery-required plus externally visible protocol metadata.

        The direct request read is deliberately part of the health decision.
        The EA may have emitted a clean heartbeat just before Windows replaces
        its owned request file.  In that interleaving the request generation
        exceeds the heartbeat acknowledgement and this adapter must keep the
        existing supervisor recovery gate closed.
        """

        heartbeat_v2 = self._source_recovery_heartbeat_v2(payload)
        request = self._read_source_recovery_request_v2()
        ack = self._read_source_recovery_ack_v2()

        if heartbeat_v2 is None:
            # A v2 control file must never be sent to an EA that did not
            # advertise the matching acknowledgement fields.  Do not quietly
            # downgrade such a mixed rollout to the racy v1 marker.
            if request is not None or ack is not None:
                raise Mql5FileAdapterError("source_recovery_protocol_unsupported")
            legacy = self._read_legacy_source_recovery_marker()
            return (
                bool(payload["source_recovery_required"]) or legacy is not None,
                1,
                0,
                0,
            )

        protocol_version, heartbeat_request, heartbeat_ack = heartbeat_v2
        recovery_required = bool(payload["source_recovery_required"])
        if heartbeat_request != heartbeat_ack:
            recovery_required = True

        if request is None:
            # Before the first external request the zero/zero heartbeat is a
            # valid clean state.  Once a nonzero generation exists, deleting
            # the Windows-owned request is itself an unsafe protocol break.
            if heartbeat_request != 0 or heartbeat_ack != 0:
                recovery_required = True
            if ack is not None and ack["generation"] != heartbeat_ack:
                recovery_required = True
            if ack is not None and ack["generation"] != 0:
                recovery_required = True
            return (
                recovery_required,
                protocol_version,
                heartbeat_request,
                heartbeat_ack,
            )

        request_generation = request["generation"]
        # A request has been durable at this path before this read.  It is
        # resolved only when the *same* generation has made it to the EA
        # heartbeat and the EA's separately owned durable acknowledgement.
        # In particular, comparing directly with ``heartbeat_ack`` closes the
        # write-after-EA-final-read / before-heartbeat micro-race.
        if (
            request_generation != heartbeat_request
            or request_generation != heartbeat_ack
            or ack is None
            or ack["generation"] != heartbeat_ack
        ):
            recovery_required = True
        return (
            recovery_required,
            protocol_version,
            heartbeat_request,
            heartbeat_ack,
        )

    def _source_recovery_generation_state(self) -> int:
        """Return the Windows-owned last allocated generation, fail closed."""

        path = self.state_dir / _SOURCE_RECOVERY_GENERATION_STATE
        try:
            if path.is_symlink():
                raise Mql5FileAdapterError("source_recovery_generation_state_invalid")
            if not path.exists():
                return 0
            value = read_json(path)
        except Mql5FileAdapterError:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise Mql5FileAdapterError(
                "source_recovery_generation_state_invalid"
            ) from exc
        generation = value.get("last_generation")
        if (
            not isinstance(value.get("schema_version"), int)
            or isinstance(value.get("schema_version"), bool)
            or value.get("schema_version")
            != _SOURCE_RECOVERY_GENERATION_STATE_SCHEMA_VERSION
            or value.get("connection_id") != self.connection_id
            or not self._is_source_recovery_generation(generation)
        ):
            raise Mql5FileAdapterError("source_recovery_generation_state_invalid")
        assert isinstance(generation, int)
        return generation

    def _save_source_recovery_generation_state(self, generation: int) -> None:
        if not self._is_source_recovery_generation(generation, minimum=1):
            raise Mql5FileAdapterError("source_recovery_generation_exhausted")
        try:
            atomic_json(
                self.state_dir / _SOURCE_RECOVERY_GENERATION_STATE,
                {
                    "schema_version": _SOURCE_RECOVERY_GENERATION_STATE_SCHEMA_VERSION,
                    "connection_id": self.connection_id,
                    "last_generation": generation,
                },
            )
        except (OSError, ValueError) as exc:
            raise Mql5FileAdapterError(
                "source_recovery_generation_state_unavailable"
            ) from exc

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
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("terminal_connected"), bool)
            # This field is a release capability gate. Accepting an old EA
            # that cannot report journal continuity would let it advertise a
            # false healthy connection during a mixed fleet rollout.
            or not isinstance(payload.get("source_recovery_required"), bool)
        ):
            raise Mql5FileAdapterError("heartbeat_schema_invalid")
        # Validate a v2 triplet whenever it is advertised.  A legacy heartbeat
        # has none of these keys and stays readable until the guarded rollout
        # upgrades that EA; a partial triplet is never a healthy downgrade.
        self._source_recovery_heartbeat_v2(payload)
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
        # Keep the legacy terminal-info façade aligned with connection_state:
        # callers that use this older API must not bypass a v2 request that
        # landed after the EA's last heartbeat read.
        return SimpleNamespace(connected=self.connection_state()["connected"])

    def connection_state(self) -> dict[str, Any]:
        envelope, payload = self._heartbeat_envelope()
        (
            source_recovery_required,
            protocol_version,
            request_generation,
            ack_generation,
        ) = self._source_recovery_connection_status(payload)
        return {
            # A terminal may still be socket-connected while its source journal
            # has an unresolved gap. It must not be advertised as remotely
            # active until the EA's authoritative replay clears that flag.
            "connected": bool(payload["terminal_connected"]) and not source_recovery_required,
            "sequence": int(envelope["sequence"]),
            "source_recovery_required": source_recovery_required,
            # These are protocol capability/diagnostic fields.  Legacy v1
            # heartbeats expose 1/0/0; v2 values are validated above and are
            # never inferred from the Windows request file.
            "source_recovery_protocol_version": protocol_version,
            "source_recovery_request_generation": request_generation,
            "source_recovery_ack_generation": ack_generation,
        }

    def account_snapshot(self) -> dict[str, Any]:
        account = self._account()
        return {
            "balance": float(account["balance"]),
            "equity": float(account["equity"]),
            "currency": str(account["currency"]).upper(),
            "leverage": int(account["leverage"]),
        }

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

    def _require_history_sequence(self, envelope: dict[str, Any]) -> dict[str, Any]:
        """Reject a historical ledger not committed with its account anchor."""
        heartbeat, _ = self._heartbeat_envelope()
        account, account_payload = self._read_envelope("account.json")
        if len({envelope["sequence"], heartbeat["sequence"], account["sequence"]}) != 1:
            raise Mql5FileAdapterError("history_snapshot_sequence_mismatch")
        if not isinstance(account_payload, dict):
            raise Mql5FileAdapterError("account_schema_invalid")
        return account_payload

    def _validate_history_high_water(
        self,
        envelope: dict[str, Any],
        anchor: dict[str, Any] | None,
        account: dict[str, Any],
    ) -> None:
        """Reject a reused generation number with different ledger economics.

        The EA reserves the shared cursor before its snapshot bundle; this
        local high-water is defense in depth for an older/external producer.
        A same-sequence account/anchor change would otherwise pass simple
        sequence alignment and corrupt a reconstructed opening balance.
        """
        if anchor is None or anchor.get("coherent") is not True:
            return
        sequence = envelope["sequence"]
        # A coherent EA bundle is certified only if the ledger's end anchor
        # and the account snapshot describe the same terminal balance/credit.
        # Do this before consulting the local high-water file: otherwise the
        # *first* corrupted bundle at a new sequence could be accepted and
        # persisted as a trusted opening-balance basis.
        def _finite_number(value: object) -> float | None:
            if isinstance(value, bool):
                return None
            try:
                parsed = float(value)
            except (TypeError, ValueError, OverflowError):
                return None
            return parsed if math.isfinite(parsed) else None

        anchor_balance = _finite_number(anchor.get("balance"))
        anchor_credit = _finite_number(anchor.get("credit"))
        account_balance = _finite_number(account.get("balance"))
        account_credit = _finite_number(account.get("credit", 0))
        if (
            anchor_balance is None
            or anchor_credit is None
            or account_balance is None
            or account_credit is None
            or not math.isclose(anchor_balance, account_balance, rel_tol=0.0, abs_tol=1e-6)
            or not math.isclose(anchor_credit, account_credit, rel_tol=0.0, abs_tol=1e-6)
        ):
            raise Mql5FileAdapterError("history_anchor_account_mismatch")
        signature_fields = {
            "sequence": sequence,
            "anchor_balance": anchor.get("balance"),
            "anchor_credit": anchor.get("credit"),
            "anchor_count": anchor.get("deal_count"),
            "anchor_last_ticket": anchor.get("last_deal_ticket"),
            "anchor_last_time_msc": anchor.get("last_deal_time_msc"),
            "account_balance": account.get("balance"),
            "account_credit": account.get("credit", 0),
        }
        try:
            signature = json.dumps(
                signature_fields, sort_keys=True, separators=(",", ":"), allow_nan=False
            )
        except (TypeError, ValueError) as exc:
            raise Mql5FileAdapterError("history_anchor_schema_invalid") from exc
        prior = read_json(self.history_high_water_path, {})
        if prior:
            if (
                prior.get("connection_id") != self.connection_id
                or not isinstance(prior.get("sequence"), int)
                or not isinstance(prior.get("signature"), str)
            ):
                raise Mql5FileAdapterError("history_high_water_invalid")
            prior_sequence = prior["sequence"]
            if sequence < prior_sequence:
                raise Mql5FileAdapterError("history_snapshot_sequence_regressed")
            if sequence == prior_sequence and signature != prior["signature"]:
                raise Mql5FileAdapterError("history_snapshot_sequence_reused")
            if sequence == prior_sequence:
                return
        self.history_high_water_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(
            self.history_high_water_path,
            {
                "connection_id": self.connection_id,
                "sequence": sequence,
                "signature": signature,
            },
        )

    def _deal_rows(
        self,
    ) -> tuple[dict[str, Any], dict[str, Any] | None, list[dict[str, Any]]]:
        """Read an anchored ledger while retaining old list payload compatibility."""
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
        # A coherent anchor also certifies the ordering metadata.  Do not fail
        # the whole import for a corrupt producer snapshot: mark it unusable so
        # reconstruction emits explicit N/D values instead of an estimate.
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

    @staticmethod
    def _positions_by_stable_id(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """Key snapshots like MT5 deal history: by POSITION_IDENTIFIER.

        ``DEAL_POSITION_ID`` is the stable logical position identifier and can
        differ from the mutable position ticket after broker-side operations.
        Old EA payloads lack ``position_id`` and remain compatible through the
        ticket fallback.
        """
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            key = row.get("position_id") or row.get("ticket")
            if key not in (None, "", 0, "0"):
                result[str(key)] = row
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
                # The activation baseline intentionally contains no deal
                # rows: a frozen full history archive, not the live snapshot,
                # owns those lifecycles. Exclude its immutable membership
                # before snapshot reconciliation so the first N+1 poll cannot
                # recreate every archived deal as ``deal_recorded``.
                archived_deal_tickets, _archived_order_tickets = (
                    self._archived_history_tickets()
                )
                mapped_deals = {
                    ticket: deal
                    for ticket, deal in mapped_deals.items()
                    if str(ticket) not in archived_deal_tickets
                }
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

    def history_orders_bundle(
        self, start: datetime, end: datetime
    ) -> tuple[tuple[dict[str, Any], ...], int]:
        self.verify_identity()
        envelope, rows = self._rows("history_orders.json")
        self._require_history_sequence(envelope)
        all_available = self._all_available_window(start)
        result = []
        for row in rows:
            moment = self._parse_time(row.get("time", row.get("close_time")))
            if moment is not None and (all_available or start <= moment < end):
                result.append(row)
        return tuple(result), int(envelope["sequence"])

    def history_orders(self, start: datetime, end: datetime) -> tuple[dict[str, Any], ...]:
        rows, _sequence = self.history_orders_bundle(start, end)
        return rows

    def history_deals(self, start: datetime, end: datetime) -> tuple[dict[str, Any], ...]:
        self.verify_identity()
        envelope, anchor, rows = self._deal_rows()
        account = self._require_history_sequence(envelope)
        self._validate_history_high_water(envelope, anchor, account)
        projected = reconstruct_trade_deals(rows, anchor) if anchor is not None else tuple(rows)
        all_available = self._all_available_window(start)
        return tuple(
            row
            for row in projected
            if row.get("project_as_trade", True) is True
            and (moment := self._parse_time(row.get("time", row.get("close_time")))) is not None
            and (all_available or start <= moment < end)
        )

    def history_ledger_bundle(
        self,
    ) -> tuple[dict[str, Any] | None, tuple[dict[str, Any], ...], int]:
        """Return the certified ledger and its immutable producer sequence."""
        self.verify_identity()
        envelope, anchor, rows = self._deal_rows()
        account = self._require_history_sequence(envelope)
        self._validate_history_high_water(envelope, anchor, account)
        return anchor, tuple(rows), int(envelope["sequence"])

    def history_ledger(self) -> tuple[dict[str, Any] | None, tuple[dict[str, Any], ...]]:
        """Return the complete ordered ledger and its certified end anchor.

        This intentionally does not filter by the user-facing date window: a
        deposit, credit, fee or overlapping position outside that window can be
        necessary to prove the balance before its first trade.
        """
        anchor, rows, _sequence = self.history_ledger_bundle()
        return anchor, rows

    def history_anchor(self) -> dict[str, Any] | None:
        anchor, _ = self.history_ledger()
        return anchor

    def history_balance_rows(self) -> tuple[dict[str, Any], ...]:
        anchor, rows = self.history_ledger()
        return reconstruct_trade_deals(rows, anchor)

    def _archived_history_tickets(self) -> tuple[frozenset[str], frozenset[str]]:
        """Read immutable deal/order history membership, never infer it by time."""
        active = read_json(self.history_handoff_path, {})
        if not active:
            return frozenset(), frozenset()
        tickets = active.get("archived_deal_tickets")
        order_tickets = active.get("archived_order_tickets")
        if (
            active.get("schema_version") != 1
            or active.get("connection_id") != self.connection_id
            or not isinstance(active.get("acknowledge_prefix"), bool)
            or not isinstance(tickets, list)
            or not isinstance(order_tickets, list)
            or any(
                not isinstance(ticket, str)
                or not re.fullmatch(r"[0-9]{1,32}", ticket)
                for ticket in tickets
            )
            or any(
                not isinstance(ticket, str)
                or not re.fullmatch(r"[0-9]{1,32}", ticket)
                for ticket in order_tickets
            )
        ):
            raise Mql5FileAdapterError("history_handoff_invalid")
        return frozenset(tickets), frozenset(order_tickets)

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
        archived_deal_tickets, archived_order_tickets = self._archived_history_tickets()
        self._prune_acknowledged_event_files(events_dir, last_sequence)
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
            event_type = str(event.get("event_type", "")).upper()
            if (
                event_type == "DEAL_ADD"
                and str(event.get("deal_id") or event.get("ticket") or "")
                in archived_deal_tickets
            ):
                event["history_archived"] = True
            elif (
                event_type in ("ORDER_ADD", "ORDER_UPDATE", "HISTORY_ADD", "HISTORY_FILLED")
                and str(event.get("order_id") or event.get("ticket") or "")
                in archived_order_tickets
            ):
                event["history_archived"] = True
            pending.append((sequence, event))
        pending.sort(key=lambda item: item[0])
        return tuple(payload for _, payload in pending)

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

    def request_certified_history_recovery(self) -> None:
        """Ask the already-running EA to replay its full authoritative ledger.

        A reduced position in a periodic snapshot has no reliable close deal
        or economics.  ``from_unix=0`` is deliberate: a guessed recent cutoff
        could omit the predecessor required to classify a delayed partial
        close.  Unlike v1, Windows never writes the EA-owned continuity state
        and the EA never deletes this request path.  A v2-capable EA proves
        completion by matching the same generation in its ack and heartbeat.
        """

        if self.files_dir.is_symlink() or not self.files_dir.is_dir():
            raise Mql5FileAdapterError("source_recovery_directory_invalid")
        _envelope, heartbeat_payload = self._heartbeat_envelope()
        heartbeat_v2 = self._source_recovery_heartbeat_v2(heartbeat_payload)
        request = self._read_source_recovery_request_v2()
        ack = self._read_source_recovery_ack_v2()

        if heartbeat_v2 is None:
            # Do not recreate the v1 same-path race during a staged rollout.
            # An inherited *full* v1 request is already safe to let the old EA
            # finish; otherwise retain the original LiveSync source prefix and
            # retry after the guarded EA upgrade advertises v2 capability.
            legacy = self._read_legacy_source_recovery_marker()
            if legacy is not None and legacy["from_unix"] == 0:
                return
            raise Mql5FileAdapterError("source_recovery_protocol_unsupported")

        _protocol_version, heartbeat_request, heartbeat_ack = heartbeat_v2

        generations = [
            self._source_recovery_generation_state(),
            heartbeat_request,
            heartbeat_ack,
        ]
        if request is not None:
            generations.append(request["generation"])
        if ack is not None:
            generations.append(ack["generation"])
        generation = max(generations)
        if generation >= _MAX_SOURCE_RECOVERY_GENERATION:
            raise Mql5FileAdapterError("source_recovery_generation_exhausted")
        generation += 1

        marker = self.files_dir / _SOURCE_RECOVERY_REQUEST_V2
        if marker.is_symlink():
            raise Mql5FileAdapterError("source_recovery_request_invalid")
        try:
            # Publish the durable externally-owned request before the local
            # counter checkpoint.  If the agent dies in between, the request
            # itself remains the authoritative high-water generation; a later
            # retry can safely recover from it without reusing N.
            #
            # Do advance even if the previous generation is still pending.
            # A second reduced snapshot can be discovered after the EA has
            # already captured N for its replay; N+1 is the durable signal
            # that makes its final request reread schedule a second pass.
            atomic_json(
                marker,
                {
                    "protocol_version": _SOURCE_RECOVERY_PROTOCOL_VERSION,
                    "generation": generation,
                    "from_unix": 0,
                },
            )
        except (OSError, ValueError) as exc:
            raise Mql5FileAdapterError("source_recovery_request_unavailable") from exc
        self._save_source_recovery_generation_state(generation)

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
