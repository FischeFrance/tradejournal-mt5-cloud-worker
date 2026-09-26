"""Executable durability contract for TradeJournalBridge's MQL5 source journal.

MQL5 cannot run in the non-Windows CI image, so this deliberately small model
uses the *same persisted file names and JSON fields* as the EA.  Static EA
tests pin the corresponding control-flow tokens; these fault-injection tests
exercise the state transitions that must remain true across a process crash.
"""

from __future__ import annotations

import json
from dataclasses import dataclass


CURSOR = "cursor.json"
PENDING = "pending-events.json"
RECOVERY = "source-recovery-required.json"
RECOVERY_STATE_V2 = "source-recovery-state-v2.json"
RECOVERY_REQUEST_V2 = "source-recovery-request-v2.json"
RECOVERY_ACK_V2 = "source-recovery-ack-v2.json"
RECOVERY_PROTOCOL_V2 = 2
PENDING_DEAL_ADD = 1
PENDING_ORDER_ADD = 2
PENDING_ORDER_UPDATE = 3
PENDING_POSITION = 4
PENDING_HISTORY_ORDER = 5
_PENDING_KINDS = frozenset(
    {
        PENDING_DEAL_ADD,
        PENDING_ORDER_ADD,
        PENDING_ORDER_UPDATE,
        PENDING_POSITION,
        PENDING_HISTORY_ORDER,
    }
)


class _FaultyFiles:
    def __init__(self) -> None:
        self.files: dict[str, str] = {}
        self.fail_writes: set[str] = set()
        self.fail_deletes: set[str] = set()
        self.unreadable: set[str] = set()

    def write_json(self, name: str, value: object) -> bool:
        if name in self.fail_writes:
            return False
        self.files[name] = json.dumps(value, sort_keys=True)
        return True

    def read_json(self, name: str) -> object:
        if name in self.unreadable:
            raise OSError("simulated locked file")
        return json.loads(self.files[name])

    def exists(self, name: str) -> bool:
        return name in self.files

    def delete(self, name: str) -> bool:
        if name in self.fail_deletes:
            return False
        self.files.pop(name, None)
        return True


@dataclass(frozen=True)
class _Pending:
    kind: int
    ticket: int
    attempts: int = 0


