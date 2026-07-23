# Strumentazione Windows del laboratorio MT5 direct endpoint

Questa directory contiene soltanto strumenti isolati per preparare e acquisire i
controlli C0-C5. Non modifica il Broker Resolver, non avvia MetaTrader, non usa
credenziali e non contiene endpoint reali.

Durante questa fase non eseguire le modalita attive. I comandi mutanti sono
destinati esclusivamente a una futura VM disposable autorizzata.

## Garanzie di sicurezza

- WPR e i marker sono `PlanOnly` per default.
- `Start` e `Stop` WPR richiedono sia `-Execute` sia un token letterale e passano
  comunque da `ShouldProcess`; `-WhatIf` impedisce l'esecuzione.
- il planner Firewall/WFP **non implementa alcuna funzione di apply**: produce
  solamente un oggetto o file JSON;
- l'executor separato e `PlanOnly` per default; `VerifyOnly` e disponibile,
  mentre `Apply` e `Rollback` sono hard-disabled finche non esiste un verifier
  esterno di autorizzazioni firmate e di una allowlist dei clone;
- il validatore endpoint e offline: non risolve DNS, non apre socket e richiede
  una porta esplicitamente approvata;
- l'export ETL e offline, usa una whitelist di campi e rimuove sempre i dump XML
  intermedi che potrebbero contenere payload non filtrati;
- nessuno script installa Sysmon, cambia il firewall per default o esegue login;
- output esistenti e directory reparse-point sono rifiutati.

## Componenti

| File | Scopo |
|---|---|
| `MT5DirectEndpoint.Lab.psm1` | parsing e validazione offline di IPv4, IPv6 e DNS con RRset fornito dall'operatore |
| `Test-LabEndpoint.ps1` | interfaccia CLI del validatore endpoint |
| `Test-LabPrerequisites.ps1` | piano o controlli locali esclusivamente read-only |
| `Get-LabPreState.ps1` | scanner pre-stato read-only con report sanitizzato e schema versionato |
| `Invoke-LabWprCapture.ps1` | piano, validazione profilo, status, start e stop WPR con doppi gate |
| `Write-LabPhaseMarker.ps1` | marker WPR e JSONL per attribuire gli eventi alle fasi C0-C5 |
| `Export-LabEtwEvidence.ps1` | conversione ETL offline e normalizzazione JSONL sanitizzata |
| `Export-LabWfpSecurityEvidence.ps1` | contratto PlanOnly dell'export sanitizzato Security/WFP; `Execute` hard-disabled finche mancano i binding forti |
| `New-LabFirewallPlan.ps1` | piano JSON di audit WFP e isolamento Defender Firewall per C3/C4/C5 |
| `Invoke-LabNetworkIsolation.ps1` | executor futuro fail-closed; oggi solo `PlanOnly`/`VerifyOnly`, con mutazioni hard-disabled |
| `Invoke-LabPrivateBootstrap.ps1` | contratto PlanOnly senza parametri secret; `Create`/`Remove` hard-disabled prima di accessi o scritture |
| `tests/Invoke-DryRunSelfTest.ps1` | self-test senza rete e senza mutazioni |
| `../profiles/mt5-network.wprp` | profilo WPR process/image, kernel TCP/IP e DNS |

## Validazione endpoint

La validazione non effettua risoluzioni DNS. Un hostname e `SAFE` soltanto se il
chiamante fornisce l'intero RRset osservato e ogni indirizzo e pubblico.

```powershell
.\Test-LabEndpoint.ps1 -Endpoint '203.0.113.10:443' -AllowedPort 443 -AsJson

.\Test-LabEndpoint.ps1 `
  -Endpoint 'one.one.one.one:443' `
  -AllowedPort 443 `
  -ResolvedAddress @('93.184.216.34') `
  -AsJson
```

Il primo esempio usa intenzionalmente una rete di documentazione e deve essere
`UNSAFE`; serve a verificare che il fail-closed sia attivo. Nel vero piano si usa
esclusivamente il candidate autorizzato, mai un valore copiato da questo README.

