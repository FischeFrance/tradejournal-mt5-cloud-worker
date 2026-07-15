param([Parameter(Mandatory)][ValidatePattern('^[0-9a-fA-F-]{36}$')][string]$ConnectionId,[Parameter(Mandatory)][string]$TerminalPath)
$ErrorActionPreference = 'Stop'
$repo = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
& "$repo\.venv\Scripts\python.exe" -m windows_agent.main provision --connection-id $ConnectionId --terminal $TerminalPath
if ($LASTEXITCODE -ne 0) { throw 'Instance creation failed.' }

