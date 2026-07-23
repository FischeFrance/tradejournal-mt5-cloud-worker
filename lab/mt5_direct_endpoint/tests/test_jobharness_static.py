from __future__ import annotations

import unittest
from pathlib import Path


LAB_ROOT = Path(__file__).resolve().parents[1]
JOB_ROOT = LAB_ROOT / "src" / "JobHarness"


class JobHarnessStaticTests(unittest.TestCase):
    def test_actual_launch_is_default_closed_and_runtime_hard_disabled(self) -> None:
        source = (JOB_ROOT / "HarnessApplication.cs").read_text(encoding="utf-8")
        missing_execute = 'if (!options.ExecuteRequested)'
        runtime_gate = 'if (!ActualLaunchRuntimeValidated)'
        launch_boundary = 'runtime.RunJob(options, metadata, writer)'
        self.assertIn(missing_execute, source)
        self.assertIn(runtime_gate, source)
        self.assertIn('"actual_launch_runtime_validation_required"', source)
        self.assertLess(source.index(missing_execute), source.index(launch_boundary))
        self.assertLess(source.index(runtime_gate), source.index(launch_boundary))

    def test_execute_contract_requires_metadata_and_expected_hash(self) -> None:
        source = (JOB_ROOT / "HarnessApplication.cs").read_text(encoding="utf-8")
        self.assertIn('"metadata_required"', source)
        self.assertIn('"expected_sha256_required"', source)
        self.assertIn('"executable_sha256_mismatch"', source)
        self.assertIn('options.ExecuteRequested && options.DryRun', source)

    def test_hash_comparison_is_fixed_time_under_lease(self) -> None:
        lease = (JOB_ROOT / "TargetExecutableLease.cs").read_text(encoding="utf-8")
        application = (JOB_ROOT / "HarnessApplication.cs").read_text(encoding="utf-8")
        self.assertIn("CryptographicOperations.FixedTimeEquals", lease)
        self.assertIn("FileShare.Read", lease)
        self.assertIn("using (targetLease)", application)
        self.assertLess(
            application.index("using (targetLease)"),
            application.index("runtime.RunJob(options, metadata, writer)"),
        )

    def test_metadata_admits_unverified_canonical_and_authenticode_bindings(self) -> None:
        metadata = (JOB_ROOT / "HarnessMetadata.cs").read_text(encoding="utf-8")
        self.assertIn('JsonPropertyName("canonical_path_verified")', metadata)
        self.assertIn('JsonPropertyName("file_identity_verified")', metadata)
        self.assertIn('JsonPropertyName("actual_launch_capability")', metadata)
        self.assertIn('"HARD_DISABLED"', metadata)
        self.assertIn('JsonPropertyName("provider_signer_binding_verified")', metadata)
        self.assertIn("LaunchApproved = false", metadata)

    def test_sensitive_values_are_not_part_of_metadata_contract(self) -> None:
        metadata = (JOB_ROOT / "HarnessMetadata.cs").read_text(encoding="utf-8")
        lowered = metadata.casefold()
        for forbidden in (
            'jsonpropertyname("password")',
            'jsonpropertyname("account_number")',
            'jsonpropertyname("command_line")',
            'jsonpropertyname("environment_values")',
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, lowered)
        self.assertIn("ArgumentsRecorded { get; init; } = false", metadata)
        self.assertIn("EnvironmentValuesRecorded { get; init; } = false", metadata)


if __name__ == "__main__":
    unittest.main()
