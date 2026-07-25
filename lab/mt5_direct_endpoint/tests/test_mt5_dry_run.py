from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.endpoint_registry import register_verified
from tools.mt5_dry_run import DryRunError, dry_run_json, mt5_config_dry_run


RUN_ID = "12345678-1234-4234-8234-123456789abc"


class MT5DryRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        artifact = self.root / "events.jsonl"
        artifact.write_text('{"sanitized":true}\n', encoding="utf-8")
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        self.manifest = self.root / "manifest.json"
        self.manifest.write_text(json.dumps({"files": [{"relative_path": "events.jsonl", "sha256": digest}]}), encoding="utf-8")
        self.registry = self.root / "registry.json"
        register_verified(
            self.registry, broker_label="FPMTrading", host="188.42.136.4", port=443,
            protocol="TCP/TLS", observed_at_unix_ms=1_000_000, discovery_method="MT5_LOGIN_DIALOG_IP",
            verification_pid=4800, verification_session_id=RUN_ID, confidence="MEDIUM",
            artifact_root=self.root, artifact_manifest=self.manifest, artifact_relative_path="events.jsonl",
            artifact_sha256=digest, ttl_seconds=100,
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_verified_endpoint_generates_server_and_cleans_config(self):
        with mt5_config_dry_run(self.registry, "FPMTrading", directory=self.root, now_unix_ms=1_000_001) as plan:
            self.assertEqual(plan.server, "188.42.136.4:443")
            self.assertIn("/config:", plan.command[2])
            self.assertTrue(plan.config_path.exists())
            text = plan.config_path.read_text(encoding="utf-16")
            self.assertIn("Server=188.42.136.4:443", text)
            self.assertNotRegex(text.lower(), r"password|token|secret|credential")
            self.assertNotIn("/login:", " ".join(plan.command))
            self.assertIn('"launched": false', dry_run_json(plan))
        self.assertFalse(plan.config_path.exists())

    def test_unverified_status_is_rejected(self):
        payload = json.loads(self.registry.read_text())
        for status in ("CANDIDATE", "METAQUOTES_CDN", "EXPIRED"):
            payload["brokers"]["FPMTrading"][0]["status"] = status
            self.registry.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(DryRunError):
                with mt5_config_dry_run(self.registry, "FPMTrading", directory=self.root):
                    pass
            payload["brokers"]["FPMTrading"][0]["status"] = "VERIFIED"

    def test_ambiguous_verified_endpoints_fail_closed(self):
        payload = json.loads(self.registry.read_text())
        payload["brokers"]["FPMTrading"].append(dict(payload["brokers"]["FPMTrading"][0], host="203.0.113.10"))
        self.registry.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(DryRunError):
            with mt5_config_dry_run(self.registry, "FPMTrading", directory=self.root):
                pass


if __name__ == "__main__":
    unittest.main()
