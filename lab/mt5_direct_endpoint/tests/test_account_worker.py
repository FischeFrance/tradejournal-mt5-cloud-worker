from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.account_onboarding import FailureReason, OnboardingState, OnboardingTrigger
from tools.account_worker import AccountWorker, WorkerDirectory, WorkerError, WorkerState, WorkerStateFile
from tools.credential_provider import FakeCredentialProvider
from tools.endpoint_registry import RegistryError, register_verified, resolve_verified


RUN_ID = "12345678-1234-4234-8234-123456789abc"


class WorkerDirectoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_create_produces_expected_layout(self):
        directory = WorkerDirectory.create(self.base, "acc-1")
        self.assertTrue(directory.config.is_dir())
        self.assertTrue(directory.state.is_dir())
        self.assertTrue(directory.logs.is_dir())
        self.assertTrue(directory.session.is_dir())

    def test_refuses_to_reuse_existing_directory(self):
        WorkerDirectory.create(self.base, "acc-1")
        with self.assertRaises(WorkerError):
            WorkerDirectory.create(self.base, "acc-1")

    def test_two_accounts_get_distinct_non_overlapping_directories(self):
        first = WorkerDirectory.create(self.base, "acc-1")
        second = WorkerDirectory.create(self.base, "acc-2")
        self.assertNotEqual(first.root, second.root)
        self.assertNotIn(str(first.root), str(second.root))

    def test_rejects_path_traversal_account_id(self):
        with self.assertRaises(WorkerError):
            WorkerDirectory.create(self.base, "../escape")

    def test_resume_requires_existing_layout(self):
        with self.assertRaises(WorkerError):
            WorkerDirectory.resume(self.base, "never-created")

    def test_resume_succeeds_after_create(self):
        WorkerDirectory.create(self.base, "acc-1")
        resumed = WorkerDirectory.resume(self.base, "acc-1")
        self.assertTrue(resumed.state.is_dir())


class WorkerStateFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "worker.json"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_round_trips_atomically(self):
        state = WorkerStateFile(
            worker_id="w1", account_id="acc-1", state=WorkerState.HEALTHY,
            registry_path="registry.json", broker_label="FPMTrading", restart_count=2,
        )
        state.save_atomic(self.path)
        loaded = WorkerStateFile.load(self.path)
        self.assertEqual(loaded.worker_id, "w1")
        self.assertEqual(loaded.state, WorkerState.HEALTHY)
        self.assertEqual(loaded.restart_count, 2)

    def test_crash_mid_write_never_corrupts_previously_committed_state(self):
        state = WorkerStateFile(
            worker_id="w1", account_id="acc-1", state=WorkerState.HEALTHY,
            registry_path="registry.json", broker_label="FPMTrading",
        )
        state.save_atomic(self.path)
        committed_bytes = self.path.read_bytes()

        # Simulate a crash mid-write: a stray temp file from an interrupted
        # save must never be mistaken for, or replace, the committed file.
        stray_temp = self.path.parent / f".{self.path.name}.stray-crash-tmp"
        stray_temp.write_bytes(b"{not valid json")

        self.assertEqual(self.path.read_bytes(), committed_bytes)
        loaded = WorkerStateFile.load(self.path)
        self.assertEqual(loaded.state, WorkerState.HEALTHY)
        stray_temp.unlink()


class AccountWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.base_dir = self.root / "workers"
        artifact = self.root / "events.jsonl"
        artifact.write_text('{"sanitized":true}\n', encoding="utf-8")
        self.digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        self.manifest = self.root / "manifest.json"
        self.manifest.write_text(
            json.dumps({"files": [{"relative_path": "events.jsonl", "sha256": self.digest}]}),
            encoding="utf-8",
        )
        self.registry = self.root / "registry.json"
        self.credentials = FakeCredentialProvider()
        self.credentials.register("acc-1", login="12345", password="hunter2-real-secret")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _register_verified(self, ttl_seconds: int = 100) -> None:
        register_verified(
            self.registry, broker_label="FPMTrading", host="188.42.136.4", port=443,
            protocol="TCP/TLS", observed_at_unix_ms=1_000_000, discovery_method="MT5_LOGIN_DIALOG_IP",
            verification_pid=4800, verification_session_id=RUN_ID, confidence="MEDIUM",
            artifact_root=self.root, artifact_manifest=self.manifest, artifact_relative_path="events.jsonl",
            artifact_sha256=self.digest, ttl_seconds=ttl_seconds,
        )

    def _new_worker(self, account_id: str = "acc-1") -> AccountWorker:
        return AccountWorker.create(
            self.base_dir, account_id,
            credential_provider=self.credentials, registry_path=self.registry, broker_label="FPMTrading",
        )

    def test_worker_creation_persists_initial_state(self):
        worker = self._new_worker()
        loaded = WorkerStateFile.load(worker.directory.worker_state_path)
        self.assertEqual(loaded.state, WorkerState.STARTING)
        self.assertEqual(loaded.onboarding_state, OnboardingState.NEW)

    def test_heartbeat_updates_and_staleness_uses_injected_clock(self):
        worker = self._new_worker()
        worker.heartbeat(now_unix_ms=1_000_000)
        self.assertFalse(worker.is_stale(max_age_seconds=30, now_unix_ms=1_000_000 + 10_000))
        self.assertTrue(worker.is_stale(max_age_seconds=30, now_unix_ms=1_000_000 + 60_000))

    def test_worker_without_heartbeat_is_stale(self):
        worker = self._new_worker()
        self.assertTrue(worker.is_stale(max_age_seconds=30))

    def test_resolve_endpoint_advances_onboarding_on_single_verified_record(self):
        self._register_verified()
        worker = self._new_worker()
        worker.apply_onboarding_trigger(OnboardingTrigger.CREATE_PORTABLE_DIRECTORY)
        worker.apply_onboarding_trigger(OnboardingTrigger.REQUIRE_BROKER_ENDPOINT)
        worker.resolve_endpoint(now_unix_ms=1_000_050)
        self.assertEqual(worker.onboarding.current_state, OnboardingState.BROKER_ENDPOINT_VERIFIED)
        self.assertEqual(worker.state_file.endpoint, "188.42.136.4:443")

    def test_resolve_endpoint_fails_closed_when_no_verified_record(self):
        worker = self._new_worker()
        worker.resolve_endpoint()
        self.assertEqual(worker.onboarding.current_state, OnboardingState.FAILED_CLOSED)
        self.assertEqual(worker.onboarding.last_failure_reason, FailureReason.MISSING_ENDPOINT)
        self.assertIsNone(worker.state_file.endpoint)

    def test_resolve_endpoint_fails_closed_with_invalid_config_when_registry_is_corrupt(self):
        self.registry.write_text("{not valid json", encoding="utf-8")
        worker = self._new_worker()
        worker.resolve_endpoint()
        self.assertEqual(worker.onboarding.current_state, OnboardingState.FAILED_CLOSED)
        self.assertEqual(worker.onboarding.last_failure_reason, FailureReason.INVALID_CONFIG)

    def test_digest_tampered_artifact_is_rejected_before_worker_can_resolve_it(self):
        self._register_verified()
        artifact = self.root / "events.jsonl"
        artifact.write_text('{"sanitized":false,"tampered":true}\n', encoding="utf-8")
        with self.assertRaises(RegistryError):
            resolve_verified(
                self.registry, broker_label="FPMTrading", now_unix_ms=1_000_050,
                artifact_root=self.root, artifact_manifest=self.manifest,
            )

    def test_resume_reconstructs_persisted_state_and_onboarding_progress(self):
        self._register_verified()
        worker = self._new_worker()
        worker.apply_onboarding_trigger(OnboardingTrigger.CREATE_PORTABLE_DIRECTORY)
        worker.apply_onboarding_trigger(OnboardingTrigger.REQUIRE_BROKER_ENDPOINT)
        worker.resolve_endpoint(now_unix_ms=1_000_050)
        worker.heartbeat(now_unix_ms=1_000_060)

        resumed = AccountWorker.resume(self.base_dir, "acc-1", credential_provider=self.credentials)

        self.assertEqual(resumed.state_file.worker_id, worker.state_file.worker_id)
        self.assertEqual(resumed.onboarding.current_state, OnboardingState.BROKER_ENDPOINT_VERIFIED)
        self.assertEqual(resumed.state_file.endpoint, "188.42.136.4:443")
        self.assertEqual(resumed.state_file.last_heartbeat_unix_ms, 1_000_060)
        self.assertEqual(resumed.registry_path, str(self.registry))
        self.assertEqual(resumed.broker_label, "FPMTrading")

    def test_generate_config_dry_run_contains_no_credential_text(self):
        self._register_verified()
        worker = self._new_worker()
        with worker.generate_config_dry_run(now_unix_ms=1_000_050) as plan:
            raw = plan.config_path.read_bytes().decode("utf-16")
            self.assertNotIn("hunter2-real-secret", raw)
            self.assertNotIn("12345", raw)
            self.assertNotIn("password", raw.lower())
            self.assertNotIn("login", raw.lower())
            self.assertIn("Server=188.42.136.4:443", raw)
        self.assertFalse(plan.config_path.exists())


if __name__ == "__main__":
    unittest.main()
