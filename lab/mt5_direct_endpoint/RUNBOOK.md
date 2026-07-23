# Runbook C0-C5 — Patch 7

Questo runbook descrive il protocollo futuro e le verifiche offline della
Patch 7. Non autorizza l'esecuzione del laboratorio.

```text
PATCH: PATCH_READY NEL SOLO SCOPE OFFLINE
HARNESS: PARTIALLY_READY
OFFLINE REVIEW: READY
C0-C5: NON ESEGUITI
WINDOWS RUNTIME: NO-GO
MT5 EXECUTION: NOT RUN
GO AL TEST REALE: NO-GO
GO SUCCESSIVO: esclusivamente revisione indipendente offline
```

Non avviare MT5 o MetaEditor, non usare credenziali, non creare bootstrap
reali, non aprire rete esterna, non applicare Firewall/WFP, non leggere
Security e non abilitare actual launch, bootstrap Create/Remove o registry
promotion.

## 1. Scope e divieti

Il profilo corrente è esclusivamente DEMO. Non è consentito sostituire
`DEMO` con `REAL` in config o fixture. Restano fuori scope:

- account cliente, finanziati o live;
- login reale e operazioni di trading;
- contatto con broker o VPS legacy;
- verifier captured;
- modifica del Broker Resolver di produzione.

La sola attività consentita da questo documento è la revisione offline di
codice, schemi, fixture, piani e pacchetto.

## 2. Contratti da revisionare

```text
experiment config:            v4
experiment manifest:          v2
control plan:                 v2
direct campaign manifest:     v2
candidate handoff:            v2
evidence / proof binding:     v6 / v5
timeline:                     v1
identity probe:               schema v3, probe 3.0.0
network summary/event:        v2 / v2
policy:                       mt5-direct-endpoint-policy-v2
```

Il config v4 include una sola modalità lifecycle:

```text
lifecycle_mode = C012_SINGLE_PROCESS_SESSION
c012_session_id = UUIDv4
launch_control = C0
teardown_control = C2
root_process_generation_policy = SINGLE_SHARED_C0_C1_C2
allowed_transient_process_policy = C2_CONFIG_SUBMITTER_SAME_JOB_ONLY
```

Qualunque altra modalità, incluso un restart implicito, deve essere
rifiutata. Le versioni Patch 6 sono legacy non probatorie e devono essere
rigenerate, non rinumerate.

## 3. Verifiche offline iniziali

Dalla root del repository:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover \
  -s lab/mt5_direct_endpoint/tests -p 'test_*.py' -v
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover \
  -s lab/mt5_direct_endpoint/mql5/tests -p 'test_*.py' -v
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover \
  -s lab/mt5_direct_endpoint/windows/tests -p 'test_*.py' -v
python3 -m compileall -q lab/mt5_direct_endpoint
```

Validare gli esempi senza creare attività runtime:

```bash
python3 lab/mt5_direct_endpoint/tools/labctl.py validate-config \
  --config lab/mt5_direct_endpoint/examples/experiment.c0-c2.example.json

python3 lab/mt5_direct_endpoint/tools/labctl.py validate-manifest \
  --manifest lab/mt5_direct_endpoint/examples/experiment-manifest.synthetic.json

python3 lab/mt5_direct_endpoint/tools/labctl.py validate-plan \
  --plan lab/mt5_direct_endpoint/examples/control-plan.c0.synthetic.json \
  --manifest lab/mt5_direct_endpoint/examples/experiment-manifest.synthetic.json
```

Ogni piano deve mantenere:

```json
{
  "plan_only": true,
  "mt5_start_enabled": false,
  "firewall_apply_enabled": false,
  "credential_access_enabled": false,
  "registry_promotion_enabled": false
}
```

Un test che richieda rete o runtime Windows non appartiene a questa fase.

## 4. Catena degli artefatti

```text
config v4
  -> experiment manifest v2
       -> piani C0/C1/C2 v2
            -> evidence C2 v6
                 -> direct campaign manifest v2
                      -> candidate handoff v2
                           -> piani C3/C4/C5 v2
                                -> evidence C3/C4/C5 v6
