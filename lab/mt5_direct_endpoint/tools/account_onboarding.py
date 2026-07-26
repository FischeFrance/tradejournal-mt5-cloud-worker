"""Explicit, fail-closed first-login onboarding state machine for one account.

Mirrors the design of ``C012StateMachine.cs``: the transition table is the
single source of truth, built once from an explicit happy path plus universal
fail-closed edges from every non-terminal state. Nothing not in the table is
ever accepted, and accepting a transition never mutates state on rejection.

This module only models *first-login onboarding* -- creating an isolated
portable directory, resolving a VERIFIED broker endpoint, and validating one
login attempt. Normal runtime operation of an already-``ACTIVE`` account is
the worker/supervisor's concern (see ``account_worker.py`` /
``worker_supervisor.py``), not this state machine's.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import Any, Mapping, Sequence


class OnboardingState(Enum):
    NEW = auto()
    PORTABLE_DIRECTORY_CREATED = auto()
    BROKER_ENDPOINT_REQUIRED = auto()
    BROKER_ENDPOINT_VERIFIED = auto()
    CREDENTIALS_PENDING = auto()
    LOGIN_VALIDATION = auto()
    ACTIVE = auto()
    FAILED_CLOSED = auto()
    STOPPED = auto()


class OnboardingTrigger(Enum):
    CREATE_PORTABLE_DIRECTORY = auto()
    REQUIRE_BROKER_ENDPOINT = auto()
    ENDPOINT_VERIFIED = auto()
    SUPPLY_CREDENTIALS = auto()
    BEGIN_LOGIN_VALIDATION = auto()
    LOGIN_ACCEPTED = auto()
    STOP = auto()

    # Universal fail-closed triggers. Each maps 1:1 onto a FailureReason of
    # the same name, so applying one always records exactly why the account
    # failed closed -- callers cannot report a failure without saying which.
    MISSING_ENDPOINT = auto()
    UNREACHABLE_ENDPOINT = auto()
    WRONG_CREDENTIALS = auto()
    SERVER_REJECTED = auto()
    PROCESS_DIED = auto()
    TIMEOUT = auto()
    INVALID_CONFIG = auto()


class FailureReason(Enum):
    MISSING_ENDPOINT = auto()
    UNREACHABLE_ENDPOINT = auto()
    WRONG_CREDENTIALS = auto()
    SERVER_REJECTED = auto()
    PROCESS_DIED = auto()
    TIMEOUT = auto()
    INVALID_CONFIG = auto()


_FAILURE_TRIGGERS: dict[OnboardingTrigger, FailureReason] = {
    OnboardingTrigger.MISSING_ENDPOINT: FailureReason.MISSING_ENDPOINT,
    OnboardingTrigger.UNREACHABLE_ENDPOINT: FailureReason.UNREACHABLE_ENDPOINT,
    OnboardingTrigger.WRONG_CREDENTIALS: FailureReason.WRONG_CREDENTIALS,
    OnboardingTrigger.SERVER_REJECTED: FailureReason.SERVER_REJECTED,
    OnboardingTrigger.PROCESS_DIED: FailureReason.PROCESS_DIED,
    OnboardingTrigger.TIMEOUT: FailureReason.TIMEOUT,
    OnboardingTrigger.INVALID_CONFIG: FailureReason.INVALID_CONFIG,
}


class RejectionReason(Enum):
    TERMINAL_STATE = auto()
    ILLEGAL_TRANSITION = auto()


@dataclass(frozen=True)
class Transition:
    from_state: OnboardingState
    trigger: OnboardingTrigger
    to_state: OnboardingState


@dataclass(frozen=True)
class TransitionResult:
    accepted: bool
    state: OnboardingState
    failure_reason: FailureReason | None = None
    rejection_reason: RejectionReason | None = None

    @staticmethod
    def accept(state: OnboardingState, failure_reason: FailureReason | None = None) -> "TransitionResult":
        return TransitionResult(accepted=True, state=state, failure_reason=failure_reason)

    @staticmethod
    def reject(state: OnboardingState, rejection_reason: RejectionReason) -> "TransitionResult":
        return TransitionResult(accepted=False, state=state, rejection_reason=rejection_reason)


TERMINAL_STATES = frozenset({OnboardingState.FAILED_CLOSED, OnboardingState.STOPPED})

_HAPPY_PATH: tuple[Transition, ...] = (
    Transition(OnboardingState.NEW, OnboardingTrigger.CREATE_PORTABLE_DIRECTORY, OnboardingState.PORTABLE_DIRECTORY_CREATED),
    Transition(OnboardingState.PORTABLE_DIRECTORY_CREATED, OnboardingTrigger.REQUIRE_BROKER_ENDPOINT, OnboardingState.BROKER_ENDPOINT_REQUIRED),
    Transition(OnboardingState.BROKER_ENDPOINT_REQUIRED, OnboardingTrigger.ENDPOINT_VERIFIED, OnboardingState.BROKER_ENDPOINT_VERIFIED),
    Transition(OnboardingState.BROKER_ENDPOINT_VERIFIED, OnboardingTrigger.SUPPLY_CREDENTIALS, OnboardingState.CREDENTIALS_PENDING),
    Transition(OnboardingState.CREDENTIALS_PENDING, OnboardingTrigger.BEGIN_LOGIN_VALIDATION, OnboardingState.LOGIN_VALIDATION),
    Transition(OnboardingState.LOGIN_VALIDATION, OnboardingTrigger.LOGIN_ACCEPTED, OnboardingState.ACTIVE),
)


def _build_all_transitions() -> tuple[Transition, ...]:
    transitions = list(_HAPPY_PATH)
    for state in OnboardingState:
        if state in TERMINAL_STATES:
            continue
        # An operator-initiated stop is legal from any in-flight state.
        transitions.append(Transition(state, OnboardingTrigger.STOP, OnboardingState.STOPPED))
        for trigger in _FAILURE_TRIGGERS:
            transitions.append(Transition(state, trigger, OnboardingState.FAILED_CLOSED))
    return tuple(transitions)


ALL_TRANSITIONS: tuple[Transition, ...] = _build_all_transitions()

_TABLE: dict[tuple[OnboardingState, OnboardingTrigger], OnboardingState] = {
    (transition.from_state, transition.trigger): transition.to_state for transition in ALL_TRANSITIONS
}


class OnboardingStateMachine:
    """One instance per account-onboarding attempt.

    Once ``FAILED_CLOSED`` or ``STOPPED`` is reached there is no transition
    back to an active state -- a caller must construct a brand-new instance
    (and, at the ``AccountWorker`` layer, a brand-new attempt) rather than
    resuming this one. This is deliberate: an ambiguous outcome must never be
    silently retried.
    """

    def __init__(self) -> None:
        self.current_state: OnboardingState = OnboardingState.NEW
        self.last_failure_reason: FailureReason | None = None

    def apply(self, trigger: OnboardingTrigger) -> TransitionResult:
        if self.current_state in TERMINAL_STATES:
            return TransitionResult.reject(self.current_state, RejectionReason.TERMINAL_STATE)

        next_state = _TABLE.get((self.current_state, trigger))
        if next_state is None:
            return TransitionResult.reject(self.current_state, RejectionReason.ILLEGAL_TRANSITION)

        failure_reason = _FAILURE_TRIGGERS.get(trigger)
        self.current_state = next_state
        if failure_reason is not None:
            self.last_failure_reason = failure_reason
        return TransitionResult.accept(next_state, failure_reason)

    def resolve_endpoint(self, verified_records: Sequence[Mapping[str, Any]]) -> TransitionResult:
        """Apply the correct endpoint trigger from already-filtered VERIFIED records.

        ``verified_records`` must come from ``endpoint_registry.resolve_verified``
        (or ``mt5_dry_run.resolve_mt5_endpoint``'s underlying records) -- this
        method never re-filters CANDIDATE/METAQUOTES_CDN/EXPIRED itself, it only
        judges ambiguity of an already-VERIFIED-only, already-non-expired list.
        """
        trigger = OnboardingTrigger.ENDPOINT_VERIFIED if len(verified_records) == 1 else OnboardingTrigger.MISSING_ENDPOINT
        return self.apply(trigger)
