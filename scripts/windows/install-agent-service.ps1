$repo = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
& "$repo\.venv\Scripts\python.exe" -m windows_agent.service.windows_service --startup auto install
Write-Host 'Service installed but not started. Use start-agent.ps1 explicitly.'