Sono rifiutati loopback, RFC1918, CGNAT, link-local, multicast, documentation,
benchmark, special-purpose, IPv4-mapped IPv6, ULA, zone ID e nomi DNS locali o
riservati. Una porta valida ma non inclusa in `AllowedPort` non e eleggibile.

## Prerequisiti

Il default stampa soltanto il piano:

```powershell
.\Test-LabPrerequisites.ps1
```

Nella futura VM Windows, le sole ispezioni read-only si attivano esplicitamente:

```powershell
.\Test-LabPrerequisites.ps1 `
  -Mode ReadOnlyChecks `
  -TerminalPath 'C:\TJLab\sealed\terminal64.exe' `
  -EvidenceRoot 'C:\TJLab\evidence'
```

Il report controlla Windows/admin, strumenti, servizi, firma/hash del binario,
assenza di `terminal64.exe`, profilo WPR e reparse point. Non crea directory.

## WPR e fasi

Piano innocuo, eseguibile anche senza Windows:

```powershell
.\Invoke-LabWprCapture.ps1 -RunId C0_run001
```

Prima del vero esperimento validare il profilo nella VM:

```powershell
.\Invoke-LabWprCapture.ps1 `
  -Action ValidateProfile `
  -RunId C0_run001 `
  -Execute
```

Le azioni `Start`/`Stop` non vanno eseguite in questa fase. In futuro richiedono:

```text
-Execute
-AuthorizationToken START_ETW_ON_DISPOSABLE_VM
-Confirm oppure -Confirm:$false deciso consapevolmente dall'operatore
console elevata
EvidenceDirectory gia esistente e non reparse-point
```

Il profilo registra globalmente gli eventi kernel; l'attribuzione non deve mai
usare il PID generico nell'header TCP/IP. L'export conserva separatamente
`header_process_id` e `payload_process_id`.

Le fasi supportate sono:

```text
C0_BASELINE
C1_DISCOVERY_NEGATIVE
C1_DISCOVERY_EXACT
C2_LOGIN
C2_CONNECTED
C2_NETWORK_INTERRUPTION
C2_RECONNECT
C3_DIRECT_LOGIN
C3_CONNECTED_STEADY
C4_ENDPOINT_BLOCKED
C5_DIRECT_LOGIN
C5_CONNECTED_STEADY
TEARDOWN
```

Questa e la timeline contrattuale Patch 6. Ogni fase richiede `START` e `END`;
per ogni controllo l'ordine deve essere esatto e la sequenza numerica deve
essere contigua. Il marker v2 verifica lo state file WPR del medesimo `RunId`,
scrive un marker ETW strutturato e aggiunge a un JSONL locale
`code`, `sequence`, tempo Unix in millisecondi, QPC e frequenza QPC. Il default
e soltanto un piano:

```powershell
.\Write-LabPhaseMarker.ps1 `
  -RunId C0_run001 `
  -Phase C0_BASELINE `
  -Boundary START
```

## Piano Firewall/WFP

Il planner non dispone di un parametro `Apply`. Verifica staticamente:

- IP letterale pubblico e porta esplicitamente approvata;
- path canonici sotto `C:\TJLab` per terminale, evidenze e piano scritto;
- path diretto a `terminal64.exe`, senza traversal o separatori duplicati;
- SHA-256 atteso;
- differenza causale fra C3/C5 (una sola allow candidate) e C4 (nessuna allow).

```powershell
.\New-LabFirewallPlan.ps1 `
  -Control C3 `
  -RunId C3_run001 `
  -TerminalPath 'C:\TJLab\C3_run001\terminal\terminal64.exe' `
  -TerminalSha256 '<64_HEX>' `
  -Endpoint '<IP_PUBBLICO>:<PORTA>' `
  -ApprovedPort <PORTA> `
  -OutputPath 'C:\TJLab\plans\C3_run001.firewall-plan.json' `
  -WhatIf
