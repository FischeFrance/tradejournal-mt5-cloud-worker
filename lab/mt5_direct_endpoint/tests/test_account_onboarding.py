from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.account_onboarding import (
    ALL_TRANSITIONS,
    FailureReason,
    OnboardingState,
    OnboardingStateMachine,
    OnboardingTrigger,
    RejectionReason,
    TERMINAL_STATES,
)
from tools.endpoint_registry import register_verified, resolve_verified


RUN_ID = "12345678-1234-4234-8234-123456789abc"

_HAPPY_SEQUENCE = (
    OnboardingTrigger.CREATE_PORTABLE_DIRECTORY,
    OnboardingTrigger.REQUIRE_BROKER_ENDPOINT,
    OnboardingTrigger.ENDPOINT_VERIFIED,
    OnboardingTrigger.SUPPLY_CREDENTIALS,
    OnboardingTrigger.BEGIN_LOGIN_VALIDATION,
    OnboardingTrigger.LOGIN_ACCEPTED,
)

_ALL_FAILURE_TRIGGERS = (
    OnboardingTrigger.MISSING_ENDPOINT,
    OnboardingTrigger.UNREACHABLE_ENDPOINT,
    OnboardingTrigger.WRONG_CREDENTIALS,
    OnboardingTrigger.SERVER_REJECTED,
    OnboardingTrigger.PROCESS_DIED,
    OnboardingTrigger.TIMEOUT,
    OnboardingTrigger.INVALID_CONFIG,
)


class OnboardingHappyPathTests(unittest.TestCase):
    def test_happy_path_reaches_active(self):
        machine = OnboardingStateMachine()
        for trigger in _HAPPY_SEQUENCE:
            result = machine.apply(trigger)
            self.assertTrue(result.accepted, f"{trigger} was rejected in state {machine.current_state}")
        self.assertEqual(machine.current_state, OnboardingState.ACTIVE)


class OnboardingFailClosedTests(unittest.TestCase):
    def test_every_non_terminal_state_has_all_universal_fail_closed_edges(self):
        for state in OnboardingState:
            if state in TERMINAL_STATES:
                continue
            for trigger in _ALL_FAILURE_TRIGGERS:
                machine = OnboardingStateMachine()
                machine.current_state = state
                result = machine.apply(trigger)
                self.assertTrue(result.accepted, f"{trigger} should be legal from {state}")
                self.assertEqual(result.state, OnboardingState.FAILED_CLOSED)

    def test_fail_closed_trigger_records_matching_failure_reason(self):
        for trigger, expected_reason in (
            (OnboardingTrigger.MISSING_ENDPOINT, FailureReason.MISSING_ENDPOINT),
            (OnboardingTrigger.UNREACHABLE_ENDPOINT, FailureReason.UNREACHABLE_ENDPOINT),
            (OnboardingTrigger.WRONG_CREDENTIALS, FailureReason.WRONG_CREDENTIALS),
            (OnboardingTrigger.SERVER_REJECTED, FailureReason.SERVER_REJECTED),
            (OnboardingTrigger.PROCESS_DIED, FailureReason.PROCESS_DIED),
            (OnboardingTrigger.TIMEOUT, FailureReason.TIMEOUT),
            (OnboardingTrigger.INVALID_CONFIG, FailureReason.INVALID_CONFIG),
        ):
            machine = OnboardingStateMachine()
            result = machine.apply(trigger)
            self.assertEqual(result.failure_reason, expected_reason)
            self.assertEqual(machine.last_failure_reason, expected_reason)

    def test_failed_closed_is_absorbing(self):
        machine = OnboardingStateMachine()
        machine.apply(OnboardingTrigger.TIMEOUT)
        self.assertEqual(machine.current_state, OnboardingState.FAILED_CLOSED)
        for trigger in OnboardingTrigger:
            result = machine.apply(trigger)
            self.assertFalse(result.accepted)
            self.assertEqual(result.rejection_reason, RejectionReason.TERMINAL_STATE)
            self.assertEqual(machine.current_state, OnboardingState.FAILED_CLOSED)

    def test_stopped_is_absorbing(self):
        machine = OnboardingStateMachine()
        machine.apply(OnboardingTrigger.STOP)
        self.assertEqual(machine.current_state, OnboardingState.STOPPED)
        for trigger in OnboardingTrigger:
            result = machine.apply(trigger)
            self.assertFalse(result.accepted)
            self.assertEqual(result.rejection_reason, RejectionReason.TERMINAL_STATE)
            self.assertEqual(machine.current_state, OnboardingState.STOPPED)

    def test_illegal_transition_never_mutates_state(self):
        machine = OnboardingStateMachine()
        result = machine.apply(OnboardingTrigger.LOGIN_ACCEPTED)
        self.assertFalse(result.accepted)
        self.assertEqual(result.rejection_reason, RejectionReason.ILLEGAL_TRANSITION)
        self.assertEqual(machine.current_state, OnboardingState.NEW)

    def test_no_transition_leaves_terminal_states_back_to_new(self):
        offending = [
            transition
            for transition in ALL_TRANSITIONS
            if transition.from_state in TERMINAL_STATES and transition.to_state not in TERMINAL_STATES
        ]
        self.assertEqual(offending, [])


class OnboardingEndpointResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        artifact = self.root / "events.jsonl"
        artifact.write_text('{"sanitized":true}\n', encoding="utf-8")
        self.digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        self.manifest = self.root / "manifest.json"
        self.manifest.write_text(
            json.dumps({"files": [{"relative_path": "events.jsonl", "sha256": self.digest}]}),
            encoding="utf-8",
        )
        self.registry = self.root / "registry.json"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _register(self, status_via_ttl_seconds: int = 100) -> None:
        register_verified(
            self.registry, broker_label="FPMTrading", host="188.42.136.4", port=443,
            protocol="TCP/TLS", observed_at_unix_ms=1_000_000, discovery_method="MT5_LOGIN_DIALOG_IP",
            verification_pid=4800, verification_session_id=RUN_ID, confidence="MEDIUM",
            artifact_root=self.root, artifact_manifest=self.manifest, artifact_relative_path="events.jsonl",
            artifact_sha256=self.digest, ttl_seconds=status_via_ttl_seconds,
        )

    def test_single_verified_record_advances_to_endpoint_verified(self):
        self._register()
        records = resolve_verified(
            self.registry, broker_label="FPMTrading", now_unix_ms=1_000_050,
            artifact_root=self.root, artifact_manifest=self.manifest,
        )
        machine = OnboardingStateMachine()
        machine.apply(OnboardingTrigger.CREATE_PORTABLE_DIRECTORY)
        machine.apply(OnboardingTrigger.REQUIRE_BROKER_ENDPOINT)
        result = machine.resolve_endpoint(records)
        self.assertTrue(result.accepted)
        self.assertEqual(machine.current_state, OnboardingState.BROKER_ENDPOINT_VERIFIED)

    def test_no_verified_records_fails_closed_with_missing_endpoint(self):
        # Nothing registered at all: CANDIDATE/METAQUOTES_CDN/EXPIRED-only or
        # empty registries are indistinguishable to this layer -- both mean
        # "no usable VERIFIED record", which is the point.
        records: list[dict] = []
        machine = OnboardingStateMachine()
        machine.apply(OnboardingTrigger.CREATE_PORTABLE_DIRECTORY)
        machine.apply(OnboardingTrigger.REQUIRE_BROKER_ENDPOINT)
        result = machine.resolve_endpoint(records)
        self.assertTrue(result.accepted)
        self.assertEqual(machine.current_state, OnboardingState.FAILED_CLOSED)
        self.assertEqual(machine.last_failure_reason, FailureReason.MISSING_ENDPOINT)

    def test_expired_verified_record_is_excluded_before_reaching_the_fsm(self):
        self._register(status_via_ttl_seconds=100)
        records = resolve_verified(
            self.registry, broker_label="FPMTrading", now_unix_ms=1_000_000 + 200_000,
            artifact_root=self.root, artifact_manifest=self.manifest,
        )
        self.assertEqual(records, [])
        machine = OnboardingStateMachine()
        machine.apply(OnboardingTrigger.CREATE_PORTABLE_DIRECTORY)
        machine.apply(OnboardingTrigger.REQUIRE_BROKER_ENDPOINT)
        result = machine.resolve_endpoint(records)
        self.assertEqual(machine.current_state, OnboardingState.FAILED_CLOSED)
        self.assertEqual(machine.last_failure_reason, FailureReason.MISSING_ENDPOINT)


if __name__ == "__main__":
    unittest.main()
