from __future__ import annotations

import copy
import json
import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

try:
    import jsonschema
except ImportError:  # pragma: no cover - dependency-poor hosts
    jsonschema = None


LAB_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_ROOT = LAB_ROOT / "schemas"
EXAMPLE_ROOT = LAB_ROOT / "examples"
PROBE_SCHEMA_PATH = LAB_ROOT / "mql5" / "identity-probe.schema.json"
sys.path.insert(0, str(LAB_ROOT / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_lab_model import build_artifacts  # noqa: E402

PATCH7_SCHEMA_FILES = (
    "experiment-config.schema.json",
    "experiment-manifest.schema.json",
    "control-plan.schema.json",
    "candidate-handoff.schema.json",
    "direct-campaign-manifest.schema.json",
    "evidence.schema.json",
)

ARTIFACT_SCHEMAS = {
    "EXPERIMENT_MANIFEST": "experiment-manifest.schema.json",
    "CONTROL_PLAN": "control-plan.schema.json",
    "CANDIDATE_HANDOFF": "candidate-handoff.schema.json",
    "DIRECT_CAMPAIGN_MANIFEST": "direct-campaign-manifest.schema.json",
}


def _load_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _schema_for_example(example: dict[str, object]) -> str:
    artifact_type = example.get("artifact_type")
    if isinstance(artifact_type, str):
        return ARTIFACT_SCHEMAS[artifact_type]
    if "run_context" in example and "proof_binding" in example:
        return "evidence.schema.json"
    return "experiment-config.schema.json"


@unittest.skipIf(jsonschema is None, "jsonschema dependency unavailable")
class ContractFileTests(unittest.TestCase):
    def _validator(
        self, schema_name: str
    ) -> "jsonschema.Draft202012Validator":
        schema = _load_json(SCHEMA_ROOT / schema_name)
        return jsonschema.Draft202012Validator(
            schema,
            format_checker=jsonschema.FormatChecker(),
        )

    def test_patch7_json_contracts_parse_are_valid_and_closed(self) -> None:
        paths = [SCHEMA_ROOT / name for name in PATCH7_SCHEMA_FILES]
        paths.append(PROBE_SCHEMA_PATH)
        self.assertEqual(len(PATCH7_SCHEMA_FILES), 6)

        for path in paths:
            with self.subTest(path=path):
                schema = _load_json(path)
                self.assertEqual(
                    schema["$schema"],
                    "https://json-schema.org/draft/2020-12/schema",
                )
                self.assertIs(schema["additionalProperties"], False)
                jsonschema.Draft202012Validator.check_schema(schema)

    def test_patch7_contract_versions_and_precandidate_config(self) -> None:
        config = _load_json(SCHEMA_ROOT / "experiment-config.schema.json")
        evidence = _load_json(SCHEMA_ROOT / "evidence.schema.json")
        probe = _load_json(PROBE_SCHEMA_PATH)

        self.assertEqual(
            config["properties"]["schema_version"],
            {"type": "integer", "const": 4},
        )
        self.assertEqual(
            evidence["properties"]["schema_version"],
            {"type": "integer", "const": 6},
        )
        self.assertEqual(
            evidence["$defs"]["proof_binding"]["properties"]["schema_version"],
            {"type": "integer", "const": 5},
        )
        self.assertEqual(probe["properties"]["schema_version"]["const"], 3)
        self.assertEqual(
            probe["properties"]["probe_version"]["const"],
            "3.0.0",
        )

        config_properties = config["properties"]
        self.assertNotIn("candidate_endpoint", config_properties)
        self.assertNotIn("probe_symbol", config_properties)
        self.assertIn("policy_version", config["required"])
        self.assertIn("probe", config["required"])
        self.assertIn("network_policy", config["required"])
        self.assertIn("lifecycle", config["required"])
        self.assertIn("signer_policy_sha256", config["$defs"]["configured_terminal"]["required"])
        self.assertEqual(
            config["$defs"]["probe_policy"]["properties"]["schema_version"]["const"],
            3,
        )
        self.assertEqual(
            config["$defs"]["probe_policy"]["properties"]["probe_version"]["const"],
            "3.0.0",
        )

    def test_evidence_v6_has_lifecycle_timeline_and_path_bindings(self) -> None:
        schema = _load_json(SCHEMA_ROOT / "evidence.schema.json")
        network = schema["$defs"]["network"]
        discovery = schema["$defs"]["discovery"]
        identity = schema["$defs"]["identity"]
        proof = schema["$defs"]["proof_binding"]
        timeline = schema["$defs"]["timeline"]
        timeline_event = schema["$defs"]["timeline_event"]

        self.assertNotIn("notes", schema["properties"])
        self.assertIn("credential_bundle_investor_confirmed", schema["required"])

        network_fields = {
            "process_scoped_tcp_flows",
            "candidate_tcp_flows",
            "other_tcp_flows",
            "dns_events",
            "non_tcp_network_events",
            "flow_record_set_sha256",
            "flow_record_set_verified",
        }
        self.assertTrue(network_fields.issubset(network["required"]))
        self.assertTrue(
            {
                "process_scoped_flows",
                "candidate_process_scoped_flows",
                "dns_flows",
                "unexpected_allowed_flows",
                "unexpected_blocked_flows",
                "fallback_flows",
            }.isdisjoint(network["properties"])
        )
        self.assertIn(
            "process_scoped_tcp_flows must equal candidate_tcp_flows plus other_tcp_flows",
            network["$comment"],
        )

        delta_fields = {
            "endpoint_delta_source",
            "endpoint_delta_source_sha256",
            "endpoint_delta_source_verified",
        }
        self.assertTrue(delta_fields.issubset(discovery["required"]))
        self.assertEqual(
            set(discovery["properties"]["endpoint_delta_source"]["enum"]),
            {
                "NONE",
                "PROCESS_SCOPED_TCP_FLOW_SET",
            },
        )

        identity_fields = {
            "probe_generated_at_unix",
            "terminal_build",
            "terminal_path_sha256",
            "terminal_data_path_sha256",
            "identity_probe_output_sha256",
            "probe_path_binding_verified",
        }
        self.assertTrue(identity_fields.issubset(identity["required"]))
        self.assertEqual(identity["properties"]["terminal_build"]["minimum"], 1)

        proof_fields = {
            "experiment_manifest_sha256",
            "control_plan_sha256",
            "candidate_handoff_manifest_sha256",
            "probe_path_binding_sha256",
            "lifecycle_binding_sha256",
            "job_portable_root_binding_sha256",
            "pre_state_binding_sha256",
            "state_transition_sha256",
            "firewall_portable_root_binding_sha256",
            "negative_query_binding_sha256",
        }
        self.assertTrue(proof_fields.issubset(proof["required"]))
        self.assertIn("lifecycle_binding", schema["required"])
        self.assertIn("initial_pre_state_binding", schema["required"])
        self.assertIn("state_transition", schema["required"])

        self.assertEqual(
            set(timeline["required"]),
            {"schema_version", "qpc_frequency_hz", "events"},
        )
        self.assertEqual(timeline["properties"]["schema_version"]["const"], 1)
        self.assertEqual(timeline["properties"]["qpc_frequency_hz"]["minimum"], 1)
        self.assertEqual(
            timeline["properties"]["qpc_frequency_hz"]["maximum"],
            10_000_000_000,
        )
        self.assertEqual(
            set(timeline_event["required"]),
            {"code", "sequence", "timestamp_unix_ms", "qpc"},
        )
        self.assertEqual(
            schema["properties"]["phase_markers"]["items"],
            {"$ref": "#/$defs/phase_code"},
        )

        c2_codes = [
            item["const"]
            for item in schema["$defs"]["c2_phase_codes"]["prefixItems"]
        ]
        self.assertEqual(
            c2_codes,
            [
                "C2_LOGIN_START",
                "C2_LOGIN_END",
                "C2_CONNECTED_START",
                "C2_CONNECTED_END",
                "C2_NETWORK_INTERRUPTION_START",
                "C2_NETWORK_INTERRUPTION_END",
                "C2_RECONNECT_START",
                "C2_RECONNECT_END",
            ],
        )

    def test_manifest_plan_handoff_and_campaign_bindings_are_explicit(self) -> None:
        manifest = _load_json(SCHEMA_ROOT / "experiment-manifest.schema.json")
        plan = _load_json(SCHEMA_ROOT / "control-plan.schema.json")
        handoff = _load_json(SCHEMA_ROOT / "candidate-handoff.schema.json")
        campaign = _load_json(
            SCHEMA_ROOT / "direct-campaign-manifest.schema.json"
        )

        for schema, artifact_type in (
            (manifest, "EXPERIMENT_MANIFEST"),
            (plan, "CONTROL_PLAN"),
            (handoff, "CANDIDATE_HANDOFF"),
            (campaign, "DIRECT_CAMPAIGN_MANIFEST"),
        ):
            with self.subTest(artifact_type=artifact_type):
                self.assertEqual(
                    schema["properties"]["artifact_type"]["const"],
                    artifact_type,
                )
                self.assertEqual(
                    schema["properties"]["schema_version"]["const"],
                    2,
                )

        manifest_terminal = manifest["$defs"]["manifest_terminal"]
        self.assertTrue(
            {
                "source_canonical_path",
                "source_path_sha256",
                "sha256",
                "publisher",
                "publisher_policy",
                "signer_policy_sha256",
            }.issubset(manifest_terminal["required"])
        )
        self.assertIn("experiment_manifest_sha256", manifest["required"])

        self.assertTrue(
            {
                "experiment_manifest_sha256",
                "candidate_handoff_manifest_sha256",
                "direct_campaign_manifest_sha256",
                "path_bindings",
                "lifecycle_control",
                "initial_pre_state_binding",
                "negative_query",
                "control_plan_sha256",
            }.issubset(plan["required"])
        )
        safety = plan["properties"]["safety"]["properties"]
        self.assertEqual(safety["plan_only"]["const"], True)
        for capability in (
            "mt5_start_enabled",
            "firewall_apply_enabled",
            "credential_access_enabled",
            "registry_promotion_enabled",
        ):
            self.assertEqual(safety[capability]["const"], False)
        self.assertEqual(
            set(plan["properties"]["path_bindings"]["required"]),
            {
                "source_terminal_path_sha256",
                "terminal_path_sha256",
                "terminal_data_path_sha256",
            },
        )

        c2_links = {
            "experiment_manifest_sha256",
            "c2_run_id",
            "c2_control_plan_sha256",
            "c2_evidence_sha256",
        }
        self.assertTrue(c2_links.issubset(handoff["required"]))
        self.assertIn("direct_campaign_manifest_sha256", handoff["required"])
        self.assertIn("c012_session_id", handoff["required"])
        self.assertIn("initial_c012_pre_state_sha256", handoff["required"])
        self.assertIn("c2_lifecycle_binding_sha256", handoff["required"])
        self.assertIn("candidate_handoff_manifest_sha256", handoff["required"])

        self.assertTrue(c2_links.issubset(campaign["required"]))
        self.assertIn("direct_campaign_manifest_sha256", campaign["required"])
        self.assertIn("canonical_order", campaign["required"])
        self.assertIn("temporal_policy", campaign["required"])
        self.assertEqual(
            set(campaign["properties"]["controls"]["required"]),
            {"C3", "C4", "C5"},
        )
        direct_network = campaign["$defs"]["network_contract"]
        self.assertIn(
            "non_tcp_network_events_max",
            direct_network["required"],
        )
        self.assertEqual(
            direct_network["properties"]["non_tcp_network_events_max"]["const"],
            0,
        )

    def test_candidate_contracts_require_literal_ip_and_c2_provenance(self) -> None:
        for schema_name in (
            "evidence.schema.json",
            "control-plan.schema.json",
            "candidate-handoff.schema.json",
            "direct-campaign-manifest.schema.json",
        ):
            with self.subTest(schema=schema_name):
                schema = _load_json(SCHEMA_ROOT / schema_name)
                candidate = schema["$defs"]["candidate"]
                ip_contract = candidate["properties"]["ip"]
                formats = {branch.get("format") for branch in ip_contract["oneOf"]}
                self.assertEqual(formats, {"ipv4", "ipv6"})
                self.assertEqual(
                    candidate["properties"]["source_control"]["const"],
                    "C2",
                )
                self.assertEqual(
                    candidate["properties"]["observed_phase"]["const"],
                    "LOGIN",
                )
                self.assertIs(
                    candidate["properties"]["process_scoped"]["const"],
                    True,
                )

    def test_human_fields_share_the_strict_patch7_policy(self) -> None:
        expected_human_ref = {"$ref": "#/$defs/human_text_128"}
        for schema_name in PATCH7_SCHEMA_FILES:
            schema = _load_json(SCHEMA_ROOT / schema_name)
            if "human_text_128" not in schema.get("$defs", {}):
                continue
            wrapper = {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "$defs": schema["$defs"],
                "$ref": "#/$defs/human_text_128",
            }
            validator = jsonschema.Draft202012Validator(wrapper)
            with self.subTest(schema=schema_name, value="Société Générale"):
                validator.validate("Société Générale")
            for unsafe in (
                "account 12345678",
                "password redacted",
                "account 1234\u200b5678",
                "account ¹²³⁴⁵⁶⁷⁸",
                "ｐａｓｓｗｏｒｄ redacted",
                "Broker\u2060Demo",
            ):
                with self.subTest(schema=schema_name, value=unsafe):
                    self.assertFalse(validator.is_valid(unsafe))

        evidence = _load_json(SCHEMA_ROOT / "evidence.schema.json")
        context = evidence["$defs"]["run_context"]["properties"]
        identity = evidence["$defs"]["identity"]["properties"]
        for field in ("expected_server", "expected_company"):
            self.assertEqual(
                context[field],
                {"$ref": "#/$defs/nullable_human_text_128"},
            )
        for field in ("server", "company"):
            self.assertEqual(identity[field], expected_human_ref)

        for schema_name in (
            "experiment-config.schema.json",
            "experiment-manifest.schema.json",
            "control-plan.schema.json",
            "candidate-handoff.schema.json",
            "direct-campaign-manifest.schema.json",
        ):
            schema = _load_json(SCHEMA_ROOT / schema_name)
            identity_contract = schema["$defs"]["demo_identity"]["properties"]
            for field in ("server", "company"):
                self.assertEqual(
                    identity_contract[field],
                    expected_human_ref,
                )

    def test_all_json_examples_validate_against_their_patch7_contract(self) -> None:
        example_paths = sorted(EXAMPLE_ROOT.glob("*.json"))
        self.assertTrue(example_paths)
        for path in example_paths:
            with self.subTest(example=path.name):
                example = _load_json(path)
                schema_name = _schema_for_example(example)
                self._validator(schema_name).validate(example)

    def test_control_plan_schema_binds_command_to_lifecycle_control(self) -> None:
        artifacts = build_artifacts()
        validator = self._validator("control-plan.schema.json")
        for control in ("C0", "C1", "C2", "C3", "C4", "C5"):
            with self.subTest(valid_control=control):
                validator.validate(artifacts.plans[control])

        invalid_commands = {
            "C0": None,
            "C1": copy.deepcopy(
                artifacts.plans["C2"]["terminal_command"]
            ),
            "C2": None,
            "C3": None,
            "C4": None,
            "C5": None,
        }
        for control, terminal_command in invalid_commands.items():
            with self.subTest(invalid_control=control):
                mutated = copy.deepcopy(artifacts.plans[control])
                mutated["terminal_command"] = terminal_command
                self.assertFalse(validator.is_valid(mutated))

    def test_evidence_schema_represents_unverified_transition(self) -> None:
        artifacts = build_artifacts()
        validator = self._validator("evidence.schema.json")
        c1 = copy.deepcopy(artifacts.evidence["C1"])
        c1["state_transition"]["transition_verified"] = False
        validator.validate(c1)

        missing_artifact = copy.deepcopy(c1)
        missing_artifact["state_transition"][
            "transition_evidence_sha256"
        ] = None
        self.assertFalse(validator.is_valid(missing_artifact))

    def test_legacy_config_evidence_and_probe_shapes_are_rejected(self) -> None:
        config = _load_json(EXAMPLE_ROOT / "experiment.c0-c2.example.json")
        config_validator = self._validator("experiment-config.schema.json")

        legacy_config = copy.deepcopy(config)
        legacy_config["schema_version"] = 2
        self.assertFalse(config_validator.is_valid(legacy_config))

        candidate_in_precandidate_config = copy.deepcopy(config)
        candidate_in_precandidate_config["candidate_endpoint"] = None
        self.assertFalse(
            config_validator.is_valid(candidate_in_precandidate_config)
        )

        legacy_probe_symbol = copy.deepcopy(config)
        legacy_probe_symbol["probe_symbol"] = "EURUSD"
        self.assertFalse(config_validator.is_valid(legacy_probe_symbol))

        evidence = _load_json(EXAMPLE_ROOT / "evidence.c0.synthetic-pass.json")
        evidence_validator = self._validator("evidence.schema.json")

        legacy_evidence = copy.deepcopy(evidence)
        legacy_evidence["schema_version"] = 4
        self.assertFalse(evidence_validator.is_valid(legacy_evidence))

        legacy_proof = copy.deepcopy(evidence)
        legacy_proof["proof_binding"]["schema_version"] = 3
        self.assertFalse(evidence_validator.is_valid(legacy_proof))

        legacy_network = copy.deepcopy(evidence)
        legacy_network["network"]["process_scoped_flows"] = 0
        legacy_network["network"]["candidate_process_scoped_flows"] = 0
        legacy_network["network"]["dns_flows"] = 0
        self.assertFalse(evidence_validator.is_valid(legacy_network))

        legacy_timeline = copy.deepcopy(evidence)
        legacy_timeline["timeline"]["schema_version"] = 0
        self.assertFalse(evidence_validator.is_valid(legacy_timeline))

        probe_schema = _load_json(PROBE_SCHEMA_PATH)
        probe_validator = jsonschema.Draft202012Validator(probe_schema)
        valid_probe = {
            "schema_version": 3,
            "probe_version": "3.0.0",
            "run_id": "00000000-0000-4000-8000-000000000001",
            "generated_at_unix": 1,
            "terminal_result": "NOT_CONNECTED",
            "expected_login_loaded": True,
            "terminal_connected": False,
            "account_match": False,
            "account_server": "",
            "account_company": "",
            "account_trade_mode": "UNKNOWN",
            "account_trade_allowed": False,
            "account_trade_expert": False,
            "terminal_trade_allowed": False,
            "terminal_build": 1,
            "terminal_path": "C:\\TJLab\\terminal64.exe",
            "terminal_data_path": "C:\\TJLab",
        }
        probe_validator.validate(valid_probe)

        legacy_probe_schema = copy.deepcopy(valid_probe)
        legacy_probe_schema["schema_version"] = 2
        self.assertFalse(probe_validator.is_valid(legacy_probe_schema))

        legacy_probe_version = copy.deepcopy(valid_probe)
        legacy_probe_version["probe_version"] = "2.0.0"
        self.assertFalse(probe_validator.is_valid(legacy_probe_version))

        unbound_normal_probe = copy.deepcopy(valid_probe)
        unbound_normal_probe["run_id"] = "00000000-0000-4000-8000-000000000000"
        self.assertFalse(probe_validator.is_valid(unbound_normal_probe))

    def test_wpr_profile_is_well_formed_xml(self) -> None:
        root = ET.parse(LAB_ROOT / "profiles" / "mt5-network.wprp").getroot()
        self.assertEqual(root.tag, "WindowsPerformanceRecorder")

    def test_all_new_components_remain_under_lab_directory(self) -> None:
        expected = {
            "examples",
            "mql5",
            "profiles",
            "schemas",
            "src",
            "tests",
            "tools",
            "windows",
        }
        actual = {path.name for path in LAB_ROOT.iterdir() if path.is_dir()}
        self.assertTrue(expected.issubset(actual))


if __name__ == "__main__":
    unittest.main()
