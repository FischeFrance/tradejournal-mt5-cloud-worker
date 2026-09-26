"""Rete di sicurezza statica per l'Expert Advisor MQL5 (mt5/experts/TradeJournalBridge.mq5):
nessuna funzione di trading, nessun import di DLL esterne. Stesso principio di
tests/test_bridge_no_trading.py, applicato al sorgente MQL5 invece che al bridge Python: un EA
che scrive file JSON in sola lettura non deve mai poter inviare, modificare o chiudere un ordine.
"""

from __future__ import annotations

import re
from pathlib import Path

MT5_EXPERTS_DIR = Path(__file__).resolve().parent.parent / "mt5" / "experts"

# Pattern di chiamata (nome seguito da parentesi aperta), non semplice presenza della sottostringa:
# permette ai commenti che DOCUMENTANO l'esclusione di restare nel sorgente senza far fallire il
# test (stesso principio di tests/test_bridge_no_trading.py).
_TRADING_CALL_PATTERNS = [
    re.compile(r"\bOrderSend\s*\("),
    re.compile(r"\bOrderSendAsync\s*\("),
    re.compile(r"\bOrderModify\s*\("),
    re.compile(r"\bOrderClose\s*\("),
    re.compile(r"\bOrderDelete\s*\("),
    re.compile(r"\bPositionClose\s*\("),
    re.compile(r"\bPositionClosePartial\s*\("),
    re.compile(r"\bPositionOpen\s*\("),
    re.compile(r"\bPositionModify\s*\("),
    # L'inclusione della libreria standard di trading (necessaria per usare CTrade) e' il segnale
    # di uso reale: il solo nome "CTrade" puo' comparire in un commento che ne documenta
    # l'assenza (come in questo stesso file), quindi non e' usato come pattern a se stante.
    re.compile(r"#include\s*[<\"]Trade[\\/]Trade\.mqh[>\"]", re.IGNORECASE),
    re.compile(r"\bTRADE_ACTION_"),
]

# #import di una libreria esterna (DLL o EX5): un EA read-only non ne ha bisogno. Il pattern
# esclude i commenti che parlano di "#import" senza aprire davvero un blocco import (nessun
# blocco #import e' comunque presente in questo file).
_DLL_IMPORT_PATTERN = re.compile(r'^\s*#import\s+"[^"]+\.(dll|ex5)"', re.IGNORECASE | re.MULTILINE)

# Frase che documenta esplicitamente, in testa al file, l'assenza di funzioni di trading (vedi
# requisito "Aggiungi commenti espliciti che documentino perche' non sono presenti funzioni di
# trading"). Non e' un vincolo sulla formulazione esatta, solo sulla presenza del concetto.
_NO_TRADING_DOC_PATTERN = re.compile(r"non\s+chiama\s+MAI\s+OrderSend", re.IGNORECASE)


def _mq5_files() -> list[Path]:
    return sorted(MT5_EXPERTS_DIR.glob("*.mq5"))


def test_mql5_experts_directory_is_not_empty():
    assert _mq5_files(), f"atteso almeno un file .mq5 sotto {MT5_EXPERTS_DIR}"


def test_no_trading_calls_in_mql5_source():
    offenders = []
    for path in _mq5_files():
        text = path.read_text(encoding="utf-8")
        for pattern in _TRADING_CALL_PATTERNS:
            if pattern.search(text):
                offenders.append((path.name, pattern.pattern))
    assert not offenders, f"Chiamate di trading trovate nel sorgente MQL5: {offenders}"


def test_no_dll_or_ex5_imports_in_mql5_source():
    offenders = []
    for path in _mq5_files():
        text = path.read_text(encoding="utf-8")
        if _DLL_IMPORT_PATTERN.search(text):
            offenders.append(path.name)
    assert not offenders, f"Import di libreria esterna trovato nel sorgente MQL5: {offenders}"


def test_no_trading_rationale_is_documented():
    for path in _mq5_files():
        text = path.read_text(encoding="utf-8")
        assert _NO_TRADING_DOC_PATTERN.search(text), (
            f"{path.name} deve documentare esplicitamente perche' non chiama funzioni di trading"
        )


def test_expert_advisor_declares_required_handlers():
    text = (MT5_EXPERTS_DIR / "TradeJournalBridge.mq5").read_text(encoding="utf-8")
    for handler in ("OnInit", "OnDeinit", "OnTimer", "OnTradeTransaction"):
        assert re.search(rf"\b{handler}\s*\(", text), f"handler mancante: {handler}"


