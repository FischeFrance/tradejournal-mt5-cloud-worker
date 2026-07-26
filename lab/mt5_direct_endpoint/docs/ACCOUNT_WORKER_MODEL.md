# Modello account/worker: onboarding, isolamento, lifecycle

Questo documento descrive lo strato applicativo aggiunto sopra il Coordinator
C012, il registry degli endpoint verificati e il generatore di config dry-run
già esistenti (vedi [README.md](../README.md) per l'architettura C012 e il
[RUNBOOK.md](../RUNBOOK.md) per lo stato GO/NO-GO formale). Non ridefinisce
né sostituisce nulla di quei componenti: li riusa.

Moduli Python: `tools/account_onboarding.py`, `tools/credential_provider.py`,
`tools/account_worker.py`, `tools/worker_supervisor.py`. CLI: sei nuovi verbi
in `tools/labctl.py` (`create-account`, `start-worker`, `stop-worker`,
`worker-status`, `verify-endpoint`, `generate-config-dry-run`). Lato C#: un
solo verbo nuovo, `c012-host start-innocuous` (vedi sezione "Eccezione
autorizzata" più sotto).

## 1. First-login onboarding vs. runtime in background

Sono due fasi distinte, con proprietà molto diverse, e non vanno confuse:

**First-login onboarding** (`tools/account_onboarding.py`,
`OnboardingStateMachine`) è il percorso che porta un account da zero a
operativo: creazione della directory portable, richiesta e verifica
dell'endpoint broker, credenziali, validazione del login. È una macchina a
stati esplicita, a tabella di transizione congelata (stesso pattern di
`C012StateMachine.cs`): nessuna transizione implicita, nessuna ripresa dopo
un esito ambiguo. Se il primo login di un broker mai censito richiede
interazione GUI per la discovery dell'endpoint, quella discovery è
onboarding controllato **fuori** da questa macchina a stati (resta un
processo operativo separato che produce, alla fine, un record `VERIFIED` nel
registry) — questa macchina inizia solo da `BROKER_ENDPOINT_REQUIRED` in poi
assumendo che la discovery, se necessaria, sia già avvenuta.

Stati: `NEW → PORTABLE_DIRECTORY_CREATED → BROKER_ENDPOINT_REQUIRED →
BROKER_ENDPOINT_VERIFIED → CREDENTIALS_PENDING → LOGIN_VALIDATION → ACTIVE`,
con `FAILED_CLOSED` e `STOPPED` come stati terminali assorbenti raggiungibili
da ogni stato non terminale.

**Runtime in background** (`tools/account_worker.py` +
`tools/worker_supervisor.py`) è cosa succede *dopo* `ACTIVE`: un worker
persistente per account che resta vivo, con heartbeat, rilevamento crash e
restart limitato. Il cliente non deve mai vedere una finestra MT5 durante
questa fase — ma questo repository non dimostra ancora un login MT5 reale
che arrivi fino a questa fase (vedi §7 "Limiti attuali").

## 2. Isolamento per account

`WorkerDirectory` (in `account_worker.py`) crea, per ogni `account_id`, una
directory dedicata sotto una root comune:

```text
<worker-root>/<account_id>/
├── config/    configurazione non sensibile
├── state/     worker.json (stato persistente)
├── logs/
└── session/   file di sessione C012 (session.id, session.secret, ...)
```

`account_id` deve rispettare `^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$` — nessun
attraversamento di percorso è possibile per costruzione. La creazione
rifiuta di riusare o sovrascrivere una directory già esistente, stesso
principio già applicato da `C012HostCli` alle proprie `--session-dir`. Due
account non condividono mai directory, file di stato, o (a livello di
`worker_supervisor.Supervisor`) lo stesso `LauncherHandle`/processo.

## 3. Lifecycle del worker: processo persistente

Stati (`WorkerState` in `account_worker.py`): `STARTING, RUNNING,
LOGIN_REQUIRED, HEALTHY, DEGRADED, RESTARTING, FAILED_CLOSED, STOPPED`.

Il supervisor (`worker_supervisor.Supervisor`) non apre mai un Job Object né
tocca API di processo direttamente: chiama soltanto un `LauncherHandle`
(interfaccia). Ci sono tre implementazioni:

- `FakeLauncherHandle` — fake in-process, usata dai test unitari del solo
  `Supervisor`;
- `SubprocessSleeperLauncherHandle` — processo reale, cross-platform,
  innocuo (nessun MT5, nessun Job Object): è il launcher di default del
  worker-host persistente;
- `JobHarnessCliLauncherHandle` — Windows-only, usata dallo smoke test reale
  (§8): guida il Coordinator C012 già esistente via `c012-host
  start-innocuous` + `c012-client`, mai reimplementando la logica di Job
  Object in Python.

**Il worker è un processo OS persistente**, non una sequenza di comandi
one-shot. `tools/worker_host.py` implementa:

- `worker-host` (`labctl.py worker-host --account-id ... --worker-root ...`)
  — il processo long-lived stesso: risolve l'account, avvia un `Supervisor`
  con il launcher innocuo di default, poi cicla (intervallo configurabile,
  default 2s) su heartbeat + `check_liveness()` finché non riceve una
  richiesta di stop o non raggiunge `FAILED_CLOSED`. Eseguibile sia in
  foreground (test/debug) sia spawnato in background.
- `start-worker` spawna `worker-host` come processo separato e distaccato
  (detached: `start_new_session` su POSIX, `CREATE_NEW_PROCESS_GROUP` su
  Windows), attende (bounded) che il PID sia realmente vivo, poi ritorna.
  Idempotente: se un worker-host per l'account è già vivo, non ne spawna un
  secondo (un solo worker-host per account, sempre).
- `stop-worker` scrive una richiesta di stop su file (vedi sotto), attende
  (bounded) che il processo termini da solo; se non termina in tempo,
  esegue un kill forzato come escalation esplicita.
- `worker-status` legge lo stato reale: non si fida del solo stato
  persistito, verifica anche a livello OS se il PID registrato è
  effettivamente vivo (`pid_is_alive`, POSIX via `os.kill(pid,0)`, Windows
  via `tasklist`).

**IPC tra i comandi CLI one-shot e il processo persistente**: un file PID
(`session/worker_host.pid`) e un file di richiesta-stop
(`session/control.stop`), entrambi scritti in modo atomico, sotto la
directory di sessione del worker — stessa filosofia già usata da
`C012SessionPaths` per la sessione C012 (la directory come unica fonte di
verità), applicata qui al livello del processo Python. Non è il Named Pipe
C012 (quello resta interno al Coordinator C#, raggiunto solo dal launcher
Windows-only); è un meccanismo di controllo su file, deliberatamente
semplice e portabile, non un servizio Windows installabile.

Logica di restart: un contatore limitato (default 3) — oltre il limite si
entra in `FAILED_CLOSED`, stato assorbente (un ulteriore `start-worker`
viene rifiutato senza spawnare nulla). `stop()` è ordinato (segnale →
attesa limitata → terminazione forzata → verifica che il processo sia
davvero terminato, altrimenti `stop()` fallisce esplicitamente invece di
dichiarare successo). `cleanup()` rimuove solo i file di sessione effimeri
(incluso il PID file e il file di controllo), mai lo storico persistito in
`state/worker.json`. Il PID del launcher supervisionato è tracciato in
`state_file.launcher_pid` — non è un segreto, serve solo a verificare
dall'esterno che non resti alcun processo residuo dopo lo stop.

## 4. Gestione degli endpoint

Ogni risoluzione di endpoint passa da `endpoint_registry.resolve_verified()`
(mai reimplementata localmente): solo record `VERIFIED`, non scaduti, e in
numero esattamente pari a uno vengono accettati. Un registry mancante,
un record `CANDIDATE`/`METAQUOTES_CDN`/`EXPIRED`, o più record `VERIFIED`
ambigui producono tutti un fail-closed esplicito nella macchina a stati di
onboarding (`MISSING_ENDPOINT`), mai un tentativo silenzioso con un valore
"probabile".

## 5. Gestione degli errori

`FailureReason` (in `account_onboarding.py`) distingue esplicitamente:

| Motivo | Innesco tipico |
|---|---|
| `MISSING_ENDPOINT` | nessun endpoint `VERIFIED` risolvibile (o registry assente) |
| `UNREACHABLE_ENDPOINT` | endpoint risolto ma non raggiungibile |
| `WRONG_CREDENTIALS` | credenziali rifiutate |
| `SERVER_REJECTED` | il server broker ha rifiutato la sessione |
| `PROCESS_DIED` | il processo MT5/worker è terminato inaspettatamente |
| `TIMEOUT` | timeout in attesa di uno stato atteso |
| `INVALID_CONFIG` | configurazione/registry presente ma non valida (JSON corrotto, digest non corrispondente, schema errato) |

Ogni transizione di fail-closed porta con sé esattamente uno di questi
motivi — mai un generico "errore", mai un motivo dedotto a posteriori.

## 6. Gestione dei segreti

`tools/credential_provider.py` fornisce `SecretString` (mai renderizzata in
`repr`/`str`/f-string, confronto a tempo costante, `pickle` bloccato) e
`json_dumps_safe()` (solleva `CredentialLeakError` invece di serializzare
silenziosamente un segreto). **Nessun provider di credenziali reale è
implementato in questo lab**: esiste solo `FakeCredentialProvider`,
esplicitamente test-only. Il punto di integrazione per un provider reale è
l'interfaccia `CredentialProvider.get(account_id) -> Credentials`,
documentata nel modulo — un'implementazione reale non deve mai persistere
credenziali su disco, variabili d'ambiente, log o argomenti di processo.

Il file di stato del worker (`state/worker.json`) non contiene mai
credenziali: solo `host:port` risolto (valore pubblico, non segreto).

## 7. Limiti attuali

- **Nessun login MT5 reale è stato eseguito o automatizzato.** Questo lavoro
  costruisce l'impalcatura (FSM di onboarding, worker, supervisor, registry,
  dry-run) ma non collega un provider di credenziali reale né un client MT5
  reale — resta un punto di integrazione esplicito, non implementato.
