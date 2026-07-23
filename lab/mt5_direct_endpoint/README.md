# MT5 Direct Endpoint Lab

Harness isolato e fail-closed per preparare i controlli falsificabili C0-C5
del test `Server=IP:porta`. La Patch 7 allinea il contratto offline al
lifecycle C0→C1→C2 che il prodotto intende eseguire, ma non avvia il
laboratorio né MetaTrader 5.

## Stato

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

Questi stati non autorizzano C0. Durante la patch:

- non sono stati avviati MT5 o MetaEditor;
- non sono stati letti, creati o usati account, password o bootstrap reali;
- non sono state aperte connessioni esterne;
- non sono stati applicati Firewall/WFP né letti eventi Security;
- actual launch, bootstrap Create/Remove e registry promotion sono rimasti
  `HARD_DISABLED`;
- non sono stati eseguiti C0-C5, commit o push;
- le modifiche sono confinate a `lab/mt5_direct_endpoint/`.

## Limite DEMO

Il profilo corrente accetta soltanto `ACCOUNT_TRADE_MODE=DEMO`. Non può
validare account REAL, inclusi account cliente o finanziati. Qualunque
eventuale estensione a `REAL_READ_ONLY_DEDICATED` richiederebbe una decisione
separata dopo un PoC demo positivo; la Patch 7 non la introduce.

## Contratti Patch 7

| Artefatto | Versione |
|---|---:|
| experiment config | 4 |
| experiment manifest | 2 |
| control plan | 2 |
| direct campaign manifest | 2 |
| candidate handoff | 2 |
| evidence | 6 |
| proof binding | 5 |
| timeline | 1 |
| identity probe schema / probe | 3 / `3.0.0` |
| network summary / sanitized event | 2 / 2 |
| policy | `mt5-direct-endpoint-policy-v2` |

Gli oggetti JSON sono chiusi e strict. UUIDv4, SHA-256, path Windows canonici
e chiavi duplicate sono validati. I commitment autorevoli usano JSON
canonico e separazione di dominio:

```text
SHA-256("MT5_DIRECT_ENDPOINT\0" + artifact_type + "\0" +
        schema_version + "\0" + canonical_json)
```

Le versioni Patch 6 non diventano Patch 7 cambiando un numero: gli artefatti
devono essere rigenerati.

## Grafo autorevole

```text
config v4
  -> experiment manifest v2
       -> piani C0/C1/C2 v2
            -> evidence C2 v6 verificata
                 -> direct campaign manifest v2
                      -> candidate handoff v2
                           -> piani C3/C4/C5 v2
                                -> evidence direct v6
```

Il candidate nasce soltanto dall'evidence C2. Il candidate handoff impegna
`direct_campaign_manifest_sha256`, oltre a C2, candidate, identità,
terminale e lifecycle. Il direct campaign manifest contiene i descriptor
C3/C4/C5 e la policy temporale. Non contiene i digest dei piani C3/C4/C5:
quei piani vengono generati dopo l'handoff, evitando un ciclo di digest.

Il builder dell'handoff accetta oggi soltanto fixture sintetiche. I
riferimenti presenti nell'handoff non provano da soli la coerenza globale
C0→C1→C2: quella viene verificata da `evaluate_campaign()` sull'intera
campagna. La promozione di evidence captured resta hard-disabled.

## Lifecycle C012 scelto

L'ispezione del flusso di produzione conferma
`C012_SINGLE_PROCESS_SESSION`: `prepare_broker()` lascia vivo il terminale
dopo la discovery e il successivo avvio `/config` consegna il bootstrap alla
stessa istanza. La seconda invocazione è un config submitter transiente, non
una nuova root generation.

