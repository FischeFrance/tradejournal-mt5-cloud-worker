# MT5 Direct Endpoint Lab — Report di implementazione Patch 7

Data: 23 luglio 2026  
Perimetro: `lab/mt5_direct_endpoint/`  
Base fattuale: `MT5_Patch6_Independent_Audit.md`  
Obiettivo: allineamento offline di lifecycle, binding e pre-state C0-C5

## 1. Stato

```text
PATCH: PATCH_READY NEL SOLO SCOPE OFFLINE
HARNESS: PARTIALLY_READY
OFFLINE REVIEW: READY
C0-C5: NON ESEGUITI
WINDOWS RUNTIME: NO-GO
MT5 EXECUTION: NOT RUN
MT5 / CREDENZIALI / RETE BROKER: NO-GO
GO AL TEST REALE: NO-GO
GO SUCCESSIVO: esclusivamente revisione indipendente offline
```

`PATCH_READY` si riferisce al contratto offline e deve essere confermato dai
risultati finali riportati nella sezione 13. Non significa che il runtime sia
pronto: il JobHarness corrente non implementa ancora la sessione persistente
C012 e il verifier captured non esiste.

Durante la patch:

- non sono stati avviati MT5 o MetaEditor;
- non sono stati letti, creati o usati account, password o bootstrap reali;
- non sono state aperte connessioni esterne;
- non sono stati applicati Firewall/WFP né letti eventi Security;
- actual launch, bootstrap Create/Remove e registry promotion sono rimasti
  hard-disabled;
- non sono stati eseguiti C0-C5;
- non sono stati effettuati commit o push;
- nessun file fuori da `lab/mt5_direct_endpoint/` è stato modificato dalla
  Patch 7.

## 2. Riproduzione L1-L7 prima della patch

Le riproduzioni sono state eseguite offline contro lo snapshot Patch 6:

```text
/tmp/mt5-prompt7-before.z9fv8I/lab/mt5_direct_endpoint
```

La fixture canonica di partenza produceva `SYNTHETIC_PASS` nel percorso
test-only. I finding sono stati riprodotti così:

| ID | Riproduzione Patch 6 | Esito pre-patch |
|---|---|---|
| L1 | C0/C1/C2 contenevano tutti `assert_terminal_not_running`, `close_job_object` e `destroy_disposable_clone_after_export`; C1 non aveva launch, C2 non aveva submit `/config` esplicito | piano lifecycle contraddittorio; fixture ancora `SYNTHETIC_PASS` |
| L2 | mutato `run_context.portable_root_sha256`, mantenuto il path reale nel piano e ricalcolati i digest, sia in C012 sia in C4 | `SYNTHETIC_PASS` |
| L3 | probe C2/C3/C5 con `generated_at_unix=1`, output e digest dipendenti ricalcolati | `SYNTHETIC_PASS`; l'identità normalizzata scartava il timestamp |
| L4 | `C1.network.attribution_unambiguous=false` | `SYNTHETIC_PASS` |
| L5 | timeline C4 sovrapposta interamente a C3, con timestamp e digest ricalcolati | `SYNTHETIC_PASS` |
| L6 | ispezione di manifest, piano ed evidence C1 | sola azione generica/booleano; nessuna label negativa o relativo digest committed |
| L7 | C0/C1/C2 con tre `pre_state` clean, inclusi `servers_dat_absent` e `accounts_dat_absent` | fixture positiva; una transizione cache reale diventava validation error e non era rappresentabile |

Questa riproduzione spiega perché la Patch 7 modifica versioni e policy
anziché mantenere compatibilità nominale con gli artefatti Patch 6.

## 3. Decisione lifecycle basata sul codice esistente

È stata ispezionata l'intenzione del PoC di produzione, senza modificarla:

- `NativeMt5Runtime.prepare_broker()` avvia la discovery senza credenziali;
- in caso di successo lascia vivo il terminale perché il bootstrap successivo
  riutilizzi la discovery in memoria;
- il successivo percorso login invoca lo stesso executable con `/portable` e
  `/config:<bootstrap>`;
- la seconda invocazione consegna la configurazione all'istanza single-instance
  già viva; non rappresenta una nuova root generation;
- il chiamante esegue prima `prepare_broker()`, poi accede al segreto, quindi
  avvia il bootstrap.

Il modello scelto è quindi uno solo:

```text
C012_SINGLE_PROCESS_SESSION
```

