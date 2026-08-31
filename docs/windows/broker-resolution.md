# Broker resolution PoC

Questo PoC introduce un registry versionato, un resolver deterministico e una Phase 0 unattended
per scegliere come una singola istanza MT5 debba raggiungere il server richiesto. La discovery usa
il terminale MetaTrader ufficiale tramite UIA o messaggi Win32; il login e i controlli finali
restano nel runtime MT5 esistente.

I componenti del PoC sono:

- `windows_agent/broker_registry.v1.json`: catalogo non sensibile dei server conosciuti;
- `windows_agent/broker_registry.py`: validazione del catalogo, risoluzione e CLI diagnostica;
- `windows_agent/broker_resolution.py`: piano zero-licenza anche per server non ancora catalogati;
- `windows_agent/worker/mt5_broker_discovery.py`: executor UIA credential-free e relativa CLI;
- `windows_agent/worker/wine_mt5_broker_discovery.py`: executor Win32 credential-free per Wine,
  senza coordinate, OCR o UIA;
- `windows_agent/worker/linux_wine_broker_discovery.py`: launcher Ubuntu confinato nel `drive_c`,
  predisposto ma non ancora collegato all'entrypoint Docker;
- `docs/windows/mt5-broker-discovery-lab.md`: evidenze di rete, ruolo di Procmon e decisione sul
  mancato replay del protocollo.

Il registry non è un elenco universale di tutti i broker. Ogni voce deve derivare da una verifica
controllata e può indicare un endpoint diretto oppure che il terminale deve eseguire la discovery.
Un broker sconosciuto non viene indovinato e non viene associato automaticamente al primo risultato
simile. Il resolver di provisioning può però inviare il nome server esatto al terminale come query
`TERMINAL_DISCOVERY`: il risultato deve contenere quello stesso server in modo esatto e univoco.

Il documento JSON ha `schema_version: 1`, una `revision` numerica e un array `profiles`. Ogni
profilo identifica broker e server tramite `profile_id`, `broker_id`, `broker_name`, `server`,
`environment`, `aliases`, `discovery_queries` e `source`. `connection_target` e
`target_verified_at` sono presenti soltanto quando esiste un endpoint diretto verificato.

## Contratto di risoluzione

Il resolver mantiene separati due concetti:

- `expected_server` è il nome canonico del server MT5 richiesto, per esempio
  `FPMTrading-Live`. Dopo l'autenticazione deve coincidere con l'identità restituita dal terminale;
- `connection_target` è il valore usato soltanto per stabilire la prima connessione. Può essere un
  endpoint `host:port` verificato oppure, nel flusso già supportato dal terminale, il nome del
  server. Non sostituisce mai `expected_server` nei controlli d'identità.

Questa distinzione è necessaria perché un terminale può collegarsi a un indirizzo di rete e poi
identificare la sessione con un nome server diverso. Il runtime non deve quindi confrontare
`host:port` con `AccountInfoString(ACCOUNT_SERVER)`.

Il resolver produce uno dei seguenti piani:

| Piano | Significato | Azione del chiamante |
| --- | --- | --- |
| `DIRECT` | Il registry possiede un target esplicito e verificato; nella CLI `method` vale `direct_endpoint`. | Usare `connection_target` nella prima configurazione MT5 e verificare comunque `expected_server` dopo il login. |
| `TERMINAL_DISCOVERY` | Il server è conosciuto, ma richiede la ricerca eseguita dalla sua istanza MT5; nella CLI `method` vale `terminal_discovery`. | Eseguire la ricerca unattended nell'istanza isolata e accettare soltanto `expected_server`; il helper non riceve credenziali. |
| `UNRESOLVED` | Il registry grezzo non contiene una voce; nella CLI diagnostica `method` vale `unresolved`. | Nel provisioning, il resolver zero-licenza converte un server sintatticamente valido in `TERMINAL_DISCOVERY` con query esatta; input non validi si fermano prima della decrittazione. |

Il flusso previsto è quindi:

```text
provision
  -> broker resolver
       -> DIRECT              -> login bootstrap
       -> TERMINAL_DISCOVERY  -> discovery unattended -> login bootstrap
       -> server non catalogato -> query esatta -> TERMINAL_DISCOVERY
  -> verifica login + expected_server + investor mode
  -> discovery simbolo e avvio del bridge read-only
```

La `TradeJournalDiscovery` MQL5 già presente nel repository risolve un simbolo del broker dopo la
connessione, per esempio `EURUSD.raw`. Non cerca il broker o il server e non sostituisce la Phase 0
UIA.

