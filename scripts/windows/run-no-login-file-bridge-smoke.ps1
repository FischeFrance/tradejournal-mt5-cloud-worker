$ErrorActionPreference = 'Stop'

$repo = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$python = Join-Path $repo '.venv\Scripts\python.exe'
$expert = 'C:\TradeJournal\artifacts\mql5\TradeJournalBridge.ex5'
$report = 'C:\TradeJournal\logs\file-bridge-no-login-heartbeat.json'

if (-not (Test-Path $python)) { throw 'Windows agent virtual environment not found.' }
if (-not (Test-Path $expert)) { throw 'Compiled read-only EA not found.' }

& $python -m windows_agent.no_login_heartbeat `
    --source-terminal 'C:\Program Files\MetaTrader 5\terminal64.exe' `
    --expert $expert `
    --work-root 'C:\TradeJournal\build\file-bridge-heartbeat' `
    --report $report `
    --timeout 120
exit $LASTEXITCODE
