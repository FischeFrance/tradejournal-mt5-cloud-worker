# MT5 Direct Endpoint Lab — Stato implementativo

## Sintesi

Il contratto offline, il Coordinator C012, l'Assembler, il Verifier,
l'endpoint registry e il worker persistente innocuo sono implementati e
coperti da test. L'automazione GUI reale e l'actual launch pubblico restano
`PARTIALLY_READY` e `HARD_DISABLED`.

```text
OFFLINE REVIEW: GO
WINDOWS INNOCUOUS VALIDATION: GO
CAPTURED EVIDENCE VERIFICATION: IMPLEMENTED
REAL UI AUTOMATION: NO-GO
PUBLIC MT5 ACTUAL LAUNCH: NO-GO
FIREWALL / WFP / BOOTSTRAP: NO-GO
```

## Componenti

| Componente | Responsabilità | Stato |
|---|---|---|
| `lab_model.py` | contratti, policy e valutazione fixture | stabile, monolitico |
| `JobHarness` | Job Object, Coordinator e IPC autenticato | verificato con processo innocuo |
| `evidence_assembler.py` | evidence v6 e manifest atomici | implementato |
| `lab_evidence_verifier.py` | verifica indipendente captured | implementato |
| `network_summary.py` | summary sanitizzato ETW/WFP | implementato |
| `endpoint_registry.py` | endpoint verificati, TTL e provenance | implementato |
| `mt5_dry_run.py` | config credential-free senza launch | implementato |
| account worker/supervisor | lifecycle persistente per account | verificato con launcher innocuo |
| broker identity resolver | server → suggestion AI | implementato, opzionale |
| `Mt5WizardAutomation` | orchestrazione UI fail-closed | PARTIALLY_READY / HARD_DISABLED |

## Binding autorevoli

C012 usa `C012_SINGLE_PROCESS_SESSION`, Job/root preimage e
`initial_c012_pre_state_sha256`. Il candidate descriptor vincola
`direct_campaign_manifest_sha256`.

La catena probe conserva `probe_source_sha256`; source, binary e deployment
restano oggetti distinti. Un digest della sorgente non dimostra quale binary
sia stato eseguito.

## Pulizia architetturale

La precedente ricerca AI diretta di IP/porta è stata rimossa dal WIP. Il solo
flusso mantenuto è:

```text
server -> suggerimento broker/search text -> UI MT5 -> exact server match
```

L'AI non produce endpoint verificati. La label suggerita non partecipa al
verdetto di selezione; l'esatto server mostrato da MT5 è autorevole.

I componenti account/registry restano nel laboratorio finché non esiste un
contratto di integrazione approvato con `windows_agent`. Non vengono copiati
o duplicati nel runtime di produzione.

## Sicurezza

- Nessun provider reale di credenziali nel laboratorio.
- Nessuna password in config, command line, metadata o output.
- Registry fail-closed su digest/provenance/TTL.
- Output assembler e CLI pubblicati atomicamente.
- WFP non applicabile a C0/C1/C2 non blocca; WFP richiesto ma mancante degrada
  secondo il controllo.
- ETW richiesto ma assente non può produrre `PASS`.
- Fixture sintetiche non possono diventare `CAPTURED_EXPORT`.

## Debito residuo

1. `lab_model.py` e i test Coordinator sono ancora grandi; dividerli richiede
   una patch dedicata senza cambiare il contratto.
2. Il profilo UI reale non è ancora versionato.
3. `Mt5WizardAutomation` non espone una CLI di actual launch.
4. L'attestazione source → binary → deployment del probe resta separata.
5. Il contratto `mt5-agent-v1` è sincronizzato manualmente tra repository.

Questi gap non autorizzano MT5, credenziali, rete, firewall, WFP, bootstrap o
registry promotion.
