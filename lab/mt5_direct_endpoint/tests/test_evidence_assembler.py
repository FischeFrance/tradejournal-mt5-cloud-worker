from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))

from evidence_assembler import AssemblerError, assemble_run  # noqa: E402
from test_lab_evidence_verifier import RUN_ID, _full_captured_fixture  # noqa: E402


class EvidenceAssemblerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = _full_captured_fixture()
        self.output = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        self.fixture.close()

    def _manifest(self) -> dict[str, object]:
        names = {
            "evidence_c0": f"{RUN_ID}.evidence.c0.json",
            "evidence_c1": f"{RUN_ID}.evidence.c1.json",
            "evidence_c2": f"{RUN_ID}.evidence.c2.json",
            "etw_evidence_export": f"{RUN_ID}.etw-evidence.sanitized.json",
            "network_summary": f"{RUN_ID}.network-summary.json",
        }
        return {"run_id": RUN_ID, "files": names}

    def test_assembles_valid_c012_without_verdict(self) -> None:
        report = assemble_run(self.fixture.run_dir, self._manifest(), self.output)
        self.assertIsNone(report["verdict"])
        self.assertFalse(report["errors"])
        self.assertTrue((self.output / f"{RUN_ID}.evidence.c2.json").exists())
        manifest = json.loads((self.output / f"{RUN_ID}.artifact-manifest.json").read_text())
        self.assertEqual(manifest["run_id"], RUN_ID)
        published = {
            entry["role"]: entry
            for entry in manifest["files"]
        }
        etw = self.output / f"{RUN_ID}.etw-evidence.sanitized.json"
        self.assertTrue(etw.is_file())
        self.assertEqual(
            published["etw_evidence_export"]["sha256"],
            hashlib.sha256(etw.read_bytes()).hexdigest(),
        )

    def test_missing_artifact_fails_closed_and_writes_report(self) -> None:
        (self.fixture.run_dir / f"{RUN_ID}.evidence.c1.json").unlink()
        with self.assertRaises(AssemblerError):
            assemble_run(self.fixture.run_dir, self._manifest(), self.output)
        report = json.loads((self.output / f"{RUN_ID}.assembler-report.json").read_text())
        self.assertIsNone(report["verdict"])
        self.assertTrue(report["errors"])
        self.assertFalse((self.output / f"{RUN_ID}.evidence.c0.json").exists())
        self.assertFalse((self.output / f"{RUN_ID}.artifact-manifest.json").exists())

    def test_rejects_unsafe_manifest_paths(self) -> None:
        manifest = {"run_id": RUN_ID, "files": {"evidence_c0": "../escape.json"}}
        with self.assertRaises(AssemblerError):
            assemble_run(self.fixture.run_dir, manifest, self.output)

    def test_rejects_synthetic_as_captured(self) -> None:
        path = self.fixture.run_dir / f"{RUN_ID}.evidence.c0.json"
        body = json.loads(path.read_text())
        body["proof_binding"]["provenance"]["origin"] = "CAPTURED_EXPORT"
        body["proof_binding"]["provenance"]["synthetic_fixture"] = True
        path.write_text(json.dumps(body), encoding="utf-8")
        with self.assertRaises(AssemblerError):
            assemble_run(self.fixture.run_dir, {"run_id": RUN_ID, "files": {"evidence_c0": path.name}}, self.output)

    def test_publication_failure_rolls_back_every_output(
        self,
    ) -> None:
        import evidence_assembler

        real_replace = evidence_assembler._durable_replace
        replacements = 0

        def fail_second(source: Path, destination: Path) -> None:
            nonlocal replacements
            replacements += 1
            if replacements == 2:
                raise OSError("publication failure")
            real_replace(source, destination)

        evidence_assembler._durable_replace = fail_second
        try:
            with self.assertRaisesRegex(OSError, "publication failure"):
                assemble_run(self.fixture.run_dir, self._manifest(), self.output)
        finally:
            evidence_assembler._durable_replace = real_replace

        self.assertEqual(list(self.output.iterdir()), [])

    def test_refuses_to_overwrite_a_published_run(self) -> None:
        assemble_run(self.fixture.run_dir, self._manifest(), self.output)
        with self.assertRaisesRegex(AssemblerError, "already published"):
            assemble_run(self.fixture.run_dir, self._manifest(), self.output)


if __name__ == "__main__":
    unittest.main()
