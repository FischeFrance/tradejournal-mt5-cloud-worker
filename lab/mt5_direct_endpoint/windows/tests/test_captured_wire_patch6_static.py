from __future__ import annotations

import re
import unittest
from pathlib import Path


WINDOWS_ROOT = Path(__file__).resolve().parents[1]
MARKER_SOURCE = (
    WINDOWS_ROOT / "Write-LabPhaseMarker.ps1"
).read_text(encoding="utf-8")
EXPORT_SOURCE = (
    WINDOWS_ROOT / "Export-LabEtwEvidence.ps1"
).read_text(encoding="utf-8")

EXPECTED_CONTROL_CODES = {
    "C0": (
        "C0_BASELINE_START",
        "C0_BASELINE_END",
    ),
    "C1": (
        "C1_DISCOVERY_NEGATIVE_START",
        "C1_DISCOVERY_NEGATIVE_END",
        "C1_DISCOVERY_EXACT_START",
        "C1_DISCOVERY_EXACT_END",
    ),
    "C2": (
        "C2_LOGIN_START",
        "C2_LOGIN_END",
        "C2_CONNECTED_START",
        "C2_CONNECTED_END",
        "C2_NETWORK_INTERRUPTION_START",
        "C2_NETWORK_INTERRUPTION_END",
        "C2_RECONNECT_START",
        "C2_RECONNECT_END",
    ),
    "C3": (
        "C3_DIRECT_LOGIN_START",
        "C3_DIRECT_LOGIN_END",
        "C3_CONNECTED_STEADY_START",
        "C3_CONNECTED_STEADY_END",
    ),
    "C4": (
        "C4_ENDPOINT_BLOCKED_START",
        "C4_ENDPOINT_BLOCKED_END",
    ),
    "C5": (
        "C5_DIRECT_LOGIN_START",
        "C5_DIRECT_LOGIN_END",
        "C5_CONNECTED_STEADY_START",
        "C5_CONNECTED_STEADY_END",
    ),
}


def extract_control_codes(source: str) -> dict[str, tuple[str, ...]]:
    block = source.split("function Get-LabControlMarkerCodes", maxsplit=1)[1]
    block = block.split("function Test-LabIntegerValue", maxsplit=1)[0]
    result: dict[str, tuple[str, ...]] = {}
    for control in EXPECTED_CONTROL_CODES:
        branch = re.search(
            rf"'{control}'\s*\{{(.*?)\n\s*\}}",
            block,
            flags=re.DOTALL,
        )
        if branch is None:
            raise AssertionError(f"missing marker switch branch for {control}")
        result[control] = tuple(
            re.findall(r"'((?:C[0-5])_[A-Z0-9_]+_(?:START|END))'", branch.group(1))
        )
    return result


class CapturedWirePatch6StaticTests(unittest.TestCase):
    def test_marker_uses_patch6_phase_vocabulary(self) -> None:
        validate_set = re.search(
            r"\[ValidateSet\((.*?)\)\]\s*\[string\]\$Phase",
            MARKER_SOURCE,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(validate_set)
        source = validate_set.group(1)
        expected = {
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
        }
        self.assertEqual(expected, set(re.findall(r"'([^']+)'", source)))
        self.assertNotIn("C3_DIRECT_ONLY", source)
        self.assertNotIn("C5_DIRECT_REPEAT", source)

    def test_marker_v2_is_structured_and_contiguous(self) -> None:
        for field in (
            "code",
            "sequence",
            "timestamp_unix_ms",
            "qpc",
            "qpc_frequency_hz",
        ):
            self.assertRegex(MARKER_SOURCE, rf"(?m)^\s*{field}\s*=")
        self.assertIn("schema_version        = 2", MARKER_SOURCE)
        self.assertIn(
            "La sequenza marker deve essere contigua e partire da 1.",
            MARKER_SOURCE,
        )
        self.assertIn(
            "L ordine dei marker non coincide con la sequenza esatta del controllo.",
            MARKER_SOURCE,
        )
        self.assertIn("MT5LAB|v2|", MARKER_SOURCE)
        self.assertIn("qpc -le [int64]$previousQpc", MARKER_SOURCE)

    def test_both_scripts_encode_the_exact_patch6_control_sequences(self) -> None:
        self.assertEqual(
            EXPECTED_CONTROL_CODES,
            extract_control_codes(MARKER_SOURCE),
        )
        self.assertEqual(
            EXPECTED_CONTROL_CODES,
            extract_control_codes(EXPORT_SOURCE),
        )

    def test_exporter_consumes_marker_v2_time_and_order(self) -> None:
        self.assertIn(
            "[DateTimeOffset]::FromUnixTimeMilliseconds",
            EXPORT_SOURCE,
        )
        self.assertIn(
            "La sequenza marker deve essere contigua e partire da 1.",
            EXPORT_SOURCE,
        )
        self.assertIn(
            "L ordine dei marker non coincide con la sequenza esatta del controllo.",
            EXPORT_SOURCE,
        )
        self.assertIn("marker_schema_version  = 2", EXPORT_SOURCE)
        phase_function = re.search(
            r"function Get-PhaseForTimestamp \{(.*?)\n\}",
            EXPORT_SOURCE,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(phase_function)
        self.assertNotIn("Select-Object -First 1", phase_function.group(1))
        self.assertIn(
            "($actualControlCodes -join ',') -ceq ($requiredCodes -join ',')",
            EXPORT_SOURCE,
        )
        equal_boundary = re.search(
            r"if \(\$sortedIntervals\[\$intervalIndex\]\.start_utc -eq "
            r"\$sortedIntervals\[\$intervalIndex - 1\]\.end_utc\) \{(.*?)\n\s*\}",
            EXPORT_SOURCE,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(equal_boundary)
        self.assertIn(
            "$phaseMappingComplete = $false",
            equal_boundary.group(1),
        )

    def test_sanitized_event_v2_has_nullable_provider_connection_hash(self) -> None:
        self.assertIn("schema_version          = 2", EXPORT_SOURCE)
        self.assertIn("connection_id_sha256", EXPORT_SOURCE)
        helper = re.search(
            r"function Get-SafeProviderConnectionIdHash \{(.*?)\n\}",
            EXPORT_SOURCE,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(helper)
        helper_source = helper.group(1)
        self.assertIn(
            "@('ConnectionId', 'ConnectionID', 'ConnId', 'ConnID')",
            helper_source,
        )
        self.assertIn("return $null", helper_source)
        self.assertIn("Get-Sha256OpaqueText", helper_source)
        self.assertNotIn("Get-Sha256Text ", helper_source)
        for forbidden_source in (
            "$remoteAddress",
            "$remotePort",
            "$localAddress",
            "$localPort",
            "$payloadPid",
            "$timestamp",
        ):
            self.assertNotIn(forbidden_source, helper_source)

    def test_exporter_remains_non_probatory_no_go(self) -> None:
        self.assertGreaterEqual(
            len(re.findall(r"readiness\s*=\s*'NO_GO'", EXPORT_SOURCE)),
            2,
        )
        self.assertGreaterEqual(
            len(re.findall(r"proof_capable\s*=\s*\$false", EXPORT_SOURCE)),
            2,
        )
        self.assertNotRegex(
            EXPORT_SOURCE,
            r"proof_capable\s*=\s*\$true",
        )


if __name__ == "__main__":
    unittest.main()
