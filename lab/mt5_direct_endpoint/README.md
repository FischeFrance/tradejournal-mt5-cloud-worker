# MT5 Direct Endpoint Lab

Laboratorio offline e fail-closed per verificare il contratto C0–C5 del
collegamento MT5 tramite endpoint diretto. Il laboratorio non è il runtime di
produzione e non abilita automaticamente MT5.

## Stato corrente

```text
CONTRACT PATCH 7.1: VERIFIED
C012 COORDINATOR + AUTHENTICATED IPC: VERIFIED OFFLINE
INNOCUOUS JOB OBJECT RUNTIME: VERIFIED ON WINDOWS
CAPTURED EVIDENCE VERIFIER: IMPLEMENTED
OFFLINE EVIDENCE ASSEMBLER: IMPLEMENTED
ASSEMBLER -> VERIFIER INTEGRATION: VERIFIED
ACCOUNT WORKER/SUPERVISOR: VERIFIED WITH INNOCUOUS PROCESS
BROKER ENDPOINT REGISTRY + DRY-RUN: IMPLEMENTED
UI ONBOARDING: PARTIALLY_READY / HARD_DISABLED
ACTUAL MT5 LAUNCH FROM PUBLIC HARNESS: NO-GO
FIREWALL / WFP / BOOTSTRAP / REGISTRY PROMOTION: NO-GO
```

## Confini

Il laboratorio può:

- validare schemi, manifest, timeline e digest;
- costruire fixture sintetiche;
- assemblare evidence da artefatti locali;
- verificare evidence captured;
- esercitare Job Object e Coordinator con il processo innocuo incorporato;
- risolvere endpoint `VERIFIED` in un config dry-run privo di credenziali.

Il laboratorio non può:

- avviare MT5 o MetaEditor dal percorso pubblico;
- usare credenziali o account reali;
- applicare firewall, WFP o bootstrap;
- promuovere automaticamente un endpoint;
- trasformare una fonte pubblica o una risposta AI in `VERIFIED`.

## Contratto C012

C0, C1 e C2 condividono:

- `c012_session_id`;
- Job identity e root process generation;
- `C012_SINGLE_PROCESS_SESSION`;
- `initial_c012_pre_state_sha256`;
- frequenza QPC e timeline monotona.

C0 crea e mantiene il processo; C1 riusa la stessa generazione; C2 è l'unico
teardown. C3, C4 e C5 sono controlli indipendenti.

Il candidate handoff include il descriptor e il binding
`direct_campaign_manifest_sha256`. I binding del probe includono
`probe_source_sha256`; l'attestazione source → binary → deployment resta un
controllo separato e non viene dedotta dal solo digest.

## Evidence

- `tools/evidence_assembler.py` produce evidence v6, artifact manifest e report.
- `tools/lab_evidence_verifier.py` ricomputa autonomamente canonical JSON,
  digest e binding.
- Una fixture sintetica può ottenere soltanto `SYNTHETIC_PASS` nel percorso
  test-only.
- ETW o WFP richiesti ma assenti degradano il risultato secondo applicabilità.
- Preimage non verificabili impediscono `PASS`.
- Output e registry sono pubblicati atomicamente e fail-closed.

## Endpoint e onboarding

`tools/endpoint_registry.py` accetta come `VERIFIED` solo endpoint associati a
un metodo di verifica MT5 allowlisted e a provenance con digest verificabile.
`tools/mt5_dry_run.py` genera soltanto:

```ini
[Common]
Server=host:port
```

e non avvia il terminale.

Il resolver AI server → broker è suggestion-only. Il Wizard C# cerca il testo
suggerito, ma seleziona esclusivamente un unico risultato MT5 contenente
l'esatto server richiesto. La label AI non è autorevole. Actual UI automation
resta `HARD_DISABLED`.

## Test offline

Dalla root:

```bash
python3 -m unittest discover -s lab/mt5_direct_endpoint/tests -q
python3 -m unittest discover -s lab/mt5_direct_endpoint/mql5/tests -q
python3 -m unittest discover -s lab/mt5_direct_endpoint/windows/tests -p 'test_*.py' -q
PYTHONPYCACHEPREFIX=/tmp/mt5-pycache python3 -m compileall -q lab/mt5_direct_endpoint
```

Su Windows la workflow compila JobHarness, esegue Coordinator e smoke
innocui, valida il profilo WPR senza iniziare una trace ed esegue i dry-run
PowerShell.

## Documenti

- `RUNBOOK.md`: sequenza operativa offline.
- `IMPLEMENTATION_REPORT.md`: componenti e gap.
- `docs/ACCOUNT_WORKER_MODEL.md`: worker persistente per account.
- `src/JobHarness/README.md`: Coordinator, Job Object e gate.
- `windows/README.md`: strumenti Windows del laboratorio.
