# JobHarness

`JobHarness` è il launcher Windows isolato del laboratorio `mt5_direct_endpoint`.
Non contiene logica del Broker Resolver, non modifica il firewall e non cerca né
avvia MetaTrader implicitamente.

Il suo unico compito corrente è validare un target esplicito senza avviarlo.
`--execute` esiste come contratto positivo, ma la capability actual è
`HARD_DISABLED`: sul percorso Windows termina con
`actual_launch_runtime_validation_required`, mentre fuori Windows viene
rifiutata prima come `PLATFORM_UNSUPPORTED`. La sequenza seguente descrive codice
dormiente da sottoporre a una futura review Windows, non una capability
autorizzata:

```text
parsing fail-closed (`--execute` opt-in; conflitto con `--dry-run`)
  → apertura target read-only senza share-write/share-delete
  → SHA-256 osservato confrontato in fixed time con `--expected-sha256`
  → per terminal.exe/terminal64.exe: conferma + WinVerifyTrust + signer allowlisted
  → persistenza metadata VALIDATED
  → CreateJobObjectW (job anonimo)
  → JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
  → verifica BREAKAWAY_OK=false e SILENT_BREAKAWAY_OK=false
  → CreateProcessW(CREATE_SUSPENDED, bInheritHandles=false)
  → AssignProcessToJobObject
  → persistenza evidenza ASSIGNED_SUSPENDED
  → armamento watchdog e ResumeThread nello stesso lifecycle gate
  → attesa di tutti i processi nel Job Object
  → chiusura del job (fail-closed)
```

Se assegnazione, verifica della policy o persistenza dell’evidenza falliscono, il
thread primario non viene eseguito. Alla chiusura del launcher tutti i processi
rimasti nel job vengono terminati.

## Requisiti

- Windows 10/11 o Windows Server moderno per l’esecuzione reale;
- SDK .NET 8 per build e test;
- nessun pacchetto NuGet esterno;
- directory dell’evidenza già esistente, locale, senza reparse point nella sua
  gerarchia e protetta con ACL del laboratorio.

Il progetto può essere compilato su altri sistemi. Nello snapshot corrente non
supera mai il confine di launch su alcuna piattaforma: accetta
`--help`/`--dry-run`; fuori Windows registra un actual launch come
`PLATFORM_UNSUPPORTED`, mentre su Windows il gate globale lo rifiuta prima delle
API Job/process.

## Contratto default-no-execute

- senza `--execute` nessun processo viene creato;
- `--execute` e `--dry-run` sono mutuamente esclusivi;
- una richiesta senza entrambi viene rifiutata con categoria
  `execute_not_requested` (se `--metadata` è presente, il rifiuto è persistito);
- ogni actual launch richiede un nuovo file `--metadata` e
  `--expected-sha256 <64-hex>`;
- il confronto del digest avviene mentre il lease read-only resta aperto, con
  confronto constant-time dei 32 byte;
- un target chiamato `terminal.exe` o `terminal64.exe` richiederebbe anche
  `--confirm-mt5-launch`, una firma Authenticode valida e un subject o thumbprint
  esplicitamente allowlisted;
- dopo questi controlli ogni actual launch viene comunque rifiutato prima di
  `CreateJobObjectW`/`CreateProcessW`, inclusi target innocui;
- il gate restera chiuso finche canonical path/file identity, immagine sospesa,
  Authenticode provider/signer e puro `KILL_ON_JOB_CLOSE` non saranno verificati
  su Windows.

## Build

```powershell
dotnet build .\JobHarness.csproj --configuration Release
```

Pubblicazione framework-dependent per il worker Windows x64:

```powershell
dotnet publish .\JobHarness.csproj `
  --configuration Release `
  --runtime win-x64 `
  --self-contained false
```

## Dry-run sicuro

Il dry-run calcola hash e metadati, ma non invoca alcuna API di creazione processo:

```powershell
$jobHarness = '.\bin\Release\net8.0\JobHarness.exe'
$metadata = 'C:\TJLab\evidence\jobharness-dry-run.json'