```text
C0 — LAUNCH_RETAIN
  terminale non avviato
    -> crea Job + root process generation
    -> avvia una volta e misura baseline
    -> ferma la sola cattura
    -> retain_c012_session
                     |
                     v  stesso c012_session_id / Job / root generation
C1 — REUSE_RETAIN
  verifica processo esistente
    -> query negativa committed -> query exact
    -> ferma la sola cattura
    -> retain_c012_session
                     |
                     v  stesso c012_session_id / Job / root generation
C2 — REUSE_CONFIG_SUBMIT_TEARDOWN
  verifica processo esistente
    -> marca C2_LOGIN_START
    -> submit_login_bootstrap_to_existing_terminal
    -> verifica submitter transiente nello stesso Job
    -> login / connected / interruption / reconnect
    -> close_job_object
    -> destroy_disposable_clone_after_export
```

Il contratto fissa:

```text
lifecycle_mode = C012_SINGLE_PROCESS_SESSION
launch_control = C0
teardown_control = C2
root_process_generation_policy = SINGLE_SHARED_C0_C1_C2
allowed_transient_process_policy = C2_CONFIG_SUBMITTER_SAME_JOB_ONLY
```

C0 e C1 non possono chiudere Job o clone. C2 è l'unico teardown di C012.
C3, C4 e C5 restano controlli indipendenti usa-e-getta con una root generation
unica per controllo.

Anche `terminal_command` segue il lifecycle: C0 contiene soltanto
`terminal64.exe /portable`, C1 è `null` perché riusa il processo esistente,
mentre C2 e i controlli direct contengono la command line `/config` che
rappresenta un'invocazione effettiva.

### Limite runtime ancora aperto

Il contratto offline ora è coerente, ma l'attuale JobHarness è un launcher
one-shot: non implementa ancora un coordinatore persistente capace di
mantenere Job e processo tra C0, C1 e C2 e ricevere comandi tramite IPC. Prima
di un dry-run Windows serve quel coordinatore, con protocollo autenticato e
fail-closed, oppure un'estensione equivalente del JobHarness. Per questo
`HARNESS` resta `PARTIALLY_READY` e `WINDOWS RUNTIME` resta `NO-GO`.

## Portable root: una sola semantica

Il campo evidence è:

```text
run_context.portable_root_path_sha256
```

È il digest del path Windows canonico e deve coincidere esattamente con:

```text
control_plan.path_bindings.terminal_data_path_sha256
```

La regola vale per C0-C5, incluso C4 che non ha identity probe. La root è
inoltre impegnata nei binding di Job/process generation, pre-state, probe
quando presente, firewall plan per i direct control e proof binding.
Una divergenza osservata è `FAIL`; una prova necessaria assente è
`INCONCLUSIVE`.

## Pre-state e transizioni

C012 possiede una sola fotografia iniziale, acquisita prima di C0:

```text
C012_INITIAL_PRE_STATE
  -> initial_c012_pre_state_sha256
  -> riferito identico da C0, C1 e C2
```

Il campo `pre_state` contiene lo snapshot completo soltanto in C0. In C1 e C2
deve essere `null`: entrambe le evidence riferiscono il commitment C0 e
registrano una transizione separata.

| Controllo | Broker cache | Account cache |
|---|---|---|
| C0 | `ABSENT` | `ABSENT` |
| C1 | `CREATED_RECORDED` oppure `ABSENT_RECORDED` | `ABSENT_RECORDED` |
| C2 | `INHERITED_RECORDED`, `CREATED_RECORDED` oppure `ABSENT_RECORDED` | `CREATED_RECORDED` oppure `ABSENT_RECORDED` |

`sensitive_material_exported` deve restare `false`. C3/C4/C5 hanno invece
snapshot cold-boot distinti, completi e obbligatoriamente puliti; non
riutilizzano il digest iniziale C012.

La campagna valida anche la continuità C1→C2: una cache creata in C1 deve
risultare ereditata in C2; se è assente in C1 può essere creata oppure restare
assente in C2. Combinazioni impossibili, come “assente” seguito da
“ereditata”, producono `FAIL / c012_state_transition_incompatible`.

## Query C1 e attribuzione

Il manifest genera una label strutturata e non sensibile:

