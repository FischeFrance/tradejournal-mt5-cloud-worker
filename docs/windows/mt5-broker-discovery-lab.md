# MT5 broker discovery: laboratorio di rete e Procmon

## Esito

Il comando **Find your broker** non è stato promosso a protocollo da replicare. Nel test del
22 luglio 2026 MetaTrader ha eseguito una discovery centrale su TLS, ha ricevuto risultati di
dimensione variabile, ha aggiornato atomicamente `Config/servers.dat` e, per un risultato nuovo,
ha contattato anche un endpoint associato al broker trovato. Il contenuto applicativo è rimasto
cifrato e non è emerso un contratto stabile o documentato su cui costruire un client indipendente.

Il percorso supportato dal progetto resta quindi:

```text
server richiesto
  -> registry validato
  -> endpoint diretto verificato, se disponibile
     oppure
  -> discovery unattended in un terminale MT5 isolato
  -> verifica del server canonico
  -> login e controlli investor/read-only
```

La discovery è *headless* dal punto di vista dell'operatore: il worker può usare un desktop
virtuale o Windows UI Automation, ma nessuna persona deve usare la GUI. `servers.dat` rimane una
cache opaca dell'istanza; non diventa un database universale mantenuto dal progetto.

## Confine del test

Il laboratorio era un container Docker distinto dalla sessione cliente attiva, con rete e volume
dedicati, senza porte pubblicate e senza login, password o altri secret. Il terminale proveniva
dall'immagine broker FPM già disponibile sulla VPS; il risultato deve quindi essere ripetuto su una
build MetaQuotes generica prima di considerarlo una proprietà invariabile di tutte le distribuzioni
MT5.

Il container di produzione non è stato riavviato, agganciato da debugger o usato come sorgente di
file. Tutti i probe applicativi sono stati limitati al processo di laboratorio.

## Strumenti e responsabilità

| Strumento | Evidenza utile | Limite |
| --- | --- | --- |
| `tcpdump`/TShark | Connessioni, direzioni, timing e dimensioni dei record TLS. | Non mostra il payload applicativo cifrato. |
| `strace` | `connect`, socket trasferiti tra processi Wine e operazioni sui file. | Non ricostruisce la semantica del protocollo. |
| `inotifywait` | Creazione di `servers.dat.new` e sostituzione atomica di `servers.dat`. | Non interpreta il formato del file. |
| Procmon for Linux 2.2.1 | Correlazione temporale tra PID, filesystem, rete e lifecycle del terminale. | Non segue automaticamente tutti i processi Wine, tronca alcuni campi e non decifra TLS. |
| `SSLKEYLOGFILE` | Ha prodotto chiavi in un controllo GnuTLS separato. | Wine/MT5 non ha emesso un key log durante la discovery. |
| Frida | Avrebbe permesso un hook in-process. | L'attach al solo lab è risultato instabile ed è stato abbandonato. |
| bpftrace uprobe | Tentativo senza injection sulle API record di una copia privata di GnuTLS. | Nessuna chiamata osservata: MT5 usa un altro percorso oppure cifra prima di GnuTLS. |

Procmon è quindi una diagnostica opzionale, mai un requisito del provisioning e mai il criterio che
decide se un broker è stato risolto. Una cattura deve avere PID espliciti, durata massima, directory
`0700`, file `0600` e query priva di credenziali. Le tracce non vanno raccolte sul terminale cliente
attivo.

## Osservazioni riproducibili

Il baseline del wizard lasciato inattivo per 35 secondi non ha generato pacchetti. Le query sono
state poi inviate nello stesso profilo pulito:

| Caso | Risultato osservato |
| --- | --- |
| Broker inesistente, prima prova | 34 pacchetti; due connessioni TLS 1.3 verso `194.164.179.28:443` e `194.164.179.33:443`; nessun DNS e nessun SNI visibile. |
| Broker inesistente, ripetizione | Stessi endpoint e forma del traffico; nessun aggiornamento di `servers.dat`. |
| `FPM Trading` | Stessi endpoint centrali; creazione di `servers.dat.new` e sostituzione di `servers.dat`. |
| `MetaQuotes` | Endpoint centrali più TLS verso `94.130.2.36:443`; nuovo risultato `MetaQuotes Ltd.`; crescita di `servers.dat` da 22.192 a 31.380 byte. |

La prima SYN è partita circa 139 ms dopo il submit. Le richieste cifrate cambiavano lievemente con
la lunghezza della query; la risposta centrale cresceva con i risultati restituiti. Nel caso
`MetaQuotes`, il terzo collegamento è iniziato dopo la risposta centrale. Questa è un'inferenza
temporale coerente con "lookup centrale, poi probe del broker", non una decodifica del payload.

`servers.dat` inizia con un'intestazione di copyright MetaQuotes in UTF-16, seguita da dati binari
opachi. Il file è stato riscritto con una sequenza `.new` più rename. Il cambio di hash prova che la
cache è cambiata, non che il server scelto sia quello corretto: il gate applicativo deve restare il
match esatto del server mostrato e, dopo il login, l'identità restituita da MT5.

## Decisione tecnica

Un replay autonomo richiederebbe almeno la decodifica del protocollo applicativo, la gestione di
eventuali chiavi o firme, compatibilità per build e una prova che il servizio sia destinato a client
non-MetaTrader. Queste condizioni non sono state dimostrate. Hardcodare gli IP osservati o
distribuire un `servers.dat` aggregato introdurrebbe invece dipendenze fragili e un rischio di
instradamento errato.

La soluzione scalabile usa due livelli:

1. Il registry risolve alias, ambiente Live/Demo, query ordinate e gli eventuali endpoint diretti
   già verificati. Un server sconosciuto sintatticamente valido passa alla ricerca esatta del
   terminale; non viene mai promosso automaticamente a endpoint diretto.
2. Su cache miss, un worker isolato avvia l'esatto terminale, esegue la ricerca unattended, accetta
   soltanto il server canonico atteso e conserva la cache nella propria istanza. Il login parte solo
   dopo la risoluzione e viene comunque verificato per account, server e modalità investor.

La portabilità di una cache tra due installazioni pulite deve essere dimostrata per la stessa
provenienza e build prima di introdurre qualsiasi riuso. Fino ad allora non si copia `servers.dat`
tra clienti e non si modifica la golden template con dati prodotti da un'istanza cliente.

## Strumento di input del laboratorio

`tools/mt5_lab_input.c` è un piccolo helper XTest usato esclusivamente per rendere ripetibili le
query sul desktop virtuale 1024x768. Usa coordinate e non legge finestre, risultati o credenziali:
non è il futuro executor e non deve essere incluso nel runtime di produzione. Gli executor UIA e
Win32 si legano invece al PID e al percorso esatto del terminale, usano match esatti e falliscono su
zero risultati, ambiguità o UI sconosciuta.

## Riferimenti

- [MetaTrader 5: apertura di un conto e ricerca del broker](https://www.metatrader5.com/en/terminal/help/startworking/acc_open)
- [MetaTrader 5: sicurezza e cifratura del traffico](https://www.metatrader5.com/en/terminal/help/start_advanced/security)
- [Microsoft Procmon for Linux](https://github.com/microsoft/ProcMon-for-Linux)
- [Microsoft Sysinternals Process Monitor](https://learn.microsoft.com/en-us/sysinternals/downloads/procmon)
- [Wireshark: TLS decryption e key log](https://wiki.wireshark.org/tls)