```

Il piano futuro include backup, audit WFP 5152/5153/5156/5157, disabilitazione
degli allow outbound preesistenti, default-deny, eventuale allow per path/IP/porta
e rollback. Queste operazioni sono invasive e sono ammesse soltanto nel clone.

La decisione Patch 6 per il secondo deny e
`DEFENSE_IN_DEPTH_NON_PROBATORY`: un default-deny aggiuntivo su gateway o
hypervisor e raccomandato come difesa operativa, ma non e un gate probatorio e
la sua presenza o assenza non determina il verdetto C0-C5. La prova deve
derivare dai binding, dalla timeline e dall'accounting process-scoped previsti
dal contratto del laboratorio.

### Executor di isolamento futuro

L'executor non esegue mai i campi `tool` o `arguments` del JSON. Valida uno
schema chiuso, ricalcola endpoint/path/porta e costruisce internamente il set
minimo di comandi. Il default seguente legge soltanto il piano e il suo digest:

```powershell
$planHash = (Get-FileHash 'C:\TJLab\plans\C3_run001.firewall-plan.json' -Algorithm SHA256).Hash
.\Invoke-LabNetworkIsolation.ps1 `
  -PlanPath 'C:\TJLab\plans\C3_run001.firewall-plan.json' `
  -PlanSha256 $planHash
```

Non eseguire le modalita attive durante la preparazione. `Apply` e `Rollback`
terminano oggi con `CAPABILITY_HARD_DISABLED` prima di ogni controllo Windows e
prima di qualunque mutazione. Digest, token e attestazioni self-asserted
proteggono da errori accidentali, ma non autenticano che la macchina corrente
sia davvero il clone autorizzato. Prima di rimuovere questo blocco serve un
verifier esterno che controlli una autorizzazione firmata, non riutilizzabile e
legata almeno a MachineGuid, immagine/clone, run, controllo, scadenza, piano e
policy locale. Un eventuale guard indipendente resta una difesa aggiuntiva:
non e una fonte probatoria e non puo essere promosso a gate del verdetto.

E inoltre un blocker il parser JSON: prima di riabilitare mutazioni deve
rifiutare chiavi duplicate e tipi primitivi non esatti (`"1"` non equivale a
`1`, `1` non equivale a `true`). Il digest rende immutabili i byte, ma non elimina
ambiguita semantiche fra parser differenti.

`VerifyOnly` rimane disponibile per diagnostica locale, ma il suo output forza
`readiness=NO_GO`, `proof_capable=false`,
`external_authorization_verified=false`, `evidence_eligible=false` e
`firewall_policy_verified=false`. Il campo annidato `verification.verified`
significa soltanto che l'ActiveStore osservato coincide con il piano fornito:
sentinel e guard sono ancora self-asserted, quindi quel booleano non puo essere
copiato nell'evidenza C3-C5.

Il codice futuro di `Apply`, oltre a tale verifier ancora mancante, richiede
cumulativamente:

- Windows 64 bit elevato, host non domain-joined e non via RDP;
- console out-of-band confermata e `ShouldProcess`;
- piano e sentinel del clone con digest esatti;
- binding a MachineGuid/run/control, scadenza massima 24 ore e token
  `APPLY_MT5LAB_<run>_<control>`;
- `terminal64.exe` sotto `C:\TJLab`, SHA-256 esatto, firma MetaQuotes valida e
  processo non ancora avviato;
- eventuale guard indipendente coerente con C3/C4/C5, trattato esclusivamente
  come `DEFENSE_IN_DEPTH_NON_PROBATORY`.

La verifica post-apply prevista controlla l'ActiveStore e legge il bitmask non
localizzato di `AuditQuerySystemPolicy`: entrambe le sottocategorie WFP devono
avere Success e Failure abilitati. Un fallimento dopo la prima mutazione lascia
il clone fail-closed: non viene eseguito rollback automatico. Il `Rollback`
previsto e una capability di recovery secondaria, accettata soltanto con NIC down, guard
esterno `DENY_ALL`, backup/state con digest corrispondenti e successiva
distruzione del clone.

## Export e normalizzazione offline

Il default mostra il piano e non richiede che l'ETL esista:

```powershell
.\Export-LabEtwEvidence.ps1 `
  -RunId C0_run001 `
  -InputEtlPath 'C:\TJLab\evidence\C0_run001.etl' `
  -OutputDirectory 'C:\TJLab\evidence'
```

La futura esecuzione richiede il token `EXPORT_ETW_OFFLINE`, un marker log e i
PID appartenenti al Job Object. Richiede inoltre lo state file `wpr-stop` dello
stesso run, con marker finale riuscito e SHA-256 coincidente. Produce:

