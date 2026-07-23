from __future__ import annotations

import copy
import hashlib
import sys
import unittest
from pathlib import Path


LAB_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB_ROOT / "tools"))

from lab_model import LabValidationError  # noqa: E402
from network_summary import DIRECT_PHASES, PHASES, summarize_events  # noqa: E402


RUN_ID = "00000000-0000-4000-8000-000000000099"
CANDIDATE = {
    "ip": "8.8.8.8",
    "port": 443,
    "source_control": "C2",
    "observed_phase": "LOGIN",
    "process_scoped": True,
}
_AUTO_CONNECTION_ID = object()


def event(
    phase: str,
    category: str,
    address: str | None = None,
    port: int | None = None,
    *,
    status: str | int | None = None,
    connection_id_sha256: str | None | object = _AUTO_CONNECTION_ID,
) -> dict[str, object]:
    if connection_id_sha256 is _AUTO_CONNECTION_ID:
        connection_id_sha256 = (
            hashlib.sha256(
                f"{phase}\0{category}\0{address}\0{port}".encode("utf-8")
            ).hexdigest()
            if category == "NETWORK"
            else None
        )
    return {
        "schema_version": 2,
        "run_id": RUN_ID,
        "phase": phase,
        "timestamp_utc": "2026-01-01T00:00:00.0000000Z",
        "category": category,
        "provider_name": "Synthetic",
        "provider_guid": None,
        "event_id": 1,
        "task": None,
        "opcode": None,
        "header_process_id": 100,
        "header_thread_id": 101,
        "payload_process_id": 100,
        "parent_process_id": None,
        "process_guid": None,
        "parent_process_guid": None,
        "connection_id_sha256": connection_id_sha256,
        "image_name": "terminal64.exe",
        "image_path_sha256": "a" * 64,
        "image_matches_terminal": True,
        "protocol": "TCP",
        "local_address": "192.0.2.10",
        "local_port": 50000,
        "remote_address": address,
        "remote_port": port,
        "dns_query_name": None,
        "dns_result_addresses": [],
        "status": status,
    }


