# Provisioning MT5 per account

Questa fase aggiunge un agent host-side e una coda filesystem. Non interroga Supabase e non
espone API HTTP. Ogni `connection_id` valido crea un progetto Docker Compose indipendente:

```text
provisioning agent (host, systemd, Docker CLI locale)
  -> tjmt5-<uuid-senza-trattini>-runtime
       Wine + Xvfb + MT5 (EA read-only) + bridge Linux read-only :8090
  -> tjmt5-<uuid-senza-trattini>-worker
       bridge HTTP -> detector -> outbox persistente -> TradeJournal
```

Il runtime e il worker condividono soltanto una rete `internal`. Ognuno ha inoltre una rete di
egress distinta: MT5 deve raggiungere il broker e il worker deve raggiungere TradeJournal. Non
esistono `ports`, `network_mode: host`, container privileged o mount del Docker socket. Il
provisioner, che e' il solo componente autorizzato a usare il socket locale, resta fuori dai
container.

Per ogni connessione vengono creati nomi derivati esclusivamente dal UUID canonico:

- progetto `tjmt5-<uuid-senza-trattini>`;
- container `...-runtime` e `...-worker`;
- reti `...-internal`, `...-runtime-egress` e `...-worker-egress`;
- volumi `...-wine-prefix` e `...-worker-data`;
- directory secret `/opt/tradejournal/secrets/<connection_id>/`.

Account, server e URL non entrano mai nei nomi Docker. Container, reti e volumi hanno label
`com.tradejournal.managed-by=provisioning-agent` e
`com.tradejournal.connection-id=<connection_id>`.

## Confine di sicurezza dei secret

Un job JSON contiene soltanto dati non sensibili. I tre secret obbligatori sono file distinti:

```text
/opt/tradejournal/secrets/<connection_id>/mt5_password
/opt/tradejournal/secrets/<connection_id>/mt5_bridge_token
/opt/tradejournal/secrets/<connection_id>/tradejournal_bridge_token
```

Docker Compose monta i file locali in `/run/secrets`. Con secret Compose aventi sorgente
`file`, Docker usa bind mount e non applica in modo portabile `uid`, `gid` o `mode` dichiarati
nel YAML. Il gate operativo e' quindi sull'host:

- root secret e directory di connessione esattamente `0700` (piu' restrittivo del requisito
  minimo `0750`), mai group/world-accessible;
- file regolari, non symlink, `0400` o `0600`, senza alcun bit group/world;
- owner UID fisso `1000`, verificato anche tramite `TJ_SECRET_OWNER_UID=1000`, che coincide con
  l'UID non-root dei due container;
- il provisioner valida metadata e dimensione, ma non stampa ne' inserisce i valori nel job,
nell'env file generato o nei comandi Docker.

Questa configurazione assume il Docker Engine rootful gia' validato sulla VPS. Con Docker
rootless la mappatura subordinate UID cambia la ownership osservata nel container: non usare il
UID `1000` senza uno smoke test esplicito della mappatura e dei tre mount.

Creare i file vuoti con metadata corretti e compilarli con un editor che non lasci copie o
backup world-readable:

```bash
connection_id="<uuid-canonico>"
sudo install -d -o tradejournal-provisioner -g tradejournal-provisioner -m 0700 \
  /opt/tradejournal/secrets/"$connection_id"
for name in mt5_password mt5_bridge_token tradejournal_bridge_token; do
  sudo install -o 1000 -g 1000 -m 0400 /dev/null \
    /opt/tradejournal/secrets/"$connection_id"/"$name"
done
sudoedit /opt/tradejournal/secrets/"$connection_id"/mt5_password
sudoedit /opt/tradejournal/secrets/"$connection_id"/mt5_bridge_token
sudoedit /opt/tradejournal/secrets/"$connection_id"/tradejournal_bridge_token
```

Usare sempre la password **investor** MT5. I due token devono essere diversi. Non usare
password di trading, token reali nei test, argomenti CLI, heredoc salvati nella shell history o
variabili nel file systemd. Le varianti legacy `MT5_PASSWORD`, `MT5_BRIDGE_TOKEN` e
`TRADEJOURNAL_BRIDGE_TOKEN` restano disponibili per sviluppo locale; impostare insieme una
variabile e la corrispondente `*_FILE` e' un errore.

## Golden template esterno

