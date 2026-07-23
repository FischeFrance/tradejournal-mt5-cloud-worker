from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


LAB_ROOT = Path(__file__).resolve().parents[1]
CLI = LAB_ROOT / "tools" / "labctl.py"
EXAMPLES = LAB_ROOT / "examples"
sys.path.insert(0, str(LAB_ROOT / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from lab_model import (  # noqa: E402
    EVIDENCE_SCHEMA_VERSION,
    contract_digest,
    validate_evidence,
)
from test_lab_model import (  # noqa: E402
    RUN_IDS,
    _identity_probe,
    build_artifacts,
)


class LabCliTests(unittest.TestCase):
    def run_cli(self, *arguments: object) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(CLI), *(str(item) for item in arguments)],
            cwd=LAB_ROOT.parents[1],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    @staticmethod
    def write_json(path: Path, payload: object) -> None:
        path.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )

    def test_validate_config_v4(self) -> None:
        result = self.run_cli(
            "validate-config",
            "--config",
            EXAMPLES / "experiment.c0-c2.example.json",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "VALID\n")

    def test_build_and_validate_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "manifest.json"
            built = self.run_cli(
                "build-manifest",
                "--config",
                EXAMPLES / "experiment.c0-c2.example.json",
                "--output",
                output,
            )
            self.assertEqual(built.returncode, 0, built.stderr)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["artifact_type"], "EXPERIMENT_MANIFEST")
            checked = self.run_cli(
                "validate-manifest",
                "--manifest",
                output,
            )
            self.assertEqual(checked.returncode, 0, checked.stderr)

    def test_plan_is_hard_disabled(self) -> None:
        result = self.run_cli(
            "plan",
            "--config",
            EXAMPLES / "experiment.c0-c2.example.json",
            "--control",
            "C0",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        plan = json.loads(result.stdout)
        self.assertEqual(
            plan["safety"],
            {
                "plan_only": True,
                "mt5_start_enabled": False,
                "firewall_apply_enabled": False,
                "credential_access_enabled": False,
                "registry_promotion_enabled": False,
            },
        )

    def test_direct_plan_without_handoff_is_rejected(self) -> None:
        result = self.run_cli(
            "plan",
            "--config",
            EXAMPLES / "experiment.c0-c2.example.json",
            "--control",
            "C3",
        )
        self.assertEqual(result.returncode, 64)
        self.assertIn("authoritative candidate handoff", result.stderr)

    def test_checked_in_evidence_validates(self) -> None:
        result = self.run_cli(
            "validate-evidence",
            "--evidence",
            EXAMPLES / "evidence.c0.synthetic-pass.json",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_evaluate_without_plan_is_inconclusive(self) -> None:
        result = self.run_cli(
            "evaluate",
            "--config",
            EXAMPLES / "experiment.c0-c2.example.json",
            "--evidence",
            EXAMPLES / "evidence.c0.synthetic-pass.json",
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(json.loads(result.stdout)["outcome"], "INCONCLUSIVE")

    def test_fixture_evaluation_never_uses_exit_zero(self) -> None:
        common = (
            "--config",
            EXAMPLES / "experiment.c0-c2.example.json",
            "--manifest",
            EXAMPLES / "experiment-manifest.synthetic.json",
            "--control-plan",
            EXAMPLES / "control-plan.c0.synthetic.json",
            "--evidence",
            EXAMPLES / "evidence.c0.synthetic-pass.json",
        )
        fixture = self.run_cli("evaluate-fixture", *common)
        self.assertEqual(fixture.returncode, 3, fixture.stderr)
        self.assertEqual(json.loads(fixture.stdout)["outcome"], "SYNTHETIC_PASS")
        ordinary = self.run_cli("evaluate", *common)
        self.assertEqual(ordinary.returncode, 2, ordinary.stderr)
        self.assertEqual(json.loads(ordinary.stdout)["outcome"], "INCONCLUSIVE")

    def test_complete_fixture_campaign_cli(self) -> None:
        artifacts = build_artifacts()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = {
                "config": root / "config.json",
                "manifest": root / "manifest.json",
                "plans": root / "plans.json",
                "campaign": root / "campaign.json",
                "handoff": root / "handoff.json",
                "direct": root / "direct.json",
            }
            payloads = {
                "config": artifacts.config,
                "manifest": artifacts.manifest,
                "plans": artifacts.plans,
                "campaign": artifacts.evidence_list(),
                "handoff": artifacts.handoff,
                "direct": artifacts.direct_manifest,
            }
            for name, path in paths.items():
                self.write_json(path, payloads[name])
            args = (
                "--config",
                paths["config"],
                "--manifest",
                paths["manifest"],
                "--control-plans",
                paths["plans"],
                "--campaign",
                paths["campaign"],
                "--candidate-handoff",
                paths["handoff"],
                "--direct-campaign-manifest",
                paths["direct"],
            )
            fixture = self.run_cli("evaluate-fixture-campaign", *args)
            self.assertEqual(fixture.returncode, 3, fixture.stderr)
            self.assertEqual(
                json.loads(fixture.stdout)["outcome"],
                "SYNTHETIC_PASS",
            )
            ordinary = self.run_cli("evaluate-campaign", *args)
            self.assertEqual(ordinary.returncode, 2, ordinary.stderr)
            self.assertEqual(
                json.loads(ordinary.stdout)["outcome"],
                "INCONCLUSIVE",
            )

    def test_duplicate_json_key_is_rejected_before_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "duplicate.json"
            path.write_text(
                '{"schema_version":4,"schema_version":4}',
                encoding="utf-8",
            )
            result = self.run_cli(
                "validate-config",
                "--config",
                path,
            )
        self.assertEqual(result.returncode, 64)
        self.assertIn("duplicate JSON key", result.stderr)

    def test_fractional_schema_version_is_rejected(self) -> None:
        config = json.loads(
            (EXAMPLES / "experiment.c0-c2.example.json").read_text(
                encoding="utf-8"
            )
        )
        config["schema_version"] = 4.0
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "fractional.json"
            self.write_json(path, config)
            result = self.run_cli(
                "validate-config",
                "--config",
                path,
            )
        self.assertEqual(result.returncode, 64)
        self.assertIn("schema_version", result.stderr)

    def test_legacy_evidence_is_rejected(self) -> None:
        evidence = json.loads(
            (EXAMPLES / "evidence.c0.synthetic-pass.json").read_text(
                encoding="utf-8"
            )
        )
        evidence["schema_version"] = 5
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "legacy.json"
            self.write_json(path, evidence)
            result = self.run_cli(
                "validate-evidence",
                "--evidence",
                path,
            )
        self.assertEqual(result.returncode, 64)
        self.assertIn("schema_version", result.stderr)

    def test_output_publish_is_create_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "manifest.json"
            args = (
                "build-manifest",
                "--config",
                EXAMPLES / "experiment.c0-c2.example.json",
                "--output",
                output,
            )
            first = self.run_cli(*args)
            before = output.read_bytes()
            second = self.run_cli(*args)
            after = output.read_bytes()
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 64)
        self.assertEqual(before, after)

    def test_digest_uses_validated_evidence_v6(self) -> None:
        result = self.run_cli(
            "digest",
            "--evidence",
            EXAMPLES / "evidence.c0.synthetic-pass.json",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        evidence = validate_evidence(
            json.loads(
                (
                    EXAMPLES / "evidence.c0.synthetic-pass.json"
                ).read_text(encoding="utf-8")
            )
        )
        self.assertEqual(
            result.stdout,
            contract_digest(
                "EVIDENCE",
                EVIDENCE_SCHEMA_VERSION,
                evidence,
            )
            + "\n",
        )

    def test_probe_output_digest_is_an_assertion_not_caller_authority(self) -> None:
        artifacts = build_artifacts()
        probe = _identity_probe(artifacts.plans["C2"], "C2")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config.json"
            plan = root / "plan.json"
            probe_path = root / "probe.json"
            self.write_json(config, artifacts.config)
            self.write_json(plan, artifacts.plans["C2"])
            self.write_json(probe_path, probe)
            result = self.run_cli(
                "compose-identity",
                "--probe",
                probe_path,
                "--config",
                config,
                "--control-plan",
                plan,
                "--expected-run-id",
                RUN_IDS["C2"],
                "--investor-provenance-confirmed",
                "--probe-hash-verified",
                "--probe-static-guard-passed",
                "--probe-output-sha256",
                "a" * 64,
            )
        self.assertEqual(result.returncode, 64)
        self.assertIn("does not match the validated probe", result.stderr)


if __name__ == "__main__":
    unittest.main()
