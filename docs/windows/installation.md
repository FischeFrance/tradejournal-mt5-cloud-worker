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

Il percorso corrente usa il file bridge MQL5 e non installa né importa il
wheel Python `MetaTrader5`.

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
TRADEJOURNAL_BROKER_IDENTITY_MODEL=gpt-5.6
```

La risposta AI non abilita MT5 e non verifica un endpoint. Il job prosegue solo
se il registry locale contiene già un endpoint `VERIFIED` per il broker
suggerito; la coppia server/broker entra nel catalogo globale esclusivamente
dopo un login investor completato e verificato.
