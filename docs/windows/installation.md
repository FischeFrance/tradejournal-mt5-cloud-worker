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

Configurare il servizio con entrambi gli endpoint HTTPS:

```text
TRADEJOURNAL_API_URL=https://<project-ref>.functions.supabase.co/trading-agent
TRADEJOURNAL_TRADING_INGESTION_URL=https://<project-ref>.functions.supabase.co/trading-mt5-events
```

`TRADEJOURNAL_POLL_SECONDS` non è più letto. Le sole riconnessioni con backoff sono quelle del
WebSocket Realtime; nessun timer produce chiamate `claim` o heartbeat account.