```

Il manifest iniziale impegna terminale, expected identity DEMO, durate,
tolleranza del timestamp probe, lifecycle, query negativa, policy di rete e
source intent del probe.

Il candidate handoff lega `direct_campaign_manifest_sha256`. Il direct
campaign manifest contiene descriptor e policy temporale C3/C4/C5; non
contiene i digest dei piani direct. I piani C3/C4/C5 vengono costruiti in
seguito e legano a loro volta handoff e direct campaign manifest. Questa
sequenza evita un ciclo di digest.

Il builder dell'handoff è test-only e accetta soltanto evidence sintetica.
I riferimenti dell'handoff non sostituiscono la validazione dell'intera
campagna C0→C1→C2, che compete a `evaluate_campaign()`. La promozione da
evidence captured resta hard-disabled.

Comandi builder esclusivamente offline:

```bash
python3 lab/mt5_direct_endpoint/tools/labctl.py build-manifest \
  --config <experiment-v4.json> --output <manifest-v2.json>

python3 lab/mt5_direct_endpoint/tools/labctl.py plan \
  --config <experiment-v4.json> --control C0 \
  --run-id <uuid-v4> --output <c0-plan-v2.json>
```

`--output` deve creare un file nuovo in modo atomico e fallire se esiste già.
La costruzione del direct campaign e dell'handoff è ammessa soltanto con una
fixture C2 sintetica positivamente valutata; captured handoff resta
hard-disabled in assenza del verifier indipendente.

## 5. Layout pianificato

```text
C:\TJLab\<experiment-id>\
  C012\terminal\
  C3\terminal\
  C4\terminal\
  C5\terminal\
  runs\<run-id>\<control>\
    private\
    raw\
    sanitized\
```

C0-C2 condividono clone, utente, portable root, Job identity,
`c012_session_id` e root process generation. C3-C5 hanno clone, utente, root,
Job e generazione indipendenti. Il planner calcola i path, ma non crea
directory né processi.

Il digest:

```text
run_context.portable_root_path_sha256
```

deve essere il digest del path Windows canonico e coincidere con:

```text
control_plan.path_bindings.terminal_data_path_sha256
```

La verifica è obbligatoria in C0-C5, incluso C4. Il proof binding v5 deve
legarla a Job manifest, Job identity, process generation, pre-state,
transizione, probe quando presente e firewall plan nei direct control.

## 6. Lifecycle operativo C0→C1→C2

L'intenzione verificata nel codice di produzione è mantenere vivo il
terminale dopo `prepare_broker()`. La successiva invocazione `/config`
consegna il bootstrap all'istanza già viva. Il contratto rappresenta quindi
un'unica sessione:

```text
NOT_RUNNING
    |
    v
C0: launch root una volta ---- retain
    |
    v
C1: reuse stessa root -------- retain
    |
    v
C2: reuse + config submitter - teardown
    |
    v
TERMINATED
```

### 6.1 Action list C0

La lista canonica deve includere, nell'ordine:

```text
assert_disposable_vm
assert_terminal_not_running
assert_terminal_hash_and_signature
start_etw_capture
assert_clean_portable_state
create_c012_job_and_root_process_generation
mark C0_BASELINE_START
start_terminal_without_identity
mark C0_BASELINE_END
stop_etw_capture
assert_capture_integrity
sanitize_evidence
retain_c012_session
```

C0 non deve contenere `close_job_object` o
`destroy_disposable_clone_after_export`.

### 6.2 Action list C1

C1 deve iniziare dal terminale ancora vivo:

```text
assert_disposable_vm
assert_existing_c012_root_process
assert_same_c012_job_and_process_generation
assert_terminal_hash_and_signature
start_etw_capture
assert_no_sensitive_config
mark C1_DISCOVERY_NEGATIVE_START
exact_search_negative_label
mark C1_DISCOVERY_NEGATIVE_END
mark C1_DISCOVERY_EXACT_START
exact_search_expected_server
mark C1_DISCOVERY_EXACT_END
assert_no_identity_submission
stop_etw_capture
assert_capture_integrity
sanitize_evidence
retain_c012_session
```

C1 non contiene `assert_terminal_not_running`, un secondo launch o teardown.

### 6.3 Action list C2

```text
assert_disposable_vm
assert_existing_c012_root_process
assert_same_c012_job_and_process_generation
assert_terminal_hash_and_signature
start_etw_capture
assert_dedicated_demo_investor_source
prepare_private_bootstrap_interactively
mark C2_LOGIN_START
submit_login_bootstrap_to_existing_terminal
verify_transient_submitter_same_job
observe LOGIN / mark C2_LOGIN_END
mark/observe CONNECTED
mark/observe NETWORK_INTERRUPTION
mark/observe RECONNECT
stop_etw_capture
assert_capture_integrity
sanitize_evidence
close_job_object
destroy_disposable_clone_after_export
```

Il config submitter è l'unico processo transiente ammesso e deve risultare
nello stesso Job. Non rappresenta una nuova root generation. C2 è l'unico
controllo che chiude C012.

Il marker `C2_LOGIN_START` precede obbligatoriamente il submit: così nessun
flow causato dal config submitter può precedere formalmente la finestra
LOGIN. Le command line canoniche sono:

```text
C0: terminal64.exe /portable
C1: null
C2/C3/C4/C5: terminal64.exe /portable /config:<private-config>
```

`terminal_command=null` in C1 significa riuso, non un launch implicito.

### 6.4 Blocco runtime

Questa action list è internamente coerente nel modello, ma il JobHarness
attuale è one-shot e non mantiene un Job aperto tra tre invocazioni. Prima di
qualsiasi dry-run Windows occorre un coordinator persistente, con IPC
autenticato, protocollo di stato C0→C1→C2, ownership del Job e teardown
fail-closed. Quel componente non è implementato da Patch 7. Non improvvisare
tre lanci del JobHarness: violerebbero il contratto di sessione e root
generation.

## 7. Pre-state e transizioni

Acquisire concettualmente una sola fotografia
`C012_INITIAL_PRE_STATE` prima di C0. Il suo
`initial_c012_pre_state_sha256` include experiment, sessione, portable root e
check clean. C0 esporta lo snapshot booleano completo; C1 e C2 impostano
`pre_state=null` e riferiscono lo stesso digest.

Transizioni ammesse e obbligatoriamente impegnate:

```text
C0_INITIAL:
  broker_cache_state = ABSENT
  account_cache_state = ABSENT