```text
<run>.events.sanitized.jsonl
<run>.export-manifest.json
```

Il manifest e intenzionalmente `readiness=NO_GO`, `proof_capable=false`,
`exploratory_dataset_only=true` e `ready_for_analysis=false`. Il campo
`capture_integrity_precheck` diventa true soltanto se:

- `EventsLost + BuffersLost == 0` nel summary `tracerpt`;
- tutte le fasi hanno START/END;
- sono stati forniti i PID target.

Se il contatore loss non e ricavabile (anche per localizzazione del sistema), lo
stato resta `UNKNOWN`. Anche con precheck positivo, il dataset non puo alimentare
un PASS: mancano digest/schema strict del marker e binding al manifest JobHarness
con PID, kernel creation time, sessione, path e hash. Un bare PID e riciclabile e
non costituisce attribuzione process-scoped.

L'ETL originale puo contenere dati sensibili e deve risiedere su volume cifrato.
Il JSONL esclude command line, environment, credenziali, account, saldi, ordini e
campi payload non esplicitamente autorizzati.

Eventi e manifest sono scritti con `CreateNew` e UTF-8 senza BOM. La modalita
`Execute` puo essere usata in futuro soltanto per esplorazione offline; finche i
binding mancanti non sono implementati, nessun suo output e evidence-eligible.

## Evidenza Security/WFP sanitizzata

WPR/ETW e il log firewall non sostituiscono gli eventi del registro Security.
Il nuovo exporter e `PlanOnly` per default e non chiama `Get-WinEvent`, non
esporta `Security.evtx` e non persiste XML grezzo:

```powershell
.\Export-LabWfpSecurityEvidence.ps1 `
  -RunId C4_run001 `
  -Control C4 `
  -MarkerLogPath 'C:\TJLab\evidence\phase-markers.jsonl' `
  -MarkerLogSha256 '<64_HEX>' `
  -IsolationAppliedStatePath 'C:\TJLab\evidence\C4_run001.network-isolation-applied.json' `
  -IsolationAppliedStateSha256 '<64_HEX>' `
  -TerminalPath 'C:\TJLab\C4_run001\terminal\terminal64.exe' `
  -TerminalSha256 '<64_HEX>' `
  -TargetProcessId <PID_JOB> `
  -Endpoint '<IP_PUBBLICO>:<PORTA>' `
  -ApprovedPort <PORTA> `
  -OutputDirectory 'C:\TJLab\evidence'
```

La modalita `Execute` e attualmente **hard-disabled** e termina prima di leggere
Security o scrivere file. Il codice preparatorio prevede `EventLogReader` in
sola lettura, dopo la fine della fase, e una whitelist di timestamp,
record/event ID, PID,
digest/match del path, codice direzione/protocollo e tuple IP/porta. Gli eventi
5156/5157 sono esportati soltanto se path o PID appartengono al target; 5152/5153,
che possono non avere identita processo, restano sola corroborazione legata
alla destinazione candidate. Il contratto target, non ancora abilitato, dovra
vincolare:

- la finestra esatta START/END del run e il digest del marker log;
- PID del Job Object, path/hash/firma del terminale e candidate IP/porta;
- lo state fail-closed applicato prima di START e il suo digest;
- audit WFP Success+Failure e copertura temporale del registro Security;
- conteggi esatti, fallback verso destinazioni inattese e hash del JSONL.

Per C4, un 5157 con `PATH_AND_PID` e destinazione candidate dimostra un tentativo
di connessione bloccato. I 5152/5153 lo corroborano; la loro assenza non lo
falsifica. Un 5156 dimostra soltanto che WFP ha consentito una connessione: non
dimostra mai login o identita. Se l'evento richiesto manca, audit/log coverage
non sono provati o l'attribuzione e parziale, l'esito resta `INCONCLUSIVE`.
Login e identita devono provenire separatamente dal probe MQL5 sanitizzato.

Questo e quindi un gap C4 esplicito, non una prova parziale promossa a PASS.
Prima di abilitare `Execute` bisogna aggiungere e testare su Windows:

