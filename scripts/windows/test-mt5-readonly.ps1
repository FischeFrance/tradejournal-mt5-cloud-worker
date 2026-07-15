$ErrorActionPreference = 'Stop'
$repo = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
& "$repo\.venv\Scripts\python.exe" -m pytest -q tests\windows
& "$repo\.venv\Scripts\python.exe" -m compileall -q windows_agent

