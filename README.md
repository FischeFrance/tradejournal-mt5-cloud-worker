# tradejournal-mt5-cloud-worker

POC **isolato** per un Cloud Sync self-hosted di MetaTrader 5: un worker Python che rileva eventi
di trading (apertura/modifica/chiusura posizioni, ordini pendenti) e li invia all'API di
ingestion di TradeJournal. Il worker applicativo gira in Docker; con MT5 reale usa un bridge
HTTP read-only eseguito da Python Windows nello stesso Wine prefix del terminale. Questo
repository e' **indipendente** dal repository principale `tradejournal-drp`, che non viene mai
modificato da questo progetto.

Stato attuale:
- **Fase 1 (fatta): modalita' mock.** Nessuna dipendenza da Wine/MT5. Simula una sequenza
  completa di eventi e li invia (o stampa, in dry-run) verso l'API TradeJournal.
- **Fase 2 (branch `feature/mt5-wine-runtime`): infrastruttura MT5 reale + Wine.** Lo stage
  `real-mt5` del Dockerfile e il client legacy `RealMt5Client` restano predisposti per una VPS
  Ubuntu amd64. Il percorso raccomandato e' ora il bridge con Python Windows: su macOS Apple
  Silicon e' stata verificata realmente la connessione IPC al terminale ufficiale tramite
  `initialize(path=...)`, con letture `terminal_info()`/`account_info()`/`positions_get()`/
  `orders_get()` e `shutdown()`. Snapshot HTTP completo e stabilita' prolungata restano da
  validare; vedi "mt5-bridge reale".
- **Fase 3 (fatta, validata su Docker locale arm64): modalita' research (dati di mercato).**
  Processo separato (`market-data-worker`, `worker/market_data_main.py`) che raccoglie candele
  OHLC e le salva su un Postgres locale, dietro `APP_MODE=research` + `ENABLE_MARKET_DATA=true`
  (disattiva per default, isolata dal trade-sync worker). Sorgente dati **solo mock** in questa
  fase (nessuna dipendenza da Wine/MT5): vedi "Modalita' research (dati di mercato)" piu' sotto.
- **Trade-sync via bridge HTTP:** il worker Linux usa `GET /health` e una sola
  `POST /v1/trading/snapshot` per poll. Il fake deterministico e il compose locale girano anche
  su ARM64. Il core IPC del bridge Windows e' stato verificato sul terminale MT5 ufficiale per
  macOS; il flusso HTTP reale completo e il deployment VPS AMD64 restano da validare.

Nessun deploy o risorsa cloud e' stato creato. Nessuna credenziale reale e' inserita nel
repository, nei test o nella documentazione; la verifica macOS ha riusato una sessione gia'
aperta senza fornire la password al bridge.

## Contratto del payload

Il worker produce esattamente i campi attesi dall'endpoint `trading-mt5-events` di TradeJournal
(schema verificato in sola lettura sul repository principale, coerente con l'EA MQL5 gia'
esistente `TradeJournalDRPConnector.mq5`):

```
event_id, event_type, platform, account_number, server, external_trade_id, symbol, direction,
volume, price, open_price, close_price, stop_loss, take_profit, previous_stop_loss,
previous_take_profit, profit, commission, swap, open_time, close_time, event_time
```

Auth: header `Authorization: Bearer <TRADEJOURNAL_BRIDGE_TOKEN>`, `Content-Type: application/json`.

`event_type` e' uno tra: `trade_opened`, `trade_modified`, `trade_closed`,
`pending_order_created`, `pending_order_modified`, `pending_order_cancelled`.

## Architettura

```
worker/
  config.py            Lettura env var (MOCK_MODE, DRY_RUN, APP_MODE, ENABLE_MARKET_DATA, ...)
  mt5_client.py         Interfaccia Mt5Client astratta + RealMt5Client (stub, richiede Wine/MT5)
  mock_mt5_client.py     MockMt5Client: macchina a stati, nessuna dipendenza reale
  snapshot_store.py     Stato dell'ultimo poll (in memoria, opzionalmente su file)
  event_detector.py     Diff puro tra due snapshot -> eventi grezzi (nessuna rete, testabile)
  event_normalizer.py   Eventi grezzi -> payload API + event_id idempotente
  event_sender.py       Invio HTTP con retry/backoff, dry-run, masking dei log
  main.py                Loop di poll, logging, ciclo di vita, heartbeat per l'healthcheck
                         (trade-sync worker: TUTTO quanto sopra, invariato dalla Fase 1/2)

  market_data_source.py Interfaccia MarketDataSource + MockMarketDataSource (deterministica) +
                         Mt5MarketDataSource (client HTTP verso mt5-bridge, vedi sotto)
  market_data_store.py  Upsert idempotente su Postgres (market_symbols/market_candles),
                         checkpoint derivato da MAX(open_time)
  db_migrate.py          Applica db/migrations/*.sql in ordine, tracciate in schema_migrations
  market_data_main.py    Entry point del market-data-worker: processo/container SEPARATO da
                         main.py, proprio loop/heartbeat/signal handling, mai importato da main.py
                         (modalita' research, vedi sezione dedicata piu' sotto)

db/
  migrations/            Schema SQL esplicito e versionato (nessuna DDL sparsa nel codice Python)

bridge/
  common.py              Scaffolding HTTP condiviso (stdlib only): auth Bearer, parsing/
                         validazione richieste, envelope JSON/errori -- nessuna logica MT5
  fake/
    fake_bridge.py        Bridge finto: dati sintetici deterministici, nessuna dipendenza da
                         Wine/MT5, gira su qualunque architettura (usato per i test/ARM64)
    Dockerfile             Immagine del fake bridge (SOLO test/validazione locale)
  windows/
    mt5_bridge.py          Bridge reale: core IPC validato su macOS; snapshot HTTP completo e
                         runtime prolungato ancora da validare (vedi sezione "mt5-bridge")
```

Il trade-sync worker (`main.py`) e il market-data-worker (`market_data_main.py`) sono due
processi indipendenti: non condividono loop, non si importano a vicenda, girano in container
Docker separati (vedi "Modalita' research" piu' sotto). Condividono solo `config.py` (lettura
env var) perche' entrambi leggono variabili d'ambiente, non perche' dipendano l'uno dall'altro.

`MockMt5Client`, `BridgeMt5Client` e `RealMt5Client` implementano la stessa interfaccia
`Mt5Client`. `main.py` seleziona `mock|bridge|direct` tramite `MT5_CLIENT_SOURCE`, mantenendo la
derivazione retrocompatibile da `MOCK_MODE` quando la nuova variabile non e' impostata. Il client
bridge usa un solo snapshot HTTP coerente per poll; quello direct resta il percorso legacy.

`RealMt5Client` (in `mt5_client.py`) implementa tutti e sei i metodi richiesti (`account_info`,
`get_open_positions`, `get_recent_deals`, `get_pending_orders`, `reconnect`, `health_status`),
con un numero limitato di retry con backoff lineare (`_call_with_retry`, 3 tentativi di default)
su ogni chiamata al pacchetto `MetaTrader5`, e non stampa mai `MT5_LOGIN`/`MT5_PASSWORD` in
chiaro (sempre mascherati via `event_sender.mask_value`). Resta pero' **non testabile
end-to-end** senza un terminale MT5 reale: vedi "Fase 2: MT5 reale + Wine" per cosa e' stato
effettivamente validato (mapping dei campi e retry, con un modulo `MetaTrader5` finto iniettato
nei test) e cosa no (la connessione IPC reale di questo client Linux legacy al terminale).

## Trade-sync tramite mt5-bridge HTTP

Il percorso raccomandato con un terminale reale separa nettamente Windows/Wine dal worker
applicativo Linux:

```text
MetaTrader 5 + Windows Python (stesso WINEPREFIX)
  -> mt5-bridge: GET /health, POST /v1/trading/snapshot
  -> trade-sync worker Linux: diff, normalizzazione, event_id, retry ingestion
  -> TradeJournal ingestion API
```

Il worker Linux non contiene credenziali MT5 e non importa mai `MetaTrader5`: conosce soltanto
`MT5_BRIDGE_URL` e `MT5_BRIDGE_TOKEN`. Percorso del terminale e configurazione della sessione
vivono esclusivamente nel processo bridge Windows. In `MT5_SESSION_MODE=login` anche login,
server e password investor appartengono solo al bridge; in `existing` non e' richiesta alcuna
password e il bridge non esegue `mt5.login()`. `MT5_BRIDGE_TOKEN` autentica worker -> bridge;
`TRADEJOURNAL_BRIDGE_TOKEN` autentica worker -> app e deve essere un valore differente.

`MT5_CLIENT_SOURCE` accetta `mock`, `bridge` o `direct`. `MOCK_MODE` resta accettato come alias
di configurazione: se la nuova variabile non e' impostata, `true` seleziona `mock` e `false`
seleziona il nuovo default `bridge`. Il valore esplicito e' autorevole. `direct` conserva il
client legacy predisposto, ma il runtime raccomandato con MT5 reale e' `bridge`.

### Contratto `POST /v1/trading/snapshot`

La richiesta richiede `Authorization: Bearer <MT5_BRIDGE_TOKEN>` e un body JSON facoltativo:

```json
{"deal_lookback_hours": 24}
```

Il valore deve essere un intero positivo; il default e' 24 ore, valori oltre 168 vengono
limitati a 168 e valori booleani/non interi/non positivi producono `422
invalid_deal_lookback_hours`. La risposta completa ha questa forma:

```json
{
  "account": {
    "login": "123456",
    "server": "Broker-Demo",
    "balance": 10000.0,
    "equity": 10010.0,
    "currency": "EUR",
    "leverage": 100
  },
  "positions": [{
    "ticket": "10001", "symbol": "EURUSD", "direction": "buy", "volume": 0.1,
    "open_price": 1.17, "stop_loss": 1.168, "take_profit": 1.174,
    "open_time": "2026-07-13T10:00:00Z"
  }],
  "orders": [{
    "ticket": "20001", "symbol": "EURUSD", "direction": "buy", "volume": 0.1,
    "price": 1.168, "stop_loss": 1.166, "take_profit": 1.172, "order_type": 2
  }],
  "deals": [{
    "deal_ticket": "30001", "position_ticket": "10001", "close_price": 1.172,
    "profit": 20.0, "commission": -0.5, "swap": 0.0,
    "close_time": "2026-07-13T10:15:00Z"
  }],
  "generated_at": "2026-07-13T10:15:01Z"
}
```

