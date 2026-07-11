# tradejournal-mt5-cloud-worker

POC **isolato** per un Cloud Sync self-hosted di MetaTrader 5: un worker Python che rileva eventi
di trading (apertura/modifica/chiusura posizioni, ordini pendenti) e li invia all'API di
ingestion di TradeJournal, in esecuzione dentro un container Docker (Wine + MT5 in una fase
futura). Questo repository e' **indipendente** dal repository principale `tradejournal-drp`, che
non viene mai modificato da questo progetto.

Stato attuale:
- **Fase 1 (fatta): modalita' mock.** Nessuna dipendenza da Wine/MT5. Simula una sequenza
  completa di eventi e li invia (o stampa, in dry-run) verso l'API TradeJournal.
- **Fase 2 (branch `feature/mt5-wine-runtime`): infrastruttura MT5 reale + Wine predisposta e
  costruibile.** Lo stage `real-mt5` del Dockerfile, `RealMt5Client` (retry limitato, mapping
  completo dei campi, nessun segreto nei log) e il compose dedicato sono implementati e validati
  per quanto possibile su macOS (config, build sintattica, nessuna regressione mock). Il test
  end-to-end contro un terminale MT5 vero resta da fare su una **VPS Ubuntu amd64** -- vedi
  "Fase 2: MT5 reale + Wine" piu' sotto per la differenza esatta tra "predisposto" e "testato".

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
`main.py` sceglie l'uno o l'altro in base a `MOCK_MODE`, senza altre modifiche. L'interfaccia
astratta (`Mt5Client`, classe base in `mt5_client.py`) non e' mai stata toccata nell'implementare
`RealMt5Client`: e' il punto di innesto stabile tra mock e reale.

`RealMt5Client` (in `mt5_client.py`) implementa tutti e sei i metodi richiesti (`account_info`,
`get_open_positions`, `get_recent_deals`, `get_pending_orders`, `reconnect`, `health_status`),
con un numero limitato di retry con backoff lineare (`_call_with_retry`, 3 tentativi di default)
su ogni chiamata al pacchetto `MetaTrader5`, e non stampa mai `MT5_LOGIN`/`MT5_PASSWORD` in
chiaro (sempre mascherati via `event_sender.mask_value`). Resta pero' **non testabile
end-to-end** senza un terminale MT5 reale: vedi "Fase 2: MT5 reale + Wine" per cosa e' stato
effettivamente validato (mapping dei campi e retry, con un modulo `MetaTrader5` finto iniettato
nei test) e cosa no (la connessione IPC reale al terminale).

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

## Fase 2: MT5 reale + Wine

Sviluppata sul branch `feature/mt5-wine-runtime`, a partire dalla base mock gia' verificata
(Fase 1, invariata). Copre l'infrastruttura container (Wine, Xvfb, directory persistente,
entrypoint/healthcheck dedicati) e il client Python (`RealMt5Client`), ma **il test end-to-end
contro un terminale MT5 vero non e' stato eseguito**: richiede una VPS Ubuntu amd64, non
disponibile in questa sessione di sviluppo (macOS Apple Silicon).

### Differenza tra modalita' mock e reale

| | Mock (Fase 1) | Reale (Fase 2) |
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
  (`python:3.11-slim`): predispone Wine/Xvfb/la directory persistente, ma la scelta tra
  "eseguire il worker stesso dentro Wine" e "un bridge IPC/HTTP verso un piccolo processo
  Python-Windows dedicato" resta un lavoro architetturale aperto, da risolvere durante il primo
  test reale su VPS. `RealMt5Client._import_mt5()` fallira' con un errore chiaro (non un crash
  silenzioso) finche' questo non e' risolto.
- **Build riuscita su macOS Apple Silicon (Colima, emulazione QEMU `linux/amd64`), ma resta solo
  un test d'infrastruttura.** `docker compose -f docker-compose.yml config` e `build` sono stati
  validati con successo (Wine `11.0.0.0~bookworm-1` installato correttamente, ~230s sotto
  emulazione), e un `docker compose run --rm worker` senza credenziali ha confermato che Xvfb si
  avvia, l'assenza di `terminal64.exe` viene segnalata chiaramente, `RealMt5Client.connect()`
  fallisce in modo pulito e senza segreti nei log, e l'entrypoint propaga l'exit code
  correttamente. Questo valida l'infrastruttura del container, **non** la connessione IPC a un
  terminale MT5 vero (mai disponibile in questa sessione) — vedi "Verifiche eseguite" per il
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

- **MT5 reale non testato end-to-end.** Lo stage `real-mt5`, `RealMt5Client` e
  `docker-compose.yml` sono implementati, con retry limitato e mapping dei campi verificati via
  test unitari (modulo `MetaTrader5` finto, vedi `tests/test_mt5_client.py`), ma la connessione
  IPC reale a un terminale MT5 sotto Wine non e' mai stata esercitata (nessun ambiente
  Linux+Wine con MT5 disponibile in questa sessione). Vedi "Fase 2: MT5 reale + Wine" per il
  dettaglio completo, inclusa l'incognita architetturale su come eseguire il pacchetto Python
  `MetaTrader5` (estensione nativa Windows) contro un worker Linux nativo.
- **Nessuna gestione multiutente/orchestrazione**, nessun Kubernetes, nessuna persistenza di
  credenziali su database: questo worker e' pensato per un singolo processo/singolo account,
  come da requisiti di questo POC.
- **Il mock non copre scenari di errore MT5** (disconnessioni, simboli non tradabili, margine
  insufficiente): simula solo il percorso "felice" richiesto (le 7 fasi elencate sopra).
- **cpus/mem_limit nei compose file** sono limiti indicativi; vanno ricalibrati per l'hardware
  reale di destinazione (laptop di sviluppo per il mock, VPS Ubuntu per il reale) prima di un
  uso prolungato.

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
- **Non verificato** (richiede una VPS Ubuntu amd64 con un vero terminale MT5, vedi "Fase 2: MT5
  reale + Wine"): connessione IPC reale al terminale, `account_info`/posizioni/ordini/deal
  contro un account demo vero, comportamento di `reconnect()` su una disconnessione reale.
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
