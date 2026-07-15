$repo = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
& "$repo\.venv\Scripts\python.exe" -m windows_agent.service.windows_service remove

