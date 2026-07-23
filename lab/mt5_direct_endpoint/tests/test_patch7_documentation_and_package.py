from __future__ import annotations

import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock


LAB_ROOT = Path(__file__).resolve().parents[1]
TOOLS = LAB_ROOT / "tools"
sys.path.insert(0, str(TOOLS))

from package_lab import (  # noqa: E402
    REQUIRED_ARCHIVE_MEMBERS,
    archive_members,
    build_archive,
    validate_archive_name_set,
    validate_archive_relative_parts,
)


class Patch7DocumentationAndPackageTests(unittest.TestCase):
    def test_documentation_matches_candidate_handoff_contract(self) -> None:
        for name in ("README.md", "RUNBOOK.md", "IMPLEMENTATION_REPORT.md"):
            text = (LAB_ROOT / name).read_text(encoding="utf-8")
            with self.subTest(document=name):
                self.assertIn("direct_campaign_manifest_sha256", text)
                self.assertIn("descriptor", text.casefold())
                self.assertIn("C012_SINGLE_PROCESS_SESSION", text)
                self.assertIn("PARTIALLY_READY", text)
                self.assertIn("initial_c012_pre_state_sha256", text)
                self.assertIn("probe_source_sha256", text)
                self.assertIn("source", text.casefold())
                self.assertIn("binary", text.casefold())
                self.assertIn("NO-GO", text)
                self.assertNotIn(
                    "candidate handoff contiene già i digest dei piani C3",
                    text,
                )

    def test_archive_layout_is_repo_relative(self) -> None:
        repo_root = LAB_ROOT.parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            archive_path = Path(temporary) / "patch7-review.zip"
            declared = build_archive(archive_path, repo_root=repo_root)
            with zipfile.ZipFile(archive_path, "r") as archive:
                actual = tuple(archive.namelist())
                self.assertIsNone(archive.testzip())
        self.assertEqual(actual, declared)
        self.assertTrue(actual)
        self.assertTrue(
            all(
                name.startswith("lab/mt5_direct_endpoint/")
                for name in actual
            )
        )
        self.assertFalse(
            any(
                name.startswith("mt5-broker-resolver-poc/")
                or name.startswith("/")
                or "__pycache__" in name
                or "/private/" in name
                or "/raw/" in name
                or "/sanitized/" in name
                or name.endswith(
                    (
                        ".etl",
                        ".evtx",
                        ".ex5",
                        ".pcap",
                        ".pcapng",
                        ".pml",
                        ".pyc",
                        ".pyd",
                        ".pyo",
                        ".wfw",
                        ".zip",
                    )
                )
                for name in actual
            )
        )

    @staticmethod
    def make_minimal_repo(root: Path) -> None:
        for archive_name in REQUIRED_ARCHIVE_MEMBERS:
            path = root / archive_name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}\n", encoding="utf-8")

    def test_archive_excludes_runtime_and_private_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.make_minimal_repo(root)
            lab = root / "lab" / "mt5_direct_endpoint"
            for suffix in (
                ".etl",
                ".evtx",
                ".ex5",
                ".pcap",
                ".pcapng",
                ".pml",
                ".pyc",
                ".pyd",
                ".pyo",
                ".wfw",
                ".zip",
            ):
                (lab / f"runtime{suffix}").write_bytes(b"not-for-review")
            for directory in ("raw", "private", "sanitized", "__pycache__"):
                output = lab / directory / "secret.json"
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text('{"secret":true}', encoding="utf-8")
            archive_path = root / "review.zip"
            names = build_archive(archive_path, repo_root=root)
        self.assertEqual(set(names), REQUIRED_ARCHIVE_MEMBERS)

    def test_archive_rejects_unknown_source_file_type(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.make_minimal_repo(root)
            unexpected = (
                root
                / "lab"
                / "mt5_direct_endpoint"
                / "unexpected.secret"
            )
            unexpected.write_text("must not be packaged", encoding="utf-8")
            with self.assertRaisesRegex(
                ValueError,
                "unapproved source file type",
            ):
                archive_members(root)

    def test_archive_requires_essential_members(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.make_minimal_repo(root)
            (
                root
                / "lab"
                / "mt5_direct_endpoint"
                / "README.md"
            ).unlink()
            with self.assertRaisesRegex(
                ValueError,
                "missing required members",
            ):
                archive_members(root)

    def test_archive_never_overwrites_destination(self) -> None:
        repo_root = LAB_ROOT.parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            archive_path = Path(temporary) / "review.zip"
            archive_path.write_bytes(b"existing")
            with self.assertRaises(FileExistsError):
                build_archive(archive_path, repo_root=repo_root)
            self.assertEqual(archive_path.read_bytes(), b"existing")

    def test_archive_rejects_dangling_destination_symlink(self) -> None:
        repo_root = LAB_ROOT.parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / "review.zip"
            dangling_target = root / "must-not-be-created.zip"
            try:
                destination.symlink_to(dangling_target)
            except OSError as exc:
                self.skipTest(f"symlink unavailable on this host: {exc}")
            self.assertTrue(os.path.lexists(destination))
            self.assertFalse(destination.exists())
            with self.assertRaises(FileExistsError):
                build_archive(destination, repo_root=repo_root)
            self.assertFalse(dangling_target.exists())

    def test_archive_member_names_are_windows_extraction_safe(self) -> None:
        unsafe = (
            ("bad:name.md",),
            ("backslash\\name.md",),
            ("less<than.md",),
            ("greater>than.md",),
            ('double"quote.md',),
            ("pipe|name.md",),
            ("question?.md",),
            ("star*.md",),
            ("control\u0001name.md",),
            ("trailing-space ",),
            ("trailing-dot.",),
            ("CON.md",),
        )
        for parts in unsafe:
            with self.subTest(parts=parts):
                with self.assertRaisesRegex(
                    ValueError,
                    "extraction-safe on Windows",
                ):
                    validate_archive_relative_parts(parts)
        validate_archive_relative_parts(
            ("windows", "Export-LabEtwEvidence.ps1")
        )
        with self.assertRaisesRegex(
            ValueError,
            "case-insensitive Windows extraction",
        ):
            validate_archive_name_set(
                (
                    "lab/mt5_direct_endpoint/Foo.md",
                    "lab/mt5_direct_endpoint/foo.md",
                )
            )

    def test_archive_failure_leaves_no_partial_output(self) -> None:
        repo_root = LAB_ROOT.parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "review.zip"
            with mock.patch.object(
                zipfile.ZipFile,
                "writestr",
                side_effect=OSError("synthetic write failure"),
            ):
                with self.assertRaisesRegex(
                    OSError,
                    "synthetic write failure",
                ):
                    build_archive(destination, repo_root=repo_root)
            self.assertFalse(destination.exists())
            self.assertEqual(
                list(Path(temporary).glob(".review.zip.*.tmp")),
                [],
            )


if __name__ == "__main__":
    unittest.main()