C1_DISCOVERY_COMPLETE:
  broker_cache_state = CREATED_RECORDED | ABSENT_RECORDED
  account_cache_state = ABSENT_RECORDED
  transition_evidence_sha256 richiesto

C2_LOGIN_COMPLETE:
  broker_cache_state = INHERITED_RECORDED | CREATED_RECORDED | ABSENT_RECORDED
  account_cache_state = CREATED_RECORDED | ABSENT_RECORDED
  transition_evidence_sha256 richiesto
```

`transition_verified` deve essere vero per una conclusione positiva e
`sensitive_material_exported` deve essere falso. La possibile creazione di
cache broker dopo C1 e account cache dopo C2 è una transizione registrata, non
un nuovo pre-state fittiziamente clean.

La macchina di stato cross-control ammette soltanto:

```text
C1 CREATED_RECORDED -> C2 INHERITED_RECORDED
C1 ABSENT_RECORDED  -> C2 CREATED_RECORDED
C1 ABSENT_RECORDED  -> C2 ABSENT_RECORDED
```

Una sequenza non rappresentata, per esempio `ABSENT_RECORDED` seguito da
`INHERITED_RECORDED`, è
`FAIL / c012_state_transition_incompatible`.

C3, C4 e C5 usano ciascuno uno snapshot cold-boot completo, distinto da C012
e obbligatoriamente clean. Cache presente prima di C0 o prima di un direct
control è `FAIL`.

## 8. Query negativa e discovery C1

Il manifest costruisce:

```text
TJ-NO-SUCH-<experiment-UUIDv4>
```

Il piano C1 impegna il digest della label e `expected_result_count=0`.
L'evidence deve riportare digest osservato, risultato zero e
`negative_query_ui_binding_verified=true`; il proof binding lega questi dati
alla timeline e al piano. La label deve essere diversa dalla requested
server label.

Per un delta con fonte `PROCESS_SCOPED_TCP_FLOW_SET`, richiedere:

```text
attribution_unambiguous = true
process_scoped_tcp_flows >= 1
flow_record_set_sha256 presente e verificato
```

Attribuzione ambigua non è un risultato negativo: è
`INCONCLUSIVE / discovery_flow_attribution_ambiguous`.

## 9. Probe

Il probe schema v3 restituisce dati sanitizzati. L'identità normalizzata
conserva:

```text
probe_run_id
probe_generated_at_unix
terminal_build
terminal_path_sha256
terminal_data_path_sha256
identity_probe_output_sha256
```

Il timestamp deve appartenere alla stessa run e cadere tra il marker login
start e connected end, con la sola
`probe_timestamp_tolerance_seconds` impegnata nel manifest. Probe stale o
futuri sono `FAIL`; prova incompleta è `INCONCLUSIVE`.

Il `probe.source_sha256` di config/manifest esprime l'intento sorgente
approvato. Non è presente come `probe_source_sha256` nell'identità
normalizzata e non dimostra quale binary sia stato eseguito. La catena
source→binary→deployment è un requisito del futuro deployment verifier EX5.
Non promuovere `probe_hash_verified` ad attestazione equivalente.

## 10. Campagna direct

Il direct campaign manifest v2 impegna:

```text
canonical_order = [C3, C4, C5]
ordering_policy = STRICT_NON_OVERLAPPING
c5_separation_anchor = C3_COMPLETED_AT_UNIX
```

Verificare sia i timestamp run context sia gli ultimi/primi marker:

```text
C3.completed_at <= C4.started_at
C4.completed_at <= C5.started_at
C5.started_at - C3.completed_at >= c5_separation_minimum
```

Overlap o inversioni sono
`INCONCLUSIVE / direct_controls_temporal_order_invalid`. Ogni controllo resta
indipendente e viene distrutto al termine.

## 11. Accounting e firewall binding

Per ogni fase:

```text
process_scoped_tcp_flows = candidate_tcp_flows + other_tcp_flows
```

Ogni record TCP appartiene una sola volta a `candidate` oppure `other` e ha
una disposition unica. DNS e non-TCP sono contatori separati.

- C3/C5: candidate connesso, `other_tcp_flows=0`, DNS e non-TCP zero.
- C4: candidate tentato/bloccato e mai connesso, stessi zeri.

Per C3-C5 il `firewall_portable_root_binding_sha256` lega piano, firewall
plan, portable root e candidate. Apply/Rollback rimangono hard-disabled:
questo è solo un contratto da verificare offline.

Il deny esterno resta `DEFENSE_IN_DEPTH_NON_PROBATORY`; non sostituisce il
controllo process-scoped e non influenza da solo il verdict.

## 12. Valutazione offline

```bash
python3 lab/mt5_direct_endpoint/tools/labctl.py validate-evidence \
  --evidence <evidence-v6.json>