& $jobHarness run `
  --executable "$env:SystemRoot\System32\cmd.exe" `
  --metadata $metadata `
  --phase DRY `
  --dry-run `
  --arg /d `
  --arg /c `
  --arg exit `
  --arg 0
```

Il file di destinazione non deve esistere: il launcher non sovrascrive mai
un’evidenza appartenente a una precedente esecuzione.

## Test offline e smoke Windows non ancora autorizzato

Senza switch, il test runner verifica parser, escaping, secret guard,
lease/hash, metadata e gate con un launch boundary iniettato che non crea
processi:

```powershell
.\scripts\Test-JobHarness.ps1
```

`-RunWindowsProcessSmoke` oggi verifica soltanto che anche su Windows il gate
hard-disabled impedisca la creazione del processo. Non dimostra Job Object,
discendenti o kill-on-close e non va interpretato come smoke runtime riuscito:

```powershell
.\scripts\Test-JobHarness.ps1 -RunWindowsProcessSmoke
```

Lo script ignora un valore live già ereditato se lo switch non è presente e
ripristina l'ambiente precedente in `finally`.

## Uso futuro con MT5

Non usare questo esempio nella fase di preparazione corrente. È documentato solo
come contratto del futuro orchestratore C0-C5:

```powershell
& $jobHarness run `
  --executable 'C:\TJLab\RUN-ID\C3\terminal\terminal64.exe' `
  --working-directory 'C:\TJLab\RUN-ID\C3\terminal' `
  --metadata 'C:\TJLab\RUN-ID\evidence\C3-job.json' `
  --expected-sha256 '<SHA256-64-HEX>' `
  --run-id '11111111-1111-4111-8111-111111111111' `
  --phase C3 `
  --timeout-seconds 900 `
  --execute `
  --confirm-mt5-launch `
  --allowed-signer-thumbprint '<CERTIFICATE-SHA256-THUMBPRINT>' `
  --arg /portable `
  --arg '/config:C:\TJLab\RUN-ID\secrets\bootstrap.ini'
