# Architettura Windows nativa

La decisione autorevole è
[ADR-001](../architecture/ADR-001-windows-native-file-bridge.md).

Il percorso predefinito è:

```text
terminal64.exe portable e isolato
  -> EA MQL5 TradeJournalBridge read-only
     -> file JSON nel sandbox per account
        -> Mql5FileMt5Adapter
           -> detector / normalizer / outbox / snapshot
              -> API TradeJournal
```

`NativeMt5Runtime` non importa il wheel Python `MetaTrader5`.
`build_real_handlers()` usa il file bridge salvo l'iniezione esplicita di un
adapter differente per test o migrazione.

Ogni account usa una directory UUID separata sotto
`C:\TradeJournal\instances`. Credenziali e token sono protetti con DPAPI e ACL,
mai inseriti negli argomenti dei processi o nei report.

La directory di un account viene preparata in staging e resa visibile con una
sola sostituzione durevole. Il terminale e l'EA sono vincolati ai digest
configurati dall'operatore; anche un'istanza già esistente viene ricontrollata
prima del riuso tramite un manifest separato dei file `.exe`, `.dll` ed `.ex5`.
Configurazione di avvio, `accounts.dat`, snapshot, checkpoint e outbox usano
scritture temporanee sincronizzate e sostituzioni atomiche.

L'agente esegue un job alla volta e supporta claim, heartbeat, running,
complete, fail, provision, deprovision, historical sync e live sync. Una lease
persa interrompe le attese runtime e impedisce sia la consegna successiva sia
`complete`. Gli eventi vengono mantenuti in outbox persistenti finché il
database non conferma l'ingestione atomica di audit, stato connessione e trade.
L'EA riserva durevolmente ogni numero di sequenza prima di pubblicare
`event-N.json`; il consumer rende durevole il batch nell'outbox prima di
avanzare il checkpoint e rimuovere i file già acquisiti. Una dead-letter
permanente blocca gli eventi causali successivi finché non viene risolta.

Il repository non contiene percorsi alternativi Docker/Wine o adapter Python
IPC diretto. Il laboratorio direct-endpoint è un harness separato e non
abilita il runtime di produzione.
