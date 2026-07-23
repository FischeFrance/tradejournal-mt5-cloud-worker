"""Guardie statiche del probe MQL5 one-shot.

Questi test non compilano e non avviano MetaTrader. Rendono falsificabili le
proprieta' di sicurezza e il contratto wire richiesti dal laboratorio C0-C5.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path


MQL5_DIR = Path(__file__).resolve().parents[1]
SOURCE_PATH = MQL5_DIR / "TradeJournalIdentityProbe.mq5"
SCHEMA_PATH = MQL5_DIR / "identity-probe.schema.json"

ALLOWED_FIELDS = {
    "schema_version",
    "probe_version",
    "run_id",
    "generated_at_unix",
    "terminal_result",
    "expected_login_loaded",
    "terminal_connected",
    "account_match",
    "account_server",
    "account_company",
    "account_trade_mode",
    "account_trade_allowed",
    "account_trade_expert",
    "terminal_trade_allowed",
    "terminal_build",
    "terminal_path",
    "terminal_data_path",
}

TERMINAL_RESULTS = {
    "CONNECTED_IDENTITY_AVAILABLE",
    "IDENTITY_MISMATCH",
    "TIMEOUT",
    "NOT_CONNECTED",
    "INPUT_INVALID",
    "OUTPUT_FAILURE",
}

UNBOUND_RUN_ID = "00000000-0000-4000-8000-000000000000"


def _source() -> str:
    return SOURCE_PATH.read_text(encoding="utf-8")


def _compact_without_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return re.sub(r"//[^\r\n]*", "", text)


def _function_body(name: str) -> str:
    """Return a balanced MQL function body, ignoring braces in literals."""

    code = _compact_without_comments(_source())
    declaration = re.search(
        rf"\b(?:bool|int|string|void)\s+{re.escape(name)}\s*\([^)]*\)\s*\{{",
        code,
        re.DOTALL,
    )
    assert declaration, f"funzione MQL5 non trovata: {name}"

    opening = declaration.end() - 1
    depth = 0
    quote: str | None = None
    escaped = False
    for index in range(opening, len(code)):
        character = code[index]
        if quote is not None:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = None
            continue
        if character in {"'", '"'}:
            quote = character
        elif character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return code[opening + 1 : index]
    raise AssertionError(f"corpo non bilanciato: {name}")


def _schema() -> dict[str, object]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def test_probe_files_are_confined_to_lab_subtree() -> None:
    assert SOURCE_PATH.is_file()
    assert SCHEMA_PATH.is_file()
    assert "lab/mt5_direct_endpoint/mql5" in SOURCE_PATH.as_posix()


def test_probe_contains_no_trading_or_external_io_capability() -> None:
    code = _compact_without_comments(_source())
    forbidden_patterns = {
        "order send": r"\bOrderSend(?:Async)?\s*\(",
        "trade request": r"\bMqlTradeRequest\b",
        "standard trade library": r"#include\s*[<\"]Trade[\\/]Trade\.mqh[>\"]",
        "trade action": r"\bTRADE_ACTION_",
        "CTrade": r"\bCTrade\b",
        "position mutation": r"\bPosition(?:Open|Close|ClosePartial|Modify)\s*\(",
        "order mutation": r"\bOrder(?:Open|Close|Delete|Modify)\s*\(",
        "DLL/EX5 import": r"^\s*#import\s+[\"<].+\.(?:dll|ex5)[\">]",
        "web request": r"\bWebRequest\s*\(",
        "socket API": r"\bSocket(?:Create|Connect|Send|TlsHandshake)\s*\(",
        "shared file sandbox": r"\bFILE_COMMON\b",
    }
    offenders = [
        name
        for name, pattern in forbidden_patterns.items()
        if re.search(pattern, code, re.IGNORECASE | re.MULTILINE)
    ]
    assert not offenders, f"capacita' vietate trovate nel probe: {offenders}"


def test_probe_does_not_read_private_trading_or_customer_data() -> None:
    code = _compact_without_comments(_source())
    forbidden_tokens = (
        "ACCOUNT_NAME",
        "ACCOUNT_BALANCE",
        "ACCOUNT_EQUITY",
        "ACCOUNT_PROFIT",
        "ACCOUNT_CREDIT",
        "ACCOUNT_MARGIN",
        "PositionsTotal",
        "PositionGet",
        "OrdersTotal",
        "OrderGet",
        "HistorySelect",
        "HistoryDeal",
        "HistoryOrder",
    )
    offenders = [token for token in forbidden_tokens if token in code]
    assert not offenders, f"letture dati non ammesse trovate: {offenders}"


def test_probe_reads_only_the_required_identity_and_terminal_properties() -> None:
    source = _source()
    required_tokens = (
        "TERMINAL_CONNECTED",
        "ACCOUNT_LOGIN",
        "ACCOUNT_SERVER",
        "ACCOUNT_COMPANY",
        "ACCOUNT_TRADE_MODE",
        "ACCOUNT_TRADE_ALLOWED",
        "ACCOUNT_TRADE_EXPERT",
        "TERMINAL_TRADE_ALLOWED",
        "TERMINAL_BUILD",
        "TERMINAL_PATH",
        "TERMINAL_DATA_PATH",
    )
    missing = [token for token in required_tokens if token not in source]
    assert not missing, f"proprieta' richieste mancanti: {missing}"


def test_expected_account_is_consumed_then_erased_and_only_compared_in_memory() -> None:
    code = _compact_without_comments(_source())
    consume = _function_body("ConsumeExpectedAccount")
    capture = _function_body("CaptureSnapshot")

    assert 'EXPECTED_ACCOUNT_FILE PROBE_DIRECTORY + "\\\\expected-account.txt"' in code
    assert "ReadSingleTextRecord(EXPECTED_ACCOUNT_FILE, value)" in consume
    assert "FileDelete(EXPECTED_ACCOUNT_FILE)" in consume
    assert consume.index("FileDelete(EXPECTED_ACCOUNT_FILE)") < consume.index(
        "IsUnsignedDecimal(value)"
    )
    assert consume.index("FileDelete(EXPECTED_ACCOUNT_FILE)") < consume.index(
        "g_expected_account = parsed"
    )
    assert "observed_account == g_expected_account" in capture
    assert "observed_account = 0" in capture
    assert "value = \"\"" in consume
    assert "IntegerToString(observed_account)" not in code
    assert "IntegerToString(g_expected_account)" not in code
    assert not re.search(
        r"Print\s*\([^;]*(?:observed_account|g_expected_account|parsed|value)", code
    )
    assert not re.search(
        r"\binput\s+[^;]*(?:login|account|password|secret|token)",
        code,
        re.IGNORECASE,
    )


def test_opaque_run_id_is_separate_consumed_and_canonical() -> None:
    code = _compact_without_comments(_source())
    consume = _function_body("ConsumeRunId")
    validate = _function_body("IsCanonicalRunId")

    assert 'RUN_ID_FILE PROBE_DIRECTORY + "\\\\run-id.txt"' in code
    assert "RUN_ID_FILE" != "EXPECTED_ACCOUNT_FILE"
    assert "ReadSingleTextRecord(RUN_ID_FILE, value)" in consume
    assert "FileDelete(RUN_ID_FILE)" in consume
    assert "IsCanonicalRunId(value)" in consume
    assert "StringLen(value) != 36" in validate
    assert "value == UNBOUND_RUN_ID" in validate
    assert "version < '1' || version > '5'" in validate
    assert "variant != '8'" in validate and "variant != 'b'" in validate
    assert f'#define UNBOUND_RUN_ID "{UNBOUND_RUN_ID}"' in code


def test_terminal_state_set_is_closed_and_exact() -> None:
    code = _compact_without_comments(_source())
    definitions = dict(
        re.findall(r'^#define\s+(RESULT_[A-Z_]+)\s+"([A-Z_]+)"\s*$', code, re.MULTILINE)
    )
    assert set(definitions.values()) == TERMINAL_RESULTS
    assert len(definitions) == len(TERMINAL_RESULTS)


def test_serialized_contract_is_exact_and_contains_no_login_or_credentials() -> None:
    source = _source()
    build = _function_body("BuildTerminalEvidence")
    emitted_keys = set(re.findall(r'json \+= "\\"([a-z_]+)\\":', source))
    assert emitted_keys == ALLOWED_FIELDS

    forbidden_keys = {
        "account",
        "account_login",
        "login",
        "password",
        "name",
        "balance",
        "equity",
        "orders",
        "positions",
        "deals",
        "history",
    }
    assert emitted_keys.isdisjoint(forbidden_keys)
    assert "ACCOUNT_LOGIN" not in build
    assert "g_expected_account" not in build
    assert "observed_account" not in build
    assert "PROBE_VERSION" in build
    assert "g_run_id" in build
    assert "terminal_result" in build


def test_schema_v3_is_closed_and_matches_exact_wire_contract() -> None:
    schema = _schema()
    properties = schema["properties"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == ALLOWED_FIELDS
    assert set(properties) == ALLOWED_FIELDS
    assert properties["schema_version"] == {"const": 3}
    assert properties["probe_version"] == {"const": "3.0.0"}
    assert set(properties["terminal_result"]["enum"]) == TERMINAL_RESULTS
    assert properties["account_match"] == {"type": "boolean"}
    assert properties["run_id"]["pattern"].startswith("^")
    assert properties["run_id"]["pattern"].endswith("$")
    for forbidden in ("account", "account_login", "login", "password", "balance", "equity"):
        assert forbidden not in properties


def test_probe_v3_keeps_raw_paths_only_in_the_local_artifact_contract() -> None:
    source = _source()
    schema = _schema()
    properties = schema["properties"]
    emitted_keys = set(re.findall(r'json \+= "\\"([a-z_]+)\\":', source))

    assert '#define PROBE_SCHEMA_VERSION 3' in source
    assert '#define PROBE_VERSION "3.0.0"' in source
    assert "terminal_build" in emitted_keys
    assert "terminal_path" in emitted_keys
    assert "terminal_data_path" in emitted_keys
    # A file cannot contain a non-circular digest of its own final bytes. The
    # trusted consumer derives these three digests after atomic publication.
    assert "identity_probe_output_sha256" not in emitted_keys
    assert "terminal_path_sha256" not in emitted_keys
    assert "terminal_data_path_sha256" not in emitted_keys
    assert "local probe artifact" in schema["$comment"]
    assert "exports only those digests" in schema["$comment"]


def test_probe_v3_normal_results_require_build_and_both_paths() -> None:
    schema = _schema()
    normal_rule = schema["allOf"][0]["then"]["properties"]
    assert normal_rule["terminal_build"] == {"minimum": 1}
    assert normal_rule["terminal_path"] == {"minLength": 1}
    assert normal_rule["terminal_data_path"] == {"minLength": 1}
    assert normal_rule["run_id"]["not"] == {"const": UNBOUND_RUN_ID}


def test_schema_encodes_connected_mismatch_and_timeout_semantics() -> None:
    schema = _schema()
    state_rules: dict[str, dict[str, object]] = {}
    for rule in schema["allOf"]:
        selector = rule["if"]["properties"]["terminal_result"]
        if "const" in selector:
            state_rules[selector["const"]] = rule["then"]["properties"]

    connected = state_rules["CONNECTED_IDENTITY_AVAILABLE"]
    mismatch = state_rules["IDENTITY_MISMATCH"]
    timeout = state_rules["TIMEOUT"]
    not_connected = state_rules["NOT_CONNECTED"]
    assert connected["terminal_connected"] == {"const": True}
    assert connected["account_match"] == {"const": True}
    assert mismatch["terminal_connected"] == {"const": True}
    assert mismatch["account_match"] == {"const": False}
    assert timeout["terminal_connected"] == {"const": True}
    assert timeout["account_match"] == {"const": False}
    assert not_connected["terminal_connected"] == {"const": False}
    assert not_connected["account_match"] == {"const": False}


def test_timeout_is_configurable_bounded_and_uses_monotonic_time() -> None:
    code = _compact_without_comments(_source())
    on_init = _function_body("OnInit")
    on_timer = _function_body("OnTimer")
    assert "input uint InpTimeoutSeconds = 120" in code
    assert "#define MIN_TIMEOUT_SECONDS 1" in code
    assert "#define MAX_TIMEOUT_SECONDS 3600" in code
    assert "InpTimeoutSeconds >= MIN_TIMEOUT_SECONDS" in on_init
    assert "InpTimeoutSeconds <= MAX_TIMEOUT_SECONDS" in on_init
    assert "GetTickCount64()" in on_init
    assert "GetTickCount64() - g_started_monotonic_ms" in on_timer
    assert "elapsed_ms < g_timeout_ms" in on_timer
    assert "Sleep(" not in code


def test_timeout_and_not_connected_are_unambiguous_at_final_sample() -> None:
    on_timer = _function_body("OnTimer")
    assert "if(elapsed_ms < g_timeout_ms)" in on_timer
    deadline_logic = on_timer[on_timer.index("if(elapsed_ms < g_timeout_ms)") :]
    assert "snapshot.terminal_connected" in deadline_logic
    assert "RESULT_TIMEOUT : RESULT_NOT_CONNECTED" in deadline_logic
    assert "CompleteProbeWithSnapshot(timeout_result, snapshot)" in deadline_logic


def test_one_shot_has_no_intermediate_rewrite_and_one_visible_atomic_result() -> None:
    code = _compact_without_comments(_source())
    on_timer = _function_body("OnTimer")
    publish = _function_body("PublishAtomically")
    complete = _function_body("CompleteProbeWithSnapshot")

    assert "PublishAtomically" not in on_timer
    assert "BuildTerminalEvidence" not in on_timer
    assert code.count("FileWriteString(handle, json)") == 1
    assert code.count("FileMove(OUTPUT_TEMP_FILE, 0, OUTPUT_FINAL_FILE, 0)") == 1
    assert "FileIsExist(OUTPUT_FINAL_FILE)" in publish
    assert publish.index("FileIsExist(OUTPUT_FINAL_FILE)") < publish.index("FileMove(")
    assert "FileFlush(handle)" in publish
    assert publish.index("FileFlush(handle)") < publish.index("FileClose(handle)")
    assert publish.index("FileClose(handle)") < publish.index("FileMove(")
    assert "FILE_REWRITE" not in code
    assert "OUTPUT_TEMP_FILE" in publish and "OUTPUT_FINAL_FILE" in publish
    assert "g_terminal_decided = true" in complete
    assert "if(!published && terminal_result != RESULT_OUTPUT_FAILURE)" in complete


def test_terminal_completion_stops_timer_clears_state_and_removes_expert() -> None:
    code = _compact_without_comments(_source())
    complete = _function_body("CompleteProbeWithSnapshot")
    cleanup = _function_body("CleanupSensitiveState")
    deinit = _function_body("OnDeinit")

    assert "EventKillTimer()" in complete
    assert "CleanupSensitiveState()" in complete
    assert complete.index("CleanupSensitiveState()") < complete.index("ExpertRemove()")
    assert "ExpertRemove()" in complete
    assert "EventKillTimer()" in cleanup
    assert "g_expected_account = 0" in cleanup
    assert "g_expected_login_loaded = false" in cleanup
    assert 'g_run_id = ""' in cleanup
    assert "g_started_monotonic_ms = 0" in cleanup
    assert "g_timeout_ms = 0" in cleanup
    assert "FileDelete(EXPECTED_ACCOUNT_FILE)" in cleanup
    assert "FileDelete(RUN_ID_FILE)" in cleanup
    assert "CleanupSensitiveState()" in deinit
    assert code.count("EventSetTimer(") == 1


def test_input_output_failures_and_stale_evidence_are_fail_closed() -> None:
    on_init = _function_body("OnInit")
    complete = _function_body("CompleteProbeWithSnapshot")
    assert "DeleteIfPresent(OUTPUT_TEMP_FILE)" in on_init
    assert "DeleteIfPresent(OUTPUT_FINAL_FILE)" not in on_init
    assert "!FileIsExist(OUTPUT_FINAL_FILE)" in on_init
    assert on_init.index("!FileIsExist(OUTPUT_FINAL_FILE)") < on_init.index("ConsumeRunId()")
    assert on_init.index("!FileIsExist(OUTPUT_FINAL_FILE)") < on_init.index(
        "ConsumeExpectedAccount()"
    )
    assert "CompleteProbe(RESULT_INPUT_INVALID)" in on_init
    assert "CompleteProbe(RESULT_OUTPUT_FAILURE)" in on_init
    assert "BuildTerminalEvidence(RESULT_OUTPUT_FAILURE, snapshot)" in complete
    assert "return INIT_SUCCEEDED" in on_init


def test_expected_account_file_is_not_share_readable() -> None:
    code = _compact_without_comments(_source())
    assert "FILE_SHARE_READ" not in code


class IdentityProbeStaticTests(unittest.TestCase):
    """Standard-library adapter; le stesse verifiche restano usabili con pytest."""

    def test_files_are_confined(self) -> None:
        test_probe_files_are_confined_to_lab_subtree()

    def test_no_trading_or_external_io(self) -> None:
        test_probe_contains_no_trading_or_external_io_capability()

    def test_no_private_trading_data(self) -> None:
        test_probe_does_not_read_private_trading_or_customer_data()

    def test_required_properties(self) -> None:
        test_probe_reads_only_the_required_identity_and_terminal_properties()

    def test_expected_account_consumption(self) -> None:
        test_expected_account_is_consumed_then_erased_and_only_compared_in_memory()

    def test_run_id_binding_input(self) -> None:
        test_opaque_run_id_is_separate_consumed_and_canonical()

    def test_terminal_states(self) -> None:
        test_terminal_state_set_is_closed_and_exact()

    def test_sanitized_contract(self) -> None:
        test_serialized_contract_is_exact_and_contains_no_login_or_credentials()

    def test_closed_schema_v3(self) -> None:
        test_schema_v3_is_closed_and_matches_exact_wire_contract()

    def test_local_path_artifact_contract(self) -> None:
        test_probe_v3_keeps_raw_paths_only_in_the_local_artifact_contract()

    def test_v3_build_and_path_requirements(self) -> None:
        test_probe_v3_normal_results_require_build_and_both_paths()

    def test_schema_state_semantics(self) -> None:
        test_schema_encodes_connected_mismatch_and_timeout_semantics()

    def test_timeout_configuration(self) -> None:
        test_timeout_is_configurable_bounded_and_uses_monotonic_time()

    def test_timeout_distinction(self) -> None:
        test_timeout_and_not_connected_are_unambiguous_at_final_sample()

    def test_single_atomic_result(self) -> None:
        test_one_shot_has_no_intermediate_rewrite_and_one_visible_atomic_result()

    def test_deactivation_and_cleanup(self) -> None:
        test_terminal_completion_stops_timer_clears_state_and_removes_expert()

    def test_failure_paths(self) -> None:
        test_input_output_failures_and_stale_evidence_are_fail_closed()

    def test_expected_account_not_shared(self) -> None:
        test_expected_account_file_is_not_share_readable()


if __name__ == "__main__":
    unittest.main()
