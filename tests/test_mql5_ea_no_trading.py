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


def test_new_only_restart_recovers_only_a_bounded_downtime_gap():
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
    assert "!all_events_written || !SaveCursorState()" in text
    assert (
        'IntegerToString(timestamp_msc) + "|" + IntegerToString(g_event_seq)'
        not in text
    )
    assert 'FileDelete(BASE_DIR + "\\\\history_from_unix")' in text
    assert "g_new_only_recovery_pending && !RunNewOnlyRecovery()" in text


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
