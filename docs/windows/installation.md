# Installazione Windows Agent

Tutti i comandi, salvo il login interattivo esplicitamente indicato, vanno eseguiti da Windows
PowerShell 5.1 come amministratore. Usare Windows Server x64 con Python 3.12 x64, Git for Windows,
VC++ Redistributable x64 e MetaTrader 5 provenienti dalle fonti ufficiali. Non disabilitare Defender
o il firewall e non aprire porte inbound per l'Agent.

## Checkout e runtime Python

Il checkout operativo è `C:\TradeJournal\releases\pool71a`; la sua `.venv` è il runtime stabile
usato per installare il servizio e costruire le release immutabili. Sostituire URL e SHA con valori
reali; lo SHA deve avere esattamente 40 caratteri esadecimali minuscoli.

```powershell
$sourceRoot = 'C:\TradeJournal\releases\pool71a'
$pythonExe = Join-Path $sourceRoot '.venv\Scripts\python.exe'
$revision = '<40-char-commit-sha>'

New-Item -ItemType Directory -Force 'C:\TradeJournal\releases' | Out-Null
git clone <repository-url> $sourceRoot
Set-Location $sourceRoot
git fetch --prune origin
git checkout --detach $revision
if ((git rev-parse HEAD).Trim() -cne $revision) { throw 'Unexpected checkout revision.' }
if (@(git status --porcelain=v1 --untracked-files=all).Count -ne 0) {
  throw 'The deployment checkout is not clean.'
}

powershell -ExecutionPolicy Bypass -File scripts\windows\bootstrap-server.ps1
& 'C:\Program Files\Python312\python.exe' -m venv (Join-Path $sourceRoot '.venv')
& $pythonExe -m pip install -r (Join-Path $sourceRoot 'requirements-windows.txt')
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
& $pythonExe -m pip check
if ($LASTEXITCODE -ne 0) { throw 'The deployment environment is inconsistent.' }
```

`requirements-windows.txt` include `cryptography`, `tzdata==2026.3` e le dipendenze opzionali di
onboarding. La deploy verifica la versione e le regole inverno/estate di `Europe/Rome`, ma non
modifica mai la `.venv`: ogni aggiornamento delle dipendenze è un'operazione separata e
controllata. Il pacchetto Python `MetaTrader5` non è richiesto dal runtime file-bridge e non deve
essere usato come smoke test.

## MT5 e golden template

Installare MT5 e creare una sola volta il template senza account salvati. Gli script verificano la
firma Authenticode dell'installer e compilano l'EA read-only senza warning.

```powershell
Set-Location $sourceRoot
powershell -ExecutionPolicy Bypass -File scripts\windows\install-mt5.ps1
powershell -ExecutionPolicy Bypass -File scripts\windows\prepare-mt5-template.ps1

Test-Path 'C:\TradeJournal\mt5-template\terminal64.exe'
Get-Content 'C:\TradeJournal\logs\mt5-template-result.json'
```

Non copiare `accounts.dat`, `accounts.ini`, log, cache `Tester` o cache storiche `Bases` nel
template. Il binario golden atteso è
`C:\TradeJournal\mt5-template\MQL5\Experts\TradeJournal\TradeJournalBridge.ex5`.

## Identità desktop dedicata

MT5 deve essere eseguito nell'unica identità locale `TradeJournalMT5`. Crearla come utente standard
con password robusta e aggiungerla soltanto al gruppo Remote Desktop Users. I gruppi vengono
individuati tramite SID, così i comandi funzionano anche su Windows non in inglese.

```powershell
$interactiveUser = 'TradeJournalMT5'
$interactivePassword = Read-Host 'Password for TradeJournalMT5' -AsSecureString
try {
  New-LocalUser -Name $interactiveUser -Password $interactivePassword `
    -AccountNeverExpires -PasswordNeverExpires -UserMayNotChangePassword
} finally {
  $interactivePassword.Dispose()
}
$rdpGroup = Get-LocalGroup -SID 'S-1-5-32-555'
Add-LocalGroupMember -Group $rdpGroup.Name -Member $interactiveUser
$createdUser = Get-LocalUser -Name $interactiveUser
$administrators = Get-LocalGroup -SID 'S-1-5-32-544'
if (@(Get-LocalGroupMember -Group $administrators.Name | Where-Object {
  [string]$_.SID -eq [string]$createdUser.SID
}).Count -ne 0) {
  throw 'TradeJournalMT5 must not be an administrator.'
}
$uacPolicy = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System'
New-ItemProperty -LiteralPath $uacPolicy -Name ConsentPromptBehaviorUser `
  -PropertyType DWord -Value 0 -Force | Out-Null
```