- digest e schema chiuso del manifest JobHarness, con PID e kernel creation time
  che coprano l'intera finestra (un PID nudo e insufficiente);
- binding a macchina corrente, piano/digest, endpoint/porta, terminale/hash,
  sentinel e policy locale dell'applied-state; un eventuale guard esterno resta
  defense-in-depth non probatorio;
- verifica ActiveStore e audit success+failure anche dopo END;
- filtro obbligatorio `Protocol=6` e direzione outbound (`%%14593`) prima di
  classificare 5156/5157;
- boundary Security attestati esattamente a START e END. Timestamp del record
  piu vecchio/nuovo dimostrano soltanto la retention envelope e non provano che
  auditing/policy siano rimasti attivi per tutta la finestra;
- parser JSON a schema chiuso con rifiuto di chiavi duplicate e tipi primitivi
  non esatti per manifest Job, piano, marker e applied-state.

Fino a quel momento la completezza Security/WFP di C3-C5 e
`INCONCLUSIVE_BY_DESIGN`.

## Contratto bootstrap privato

Il contratto accetta soltanto identita e path, mai secret. Questo comando e un
dry-run puro e deve restituire `NO_GO`:

```powershell
.\Invoke-LabPrivateBootstrap.ps1 `
  -ExperimentId '11111111-1111-4111-8111-111111111111' `
  -RunId '22222222-2222-4222-8222-222222222222' `
  -Control C3 `
  -PrivateDirectory 'C:\TJLab\11111111-1111-4111-8111-111111111111\runs\22222222-2222-4222-8222-222222222222\C3\private' `
  -PortableRoot 'C:\TJLab\11111111-1111-4111-8111-111111111111\C3\terminal'
```

`Create` e `Remove` terminano con `CAPABILITY_HARD_DISABLED`; non esiste un
parametro per account o password e non viene toccato il filesystem.

## Self-test dry-run

Su un host con PowerShell 5.1+:

```powershell
.\tests\Invoke-DryRunSelfTest.ps1
```

Il test usa soltanto valori di esempio, non apre rete, non crea file, non avvia
WPR e non modifica firewall/audit.

## Limiti noti e gate prima del GO

- Il profilo WPR deve superare `wpr -profiles <file>` sulla golden image Windows;
  non puo essere validato su macOS.
- Il cap circolare protegge il disco, ma se viene raggiunto il test e
  `INCONCLUSIVE` per possibile perdita degli eventi iniziali.
- Sysmon rimane una corroborazione separata: questi strumenti non lo installano.
- Il planner firewall non sostituisce revisione operatore, snapshot e guard
  esterno.
- Le modalita attive dell'executor e dell'export Security/WFP non sono state
  eseguite ne validate su questo host non-Windows; richiedono una golden image
  Windows disposable prima del GO.
- `Apply`/`Rollback` restano un blocker intenzionale, non una capability pronta:
  non vanno riabilitati senza verifier esterno firmato e test Windows negativi
  su wrong-host, replay, scadenza, digest mismatch e recovery fail-closed.
- PID senza creation time/ProcessGuid/Job membership non basta per una prova
  process-scoped; tali evidenze devono provenire anche dal launcher Job Object.
- `Invoke-LabPrivateBootstrap.ps1` esiste soltanto come contratto PlanOnly e non
  accetta account, password, `PSCredential` o `SecureString`: `Create` e `Remove`
  sono hard-disabled prima di qualunque accesso credenziale o filesystem. C2-C5
  restano quindi non eseguibili finche ACL/lifetime, writer plaintext a residuo
  minimo, cleanup handle/file-ID anti-reparse e distruzione clone non vengono
  implementati, revisionati e validati end-to-end su Windows.

Riferimenti ufficiali:

- [WPR command-line options](https://learn.microsoft.com/windows-hardware/test/wpt/wpr-command-line-options)
- [WPR profile schema](https://learn.microsoft.com/windows-hardware/test/wpt/wprcontrolprofiles-schema)
- [ETW TCP/IP](https://learn.microsoft.com/windows/win32/etw/tcpip)
- [WFP auditing and logging](https://learn.microsoft.com/windows/win32/fwp/auditing-and-logging)
