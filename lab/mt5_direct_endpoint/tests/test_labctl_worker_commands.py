from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

LAB_ROOT = Path(__file__).resolve().parents[1]
CLI = LAB_ROOT / "tools" / "labctl.py"

sys.path.insert(0, str(LAB_ROOT))

from tools.endpoint_registry import register_verified  # noqa: E402


RUN_ID = "12345678-1234-4234-8234-123456789abc"


class LabCliWorkerCommandsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.worker_root = self.root / "workers"
        self.registry = self.root / "registry.json"

    def tearDown(self) -> None:
        # Best-effort: if a test spawned a persistent worker-host and failed before its own
        # stop-worker call, this must still reach it -- an assertion failure must never leak
        # a real, long-lived OS process past the (now-deleted) tempdir it was tracked under.
        self.run_cli("stop-worker", "--account-id", "acc-1", "--worker-root", self.worker_root)
        self.tmp.cleanup()

    def run_cli(self, *arguments: object) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(CLI), *(str(item) for item in arguments)],
            cwd=LAB_ROOT.parents[1],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def _register_verified(self, ttl_seconds: int = 100) -> None:
        artifact = self.root / "events.jsonl"
        artifact.write_text('{"sanitized":true}\n', encoding="utf-8")
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        manifest = self.root / "manifest.json"
        manifest.write_text(
            json.dumps({"files": [{"relative_path": "events.jsonl", "sha256": digest}]}),
            encoding="utf-8",
        )
        register_verified(
            self.registry, broker_label="FPMTrading", host="188.42.136.4", port=443,
            protocol="TCP/TLS", observed_at_unix_ms=1_000_000, discovery_method="MT5_LOGIN_DIALOG_IP",
            verification_pid=4800, verification_session_id=RUN_ID, confidence="MEDIUM",
            artifact_root=self.root, artifact_manifest=manifest, artifact_relative_path="events.jsonl",
            artifact_sha256=digest, ttl_seconds=ttl_seconds,
        )

    def test_create_account_produces_worker_directory_and_no_credential_fields(self) -> None:
        result = self.run_cli(
            "create-account", "--account-id", "acc-1", "--worker-root", self.worker_root,
            "--registry", self.registry, "--broker-label", "FPMTrading",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["account_id"], "acc-1")
        self.assertEqual(payload["state"], "STARTING")
        self.assertEqual(payload["onboarding_state"], "NEW")
        self.assertTrue(Path(payload["directory"]).is_dir())
        for forbidden in ("password", "login", "credential", "secret", "token"):
            self.assertNotIn(forbidden, json.dumps(payload).lower())

    def test_full_lifecycle_create_start_status_stop(self) -> None:
        # Deep coverage of the persistent worker-host (real process, heartbeat, crash,
        # restart cap, isolation, cleanup) lives in test_worker_host.py; this is just the
        # CLI-level smoke check that the four verbs still wire together end to end.
        create = self.run_cli(
            "create-account", "--account-id", "acc-1", "--worker-root", self.worker_root,
            "--registry", self.registry, "--broker-label", "FPMTrading",
        )
        self.assertEqual(create.returncode, 0, create.stderr)

        start = self.run_cli(
            "start-worker", "--account-id", "acc-1", "--worker-root", self.worker_root,
            "--poll-interval-seconds", "0.5",
        )
        self.assertEqual(start.returncode, 0, start.stderr)
        started = json.loads(start.stdout)
        self.assertEqual(started["state"], "RUNNING")
        self.assertTrue(started["process_alive"])
        self.assertIsInstance(started["pid"], int)

        status = self.run_cli("worker-status", "--account-id", "acc-1", "--worker-root", self.worker_root)
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(json.loads(status.stdout)["state"], "RUNNING")

        stop = self.run_cli("stop-worker", "--account-id", "acc-1", "--worker-root", self.worker_root)
        self.assertEqual(stop.returncode, 0, stop.stderr)
        stopped = json.loads(stop.stdout)
        self.assertEqual(stopped["state"], "STOPPED")
        self.assertFalse(stopped["process_alive"])

        status_after_stop = self.run_cli("worker-status", "--account-id", "acc-1", "--worker-root", self.worker_root)
        self.assertEqual(json.loads(status_after_stop.stdout)["state"], "STOPPED")

    def test_worker_status_reports_missing_worker_as_error(self) -> None:
        result = self.run_cli("worker-status", "--account-id", "never-created", "--worker-root", self.worker_root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ERROR", result.stderr)

    def test_verify_endpoint_fails_closed_when_registry_is_missing(self) -> None:
        result = self.run_cli("verify-endpoint", "--registry", self.registry, "--broker-label", "FPMTrading")
        self.assertEqual(result.returncode, 64)
        self.assertIn("ERROR", result.stderr)

    def test_verify_endpoint_fails_closed_when_broker_has_no_verified_record(self) -> None:
        self._register_verified()
        result = self.run_cli(
            "verify-endpoint", "--registry", self.registry, "--broker-label", "SomeOtherBroker",
            "--now-unix-ms", "1000050",
        )
        self.assertEqual(result.returncode, 2)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["usable"])
        self.assertIsNone(payload["endpoint"])
        self.assertEqual(payload["verified_count"], 0)

    def test_verify_endpoint_succeeds_on_single_verified_record(self) -> None:
        self._register_verified()
        result = self.run_cli(
            "verify-endpoint", "--registry", self.registry, "--broker-label", "FPMTrading",
            "--now-unix-ms", "1000050",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["usable"])
        self.assertEqual(payload["endpoint"], "188.42.136.4:443")

    def test_generate_config_dry_run_has_no_password_flag_and_no_credentials_in_output(self) -> None:
        self._register_verified()
        result = self.run_cli(
            "generate-config-dry-run", "--registry", self.registry, "--broker-label", "FPMTrading",
            "--now-unix-ms", "1000050",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["server"], "188.42.136.4:443")
        self.assertFalse(payload["launched"])
        self.assertNotIn("password", json.dumps(payload).lower())

    def test_generate_config_dry_run_rejects_password_argument(self) -> None:
        result = self.run_cli(
            "generate-config-dry-run", "--registry", self.registry, "--broker-label", "FPMTrading",
            "--password", "should-not-be-a-real-flag",
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("unrecognized arguments", result.stderr)


if __name__ == "__main__":
    unittest.main()