Il repository e le immagini non contengono MT5 o un Wine prefix: l'unico artefatto aggiuntivo
richiesto e' l'Expert Advisor gia' compilato (`mt5/experts/TradeJournalBridge.mq5` ->
`TradeJournalBridge.ex5`, nessun Python Windows/pacchetto `MetaTrader5` in questa architettura).
Preparare una volta un prefix pulito sulla VPS, senza sessioni o account collegati, compilare
l'EA, quindi creare l'archivio:

```bash
scripts/compile_mt5_expert.sh /home/ubuntu/.mt5
sudo install -d -o "$USER" -g tradejournal-provisioner -m 0750 \
  /opt/tradejournal/templates
scripts/create_mt5_runtime_template.sh \
  /home/ubuntu/.mt5 \
  /opt/tradejournal/templates/mt5-prefix.tar.zst
sudo chown 1000:tradejournal-provisioner \
  /opt/tradejournal/templates/mt5-prefix.tar.zst{,.sha256}
sudo chmod 0440 /opt/tradejournal/templates/mt5-prefix.tar.zst{,.sha256}
```

`compile_mt5_expert.sh` non richiede alcuna credenziale MT5: usa MetaEditor sotto Wine
(`metaeditor64.exe /compile`) per pubblicare `TradeJournalBridge.ex5` nel prefix indicato; se la
compilazione headless non fosse disponibile o affidabile in un dato ambiente, il fallback
documentato e' MetaEditor in modalita' grafica (F7, verificare "0 error(s)"). Prima di copiare il
prefix, `create_mt5_runtime_template.sh` rifiuta processi Wine/MT5 attivi, controlla la presenza
del terminale e del `.ex5` compilato su una copia privata. Una allowlist top-level e una denylist
case-insensitive bloccano artefatti noti di account, sessione, profili, log, credential store e
file residui dell'EA (`MQL5/Files/TradeJournal`, che potrebbe contenere dati di una sessione gia'
eseguita); il controllo dei registry cerca soltanto nomi di chiavi noti e non ne stampa i valori.
Un match non viene cancellato automaticamente: pulire deliberatamente il prefix sorgente e
rilanciare.

Lo script non segue i symlink `dosdevices`, non modifica il prefix originale, pubblica archivio
e sidecar `.sha256` con rename atomici e aggiunge il marker
`.tradejournal-template-version`. Conservare il digest in `MT5_TEMPLATE_SHA256` o lasciare il
sidecar accanto all'archivio. Il runtime ricalcola il digest prima dell'estrazione e rifiuta un
volume inizializzato con versione o checksum differenti.

Il template e il sidecar sono dati operativi esterni: non copiarli nel checkout e non
aggiungerli a Git. L'owner UID `1000` permette al runtime privo di capability DAC di leggere il
bind mount; il gruppo e il mode `0440` permettono al provisioner di verificarne il checksum.

## Configurazione dell'agent

