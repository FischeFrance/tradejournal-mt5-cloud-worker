from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))

from evidence_assembler import AssemblerError, assemble_run  # noqa: E402
from lab_evidence_verifier import verify_captured_run  # noqa: E402
from test_lab_evidence_verifier import RUN_ID, _full_captured_fixture  # noqa: E402


class AssemblerVerifierIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source_fixture = _full_captured_fixture()
        self.output_tmp = tempfile.TemporaryDirectory()
        self.output = Path(self.output_tmp.name)

    def tearDown(self) -> None:
        self.source_fixture.close()
        self.output_tmp.cleanup()

    def _make_synthetic_source(self) -> None:
        for control in ("C0", "C1", "C2"):
            path = self.source_fixture.run_dir / f"{RUN_ID}.evidence.{control.lower()}.json"
            body = json.loads(path.read_text(encoding="utf-8"))
            body["proof_binding"]["provenance"] = {
                "origin": "SYNTHETIC_FIXTURE",
                "synthetic_fixture": True,
                "producer": "OFFLINE_TEST_FIXTURE",
                "artifact_set_id": "10000000-0000-4000-8000-000000000001",
            }
            path.write_text(json.dumps(body), encoding="utf-8")

    def _manifest(self) -> dict[str, object]:
        files = {
            "evidence_c0": f"{RUN_ID}.evidence.c0.json",
            "evidence_c1": f"{RUN_ID}.evidence.c1.json",
            "evidence_c2": f"{RUN_ID}.evidence.c2.json",
            "etw_evidence_export": f"{RUN_ID}.etw-evidence.sanitized.json",
            "network_summary": f"{RUN_ID}.network-summary.json",
            "job_identity_preimage_c0": f"{RUN_ID}.job-identity.c0.json",
            "job_identity_preimage_c1": f"{RUN_ID}.job-identity.c1.json",
            "job_identity_preimage_c2": f"{RUN_ID}.job-identity.c2.json",
            "root_process_generation_preimage_c0": f"{RUN_ID}.root-process-generation.c0.json",
            "root_process_generation_preimage_c1": f"{RUN_ID}.root-process-generation.c1.json",
            "root_process_generation_preimage_c2": f"{RUN_ID}.root-process-generation.c2.json",
        }
        return {"run_id": RUN_ID, "files": files}

    def _publish_non_evidence_artifacts(self) -> None:
        for path in self.source_fixture.run_dir.iterdir():
            if ".evidence." not in path.name:
                shutil.copy2(path, self.output / path.name)

    def test_synthetic_assembler_to_verifier_test_only(self) -> None:
        self._make_synthetic_source()
        report = assemble_run(self.source_fixture.run_dir, self._manifest(), self.output)
        self.assertIsNone(report["verdict"])
        self._publish_non_evidence_artifacts()
        result = verify_captured_run(self.output, RUN_ID, allow_synthetic=True)
        self.assertEqual(result.outcome, "SYNTHETIC_PASS", result.reasons)

    def test_tampered_digest_fails(self) -> None:
        self._make_synthetic_source()
        assemble_run(self.source_fixture.run_dir, self._manifest(), self.output)
        self._publish_non_evidence_artifacts()
        evidence = self.output / f"{RUN_ID}.evidence.c0.json"
        body = json.loads(evidence.read_text(encoding="utf-8"))
        body["proof_binding"]["etw_evidence_sha256"] = "0" * 64
        evidence.write_text(json.dumps(body), encoding="utf-8")
        result = verify_captured_run(self.output, RUN_ID, allow_synthetic=True)
        self.assertEqual(result.outcome, "FAIL")

    def test_missing_etw_is_degraded(self) -> None:
        self._make_synthetic_source()
        assemble_run(self.source_fixture.run_dir, self._manifest(), self.output)
        self._publish_non_evidence_artifacts()
        (self.output / f"{RUN_ID}.etw-evidence.sanitized.json").unlink()
        result = verify_captured_run(self.output, RUN_ID, allow_synthetic=True)
        self.assertIn(result.outcome, ("INCONCLUSIVE", "FAIL"))

    def test_unsafe_path_rejects_without_partial_publication(self) -> None:
        self._make_synthetic_source()
        bad = {"run_id": RUN_ID, "files": {"evidence_c0": "../escape.json"}}
        with self.assertRaises(AssemblerError):
            assemble_run(self.source_fixture.run_dir, bad, self.output)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_captured_export_cannot_wrap_synthetic_fixture(self) -> None:
        path = self.source_fixture.run_dir / f"{RUN_ID}.evidence.c0.json"
        body = json.loads(path.read_text(encoding="utf-8"))
        body["proof_binding"]["provenance"]["origin"] = "CAPTURED_EXPORT"
        body["proof_binding"]["provenance"]["synthetic_fixture"] = True
        path.write_text(json.dumps(body), encoding="utf-8")
        with self.assertRaises(AssemblerError):
            assemble_run(self.source_fixture.run_dir, {"run_id": RUN_ID, "files": {"evidence_c0": path.name}}, self.output)
        self.assertFalse((self.output / path.name).exists())


if __name__ == "__main__":
    unittest.main()