def test_expert_declares_versioned_file_bridge_contract():
    text = (MT5_EXPERTS_DIR / "TradeJournalBridge.mq5").read_text(encoding="utf-8")
    for required in (
        "schema_version",
        "generated_at",
        "sequence",
        "account_identity",
        "server_identity",
        "payload",
        "deals.json",
        "candles\\\\",
        "events\\\\",
    ):
        assert required in text, f"contratto file bridge mancante: {required}"


def test_new_only_defers_account_reads_until_after_on_init():
    text = (MT5_EXPERTS_DIR / "TradeJournalBridge.mq5").read_text(encoding="utf-8")
    assert "if(!g_new_only)\n      WriteAllSnapshots();" in text
    assert (
        "GetTickCount64() - g_new_only_started_ms < NEW_ONLY_STARTUP_GRACE_MS" in text
    )


def test_new_only_restart_uses_a_durable_watermark_and_never_slides_a_source_gap():
    text = (MT5_EXPERTS_DIR / "TradeJournalBridge.mq5").read_text(
        encoding="utf-8"
    )
    assert "NEW_ONLY_RECOVERY_MAX_SECONDS = 21600" in text
    assert "g_new_only_recovery_pending = g_new_only && g_history_from > 0" in text
    assert "(long)TimeGMT() - (long)g_history_from" in text
    assert "HistorySelect(from_time, to_time)" in text
    assert "ulong deal_tickets[]" in text
    assert "ulong order_tickets[]" in text
    assert "state == ORDER_STATE_CANCELED" in text
    assert "EmitDealAddEvent(ticket)" in text
    assert "if(!all_events_written)" in text
    assert "EstablishSourceContinuityFromHistory()" in text
    assert "source_watermark_unix" in text
    assert "EarliestRecoveryFrom" in text
    assert "g_history_from > 0" in text
    assert (
        'IntegerToString(timestamp_msc) + "|" + IntegerToString(g_event_seq)'
        not in text
    )
    assert 'FileDelete(BASE_DIR + "\\\\history_from_unix")' in text
    assert "g_new_only_recovery_pending && !RunNewOnlyRecovery()" in text


def test_live_opening_balance_is_not_fabricated_from_callback_time_account_balance():
    text = (MT5_EXPERTS_DIR / "TradeJournalBridge.mq5").read_text(encoding="utf-8")
    assert '"\\\"balance_before_open\\\":null,"' in text
    assert "frozen full historical ledger can certify the denominator" in text
    assert "account_balance - profit - effective_commission - swap" not in text


def test_cursor_seed_and_source_journal_are_fail_closed_before_a_healthy_heartbeat():
    text = (MT5_EXPERTS_DIR / "TradeJournalBridge.mq5").read_text(encoding="utf-8")
    assert "SeedEventSequenceFromExistingFiles();" in text
    assert "PrepareSourceContinuityAtBoot();" in text
    assert "g_pending_events_load_failed = true;" in text
    assert "g_source_marker_load_failed = true;" in text
    assert "if(g_new_only && g_source_recovery_required && !EnsureSourceRecoveryMarker())" in text
    assert "g_event_seq++;\n   if(!SaveCursorState())" in text
    heartbeat = text.split("string BuildHeartbeatJson", 1)[1].split(
        "string BuildAccountJson", 1
    )[0]
    assert "g_source_recovery_required ||" in heartbeat
    assert "ArraySize(g_pending_event_tickets) > 0" in heartbeat


