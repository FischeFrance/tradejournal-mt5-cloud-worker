param([Parameter(Mandatory)][ValidatePattern('^[0-9a-fA-F-]{36}$')][string]$ConnectionId)
$ErrorActionPreference = 'Stop'
$repo = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
& "$repo\.venv\Scripts\python.exe" -m windows_agent.main poc --connection-id $ConnectionId --report C:\TradeJournal\logs\demo-poc-result.json
if ($LASTEXITCODE -ne 0) { throw 'Read-only POC failed.' }
Get-Content C:\TradeJournal\logs\demo-poc-result.json

