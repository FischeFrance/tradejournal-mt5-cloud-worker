# Architettura Windows nativa

Il percorso primario è `terminal64.exe` nativo -> Expert Advisor read-only -> file JSON atomici ->
adapter read-only -> detector/normalizer/outbox persistente -> API TradeJournal.
Docker, Wine, EA MQL5, bridge HTTP Linux e provisioning systemd restano nel repository come
legacy documentato e non vengono eliminati. Ogni account usa una directory UUID separata sotto
`C:\TradeJournal\instances`; non esistono porte inbound né endpoint di produzione configurati.

V1 esegue un job alla volta. State/checkpoint sono atomici; dedup è persistente SQLite. Il
managed agent supporta claim, heartbeat di lease, running, complete, fail, provision,
deprovision e historical_sync. Un complete è vietato dopo perdita della lease.

Il control plane non esegue polling. `mt5_provisioning_jobs` è la coda durevole e Supabase
Realtime Broadcast privato è soltanto il wake-up: startup, reconnect e `command_available`
provocano un drain fino al primo 204. Un Broadcast perso non perde il comando, perché il drain
di recovery rilegge sempre la tabella autorevole.

Al riavvio del servizio, le istanze pubblicate che non hanno più il proprio processo MT5 vengono
recuperate con un singolo tentativo locale e passwordless. Il recupero usa la sessione protetta di
MT5, verifica i pin di integrità e non crea comandi nel control plane. Non esiste quindi alcun job
periodico di liveness necessario per riaccendere un terminale dopo il reboot della VPS.

Il data plane è separato. `watchdog` osserva localmente `events/event-*.json` e
`heartbeat.json`; un evento MT5 provoca un solo diff degli snapshot. Prima della rete ogni
payload entra in `state/trading-ingestion-outbox.json`. `heartbeat.json` non produce chiamate
periodiche: viene inviato un aggiornamento remoto soltanto quando `terminal_connected` cambia.