Accedere una volta via RDP come `TradeJournalMT5`, quindi disconnettere la sessione senza eseguire
Sign out. Una sessione `Active` o `Disconnected` deve restare disponibile; l'Agent assegna a runtime
ACL minime alle sole directory terminale e crea task interattivi con livello `LIMITED`. Non usare
la sessione dell'amministratore per MT5. Dopo un riavvio la deploy si rifiuta di procedere finché
questa sessione non esiste. Se serve il recovery unattended, usare una procedura di autologon
approvata che conservi la password come secret LSA (per esempio Microsoft Sysinternals Autologon),
mai una password in chiaro nel registro, nel repository o negli script.

`ConsentPromptBehaviorUser=0` è una policy macchina: nega automaticamente le richieste di
elevazione provenienti dagli utenti standard, senza mostrare un prompt per credenziali. Sulla VPS
dedicata impedisce quindi al LiveUpdate avviato dal desktop `TradeJournalMT5` di aprire una finestra
UAC; l'Agent `LocalSystem` riconosce il pacchetto annunciato, ne verifica firma e contenuto e lo
riesegue dal proprio deposito protetto. Gli account amministrativi mantengono la policy UAC
separata definita da `ConsentPromptBehaviorAdmin`.

## Servizio, release iniziale e configurazione

Installare il servizio senza avviarlo. Lo script controlla prima runtime, modulo e dipendenze e
configura il riavvio SCM in caso di errore o uscita non pulita.

```powershell
Set-Location $sourceRoot
powershell -ExecutionPolicy Bypass -File scripts\windows\install-agent-service.ps1 `
  -DeploymentPython $pythonExe
```

Pubblicare la release iniziale e verificare che corrisponda byte per byte al checkout. Se il
percorso esiste già, non sovrascriverlo: la verifica deve riuscire oppure l'installazione si ferma.

```powershell
$releaseRoot = 'C:\TradeJournal\releases'
$releasePath = Join-Path $releaseRoot ('agent-' + $revision.Substring(0, 12))
$previousPythonPath = $env:PYTHONPATH
$env:PYTHONPATH = $sourceRoot
try {
  if (-not (Test-Path -LiteralPath $releasePath -PathType Container)) {
    $build = 'import sys; from windows_agent.release_manifest import build_release; build_release(sys.argv[1], sys.argv[2], revision=sys.argv[3])'
    & $pythonExe -B -c $build $sourceRoot $releaseRoot $revision
    if ($LASTEXITCODE -ne 0) { throw 'Initial release build failed.' }
  }
  $verify = 'import sys; from windows_agent.release_manifest import verify_release_matches_source; verify_release_matches_source(sys.argv[1], sys.argv[2], revision=sys.argv[3])'
  & $pythonExe -B -c $verify $releasePath $sourceRoot $revision
  if ($LASTEXITCODE -ne 0) { throw 'Initial release verification failed.' }
} finally {
  $env:PYTHONPATH = $previousPythonPath
}
if (Test-Path -LiteralPath 'C:\TradeJournal\current') {
  throw 'C:\TradeJournal\current already exists; inspect it before continuing.'
}
New-Item -ItemType Junction -Path 'C:\TradeJournal\current' -Target $releasePath | Out-Null
```

Configurare il servizio con entrambi gli endpoint HTTPS e con i digest del template. Il bearer
token e la chiave di provisioning non devono essere inseriti nell'environment.

```powershell
$templateHash = (Get-FileHash 'C:\TradeJournal\mt5-template\terminal64.exe' -Algorithm SHA256).Hash
$expertHash = (Get-FileHash 'C:\TradeJournal\mt5-template\MQL5\Experts\TradeJournal\TradeJournalBridge.ex5' -Algorithm SHA256).Hash
$serviceEnvironment = @(
  "PYTHONPATH=$releasePath",
  'PYTHONDONTWRITEBYTECODE=1',
  'TRADEJOURNAL_API_URL=https://<project-ref>.functions.supabase.co/trading-agent',
  'TRADEJOURNAL_TRADING_INGESTION_URL=https://<project-ref>.functions.supabase.co/trading-mt5-events',
  "TRADEJOURNAL_MT5_TEMPLATE_SHA256=$templateHash",
  "TRADEJOURNAL_MT5_EXPERT_SHA256=$expertHash",
  'TRADEJOURNAL_MT5_INTERACTIVE_USER=TradeJournalMT5',
  'TRADEJOURNAL_MT5_MAINTENANCE_ENABLED=1',
  'TRADEJOURNAL_MT5_MAINTENANCE_LOCAL_TIME=23:30',
  'TRADEJOURNAL_MT5_MAINTENANCE_TIMEZONE=Europe/Rome',
  'TRADEJOURNAL_MT5_MAINTENANCE_GRACE_MINUTES=120',
  'TRADEJOURNAL_MT5_MAINTENANCE_STATE_PATH=C:\TradeJournal\state\mt5-maintenance.json',
  "TRADEJOURNAL_AGENT_RELEASE_REVISION=$revision",
  'TRADEJOURNAL_AGENT_DEPLOYMENT_ID=<deployment-uuid>',
  'TRADEJOURNAL_AGENT_READINESS_PATH=C:\TradeJournal\state\agent-readiness.json'
)
$serviceRegistry = 'HKLM:\SYSTEM\CurrentControlSet\Services\TradeJournalMT5Agent'
New-ItemProperty -Path $serviceRegistry -Name Environment -PropertyType MultiString `
  -Value $serviceEnvironment -Force | Out-Null
```