Ticket e login sono sempre stringhe; timestamp sempre UTC; `positions`, `orders` e `deals` sono
sempre array, anche se vuoti. Gli ordini MT5 di tipo 0/2/4/6 sono mappati `buy`, 1/3/5/7
`sell`; nei deal entrano soltanto uscite `DEAL_ENTRY_OUT`/`DEAL_ENTRY_OUT_BY`. La risposta non
contiene password. Errori di auth/validazione/MT5 usano l'envelope strutturato
`{"error":{"code":"...","message":"..."}}`. Non esiste alcun endpoint di apertura,
modifica, cancellazione o chiusura ordini.

`BridgeMt5Client.snapshot()` effettua una sola richiesta per poll, valida l'intero payload e
converte gli array in dizionari indicizzati per ticket, come richiesto da `event_detector.py`.
L'account dello stesso payload viene memorizzato; la successiva `account_info()` non effettua una
seconda richiesta. Timeout/errori rete/5xx hanno retry limitato; 401, 403, 404 e 422 non vengono
ritentati.

### Test manuale locale: fake bridge -> app locale

Il compose dedicato contiene soltanto `mt5-bridge-fake` e `trade-sync-worker`: nessun Postgres,
nessun market-data-worker e nessuna porta pubblicata. Il worker usa un volume nominato per
`/app/data/snapshot.json`; non rimuoverlo durante un normale riavvio.

```bash
cp .env.example .env

# .env: usare l'IP LAN del Mac su cui ascolta l'app, per esempio da `ipconfig getifaddr en0`.
MT5_BRIDGE_TOKEN=<token-locale-privato>
TRADEJOURNAL_BRIDGE_TOKEN=<token-ingestion-diverso>
TRADEJOURNAL_API_URL=http://<IP-MAC>:3000/api/mt5-events
DRY_RUN=false
POLL_INTERVAL_SECONDS=5

docker compose -f docker-compose.trade-sync-fake.yml config
docker compose -f docker-compose.trade-sync-fake.yml up -d --build
docker compose -f docker-compose.trade-sync-fake.yml ps
docker compose -f docker-compose.trade-sync-fake.yml logs -f trade-sync-worker
```

L'app deve ascoltare su un'interfaccia raggiungibile dalla VM Docker e il firewall del Mac deve
consentire la sola rete locale necessaria. Nei log devono comparire, una volta ciascuno e in
ordine: `trade_opened`, modifica SL, modifica TP, `trade_closed`, pending create/modify/cancel.
Verifiche operative:

```bash
# La colonna PORTS puo' mostrare solo `8080/tcp`, mai `0.0.0.0:...`/`127.0.0.1:...`:
# e' metadato interno dell'immagine, non una pubblicazione sull'host.
docker compose -f docker-compose.trade-sync-fake.yml ps

# Verifica inequivocabile: atteso {"8080/tcp":null}.
docker inspect $(docker compose -f docker-compose.trade-sync-fake.yml ps -q mt5-bridge-fake) \
  --format '{{json .NetworkSettings.Ports}}'

# Il fake resta allo stato corrente; il volume consente al worker di ripartire senza duplicare.
docker compose -f docker-compose.trade-sync-fake.yml restart trade-sync-worker
docker compose -f docker-compose.trade-sync-fake.yml logs --since=2m trade-sync-worker

# Arresto normale: conserva trade-sync-snapshot.
docker compose -f docker-compose.trade-sync-fake.yml down
```

`down -v` e' un reset distruttivo riservato a un nuovo test deliberato. Ricreare anche il fake
riporta la sua macchina a stati all'inizio; non equivale a un normale restart del solo worker.
Al primissimo avvio, se il bridge reale riporta una posizione gia' aperta e non esiste uno
snapshot persistito, il worker emette `trade_opened`: lo snapshot iniziale vuoto rappresenta
"mai osservato", non "ignora lo stato corrente". I successivi riavvii usano il file persistito.

### Successivo test su VPS Ubuntu AMD64

Questo test resta obbligatorio prima di dichiarare funzionante il runtime reale:

1. usare Ubuntu AMD64, un terminale separato e un account demo con
   `MT5_SESSION_MODE=login` e password investor, mai un account live;
2. installare terminale MT5 e Python Windows nello stesso `WINEPREFIX` e installare
   `bridge/windows/requirements.txt` con quel Python Windows;
3. avviare `bridge/windows/mt5_bridge.py` nello stesso prefix, con
   `MT5_LOGIN`/`MT5_PASSWORD`/`MT5_SERVER`, percorso terminale e `MT5_BRIDGE_TOKEN`
   nell'ambiente del solo bridge; non esporre la porta 8080 su Internet;
4. verificare da una rete privata `GET /health`, auth 401 con token errato e uno snapshot con
   ticket stringa/timestamp UTC/array vuoti; confrontare account, posizioni, pending e deal con
   il terminale;
5. avviare il worker Linux con `MT5_CLIENT_SOURCE=bridge`, URL privato del bridge, token bridge,
   token ingestion distinto e inizialmente `DRY_RUN=true`; verificare una sola POST snapshot per
   poll e nessuna credenziale/token nei log;
6. con un account demo aprire/modificare/chiudere una posizione e creare/modificare/cancellare un
   pending; solo dopo il dry-run impostare `DRY_RUN=false` verso un'API DEV;
7. riavviare soltanto il worker e verificare volume snapshot, event_id stabili, zero eventi
   duplicati e comportamento corretto dopo disconnessione/riconnessione.

Il core IPC e' gia' stato verificato localmente su macOS; il deployment VPS e il flusso HTTP
completo restano **prepared but unvalidated** fino al completamento di questa checklist.
Una chiusura parziale non e' rappresentata correttamente dal contratto attuale: il detector non
tratta ancora una riduzione di volume come chiusura parziale e questa fase non ne amplia il
contratto. Nell'app corrente `trade_opened` crea una notifica dedicata; modifica e chiusura
aggiornano il trade importato senza creare necessariamente una nuova notifica.

## Test locale in modalita' mock (senza Docker)

Richiede Python 3.11 (compatibile anche con 3.9+; testato anche con Python 3.9.6).

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env   # MOCK_MODE=true, DRY_RUN=true di default

cd worker
python main.py
```

Con `POLL_INTERVAL_SECONDS=5` (default), lo scenario mock completo (7 eventi) impiega circa
30-35 secondi a essere emesso interamente; dopo l'ultimo evento (`pending_order_cancelled`) il
worker resta in esecuzione senza generare altri eventi, finche' non viene fermato con `Ctrl+C`.

Sequenza simulata, in ordine:
1. `trade_opened`
2. `trade_modified` (modifica SL)
3. `trade_modified` (modifica TP)
4. `trade_closed`
5. `pending_order_created`
6. `pending_order_modified`
7. `pending_order_cancelled`

## Modalita' dry-run

Con `DRY_RUN=true` (default), il worker **non esegue alcuna chiamata di rete**: ogni evento
normalizzato viene stampato nel log, con `account_number` e `server` mascherati (es.
`12****78`, `Mo***********mo`). Nessuna password o token compare mai nei log, in nessuna forma.

Per validare il payload esatto che verrebbe inviato, ispezionare l'output di log: contiene il
dizionario completo del payload (sanitizzato solo su `account_number`/`server`).

## Invio verso un'API DEV

Per testare l'invio reale verso un ambiente di **sviluppo** di TradeJournal (mai produzione, mai
credenziali reali in questo repository):

```bash
# .env
MOCK_MODE=true
DRY_RUN=false
TRADEJOURNAL_API_URL=https://<il-tuo-ambiente-dev>/functions/v1/trading-mt5-events
TRADEJOURNAL_BRIDGE_TOKEN=<bridge token generato in Impostazioni -> Trading Accounts, ambiente DEV>
```

Il bridge token si genera dall'app TradeJournal (Impostazioni -> Trading Accounts), non da
questo repository. Non committare mai `.env` (e' in `.gitignore`).

Con `DRY_RUN=false`, `event_sender.py` esegue fino a 3 tentativi con backoff esponenziale
limitato (0.5s, 1s, 2s, cap a 8s) sui soli errori transitori (timeout, errori di rete, HTTP
5xx). Un HTTP 4xx (token invalido, payload rifiutato) non viene ritentato: e' un errore
permanente finche' non si corregge la configurazione.

## Build Docker

Il `docker/Dockerfile` e' multi-stage:
- **`mock`** (default): solo Python, nessuna dipendenza da Wine/MT5. E' lo stage usato da
  `docker-compose.mock.yml` e funziona su qualunque host con Docker (incluso macOS).
- **`real-mt5`**: Fase 2 (Wine + Xvfb + directory persistente per il terminale). Costruibile e
  con un entrypoint/healthcheck propri, ma richiede un terminale MT5 fornito manualmente e un
  test reale su Ubuntu amd64 per essere considerato pronto all'uso -- vedi "Fase 2: MT5 reale +
  Wine" piu' sotto.

```bash
# Fase 1 - build + avvio in mock
docker compose -f docker-compose.mock.yml build
docker compose -f docker-compose.mock.yml up
```

Oppure tramite gli script:

```bash
scripts/start.sh   # copia .env.example -> .env se assente, poi build+up del compose mock
scripts/stop.sh    # down del compose mock
```

Il container gira con un **utente non-root** (`worker`, uid 1000), ha un `HEALTHCHECK` basato su
un file di heartbeat aggiornato a ogni ciclo di poll (`worker/main.py:HEARTBEAT_FILE`,
verificato da `docker/healthcheck.sh`) e `restart: unless-stopped`.

## Stato del test su macOS

Questo POC e' stato verificato su macOS Apple Silicon con Colima e Docker CLI. In particolare:

- La modalita' mock (Fase 1) e' stata validata **direttamente in Python** (venv locale, 35 test
  unitari, esecuzione manuale del worker end-to-end con `DRY_RUN=true` per l'intera sequenza di
  7 eventi) — vedi la sezione "Verifiche eseguite" piu' sotto per l'esito esatto.
- I comandi Docker (`docker compose -f docker-compose.mock.yml config|build|up|down`) sono stati
  eseguiti con runtime Linux `aarch64`; build, healthcheck e arresto sono risultati corretti.
- L'app ufficiale MetaTrader 5 fornisce il proprio Wine prefix. Usando quel runtime e un Python
  Windows 3.11 AMD64 embeddable installato nello stesso prefix sono stati verificati realmente
  import di `MetaTrader5` e NumPy, `initialize(path=...)`, `terminal_info()`, `account_info()`,
  `positions_get()`, `orders_get()` e `shutdown()` contro il terminale principale gia' aperto.
- Questa prova convalida la compatibilita' IPC di base del bridge su Apple Silicon, non ancora
  l'intero server HTTP: restano da esercitare end-to-end `/v1/candles` e
  `/v1/trading/snapshot` (inclusi deal reali), riconnessioni e una sessione prolungata 24/48 ore.
  Il deployment separato/headless resta da provare su **Ubuntu AMD64**.

## Fase 2: MT5 reale + Wine

Sviluppata sul branch `feature/mt5-wine-runtime`, a partire dalla base mock gia' verificata
(Fase 1, invariata). Copre l'infrastruttura container (Wine, Xvfb, directory persistente,
entrypoint/healthcheck dedicati) e il client Python legacy (`RealMt5Client`). Il suo test
end-to-end containerizzato contro un terminale MT5 vero non e' stato eseguito e richiede una
VPS Ubuntu amd64. Questo limite riguarda il percorso legacy `direct`; il percorso bridge con
Python Windows ha invece superato la prova IPC di base sul Mac descritta sopra.

### Differenza tra mock e percorso `direct` legacy

| | Mock (Fase 1) | `direct` legacy (Fase 2) |
|---|---|---|
| Stage Dockerfile | `mock` | `real-mt5` |
| Compose | `docker-compose.mock.yml` | `docker-compose.yml` |
| Entrypoint | `docker/entrypoint.sh` | `docker/entrypoint-real.sh` (avvia anche Xvfb) |
| Healthcheck | `docker/healthcheck.sh` (heartbeat) | `docker/healthcheck-mt5.sh` (heartbeat + Xvfb) |
| Client MT5 | `mock_mt5_client.MockMt5Client` | `mt5_client.RealMt5Client` |
| Dipendenze | nessuna (solo Python) | Wine, Xvfb, terminale MT5 fornito manualmente |
| Architettura testata | qualunque (incluso arm64) | target `linux/amd64` |
| Credenziali | nessuna | `MT5_LOGIN`/`MT5_PASSWORD` (investor, sola lettura) |
| Stato | verificato end-to-end | infrastruttura pronta, connessione reale non testata |

### Requisiti VPS Ubuntu amd64

- Ubuntu 22.04/24.04 LTS, architettura **amd64** (non arm64: Wine + MT5 sotto emulazione non e'
  un percorso supportato per un uso prolungato).
- Almeno 2 vCPU / 4 GB RAM liberi per il container (i limiti in `docker-compose.yml` sono
  `cpus: 2.0` / `mem_limit: 2g`, indicativi: Wine + un terminale MT5 headless pesano
  sensibilmente di piu' del solo worker Python mock).
- Accesso di rete in uscita per il build (repository WineHQ, `pip install`) e per il worker
  (verso `TRADEJOURNAL_API_URL`).
- Nessun requisito grafico reale: Xvfb fornisce un display virtuale, non serve un desktop.

### Installazione Docker su Ubuntu

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"   # richiede un nuovo login per avere effetto
docker compose version            # verifica che il plugin compose sia incluso
```

