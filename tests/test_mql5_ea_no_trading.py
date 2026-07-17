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