class _Mql5SourceJournal:
    """A narrow model of Queue/BuildEventJson/RetryPendingEvents persistence.

    It intentionally has no wall-clock six-hour fallback: ``0`` is the
    full-history sentinel and all other recovery cutoffs are monotonic minima.
    """

    def __init__(self, files: _FaultyFiles, *, now: int, limit: int = 256) -> None:
        self.files = files
        self.now = now
        self.limit = limit
        self.event_seq = 0
        self.watermark: int | None = None
        self.pending: list[_Pending] = []
        self.recovery_required = False
        self.recovery_from: int | None = None
        self.recovery_marker_persisted = False
        self.pending_load_failed = False
        self.cursor_valid = False
        self.cursor_present = False
        self.pending_file_present = False

    @property
    def healthy(self) -> bool:
        return (
            self.cursor_valid
            and self.watermark is not None
            and not self.recovery_required
            and not self.pending
        )

    @property
    def heartbeat_source_recovery_required(self) -> bool:
        """Mirror BuildHeartbeatJson's strict readiness capability bit."""
        return self.recovery_required or bool(self.pending)

    def _save_cursor(self) -> bool:
        if self.watermark is None:
            return False
        saved = self.files.write_json(
            CURSOR,
            {
                "event_seq": self.event_seq,
                "backfill_done": False,
                "source_watermark_unix": self.watermark,
                "continuity_version": 1,
            },
        )
        if saved:
            self.cursor_present = True
            self.cursor_valid = True
        return saved

    def _save_pending(self) -> bool:
        return self.files.write_json(
            PENDING,
            {
                "events": [
                    {"kind": item.kind, "ticket": str(item.ticket), "attempts": item.attempts}
                    for item in self.pending
                ]
            },
        )

    def _persist_recovery(self) -> bool:
        assert self.recovery_from is not None
        saved = self.files.write_json(
            RECOVERY,
            {"from_unix": self.recovery_from, "continuity_version": 1},
        )
        self.recovery_marker_persisted = saved
        return saved

    def require_recovery(self, requested_from: int) -> None:
        # Unknown/corrupt source always means full history. Never replace a
        # prior full sentinel with a later watermark.
        requested_from = max(0, requested_from)
        if requested_from == 0:
            earliest = 0
        elif self.recovery_from is None:
            earliest = requested_from
        elif self.recovery_from == 0:
            earliest = 0
        else:
            earliest = min(self.recovery_from, requested_from)
        self.recovery_required = True
        self.recovery_from = earliest
        self._persist_recovery()

    def refresh_recovery_marker(self) -> None:
        """Mirror the timer's repeated marker read while already latched."""
        marker = self.files.read_json(RECOVERY)
        cutoff = marker.get("from_unix") if isinstance(marker, dict) else None
        if type(cutoff) is not int or cutoff < 0:
            self.require_recovery(0)
            return
        self.require_recovery(cutoff)

    def boot(self) -> bool:
        """Load persisted evidence and decide whether bootstrap is safe."""
        cursor_bad = False
        if self.files.exists(CURSOR):
            self.cursor_present = True
            try:
                cursor = self.files.read_json(CURSOR)
                if not isinstance(cursor, dict):
                    raise ValueError("cursor object")
                sequence = cursor.get("event_seq")
                watermark = cursor.get("source_watermark_unix")
                if (
                    type(sequence) is not int
                    or sequence < 0
                    or type(watermark) is not int
                    or watermark <= 0
                ):
                    raise ValueError("cursor fields")
                self.event_seq = sequence
                self.watermark = watermark
                self.cursor_valid = True
            except (OSError, ValueError, json.JSONDecodeError):
                cursor_bad = True

        if self.files.exists(PENDING):
            self.pending_file_present = True
            try:
                value = self.files.read_json(PENDING)
                events = value["events"] if isinstance(value, dict) else None
                if not isinstance(events, list):
                    raise ValueError("events")
                loaded: list[_Pending] = []
                for event in events:
                    if not isinstance(event, dict):
                        raise ValueError("event")
                    kind, ticket = event.get("kind"), event.get("ticket")
                    if type(kind) is not int or kind not in _PENDING_KINDS:
                        raise ValueError("kind")
                    parsed_ticket = int(str(ticket))
                    if parsed_ticket <= 0:
                        raise ValueError("ticket")
                    loaded.append(_Pending(kind, parsed_ticket, int(event.get("attempts", 0))))
                if len(loaded) > self.limit:
                    raise ValueError("overflow")
                self.pending = loaded
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                # Match EA quarantine: preserve the file for forensics, latch
                # authoritative recovery, but never retry a parsed prefix.
                self.pending_load_failed = True
                self.pending = []

        if self.files.exists(RECOVERY):
            try:
                marker = self.files.read_json(RECOVERY)
                cutoff = marker.get("from_unix") if isinstance(marker, dict) else None
                if type(cutoff) is not int or cutoff < 0:
                    raise ValueError("marker")
                self.recovery_required = True
                self.recovery_from = cutoff
                self.recovery_marker_persisted = True
            except (OSError, ValueError, json.JSONDecodeError):
                self.require_recovery(0)

        if cursor_bad or self.pending_load_failed:
            self.require_recovery(0)
        elif not self.cursor_present and not self.pending_file_present and not self.recovery_required:
            # Truly pristine new_only directory: persist the initial boundary
            # before it can ever be considered healthy.
            self.watermark = self.now
            if not self._save_cursor():
                self.watermark = None
                self.require_recovery(0)
        elif not self.cursor_valid or self.watermark is None:
            self.require_recovery(0)
        elif not self.recovery_required:
            # Cold restart starts from the old durable boundary; it remains
            # unhealthy until the authoritative replay commits a new one.
            self.require_recovery(self.watermark)

        # This matches OnInit's bootstrap fail-closed rule. A process without
        # cursor/marker evidence must not survive long enough to look pristine
        # after a crash.
        return not (
            not self.cursor_present
            and not self.cursor_valid
            and self.recovery_required
            and not self.recovery_marker_persisted
        )

    def queue(self, kind: int, ticket: int) -> bool:
        if ticket <= 0:
            self.require_recovery(self.watermark or 0)
            return False
        if kind not in _PENDING_KINDS:
            self.require_recovery(self.watermark or 0)
            return False
        item = _Pending(kind, ticket)
        # MQL FindPendingEvent deduplicates only the immutable callback
        # identity. ``attempts`` is mutable retry metadata and must never turn
        # a reentrant callback into a second source queue entry.
        if any(
            existing.kind == kind and existing.ticket == ticket
            for existing in self.pending
        ):
            return True
        if len(self.pending) >= self.limit:
            self.require_recovery(self.watermark or 0)
            return False
        self.pending.append(item)
        if self._save_pending():
            return True
        self.pending.pop()
        self.require_recovery(self.watermark or 0)
        return False

    def _emit_head(self, can_emit) -> bool:
        if not self.pending or not can_emit(self.pending[0]):
            return False
        head = self.pending[0]
        self.event_seq += 1
        if not self._save_cursor():
            return False
        return self.files.write_json(
            f"events/event-{self.event_seq}.json",
            {"source_event_id": f"{head.kind}:{head.ticket}", "ticket": str(head.ticket)},
        )

    def drain_fifo(self, can_emit) -> None:
        while self.pending:
            if not self._emit_head(can_emit):
                return
            head = self.pending.pop(0)
            if self._save_pending():
                continue
            # MQL restores the entry after a failed RemovePendingEvent write;
            # duplicate-safe replay is preferable to dropping the source.
            self.pending.insert(0, head)
            return

    def publish(self, kind: int, ticket: int, can_emit) -> bool:
        if not self.queue(kind, ticket):
            return False
        # Same centralized prefix drain for callbacks and timer retries.
        self.drain_fifo(can_emit)
        return not any(
            existing.kind == kind and existing.ticket == ticket
            for existing in self.pending
        )

    def authoritative_recovery_complete(self, *, now: int) -> bool:
        if self.pending:
            return False
        self.watermark = now
        if not self._save_cursor():
            self.require_recovery(0)
            return False
        if RECOVERY in self.files.files and not self.files.delete(RECOVERY):
            # The cursor is newer, but the durable unresolved marker remains
            # authoritative. Match EstablishSourceContinuityFromHistory:
            # never claim a healthy bridge until a later deletion succeeds.
            self.recovery_required = True
            self.recovery_marker_persisted = True
            return False
        self.recovery_required = False
        self.recovery_from = None
        self.recovery_marker_persisted = False
        self.pending_load_failed = False
        return True