(Su Ubuntu amd64 nativo non serve alcuna emulazione: a differenza del build da macOS ARM64
descritto sotto, `docker build`/`up` girano a velocita' piena.)

### Preparazione runtime MT5

Questo repository **non scarica e non include** alcun installer o binario MetaTrader 5: sarebbe
distribuzione non autorizzata di software proprietario. Nessun URL e' incluso nel Dockerfile per
questo scopo.

`docker-compose.yml` monta `./runtime/mt5:/opt/mt5` (bind mount host, cartella ignorata da git e
da Docker build context — vedi `.gitignore`/`.dockerignore`). Popolarla manualmente in uno dei
due modi:

1. **Installer ufficiale**: scaricare `mt5setup.exe` dal sito del proprio broker o da
   MetaQuotes sulla VPS, copiarlo in `./runtime/mt5/`, poi eseguirlo una volta sotto Wine dentro
   il container (comando manuale, non automatizzato da questo entrypoint):
   ```bash
   docker compose -f docker-compose.yml run --rm worker \
     wine /opt/mt5/mt5setup.exe /auto  # /auto = installazione silenziosa, se supportato dal broker
   ```
2. **Directory terminale gia' installata**: se si dispone gia' di un'installazione MT5
   funzionante (es. copiata da un'altra macchina Windows/Wine), copiare l'intero contenuto della
   cartella del terminale (deve contenere `terminal64.exe`) direttamente in `./runtime/mt5/`.

`docker/entrypoint-real.sh` verifica ad ogni avvio la presenza di `${MT5_TERMINAL_DIR}/terminal64.exe`
(default `/opt/mt5/terminal64.exe`) e logga un avviso chiaro se manca, senza bloccare l'avvio del
worker Python (che fallira' la connessione con un errore esplicito finche' il terminale non e'
presente).

Non versionare mai l'installer o il terminale in questo repository: `./runtime/` e' esclusa sia
da `.gitignore` sia da `.dockerignore`.

### Configurazione account demo

- Usare **sempre** un account **demo** per il primo test reale, mai un account live.
- In MT5, generare (o recuperare) la **investor password** (sola lettura) dell'account, dalle
  proprieta' dell'account nel terminale (Strumenti -> Opzioni -> Server, o dal report
  broker/MetaQuotes ID). **Non usare la password di trading/principale**: questo worker legge
  soltanto (account_info/posizioni/ordini/deal), non ha alcun bisogno di poter operare
  sull'account, e una password investor limita il danno anche in caso di fuga di credenziali
  (vedi anche `.env.example`, che riporta lo stesso promemoria, e `worker/main.py`, che lo
  logga all'avvio quando `MOCK_MODE=false`).
- Popolare `.env` (mai versionato) con `MT5_LOGIN`, `MT5_PASSWORD` (investor), `MT5_SERVER`
  (es. `MetaQuotes-Demo`).

### Avvio

```bash
cp .env.example .env
# Modificare .env: MOCK_MODE=false, MT5_LOGIN/MT5_PASSWORD (investor)/MT5_SERVER, DRY_RUN=true
# (consigliato per il primo test: valida la connessione MT5 senza ancora inviare eventi reali).

mkdir -p runtime/mt5   # popolata come descritto sopra, prima di 'up'

docker compose -f docker-compose.yml config   # valida l'interpolazione env/volumi
docker compose -f docker-compose.yml build    # build dello stage real-mt5 (target linux/amd64)
docker compose -f docker-compose.yml up
```

### Log

```bash
docker compose -f docker-compose.yml logs -f worker
```

All'avvio ci si aspetta, in ordine: `[entrypoint-real]` con lo stato di Xvfb e la presenza (o
assenza) del terminale, poi i log Python standard del worker (stesso formato della Fase 1). Come
in Fase 1, `MT5_LOGIN`/`MT5_PASSWORD`/`TRADEJOURNAL_BRIDGE_TOKEN` non compaiono mai in chiaro;
`account_number`/`server` sono sempre mascherati.

### Healthcheck

`docker/healthcheck-mt5.sh` verifica **due** condizioni (a differenza della Fase 1, che ne
verifica solo una): il file di heartbeat del worker Python aggiornato di recente **e** il
processo Xvfb ancora in esecuzione. Se Xvfb muore, il container viene marcato `unhealthy` anche
se il processo Python e' ancora vivo, perche' senza display virtuale Wine (e quindi il
terminale) smette di funzionare.

```bash
docker inspect --format '{{.State.Health.Status}}' tradejournal-mt5-cloud-worker
```

### Benchmark

```bash
scripts/benchmark.sh --real
```

Stessa logica della Fase 1 (campiona CPU/RAM ripetutamente via `docker stats`), ma punta al
compose reale. Attendersi consumi piu' alti: Wine + un terminale MT5 headless, anche senza
posizioni aperte, pesano piu' del solo worker Python.

### Checklist test 24/48 ore (specifica per la modalita' reale)

Oltre alla checklist generica di Fase 1 (vedi sopra, resta valida anche qui), per il primo test
prolungato contro un terminale MT5 reale:

- [ ] Il terminale MT5 resta connesso al broker per l'intera finestra (verificare dal terminale
      stesso, non solo dall'healthcheck del container: l'healthcheck verifica Xvfb+worker, non
      lo stato della connessione broker-terminale).
- [ ] `RealMt5Client.reconnect()` viene esercitato almeno una volta (es. riavviando il container
      o simulando una disconnessione di rete) e il worker si riprende senza intervento manuale.
- [ ] Nessun evento duplicato generato da `get_recent_deals()` (finestra scorrevole di 24h, vedi
      `mt5_client.py:RealMt5Client.DEAL_LOOKBACK_HOURS`): verificare a campione lato TradeJournal
      che ogni chiusura compaia una sola volta.
- [ ] Uso di RAM/CPU di Wine stabile nel tempo (Wine non e' immune a leak su esecuzioni molto
      lunghe): campionare con `scripts/benchmark.sh --real` a intervalli regolari.
- [ ] `MT5_PASSWORD` in uso e' effettivamente la investor password (verificare dal terminale MT5
      che l'account sia in sola lettura), non quella di trading.

### Limiti noti Wine/MT5

- **Il pacchetto Python `MetaTrader5` e' un'estensione nativa Windows.** Funziona in modo
  affidabile solo quando il processo Python che lo importa gira anch'esso dentro Wine (Python
  Windows eseguito via `wine python.exe`), non come processo Python Linux nativo che parla con
  un terminale Wine "esterno". Questo Dockerfile/worker eseguono oggi un Python Linux nativo
  (`python:3.11-slim`) e quindi il percorso `direct` non puo' importare quel pacchetto. Il
  percorso raccomandato risolve il confine tramite il bridge Python-Windows dedicato descritto
  piu' sotto; `RealMt5Client._import_mt5()` continua a fallire con un errore chiaro nel runtime
  Linux legacy.
