# Single customer flow

Il flusso normale riceve dal sito `connection_id`, login, server, password investor e modalità
storico. La password viene cifrata immediatamente con DPAPI e non entra mai nel job. L'agente
esegue provisioning, avvio `/portable`, `MetaTrader5.initialize`, login API, verifica identità e
`trade_allowed == false`, import storico, live poll e heartbeat. Nessun passaggio cliente usa GUI,
RDP, script o percorsi Windows.

## Test DEMO reale

Eseguire `scripts/windows/run-single-customer-flow.ps1`. Le credenziali sono lette in modo
interattivo; la password usa `Read-Host -AsSecureString`, passa su stdin e non appare nei parametri
processo o nel report. Usare esclusivamente un account DEMO e password investor.

## Template globale una tantum

Prima provare il flusso headless senza template. Se e solo se `MetaTrader5.initialize` fallisce
per inizializzazione del terminale, eseguire `scripts/windows/prepare-mt5-template.ps1`, avviare
una sola volta il terminale template con `/portable` per completare l'inizializzazione globale,
quindi chiuderlo senza salvare credenziali. Questa è preparazione VPS e non fa parte del flusso
cliente. Ogni nuova istanza riceve poi una copia completa dell'installazione/template.

Gli errori sono esposti soltanto come `final_status=error:<codice>`; password, login completo,
server completo, token e trade non vengono inclusi nel report.

## Compatibilita' IPC

Prima del provisioning reale l'adapter verifica la coppia terminale/wheel. Al 2026-07-15 il
terminale MetaQuotes build 5836 non comunica con il wheel MetaTrader5 5.0.5735 e restituisce
`-10005 IPC timeout`; lo stesso break e' documentato da build 5833. Il flusso fallisce quindi
rapidamente come `mt5_ipc_failed`, senza avviare login o lasciare processi. Non usare binari
terminali non ufficiali: attendere un wheel compatibile o usare un terminale precedente fornito
da una fonte ufficiale/supportata.

Il runtime agente deve operare in una sessione Windows interattiva persistente. Se lo script di
test viene avviato da Session 0, memorizza prima i secret in DPAPI e inoltra soltanto connection ID
e history mode a un task `InteractiveToken`; nessun secret compare negli argomenti del processo.