def _booted(files: _FaultyFiles, *, now: int = 1_000) -> _Mql5SourceJournal:
    journal = _Mql5SourceJournal(files, now=now)
    assert journal.boot()
    assert journal.healthy
    return journal


def test_event_write_failure_then_crash_restarts_and_reemits_same_source() -> None:
    files = _FaultyFiles()
    journal = _booted(files)
    files.fail_writes.add("events/event-1.json")
    assert not journal.publish(PENDING_DEAL_ADD, 101, lambda _head: True)
    assert [item.ticket for item in journal.pending] == [101]
    assert files.exists(CURSOR)  # reservation survived before the failed write

    files.fail_writes.clear()
    restarted = _Mql5SourceJournal(files, now=1_100)
    assert restarted.boot()
    assert [item.ticket for item in restarted.pending] == [101]
    restarted.drain_fifo(lambda _head: True)
    emitted = [
        json.loads(value)["source_event_id"]
        for path, value in sorted(files.files.items())
        if path.startswith("events/event-")
    ]
    assert emitted == ["1:101"]
    assert restarted.pending == []


def test_failed_queue_removal_replays_duplicate_source_without_dropping_it() -> None:
    files = _FaultyFiles()
    journal = _booted(files)
    assert journal.queue(PENDING_DEAL_ADD, 101)
    files.fail_writes.add(PENDING)
    journal.drain_fifo(lambda _head: True)
    assert [item.ticket for item in journal.pending] == [101]
    files.fail_writes.clear()

    restarted = _Mql5SourceJournal(files, now=1_100)
    assert restarted.boot()
    restarted.drain_fifo(lambda _head: True)
    emitted = [
        json.loads(value)["source_event_id"]
        for path, value in sorted(files.files.items())
        if path.startswith("events/event-")
    ]
    assert emitted == ["1:101", "1:101"]
    assert restarted.pending == []