```text
C0 LAUNCH_RETAIN
  NOT_RUNNING -> start root/Job -> baseline -> RETAINED
                                   |
C1 REUSE_RETAIN                    v
  RUNNING -> negative/exact discovery -> RETAINED
                                   |
C2 REUSE_CONFIG_SUBMIT_TEARDOWN    v
  RUNNING -> config submitter same Job -> login/reconnect -> TERMINATED
```

Il config v4 e il manifest v2 impegnano:

```text
c012_session_id
launch_control = C0
teardown_control = C2
root_process_generation_policy = SINGLE_SHARED_C0_C1_C2
allowed_transient_process_policy = C2_CONFIG_SUBMITTER_SAME_JOB_ONLY
```

Il control plan e l'evidence aggiungono ruolo, entry/exit state, Job identity,
Job manifest, root process generation e commitment del set di processi
transienti C2. C3-C5 usano invece
`INDEPENDENT_DISPOSABLE_CONTROL`.

## 4. Action list finale C0-C2

### C0

```text
assert_disposable_vm
assert_terminal_not_running
assert_terminal_hash_and_signature
start_etw_capture
assert_clean_portable_state
create_c012_job_and_root_process_generation
C0_BASELINE_START
start_terminal_without_identity
C0_BASELINE_END
stop_etw_capture
assert_capture_integrity
sanitize_evidence
retain_c012_session
```

### C1

```text
assert_disposable_vm
assert_existing_c012_root_process
assert_same_c012_job_and_process_generation
assert_terminal_hash_and_signature
start_etw_capture
assert_no_sensitive_config
C1_DISCOVERY_NEGATIVE_START
exact_search_negative_label
C1_DISCOVERY_NEGATIVE_END
C1_DISCOVERY_EXACT_START
exact_search_expected_server
C1_DISCOVERY_EXACT_END
assert_no_identity_submission
stop_etw_capture
assert_capture_integrity
sanitize_evidence
retain_c012_session
```

### C2

```text
assert_disposable_vm
assert_existing_c012_root_process
assert_same_c012_job_and_process_generation
assert_terminal_hash_and_signature
start_etw_capture
assert_dedicated_demo_investor_source
prepare_private_bootstrap_interactively
C2_LOGIN_START
submit_login_bootstrap_to_existing_terminal
verify_transient_submitter_same_job
C2_LOGIN_END
C2_CONNECTED_START / END
C2_NETWORK_INTERRUPTION_START / END
C2_RECONNECT_START / END
stop_etw_capture
assert_capture_integrity
sanitize_evidence
close_job_object
destroy_disposable_clone_after_export
```

C0/C1 non hanno teardown; C2 è l'unico teardown C012. C1 non richiede il
terminale spento e non avvia una seconda root.

Il marker `C2_LOGIN_START` precede il submit, quindi il trigger non può
generare traffico prima della finestra probatoria LOGIN. Anche
`terminal_command` è coerente con il ruolo: C0 usa
`terminal64.exe /portable`, C1 è `null`, C2/C3/C4/C5 usano
`/portable /config:<private-config>`.

## 5. Contratti aggiornati

| Contratto | Patch 6 | Patch 7 | Funzione principale |
|---|---:|---:|---|
| config | 3 | 4 | lifecycle e tolleranza probe |
| experiment manifest | 1 | 2 | commitment lifecycle e negative query |
| control plan | 1 | 2 | ruolo lifecycle, pre-state e azioni eseguibili |
| direct campaign manifest | 1 | 2 | ordine/descriptor temporali direct |
| candidate handoff | 1 | 2 | C2 lifecycle e digest direct manifest |
| evidence | 5 | 6 | lifecycle, transizioni e nuovi binding |
| proof binding | 4 | 5 | Job/root/pre-state/firewall/query |
| policy | v1 | v2 | freeze Patch 7 |

Timeline, network summary e identity probe wire schema restano rispettivamente
v1, v2 e v3/`3.0.0`; cambia l'identità normalizzata perché conserva il
timestamp.

Il comando CLI `digest` usa la stessa costante
`EVIDENCE_SCHEMA_VERSION = 6` impiegata da direct manifest e handoff; il test
confronta il digest esatto, non soltanto il formato esadecimale.

## 6. Portable-root binding

È stata eliminata la semantica opaca di `portable_root_sha256`. Il campo
autorevole nell'evidence è:

```text
run_context.portable_root_path_sha256
```

e deve essere uguale a:

```text
control_plan.path_bindings.terminal_data_path_sha256
```

Il digest è derivato dal path Windows canonico e verificato per C0-C5,
compreso C4 senza probe. I nuovi commitment comprendono:

