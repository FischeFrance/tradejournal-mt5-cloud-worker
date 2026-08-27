param(
  [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')][string]$Revision,
  [Parameter(Mandatory = $true)][string]$SourceRoot,
  [int]$ExpectedTerminalCount = 0,
  [string]$RecoveryConnectionId = ''
)

$ErrorActionPreference = 'Stop'
$serviceName = 'TradeJournalMT5Agent'
$releaseRoot = 'C:\TradeJournal\releases'
$currentPath = 'C:\TradeJournal\current'
$pythonExe = 'C:\TradeJournal\releases\pool71a\.venv\Scripts\python.exe'
$serviceRegistry = "HKLM:\SYSTEM\CurrentControlSet\Services\$serviceName"
$goldenExpert = 'C:\TradeJournal\mt5-template\MQL5\Experts\TradeJournal\TradeJournalBridge.ex5'
$releasePath = Join-Path $releaseRoot ("agent-" + $Revision.Substring(0, 12))
$backupRoot = Join-Path 'C:\TradeJournal\backups' ("history-import-" + $Revision.Substring(0, 12))

if (-not (Test-Path $SourceRoot -PathType Container)) { throw 'Staged source root is missing.' }
if (-not (Test-Path $pythonExe -PathType Leaf)) { throw 'Pinned deployment Python is missing.' }
if (-not (Test-Path $goldenExpert -PathType Leaf)) { throw 'Golden bridge binary is missing.' }
if (Test-Path $releasePath) { throw 'Release already exists.' }

$previousPythonPath = $env:PYTHONPATH
$env:PYTHONPATH = $SourceRoot
try {
  & $pythonExe -B -m pytest -q `
    (Join-Path $SourceRoot 'tests\windows\test_contract.py') `
    (Join-Path $SourceRoot 'tests\windows\test_atomic_file_retry.py') `
    (Join-Path $SourceRoot 'tests\windows\test_historical_trade_import.py') `
    (Join-Path $SourceRoot 'tests\windows\test_mt5_live_update.py') `
    (Join-Path $SourceRoot 'tests\windows\test_mt5_template_rotation.py') `
    (Join-Path $SourceRoot 'tests\windows\test_startup_recovery.py') `
    (Join-Path $SourceRoot 'tests\test_mql5_ea_no_trading.py')
  if ($LASTEXITCODE -ne 0) { throw 'Windows release tests failed.' }

  & (Join-Path $SourceRoot 'scripts\windows\compile-readonly-ea.ps1')
  $compiledExpert = 'C:\TradeJournal\artifacts\mql5\TradeJournalBridge.ex5'
  if (-not (Test-Path $compiledExpert -PathType Leaf)) { throw 'Compiled bridge is missing.' }
  $expertSha256 = (Get-FileHash $compiledExpert -Algorithm SHA256).Hash.ToLowerInvariant()

  $buildCode = 'import sys; from windows_agent.release_manifest import build_release; print(build_release(sys.argv[1], sys.argv[2], revision=sys.argv[3]))'
  & $pythonExe -B -c $buildCode $SourceRoot $releaseRoot $Revision
  if ($LASTEXITCODE -ne 0 -or -not (Test-Path $releasePath -PathType Container)) {
    throw 'Immutable Agent release build failed.'
  }
  $env:PYTHONPATH = $releasePath
  $verifyCode = 'import sys; from windows_agent.release_manifest import verify_release; verify_release(sys.argv[1])'
  & $pythonExe -B -c $verifyCode $releasePath
  if ($LASTEXITCODE -ne 0) { throw 'Immutable Agent release verification failed.' }
} finally {
  $env:PYTHONPATH = $previousPythonPath
}

$service = Get-Service -Name $serviceName
$oldEnvironment = @((Get-ItemProperty $serviceRegistry -Name Environment).Environment)
$oldCurrentTarget = [string]((Get-Item $currentPath).Target | Select-Object -First 1)
$terminalCountBefore = @(Get-Process terminal64 -ErrorAction SilentlyContinue).Count
if ($ExpectedTerminalCount -le 0) { $ExpectedTerminalCount = $terminalCountBefore }
if ($RecoveryConnectionId -and $RecoveryConnectionId -notmatch '^[0-9a-f-]{36}$') {
  throw 'Recovery connection id is invalid.'
}
$pythonPathFound = $false
$expertPinFound = $false
$nextEnvironment = @(
  foreach ($entry in $oldEnvironment) {
    if ($entry -like 'PYTHONPATH=*') {
      $parts = @($entry.Substring('PYTHONPATH='.Length).Split(';'))
      if ($parts.Count -lt 1) { throw 'Service PYTHONPATH is invalid.' }
      $parts[0] = $releasePath
      $pythonPathFound = $true
      'PYTHONPATH=' + ($parts -join ';')
    } elseif ($entry -like 'TRADEJOURNAL_MT5_EXPERT_SHA256=*') {
      $expertPinFound = $true
      'TRADEJOURNAL_MT5_EXPERT_SHA256=' + $expertSha256.ToUpperInvariant()
    } else {
      $entry
    }
  }
)
if (-not $pythonPathFound -or -not $expertPinFound) {
  throw 'Required service environment pins are missing.'
}

New-Item -ItemType Directory -Force $backupRoot | Out-Null
$expertBackup = Join-Path $backupRoot 'TradeJournalBridge.ex5'
Copy-Item $goldenExpert $expertBackup

try {
  if ($service.Status -ne 'Stopped') {
    Stop-Service -Name $serviceName -Force
    (Get-Service -Name $serviceName).WaitForStatus('Stopped', [TimeSpan]::FromSeconds(30))
  }

  $expertStage = $goldenExpert + '.new'
  Copy-Item $compiledExpert $expertStage -Force
  if ((Get-FileHash $expertStage -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expertSha256) {
    throw 'Golden bridge staging hash mismatch.'
  }
  Move-Item $expertStage $goldenExpert -Force
  Set-ItemProperty $serviceRegistry -Name Environment -Value $nextEnvironment

  if (Test-Path $currentPath) { cmd.exe /c "rmdir $currentPath" | Out-Null }
  New-Item -ItemType Junction -Path $currentPath -Target $releasePath | Out-Null

  Start-Service -Name $serviceName
  (Get-Service -Name $serviceName).WaitForStatus('Running', [TimeSpan]::FromSeconds(30))
  $recoveryDeadline = (Get-Date).AddMinutes(6)
  $terminalCountAfter = -1
  $liveUpdateCountAfter = -1
  $recoveryProcessCountAfter = if ($RecoveryConnectionId) { -1 } else { 1 }
  $stableSince = $null
  do {
    Start-Sleep -Seconds 2
    if ((Get-Service -Name $serviceName).Status -ne 'Running') {
      throw 'Agent service did not remain running.'
    }
    $terminalProcesses = @(Get-CimInstance Win32_Process | Where-Object {
      $_.Name -eq 'terminal64.exe'
    })
    $terminalCountAfter = $terminalProcesses.Count
    $liveUpdateCountAfter = @($terminalProcesses | Where-Object {
      [string]$_.ExecutablePath -match '\\liveupdate\\terminal64\.exe$'
    }).Count
    if ($RecoveryConnectionId) {
      $expectedRecoveryPath = "C:\TradeJournal\instances\$RecoveryConnectionId\terminal\terminal64.exe"
      $recoveryProcessCountAfter = @($terminalProcesses | Where-Object {
        [string]::Equals(
          [string]$_.ExecutablePath,
          $expectedRecoveryPath,
          [StringComparison]::OrdinalIgnoreCase
        )
      }).Count
    }
    $healthy = (
      $terminalCountAfter -eq $ExpectedTerminalCount -and
      $liveUpdateCountAfter -eq 0 -and
      $recoveryProcessCountAfter -eq 1
    )
    if ($healthy) {
      if ($null -eq $stableSince) { $stableSince = Get-Date }
    } else {
      $stableSince = $null
    }
  } while (
    ($null -eq $stableSince -or ((Get-Date) - $stableSince).TotalSeconds -lt 30) -and
    (Get-Date) -lt $recoveryDeadline
  )
  if ($liveUpdateCountAfter -ne 0) {
    throw 'Agent rollout left a MetaQuotes LiveUpdate process running.'
  }
  if ($terminalCountAfter -ne $ExpectedTerminalCount) {
    throw 'Agent rollout did not reach the expected terminal count.'
  }
  if ($recoveryProcessCountAfter -ne 1) {
    throw 'Agent rollout did not keep the recovery terminal running.'
  }
  if ($null -eq $stableSince -or ((Get-Date) - $stableSince).TotalSeconds -lt 30) {
    throw 'Agent rollout did not reach a stable terminal state.'
  }

  [pscustomobject]@{
    release = $releasePath
    source_revision = $Revision
    expert_sha256 = $expertSha256
    service = 'running'
    terminal_count_before = $terminalCountBefore
    terminal_count_expected = $ExpectedTerminalCount
    terminal_count_after = $terminalCountAfter
    live_update_count_after = $liveUpdateCountAfter
    recovery_process_count_after = $recoveryProcessCountAfter
  } | ConvertTo-Json
} catch {
  Stop-Service -Name $serviceName -Force -ErrorAction SilentlyContinue
  Copy-Item $expertBackup $goldenExpert -Force
  Set-ItemProperty $serviceRegistry -Name Environment -Value $oldEnvironment
  if (Test-Path $currentPath) { cmd.exe /c "rmdir $currentPath" | Out-Null }
  if ($oldCurrentTarget) {
    New-Item -ItemType Junction -Path $currentPath -Target $oldCurrentTarget | Out-Null
  }
  Start-Service -Name $serviceName
  throw
}