Il servizio gira come `LocalSystem`, quindi i blob DPAPI globali devono essere creati dalla stessa
identità. Aprire una PowerShell interattiva `LocalSystem` con PsExec64 ufficiale di Microsoft
Sysinternals (`PsExec64.exe -accepteula -i -s powershell.exe -NoProfile`), verificare che `whoami`
restituisca `nt authority\system`, quindi eseguire nel nuovo prompt:

```powershell
Set-Location 'C:\TradeJournal\releases\pool71a'
powershell -ExecutionPolicy Bypass -File scripts\windows\receive-agent-token.ps1 `
  -SecretName agent_token
powershell -ExecutionPolicy Bypass -File scripts\windows\receive-agent-token.ps1 `
  -SecretName mt5_provisioning_key
```

I prompt usano `SecureString`; non passare segreti come parametri, environment, file di testo o
cronologia PowerShell.

## Prima attivazione e deploy successivi

Con la sessione `TradeJournalMT5` ancora attiva o disconnessa, eseguire il gate completo. Sul primo
avvio la release esiste già: viene riutilizzata soltanto se manifest, SHA completo, allowlist,
dimensioni e hash corrispondono al checkout.

```powershell
Set-Location $sourceRoot
powershell -ExecutionPolicy Bypass -File scripts\windows\deploy-history-import-release.ps1 `
  -Revision $revision `
  -SourceRoot $sourceRoot `
  -DeploymentPython $pythonExe `
  -RecoveryConnectionId '<uuid-account-FPMTrading-Live>'

powershell -ExecutionPolicy Bypass -File scripts\windows\status.ps1
Get-Content 'C:\TradeJournal\logs\agent-service.log' -Tail 100
```

`RecoveryConnectionId` è obbligatorio quando esiste già almeno un'istanza e deve identificare
l'account investor-only su `FPMTrading-Live` usato come prova end-to-end. Va omesso soltanto per
una prima attivazione in cui il preflight `LocalSystem` attesta contemporaneamente zero processi
MT5 e zero istanze provisioned.

Fuori dalla finestra notturna si può preparare e collaudare la release senza fermare il servizio,
commutare la junction o modificare golden, pool e istanze:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\windows\deploy-history-import-release.ps1 `
  -Revision $revision `
  -SourceRoot $sourceRoot `
  -DeploymentPython $pythonExe `
  -RecoveryConnectionId '<uuid-account-FPMTrading-Live>' `
  -PrepareOnly
```

