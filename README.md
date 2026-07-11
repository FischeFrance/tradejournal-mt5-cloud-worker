# tradejournal-mt5-cloud-worker

POC **isolato** per un Cloud Sync self-hosted di MetaTrader 5: un worker Python che rileva eventi
di trading (apertura/modifica/chiusura posizioni, ordini pendenti) e li invia all'API di
ingestion di TradeJournal, in esecuzione dentro un container Docker (Wine + MT5 in una fase
futura). Questo repository e' **indipendente** dal repository principale `tradejournal-drp`, che
non viene mai modificato da questo progetto.

Stato attuale:
- **Fase 1 (fatta): modalita' mock.** Nessuna dipendenza da Wine/MT5. Simula una sequenza
  completa di eventi e li invia (o stampa, in dry-run) verso l'API TradeJournal.
- **Fase 2 (predisposta, non testata): MT5 reale + Wine su Ubuntu.** Il Dockerfile ha uno stage
  dedicato, documentato, ma non e' stato ne' costruito ne' avviato in questo POC (richiede Wine
  e un installer MT5 forniti manualmente, non disponibili in questo ambiente macOS).

Nessun deploy, nessuna risorsa cloud, nessuna credenziale reale sono stati usati per costruire
questo POC.

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
  config.py            Lettura env var (MOCK_MODE, DRY_RUN, ...)
  mt5_client.py         Interfaccia Mt5Client astratta + RealMt5Client (stub, richiede Wine/MT5)
  mock_mt5_client.py     MockMt5Client: macchina a stati, nessuna dipendenza reale
  snapshot_store.py     Stato dell'ultimo poll (in memoria, opzionalmente su file)
  event_detector.py     Diff puro tra due snapshot -> eventi grezzi (nessuna rete, testabile)
  event_normalizer.py   Eventi grezzi -> payload API + event_id idempotente
  event_sender.py       Invio HTTP con retry/backoff, dry-run, masking dei log
  main.py                Loop di poll, logging, ciclo di vita, heartbeat per l'healthcheck
```

`mt5_client.py` e `mock_mt5_client.py` implementano la stessa interfaccia (`account_info`,
`get_open_positions`, `get_recent_deals`, `get_pending_orders`, `reconnect`, `health_status`):
`main.py` sceglie l'uno o l'altro in base a `MOCK_MODE`, senza altre modifiche. Questo e' il
punto di innesto per il futuro client MT5 reale.

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
- **`real-mt5`**: predisposto per la Fase 2 (Wine + Xvfb), **non pronto all'uso**, vedi sotto.

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

## Limiti del test su macOS

Questo POC e' stato verificato su macOS Apple Silicon con Colima e Docker CLI. In particolare:

- La modalita' mock (Fase 1) e' stata validata **direttamente in Python** (venv locale, 35 test
  unitari, esecuzione manuale del worker end-to-end con `DRY_RUN=true` per l'intera sequenza di
  7 eventi) — vedi la sezione "Verifiche eseguite" piu' sotto per l'esito esatto.
- I comandi Docker (`docker compose -f docker-compose.mock.yml config|build|up|down`) sono stati
  eseguiti con runtime Linux `aarch64`; build, healthcheck e arresto sono risultati corretti.
- MetaTrader 5 non gira nativamente su macOS (e' un eseguibile Windows) ne' sotto Docker su
  macOS senza un layer Windows/Wine aggiuntivo con supporto grafico — per questo la Fase 2
  (MT5 reale) e' esplicitamente rimandata a un test su **Ubuntu** (vedi sotto), dove Wine e'
  supportato nativamente da Docker senza virtualizzazione annidata.

## Futuro deploy Ubuntu (Fase 2: MT5 reale + Wine)

Non ancora implementato ne' testato. Passi previsti quando si affrontera' questa fase su una
macchina Ubuntu con Docker:

1. Costruire esplicitamente lo stage `real-mt5`:
   ```bash
   docker build -f docker/Dockerfile --target real-mt5 -t tradejournal-mt5-cloud-worker:real-mt5 .
   ```
2. **Fornire manualmente l'installer MT5** (vedi sezione dedicata sotto) nel volume
   `mt5-installer` dichiarato in `docker-compose.yml`.
3. Inizializzare Wine (`WINEPREFIX=/home/worker/.wine`, `WINEARCH=win64`) ed eseguire
   l'installer sotto un display virtuale (`Xvfb :99`) — passi manuali, da scriptare in un task
   dedicato quando si avra' un ambiente Ubuntu su cui validarli.
4. Installare il pacchetto Python `MetaTrader5` nel container ed eseguire `mt5_client.py`
   (`RealMt5Client`) con `MOCK_MODE=false` e credenziali reali fornite solo a runtime (mai
   committate).
5. Validare `account_info`, `get_open_positions`, `get_recent_deals`, `get_pending_orders`
   contro un account demo reale prima di qualunque account live.

`docker-compose.yml` (root) e' gia' predisposto per questo scenario ma **non va eseguito** prima
di aver completato questi passi manuali.

### Fornire l'installer MT5 manualmente

Questo repository **non scarica e non include** alcun installer MetaTrader 5: sarebbe
distribuzione non autorizzata di software proprietario. Per il test reale:

1. Scaricare l'installer ufficiale (es. `mt5setup.exe`) dal sito del proprio broker o da
   MetaQuotes, sulla macchina Ubuntu di destinazione.
2. Copiarlo nel volume Docker `mt5-installer` prima di avviare lo stage `real-mt5`, ad esempio:
   ```bash
   docker run --rm -v tradejournal-mt5-cloud-worker_mt5-installer:/mt5/installer \
     -v /percorso/locale:/src alpine cp /src/mt5setup.exe /mt5/installer/
   ```
3. Non versionare mai l'installer in questo repository (e' comunque escluso da `.dockerignore`
   e `.gitignore` in quanto binario esterno).

## Checklist 24/48 ore

Da eseguire quando il worker gira per un periodo prolungato (in mock o, in futuro, in reale) per
verificarne la stabilita' prima di qualunque uso non-POC:

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
# Snapshot singolo
docker stats --no-stream tradejournal-mt5-cloud-worker-mock

# Campionamento ripetuto (5 volte, ogni 2s) via script incluso
scripts/benchmark.sh

# Limiti configurati nel compose mock: mem_limit 256m, cpus 0.50 (docker-compose.mock.yml)
# Limiti nel compose reale (futuro): mem_limit 1g, cpus 1.0 (docker-compose.yml)
```

