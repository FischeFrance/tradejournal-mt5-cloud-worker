# Runbook POC DEMO read-only

1. Creare un nuovo connection ID: `[guid]::NewGuid().ToString()`.
2. Eseguire `create-instance.ps1` indicando il `terminal64.exe` installato.
3. Avviare una volta il terminale copiato se Windows richiede l'inizializzazione grafica, poi
   chiuderlo senza configurare credenziali master.
4. Eseguire `prepare-demo-credentials.ps1`; fornire solo login DEMO, server DEMO e password
   investor tramite prompt protetto.
5. Eseguire `run-demo-poc.ps1` e leggere `C:\TradeJournal\logs\demo-poc-result.json`.

Il report contiene soltanto booleani e conteggi allowlisted. `PASS` richiede inizializzazione,
autorizzazione e identità attesa. Non mostra account/server completi o trade. In caso di mismatch
l'elaborazione si ferma e non produce eventi. Chiudere con `stop-agent.ps1` se il servizio è
stato installato manualmente e verificare che non restino processi `terminal64`.

