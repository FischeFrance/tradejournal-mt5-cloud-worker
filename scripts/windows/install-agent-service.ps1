$repo = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
Push-Location $repo
try {
  # `windows_agent` is a repository package, not a separately installed wheel. Running the
  # module from the repository root keeps the installer and the resulting pywin32 service on
  # the same import path as the manually verified agent commands.
  & ".\.venv\Scripts\python.exe" -m windows_agent.service.windows_service --startup auto install
  if ($LASTEXITCODE -ne 0) { throw "TradeJournal Agent service installation failed (exit $LASTEXITCODE)." }
} finally {
  Pop-Location
}
Write-Host 'Service installed but not started. Use start-agent.ps1 explicitly.'
