# Architettura Windows nativa

Il percorso primario è `terminal64.exe` nativo -> pacchetto Python ufficiale MetaTrader5 ->
adapter read-only -> detector/normalizer/outbox/snapshot esistenti -> futura API TradeJournal.
Docker, Wine, EA MQL5, bridge HTTP Linux e provisioning systemd restano nel repository come
legacy documentato e non vengono eliminati. Ogni account usa una directory UUID separata sotto
`C:\TradeJournal\instances`; non esistono porte inbound né endpoint di produzione configurati.

V1 esegue un job alla volta. State/checkpoint sono atomici; dedup è persistente SQLite. Il
managed agent supporta claim, heartbeat, running, complete, fail, provision, deprovision e
historical_sync. Un complete è vietato dopo perdita della lease.