def test_fifo_prefix_blocks_ready_close_until_unavailable_open_recovers_across_restart() -> None:
    files = _FaultyFiles()
    journal = _booted(files)
    availability = {101: False, 102: True}
    assert not journal.publish(PENDING_DEAL_ADD, 101, lambda head: availability[head.ticket])
    # A ready close is only durably queued behind its unavailable predecessor;
    # it cannot be directly emitted by the callback path.
    assert not journal.publish(PENDING_DEAL_ADD, 102, lambda head: availability[head.ticket])
    assert not [path for path in files.files if path.startswith("events/event-")]
    # A durable queue is not a full-history-loss marker, but it must still
    # make the heartbeat ineligible for supervisor/V2 acknowledgement.
    assert journal.recovery_required is False
    assert journal.heartbeat_source_recovery_required is True
    assert not journal.healthy

    restarted = _Mql5SourceJournal(files, now=1_100)
    assert restarted.boot()
    availability[101] = True
    restarted.drain_fifo(lambda head: availability[head.ticket])
    emitted = [
        json.loads(value)["source_event_id"]
        for path, value in sorted(files.files.items())
        if path.startswith("events/event-")
    ]
    assert emitted == ["1:101", "1:102"]
    # A cold restart replays from its durable watermark even after this queue
    # drains, so the explicit marker remains until authoritative history has
    # committed a replacement boundary.
    assert restarted.heartbeat_source_recovery_required is True
    assert restarted.authoritative_recovery_complete(now=1_101)
    assert restarted.heartbeat_source_recovery_required is False


def test_queue_failure_overflow_and_zero_ticket_latch_oldest_recovery_cutoff() -> None:
    files = _FaultyFiles()
    journal = _booted(files, now=100)
    # More than six hours later, the durable earliest point remains 100; it is
    # never replaced with a recent rolling window.
    journal.now = 100 + 7 * 60 * 60
    files.fail_writes.add(PENDING)
    assert not journal.queue(PENDING_DEAL_ADD, 101)
    assert journal.recovery_required is True
    assert journal.recovery_from == 100
    assert json.loads(files.files[RECOVERY])["from_unix"] == 100

    overflow_files = _FaultyFiles()
    overflow = _booted(overflow_files, now=500)
    overflow.limit = 1
    assert overflow.queue(PENDING_DEAL_ADD, 101)
    assert not overflow.queue(PENDING_DEAL_ADD, 102)
    assert overflow.recovery_from == 500
    assert not overflow.queue(PENDING_DEAL_ADD, 0)
    assert overflow.recovery_from == 500


def test_corrupt_prefix_is_quarantined_then_authoritative_recovery_makes_bridge_healthy() -> None:
    files = _FaultyFiles()
    files.files[PENDING] = (
        '{"events":[{"kind":1,"ticket":"101","attempts":0}],garbage}'
    )
    restarted = _Mql5SourceJournal(files, now=2_000)
    assert restarted.boot()
    assert restarted.pending == []
    assert restarted.pending_load_failed is True
    assert restarted.recovery_required is True
    assert restarted.recovery_from == 0
    assert json.loads(files.files[RECOVERY])["from_unix"] == 0
    assert restarted.authoritative_recovery_complete(now=2_100)
    assert restarted.healthy


def test_unreadable_pending_journal_is_not_treated_as_an_empty_queue() -> None:
    files = _FaultyFiles()
    files.files[PENDING] = '{"events":[]}'
    files.unreadable.add(PENDING)
    restarted = _Mql5SourceJournal(files, now=2_000)
    assert restarted.boot()
    assert restarted.pending == []
    assert restarted.pending_load_failed is True
    assert restarted.recovery_required is True
    assert restarted.recovery_from == 0
    assert not restarted.healthy


def test_fresh_boot_without_cursor_or_persisted_marker_fails_closed() -> None:
    files = _FaultyFiles()
    files.fail_writes.update({CURSOR, RECOVERY})
    journal = _Mql5SourceJournal(files, now=1_000)
    assert not journal.boot()
    assert journal.recovery_required is True
    assert journal.recovery_marker_persisted is False
    assert not journal.healthy


