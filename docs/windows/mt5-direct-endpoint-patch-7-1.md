# MT5 Direct Endpoint Lab — Patch 7.1

Data: 23 luglio 2026

Branch di coordinamento: `assistant/mt5-patch-7-1-temporal-order`

## Stato

La correzione chirurgica 7.1 è stata applicata e verificata offline sulla copia autorevole del laboratorio ricevuta come ZIP.

```text
PATCH 7.1: READY FOR INDEPENDENT OFFLINE REVIEW
HARNESS: PARTIALLY_READY
C0-C5: NON ESEGUITI
WINDOWS RUNTIME: NO-GO
GO AL TEST REALE: NO-GO
```

Nessun processo MT5 o MetaEditor è stato avviato. Non sono state utilizzate credenziali, aperte connessioni broker, applicate regole firewall/WFP o eseguite operazioni sulla VPS.

## Difetto corretto

Patch 7 confrontava l'ordine C0→C1→C2 anche tramite timestamp arrotondati al secondo. Una sovrapposizione di 1 ms poteva quindi essere mascherata quando i due estremi cadevano nello stesso secondo. Inoltre la continuità QPC fra C0, C1 e C2 non era imposta.

Patch 7.1 richiede ora:

```text
C0.last.timestamp_unix_ms <= C1.first.timestamp_unix_ms
C1.last.timestamp_unix_ms <= C2.first.timestamp_unix_ms
C0.last.qpc < C1.first.qpc
C1.last.qpc < C2.first.qpc
qpc_frequency_hz identica per C0, C1 e C2
```

L'adiacenza UTC esatta resta valida; il QPC deve comunque essere strettamente crescente.

Nuovi reason code fail-closed:

```text
c012_timeline_overlap_or_order_invalid
c012_qpc_frequency_mismatch
c012_qpc_continuity_invalid
```

## Riproduzione

Prima della patch:

```text
C0 ultimo marker: 1.600.003 ms
C1 primo marker:  1.600.002 ms
coarse second per entrambi: 1600
risultato: SYNTHETIC_PASS
```

Dopo la patch:

```text
risultato: INCONCLUSIVE
reason: c012_timeline_overlap_or_order_invalid
```

## File modificati rispetto alla Patch 7

```text
lab/mt5_direct_endpoint/README.md
lab/mt5_direct_endpoint/RUNBOOK.md
lab/mt5_direct_endpoint/IMPLEMENTATION_REPORT.md
lab/mt5_direct_endpoint/tools/lab_model.py
lab/mt5_direct_endpoint/tests/test_lab_model.py
lab/mt5_direct_endpoint/tests/test_patch7_documentation_and_package.py
```

## Test offline

```text
Suite Python/evaluator: 182/182 PASS
Test statici MQL5:       18/18 PASS
Test statici Windows:    22/22 PASS
Totale:                 222/222 PASS
Python compileall:       PASS
```

I warning Python sono stati trattati come errori.

## Artefatti verificati

Archivio completo:

```text
mt5_direct_endpoint_patch7_1_review_2026-07-23.zip
SHA-256: e2f93f611cfabca923c70f1a1e1753593049fa39f01fb955973d4ebe3b2cbd5a
File: 75
```

Patch testuale Patch 7 → Patch 7.1:

```text
mt5_patch7_to_7_1.patch
```

Gli artefatti non sono stati inseriti automaticamente nel repository perché il branch remoto `main` non contiene ancora la directory sorgente `lab/mt5_direct_endpoint/`. Questa pagina conserva stato, risultati e provenienza; l'import del laboratorio completo deve mantenere esattamente il percorso `lab/mt5_direct_endpoint/`.

## Prossimo gate

Dopo l'import del laboratorio completo e una revisione indipendente della Patch 7.1:

```text
freeze del contratto offline
→ build .NET su Windows
→ compilazione del probe MQL5
→ smoke test innocui del Job Object
→ validazione ETW/WFP senza credenziali
→ eventuale autorizzazione separata per C0
```

Nessun passaggio autorizza ancora MT5, credenziali o test broker reali.
