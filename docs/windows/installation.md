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