def test_source_recovery_v2_separates_windows_request_from_ea_state_and_ack():
    text = (MT5_EXPERTS_DIR / "TradeJournalBridge.mq5").read_text(encoding="utf-8")

    # The persistent names are an ownership boundary, not merely cosmetic
    # aliases for a last-writer-wins marker.  A Windows request must have no
    # EA cleanup path, while the EA may safely replace/delete only STATE/ACK.
    for required in (
        'SOURCE_RECOVERY_STATE_V2 "source-recovery-state-v2.json"',
        'SOURCE_RECOVERY_REQUEST_V2 "source-recovery-request-v2.json"',
        'SOURCE_RECOVERY_ACK_V2 "source-recovery-ack-v2.json"',
        '\\"source_recovery_protocol_version\\"',
        '\\"source_recovery_request_generation\\"',
        '\\"source_recovery_ack_generation\\"',
    ):
        assert required in text

    # Control files are a small security/continuity protocol, not a permissive
    # substring parse.  In particular a decimal, duplicate key or trailing
    # fragment must not make a damaged REQUEST look acknowledged.
    strict_parser = text.split("bool TryExtractStrictJsonLong", 1)[1].split(
        "bool IsExactSourceRecoveryV2ControlJson", 1
    )[0]
    assert "IntegerToString(value) == token" in strict_parser
    control_shape = text.split("bool IsExactSourceRecoveryV2ControlJson", 1)[1].split(
        "bool IsExactSourceRecoveryStateV2Json", 1
    )[0]
    assert "windows_request" in control_shape
    assert "ea_ack" in control_shape
    v2_reader = text.split("bool ReadSourceRecoveryV2Control", 1)[1].split(
        "bool PersistSourceRecoveryAck", 1
    )[0]
    assert v2_reader.count("TryExtractStrictJsonLong") == 3
    assert "IsExactSourceRecoveryV2ControlJson(content, generation)" in v2_reader

    establish = text.split("bool EstablishSourceContinuityFromHistory()", 1)[1].split(
        "void PrepareSourceContinuityAtBoot()", 1
    )[0]
    assert 'SOURCE_RECOVERY_STATE_V2' in establish
    assert 'SOURCE_RECOVERY_REQUEST_V2' not in establish
    assert "AcknowledgeExternalSourceRecoveryAfterReplay()" in establish
    assert establish.index("SaveCursorState()") < establish.index(
        "AcknowledgeExternalSourceRecoveryAfterReplay()"
    )

    acknowledge = text.split("bool AcknowledgeExternalSourceRecoveryAfterReplay()", 1)[1].split(
        "void LoadSourceRecoveryRequired()", 1
    )[0]
    assert "PersistSourceRecoveryAck(g_source_recovery_request_generation)" in acknowledge
    assert "LoadExternalSourceRecoveryRequest();" in acknowledge
    assert acknowledge.index("PersistSourceRecoveryAck") < acknowledge.index(
        "LoadExternalSourceRecoveryRequest();"
    )

    migration = text.split("void MigrateLegacySourceRecoveryMarker()", 1)[1].split(
        "void LoadExternalSourceRecoveryRequest()", 1
    )[0]
    assert "SOURCE_RECOVERY_LEGACY_V1" in migration
    assert migration.index("PersistSourceRecoveryMarker()") < migration.index(
        "FileDelete(path)"
    )

    # The v1 pathname occurs only as an explicitly documented, persist-first
    # migration source.  In particular, v2 request cleanup is absent.
    assert "FileDelete(BASE_DIR + \"\\\\\" + SOURCE_RECOVERY_REQUEST_V2)" not in text


def test_source_continuity_handles_pristine_bootstrap_and_non_source_position_callbacks():
    text = (MT5_EXPERTS_DIR / "TradeJournalBridge.mq5").read_text(encoding="utf-8")
    # A valid-but-empty journal is previous durable state, never proof that a
    # cursor-less restart may invent a fresh watermark. A zero native ticket
    # also latches the same authoritative recovery path before returning.
    assert "g_pending_events_file_present = true;" in text
    assert "!g_pending_events_file_present &&" in text
    queue = text.split("bool QueuePendingEvent", 1)[1].split("int FindPendingEvent", 1)[0]
    assert "if(ticket == 0)" in queue
    assert "MarkSourceRecoveryRequired();" in queue
    retry = text.split("void RetryPendingEvents()", 1)[1].split("bool PublishNativeEvent", 1)[0]
    assert "while(ArraySize(g_pending_event_tickets) > 0)" in retry
    assert "int i = 0;" in retry
    assert "if(!RemovePendingEvent(i))\n            return;" in retry
    publish = text.split("bool PublishNativeEvent", 1)[1].split("bool EmitOrderEvent", 1)[0]
    assert "RetryPendingEvents();" in publish
    assert "return FindPendingEvent(kind, ticket) < 0;" in publish
    pending_loader = text.split("void LoadPendingEvents()", 1)[1].split(
        "bool AdvanceSourceWatermarkAfterLiveDrain", 1
    )[0]
    assert "RequireSourceRecovery(0, true);" in pending_loader
    assert "ArrayResize(g_pending_event_tickets, 0);" in pending_loader
    earliest = text.split("datetime EarliestRecoveryFrom", 1)[1].split(
        "bool PersistSourceRecoveryMarker", 1
    )[0]
    assert "if(earliest == 0)\n      return 0;" in earliest
    # Position notifications are snapshot hints. An absent position after its
    # DEAL_ADD close must drain legacy queue entries rather than poison source
    # recovery across a restart.
    position = text.split("bool EmitPositionEvent", 1)[1].split("bool EmitHistoryOrderEvent", 1)[0]
    assert "if(!PositionSelectByTicket(position_ticket))" in position
    assert "return true;" in position
    transaction = text.split("void OnTradeTransaction", 1)[1]
    assert "PublishBestEffortPositionSnapshot(trans.position);" in transaction
    assert "PublishNativeEvent(PENDING_POSITION, trans.position);" not in transaction
    # When both first cursor persistence and the recovery marker fail, retain
    # the in-memory fail-closed latch and the timer retry. A transient local
    # storage lock must not tear down MT5 as an initialization failure.
    on_init = text.split("int OnInit()", 1)[1].split("void OnDeinit", 1)[0]
    assert "continuity-storage-unavailable" in on_init
    assert "g_source_recovery_required && !g_source_recovery_marker_persisted" in on_init
    continuity_branch = on_init.split("if(continuity_storage_unavailable)", 1)[1].split(
        "else\n      WriteInitMarker(\"cursor-ready\");", 1
    )[0]
    assert "retry timer mantenuto" in continuity_branch
    assert "return(INIT_FAILED);" not in continuity_branch
    assert "timer-ready-source-recovery" in on_init
    on_timer = text.split("void OnTimer()", 1)[1].split("void OnTradeTransaction", 1)[0]
    assert "if(g_new_only)\n      LoadSourceRecoveryRequired();" in on_timer
    assert "if(g_new_only && !g_source_recovery_required)" not in on_timer


