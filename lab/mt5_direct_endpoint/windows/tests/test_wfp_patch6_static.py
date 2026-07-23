from __future__ import annotations

import re
import unittest
from pathlib import Path


WINDOWS_ROOT = Path(__file__).resolve().parents[1]
WFP_SOURCE = (
    WINDOWS_ROOT / "Export-LabWfpSecurityEvidence.ps1"
).read_text(encoding="utf-8")
README_SOURCE = (WINDOWS_ROOT / "README.md").read_text(encoding="utf-8")
FIREWALL_PLAN_SOURCE = (WINDOWS_ROOT / "New-LabFirewallPlan.ps1").read_text(
    encoding="utf-8"
)


class WfpPatch6StaticTests(unittest.TestCase):
    def test_wfp_exporter_uses_only_patch6_direct_phase_names(self) -> None:
        for required in (
            "C3_DIRECT_LOGIN",
            "C3_CONNECTED_STEADY",
            "C4_ENDPOINT_BLOCKED",
            "C5_DIRECT_LOGIN",
            "C5_CONNECTED_STEADY",
        ):
            self.assertIn(required, WFP_SOURCE)
        self.assertNotIn("C3_DIRECT_ONLY", WFP_SOURCE)
        self.assertNotIn("C5_DIRECT_REPEAT", WFP_SOURCE)

    def test_wfp_plan_exposes_plural_phases_and_remains_hard_disabled(self) -> None:
        plan_only = WFP_SOURCE.split("if ($Mode -eq 'PlanOnly')", maxsplit=1)[1]
        plan_only = plan_only.split(
            "throw 'CAPABILITY_HARD_DISABLED",
            maxsplit=1,
        )[0]
        self.assertRegex(plan_only, r"phases\s*=\s*\$ExpectedPhases")
        self.assertIn("phase_vocabulary", plan_only)
        self.assertIn("PATCH6_EXACT_CONTROL_TIMELINE_V1", WFP_SOURCE)
        self.assertIn("proof_capable", plan_only)
        self.assertRegex(plan_only, r"proof_capable\s*=\s*\$false")
        self.assertIn("execute_capability", plan_only)
        self.assertIn("'HARD_DISABLED'", plan_only)

    def test_wfp_unreachable_preparatory_path_consumes_marker_v2(self) -> None:
        self.assertIn("[DateTimeOffset]::FromUnixTimeMilliseconds", WFP_SOURCE)
        self.assertIn("timestamp_unix_ms", WFP_SOURCE)
        self.assertIn("schema_version -ne 2", WFP_SOURCE)
        self.assertIn(
            "I marker Security/WFP non coincidono con la timeline Patch 6",
            WFP_SOURCE,
        )

    def test_readme_declares_external_deny_decision_b(self) -> None:
        self.assertIn("DEFENSE_IN_DEPTH_NON_PROBATORY", README_SOURCE)
        self.assertRegex(
            README_SOURCE,
            r"non e un gate probatorio",
        )
        self.assertNotRegex(
            README_SOURCE,
            r"obbligatorio un secondo guard default-deny",
        )

    def test_firewall_plan_calls_external_guard_non_probatory(self) -> None:
        self.assertIn(
            "Difesa operativa addizionale raccomandata",
            FIREWALL_PLAN_SOURCE,
        )
        self.assertIn(
            "non produce proof binding e non partecipa al verdict C0-C5",
            FIREWALL_PLAN_SOURCE,
        )
        self.assertNotIn(
            "Controllo indipendente obbligatorio",
            FIREWALL_PLAN_SOURCE,
        )

    def test_readme_has_exact_patch6_timeline_vocabulary(self) -> None:
        timeline = README_SOURCE.split(
            "Le fasi supportate sono:",
            maxsplit=1,
        )[1].split("```", maxsplit=2)[1]
        expected = (
            "C0_BASELINE",
            "C1_DISCOVERY_NEGATIVE",
            "C1_DISCOVERY_EXACT",
            "C2_LOGIN",
            "C2_CONNECTED",
            "C2_NETWORK_INTERRUPTION",
            "C2_RECONNECT",
            "C3_DIRECT_LOGIN",
            "C3_CONNECTED_STEADY",
            "C4_ENDPOINT_BLOCKED",
            "C5_DIRECT_LOGIN",
            "C5_CONNECTED_STEADY",
            "TEARDOWN",
        )
        self.assertEqual(expected, tuple(re.findall(r"^([A-Z0-9_]+)$", timeline, re.M)))
        self.assertNotIn("C3_DIRECT_ONLY", timeline)
        self.assertNotIn("C5_DIRECT_REPEAT", timeline)


if __name__ == "__main__":
    unittest.main()
