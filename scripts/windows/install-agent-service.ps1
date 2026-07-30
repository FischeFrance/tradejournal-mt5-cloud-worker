param(
  [Parameter(Mandatory = $true)]
  [string]$ReleaseRoot,
  [Parameter(Mandatory = $true)]
  [string]$PythonExecutable
)

$repo = (Resolve-Path -LiteralPath $ReleaseRoot -ErrorAction Stop).Path
$python = (Resolve-Path -LiteralPath $PythonExecutable -ErrorAction Stop).Path
if (-not (Test-Path -LiteralPath (Join-Path $repo 'release-manifest.json') -PathType Leaf)) {
  throw 'Release manifest is missing: refusing to install a mutable working copy.'
}
Push-Location $repo
try {
  # The service runs only content that has been checked against the immutable
  # release manifest.  The Python runtime is intentionally external to the
  # release so a venv update cannot silently alter released source code.
  # Validation itself must not mutate the immutable release by materialising
  # ``__pycache__`` files that are intentionally absent from its manifest.
  & $python -B -c "from windows_agent.release_manifest import verify_release; verify_release(r'$repo')"
  if ($LASTEXITCODE -ne 0) {
    throw "Release manifest verification failed (exit $LASTEXITCODE)."
  }

  # A Python service may still materialise bytecode even when started through
  # pythonservice.exe. Keep any such cache outside the manifest-bound release
  # and deny write access to the release itself for both service and admins.
  $bytecodeCache = 'C:\TradeJournal\pycache'
  New-Item -ItemType Directory -Path $bytecodeCache -Force | Out-Null
  & icacls.exe $repo /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)(RX)' '*S-1-5-32-544:(OI)(CI)(RX)' | Out-Null
  if ($LASTEXITCODE -ne 0) {
    throw "Release immutability ACL registration failed (exit $LASTEXITCODE)."
  }
  $existingService = Get-Service -Name TradeJournalMT5Agent -ErrorAction SilentlyContinue
  if ($existingService -and $existingService.Status -ne 'Stopped') {
    throw 'TradeJournal Agent service must be stopped before update.'
  }
  $serviceCommand = if ($existingService) { 'update' } else { 'install' }

  # `windows_agent` is a repository package, not a separately installed wheel. Running the
  # module from the repository root keeps the installer and the resulting pywin32 service on
  # the same import path as the manually verified agent commands.
  & $python -m windows_agent.service.windows_service --startup auto $serviceCommand
  if ($LASTEXITCODE -ne 0) {
    throw "TradeJournal Agent service $serviceCommand failed (exit $LASTEXITCODE)."
  }

  # pythonservice.exe embeds Python before importing the registered service class. In a venv it
  # does not reliably process pywin32.pth, so servicemanager.pyd and the other win32 modules can
  # be invisible even though normal `python.exe` imports work. Scope PYTHONPATH to this service
  # only: a machine-wide value would affect unrelated Python workloads on the VPS.
  $venvRoot = Split-Path (Split-Path $python -Parent) -Parent
  $sitePackages = Join-Path $venvRoot 'Lib\site-packages'
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
  $preserved = @($existing | Where-Object {
    $_ -and $_ -notlike 'PYTHONPATH=*' -and $_ -notlike 'PYTHONDONTWRITEBYTECODE=*' -and $_ -notlike 'PYTHONPYCACHEPREFIX=*'
  })
  $serviceEnvironment = @($preserved) + @(
    "PYTHONPATH=$pythonPath"
    "PYTHONDONTWRITEBYTECODE=1"
    "PYTHONPYCACHEPREFIX=$bytecodeCache"
  )
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
  if (@($stored | Where-Object { $_ -eq 'PYTHONDONTWRITEBYTECODE=1' }).Count -ne 1) {
    throw 'TradeJournal Agent service bytecode suppression registration failed.'
  }
  if (@($stored | Where-Object { $_ -eq "PYTHONPYCACHEPREFIX=$bytecodeCache" }).Count -ne 1) {
    throw 'TradeJournal Agent service bytecode cache registration failed.'
  }

  # A configuration failure must terminate the service process (see
  # windows_service.py). Configure SCM to restart that failed process without
  # touching already-running MT5 instances; the agent reconciles them on boot.
  & sc.exe failure TradeJournalMT5Agent reset= 86400 actions= restart/60000/restart/60000/restart/300000
  if ($LASTEXITCODE -ne 0) {
    throw "TradeJournal Agent service recovery registration failed (exit $LASTEXITCODE)."
  }
  & sc.exe failureflag TradeJournalMT5Agent 1
  if ($LASTEXITCODE -ne 0) {
    throw "TradeJournal Agent service recovery failure flag registration failed (exit $LASTEXITCODE)."
  }
} finally {
  Pop-Location
}
Write-Host "Service $serviceCommand but not started. Use start-agent.ps1 explicitly."