Creare `/etc/tradejournal/provisioning-agent.env` come `root:root 0600`, copiando soltanto le
chiavi necessarie (non l'intero `.env.example`). Valori essenziali:

```dotenv
TJ_PROVISIONING_ROOT=/opt/tradejournal
TJ_INSTANCES_ROOT=/opt/tradejournal/instances
TJ_STATE_ROOT=/opt/tradejournal/state
TJ_LOCKS_ROOT=/opt/tradejournal/locks
TJ_SECRETS_ROOT=/opt/tradejournal/secrets
TJ_QUEUE_ROOT=/opt/tradejournal/queue
TJ_COMPOSE_TEMPLATE=/opt/tradejournal/tradejournal-mt5-cloud-worker/deploy/instance/compose.yaml
MT5_TEMPLATE_ARCHIVE=/opt/tradejournal/templates/mt5-prefix.tar.zst
MT5_TEMPLATE_SHA256=<sha256>
MT5_RUNTIME_TARGET=real
TJ_SECRET_OWNER_UID=1000
TJ_ALLOW_INSECURE_HTTP=false
```

Preparare le directory prima di avviare systemd e assegnarle all'utente dedicato scelto per la
unit. L'appartenenza al gruppo `docker` equivale a privilegi amministrativi sull'host: non dare
accesso a utenti o processi applicativi.

```bash
sudo install -d -o tradejournal-provisioner -g tradejournal-provisioner -m 0750 \
  /opt/tradejournal/{instances,state,locks,queue}
sudo install -d -o tradejournal-provisioner -g tradejournal-provisioner -m 0700 \
  /opt/tradejournal/secrets
sudo install -d -o root -g root -m 0750 /etc/tradejournal
sudo install -o root -g root -m 0600 /dev/null \
  /etc/tradejournal/provisioning-agent.env
sudoedit /etc/tradejournal/provisioning-agent.env
sudo install -o root -g root -m 0644 \
  deploy/systemd/tradejournal-provisioning-agent.service \
  /etc/systemd/system/tradejournal-provisioning-agent.service
sudo systemctl daemon-reload
sudo systemctl enable --now tradejournal-provisioning-agent
journalctl -u tradejournal-provisioning-agent -f
```

Adattare `User`, `Group`, `WorkingDirectory`, `ExecStart` e i path mediante una copia controllata
o un drop-in. Non installare la unit automaticamente dal repository.

## Contratto job V1 e CLI

Esempio `provision` senza secret:

```json
{
  "version": 1,
  "job_id": "11111111-1111-4111-8111-111111111111",
  "action": "provision",
  "connection_id": "22222222-2222-4222-8222-222222222222",
  "account_number": "12345678",
  "server": "Broker-Demo",
  "tradejournal_api_url": "https://app.example.test/api/mt5-events",
  "created_at": "2026-07-13T12:00:00Z"
}
```

`job_id` e `connection_id` sono UUID canonici. `provision` richiede account, server e URL
HTTPS; HTTP e' accettato soltanto con la modalita' test esplicita. Azioni supportate:
`provision`, `start`, `stop`, `restart`, `status`, `deprovision`.

`account_number`/`server`/`connection_id` del job vengono scritti in `instance.env` sia come
`MT5_LOGIN`/`MT5_SERVER`/`TJ_CONNECTION_ID` (letti dall'entrypoint per lo `startup.ini`) sia come
`TJ_EXPECTED_MT5_LOGIN`/`TJ_EXPECTED_MT5_SERVER` (letti dal bridge Linux per verificare che
l'account effettivamente connesso coincida con quello richiesto): nessuna azione manuale e'
necessaria, il provisioner tiene i due lati sempre coerenti. Vedi README, sezione "Verifica
obbligatoria dell'identita' dell'account", per cosa succede in caso di mismatch.

```bash
.venv/bin/python -m provisioning.cli validate-job JOB.json
.venv/bin/python -m provisioning.cli enqueue JOB.json
.venv/bin/python -m provisioning.cli provision JOB.json
.venv/bin/python -m provisioning.cli start CONNECTION_ID
.venv/bin/python -m provisioning.cli stop CONNECTION_ID
.venv/bin/python -m provisioning.cli restart CONNECTION_ID
.venv/bin/python -m provisioning.cli status CONNECTION_ID
.venv/bin/python -m provisioning.cli deprovision CONNECTION_ID
.venv/bin/python -m provisioning.cli run-filesystem-agent
```

Il ledger lega il `job_id` al contratto completo: riusare lo stesso ID con payload diverso e'
un errore; ripetere un job completato restituisce il risultato persistito senza rieseguirlo.
Un lock per job e uno per connessione serializzano agent e CLI concorrenti. State e ledger sono
scritti con file temporaneo, `fsync` e `os.replace` nella stessa directory.

## Coda filesystem

Le directory sono `inbox`, `processing`, `completed` e `failed`. Pubblicare un job tramite
l'helper no-clobber, che valida il contratto, scrive il file e la directory con `fsync` e non
sostituisce mai un job omonimo differente:

```bash
.venv/bin/python -m provisioning.cli enqueue JOB.json
```

Non pubblicare job con un semplice `mv`: non offre insieme no-clobber e durabilita' della
directory. L'agent sposta il job nella directory `processing` con rename sullo stesso
filesystem. Dopo successo o errore conserva il
JSON originale e un sidecar `.result.json` o `.error.json`; collisioni di nomi non sovrascrivono
job precedenti. All'avvio, i job rimasti in `processing` vengono riconciliati col ledger: un job
gia' completato va in `completed` con il risultato, gli altri tornano in inbox. Un lock singleton
impedisce a due filesystem-agent di recuperare contemporaneamente lo stesso lavoro.

## Persistenza e recovery

Il volume Wine e il volume worker sono dedicati alla connessione. `worker-data` contiene
`snapshot.json` e `event_outbox.json`: l'outbox viene salvata prima del checkpoint e mantiene
ordine FIFO, `event_id`, pending e dead-letter dopo restart. Un errore transitorio blocca gli
eventi successivi dello stesso ordine causale; un rifiuto permanente viene conservato in
dead-letter e non genera retry infinito. In dry-run gli eventi rimangono pending: il checkpoint
puo' avanzare solo dopo che l'outbox li ha salvati. Per passare all'invio reale, impostare
`TJ_WORKER_DRY_RUN=false` nell'agent, riavviare l'agent se usa systemd e inviare un nuovo job
`provision` con un nuovo `job_id` e la stessa identita' della connessione: il rendering aggiorna
`instance.env` e `docker compose up` drena la coda senza perdere le transizioni osservate. I
comandi `start` e `restart` da soli non rigenerano la configurazione.

Un normale `stop`, `restart`, `docker compose down` senza `--volumes`, riavvio host o riavvio
dell'agent non deve eliminare i volumi. Non usare `down -v` fuori da un deprovision deliberato.

Per disaster recovery conservare separatamente e cifrare:

- golden template e checksum;
- `/opt/tradejournal/{instances,state,queue}`;
- `/opt/tradejournal/secrets` con ACL/ownership originali;
- backup dei volumi `*-wine-prefix` e soprattutto `*-worker-data`.

Dopo un crash dell'agent, riavviarlo: la queue recupera `processing` e il ledger impedisce il
doppio completamento. Dopo perdita host, ripristinare prima template, secret e volumi, poi state
e configurazioni istanza; verificare checksum/ownership e usare `status`, quindi ripetere il job
`provision` originale o un nuovo job con la stessa identita' per riconciliare Compose. Non
marcare manualmente un'istanza `deleted` se Docker contiene ancora risorse con le sue label.

## Rotazione e rimozione

I processi leggono i secret all'avvio. Per ruotare senza finestra di file parziale:

1. `stop CONNECTION_ID`;
2. creare un file temporaneo nella stessa directory, `chown` all'UID configurato e `chmod 0400`;
3. compilare il file senza stamparlo, poi rinominarlo atomicamente sul nome definitivo;
4. ripetere per il solo secret da ruotare;
5. `start CONNECTION_ID` e verificare health/log sanitizzati.

Gli errori di autenticazione `401/403` rimangono pending e vengono quindi riprovati dopo la
rotazione. Le dead-letter riguardano rifiuti del payload non recuperabili (per esempio `422`):
ispezionarle prima di un eventuale replay amministrativo controllato con gli stessi `event_id`.

`deprovision CONNECTION_ID` esegue `docker compose down --volumes --remove-orphans`, poi elimina
configurazione d'istanza e directory secret. Rifiuta configurazioni parziali che potrebbero
lasciare risorse Docker orfane. Il record state `deleted` e il ledger restano come audit trail.
Verificare infine l'assenza di risorse:

```bash
docker ps -a --filter label=com.tradejournal.connection-id=CONNECTION_ID
docker network ls --filter label=com.tradejournal.connection-id=CONNECTION_ID
docker volume ls --filter label=com.tradejournal.connection-id=CONNECTION_ID
```

## Mock e smoke test VPS

`MT5_RUNTIME_TARGET=mock` sostituisce solo il runtime Wine/MT5 con il fake bridge standard
library. Il provisioner, il Compose per-account, le reti, i volumi, i secret mount, il worker e
il lifecycle restano gli stessi. Usare esclusivamente token/password throwaway e
`TJ_WORKER_DRY_RUN=true`; gli eventi prodotti durante lo smoke restano nell'outbox persistente.

Sulla VPS Ubuntu 24.04 AMD64:

```bash
cd /opt/tradejournal/tradejournal-mt5-cloud-worker
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
test "$(id -u)" -eq 1000

export TJ_PROVISIONING_ROOT=/opt/tradejournal-smoke
export TJ_INSTANCES_ROOT="$TJ_PROVISIONING_ROOT/instances"
export TJ_STATE_ROOT="$TJ_PROVISIONING_ROOT/state"
export TJ_LOCKS_ROOT="$TJ_PROVISIONING_ROOT/locks"
export TJ_SECRETS_ROOT="$TJ_PROVISIONING_ROOT/secrets"
export TJ_QUEUE_ROOT="$TJ_PROVISIONING_ROOT/queue"
export TJ_COMPOSE_TEMPLATE="$PWD/deploy/instance/compose.yaml"
export MT5_RUNTIME_TARGET=mock
export TJ_SECRET_OWNER_UID=1000
export TJ_WORKER_DRY_RUN=true
export TJ_ALLOW_INSECURE_HTTP=false

sudo install -d -o "$USER" -g "$USER" -m 0750 \
  "$TJ_PROVISIONING_ROOT" \
  "$TJ_INSTANCES_ROOT" "$TJ_STATE_ROOT" "$TJ_LOCKS_ROOT" "$TJ_QUEUE_ROOT"
sudo install -d -o "$USER" -g "$USER" -m 0700 "$TJ_SECRETS_ROOT"

id1="$(uuidgen | tr '[:upper:]' '[:lower:]')"
id2="$(uuidgen | tr '[:upper:]' '[:lower:]')"
job1="$(uuidgen | tr '[:upper:]' '[:lower:]')"
job2="$(uuidgen | tr '[:upper:]' '[:lower:]')"
for id in "$id1" "$id2"; do
  sudo install -d -o "$USER" -g "$USER" -m 0700 "$TJ_SECRETS_ROOT/$id"
  for name in mt5_password mt5_bridge_token tradejournal_bridge_token; do
    sudo install -o 1000 -g 1000 -m 0400 /dev/null "$TJ_SECRETS_ROOT/$id/$name"
  done
  printf 'throwaway-%s-password' "$id" | sudo tee "$TJ_SECRETS_ROOT/$id/mt5_password" >/dev/null
  printf 'throwaway-%s-mt5' "$id" | sudo tee "$TJ_SECRETS_ROOT/$id/mt5_bridge_token" >/dev/null
  printf 'throwaway-%s-api' "$id" | sudo tee "$TJ_SECRETS_ROOT/$id/tradejournal_bridge_token" >/dev/null
done
```

I tre `printf` precedenti sono ammessi **solo** per valori throwaway di smoke test. Per secret
reali usare l'editor e la procedura sicura descritti sopra. Creare i due job con account fittizi
diversi e lo stesso endpoint HTTPS, che non viene contattato in dry-run:

```bash
now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
cat > /tmp/job1.json <<EOF
{"version":1,"job_id":"$job1","action":"provision","connection_id":"$id1","account_number":"12345001","server":"Mock-Broker","tradejournal_api_url":"https://example.invalid/api/mt5-events","created_at":"$now"}
EOF
cat > /tmp/job2.json <<EOF
{"version":1,"job_id":"$job2","action":"provision","connection_id":"$id2","account_number":"12345002","server":"Mock-Broker","tradejournal_api_url":"https://example.invalid/api/mt5-events","created_at":"$now"}
EOF
.venv/bin/python -m provisioning.cli validate-job /tmp/job1.json
.venv/bin/python -m provisioning.cli provision /tmp/job1.json
.venv/bin/python -m provisioning.cli provision /tmp/job1.json
.venv/bin/python -m provisioning.cli provision /tmp/job2.json
```

Verificare due progetti isolati, health, nessuna porta e nessun valore throwaway in inspect/log:

```bash
for id in "$id1" "$id2"; do
  .venv/bin/python -m provisioning.cli status "$id"
  docker ps --filter "label=com.tradejournal.connection-id=$id"
  docker network ls --filter "label=com.tradejournal.connection-id=$id"
  docker volume ls --filter "label=com.tradejournal.connection-id=$id"
  for container in $(docker ps -q --filter "label=com.tradejournal.connection-id=$id"); do
    docker inspect "$container" --format '{{json .HostConfig.PortBindings}}'
    docker inspect "$container" | grep -F "throwaway-$id-" && exit 1 || true
    docker logs "$container" 2>&1 | grep -F "throwaway-$id-" && exit 1 || true
  done
  .venv/bin/python -m provisioning.cli stop "$id"
  .venv/bin/python -m provisioning.cli start "$id"
  .venv/bin/python -m provisioning.cli restart "$id"
done
```

Il valore `PortBindings` deve essere `{}` o `null`; le reti e i volumi dei due UUID devono
avere nomi differenti. Concludere rimuovendo entrambe le istanze e ricontrollando le label:

```bash
for id in "$id1" "$id2"; do
  .venv/bin/python -m provisioning.cli deprovision "$id"
  test -z "$(docker ps -aq --filter "label=com.tradejournal.connection-id=$id")"
  test -z "$(docker network ls -q --filter "label=com.tradejournal.connection-id=$id")"
  test -z "$(docker volume ls -q --filter "label=com.tradejournal.connection-id=$id")"
done
```

Solo dopo questo smoke test passare a `MT5_RUNTIME_TARGET=real`, golden template verificato e
account demo con password investor. Il polling Supabase e l'endpoint del sito restano fuori da
questa fase; la futura integrazione `tradejournal-drp` dovra' soltanto produrre lo stesso job V1
e i secret mediante un canale server-side autorizzato.