python3 lab/mt5_direct_endpoint/tools/labctl.py evaluate \
  --config <experiment-v4.json> \
  --manifest <manifest-v2.json> \
  --control-plan <control-plan-v2.json> \
  --evidence <evidence-v6.json>
```

Esiti:

- `FAIL`: osservazione che contraddice il requisito;
- `INCONCLUSIVE`: prova, health, attribuzione, binding o contesto mancanti;
- `SYNTHETIC_PASS`: soltanto fixture test-only, exit 3;
- `PASS`: riservato al futuro verifier e oggi irraggiungibile.

`CAPTURED_EXPORT` resta inconclusivo perché i producer correnti dichiarano
`NO_GO` e `proof_capable=false`.

## 13. Packaging per la review

Creare il pacchetto dalla root del repository usando il packager offline
dedicato. La struttura interna obbligatoria è:

```text
lab/mt5_direct_endpoint/
```

```bash
python3 lab/mt5_direct_endpoint/tools/package_lab.py \
  <percorso-destinazione/patch7-review.zip>
```

Il packager usa un allowlist dei tipi sorgente, richiede documenti, modello e
schemi essenziali, esclude cache, raw/private/sanitized, ZIP annidati,
bytecode, `.ex5` e artefatti ETW/WFP/packet capture. Scrive prima un file
temporaneo, ne verifica membri e CRC, poi lo pubblica atomicamente senza
sovrascrivere una destinazione esistente. Rifiuta anche symlink e nomi
ambigui per l'estrazione Windows. Prima della consegna:

```bash
unzip -t <patch7-review.zip>
unzip -Z1 <patch7-review.zip> | sed -n '1,20p'
shasum -a 256 <patch7-review.zip>
```

Non includere segreti, output runtime, `.DS_Store` o file temporanei.

## 14. Gate prima di qualsiasi futura autorizzazione

Servono almeno:

1. review indipendente offline finale e freeze del contratto;
2. coordinator persistente C012/IPC e threat model approvati;
3. verifier captured indipendente;
4. build .NET e test Job Object con processo innocuo su Windows;
5. test PowerShell PlanOnly/AST e dry-run Windows;
6. deployment verifier EX5 del probe source/binary;
7. validazione ETW/WFP senza broker, credenziali o rete esterna;
8. VM disposable, volume cifrato e console out-of-band;
9. autorizzazione separata, esplicita e revocabile per C0.

Decisione corrente:

```text
REVISIONE INDIPENDENTE OFFLINE:        GO
WINDOWS RUNTIME VALIDATION:            NO-GO
C0-C5:                                 NO-GO
MT5 / METAEDITOR / CREDENZIALI:        NO-GO
FIREWALL / WFP / BOOTSTRAP:            NO-GO
PROFILO REAL:                          NO-GO
```