```text
job_portable_root_binding_sha256
pre_state_binding_sha256
probe_path_binding_sha256              quando applicabile
firewall_portable_root_binding_sha256  per C3/C4/C5
lifecycle_binding_sha256
state_transition_sha256
```

Mismatch osservati sono `FAIL`; proof mancanti restano `INCONCLUSIVE`.

## 7. Probe timestamp e source intent

`compose_identity()` conserva ora:

```text
probe_generated_at_unix
```

Per C2, C3 e C5 il modello lo confronta con la finestra autenticata della
timeline, da login start a connected end, usando soltanto la
`probe_timestamp_tolerance_seconds` piccola e manifest-bound. Un timestamp
prima o dopo la finestra è `FAIL`; l'assenza della prova necessaria è
`INCONCLUSIVE`.

È stata scelta l'opzione B del Prompt 7 per il source hash:

- `probe.source_sha256` resta nel config/manifest come commitment
  dell'intento sorgente approvato;
- l'identità normalizzata non dichiara `probe_source_sha256`;
- non viene affermato che il source committed sia il binary realmente
  compilato/eseguito;
- la prova source→binary→deployment è rimandata al futuro deployment verifier
  EX5.

Il booleano `probe_hash_verified` non è presentato come sostituto di questa
attestazione.

## 8. C1: attribuzione e negative query

Quando:

```text
endpoint_delta_source = PROCESS_SCOPED_TCP_FLOW_SET
```

il modello richiede `network.attribution_unambiguous=true`. In caso contrario
restituisce:

```text
INCONCLUSIVE / discovery_flow_attribution_ambiguous
```

Il manifest v2 genera una query non sensibile e univoca:

```text
TJ-NO-SUCH-<experiment-UUIDv4>
```

Piano, evidence e proof binding impegnano:

```text
negative_query_label_sha256
negative_query_result_count = 0
negative_query_ui_binding_verified
negative_query_binding_sha256
```

La label è strutturata, diversa dalla requested server label e deterministica
per experiment ID; esperimenti differenti non la riutilizzano.

## 9. Ordine C3→C4→C5

Il direct campaign manifest v2 contiene:

```text
canonical_order = [C3, C4, C5]
ordering_policy = STRICT_NON_OVERLAPPING
```

L'evaluator verifica sia run context sia timeline:

```text
C3.completed_at <= C4.started_at
C4.completed_at <= C5.started_at
C5.started_at - C3.completed_at >= c5_separation_minimum
```

Overlap, inversione o incongruenza fra i due clock producono:

```text
INCONCLUSIVE / direct_controls_temporal_order_invalid
```

## 10. Pre-state C012 e transizioni

Il nuovo artefatto:

```text
C012_INITIAL_PRE_STATE
```

produce un solo `initial_c012_pre_state_sha256` prima di C0. C0 include la
fotografia clean; C1 e C2 hanno `pre_state=null` e devono riferire lo stesso
commitment.

Le transizioni sono distinte:

| Controllo | Stage | Broker cache | Account cache |
|---|---|---|---|
| C0 | `C0_INITIAL` | `ABSENT` | `ABSENT` |
| C1 | `C1_DISCOVERY_COMPLETE` | `CREATED_RECORDED` oppure `ABSENT_RECORDED` | `ABSENT_RECORDED` |
| C2 | `C2_LOGIN_COMPLETE` | `INHERITED_RECORDED`, `CREATED_RECORDED` oppure `ABSENT_RECORDED` | `CREATED_RECORDED` oppure `ABSENT_RECORDED` |

C1/C2 richiedono un `transition_evidence_sha256`. Nessuna transizione può
esportare materiale sensibile. C3/C4/C5 conservano snapshot cold-boot
indipendenti e obbligatoriamente clean.

La validazione cross-control concatena gli stati C1→C2. Sono ammesse soltanto
le coppie broker cache `CREATED→INHERITED`, `ABSENT→CREATED` e
`ABSENT→ABSENT`; una sequenza impossibile produce
`FAIL / c012_state_transition_incompatible`.

## 11. Handoff e grafo dei digest

La catena resta aciclica:

```text
C2 evidence
  -> direct campaign manifest v2
       -> candidate handoff v2
            -> piani C3/C4/C5 v2
```

Il candidate handoff lega `direct_campaign_manifest_sha256`, oltre a
candidate, identità, terminale e commitment lifecycle/pre-state C2. Il direct
campaign manifest contiene i descriptor e la policy temporale dei controlli
direct. Non contiene i digest dei piani C3/C4/C5: vengono generati dopo e
legano handoff/manifest.

