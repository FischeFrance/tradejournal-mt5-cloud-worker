# Installazione Windows

Prerequisiti:

- Windows Server 2022 x64 in sessione interattiva;
- Python 3.12 x64;
- .NET 8 SDK per JobHarness e strumenti di laboratorio;
- PowerShell 5.1 o successivo;
- runtime VC++ x64 richiesto da MT5;
- installazione MT5 proveniente da fonte ufficiale.

Da PowerShell amministrativo:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File scripts\windows\bootstrap-server.ps1

py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-windows.txt
```

Il bootstrap non deve installare, avviare o configurare MT5 senza
autorizzazione esplicita. Prima di un test controllare che non siano attivi:

```text
terminal.exe
terminal64.exe
metaeditor.exe
metaeditor64.exe
metatester.exe
metatester64.exe
```

## Sessione MT5 separata

Le istanze MT5 non devono essere lanciate nella sessione RDP
`Administrator`. Configurare un account locale dedicato e privo di privilegi
amministrativi, per esempio `TradeJournalMT5`, quindi impostare:

```text
TRADEJOURNAL_MT5_INTERACTIVE_USER=TradeJournalMT5
TRADEJOURNAL_MT5_WIZARD_ENABLED=0
```

L'account deve avere una sessione Windows interattiva attiva; la sessione può
essere disconnessa, ma non terminata. I task MT5 vengono eseguiti con livello
`LIMITED` in quella sessione, quindi le finestre non appaiono sul desktop
dell'operatore Administrator. Su Windows il runtime rifiuta sia una
configurazione assente, sia `Administrator` e le identità di servizio
integrate: non esiste un fallback silenzioso sulla sessione del servizio.

Questa separazione non è una minimizzazione della finestra: MT5 conserva un
desktop reale per inizializzare chart e script, ma quel desktop appartiene
esclusivamente all'account runtime. Dopo un riavvio della VPS la sessione
dedicata deve essere ricreata prima che l'agente accetti nuovi provisioning;
la futura automazione del bootstrap della sessione deve restare separata dalle
credenziali MT5.

`TRADEJOURNAL_MT5_WIZARD_ENABLED=1` abilita esplicitamente il censimento
credential-free tramite il wizard ufficiale soltanto quando il registry non
contiene già un endpoint `VERIFIED`. Il helper usa la sessione dedicata,
richiede la presenza esatta del server atteso e chiude il terminale di
censimento prima che l'agente decifri la password investor. La promozione
server/broker avviene soltanto dopo il successivo login read-only verificato.

Il flusso ordinario usa il file bridge MQL5. La manutenzione utilizza un reader
Python isolato per acquisire i soli ticket dal terminale già aperto, prima di
arrestarlo; non sostituisce il bridge e non esegue operazioni di trading.

## Compatibilità della manutenzione notturna

La manutenzione della flotta è una funzionalità del worker, non un task
dell'Utilità di pianificazione di Windows. Il solo valore
`TRADEJOURNAL_MT5_MAINTENANCE_ENABLED=1` non prova che il servizio la esegua.

Per una VPS con manutenzione abilitata, preparare la release con:

```powershell
python -B scripts\windows\package-agent-release.py `
  --output-root C:\TradeJournal\releases --require-maintenance
```

Il controllo richiede i moduli di manutenzione, il probe della release pubblica,
la lettura della configurazione e il collegamento al ciclo del worker.
`install-agent-service.ps1` ripete il controllo sulla configurazione effettiva
del servizio prima di modificare ACL, registrazione Python o registro Windows.
Una release integra nei byte ma priva di una funzione abilitata viene rifiutata.

Il reader richiede un ambiente separato con `MetaTrader5` e `psutil`, accessibile
in sola lettura all'utente MT5 dedicato e modificabile solo da SYSTEM e
Administrators. Un manifest SHA-256 elenca tutti i suoi file `.py`, `.pyd`,
`.dll` e `.exe`. Configurare `TRADEJOURNAL_MT5_HISTORY_READER_PYTHON`,
`TRADEJOURNAL_MT5_HISTORY_READER_MANIFEST` e il relativo
`TRADEJOURNAL_MT5_HISTORY_READER_MANIFEST_SHA256`. Abilitare
`TRADEJOURNAL_MT5_TICKET_RECOVERY_ENABLED=1` solo dopo la verifica reale delle
acquisizioni e della consegna di un job con lease.

