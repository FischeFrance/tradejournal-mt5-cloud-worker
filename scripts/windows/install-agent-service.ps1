$repo = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
Push-Location $repo
try {
  $existingService = Get-Service -Name TradeJournalMT5Agent -ErrorAction SilentlyContinue
  if ($existingService -and $existingService.Status -ne 'Stopped') {
    throw 'TradeJournal Agent service must be stopped before update.'
  }
  $serviceCommand = if ($existingService) { 'update' } else { 'install' }

  # `windows_agent` is a repository package, not a separately installed wheel. Running the
  # module from the repository root keeps the installer and the resulting pywin32 service on
  # the same import path as the manually verified agent commands.
  & ".\.venv\Scripts\python.exe" -m windows_agent.service.windows_service --startup auto $serviceCommand
  if ($LASTEXITCODE -ne 0) {
    throw "TradeJournal Agent service $serviceCommand failed (exit $LASTEXITCODE)."
  }

  # pythonservice.exe embeds Python before importing the registered service class. In a venv it
  # does not reliably process pywin32.pth, so servicemanager.pyd and the other win32 modules can
  # be invisible even though normal `python.exe` imports work. Scope PYTHONPATH to this service
  # only: a machine-wide value would affect unrelated Python workloads on the VPS.
  $sitePackages = Join-Path $repo '.venv\Lib\site-packages'
  $pythonPath = @(
    $repo
    $sitePackages
    (Join-Path $sitePackages 'win32')
    (Join-Path $sitePackages 'win32\lib')
    (Join-Path $sitePackages 'Pythonwin')
  ) -join ';'
  $serviceKey = 'HKLM:\SYSTEM\CurrentControlSet\Services\TradeJournalMT5Agent'
  $existing = @(
    (Get-ItemProperty -Path $serviceKey -Name Environment -ErrorAction SilentlyContinue).Environment
  )
  $preserved = @($existing | Where-Object { $_ -and $_ -notlike 'PYTHONPATH=*' })
  $serviceEnvironment = @($preserved) + @("PYTHONPATH=$pythonPath")
  New-ItemProperty `
    -Path $serviceKey `
    -Name Environment `
    -PropertyType MultiString `
    -Value $serviceEnvironment `
    -Force | Out-Null

  $stored = @((Get-ItemProperty -Path $serviceKey -Name Environment).Environment)
  if (@($stored | Where-Object { $_ -like 'PYTHONPATH=*' }).Count -ne 1) {
    throw 'TradeJournal Agent service PYTHONPATH registration failed.'
  }
} finally {
  Pop-Location
}
Write-Host "Service $serviceCommand but not started. Use start-agent.ps1 explicitly."