def test_cursor_reservation_failure_keeps_durable_head_for_restart_retry() -> None:
    files = _FaultyFiles()
    journal = _booted(files)
    files.fail_writes.add(CURSOR)

    # Queue persistence succeeds before BuildEventJson reserves event-N. A
    # failed reservation may advance only volatile memory; it must not remove
    # the source or publish an empty event file.
    assert not journal.publish(PENDING_DEAL_ADD, 101, lambda _head: True)
    assert [item.ticket for item in journal.pending] == [101]
    assert not [path for path in files.files if path.startswith("events/event-")]

    files.fail_writes.clear()
    restarted = _Mql5SourceJournal(files, now=1_100)
    assert restarted.boot()
    restarted.drain_fifo(lambda _head: True)
    emitted = [
        json.loads(value)["source_event_id"]
        for path, value in sorted(files.files.items())
        if path.startswith("events/event-")
    ]
    assert emitted == ["1:101"]
    assert restarted.pending == []


def test_reentrant_callback_deduplicates_numeric_kind_and_ticket_not_attempt_count() -> None:
    files = _FaultyFiles()
    journal = _booted(files)
    assert journal.queue(PENDING_DEAL_ADD, 101)
    journal.pending[0] = _Pending(PENDING_DEAL_ADD, 101, attempts=4)
    assert journal.queue(PENDING_DEAL_ADD, 101)
    assert journal.pending == [_Pending(PENDING_DEAL_ADD, 101, attempts=4)]


def test_existing_cursor_marker_write_failure_stays_unhealthy_across_crash() -> None:
    files = _FaultyFiles()
    journal = _booted(files, now=1_000)
    files.fail_writes.update({PENDING, RECOVERY})

    assert not journal.queue(PENDING_DEAL_ADD, 101)
    assert journal.recovery_required is True
    assert journal.recovery_marker_persisted is False
    assert not journal.healthy

    # The previous durable cursor is independent evidence: a process crash
    # cannot turn this loss into a pristine boot just because both new writes
    # failed once. It latches the same old watermark on the next boot.
    files.fail_writes.clear()
    restarted = _Mql5SourceJournal(files, now=9_000)
    assert restarted.boot()
    assert restarted.recovery_required is True
    assert restarted.recovery_from == 1_000
    assert restarted.recovery_marker_persisted is True
    assert not restarted.healthy


def test_recovery_marker_delete_failure_never_claims_clean_heartbeat() -> None:
    files = _FaultyFiles()
    journal = _booted(files, now=1_000)
    journal.require_recovery(1_000)
    assert files.exists(RECOVERY)
    files.fail_deletes.add(RECOVERY)

    assert not journal.authoritative_recovery_complete(now=1_100)
    assert journal.recovery_required is True
    assert journal.recovery_marker_persisted is True
    assert files.exists(RECOVERY)
    assert not journal.healthy

    files.fail_deletes.clear()
    assert journal.authoritative_recovery_complete(now=1_101)
    assert not files.exists(RECOVERY)
    assert journal.healthy


def test_latched_bounded_recovery_absorbs_later_full_history_request() -> None:
    files = _FaultyFiles()
    journal = _booted(files, now=1_000)
    journal.require_recovery(900)
    assert journal.recovery_from == 900

    # LiveSync can discover a stronger gap while the EA is already recovering
    # and atomically replace the marker with the full-history sentinel.
    assert files.write_json(RECOVERY, {"continuity_version": 1, "from_unix": 0})
    journal.refresh_recovery_marker()

    assert journal.recovery_required is True
    assert journal.recovery_from == 0
    assert json.loads(files.files[RECOVERY])["from_unix"] == 0