def test_expert_never_uses_chart_close_or_init_failed_for_window_cleanup():
    text = (MT5_EXPERTS_DIR / "TradeJournalBridge.mq5").read_text(encoding="utf-8")
    assert "CloseOtherCharts(" not in text
    assert "ChartClose(" not in text
    # INIT_FAILED is allowed only for timer creation; hiding/closing a chart
    # must never turn an otherwise connected terminal into mt5_initialize_failed.
    on_init = text.split("int OnInit()", 1)[1].split("void OnDeinit", 1)[0]
    assert "terminal_window" not in on_init
    assert on_init.count("return(INIT_FAILED);") == 1
    timer_failure = on_init.split("if(InpTimerSeconds <= 0 || !EventSetTimer", 1)[1]
    assert "return(INIT_FAILED);" in timer_failure


def test_pending_order_fill_has_a_distinct_terminal_source_event():
    text = (MT5_EXPERTS_DIR / "TradeJournalBridge.mq5").read_text(encoding="utf-8")
    for order_type in (
        "ORDER_TYPE_BUY_LIMIT",
        "ORDER_TYPE_SELL_LIMIT",
        "ORDER_TYPE_BUY_STOP",
        "ORDER_TYPE_SELL_STOP",
        "ORDER_TYPE_BUY_STOP_LIMIT",
        "ORDER_TYPE_SELL_STOP_LIMIT",
    ):
        assert order_type in text
    assert "ORDER_STATE_FILLED" in text
    assert 'event_kind = "HISTORY_FILLED"' in text


def test_loader_hands_off_to_bridge_template_after_connection_grace_period():
    text = (MT5_EXPERTS_DIR / "TradeJournalLoader.mq5").read_text(encoding="utf-8")
    assert "void OnStart()" in text
    assert "HistorySelect" not in text
    assert "TerminalInfoInteger(TERMINAL_CONNECTED)" in text
    assert "now_ms - connected_since_ms >= connection_grace_ms" in text
    assert 'ChartApplyTemplate(0, "\\\\Files\\\\TradeJournal\\\\TradeJournalBridge.tpl")' in text


def test_discovery_script_publishes_versioned_identity_bound_handoff():
    text = (MT5_EXPERTS_DIR / "TradeJournalDiscovery.mq5").read_text(encoding="utf-8")
    assert "void OnStart()" in text
    assert "WriteStarted()" in text
    assert "discovery-started.json" in text
    assert '\\"chart_symbol\\"' in text
    assert "TerminalInfoInteger(TERMINAL_CONNECTED)" in text
    assert "SymbolIsSynchronized" in text
    for field in (
        "schema_version",
        "connection_id",
        "login",
        "server",
        "requested_symbol",
        "resolution",
        "catalog_total",
        "synchronized",
        "terminal_connected",
        "account_trade_allowed",
        "terminal_build",
        "symbol",
    ):
        assert f'\\\"{field}\\\"' in text
    assert 'FileIsExist(BASE_DIR + "\\\\bridge-ready")' in text
    assert 'ChartApplyTemplate(0, "\\\\Files\\\\TradeJournal\\\\TradeJournalBridge.tpl")' in text