class NetworkSummaryTests(unittest.TestCase):
    def test_phase_vocabulary_matches_patch6_timeline_intervals(self) -> None:
        self.assertEqual(
            PHASES,
            (
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
            ),
        )
        self.assertEqual(
            DIRECT_PHASES,
            (
                "C3_DIRECT_LOGIN",
                "C3_CONNECTED_STEADY",
                "C4_ENDPOINT_BLOCKED",
                "C5_DIRECT_LOGIN",
                "C5_CONNECTED_STEADY",
            ),
        )

        summary = summarize_events(
            [event(phase, "OTHER_CAPTURED") for phase in PHASES],
            run_id=RUN_ID,
            candidate=None,
            source_sha256="f" * 64,
        )
        self.assertEqual(
            set(summary["phase_endpoint_sets"]),
            set(PHASES),
        )

        for legacy_phase in ("C3_DIRECT_ONLY", "C5_DIRECT_REPEAT"):
            with self.subTest(legacy_phase=legacy_phase), self.assertRaisesRegex(
                LabValidationError,
                "unknown phase",
            ):
                summarize_events(
                    [event(legacy_phase, "OTHER_CAPTURED")],
                    run_id=RUN_ID,
                    candidate=None,
                    source_sha256="f" * 64,
                )

    def test_phase_deltas_and_hidden_fallback_are_explicit(self) -> None:
        events = [
            event("C0_BASELINE", "NETWORK", "1.1.1.1", 443),
            event("C1_DISCOVERY_NEGATIVE", "NETWORK", "1.1.1.1", 443),
            event("C1_DISCOVERY_EXACT", "NETWORK", "1.1.1.1", 443),
            event("C1_DISCOVERY_EXACT", "NETWORK", "8.8.4.4", 443),
            event("C2_LOGIN", "NETWORK", "8.8.8.8", 443),
            event("C3_DIRECT_LOGIN", "NETWORK", "8.8.8.8", 443),
            event("C3_CONNECTED_STEADY", "NETWORK", "9.9.9.9", 443),
            event("C3_CONNECTED_STEADY", "DNS"),
        ]
        summary = summarize_events(
            events, run_id=RUN_ID, candidate=CANDIDATE, source_sha256="f" * 64
        )
        self.assertEqual(summary["deltas"]["discovery_common"], ["1.1.1.1:443"])
        self.assertEqual(summary["deltas"]["discovery_exact_only"], ["8.8.4.4:443"])
        self.assertEqual(summary["deltas"]["login_only"], ["8.8.8.8:443"])
        self.assertEqual(summary["schema_version"], 2)
        self.assertEqual(
            summary["discovery_delta_source"]["kind"],
            "PROCESS_SCOPED_TCP_FLOW_SET",
        )
        self.assertTrue(summary["discovery_delta_source"]["verified"])
        self.assertRegex(summary["discovery_delta_source"]["sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            summary["unexpected_direct_endpoints"]["C3_CONNECTED_STEADY"],
            ["9.9.9.9:443"],
        )
        self.assertTrue(summary["interpretation"]["fallback_present"])
        self.assertTrue(summary["interpretation"]["direct_dns_present"])
        self.assertTrue(summary["interpretation"]["candidate_seen_in_c3"])
        self.assertFalse(summary["interpretation"]["connection_success_proven"])
        self.assertTrue(summary["interpretation"]["requires_wfp_and_identity_corroboration"])

    def test_new_direct_and_interruption_phases_feed_accounting(self) -> None:
        summary = summarize_events(
            [
                event(
                    "C2_NETWORK_INTERRUPTION",
                    "NETWORK",
                    "9.9.9.9",
                    443,
                    status="blocked",
                ),
                event(
                    "C3_DIRECT_LOGIN",
                    "NETWORK",
                    "8.8.8.8",
                    443,
                    status="connected",
                ),
                event(
                    "C5_CONNECTED_STEADY",
                    "NETWORK",
                    "8.8.8.8",
                    443,
                    status="connected",
                ),
            ],
            run_id=RUN_ID,
            candidate=CANDIDATE,
            source_sha256="f" * 64,
        )
        interruption = summary["flow_accounting_by_phase"][
            "C2_NETWORK_INTERRUPTION"
        ]
        self.assertEqual(interruption["process_scoped_tcp_flows"], 1)
        self.assertEqual(interruption["other_tcp_flows"], 1)
        self.assertTrue(summary["interpretation"]["candidate_seen_in_c3"])
        self.assertTrue(summary["interpretation"]["candidate_seen_in_c5"])

    def test_tcp_flow_accounting_is_exclusive_and_dispositions_are_derived(self) -> None:
        events = [
            event(
                "C3_CONNECTED_STEADY",
                "NETWORK",
                "8.8.8.8",
                443,
                status="connected",
            ),
            event(
                "C3_CONNECTED_STEADY",
                "NETWORK",
                "9.9.9.9",
                443,
                status="blocked",
            ),
            event("C3_CONNECTED_STEADY", "DNS"),
        ]
        summary = summarize_events(
            events, run_id=RUN_ID, candidate=CANDIDATE, source_sha256="f" * 64
        )
        accounting = summary["flow_accounting_by_phase"]["C3_CONNECTED_STEADY"]
        self.assertEqual(accounting["process_scoped_tcp_flows"], 2)
        self.assertEqual(accounting["candidate_tcp_flows"], 1)
        self.assertEqual(accounting["other_tcp_flows"], 1)
        self.assertEqual(accounting["non_tcp_network_events"], 0)
        self.assertEqual(
            accounting["process_scoped_tcp_flows"],
            accounting["candidate_tcp_flows"] + accounting["other_tcp_flows"],
        )
        self.assertEqual(accounting["dns_events"], 1)
        self.assertEqual(accounting["candidate_dispositions"]["connected"], 1)
        self.assertEqual(accounting["candidate_dispositions"]["blocked"], 0)
        self.assertEqual(accounting["other_dispositions"]["blocked"], 1)
        self.assertTrue(summary["flow_record_set_verified"])
        self.assertRegex(summary["flow_record_set_sha256"], r"^[0-9a-f]{64}$")

    def test_direct_non_tcp_network_event_is_explicit_for_fail_closed_gate(self) -> None:
        non_tcp = event(
            "C3_CONNECTED_STEADY", "NETWORK", "8.8.8.8", 443
        )
        non_tcp["protocol"] = "UDP"
        summary = summarize_events(
            [non_tcp],
            run_id=RUN_ID,
            candidate=CANDIDATE,
            source_sha256="f" * 64,
        )
        accounting = summary["flow_accounting_by_phase"]["C3_CONNECTED_STEADY"]
        self.assertEqual(accounting["process_scoped_tcp_flows"], 0)
        self.assertEqual(accounting["candidate_tcp_flows"], 0)
        self.assertEqual(accounting["other_tcp_flows"], 0)
        self.assertEqual(accounting["non_tcp_network_events"], 1)
        self.assertTrue(
            summary["interpretation"]["direct_non_tcp_network_present"]
        )

    def test_repeated_records_form_one_flow_and_merge_one_disposition(self) -> None:
        attempted = event(
            "C3_CONNECTED_STEADY", "NETWORK", "8.8.8.8", 443
        )
        connected = copy.deepcopy(attempted)
        connected["status"] = "success"
        summary = summarize_events(
            [attempted, connected],
            run_id=RUN_ID,
            candidate=CANDIDATE,
            source_sha256="f" * 64,
        )
        accounting = summary["flow_accounting_by_phase"]["C3_CONNECTED_STEADY"]
        self.assertEqual(accounting["process_scoped_tcp_flows"], 1)
        self.assertEqual(accounting["candidate_tcp_flows"], 1)
        self.assertEqual(accounting["other_tcp_flows"], 0)
        self.assertEqual(accounting["candidate_dispositions"]["connected"], 1)
        self.assertEqual(
            accounting["candidate_dispositions"]["attempted_not_connected"], 0
        )

    def test_discovery_delta_requires_an_explicit_process_scoped_flow_set(self) -> None:
        no_delta = [
            event("C1_DISCOVERY_NEGATIVE", "NETWORK", "1.1.1.1", 443),
            event("C1_DISCOVERY_EXACT", "NETWORK", "1.1.1.1", 443),
        ]
        summary = summarize_events(
            no_delta, run_id=RUN_ID, candidate=None, source_sha256="f" * 64
        )
        self.assertEqual(
            summary["discovery_delta_source"],
            {"kind": "NONE", "sha256": None, "verified": False},
        )
        self.assertFalse(
            summary["interpretation"]["discovery_delta_has_process_scoped_source"]
        )

        with_delta = no_delta + [
            event("C1_DISCOVERY_EXACT", "NETWORK", "8.8.4.4", 443)
        ]
        summary = summarize_events(
            with_delta, run_id=RUN_ID, candidate=None, source_sha256="f" * 64
        )
        source = summary["discovery_delta_source"]
        self.assertEqual(source["kind"], "PROCESS_SCOPED_TCP_FLOW_SET")
        self.assertTrue(source["verified"])
        self.assertRegex(source["sha256"], r"^[0-9a-f]{64}$")

    def test_unattributed_tcp_event_is_rejected_fail_closed(self) -> None:
        unattributed = event("C2_LOGIN", "NETWORK", "8.8.8.8", 443)
        unattributed["payload_process_id"] = None
        unattributed["image_matches_terminal"] = False
        with self.assertRaisesRegex(
            LabValidationError, "lacks process-scoped attribution"
        ):
            summarize_events(
                [unattributed],
                run_id=RUN_ID,
                candidate=CANDIDATE,
                source_sha256="f" * 64,
            )

    def test_contradictory_dispositions_make_the_flow_set_unverified(self) -> None:
        connected = event(
            "C4_ENDPOINT_BLOCKED",
            "NETWORK",
            "8.8.8.8",
            443,
            status="connected",
        )
        blocked = copy.deepcopy(connected)
        blocked["status"] = "blocked"
        summary = summarize_events(
            [connected, blocked],
            run_id=RUN_ID,
            candidate=CANDIDATE,
            source_sha256="f" * 64,
        )
        accounting = summary["flow_accounting_by_phase"]["C4_ENDPOINT_BLOCKED"]
        self.assertFalse(summary["flow_record_set_verified"])
        self.assertFalse(accounting["flow_records_verified"])
        self.assertEqual(accounting["candidate_tcp_flows"], 2)
        self.assertEqual(accounting["candidate_dispositions"]["connected"], 1)
        self.assertEqual(accounting["candidate_dispositions"]["blocked"], 1)

    def test_missing_connection_digest_stays_diagnostic_and_unverified(self) -> None:
        summary = summarize_events(
            [
                event(
                    "C1_DISCOVERY_EXACT",
                    "NETWORK",
                    "8.8.4.4",
                    443,
                    connection_id_sha256=None,
                )
            ],
            run_id=RUN_ID,
            candidate=None,
            source_sha256="f" * 64,
        )
        accounting = summary["flow_accounting_by_phase"]["C1_DISCOVERY_EXACT"]
        self.assertEqual(accounting["process_scoped_tcp_flows"], 1)
        self.assertFalse(accounting["flow_records_verified"])
        self.assertFalse(summary["flow_record_set_verified"])
        self.assertEqual(
            summary["discovery_delta_source"]["kind"],
            "PROCESS_SCOPED_TCP_FLOW_SET",
        )
        self.assertFalse(summary["discovery_delta_source"]["verified"])

    def test_reused_connection_digest_with_different_tuple_is_unverified(self) -> None:
        first = event("C2_LOGIN", "NETWORK", "8.8.8.8", 443)
        second = event("C2_LOGIN", "NETWORK", "9.9.9.9", 443)
        second["connection_id_sha256"] = first["connection_id_sha256"]
        summary = summarize_events(
            [first, second],
            run_id=RUN_ID,
            candidate=CANDIDATE,
            source_sha256="f" * 64,
        )
        accounting = summary["flow_accounting_by_phase"]["C2_LOGIN"]
        self.assertEqual(accounting["process_scoped_tcp_flows"], 2)
        self.assertEqual(accounting["candidate_tcp_flows"], 1)
        self.assertEqual(accounting["other_tcp_flows"], 1)
        self.assertFalse(accounting["flow_records_verified"])
        self.assertFalse(summary["flow_record_set_verified"])

    def test_flow_record_digest_changes_with_the_classified_flow_set(self) -> None:
        first = summarize_events(
            [event("C2_LOGIN", "NETWORK", "8.8.8.8", 443)],
            run_id=RUN_ID,
            candidate=CANDIDATE,
            source_sha256="f" * 64,
        )
        second = summarize_events(
            [
                event("C2_LOGIN", "NETWORK", "8.8.8.8", 443),
                event("C2_LOGIN", "NETWORK", "9.9.9.9", 443),
            ],
            run_id=RUN_ID,
            candidate=CANDIDATE,
            source_sha256="f" * 64,
        )
        self.assertNotEqual(
            first["flow_record_set_sha256"], second["flow_record_set_sha256"]
        )

    def test_flow_record_digest_is_domain_and_run_bound(self) -> None:
        first_event = event("C2_LOGIN", "NETWORK", "8.8.8.8", 443)
        first = summarize_events(
            [first_event],
            run_id=RUN_ID,
            candidate=CANDIDATE,
            source_sha256="f" * 64,
        )
        second_run = "00000000-0000-4000-8000-000000000098"
        second_event = copy.deepcopy(first_event)
        second_event["run_id"] = second_run
        second = summarize_events(
            [second_event],
            run_id=second_run,
            candidate=CANDIDATE,
            source_sha256="f" * 64,
        )
        self.assertNotEqual(
            first["flow_record_set_sha256"], second["flow_record_set_sha256"]
        )

    def test_unknown_fields_and_sensitive_text_are_rejected(self) -> None:
        extra = event("C0_BASELINE", "NETWORK", "1.1.1.1", 443)
        extra["raw_payload"] = "not allowed"
        with self.assertRaises(LabValidationError):
            summarize_events([extra], run_id=RUN_ID, candidate=None, source_sha256="f" * 64)

        sensitive = event("C0_BASELINE", "NETWORK", "1.1.1.1", 443)
        sensitive["status"] = "Password=not-allowed"
        with self.assertRaises(LabValidationError):
            summarize_events([sensitive], run_id=RUN_ID, candidate=None, source_sha256="f" * 64)

    def test_run_binding_is_strict(self) -> None:
        mismatched = event("C0_BASELINE", "NETWORK", "1.1.1.1", 443)
        mismatched["run_id"] = "00000000-0000-4000-8000-000000000098"
        with self.assertRaises(LabValidationError):
            summarize_events([mismatched], run_id=RUN_ID, candidate=None, source_sha256="f" * 64)

        old_contract = event("C0_BASELINE", "NETWORK", "1.1.1.1", 443)
        old_contract["schema_version"] = 1
        with self.assertRaisesRegex(
            LabValidationError, "schema/run binding mismatch"
        ):
            summarize_events(
                [old_contract],
                run_id=RUN_ID,
                candidate=None,
                source_sha256="f" * 64,
            )

        fractional_contract = event(
            "C0_BASELINE", "NETWORK", "1.1.1.1", 443
        )
        fractional_contract["schema_version"] = 2.0
        with self.assertRaisesRegex(
            LabValidationError, "schema/run binding mismatch"
        ):
            summarize_events(
                [fractional_contract],
                run_id=RUN_ID,
                candidate=None,
                source_sha256="f" * 64,
            )


if __name__ == "__main__":
    unittest.main()