## Rischi noti

- **MT5 reale non testato in questa sessione.** Lo stage `real-mt5` e `docker-compose.yml` sono
  scritti per essere plausibili e coerenti con la documentazione ufficiale di Wine/MetaTrader5,
  ma non sono stati costruiti ne' eseguiti (nessun ambiente Linux+Wine disponibile qui). Vanno
  validati passo-passo su Ubuntu prima di qualunque uso reale.
- **MT5 reale resta fuori scope su macOS.** Le verifiche Docker descritte sotto coprono soltanto
  lo stage `mock`; non costituiscono una validazione dello stage Wine/MT5.
- **`RealMt5Client.get_recent_deals()` e' un placeholder** (restituisce sempre `{}`): il
  pacchetto `MetaTrader5` richiede una chiamata separata (`history_deals_get`) con una finestra
  temporale esplicita, da implementare quando si affrontera' la Fase 2 su un account reale.
- **Nessuna gestione multiutente/orchestrazione**, nessun Kubernetes, nessuna persistenza di
  credenziali su database: questo worker e' pensato per un singolo processo/singolo account,
  come da requisiti di questo POC.
- **Il mock non copre scenari di errore MT5** (disconnessioni, simboli non tradabili, margine
  insufficiente): simula solo il percorso "felice" richiesto (le 7 fasi elencate sopra).
- **cpus/mem_limit nei compose file** sono limiti indicativi pensati per un laptop di sviluppo;
  vanno ricalibrati per l'hardware reale di destinazione prima di un uso prolungato.

## Verifiche eseguite

In questa sessione (macOS Apple Silicon, Colima + Docker CLI):

- `python -m pytest`: 35 test, tutti verdi (`event_detector`, `event_normalizer`,
  `event_sender` — nessuno di questi test tocca la rete: `event_sender` patcha
  `requests.post`).
- Controllo sintassi Python (`python -m py_compile`) su tutti i moduli in `worker/`.
- Esecuzione Docker end-to-end del worker (`MOCK_MODE=true DRY_RUN=true`): tutti e 7 gli eventi
  emessi nell'ordine corretto, log senza token/password o identificativi account/server in
  chiaro, healthcheck `healthy`, nessun restart e arresto pulito.
- `git diff --check`: nessun conflitto/whitespace error.
- Nessun segreto versionato: `.env` e' in `.gitignore`, solo `.env.example` (valori vuoti) e'
  committato; grep manuale su tutto il repository per pattern di password/token non ha trovato
  valori reali.

Comandi Docker verificati per la Fase 1:

```bash
docker compose -f docker-compose.mock.yml config
docker compose -f docker-compose.mock.yml build
docker compose -f docker-compose.mock.yml up
docker compose -f docker-compose.mock.yml down
```
