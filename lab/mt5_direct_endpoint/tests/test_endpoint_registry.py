from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.endpoint_registry import RegistryError, register_verified, resolve_verified


RUN_ID = "12345678-1234-4234-8234-123456789abc"


class EndpointRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "events.jsonl").write_text('{"sanitized":true}\n', encoding="utf-8")
        digest = hashlib.sha256((self.root / "events.jsonl").read_bytes()).hexdigest()
        self.digest = digest
        self.manifest = self.root / "artifact-manifest.json"
        self.manifest.write_text(json.dumps({"files": [{"relative_path": "events.jsonl", "sha256": digest}]}), encoding="utf-8")
        self.registry = self.root / "registry.json"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def kwargs(self, **changes):
        values = dict(
            path=self.registry, broker_label="FPMTrading", host="188.42.136.4", port=443,
            protocol="TCP/TLS", observed_at_unix_ms=1_000_000, discovery_method="MT5_LOGIN_DIALOG_IP",
            verification_pid=4800, verification_session_id=RUN_ID, confidence="MEDIUM",
            artifact_root=self.root, artifact_manifest=self.manifest,
            artifact_relative_path="events.jsonl", artifact_sha256=self.digest, ttl_seconds=100,
        )
        values.update(changes)
        return values

    def test_verified_round_trip_and_multiple_endpoints(self):
        register_verified(**self.kwargs())
        register_verified(**self.kwargs(host="203.0.113.1"))
        found = resolve_verified(self.registry, broker_label="FPMTrading", now_unix_ms=1_000_001, artifact_root=self.root, artifact_manifest=self.manifest)
        self.assertEqual(len(found), 2)
        self.assertTrue(all(item["status"] == "VERIFIED" for item in found))

    def test_invalid_candidate_status_never_resolves(self):
        register_verified(**self.kwargs())
        payload = json.loads(self.registry.read_text())
        payload["brokers"]["FPMTrading"][0]["status"] = "CANDIDATE"
        self.registry.write_text(json.dumps(payload))
        self.assertEqual(resolve_verified(self.registry, broker_label="FPMTrading"), [])

    def test_ttl_expiry(self):
        register_verified(**self.kwargs())
        self.assertEqual(resolve_verified(self.registry, broker_label="FPMTrading", now_unix_ms=1_100_001), [])

    def test_digest_altered_or_manifest_missing_is_rejected(self):
        with self.assertRaises(RegistryError):
            register_verified(**self.kwargs(artifact_sha256="0" * 64))
        self.manifest.unlink()
        with self.assertRaises(RegistryError):
            register_verified(**self.kwargs())

    def test_artifact_mutation_is_rejected_on_resolution(self):
        register_verified(**self.kwargs())
        (self.root / "events.jsonl").write_text("changed\n", encoding="utf-8")
        with self.assertRaises(RegistryError):
            resolve_verified(self.registry, broker_label="FPMTrading", artifact_root=self.root, artifact_manifest=self.manifest)

    def test_json_corruption_and_unsafe_paths_are_rejected(self):
        register_verified(**self.kwargs())
        self.registry.write_text("not json")
        with self.assertRaises(RegistryError):
            resolve_verified(self.registry, broker_label="FPMTrading")
        with self.assertRaises(RegistryError):
            register_verified(**self.kwargs(artifact_relative_path="../events.jsonl"))

    def test_no_secrets_and_atomic_output(self):
        register_verified(**self.kwargs())
        text = self.registry.read_text(encoding="utf-8")
        self.assertNotRegex(text.lower(), r"password|token|secret|credential|hmac")
        self.assertFalse(list(self.root.glob(".registry.json.*")))

    def test_invalid_endpoint_and_session_rejected(self):
        with self.assertRaises(RegistryError):
            register_verified(**self.kwargs(host="127.0.0.1"))
        with self.assertRaises(RegistryError):
            register_verified(**self.kwargs(port=0))
        with self.assertRaises(RegistryError):
            register_verified(**self.kwargs(verification_session_id="not-a-uuid"))


if __name__ == "__main__":
    unittest.main()