```

L'esempio non contiene valori reali e non va eseguito nella fase di preparazione;
il codice corrente lo rifiuta comunque prima della creazione del processo.
Il target deve sempre essere un path assoluto: non esistono ricerca automatica,
default MT5 o discovery del broker.

La verifica Authenticode attuale usa `WinVerifyTrust` senza UI, con chain
revocation cache-only e nessun recupero URL, ma l'estrazione del certificato non
e ancora legata alla stessa provider state/firma accettata da WinVerifyTrust.
Il risultato e quindi preliminare e `launch_approved=false`; il launch MT5 resta
sempre chiuso. Il thumbprint SHA-256
del certificato è preferibile; sono accettati anche thumbprint SHA-1 da 40 hex per
compatibilità operativa. Il subject, se usato, deve corrispondere esattamente al
distinguished name (case-insensitive): non si fanno match parziali.

## Credenziali

Il launcher non è un secret transport.

- Non passare account, password, token o secret con `--arg`.
- Non inserirli in path, nome del file metadata, working directory o variabili
  d’ambiente.
- Il secret guard rifiuta switch evidenti come `/login:`, `--password=` e
  `token=`, ma non può riconoscere ogni valore posizionale: la regola operativa
  resta obbligatoria.
- Per l’esperimento futuro usare esclusivamente il bootstrap file protetto e
  cancellabile previsto dal runbook generale.
- Gli argomenti, il working directory e i valori dell’ambiente non vengono mai
  scritti nel JSON.
- Il figlio riceve sempre e soltanto l’allowlist fissa di variabili Windows. Non
  esiste alcun opt-out per ereditare l’ambiente completo del launcher.

Anche se il JSON non registra gli argomenti, strumenti come Sysmon o ETW possono
registrare la command line. Per questo l’assenza di credenziali dalla command line
è una proprietà del protocollo, non una semplice redazione a posteriori.

## Evidenza JSON

Schema: `jobharness.process-metadata.v2`.

Contiene:

- `run_id`, fase e timestamp UTC;
- path assoluto normalizzato lessicalmente, nome, dimensione, mtime, SHA-256
  atteso/osservato e risultato del confronto;
- dichiarazioni esplicite `canonical_path_verified=false` e
  `file_identity_verified=false` finché la verifica handle-based Windows non
  viene implementata;
- SID utente; root PID, TID, Windows Session ID e kernel creation time restano
  `null` perché il gate globale precede ogni creazione processo;
- job ID opaco e flag espliciti `execute_requested`/`dry_run_requested`;
- firma presente/valida, modalità WinVerifyTrust, subject/thumbprint sanitizzati,
  match allowlist e risultato preliminare per target MT5-like, con
  `provider_signer_binding_verified=false`,
  `windows_runtime_validation_complete=false` e `launch_approved=false`;
- campi schema per `created_suspended`, `assigned_before_resume`, policy Job,
  exit code e contatori: nella policy corrente restano non attestati/non
  popolati perché il codice runtime è irraggiungibile;
- dichiarazioni esplicite sui campi omessi.

Non contiene:

- valori degli argomenti;
- command line completa;
- valori dell’ambiente;
- working directory;
- stdout/stderr del target;
- account, password o token.

Ogni aggiornamento usa un file temporaneo nella stessa directory, flush su disco
e rename atomico. Il percorso dormiente prevede di scrivere
`ASSIGNED_SUSPENDED` prima di `ResumeThread`; non è una proprietà runtime
verificata. Su Windows i controlli preliminari rifiutano UNC/device path, volumi
non locali e directory in cui il controllo lessicale individua reparse point.

L’handle read-only usato per calcolare SHA-256 resta aperto, senza condivisione di
scrittura o cancellazione, durante i controlli preliminari. Questo lease non
dimostra da solo l'identità finale del file attraverso alias/reparse point e non
lega l'immagine sospesa all'handle hashato. Il blocker è il motivo per cui ogni
actual launch è hard-disabled.

Il codice dormiente contiene un timer one-shot e una gestione timeout del Job,
ma non è stato compilato o esercitato su Windows in questa fase. In particolare,
il comportamento puro `KILL_ON_JOB_CLOSE` non è ancora provato.

## Exit code

| Codice | Significato |
|---:|---|
| `0` | help o dry-run riuscito |
| `2` | command line non valida o gate di policy rifiutato, incluso il gate globale actual-launch |
| `124` | riservato al futuro timeout Job; percorso oggi irraggiungibile |
| `125` | errore del launcher/API/evidenza |
| `130` | riservato alla futura interruzione del Job; percorso oggi irraggiungibile |
| altro | riservato al futuro exit code del processo root |

## Limiti dichiarati

- L'actual launch è interamente `HARD_DISABLED`; pertanto Job containment,
  assegnazione prima del resume, timeout, discendenti e kill-on-close non sono
  ancora proprietà runtime dimostrate.
- Manca una canonical file identity ottenuta dall'handle e confrontata con
  l'immagine realmente creata; i controlli correnti su path/reparse point non
  chiudono tutte le varianti di alias e TOCTOU.
- `WinVerifyTrust` e l'estrazione separata del certificato sono controlli
  preliminari: il signer allowlisted non è ancora dimostrato come quello della
  firma/provider state accettata.
- Il riconoscimento MT5-like per nome file non è una security boundary; una
  rinomina potrebbe evitarlo. Il gate globale, indipendente dal nome, impedisce
  comunque ogni launch nello snapshot corrente.
- Il secret guard è euristico e non riconosce ogni valore posizionale; la CLI
  non deve mai essere usata come secret transport.
- Anche dopo una futura riabilitazione, un Job Object non sostituirà ETW/WFP per
  attribuire i flow di rete, e codice esterno con privilegi sufficienti resterà
  fuori dal modello del laboratorio.
- Il launcher non cattura output del target per evitare raccolta accidentale di
  dati sensibili.