- `HARD_DISABLED` resta invariato, ovunque, per MT5: nessuna modifica lo
  indebolisce.
- Il worker-host persistente (`labctl.py worker-host`, spawnato da
  `start-worker`) è un processo Python foreground-capable, testabile
  direttamente — **non** un servizio Windows installabile. L'IPC verso
  `stop-worker`/`worker-status` è basato su file (PID file + file di
  controllo), non sul Named Pipe C012 (quello resta interno al Coordinator
  C#, raggiunto solo dal launcher Windows-only via `start-innocuous`).
- Il launcher di default del worker-host è `SubprocessSleeperLauncherHandle`
  (processo reale ma innocuo, nessun Job Object): il Job Object reale via
  Coordinator C012 è usato solo nello smoke Windows (§8), non nell'uso
  generale di `labctl.py`.
- Il ponte tra il tracciato di produzione esistente (`windows_agent/`, con
  provisioning DPAPI per-account già maturo) e questo tracciato lab
  (credential-free, C012-based) non è stato costruito: sono rimasti
  intenzionalmente disgiunti in questa patch.

## 8. Eccezione autorizzata: `c012-host start-innocuous`

Fino a questa patch, nessun punto della CLI di produzione poteva avviare un
processo reale: `c012-host start` usa sempre
`C012NotImplementedRootProcessLauncher` (lancia eccezione su ogni chiamata) e
il comando legacy `run --execute` è `HARD_DISABLED` e non crea mai un
processo, qualunque sia il target. Per dimostrare un ciclo di vita Job
Object reale end-to-end dal worker Python (smoke Windows, §6 della richiesta
originale), è stata autorizzata esplicitamente un'eccezione mirata:

- nuovo verbo `c012-host start-innocuous --session-dir <path> --target
  self-sleeper` — l'unico target ammesso, `self-sleeper`, fa sì che il
  processo JobHarness rilanci **se stesso** con un flag innocuo
  (`--innocent-lab-sleeper`), mai un eseguibile arbitrario o fornito dal
  chiamante;
- nessun percorso o hash SHA-256 è mai accettato da linea di comando: sono
  calcolati internamente dal binario in esecuzione;
- il costruttore di `C012InnocuousRootProcessLauncher` continua comunque a
  rifiutare, indipendentemente, nomi tipo `terminal(64).exe`/
  `metaeditor(64).exe` — secondo livello di difesa;
- `c012-host start` (il verbo di produzione) resta byte-per-byte invariato;
- `HARD_DISABLED` per MT5 resta invariato.

Il verbo è marcato esplicitamente come test-only/harmless nei commenti del
codice sorgente ed è raggiungibile solo tramite lo smoke test Windows
(`tests/test_worker_supervisor_windows_smoke.py`), mai tramite `labctl.py`.
