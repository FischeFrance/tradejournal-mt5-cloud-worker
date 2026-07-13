"""Rete di sicurezza statica: nessuno dei moduli bridge deve mai chiamare una funzione di
trading del pacchetto MetaTrader5 (order_send, order_check, order_close, ecc.). Questo
complementa (non sostituisce) i test live in tests/test_fake_bridge.py che verificano l'assenza
di endpoint di trading via HTTP."""

from __future__ import annotations

import re
from pathlib import Path

BRIDGE_DIR = Path(__file__).resolve().parent.parent / "bridge"

# Pattern di chiamata (nome seguito da parentesi aperta), non semplice presenza della sottostringa:
# permette ai commenti che DOCUMENTANO l'esclusione (es. "nessuna chiamata a order_send") di
# restare nel codice senza far fallire il test.
_TRADING_CALL_PATTERNS = [
    re.compile(r"\border_send\s*\("),
    re.compile(r"\border_check\s*\("),
    re.compile(r"\border_close\s*\("),
    re.compile(r"\bposition_close\s*\("),
    re.compile(r"\bTRADE_ACTION_"),
]


def test_no_trading_calls_in_bridge_source():
    python_files = list(BRIDGE_DIR.rglob("*.py"))
    assert python_files, "atteso almeno un file Python sotto bridge/"

    offenders = []
    for path in python_files:
        text = path.read_text(encoding="utf-8")
        for pattern in _TRADING_CALL_PATTERNS:
            if pattern.search(text):
                offenders.append((str(path.relative_to(BRIDGE_DIR.parent)), pattern.pattern))

    assert not offenders, f"Chiamate di trading trovate nel codice del bridge: {offenders}"