Questa modalità esegue checkout gate, test, compilazione EA, build immutabile e preflight reale.
L'attivazione completa va poi rilanciata senza `PrepareOnly` durante la finestra approvata.

Per una release successiva, fare fetch, eseguire checkout detached dello SHA completo e verificare
che `git status --porcelain=v1 --untracked-files=all` non produca output; poi rilanciare lo stesso
comando con il nuovo `-Revision`. La deploy ripete il controllo Git subito prima del packaging,
esegue test Python e parser PowerShell, compila l'EA, verifica la release e solo allora avvia il
protocollo `LocalSystem`. Prima della commutazione salva byte per byte Bridge, marker, environment
e junction precedenti. Il rollback esatto è consentito soltanto prima della barriera write-once di
attivazione; dopo quella barriera il nuovo servizio può avere già riconciliato stato, pool o
istanze, quindi ogni errore viene recuperato esclusivamente in avanti sulla nuova release. Il
servizio stesso rifiuta di costruire il runner se la barriera non corrisponde a SHA completo e UUID
del deploy e alla prova di convergenza completa di golden, pool e fleet. Il comando può essere
ritentato con lo stesso SHA: una directory esistente viene accettata soltanto se è l'esatta
release richiesta. La convergenza può occupare gran parte della finestra perché canary, pool e
istanze vengono verificati in modo conservativo; un timeout di trasporto non autorizza mai l'avvio
del nuovo servizio senza il record di convergenza.

L'attivazione è ammessa solo nella finestra di manutenzione che parte alle 23:30 `Europe/Rome` e
richiede una sessione nuova di `TradeJournalMT5` realmente standard. Dopo una demozione dal gruppo
Administrators occorre quindi fare un `Sign out` completo e un nuovo login con la password già
esistente: disconnettere soltanto RDP conserva il vecchio token, mentre reimpostare la password può
rendere irrecuperabili dati DPAPI legati all'utente. Il gate controlla anche token e sessione di
ogni `terminal64.exe`; un processo elevato preesistente non viene mai adottato.

## Prova ad hoc su un solo account

La prova ad hoc è distinta dalla manutenzione notturna e può essere avviata soltanto fuori dalla
finestra configurata della manutenzione. Con i valori di produzione viene rifiutata dalle 23:30
alle 01:30 `Europe/Rome`, estremi inclusi. Richiede una release immutabile già prodotta con
`-PrepareOnly`, ferma temporaneamente soltanto il servizio Agent per evitare concorrenza tra
processi e lascia accesi tutti i terminali diversi dal canary richiesto:

```powershell
$releasePath = 'C:\TradeJournal\releases\agent-' + $revision.Substring(0, 12)
powershell -ExecutionPolicy Bypass `
  -File (Join-Path $releasePath 'scripts\windows\invoke-mt5-adhoc-canary.ps1') `
  -Revision $revision `
  -ReleasePath $releasePath `
  -ConnectionId '<uuid-account-FPMTrading-Live>' `
  -ExpectedServer 'FPMTrading-Live' `
  -ExpectedTerminalCount 4 `
  -DeploymentPython $pythonExe
