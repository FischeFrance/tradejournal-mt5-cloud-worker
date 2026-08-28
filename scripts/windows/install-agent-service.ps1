#Requires -Version 5.1
#Requires -RunAsAdministrator
[CmdletBinding()]
param(
  [string]$DeploymentPython = 'C:\TradeJournal\releases\pool71a\.venv\Scripts\python.exe'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$repo = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$serviceName = 'TradeJournalMT5Agent'
$serviceModule = Join-Path $repo 'windows_agent\service\windows_service.py'
if (-not (Test-Path -LiteralPath $serviceModule -PathType Leaf)) {
  throw 'The Windows Agent service module is missing from the source checkout.'
}
if (-not (Test-Path -LiteralPath $DeploymentPython -PathType Leaf)) {
  throw 'Pinned deployment Python is missing.'
}
$scCommand = Get-Command sc.exe -CommandType Application -ErrorAction Stop

$previousPythonPath = $env:PYTHONPATH
Push-Location $repo
try {
  $env:PYTHONPATH = $repo
  # `windows_agent` is a repository package, not a separately installed wheel. Running the
  # module from the repository root keeps the installer and the resulting pywin32 service on
  # the same import path as the manually verified agent commands.
  $importCheck = 'import sys; import windows_agent.service.windows_service; sys.exit(0 if sys.version_info[:2]==(3,12) and sys.maxsize>2**32 else 1)'
  & $DeploymentPython -B -c $importCheck
  if ($LASTEXITCODE -ne 0) {
    throw "TradeJournal Agent service dependencies are unavailable (exit $LASTEXITCODE)."
  }

  & $DeploymentPython -B -m windows_agent.service.windows_service --startup auto install
  if ($LASTEXITCODE -ne 0) { throw "TradeJournal Agent service installation failed (exit $LASTEXITCODE)." }
  $service = Get-Service -Name $serviceName -ErrorAction Stop
  $serviceConfiguration = Get-CimInstance Win32_Service -Filter "Name='$serviceName'" -ErrorAction Stop
  if (
    $service.Status -ne 'Stopped' -or
    -not [string]::Equals(
      [string]$serviceConfiguration.StartName,
      'LocalSystem',
      [StringComparison]::OrdinalIgnoreCase
    )
  ) {
    throw 'TradeJournal Agent service was not installed as a stopped LocalSystem service.'
  }

  & $scCommand.Source failure $serviceName reset= 86400 actions= restart/5000/restart/15000/restart/60000
  if ($LASTEXITCODE -ne 0) { throw "TradeJournal Agent recovery policy configuration failed (exit $LASTEXITCODE)." }
  & $scCommand.Source failureflag $serviceName 1
  if ($LASTEXITCODE -ne 0) { throw "TradeJournal Agent non-crash recovery policy configuration failed (exit $LASTEXITCODE)." }
} finally {
  $env:PYTHONPATH = $previousPythonPath
  Pop-Location
}
Write-Host 'Service installed but not started. Use start-agent.ps1 explicitly.'
