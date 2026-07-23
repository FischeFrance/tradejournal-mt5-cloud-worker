import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "windows" / "Invoke-LabPrivateBootstrap.ps1"


class PrivateBootstrapStaticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = SCRIPT.read_text(encoding="utf-8")

    def test_plan_only_is_the_default_and_no_go(self) -> None:
        self.assertRegex(self.source, r"\[string\]\$Mode\s*=\s*'PlanOnly'")
        self.assertIn("accesses_credentials         = $false", self.source)
        self.assertIn("writes_private_files         = $false", self.source)
        self.assertIn("deletes_private_files        = $false", self.source)
        self.assertIn("readiness                    = 'NO_GO'", self.source)
        self.assertIn("proof_capable                = $false", self.source)

    def test_both_active_modes_are_hard_disabled(self) -> None:
        self.assertIn("create_capability            = 'HARD_DISABLED'", self.source)
        self.assertIn("remove_capability            = 'HARD_DISABLED'", self.source)
        self.assertIn("CAPABILITY_HARD_DISABLED", self.source)
        self.assertNotIn("OSVersion.Platform", self.source)
        self.assertNotIn("ShouldProcess", self.source)

    def test_no_secret_input_or_file_mutation_implementation_exists(self) -> None:
        for forbidden in (
            "PSCredential",
            "SecureString",
            "GetNetworkCredential",
            "SecureStringToGlobalAllocUnicode",
            "Set-Content",
            "Add-Content",
            "Out-File",
            "CreateNew",
            "Remove-Item",
            "Directory]::Delete",
        ):
            self.assertNotIn(forbidden, self.source)

    def test_paths_match_the_canonical_mql5_sandbox(self) -> None:
        self.assertIn("C:\\TJLab\\", self.source)
        self.assertIn(
            "Join-LabPath $expectedPortableRoot 'MQL5\\Files\\MT5DirectEndpointLab'",
            self.source,
        )
        self.assertIn("expected-account.txt", self.source)
        self.assertIn("startup.ini", self.source)
        self.assertIn("layout canonico experiment/run/control", self.source)

    def test_future_contract_is_fail_closed(self) -> None:
        for required in (
            "ProxyEnable=0",
            "KeepPrivate=0",
            "AllowLiveTrading=0",
            "AllowDllImport=0",
            "Expert=TradeJournal\\TradeJournalIdentityProbe",
            "attestazione firmata",
            "security descriptor",
            "buffer managed",
            "file-ID",
        ):
            self.assertIn(required, self.source)


if __name__ == "__main__":
    unittest.main()