```

Il launcher rifiuta l'esecuzione se si trova nella finestra notturna, se un deploy completo è già in
esecuzione, se l'Agent sta elaborando un job o se UUID, server, release, identità interattiva e
numero di terminali non corrispondono. Il controllo temporale viene ripetuto immediatamente prima
dello stop dell'Agent e dall'helper `LocalSystem` prima del probe.
Durante questa prima fase il launcher e l'helper SYSTEM hanno una doppia allowlist compilata nel
codice: accettano esclusivamente l'UUID `2f1647b4-035e-41be-b634-0cf785a70b07` sul server esatto
`FPMTrading-Live`. Il primo collaudo richiede inoltre la release legacy verificata attualmente
attiva: questo impedisce che il riavvio abiliti per errore lo scheduler nuovo e consumi la receipt
con una cascata nella finestra notturna. Un task completo semplicemente pianificato non blocca la
prova; mutex e gate reciproci impediscono invece la sovrapposizione quando un deploy o un altro
helper è realmente in esecuzione.

Il probe viene eseguito come `LocalSystem` e scarica sempre una copia nuova dell'installer stabile
ufficiale MetaQuotes. Verifica Authenticode, materializza la distribuzione in una directory isolata,
legge la build PE e inventaria in sola lettura tutte le istanze. Durante il collaudo soltanto
l'account FPM allowlisted può essere riavviato e solo se la sua build è `older`; `ahead`,
`same_build_divergent` e `unverifiable` bloccano qualsiasi mutazione. Il riavvio usa `new_only`,
richiede login, heartbeat e accesso investor-only e confronta PID e creation time di tutti gli
altri terminali prima e dopo. Il servizio Agent viene riavviato soltanto dopo un risultato
SYSTEM valido e completo, quindi deve rimanere stabile per almeno dieci secondi; quando la release
attiva supporta il protocollo corrente viene richiesta anche la readiness legata al PID. Se
l'helper parte ma fallisce, resta in esecuzione o non pubblica un risultato attendibile, il servizio
rimane prudenzialmente fermo per non affidare uno stato incerto a una release incompatibile. In tal
caso resta anche in avvio `Manual`, così un reboot della VPS non può far ripartire il vecchio Agent
durante una LiveUpdate incerta. Non avviare un deploy: ispezionare prima il task
`TradeJournal-MT5-AdHoc-*`, il result JSON e il log indicati dal launcher, quindi ripristinare
`Automatic` e avviare il servizio soltanto dopo aver escluso processi di update ancora attivi.

L'eventuale LiveUpdate valido resta conservato come receipt nel deposito pending, ma non è la prova
che il terminale fosse già allineato all'ultima release pubblica. Questa modalità non promuove il
golden, non apre o ricostruisce il pool, non ruota la flotta e non scrive lo stato dello scheduler.
La successiva manutenzione completa delle 23:30 rimane l'unico percorso che può pubblicare il
template e aggiornare pool e account a cascata.

## Manutenzione MT5

Ogni sera alle 23:30 la manutenzione scarica nuovamente l'installer stabile ufficiale MetaQuotes,
ne verifica firma, build e distribuzione isolata e confronta quella build con golden, pool e tutte
le connessioni provisioned. Non presume che l'assenza di un'offerta LiveUpdate significhi
"aggiornato". Se la build pubblica è più nuova del golden, costruisce un candidato non pubblicato
con i tre asset TradeJournal già fissati e lo verifica su una connessione autenticata per ogni
server broker. Solo dopo login, heartbeat e verifica investor-only di tutti i canary pubblica
atomicamente il template, ricostruisce in modo sincrono gli slot READY del pool e migra, una alla
volta, esclusivamente le istanze `older`. Una build `ahead` non viene mai retrocessa; una release
`same_build_divergent` non viene sostituita in base al solo numero; uno stato `unverifiable` blocca
il pass prima delle mutazioni. Se una verifica fallisce prima della pubblicazione, golden e pool
restano sulla release precedente; dopo la barriera il recupero è esclusivamente in avanti.

Durante una nuova connessione o un recovery diurno, il LiveUpdate firmato può ancora completarsi
esclusivamente dentro la directory isolata di quell'istanza. L'Agent ne salva la ricevuta nel
deposito protetto per sopprimere popup/UAC e conservare evidenza, ma nel flusso con baseline
pubblica non usa quel delta broker come autorità per una cascata.

Ogni riavvio usa `history_mode=new_only`. Prima dello stop viene però salvato l'ultimo heartbeat e,
al riavvio, il bridge rilegge soltanto il piccolo intervallo scoperto (un minuto di sovrapposizione,
massimo sei ore): così una chiusura avvenuta durante lo swap non viene persa e lo storico completo
non viene reimportato. Log, cache Tester e cache storiche `Bases` non vengono ricopiati.

MetaTrader non offre un interruttore supportato per disabilitare LiveUpdate. L'Agent elevato
intercetta l'updater firmato e impedisce che la richiesta UAC venga mostrata nella sessione
amministrativa.

`TRADEJOURNAL_POLL_SECONDS` non è più letto. Le sole riconnessioni con backoff sono quelle del
WebSocket Realtime; nessun timer produce chiamate `claim` o heartbeat account.