## Caso FPMTrading-Live

Nel registry del PoC `FPMTrading-Live` è classificato come `discovery_required`: non è associato a
un endpoint `host:port` verificato. La risoluzione deve pertanto produrre
`TERMINAL_DISCOVERY`, lasciando `connection_target` non valorizzato.

Questa classificazione attiva l'executor dell'agente. Il test controllato descritto sotto ha
validato il flusso funzionale su Ubuntu/Wine; gli executor Windows UIA e Wine/Win32 sono coperti da
test con backend falsi, ma richiedono ancora uno smoke end-to-end sulla relativa golden image prima
del rollout di produzione.

### Validazione controllata sulla VPS (22 luglio 2026)

Il flusso è stato provato su un terminale MetaQuotes generico, build 6036, dentro un desktop
virtuale Wine isolato. Prima della discovery il terminale accettava il nome
`FPMTrading-Live` nella configurazione ma non registrava alcuna autorizzazione o connessione al
broker. La procedura `File -> Open an Account`, con query `FPM Trading`, ha restituito il match
`FPM Trading Ltd.`.

Dopo la selezione del match, il journal ha confermato:

- autorizzazione sul server canonico `FPMTrading-Live`;
- sincronizzazione con `FPM Trading Ltd.` di 829 simboli;
- zero posizioni e zero ordini;
- trading e gestione del saldo disabilitati in modalità investor.

La discovery ha aggiornato soltanto il catalogo cifrato dell'istanza generica; non è stato copiato
il `servers.dat` del terminale brandizzato e non è stato inviato alcun ordine. È stato osservato un
access point di rete durante la sessione, ma non viene registrato come `DIRECT`: un singolo IP può
essere dinamico e richiede una verifica separata di stabilità e provenienza.

Una seconda prova funzionale ha usato una copia `/portable` nuova, senza directory `Config` e senza
credenziali. La ricerca ha trovato lo stesso broker e il wizard ha mostrato separatamente
`FPMTrading-Demo` e `FPMTrading-Live`. Annullare il wizard, però, non ha persistito il catalogo;
neppure una conferma con i campi login/password vuoti lo ha fatto. Questa build non consente quindi
di assumere che una ricerca annullata sopravviva al riavvio. L'executor deve mantenere la stessa
istanza e verificare esplicitamente la persistenza; se deve completare discovery e login nello stesso
processo, il bootstrap resta protetto e l'helper UIA non riceve mai la password.

Il test ha inoltre individuato un vecchio file INI temporaneo con permessi troppo larghi e password
in chiaro. Il file è stato cancellato in modo sicuro. Il runtime Windows del repository usa invece
ACL ristrette e attende la conferma `successfully initialized from start config` per cancellare il
file prima dell'autorizzazione. La Phase 0 parte senza credenziali, mantiene viva la stessa istanza
dopo la selezione e solo allora il runtime legge la password investor da DPAPI.

### Cattura di rete e Procmon sulla VPS (22 luglio 2026)

Un secondo laboratorio, questa volta sulla distribuzione FPM dell'immagine Docker e senza alcuna
credenziale, ha correlato TShark, `strace`, `inotifywait` e Procmon for Linux. Una query inesistente
ha aperto soltanto due sessioni TLS centrali; `FPM Trading` ha causato la sostituzione atomica di
`servers.dat`; `MetaQuotes` ha aggiunto un collegamento a un endpoint associato al risultato e ha
aumentato il file da 22.192 a 31.380 byte. Non sono comparsi DNS o SNI in chiaro.

Né `SSLKEYLOGFILE`, né un attach Frida limitato al lab, né uprobes bpftrace su una copia privata di
GnuTLS hanno prodotto il payload applicativo. Procmon è utile per correlare processi, file e timing,
ma non decifra TLS. Di conseguenza il progetto non replica il servizio di discovery e non hardcoda
gli IP osservati: usa il terminale come resolver compatibile. Evidenze, limiti e runbook sono in
`docs/windows/mt5-broker-discovery-lab.md`.

### Probe Win32/Wine senza coordinate (22 luglio 2026)