class _V2RecoveryProtocol:
    """Small ownership model for the v2 request/state/ack fence.

    It intentionally models the cross-process interleaving rather than MQL5
    syntax: Windows owns REQUEST, while the EA owns STATE and ACK.  No EA
    completion path has a delete operation on REQUEST.
    """

    def __init__(self, files: _FaultyFiles) -> None:
        self.files = files
        self.recovery_required = False
        self.request_generation = 0
        self.ack_generation = 0
        self.operations: list[str] = []

    @staticmethod
    def _control(value: object, *, request: bool) -> int:
        if not isinstance(value, dict):
            raise ValueError("control object")
        generation = value.get("generation")
        if (
            value.get("protocol_version") != RECOVERY_PROTOCOL_V2
            or value.get("from_unix") != 0
            or type(generation) is not int
            or generation < (1 if request else 0)
        ):
            raise ValueError("control fields")
        return generation

    def refresh(self) -> bool:
        """Return whether a full replay remains required, fail-closed."""
        try:
            ack = 0
            if self.files.exists(RECOVERY_ACK_V2):
                ack = self._control(
                    self.files.read_json(RECOVERY_ACK_V2), request=False
                )
            request = ack
            request_present = self.files.exists(RECOVERY_REQUEST_V2)
            if request_present:
                request = self._control(
                    self.files.read_json(RECOVERY_REQUEST_V2), request=True
                )
            elif ack != 0:
                # A request is Windows-owned and intentionally retained even
                # after acknowledgement.  A missing nonzero request is an
                # integrity fault, never a reason to reset the protocol.
                raise ValueError("acknowledged request missing")
            if request < ack:
                raise ValueError("generation regression")
        except (OSError, ValueError, json.JSONDecodeError):
            self.recovery_required = True
            return True

        self.request_generation = request
        self.ack_generation = ack
        self.recovery_required = request_present and request != ack
        return self.recovery_required

    def start_replay(self) -> int:
        assert self.refresh()
        return self.request_generation

    def complete_replay(self, captured_generation: int) -> bool:
        """Mirror event replay -> cursor/watermark -> state cleanup -> ACK."""
        self.operations.append("cursor")
        assert self.files.write_json(CURSOR, {"source_watermark_unix": 2_000})
        # Only EA STATE can be removed.  REQUEST remains Windows-owned even
        # when a newer request appears during this completion.
        self.operations.append("state-delete")
        assert self.files.delete(RECOVERY_STATE_V2)
        self.operations.append(f"ack:{captured_generation}")
        assert self.files.write_json(
            RECOVERY_ACK_V2,
            {
                "protocol_version": RECOVERY_PROTOCOL_V2,
                "generation": captured_generation,
                "from_unix": 0,
            },
        )
        # The required post-ack re-read is what exposes a request published
        # during the replay rather than letting a clean heartbeat hide it.
        return not self.refresh()


def _v2_request(files: _FaultyFiles, generation: int) -> None:
    assert files.write_json(
        RECOVERY_REQUEST_V2,
        {
            "protocol_version": RECOVERY_PROTOCOL_V2,
            "generation": generation,
            "from_unix": 0,
        },
    )


def test_v2_request_arriving_during_replay_survives_old_ack_and_forces_second_replay() -> None:
    files = _FaultyFiles()
    _v2_request(files, 1)
    files.write_json(RECOVERY_STATE_V2, {"from_unix": 0})
    journal = _V2RecoveryProtocol(files)

    captured = journal.start_replay()
    assert captured == 1
    # This is the historical marker-vs-delete interleaving.  In v2 it replaces
    # only the Windows request record; the EA has no deletion right to it.
    _v2_request(files, 2)
    assert not journal.complete_replay(captured)

    assert json.loads(files.files[RECOVERY_REQUEST_V2])["generation"] == 2
    assert json.loads(files.files[RECOVERY_ACK_V2])["generation"] == 1
    assert journal.recovery_required is True
    assert journal.start_replay() == 2
    assert journal.complete_replay(2)
    assert journal.recovery_required is False
    assert journal.operations.index("cursor") < journal.operations.index("ack:1")


def test_v2_corrupt_request_or_regressing_generation_never_becomes_healthy() -> None:
    files = _FaultyFiles()
    files.files[RECOVERY_REQUEST_V2] = "{broken"
    journal = _V2RecoveryProtocol(files)
    assert journal.refresh()
    assert journal.recovery_required is True

    files = _FaultyFiles()
    files.write_json(
        RECOVERY_ACK_V2,
        {
            "protocol_version": RECOVERY_PROTOCOL_V2,
            "generation": 1,
            "from_unix": 0,
        },
    )
    journal = _V2RecoveryProtocol(files)
    assert journal.refresh()
    assert journal.recovery_required is True

    files = _FaultyFiles()
    _v2_request(files, 4)
    files.write_json(
        RECOVERY_ACK_V2,
        {
            "protocol_version": RECOVERY_PROTOCOL_V2,
            "generation": 5,
            "from_unix": 0,
        },
    )
    journal = _V2RecoveryProtocol(files)
    assert journal.refresh()
    assert journal.recovery_required is True
