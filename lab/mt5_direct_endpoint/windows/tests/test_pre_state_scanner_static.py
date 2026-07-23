from __future__ import annotations

import json
import re
import unittest
from pathlib import Path


WINDOWS_ROOT = Path(__file__).resolve().parents[1]
SCANNER = WINDOWS_ROOT / "Get-LabPreState.ps1"
DRY_RUN = WINDOWS_ROOT / "tests" / "Invoke-PreStateScannerDryRunTest.ps1"
SCHEMA = WINDOWS_ROOT / "pre-state-report.schema.json"


class PreStateScannerStaticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = SCANNER.read_text(encoding="utf-8")
        cls.lower = cls.source.lower()

    def test_contract_files_exist_and_schema_is_json(self) -> None:
        self.assertTrue(SCANNER.is_file())
        self.assertTrue(DRY_RUN.is_file())
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        self.assertEqual(schema["properties"]["schema_version"]["const"], 1)
        self.assertEqual(schema["properties"]["checks"]["minItems"], 16)

    def test_plan_only_is_default_and_read_only_is_explicit(self) -> None:
        parameter_block = self.source.split("Set-StrictMode", 1)[0]
        self.assertRegex(parameter_block, r"\[string\]\$Mode\s*=\s*'PlanOnly'")
        self.assertIn("[ValidateSet('PlanOnly', 'ReadOnlyChecks')]", parameter_block)
        self.assertIn("if ($Mode -ceq 'PlanOnly')", self.source)

    def test_no_network_or_system_mutation_cmdlets(self) -> None:
        forbidden = (
            "invoke-webrequest",
            "invoke-restmethod",
            "test-netconnection",
            "resolve-dnsname",
            "new-netfirewallrule",
            "set-netfirewallprofile",
            "remove-netfirewallrule",
            "set-itemproperty",
            "new-itemproperty",
            "remove-itemproperty",
            "reg.exe add",
            "reg.exe delete",
            "wpr -start",
            "wpr -stop",
            "terminal64.exe /",
            "metaeditor64.exe /",
        )
        for token in forbidden:
            with self.subTest(token=token):
                self.assertNotIn(token, self.lower)

    def test_only_explicit_output_code_writes_a_file(self) -> None:
        write_function = self.source.split("function Write-LabNewJsonFile", 1)[1]
        write_function = write_function.split("function Get-LabPlanOnlyChecks", 1)[0]
        self.assertIn("[IO.File]::Open", write_function)
        outside = self.source.replace(write_function, "")
        for token in ("Set-Content", "Add-Content", "Out-File", "Export-Clixml"):
            self.assertNotIn(token, self.source)
        self.assertNotIn("[IO.File]::Open", outside)
        self.assertIn("[IO.FileMode]::CreateNew", write_function)

    def test_no_recursive_follow_on_appdata_or_registry_content_export(self) -> None:
        appdata = self.source.split("function Get-LabAppDataAssessment", 1)[1]
        appdata = appdata.split("function Get-LabMetaQuotesRegistryAssessment", 1)[0]
        self.assertNotIn("-Recurse", appdata)
        registry = self.source.split("function Get-LabMetaQuotesRegistryAssessment", 1)[1]
        registry = registry.split("function Get-LabProcessAssessment", 1)[0]
        self.assertNotIn("GetValueNames", registry)
        self.assertNotIn("GetSubKeyNames", registry)

    def test_report_has_no_raw_detail_fields(self) -> None:
        prohibited_properties = (
            "full_name",
            "raw_path",
            "target_name",
            "credential_name",
            "proxy_server",
            "auto_config_url",
            "process_id",
            "command_line",
        )
        for name in prohibited_properties:
            with self.subTest(name=name):
                self.assertNotRegex(self.source, rf"(?im)^\s*{re.escape(name)}\s*=")
        self.assertIn("raw_paths_exported             = $false", self.source)
        self.assertIn("credential_names_exported      = $false", self.source)
        self.assertIn("proxy_values_exported          = $false", self.source)

    def test_all_required_cold_state_targets_are_covered(self) -> None:
        required_tokens = (
            "Config\\accounts.dat",
            "Config\\servers.dat",
            "Bases",
            "MQL5\\Profiles",
            "MetaQuotes",
            "Registry]::CurrentUser",
            "terminal64",
            "metaeditor64",
            "ReparsePoint",
            "DriveType]::Fixed",
            "WinHttpSettings",
            "DefaultConnectionSettings",
            "cmdkey.exe",
            "expected-account.txt",
        )
        for token in required_tokens:
            with self.subTest(token=token):
                self.assertIn(token, self.source)

    def test_operator_only_fields_are_not_automatically_promoted_true(self) -> None:
        projection = self.source.split("function New-LabEvidenceProjection", 1)[1]
        projection = projection.split("function New-LabPreStateReport", 1)[0]
        always_null = (
            "portable_root_new",
            "disposable_clone_new",
            "windows_user_new",
            "no_shared_storage",
            "terminal_data_path_matches",
        )
        for field in always_null:
            with self.subTest(field=field):
                self.assertRegex(projection, rf"(?m)^\s*{field}\s*=\s*\$null\s*$")
        self.assertIn("if ($credentialCheck.status -ceq 'FAIL') { $false } else { $null }", projection)
        self.assertIn("if ($communityCheck.status -ceq 'FAIL') { $false } else { $null }", projection)

    def test_script_does_not_contain_real_endpoints_or_credentials(self) -> None:
        self.assertNotRegex(self.source, r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
        self.assertNotRegex(self.source, r"(?i)password\s*[:=]")
        self.assertNotRegex(self.source, r"(?i)account\s*[:=]\s*\d+")

    def test_active_paths_are_confined_to_the_dedicated_lab_root(self) -> None:
        self.assertIn("Test-LabPathUnderDedicatedRoot", self.source)
        self.assertIn("PORTABLE_ROOT_MUST_BE_UNDER_C_TJLAB", self.source)
        self.assertIn("PRIVATE_DIRECTORY_MUST_BE_UNDER_C_TJLAB", self.source)
        self.assertIn("OUTPUT_PATH_MUST_BE_UNDER_C_TJLAB", self.source)


if __name__ == "__main__":
    unittest.main()