Il builder dell'handoff è deliberatamente test-only e accetta soltanto
evidence sintetica. I riferimenti dell'handoff non dimostrano isolatamente
la coerenza globale C0→C1→C2: `evaluate_campaign()` la verifica sull'intera
campagna. La promozione di evidence captured resta hard-disabled.

## 12. Re-audit avversariale L1-L7

La suite Patch 7 include test nominativi per lifecycle, portable root,
timestamp, attribuzione, negative query, ordine direct, pre-state,
documentazione e layout ZIP. Gli esiti finali devono essere riportati senza
promuovere fixture a prova reale.

| ID | Gate post-patch atteso |
|---|---|
| L1 | action list non canonica rifiutata; C0/C1 retain, solo C2 teardown |
| L2 | root diversa dal terminal data path: `FAIL / portable_root_control_plan_path_mismatch` |
| L3 | timestamp prima/dopo finestra: `FAIL` |
| L4 | attribuzione C1 ambigua: `INCONCLUSIVE / discovery_flow_attribution_ambiguous` |
| L5 | overlap/inversione direct: `INCONCLUSIVE / direct_controls_temporal_order_invalid` |
| L6 | query/digest/UI/count mancanti o incoerenti: non positivo |
| L7 | commitment C012 divergente o cache iniziale contaminata: non positivo |

Risultato effettivo del gate finale:

```text
L1   VALIDATION_ERROR  control plan actions differ from the committed policy
L2   FAIL              portable_root_control_plan_path_mismatch
L3   FAIL              identity_probe_timestamp_outside_authenticated_window
L4   INCONCLUSIVE      discovery_flow_attribution_ambiguous
L5   INCONCLUSIVE      direct_controls_temporal_order_invalid
L6   FAIL              negative_query_returned_results
L7a  VALIDATION_ERROR  C1 must reference the single C012 initial pre-state
L7b  FAIL              c012_pre_state_not_clean
```

Tutte le otto mutazioni hanno quindi prodotto un esito non positivo. Nessuna
fixture alterata è stata promossa a `SYNTHETIC_PASS` o `PASS`.

## 13. Test eseguiti

Comandi autorizzati:

```text
python3 -m unittest discover -s lab/mt5_direct_endpoint/tests -p 'test_*.py' -v
python3 -m unittest discover -s lab/mt5_direct_endpoint/mql5/tests -p 'test_*.py' -v
python3 -m unittest discover -s lab/mt5_direct_endpoint/windows/tests -p 'test_*.py' -v
python3 -m compileall -q lab/mt5_direct_endpoint
```

Risultati:

```text
suite laboratorio:                 174/174 PASS
suite MQL5 statica:                  18/18 PASS
suite Windows statica:               22/22 PASS
totale:                            214/214 PASS
warnings trattati come errori:     214/214 PASS
compileall (Python 3.9.6):                 PASS
lettura JSON UTF-8/UTF-8-BOM:        16/16 PASS
meta-validazione JSON Schema:          8/8 PASS
esempi validati via API e schema:      4/4 PASS
audit action list C0-C5:                   PASS
mutation test L1-L7:                    8/8 non positive
```

Non sono stati eseguiti:

| Verifica | Motivo |
|---|---|
| PowerShell runtime | ambiente corrente non Windows; runtime vietato |
| JobHarness .NET runtime | actual launch vietato; coordinator C012 assente |
| MetaEditor | non disponibile e avvio vietato |
| MT5 / login / C0-C5 | vietati dalla consegna |
| ETW/WFP/Firewall runtime | vietati dalla consegna |
| rete esterna | vietata dalla consegna |

## 14. File Patch 7

Le modifiche restano sotto `lab/mt5_direct_endpoint/` e comprendono queste
classi di file:

```text
README.md
RUNBOOK.md
IMPLEMENTATION_REPORT.md
tools/lab_model.py
tools/labctl.py
tools/package_lab.py
schemas/*.schema.json
examples/*.json
tests/test_lab_model.py
tests/test_contract_files.py
tests/test_patch7_documentation_and_package.py
```

```text
snapshot Patch 6: 73 file
stato Patch 7:     75 file
aggiunti:           2 file
modificati:        18 file
rimossi:            0 file

AGGIUNTI
tests/test_patch7_documentation_and_package.py
tools/package_lab.py

MODIFICATI
IMPLEMENTATION_REPORT.md
README.md
RUNBOOK.md
examples/control-plan.c0.synthetic.json
examples/evidence.c0.synthetic-pass.json
examples/experiment-manifest.synthetic.json
examples/experiment.c0-c2.example.json
schemas/candidate-handoff.schema.json
schemas/control-plan.schema.json
schemas/direct-campaign-manifest.schema.json
schemas/evidence.schema.json
schemas/experiment-config.schema.json
schemas/experiment-manifest.schema.json
tests/test_contract_files.py
tests/test_lab_model.py
tests/test_labctl.py
tools/lab_model.py
tools/labctl.py
```