Un ulteriore container usa-e-getta ha installato il terminale MetaQuotes generico e un Python
Windows a 64 bit nello stesso Wine prefix. Il wizard ha esposto controlli Win32 stabili per query,
Find, lista risultati, Back, Next, Cancel e server canonico. `WM_SETTEXT`, `BM_CLICK`, selezione
della riga owner-data e lettura `WM_GETTEXT` dalla ComboBox hanno completato il percorso fino a
`MetaQuotes-Demo` senza account o password. Questo giustifica l'adapter Win32 dedicato, che prova
ogni riga aprendo la seconda pagina e non legge pixel o coordinate.

Il probe non costituisce ancora uno smoke del provisioning completo: deve essere dimostrato che la
stessa istanza che ha eseguito la discovery consumi poi il bootstrap `/config`. I container, volumi
e reti del laboratorio sono stati eliminati; il container di produzione è rimasto invariato.

## CLI del PoC

Dal root del repository:

```bash
python3 -m windows_agent.broker_registry --server FPMTrading-Live
```

Il comando interroga e valida soltanto il registry, quindi restituisce il piano di risoluzione. Non
avvia MetaTrader, non apre la GUI e non tenta il login. L'opzione `--server` riceve il nome server
richiesto o un alias esplicitamente catalogato, mai un account, una password investor o un token.

L'output JSON espone `method`, `requested_server`, `expected_server`, `connection_target`,
`discovery_queries`, gli identificatori del profilo/broker, ambiente e revisione del registry, oltre
a `matched_by` (`exact`, `casefold`, `alias` oppure `none`). Per una verifica su un catalogo
alternativo è disponibile `--registry PATH`.

I codici d'uscita distinguono un target immediatamente utilizzabile da un'azione successiva:

- `0`: `direct_endpoint`;
- `2`: `terminal_discovery`, risultato atteso per `FPMTrading-Live` nel PoC;
- `3`: `unresolved`, registry non valido o altro errore di risoluzione.

La CLI può essere usata anche come controllo preliminare in CI o durante l'onboarding. Un risultato
`TERMINAL_DISCOVERY` o `UNRESOLVED` non deve essere trasformato silenziosamente in `DIRECT`.

## Confini di sicurezza

- Non distribuire, aggregare o scaricare un `servers.dat` "universale". Gli artefatti prodotti da
  MetaTrader restano nella directory `/portable` della singola istanza e non vengono copiati tra
  clienti.
- Il registry contiene soltanto metadati di routing. Non contiene login, password, envelope cifrati,
  token, `accounts.dat` o altri file del profilo cliente.
- Un endpoint `host:port` può entrare nel percorso `DIRECT` soltanto dopo una verifica controllata;
  non deve essere accettato come destinazione arbitraria fornita dal browser.
- Server sconosciuti, hint e piani iniettati rifiutano IP, FQDN, localhost e target di rete
  evidenti. Questo filtro non sostituisce l'egress firewall: durante la discovery il
  namespace/container deve bloccare loopback, link-local, RFC1918 e altre reti riservate per
  impedire rebinding e nomi single-label interni.
- Il lookup nel registry avviene prima di decrittare la password investor. La discovery gira
  nella sola istanza isolata e il helper UIA/Win32 riceve esclusivamente percorso del terminale, PID,
  query e `expected_server`, mai account, password, token o envelope.
- Dopo ogni login restano obbligatori il match esatto di account/server e la verifica della modalità
  investor read-only. Una discovery riuscita non rende facoltativi questi controlli.

## Stato Phase 0

Sul percorso Windows nativo la Phase 0 è collegata al runtime con questo ordine:

1. risoluzione locale del piano prima di aprire l'envelope;
2. avvio `/portable` credential-free della sola copia associata al `connection_id`;
3. esecuzione UIA nella stessa sessione desktop Windows;
4. query ordinate e selezione del solo `expected_server` esatto e univoco;
5. chiusura del wizard mantenendo vivo il terminale e lettura della password da DPAPI solo dopo la
   discovery;
6. bootstrap ufficiale MT5, cancellazione anticipata dell'INI e controlli obbligatori su account,
   server canonico e modalità investor.

Il backend Win32 e il launcher confinato per Wine sono preparati separatamente, ma non sono ancora
collegati all'entrypoint Ubuntu, che oggi legge il bootstrap segreto prima della discovery. Il codice
usa controlli nominati/identificati e non coordinate, ha timeout e cleanup deterministici e non
scrive credenziali nei file di scambio. Restano tre gate prima della produzione: smoke
discovery→login sulla stessa istanza, isolamento egress contro reti interne e verifica dell'EULA per
l'impiego commerciale multi-cliente. I limiti dei due backend sono descritti nei documenti UIA e
Win32/Wine dedicati.