```text
TJ-NO-SUCH-<experiment-UUIDv4>
```

C1 impegna label/digest e risultato atteso zero nel piano. L'evidence deve
legare:

```text
negative_query_label_sha256
negative_query_result_count = 0
negative_query_ui_binding_verified = true
```

La label deve differire dalla requested server label e non può essere
riutilizzata fra experiment ID differenti. Quando il delta C1 proviene da
`PROCESS_SCOPED_TCP_FLOW_SET`, `attribution_unambiguous` deve essere `true`;
altrimenti l'esito è
`INCONCLUSIVE / discovery_flow_attribution_ambiguous`.

## Probe: tempo e source intent

L'identità normalizzata conserva `probe_generated_at_unix`. Per C2, C3 e C5
deve cadere nella finestra autenticata tra login start e connected end, entro
la piccola `probe_timestamp_tolerance_seconds` impegnata nel manifest.
Timestamp stale o futuri che contraddicono la timeline sono `FAIL`; prova
temporale mancante è `INCONCLUSIVE`.

`probe.source_sha256` nel config/manifest è un commitment dell'intento
approvato, non un'attestazione che quel sorgente sia stato compilato ed
eseguito. L'identità normalizzata non dichiara `probe_source_sha256`.
L'attestazione source→binary→deployment è deliberatamente rimandata al futuro
deployment verifier Windows (EX5). `probe_hash_verified` non sostituisce
questa catena.

## Campagna direct e rete

La campagna è strettamente sequenziale:

```text
C3.completed <= C4.started
C4.completed <= C5.started
C5.started - C3.completed >= c5_separation_minimum
```

Le stesse condizioni devono risultare dai marker timeline. Overlap o
inversione producono
`INCONCLUSIVE / direct_controls_temporal_order_invalid`.

Per ogni fase i flow TCP process-scoped sono una partizione:

```text
process_scoped_tcp_flows = candidate_tcp_flows + other_tcp_flows
```

C3/C5 richiedono sola tupla candidate connessa, zero DNS, zero other TCP e
zero non-TCP. C4 richiede il candidate tentato e bloccato, mai connesso, con
gli stessi zeri. Il deny esterno resta
`DEFENSE_IN_DEPTH_NON_PROBATORY`: è difesa aggiuntiva, non prova primaria.

## Esiti

| Percorso | Esito massimo corrente | Exit CLI |
|---|---|---:|
| fixture, comando ordinario | `INCONCLUSIVE` | 2 |
| fixture, comando test-only | `SYNTHETIC_PASS` | 3 |
| `CAPTURED_EXPORT` corrente | `INCONCLUSIVE` | 2 |
| futuro verifier indipendente | `PASS` non implementato | n/d |

`SYNTHETIC_PASS` verifica soltanto la logica offline. Il normale `PASS` resta
intenzionalmente irraggiungibile; gli exporter captured dichiarano `NO_GO` e
`proof_capable=false`.

## Verifiche offline

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

Questi comandi non autorizzano test Windows runtime, rete, firewall, WFP,
bootstrap o MT5.

## Packaging

Il pacchetto di revisione deve essere creato dalla root del repository e
conservare il prefisso:

```text
lab/mt5_direct_endpoint/
```

Non deve contenere cache, bytecode, ZIP annidati, artefatti ETW/WFP/packet
capture, `.ex5`, `.DS_Store`, segreti o directory `private`, `raw` e
`sanitized` prodotte dai run. Il packager usa un allowlist dei tipi sorgente,
verifica i membri obbligatori e l'integrità, quindi pubblica lo ZIP in modo
atomico senza sovrascrivere una destinazione esistente. Symlink e nomi
ambigui durante l'estrazione Windows sono rifiutati.

La procedura operativa è in [RUNBOOK.md](RUNBOOK.md); evidenze, finding e
limiti della Patch 7 sono in [IMPLEMENTATION_REPORT.md](IMPLEMENTATION_REPORT.md).