Nessun file di produzione fuori dal lab deve comparire nel diff Patch 7.

## 15. Hard-disable confermati

| Capability | Stato |
|---|---|
| JobHarness actual launch | `HARD_DISABLED`, stop prima di `CreateProcessW` |
| bootstrap Create/Remove | `CAPABILITY_HARD_DISABLED` |
| Firewall Apply/Rollback | `HARD_DISABLED` |
| WFP/Security Execute | `HARD_DISABLED`, nessuna lettura Security |
| MT5 / MetaEditor | non avviati |
| credential access/injection | non usato né abilitato |
| registry promotion | disabilitata |
| captured evidence probatoria | non implementata |

Gli exporter captured restano:

```text
readiness = NO_GO
proof_capable = false
```

La CLI ordinaria non può promuovere fixture sintetiche a `PASS`.

## 16. Rischi residui

1. Il JobHarness è one-shot. Per eseguire davvero C0→C1→C2 serve un
   coordinator persistente/IPC che mantenga ownership di Job, processo,
   clone e root tra le fasi. Il contratto offline è coerente, il runtime
   corrente non lo è ancora.
2. Manca il verifier indipendente degli artefatti captured.
3. Manca il deployment verifier EX5 per attestare source, binary e probe
   effettivamente caricato.
4. Job Object, ETW, WFP, exporter e marker non sono stati provati su Windows.
5. I digest sono commitment, non firme, TPM quote o attestazioni hardware.
6. La sorgente captured richiede connection ID provider-derived; una
   attribuzione assente o ambigua degrada la prova.
7. Il config submitter single-instance e la sua appartenenza allo stesso Job
   devono ancora essere dimostrati con un processo innocuo e poi revisionati
   su Windows, senza MT5.
8. Il probe MQL5 non è stato compilato con MetaEditor.
9. La gestione credenziali e il bootstrap reale restano intenzionalmente
   assenti/hard-disabled.
10. Nessun risultato corrente dimostra o falsifica l'ipotesi
    `Server=IP:porta`.

## 17. Isolamento delle modifiche

Branch e HEAD non devono cambiare. Il repository era già dirty prima di
Patch 7; le modifiche preesistenti appartengono all'utente e non fanno parte
del lavoro. Il confronto autorevole per questa patch è fra lo snapshot:

```text
/tmp/mt5-prompt7-before.z9fv8I/lab/mt5_direct_endpoint
```

e la directory corrente `lab/mt5_direct_endpoint/`, escludendo cache e
pacchetti generati. Nessun commit o push è stato effettuato.

## 18. Pacchetto di revisione

Il packager Patch 7 conserva il prefisso repository-relative:

```text
lab/mt5_direct_endpoint/
```

e deve escludere cache, bytecode, `.DS_Store`, ZIP annidati, `.ex5`,
artefatti ETW/WFP/packet capture, segreti e output raw/private/sanitized di
run. Usa un allowlist dei tipi sorgente, richiede i membri essenziali, verifica
membership e CRC, rifiuta symlink e nomi ambigui per l'estrazione Windows e
pubblica atomicamente senza overwrite.

```text
nome ZIP:    mt5_direct_endpoint_patch7_review_2026-07-23.zip
entry:       75
layout:      tutte sotto lab/mt5_direct_endpoint/
unzip -t:    No errors detected
```

Il digest SHA-256 dell'archivio finale viene calcolato dopo la sua creazione
e riportato nella consegna esterna. Non può essere auto-incluso nel report
contenuto nello stesso ZIP senza modificare, e quindi invalidare, il digest
dell'archivio.

## 19. GO/NO-GO

| Attività | Decisione |
|---|---|
| revisione indipendente offline finale di codice, schemi, test e ZIP | **GO** |
| freeze del contratto | dopo review offline senza finding bloccanti |
| build/dry-run Windows | **NO-GO** finché manca il coordinator C012 e un'autorizzazione separata |
| C0-C5 | **NO-GO** |
| MT5, login, account, password o rete broker | **NO-GO** |
| Firewall, WFP, bootstrap o actual launch | **NO-GO** |
| profilo REAL | **NO-GO** |

Il solo GO espresso è per la revisione indipendente offline. Patch 7 non
autorizza C0, Windows runtime, MT5 o credenziali.
