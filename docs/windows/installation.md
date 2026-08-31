# Installazione

Eseguire PowerShell come amministratore dal repository:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\windows\bootstrap-server.ps1
C:\Program Files\Python312\python.exe -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-windows.txt
powershell -ExecutionPolicy Bypass -File scripts\windows\install-mt5.ps1
```

Python 3.12 x64, VC++ x64 e MT5 devono provenire dalle fonti ufficiali. Nessun riavvio è
richiesto dal progetto. Verificare `terminal64.exe` e import MetaTrader5 prima del POC.

## Sessione interattiva per broker discovery

La Phase 0 UIA richiede un utente locale dedicato con sessione desktop attiva e terminale in
inglese. Impostare il nome come variabile macchina e riavviare soltanto il servizio dell'agente:

```powershell
[Environment]::SetEnvironmentVariable(
  "TRADEJOURNAL_MT5_INTERACTIVE_USER",
  "TradeJournalDesktop",
  "Machine"
)
```

L'utente deve essere standard, dedicato e non amministratore. Deve poter leggere/eseguire il
repository e il relativo `.venv\Scripts\python.exe` e scrivere nella sola directory dell'istanza
MT5. Non inserire password Windows, account MT5 o token nella variabile. Il runtime avvia terminale
e helper UIA con scheduled task `/IT` a livello `LIMITED`, passa soltanto percorso, PID, query e
server atteso e cancella i file di scambio al termine. Una sessione assente, bloccata o una UI
localizzata/non riconosciuta deve produrre `broker_resolution_failed`, mai un fallback a coordinate.

Il launcher usa esplicitamente il Python del virtualenv; `sys.executable` dentro un servizio
pywin32 può indicare `PythonService.exe` e non è un interprete CLI valido. All'avvio del daemon gli
eventuali `login-bootstrap.ini` lasciati da un crash vengono cancellati e ne viene verificata
l'assenza prima di accettare nuovi job.

Prima del rollout eseguire lo smoke test con la stessa build della golden template e verificare che
la pagina `Open an Account` esponga via UIA sia il server Live sia il server Demo come elementi
distinti.
