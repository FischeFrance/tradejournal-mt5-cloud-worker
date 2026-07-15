$ErrorActionPreference='Stop'
$source='C:\Program Files\MetaTrader 5'
$target='C:\TradeJournal\mt5-template'
if(-not(Test-Path "$source\terminal64.exe")){throw 'MT5 installation not found.'}
if(Test-Path $target){throw 'Template exists; refusing overwrite.'}
$repo=Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
& "$repo\.venv\Scripts\python.exe" -m windows_agent.initialize_probe --terminal "$source\terminal64.exe"
if($LASTEXITCODE -ne 0){throw 'MT5/Python IPC is incompatible; template creation cannot fix it.'}
Copy-Item $source $target -Recurse
Write-Host 'IPC-compatible template copied; no customer GUI step is required.'