- **Build riuscita su macOS Apple Silicon (Colima, emulazione QEMU `linux/amd64`), ma resta solo
  un test d'infrastruttura.** `docker compose -f docker-compose.yml config` e `build` sono stati
  validati con successo (Wine `11.0.0.0~bookworm-1` installato correttamente, ~230s sotto
  emulazione), e un `docker compose run --rm worker` senza credenziali ha confermato che Xvfb si
  avvia, l'assenza di `terminal64.exe` viene segnalata chiaramente, `RealMt5Client.connect()`
  fallisce in modo pulito e senza segreti nei log, e l'entrypoint propaga l'exit code
  correttamente. Questo valida l'infrastruttura del container, **non** la connessione IPC a un
  terminale MT5 vero del percorso container `direct` — vedi "Verifiche eseguite" per il
  dettaglio. Il test definitivo va comunque rifatto nativamente su una VPS Ubuntu amd64 (build
  nativa piu' veloce, e soprattutto un terminale MT5 reale da fornire).
- **Xvfb stampa un avviso innocuo in fase di avvio da utente non-root**
  (`_XSERVTransmkdir: ERROR: euid != 0, directory /tmp/.X11-unix will not be created`), osservato
  durante la verifica sopra: il display resta comunque utilizzabile (confermato via
  `xdpyinfo`), ma l'avviso compare ad ogni avvio del container ed e' bene non scambiarlo per un
  errore bloccante quando si legge `docker compose logs`.
- **Nessuna installazione automatica del terminale.** `docker/entrypoint-real.sh` avvia Xvfb e
  verifica la presenza di `terminal64.exe`, ma non esegue l'installer ne' avvia il terminale: e'
  un passo manuale (vedi "Preparazione runtime MT5").
- **Gestione dei segnali dell'entrypoint e' best-effort.** `entrypoint-real.sh` inoltra
  `SIGTERM`/`SIGINT` al processo worker e poi ferma Xvfb, ma non e' stata validata contro un
  arresto reale del terminale MT5 sotto Wine (che potrebbe richiedere una chiusura piu' garbata
  per evitare file di configurazione corrotti).
- **Nessuna gestione multiutente/orchestrazione**, invariato dalla Fase 1: un solo
  processo/account per container.

## Modalita' research (dati di mercato)

Modalita' **privata**, disattiva per default, pensata per raccogliere candele OHLC e salvarle su
un Postgres locale alla macchina Ubuntu -- **mai** per le installazioni cliente. Non tocca in
alcun modo la sincronizzazione dei trade verso TradeJournal (sezioni precedenti di questo
README): sono due processi Docker separati, con due compose file separati.

**Cosa NON e' (ancora) questa fase**: nessuna AI, nessun Bias Engine, nessun pattern recognition,
nessuna connessione a Supabase e nessun deploy cloud. La sorgente `mt5` parla con un servizio
bridge separato via HTTP (vedi "mt5-bridge" piu' sotto), che nei test automatici resta un *fake*
deterministico (nessuna dipendenza da Wine/MT5, gira anche su Ubuntu ARM64). Del bridge reale
(`bridge/windows/mt5_bridge.py`) e' stata verificata localmente su macOS la connessione IPC di
base al terminale; l'intero percorso research via `/v1/candles`, lo snapshot HTTP e la stabilita'
prolungata non sono ancora stati validati contro dati reali.

### Principio di isolamento

- `APP_MODE` accetta solo `client` (default) o `research`; qualunque altro valore fa fallire
  l'avvio con un errore esplicito (vedi `worker/config.py:Config.__post_init__`).
- `ENABLE_MARKET_DATA=true` e' valido solo insieme ad `APP_MODE=research`: impostarlo con
  `APP_MODE=client` fa fallire l'avvio, non viene silenziosamente ignorato.
- Il market-data-worker (`worker/market_data_main.py`) si rifiuta di partire se
  `ENABLE_MARKET_DATA` non e' `true`: e' un processo dedicato, non ha altro scopo.
- **Garanzia piu' forte di un flag**: Postgres e il market-data-worker sono definiti
  *esclusivamente* in `docker-compose.research.yml`, un file compose separato mai incluso di
  default. Un'installazione cliente che avvia solo `docker-compose.mock.yml` (o
  `docker-compose.yml`) non vede questi servizi nel proprio compose graph: non c'e' un flag da
  disattivare per errore, il servizio semplicemente non esiste per quel deployment.
- Il trade-sync worker (`main.py`) non importa mai nessuno dei moduli `market_data_*`: un guasto
  o un rallentamento nella raccolta dati di mercato non puo' propagarsi alla sincronizzazione dei
  trade, e viceversa.

### Nuove variabili d'ambiente (vedi `.env.example`)

| Variabile | Default | Note |
|---|---|---|
| `MT5_CLIENT_SOURCE` | derivato da `MOCK_MODE` | trade-sync: `mock`, `bridge` (raccomandato con MT5 reale) o `direct` legacy |
| `APP_MODE` | `client` | `client` o `research`, nessun altro valore |
| `ENABLE_MARKET_DATA` | `false` | richiede `APP_MODE=research` |
| `DATABASE_URL` | *(vuoto)* | richiesto se `ENABLE_MARKET_DATA=true`; con Docker Compose e' costruita automaticamente dal servizio `market-data-worker`, non va impostata a mano |
| `MARKET_SYMBOLS` | `EURUSD` | lista separata da virgola |
| `MARKET_TIMEFRAMES` | `M1,M5,M15,H1,H4,D1` | lista separata da virgola |
| `MARKET_DATA_POLL_SECONDS` | `60` | indipendente da `POLL_INTERVAL_SECONDS` (solo trade-sync) |
| `MARKET_DATA_SOURCE` | `mock` | `mock` (in-process) o `mt5` (client HTTP verso mt5-bridge, vedi sotto) |
| `MT5_BRIDGE_URL` | *(vuoto)* | richiesto se `MT5_CLIENT_SOURCE=bridge` o `MARKET_DATA_SOURCE=mt5`; es. `http://mt5-bridge-fake:8080` |
| `MT5_BRIDGE_TOKEN` | *(vuoto)* | richiesto per i client bridge; stesso valore sul worker e sul bridge, distinto dal token ingestion |
| `MT5_BRIDGE_TIMEOUT_SECONDS` | `10` | timeout per singola richiesta HTTP al bridge |
| `MT5_DEAL_LOOKBACK_HOURS` | `24` | trade-sync snapshot; intero 1..168 |
| `MT5_SESSION_MODE` | `login` | bridge reale: `login` oppure `existing`; valori diversi bloccano l'avvio |
| `MT5_TERMINAL_PATH` | *(vuoto)* | bridge reale: obbligatorio in `existing`, opzionale (raccomandato) in `login` |
| `MT5_EXPECTED_LOGIN` / `MT5_EXPECTED_SERVER` | *(vuoto)* | controlli opzionali della sessione gia' aperta in modalita' `existing` |
| `EURUSD_BROKER_SYMBOL` | `EURUSD` | simbolo con cui il bridge interroga MT5 (puo' differire dal canonico, es. `EURUSD.a`) |
| `POSTGRES_DB` / `POSTGRES_USER` / `POSTGRES_PASSWORD` | `tradejournal_research` / `research` / *(nessun default)* | credenziali del Postgres locale in `docker-compose.research.yml`; `POSTGRES_PASSWORD` e' obbligatoria, `up`/`config` falliscono subito se mancante |

Con `MT5_CLIENT_SOURCE=bridge`, `MT5_SESSION_MODE`, le variabili `MT5_EXPECTED_*` e
`MT5_TERMINAL_PATH` appartengono esclusivamente al servizio `mt5-bridge` reale
(`bridge/windows/mt5_bridge.py`), mai ai worker Linux. `MT5_LOGIN` / `MT5_PASSWORD` /
`MT5_SERVER` sono richieste al bridge soltanto in modalita' `login`; il percorso legacy `direct`
continua a leggere queste tre variabili nel trade-sync worker.

### Due sorgenti dati: mock (in-process) e mt5 (bridge HTTP)

`MockMarketDataSource` (`worker/market_data_source.py`) e' **deterministica**: stesso
symbol/timeframe/indice di candela producono sempre esattamente lo stesso valore (nessun
`random`, nessuno stato condiviso tra chiamate). Genera candele OHLC sempre coerenti (`high` e'
sempre il massimo, `low` sempre il minimo) per qualunque combinazione di simboli/timeframe, e
supporta la simulazione di un buco temporale (`gap_indices`) per verificare che il worker non si
blocchi ne' inventi dati quando una barra manca dalla sorgente. Gira interamente dentro il
processo `market_data_main.py`, nessuna rete coinvolta.

`Mt5MarketDataSource` (`MARKET_DATA_SOURCE=mt5`) e' un **client HTTP**: parla con un servizio
esterno chiamato `mt5-bridge` (vedi la sezione dedicata subito sotto), non importa mai il
pacchetto `MetaTrader5` ne' apre connessioni Wine/IPC. Non nasconde mai un errore dietro una
lista vuota: timeout, autenticazione fallita, payload non conforme al contratto o OHLC
incoerente sollevano `Mt5BridgeError`/`Mt5BridgeAuthError` in modo esplicito
(`worker/market_data_main.py` intercetta l'eccezione per singola coppia simbolo/timeframe,
logga un errore e ritenta al ciclo di poll successivo, senza mai far cadere l'intero processo).

## mt5-bridge

Il pacchetto Python ufficiale `MetaTrader5` e' un'estensione nativa **Windows**: comunica via IPC
direttamente con il processo del terminale MetaTrader 5. **Non e' importabile da un interprete
Python Linux**, nemmeno dentro lo stesso container Wine, se il processo Python che lo importa non
e' anch'esso un Python Windows (stesso limite gia' descritto per `RealMt5Client` in "Fase 2: MT5
reale + Wine" piu' sopra). Per questo il market-data-worker Linux **non tenta mai** di importare
`MetaTrader5`: parla con un servizio HTTP separato, `mt5-bridge`, che nella sua forma reale gira
come **Windows Python sotto Wine**, nello stesso `WINEPREFIX` del terminale MT5.

Tre pezzi, stesso contratto HTTP (vedi `bridge/common.py`):

| Servizio | Codice | Dove gira | Dipendenze | Stato |
|---|---|---|---|---|
| `mt5-bridge-fake` | `bridge/fake/fake_bridge.py` | Qualunque architettura, incluso Ubuntu ARM64 | Solo standard library | **Funzionante**, usato per i test |
| `mt5-bridge` (reale) | `bridge/windows/mt5_bridge.py` | Windows Python sotto Wine (macOS ufficiale o VPS AMD64) | Pacchetto `MetaTrader5` | **IPC base validato su macOS; HTTP completo da validare** |
| Client Linux | `worker/market_data_source.py:Mt5MarketDataSource` | market-data-worker (qualunque architettura) | `requests` (gia' presente) | **Funzionante** |

### Contratto API

Autenticazione Bearer (`MT5_BRIDGE_TOKEN`) su **tutti** gli endpoint. JSON UTF-8. Nessun
endpoint di trading esiste (ne' `/v1/order_send` ne' equivalenti): il bridge e' di sola lettura.

```
GET /health
  -> 200 {"status": "ok", "terminal_connected": true, "account_connected": true,
          "server": "<nome server sanitizzato>", "version": "<versione terminale>"}

POST /v1/candles
  <- {"symbol": "EURUSD", "timeframe": "M5", "since": "2026-07-12T10:00:00Z"|null, "limit": 500}
  -> 200 {"symbol": "EURUSD", "timeframe": "M5", "candles": [
            {"open_time": "2026-07-12T10:05:00Z", "open": "1.17001", "high": "1.17045",
             "low": "1.16990", "close": "1.17030", "tick_volume": 122, "spread": 8,
             "source": "mt5"}
          ]}
  -> 401 {"error": {"code": "unauthorized", "message": "..."}}          token mancante/errato
  -> 422 {"error": {"code": "unsupported_symbol"|"unsupported_timeframe"|..., "message": "..."}}
  -> 502 {"error": {"code": "mt5_error", "message": "..."}}             terminale MT5 non risponde

POST /v1/trading/snapshot
  <- {"deal_lookback_hours": 24}
  -> 200 {"account": {...}, "positions": [...], "orders": [...], "deals": [...],
          "generated_at": "2026-07-13T10:15:01Z"}
```

Regole applicate dal bridge (fake e reale, stesso `bridge/common.py`): solo `EURUSD`/il broker
symbol configurato; solo i sei timeframe `M1,M5,M15,H1,H4,D1`; `limit` troncato a un massimo
protetto lato server (1000) indipendentemente da cosa chiede il client; `since` **esclusivo**
(candele con `open_time` strettamente maggiore); candele sempre in ordine cronologico crescente;
timestamp sempre UTC (suffisso `Z`); prezzi sempre come **stringhe** decimali, mai numeri JSON
(evita qualunque arrotondamento binario intermedio); la candela ancora in formazione al momento
della richiesta non e' mai inclusa.

`Mt5MarketDataSource` applica comunque una difesa in profondita' lato client (non si fida
ciecamente del bridge): riordina le candele, scarta quelle con `open_time <= since`, tronca al
`limit` richiesto, e valida `Decimal`/UTC/coerenza OHLC su ogni candela ricevuta.

### mt5-bridge-fake (test locali, qualunque architettura)

```bash
cp .env.example .env
# Impostare in .env: POSTGRES_PASSWORD e MT5_BRIDGE_TOKEN (nessun default per nessuno dei due).
# MARKET_DATA_SOURCE=mt5 (il default del file .env.example resta "mock": va cambiato per usare
# il bridge invece della sorgente in-process).

docker compose -f docker-compose.mock.yml -f docker-compose.research.yml \
  -f docker-compose.research-mt5-fake.yml up -d --build
```

Avvia **quattro** container: `worker` (trade-sync, invariato), `postgres`, `mt5-bridge-fake`
(candele sintetiche deterministiche per EURUSD sui sei timeframe, nessuna dipendenza da Wine/MT5)
e `market-data-worker` (ora configurato per parlare con `mt5-bridge-fake` invece che con
`MockMarketDataSource`). `docker-compose.research-mt5-fake.yml` va sempre combinato con
`docker-compose.research.yml`: da solo non definisce ne' `postgres` ne' l'intero
`market-data-worker`, solo `mt5-bridge-fake` e alcune chiavi aggiuntive per `market-data-worker`
(vedi commenti in quel file).

`mt5-bridge-fake` **non pubblica alcuna porta verso l'host**: raggiungibile solo dalla rete
Docker interna del compose, stesso principio gia' applicato a `postgres`.

Il fake bridge simula anche gli scenari di errore di un bridge reale (401 con token errato,
timeout, errore MT5, payload malformato, candela duplicata) tramite un header di test
(`X-Mt5-Fake-Scenario`) usato **solo** dai test automatici (`tests/test_fake_bridge.py`): il
client di produzione non lo invia mai.

### mt5-bridge reale: modalita' di sessione

`bridge/windows/mt5_bridge.py` implementa lo stesso contratto usando il pacchetto `MetaTrader5`
reale. `MT5_SESSION_MODE` accetta soltanto i due valori seguenti; il default conservativo e'
`login` e qualunque altro valore blocca l'avvio con un errore esplicito.

- `MT5_SESSION_MODE=existing`: richiede `MT5_TERMINAL_PATH`, che deve indicare `terminal64.exe`
  nello stesso `WINEPREFIX` del Python Windows. Esegue
  `mt5.initialize(path=MT5_TERMINAL_PATH)` e usa l'account gia' collegato nel terminale. Non
  chiama mai `mt5.login()`, non richiede `MT5_PASSWORD` e non cambia account o tipo di sessione.
  Dopo l'inizializzazione `account_info()` deve restituire un account, altrimenti l'avvio
  fallisce. `MT5_EXPECTED_LOGIN` e `MT5_EXPECTED_SERVER` sono
  controlli opzionali: se impostati devono coincidere rispettivamente con `account_info().login`
  e, con confronto esatto, `account_info().server`. E' la modalita' adatta al test locale con il
  terminale principale gia' aperto e collegato.
- `MT5_SESSION_MODE=login`: mantiene il comportamento storico, eseguendo `initialize()` e poi
  `mt5.login()`; richiede `MT5_LOGIN`, `MT5_PASSWORD` e `MT5_SERVER`. `MT5_TERMINAL_PATH` resta
  opzionale: se assente `initialize()` viene chiamato senza `path`, anche se specificarlo e'
  raccomandato per selezionare il terminale corretto. Va usata con un terminale separato, per
  esempio su una VPS, e `MT5_PASSWORD` deve essere esclusivamente la password **investor** (sola
  lettura), mai quella principale/di trading.

Dopo la connessione il bridge offre snapshot read-only via `account_info()`/`positions_get()`/
`orders_get()`/`history_deals_get()`, selezione del broker symbol e lettura candele tramite
`copy_rates_from_pos` o `copy_rates_range`; `shutdown()` chiude la connessione. Login, server,
token e altri valori sensibili non vengono stampati integralmente. Non esistono chiamate
`order_send`, `order_check`, `TRADE_ACTION_*` o endpoint che aprono, modificano o chiudono
ordini: gli unici endpoint restano `GET /health`, `POST /v1/candles` e
`POST /v1/trading/snapshot`.

#### Test locale macOS con il terminale gia' aperto

L'installazione ufficiale MetaTrader 5 su macOS usa questi valori, gia' configurati come default
in `scripts/run_mt5_bridge_macos.sh`:

```text
WINEPREFIX=$HOME/Library/Application Support/net.metaquotes.wine.metatrader5
WINE_BIN=/Applications/MetaTrader 5.app/Contents/SharedSupport/wine/bin/wine
PYTHON_WINDOWS_PATH=C:\Python311Embed\python.exe
MT5_TERMINAL_PATH=C:\Program Files\MetaTrader 5\terminal64.exe
```

Il Python Windows 3.11 AMD64 embeddable e il terminale devono appartenere allo stesso
`WINEPREFIX`. Il test locale usa la sessione principale gia' collegata senza trasformarla in una
sessione investor e senza effettuare un nuovo login. Configurazione di esempio richiesta al
processo bridge:

```dotenv
MT5_SESSION_MODE=existing
MT5_TERMINAL_PATH=C:\Program Files\MetaTrader 5\terminal64.exe
MT5_EXPECTED_LOGIN=<login>
MT5_EXPECTED_SERVER=<server esatto>
MT5_BRIDGE_TOKEN=<token locale>
HOST=0.0.0.0
PORT=8090
```

Esportare le variabili nella shell (quotare i percorsi Windows e quelli macOS con spazi) e poi
avviare:

```bash
export MT5_SESSION_MODE=existing
export MT5_TERMINAL_PATH='C:\Program Files\MetaTrader 5\terminal64.exe'
export MT5_EXPECTED_LOGIN='<login>'
export MT5_EXPECTED_SERVER='<server esatto>'
export MT5_BRIDGE_TOKEN='<token locale>'
export HOST=0.0.0.0
export PORT=8090

scripts/run_mt5_bridge_macos.sh
```

Lo script non contiene credenziali, usa soltanto le variabili gia' esportate, verifica Wine,
Python Windows e file del bridge, e mostra solo configurazioni non sensibili o mascherate.
`WINEPREFIX`, `WINE_BIN` e `PYTHON_WINDOWS_PATH` possono essere sovrascritti nell'ambiente. Con
`HOST=0.0.0.0` proteggere la porta tramite firewall e limitarla alla rete privata; per un test
solo locale preferire `HOST=127.0.0.1`.

Sul Mac Apple Silicon reale sono gia' riusciti import `MetaTrader5`/NumPy,
`initialize(path=...)`, `terminal_info()`, `account_info()`, `positions_get()`, `orders_get()` e
`shutdown()`. Questo dimostra che Python Windows e il terminale comunicano nello stesso prefix;
non equivale ancora alla validazione di richieste HTTP complete a `/v1/candles` e
`/v1/trading/snapshot`, di `history_deals_get()` con uscite reali, delle riconnessioni o di una
prova continuativa 24/48 ore.

#### Deployment separato/VPS in modalita' login

Il bridge non e' ancora impacchettato in un'immagine Docker o compose dedicato. Su una VPS
Ubuntu **AMD64**, predisporre Wine, terminale MT5 e Python Windows nello stesso `WINEPREFIX`,
installare `bridge/windows/requirements.txt` con quel Python e avviare il bridge con
`MT5_SESSION_MODE=login`, `MT5_LOGIN`, `MT5_PASSWORD` investor, `MT5_SERVER`,
`MT5_BRIDGE_TOKEN` e, raccomandato ma opzionale in questa modalita', `MT5_TERMINAL_PATH`
nell'ambiente del solo processo bridge. Il worker Linux deve ricevere soltanto URL e token del
bridge. Esporre la porta esclusivamente su loopback o rete privata, mai direttamente su Internet.

### Schema database

Definito interamente in `db/migrations/0001_initial_schema.sql`, applicato da
`worker/db_migrate.py` all'avvio del market-data-worker (nessuna DDL sparsa nel codice Python;
tracciamento delle migration gia' applicate in una tabella `schema_migrations`).

```
market_symbols
  id (PK), canonical_symbol, broker_symbol, source, enabled, created_at, updated_at
  UNIQUE (canonical_symbol, broker_symbol, source)

market_candles
  id (PK), symbol_id (FK -> market_symbols), timeframe, open_time (TIMESTAMPTZ, UTC),
  open, high, low, close (NUMERIC(18,8)), tick_volume (BIGINT, nullable),
  spread (INTEGER, nullable), source, created_at, updated_at
  CHECK (high >= open, close, low  AND  low <= open, close)
  UNIQUE INDEX (symbol_id, timeframe, open_time)  -- dedup + lookup/checkpoint
```

Prezzi in `NUMERIC(18,8)`, non float/double: un float binario introduce errori di arrotondamento
non deterministici sulle ultime cifre decimali, inaccettabili per uno storico salvato in modo
permanente. Il checkpoint per riprendere il polling dopo un riavvio **non** e' una tabella
separata: si deriva con `MAX(open_time)` per `(symbol_id, timeframe)`, cosi' lo schema resta
minimo e non puo' mai disallinearsi dai dati realmente salvati (vedi
`worker/market_data_store.py:get_checkpoint`).

L'upsert (`worker/market_data_store.py:upsert_candles`) usa `ON CONFLICT (symbol_id, timeframe,
open_time) DO UPDATE`: salvare due volte la stessa candela aggiorna la riga esistente, non la
duplica -- e' il meccanismo alla base sia della deduplicazione sia della ripresa senza perdita di
checkpoint dopo un riavvio del collector.

### Avvio locale (research)

```bash
cp .env.example .env
# Modificare .env: impostare POSTGRES_PASSWORD (nessun default, vedi sopra). Tutto il resto puo'
# restare ai valori di default per una prima prova locale (mock + mock).

docker compose -f docker-compose.mock.yml -f docker-compose.research.yml up -d --build
docker compose -f docker-compose.mock.yml -f docker-compose.research.yml ps
```

Avvia **tre** container: `tradejournal-mt5-cloud-worker-mock` (trade-sync, invariato),
`tradejournal-mt5-research-postgres` (Postgres locale, nessuna porta esposta verso l'host) e
`tradejournal-mt5-market-data-worker` (raccoglie candele mock e le salva su Postgres).

Per usare `MARKET_DATA_SOURCE=mt5` contro il fake bridge invece del mock in-process, vedi
"mt5-bridge-fake (test locali, qualunque architettura)" piu' sopra: stesso comando con in piu'
`-f docker-compose.research-mt5-fake.yml` (quarto container, `mt5-bridge-fake`).

### Stop

```bash
docker compose -f docker-compose.mock.yml -f docker-compose.research.yml down
```

Senza `-v`: il volume Postgres (`mt5-research-postgres-data`) **non** viene rimosso, i dati
restano tra un `down`/`up` successivo (vedi "Persistenza" piu' sotto). Aggiungere `-v` solo se si
vuole ripartire da un database vuoto deliberatamente. Con il fake bridge incluso, aggiungere
allo stesso modo `-f docker-compose.research-mt5-fake.yml` sia a `down` sia a ogni comando
successivo di questa sezione (`logs`/`exec`/...): i file `-f` vanno sempre combinati assieme.

### Verifica log / healthcheck

```bash
docker compose -f docker-compose.mock.yml -f docker-compose.research.yml logs market-data-worker
docker compose -f docker-compose.mock.yml -f docker-compose.research.yml logs postgres
# Con il fake bridge incluso (-f docker-compose.research-mt5-fake.yml), anche:
#   docker compose ... logs mt5-bridge-fake

docker inspect --format '{{.State.Health.Status}}' tradejournal-mt5-market-data-worker
docker inspect --format '{{.State.Health.Status}}' tradejournal-mt5-research-postgres
docker inspect --format '{{.State.Health.Status}}' tradejournal-mt5-bridge-fake
```

Ci si aspetta, in ordine nei log del market-data-worker: avvio, applicazione delle migration
(`Applico migration 0001_initial_schema...` alla primissima esecuzione, nessun log alle
successive perche' gia' applicata), `Backfill iniziale...` con un conteggio di candele per ogni
simbolo/timeframe, poi `Sync <symbol>/<timeframe>: N nuove candele.` a ogni ciclo di poll
successivo. **Mai** `DATABASE_URL` in chiaro: solo `app_mode`/simboli/timeframe/conteggi.

### Verifica righe salvate

```bash
docker compose -f docker-compose.mock.yml -f docker-compose.research.yml exec postgres \
  psql -U research -d tradejournal_research -c \
  "SELECT s.canonical_symbol, c.timeframe, COUNT(*), MAX(c.open_time) \
   FROM market_candles c JOIN market_symbols s ON s.id = c.symbol_id \
   GROUP BY 1, 2 ORDER BY 1, 2;"
```

Rilanciando lo stesso container (`docker compose ... restart market-data-worker`) il conteggio
per ogni riga non deve mai diminuire ne' saltare all'indietro: il collector riprende dal
checkpoint (`MAX(open_time)`) letto da Postgres, non da uno stato interno al processo.

### Backup / ripristino del volume Postgres

Backup logico (consigliato per questa fase, indipendente dalla versione di Postgres):

```bash
docker compose -f docker-compose.mock.yml -f docker-compose.research.yml exec postgres \
  pg_dump -U research -d tradejournal_research > research_backup_$(date +%Y%m%d).sql
```

Ripristino su un volume vuoto:

```bash
cat research_backup_YYYYMMDD.sql | docker compose -f docker-compose.mock.yml \
  -f docker-compose.research.yml exec -T postgres psql -U research -d tradejournal_research
```

Backup a livello di volume Docker (copia grezza dei file, utile per uno snapshot completo prima
di un aggiornamento):

```bash
docker run --rm -v tradejournal-mt5-cloud-worker_mt5-research-postgres-data:/data \
  -v "$(pwd)":/backup alpine \
  tar czf /backup/postgres_volume_$(date +%Y%m%d).tar.gz -C /data .
```

(Il nome esatto del volume dipende dal nome della directory del progetto: verificarlo con
`docker volume ls | grep postgres-data`.)

### Limitazioni note

- **Il percorso MT5 reale non e' ancora validato end-to-end.** Il bridge reale e' stato eseguito
  nel runtime ufficiale macOS fino alle letture IPC di account, terminale, posizioni e ordini;
  non sono ancora stati verificati contro dati reali il server HTTP, `/v1/candles`, lo snapshot
  completo (inclusi deal) e il salvataggio research. I test automatici research continuano a
  usare `MARKET_DATA_SOURCE=mock` o `MARKET_DATA_SOURCE=mt5` contro il fake bridge.
- **Il bridge reale non ha ancora un'immagine Docker/compose dedicati**: richiede prima Wine +
  Windows Python + il pacchetto `MetaTrader5` installati manualmente sotto lo stesso `WINEPREFIX`
  del terminale (vedi "mt5-bridge reale: modalita' di sessione" piu' sopra) -- impacchettarlo in
  un Dockerfile non validabile in questa sessione avrebbe rischiato di sembrare "pronto" senza
  esserlo davvero.
- **Nessuna limitazione Wine/ARM64 per la modalita' research in se'** (ne' per lo stage
  `research-market-data` ne' per `mt5-bridge-fake`): entrambi sono Python standard library/stdlib
  HTTP, nessuna dipendenza da Wine, girano nativamente su Ubuntu ARM64 (e su qualunque altra
  architettura con Docker). Il limite AMD64 resta per `RealMt5Client`/stage `real-mt5` e per il
  Python Windows del bridge; sul Mac Apple Silicon quest'ultimo gira attraverso il Wine fornito
  dall'app ufficiale, non come binario ARM64 nativo.
- Un solo processo market-data-worker e un solo bridge per deployment (nessuna orchestrazione
  multi-istanza), coerente con il limite gia' noto del trade-sync worker.
- Il mock/fake bridge non riproducono condizioni di errore realistiche della fonte dati (simboli
  non disponibili, storico incompleto lato broker, riconnessioni broker-terminale): simulano un
  insieme scelto di scenari (gap, timeout, errore MT5, payload malformato, candela duplicata),
  sufficiente per validare dedup/checkpoint/gestione errori del client, non il comportamento
  completo di un broker reale.
- Scope volutamente ristretto a **EURUSD** (`EURUSD_BROKER_SYMBOL` per il mapping broker) sui sei
  timeframe supportati: nessun altro asset e' previsto in questa fase, ne' lato bridge ne' lato
  mapping canonical/broker in `worker/market_data_main.py:_broker_symbol`.

## Checklist 24/48 ore

Da eseguire quando il worker gira per un periodo prolungato (mock o reale) per verificarne la
stabilita' prima di qualunque uso non-POC. Valida per entrambe le modalita'; la sezione "Fase 2:
MT5 reale + Wine" piu' sopra aggiunge una checklist specifica per il test con MT5 vero:

- [ ] Il container resta `healthy` per l'intera finestra (`docker inspect --format
      '{{.State.Health.Status}}' <container>`), senza restart involontari
      (`docker inspect --format '{{.RestartCount}}' <container>`).
- [ ] RAM e CPU restano stabili nel tempo (nessun trend di crescita continua): campionare con
      `scripts/benchmark.sh` a intervalli regolari (es. ogni 2-4 ore) e confrontare i valori.
- [ ] Il file di log non cresce in modo abnorme (`docker logs <container> | wc -l` nel tempo) e
      non contiene mai stringhe di password/token (grep negativo, vedi "Verifiche finali").
- [ ] In `DRY_RUN=false` verso un'API DEV: nessun evento duplicato lato TradeJournal (l'API fa
      dedup su `event_id`; verificare a campione che i retry non abbiano creato righe doppie).
- [ ] Il worker sopravvive a un riavvio del container (`docker compose restart`) senza crash al
      boot e senza rigenerare eventi gia' inviati (in modalita' reale, grazie a
      `snapshot_store.py`; in modalita' mock lo scenario riparte sempre da zero per design).
- [ ] Nessuna credenziale reale e' mai finita in `.env` versionato, log, o commit
      (`git log -p | grep -i` su pattern di password/token, vedi sotto).

## Comandi utili per RAM/CPU

```bash
# Snapshot singolo (mock)
docker stats --no-stream tradejournal-mt5-cloud-worker-mock

# Snapshot singolo (reale, Fase 2)
docker stats --no-stream tradejournal-mt5-cloud-worker

# Campionamento ripetuto via script incluso
scripts/benchmark.sh          # compose mock (default)
scripts/benchmark.sh --real   # compose reale (Fase 2)

# Limiti configurati nel compose mock: mem_limit 256m, cpus 0.50 (docker-compose.mock.yml)
# Limiti nel compose reale: mem_limit 2g, cpus 2.0 (docker-compose.yml, indicativi -- vedi
# "Fase 2: MT5 reale + Wine")
```

## Rischi noti

- **Percorso legacy `direct` non testato end-to-end.** Lo stage `real-mt5`, `RealMt5Client` e
  `docker-compose.yml` sono implementati, con retry limitato e mapping dei campi verificati via
  test unitari (modulo `MetaTrader5` finto, vedi `tests/test_mt5_client.py`), ma il worker Python
  Linux nativo non puo' importare l'estensione Windows. La connessione IPC reale e' stata invece
  esercitata dal Python Windows del bridge sul Mac; vedi "mt5-bridge reale" per il perimetro.
- **Nessuna gestione multiutente/orchestrazione**, nessun Kubernetes, nessuna persistenza di
  credenziali su database: questo worker e' pensato per un singolo processo/singolo account,
  come da requisiti di questo POC.
- **Il mock non copre scenari di errore MT5** (disconnessioni, simboli non tradabili, margine
  insufficiente): simula solo il percorso "felice" richiesto (le 7 fasi elencate sopra).
- **cpus/mem_limit nei compose file** sono limiti indicativi; vanno ricalibrati per l'hardware
  reale di destinazione (laptop di sviluppo per il mock, VPS Ubuntu per il reale) prima di un
  uso prolungato.
- **mt5-bridge reale validato solo a livello IPC di base.** Il client Linux
  (`Mt5MarketDataSource`) e il fake bridge
  (`bridge/fake/fake_bridge.py`) sono implementati e testati (unitari, contratto HTTP,
  end-to-end contro Postgres reale, e manualmente via Docker Compose). Il bridge reale e' stato
  collegato al terminale ufficiale sul Mac e ha letto terminale/account/posizioni/ordini, ma il
  contratto HTTP completo, i deal, le riconnessioni e il funzionamento prolungato restano da
  provare. Nessuna immagine Docker/compose esiste ancora per il bridge reale.
- **Log "Backfill" del market-data-worker e' riusato anche per il ciclo immediatamente dopo un
  riavvio**, anche quando esiste gia' un checkpoint: il comportamento e' corretto (riparte dal
  checkpoint reale letto da Postgres via `MAX(open_time)`, nessuna candela persa ne' duplicata,
  verificato con un riavvio reale in fase di validazione), ma l'etichetta di log e' la stessa sia
  al primissimo avvio (checkpoint assente) sia dopo un riavvio (checkpoint presente) -- possibile
  micro-miglioramento di chiarezza nei log, non una correttezza da sistemare.
- **Il mock di mercato non riproduce condizioni di errore realistiche** (vedi sezione "Modalita'
  research" piu' sopra): copre dedup/checkpoint/gap sintetici, non il comportamento di un broker
  reale.
- **Nessun accesso esterno documentato/abilitato al Postgres locale** in questa fase (nessuna
  porta pubblicata verso l'host): un domani, se servisse ispezionarlo da uno strumento esterno
  alla VM, va aggiunta una scelta esplicita e documentata (bind a 127.0.0.1, mai 0.0.0.0), non
  assunta implicitamente.

## Verifiche eseguite

### Fase 1 (mock)

In macOS Apple Silicon, Colima + Docker CLI:

- `python -m pytest`: tutti i test verdi (vedi conteggio aggiornato nella sezione Fase 2 sotto:
  la suite e' condivisa, mock e reale sono nello stesso `python -m pytest`).
- Controllo sintassi Python (`python -m py_compile`) su tutti i moduli in `worker/`.
- Esecuzione Docker end-to-end del worker (`MOCK_MODE=true DRY_RUN=true`): tutti e 7 gli eventi
  emessi nell'ordine corretto, log senza token/password o identificativi account/server in
  chiaro, healthcheck `healthy`, nessun restart e arresto pulito. `event_id` non contiene piu'
  l'`account_number` in chiaro (bug corretto in `event_sender.sanitize_payload_for_log`, con
  test di regressione dedicato).

```bash
docker compose -f docker-compose.mock.yml config   # OK
docker compose -f docker-compose.mock.yml build    # OK
docker compose -f docker-compose.mock.yml up       # OK, healthy
docker compose -f docker-compose.mock.yml down     # OK
```

### Fase 2 (branch `feature/mt5-wine-runtime`)

In macOS Apple Silicon, Colima + Docker CLI (build amd64 sotto emulazione QEMU):

- `python -m pytest`: **48 test, tutti verdi** (35 di Fase 1 + 13 nuovi in
  `tests/test_mt5_client.py` per `RealMt5Client`: retry limitato con successo/esaurimento,
  mapping di `account_info`/posizioni/ordini/deal, filtro dei soli deal di uscita in
  `get_recent_deals`, e due test dedicati che confermano che login/password non compaiono mai
  nei log ne' in caso di successo ne' di fallimento). Nessuno di questi test richiede Wine/MT5
  reali: il pacchetto `MetaTrader5` e' sostituito con un doppio di test iniettato in
  `sys.modules`.
- Controllo sintassi Python invariato, verde su tutti i moduli aggiornati.
- `docker compose -f docker-compose.yml config`: OK, interpolazione env/volumi/`platform:
  linux/amd64` corretta.
- `docker compose -f docker-compose.yml build`: **completata con successo** (non solo
  sintattica): Wine `11.0.0.0~bookworm-1` installato correttamente sotto emulazione amd64
  (~230s), immagine `tradejournal-mt5-cloud-worker:real-mt5` da 787MB. Smoke test dei binari
  nell'immagine: `Xvfb`, `xdpyinfo`, `pgrep`, `wine --version` (→ `wine-11.0`) tutti presenti e
  funzionanti.
- `docker compose -f docker-compose.yml run --rm worker` (senza credenziali, senza terminale in
  `./runtime/mt5`): Xvfb si avvia (con un avviso innocuo da utente non-root, vedi "Limiti noti
  Wine/MT5"), l'assenza di `terminal64.exe` viene segnalata, il worker logga il promemoria sulla
  investor password, `RealMt5Client.connect()` fallisce in modo pulito
  (`MT5_LOGIN / MT5_PASSWORD / MT5_SERVER non configurati.`, nessun segreto in chiaro dato che
  erano tutti vuoti), l'entrypoint propaga l'exit code (1) e arresta Xvfb ordinatamente.
- **Non verificato per il client `direct`** (richiede una VPS Ubuntu amd64 con un vero terminale
  MT5, vedi "Fase 2: MT5 reale + Wine"): connessione IPC del worker Linux legacy,
  `account_info`/posizioni/ordini/deal e `reconnect()` su una disconnessione reale.
- Dopo la verifica, network/volume Docker creati per il test e la directory `runtime/` locale
  sono stati rimossi: nessuna risorsa lasciata in esecuzione.
- `git diff --check`: nessun conflitto/whitespace error (invariato dalla Fase 1).
- Nessun segreto versionato: `.env` resta in `.gitignore` (confermato con
  `git check-ignore -v .env`), `runtime/` aggiunta a `.gitignore`/`.dockerignore`; grep manuale
  su tutto il repository per pattern di password/token non ha trovato valori reali (solo
  fixture di test esplicitamente fittizie, es. `"SuperInvestorPass123"`,
  `"hyper-secret-value"`).

```bash
docker compose -f docker-compose.yml config              # OK
docker compose -f docker-compose.yml build                # OK, Wine installato, ~230s sotto emulazione
docker compose -f docker-compose.yml run --rm worker       # OK, fallisce in modo pulito senza credenziali/terminale
```

### Fase 3 (modalita' research)

Su Docker Desktop/CLI, macOS Apple Silicon (**arm64 nativo, nessuna emulazione**: a differenza
dello stage `real-mt5`, questo stage non richiede Wine ne' `platform: linux/amd64`):

- `python -m pytest`: **92 test, tutti verdi** (48 preesistenti, invariati, + 44 nuovi: 18 in
  `tests/test_config_research.py`, 16 in `tests/test_market_data_source.py`, 5 in
  `tests/test_market_data_main_startup.py`, 10 in
  `tests/test_market_data_store_integration.py` contro un Postgres reale avviato via Docker
  dalla fixture `postgres_database_url`, non mockato).
- `docker compose -f docker-compose.mock.yml -f docker-compose.research.yml config`: OK,
  interpolazione corretta; confermato che il servizio `worker` (trade-sync) non viene alterato
  da `docker-compose.research.yml` (nessuna chiave in comune tra i due file per quel servizio) e
  che `postgres` non ha alcuna sezione `ports:`.
- `up -d --build`: build e avvio di tutti e tre i container (`tradejournal-mt5-cloud-worker-mock`,
  `tradejournal-mt5-research-postgres`, `tradejournal-mt5-market-data-worker`), tutti `healthy`.
- Log del market-data-worker: migration applicata una sola volta (`Applico migration
  0001_initial_schema...`), backfill iniziale (500 candele per ciascuna delle 4 combinazioni
  simbolo/timeframe testate), poi cicli di sync incrementali regolari; **mai** `DATABASE_URL` o
  la password Postgres in chiaro (verificato con grep negativo su tutti i log dei tre container).
- Log del trade-sync worker (`worker`): **identico** al comportamento di Fase 1 (stessa sequenza
  di 7 eventi mock, stesso formato), a conferma che l'aggiunta della modalita' research non lo
  altera in alcun modo.
- Query diretta su Postgres (`SELECT COUNT(*) ... GROUP BY symbol, timeframe`): conteggi coerenti
  con backfill + cicli di sync osservati nei log.
- **Riavvio del solo market-data-worker** (`docker compose restart market-data-worker`): riparte
  dal checkpoint reale (`MAX(open_time)` per symbol/timeframe letto da Postgres), non da zero;
  verificato che `COUNT(*) = COUNT(DISTINCT (symbol_id, timeframe, open_time))` prima e dopo il
  riavvio (nessuna riga duplicata, garantito anche strutturalmente dall'indice unique).
- **`down` (senza `-v`) seguito da `up`**: il volume `mt5-research-postgres-data` sopravvive, le
  candele salvate in precedenza sono ancora presenti dopo il riavvio dell'intero stack.
- **Isolamento client verificato**: avviando **solo** `docker compose -f docker-compose.mock.yml
  up`, nessun container Postgres ne' market-data-worker viene creato (verificato con `docker ps
  --filter name=tradejournal-mt5`): il servizio non esiste nel compose graph di
  un'installazione cliente, non e' un flag da disattivare.
- Dopo la verifica, container/rete/volume di test creati per questa sessione sono stati rimossi
  (`docker compose down`, poi `docker volume rm` sul volume Postgres di test, contenente solo
  dati mock generati durante la validazione): nessuna risorsa lasciata in esecuzione.
- **Non verificato** (richiede la VPS Ubuntu ARM64 di destinazione, non disponibile in questa
  sessione locale): comportamento sotto carico prolungato (24/48 ore), consumo RAM/CPU reale di
  Postgres + market-data-worker insieme al trade-sync worker sulla stessa macchina, backup/
  ripristino (`pg_dump`/`psql`) eseguito realmente (i comandi sono documentati ma non ancora
  eseguiti in questa sessione).

```bash
docker compose -f docker-compose.mock.yml -f docker-compose.research.yml config   # OK
docker compose -f docker-compose.mock.yml -f docker-compose.research.yml up -d --build  # OK, 3 container healthy
docker compose -f docker-compose.mock.yml -f docker-compose.research.yml down     # OK, volume Postgres sopravvive
docker compose -f docker-compose.mock.yml up -d                                    # OK, nessun servizio research creato
```

### Fase 4 (mt5-bridge: client HTTP + fake bridge)

Stesso ambiente della Fase 3 (Docker locale, arm64 nativo -- **il fake bridge non richiede Wine
ne' `platform: linux/amd64`, a differenza dello stage `real-mt5`**):

- `python -m pytest`: **160 test, tutti verdi** (92 di Fase 1-3 + 68 nuovi: config/bridge in
  `tests/test_config_research.py`, `tests/test_bridge_common.py` per le funzioni pure di
  `bridge/common.py`, `tests/test_fake_bridge.py` per il contratto HTTP del fake bridge -- vero
  server in ascolto su 127.0.0.1, non un mock --, `tests/test_mt5_market_data_source.py` per il
  client (retry su 5xx, nessun retry su 401/400, timeout, Decimal da stringhe, validazione UTC/
  OHLC, difesa in profondita' su ordine/since/limit, nessun segreto nei log),
  `tests/test_bridge_no_trading.py` (controllo statico: nessuna chiamata `order_send`/
  `order_check`/`TRADE_ACTION_*` in nessun file sotto `bridge/`), e
  `tests/test_mt5_bridge_end_to_end.py` (fake bridge reale -> `Mt5MarketDataSource` -> Postgres
  reale via Docker, stesso principio della fixture gia' usata in Fase 3). Un test preesistente
  (`test_build_market_data_source_mt5_is_not_implemented`) e' stato deliberatamente aggiornato:
  il suo intero scopo era verificare lo stub `NotImplementedError` che questa fase sostituisce
  con un'implementazione reale, vedi `tests/test_market_data_source.py`.
- `docker compose -f docker-compose.mock.yml -f docker-compose.research.yml -f
  docker-compose.research-mt5-fake.yml config`: OK, `market-data-worker` riceve correttamente
  `MARKET_DATA_SOURCE=mt5`/`MT5_BRIDGE_URL=http://mt5-bridge-fake:8080` (default di
  `docker-compose.research-mt5-fake.yml`, sovrascrivibile da `.env`), `depends_on` include sia
  `postgres` sia `mt5-bridge-fake`, nessuno dei due espone una sezione `ports:`.
- `up -d --build`: build e avvio di **quattro** container (`worker`, `postgres`,
  `mt5-bridge-fake`, `market-data-worker`), tutti `healthy`.
- Log del market-data-worker: `mt5-bridge raggiungibile: terminal_connected=True
  account_connected=True server=FakeBridge-Demo` (health check opzionale all'avvio), poi backfill
  (500 candele per EURUSD/M1 e EURUSD/M5) e cicli di sync incrementali via richieste HTTP reali a
  `mt5-bridge-fake` (confermato anche dai log del bridge stesso: `POST /v1/candles HTTP/1.1 200`
  dall'IP interno Docker di market-data-worker, non `127.0.0.1`). **Mai** `MT5_BRIDGE_TOKEN` ne'
  `DATABASE_URL`/`POSTGRES_PASSWORD` in chiaro in nessuno dei quattro log (grep negativo su tutti
  i container).
- Query diretta su Postgres: righe salvate con `canonical_symbol=EURUSD`,
  `broker_symbol=EURUSD` (da `EURUSD_BROKER_SYMBOL`), `source=mt5` -- distinte dalle righe
  `source=mock` di eventuali sync precedenti (stesso `canonical_symbol`, chiave univoca diversa
  per `source`, nessun conflitto).
- **Riavvio del solo market-data-worker** contro il fake bridge: `SIGTERM` gestito (`Ricevuto
  segnale 15, arresto in corso...` poi `arrestato in modo pulito`), ripartenza dal checkpoint
  reale; verificato `COUNT(*) = COUNT(DISTINCT (symbol_id, timeframe, open_time))` subito dopo,
  nessuna riga duplicata.
- **Nessuna porta pubblicata** per `mt5-bridge-fake` ne' per `postgres` (`docker port` vuoto per
  entrambi).
- **Regressione `MARKET_DATA_SOURCE=mock` verificata esplicitamente**: passato da `mt5` a `mock`
  in `.env` e riavviato **senza** includere `docker-compose.research-mt5-fake.yml` -- solo 3
  container (nessun `mt5-bridge-fake`), log tornati al formato di Fase 3 (nessuna riga
  "mt5-bridge raggiungibile"), le candele `source=mt5` gia' salvate restano intatte insieme alle
  nuove `source=mock`, `COUNT(*) = COUNT(DISTINCT ...)` ancora verificato su tutta la tabella.
- Trade-sync worker (`worker`): log identici a Fase 1/3 in ogni combinazione provata, a conferma
  che il lavoro di questa fase non lo tocca in alcun modo.
- Dopo la verifica, container/rete/volume/immagini di test sono stati fermati e il volume
  Postgres di test rimosso (conteneva solo dati sintetici generati durante la validazione):
  nessuna risorsa lasciata in esecuzione.
- **In quella fase non verificato contro MT5 reale**: successivamente, sul Mac Apple Silicon con
  il Wine dell'app ufficiale, un Python Windows 3.11 AMD64 nello stesso prefix ha completato
  `initialize(path=...)`, letture terminale/account/posizioni/ordini e `shutdown()`. Restano non
  verificati end-to-end il server HTTP, `symbol_select()`/candele reali, deal/snapshot completo,
  errori e riconnessioni reali.

```bash
docker compose -f docker-compose.mock.yml -f docker-compose.research.yml \
  -f docker-compose.research-mt5-fake.yml config                                   # OK
docker compose -f docker-compose.mock.yml -f docker-compose.research.yml \
  -f docker-compose.research-mt5-fake.yml up -d --build                            # OK, 4 container healthy
docker compose -f docker-compose.mock.yml -f docker-compose.research.yml \
  -f docker-compose.research-mt5-fake.yml restart market-data-worker               # OK, checkpoint ripreso, 0 duplicati
docker compose -f docker-compose.mock.yml -f docker-compose.research.yml \
  -f docker-compose.research-mt5-fake.yml down                                     # OK, volume Postgres sopravvive
# .env: MARKET_DATA_SOURCE=mt5 -> mock
docker compose -f docker-compose.mock.yml -f docker-compose.research.yml up -d     # OK, nessun mt5-bridge-fake creato
```

### Fase 5 (trade-sync tramite snapshot HTTP)

Verifica eseguita localmente su Docker arm64 con soli dati e token throwaway:

- `python -m pytest`: **231 test, tutti verdi** (160 preesistenti + 71 nuovi per contratto
  snapshot, mapping Windows read-only, client/config/checkpoint, scenario fake, Compose ed E2E
  socket reale fino a una ingestion HTTP finta); unico warning locale `urllib3`/LibreSSL;
- `docker compose -f docker-compose.trade-sync-fake.yml config`: OK; grafo limitato ai due
  servizi previsti e nessuna porta pubblicata;
- `up -d --build --wait`: fake bridge e worker entrambi `healthy`; osservati esattamente i sette
  eventi nell'ordine previsto;
- restart del solo `trade-sync-worker`: snapshot persistito nel volume e conteggio eventi
  invariato a 7, quindi nessun duplicato;
- `docker inspect` sul fake bridge: `{"8080/tcp":null}`; token bridge e ingestion assenti dai
  log; nessuna chiamata o endpoint di trading trovati dai controlli statici/socket;
- container, rete e volume throwaway rimossi al termine. Le immagini locali costruite possono
  essere riusate per un test successivo.

Il bridge Windows reale ha superato la prova IPC di base sul Mac, ma resta **prepared but
unvalidated end-to-end** per snapshot HTTP completo e deployment VPS Ubuntu AMD64 con Python
Windows e terminale MT5 nello stesso `WINEPREFIX`.

### Verifica IPC reale macOS (`existing`)

Sul Mac Apple Silicon sono stati usati il Wine prefix dell'app ufficiale, il terminale
`C:\Program Files\MetaTrader 5\terminal64.exe` gia' aperto e un Python Windows 3.11 AMD64
embeddable in `C:\Python311Embed\python.exe`. Sono riusciti import di `MetaTrader5` e NumPy,
`mt5.initialize(path=terminal_path)`, `terminal_info()`, `account_info()`, `positions_get()`,
`orders_get()` e `mt5.shutdown()` con dati reali. Non e' stato effettuato un nuovo login ne'
cambiato l'account del terminale principale.

Questa verifica non certifica ancora `POST /v1/trading/snapshot` completo, deal di uscita,
`POST /v1/candles`, errori/retry/reconnect reali o stabilita' 24/48 ore. Questi punti e il
deployment VPS AMD64 restano esplicitamente nella checklist successiva.