Il preflight acquisisce una base verificata per ogni account attivo assegnato
prima di fermare il primo canary. Dopo il riavvio il producer pubblica uno
snapshot storico immutabile; il job recupera i ticket nuovi e il contesto delle
relative posizioni. I timestamp del broker non vengono reinterpretati come
UTC. Il supervisore live attende il completamento dell'handoff. Un cursore
incompleto o una consegna non confermata impediscono un'ulteriore rotazione;
un errore del control plane ripristina il producer investitore live.

Il monitor indipendente scrive `mt5-maintenance-health.json` accanto al journal
originale e invia tramite il logger del servizio un evento ogni minuto. Gli
stati `unsupported`, `overdue`, `failed` e `invalid_state` sono errori, anche se
gli account continuano a ricevere eventi. Non modifica né azzera
`mt5-maintenance.json`; il controllo usa `Europe/Rome` e la finestra configurata,
anche quando l'orologio Windows è impostato su un altro fuso.

In Grafana/Loki filtrare gli eventi con:

```logql
{service="tradejournal-mt5-agent", logger="windows_agent.observability.maintenance_health"}
```

Per le regole di alert includere gli eventi di livello `error` e l'assenza di
eventi del monitor. La sola presenza dell'heartbeat degli account non è una
verifica della manutenzione. L'invio dei log non crea automaticamente una
regola di alert Grafana.

Le protezioni verificano e osservano la funzione esistente: il ripristino della
pipeline deve includere scheduler, coordinatore e dipendenze, conservando le
correzioni del worker in uso. Una copia WIP o un ritorno indiscriminato alla
vecchia release non sono una release di ripristino verificata.

La sincronizzazione storico nativa usa il pin del terminale pubblicato per
l'account e il pin dei file TradeJournal, come la sincronizzazione live. Non
confronta MetaEditor e MetaTester con il template globale: MT5 può aggiornare
questi strumenti separatamente. Il manifest completo resta obbligatorio per
la pubblicazione di nuove istanze e per il percorso adapter non nativo.

## Pin obbligatori del runtime

Il servizio rifiuta l'avvio se i due binari eseguibili non sono vincolati a un
digest SHA-256 atteso. Calcolare i digest soltanto dopo aver pubblicato e
verificato il template approvato:

```powershell
$terminal = 'C:\TradeJournal\mt5-template\terminal64.exe'
$expert = 'C:\TradeJournal\mt5-template\MQL5\Experts\TradeJournal\TradeJournalBridge.ex5'

$terminalSha256 = (Get-FileHash -LiteralPath $terminal -Algorithm SHA256).Hash.ToLowerInvariant()
$expertSha256 = (Get-FileHash -LiteralPath $expert -Algorithm SHA256).Hash.ToLowerInvariant()
```

Configurazione minima del servizio:

```text
TRADEJOURNAL_API_URL=https://<project-ref>.functions.supabase.co/trading-agent
TRADEJOURNAL_TRADING_INGESTION_URL=https://<project-ref>.functions.supabase.co/trading-mt5-events
TRADEJOURNAL_MT5_TEMPLATE_SHA256=<64 caratteri esadecimali>
TRADEJOURNAL_MT5_EXPERT_SHA256=<64 caratteri esadecimali>
```

Non inserire token o password in queste variabili: il token agente, la password
investor e il bridge token restano esclusivamente nel secret store DPAPI.
Ogni riuso dell'istanza ricalcola il digest di `terminal64.exe` e il manifest
dei binari eseguibili del template; una differenza blocca il job prima di
avviare o riavviare il processo. File runtime mutabili come log, cache e
configurazioni non fanno parte del manifest dei binari.

## Risoluzione identità broker

Per server non ancora presenti nel catalogo globale, il servizio può chiedere
una sola identificazione suggestion-only a OpenAI e conservarla in una cache
atomica con TTL. Configurare `OPENAI_API_KEY` esclusivamente nell'ambiente
protetto del servizio o nel secret manager Windows, mai nel repository o nei
metadata del job. Il provider riceve soltanto il server MT5:

```text
TRADEJOURNAL_BROKER_IDENTITY_CACHE=C:\TradeJournal\broker-registry\broker-identity-cache.json
TRADEJOURNAL_BROKER_IDENTITY_CACHE_TTL_SECONDS=86400
TRADEJOURNAL_BROKER_IDENTITY_MODEL=gpt-5.6-terra
```

La risposta AI non abilita MT5 e non verifica un endpoint. Il job prosegue solo
se il registry locale contiene già un endpoint `VERIFIED` per il broker
suggerito; la coppia server/broker entra nel catalogo globale esclusivamente
dopo un login investor completato e verificato.
