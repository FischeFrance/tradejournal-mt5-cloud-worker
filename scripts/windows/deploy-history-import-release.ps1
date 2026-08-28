#Requires -Version 5.1
#Requires -RunAsAdministrator
param(
  [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')][string]$Revision,
  [Parameter(Mandatory = $true)][string]$SourceRoot,
  [int]$ExpectedTerminalCount = 0,
  [string]$RecoveryConnectionId = '',
  [switch]$PrepareOnly,
  [string]$DeploymentPython = 'C:\TradeJournal\releases\pool71a\.venv\Scripts\python.exe'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$serviceName = 'TradeJournalMT5Agent'
$releaseRoot = 'C:\TradeJournal\releases'
$currentPath = 'C:\TradeJournal\current'
$pythonExe = $DeploymentPython
$serviceRegistry = "HKLM:\SYSTEM\CurrentControlSet\Services\$serviceName"
$goldenExpert = 'C:\TradeJournal\mt5-template\MQL5\Experts\TradeJournal\TradeJournalBridge.ex5'
$releasePath = Join-Path $releaseRoot ("agent-" + $Revision.Substring(0, 12))
$guardStateRoot = 'C:\TradeJournal\state'
$guardRequestPath = Join-Path $guardStateRoot 'deploy-guard-request.json'
$guardResultPath = Join-Path $guardStateRoot 'deploy-guard-result.json'
$readinessPath = Join-Path $guardStateRoot 'agent-readiness.json'
$deploymentId = [Guid]::NewGuid().ToString('D').ToLowerInvariant()
$deploymentMutex = [System.Threading.Mutex]::new($false, 'Global\TradeJournalMT5AgentDeployment')
$deploymentMutexOwned = $false
trap {
  $trappedError = $_
  if ($deploymentMutexOwned) {
    $deploymentMutex.ReleaseMutex()
    $deploymentMutexOwned = $false
  }
  $deploymentMutex.Dispose()
  throw $trappedError
}
try {
  $deploymentMutexOwned = $deploymentMutex.WaitOne(0)
} catch [System.Threading.AbandonedMutexException] {
  $deploymentMutexOwned = $true
}
if (-not $deploymentMutexOwned) {
  throw 'Another guarded Agent deployment is already running.'
}
$releaseSourcePaths = @(
  'windows_agent',
  'worker',
  'contracts/mt5-agent-v1',
  'mt5/experts',
  'scripts/windows',
  'requirements.txt',
  'requirements-ai.txt',
  'requirements-windows.txt'
)

function Assert-CleanSourceCheckout {
  param(
    [Parameter(Mandatory = $true)][string]$GitExe,
    [Parameter(Mandatory = $true)][string[]]$GitArguments,
    [Parameter(Mandatory = $true)][string]$CheckoutRoot,
    [Parameter(Mandatory = $true)][string]$ExpectedRevision,
    [Parameter(Mandatory = $true)][string[]]$PackagePaths
  )

  $gitRootOutput = @(& $GitExe @GitArguments rev-parse --show-toplevel)
  if ($LASTEXITCODE -ne 0 -or $gitRootOutput.Count -ne 1) {
    throw 'Source checkout Git root cannot be verified.'
  }
  $gitRoot = (Get-Item -LiteralPath $gitRootOutput[0].Trim()).FullName
  if (-not [string]::Equals($gitRoot, $CheckoutRoot, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'SourceRoot must be the root of the Git checkout.'
  }
  $headOutput = @(& $GitExe @GitArguments rev-parse --verify 'HEAD^{commit}')
  if (
    $LASTEXITCODE -ne 0 -or
    $headOutput.Count -ne 1 -or
    -not [string]::Equals($headOutput[0].Trim(), $ExpectedRevision, [StringComparison]::Ordinal)
  ) {
    throw 'Source checkout HEAD does not match Revision.'
  }
  $sourceStatus = @(& $GitExe @GitArguments status --porcelain=v1 --untracked-files=all)
  if ($LASTEXITCODE -ne 0 -or $sourceStatus.Count -ne 0) {
    throw 'Source checkout must be clean and contain no untracked files.'
  }
  $ignoredPackageFiles = @(
    & $GitExe @GitArguments ls-files --others --ignored --exclude-standard -- @PackagePaths
  )
  if ($LASTEXITCODE -ne 0) { throw 'Ignored release files cannot be inspected.' }
  $ignoredPackageFiles = @($ignoredPackageFiles | Where-Object {
    $_ -and $_ -notmatch '(^|/)__pycache__(/|$)' -and $_ -notmatch '\.pyc$'
  })
  if ($ignoredPackageFiles.Count -ne 0) {
    throw 'Source checkout contains ignored files that would enter the release.'
  }
}

function Set-DeployGuardFileAcl {
  param([Parameter(Mandatory = $true)][string]$Path)

  $acl = New-Object System.Security.AccessControl.FileSecurity
  $acl.SetAccessRuleProtection($true, $false)
  foreach ($sidValue in @('S-1-5-18', 'S-1-5-32-544')) {
    $sid = [System.Security.Principal.SecurityIdentifier]::new($sidValue)
    $rule = [System.Security.AccessControl.FileSystemAccessRule]::new(
      $sid,
      [System.Security.AccessControl.FileSystemRights]::FullControl,
      [System.Security.AccessControl.AccessControlType]::Allow
    )
    [void]$acl.AddAccessRule($rule)
  }
  Set-Acl -LiteralPath $Path -AclObject $acl
}

function Assert-EffectiveRuntimeConfiguration {
  param([Parameter(Mandatory = $true)][string[]]$ServiceEnvironment)

  # SCM composes the service process from Machine environment variables and
  # the service-specific REG_MULTI_SZ overrides. Reproduce that composition
  # without ever placing an environment value (which may be a secret) in a
  # command line or diagnostic message.
  $effectiveEnvironment = @{}
  foreach ($entry in [Environment]::GetEnvironmentVariables('Machine').GetEnumerator()) {
    $effectiveEnvironment[[string]$entry.Key] = [string]$entry.Value
  }
  foreach ($entry in $ServiceEnvironment) {
    $separator = $entry.IndexOf('=')
    if ($separator -le 0) { throw 'Next service environment contains an invalid entry.' }
    $effectiveEnvironment[$entry.Substring(0, $separator)] = $entry.Substring($separator + 1)
  }

  $processEnvironment = [Environment]::GetEnvironmentVariables('Process')
  $affectedNames = @{}
  foreach ($name in $effectiveEnvironment.Keys) { $affectedNames[$name] = $true }
  foreach ($entry in $processEnvironment.GetEnumerator()) {
    $name = [string]$entry.Key
    if (
      $name.StartsWith('TRADEJOURNAL_', [StringComparison]::OrdinalIgnoreCase) -or
      $name -in @('OPENAI_API_KEY', 'PYTHONPATH', 'PYTHONPYCACHEPREFIX', 'PYTHONDONTWRITEBYTECODE')
    ) {
      $affectedNames[$name] = $true
    }
  }

  $originalValues = @{}
  $originalPresence = @{}
  foreach ($name in $affectedNames.Keys) {
    if ($processEnvironment.Contains($name)) {
      $originalPresence[$name] = $true
      $originalValues[$name] = [string]$processEnvironment[$name]
    } else {
      $originalPresence[$name] = $false
    }
  }

  try {
    foreach ($name in $affectedNames.Keys) {
      $value = if ($effectiveEnvironment.ContainsKey($name)) {
        [string]$effectiveEnvironment[$name]
      } else {
        $null
      }
      [Environment]::SetEnvironmentVariable($name, $value, 'Process')
    }
    $configCheck = 'import sys;sys.path.insert(0,sys.argv[1]);from windows_agent.runtime_config import load_runtime_config;load_runtime_config()'
    & $pythonExe -I -B -c $configCheck $releasePath
    if ($LASTEXITCODE -ne 0) { throw 'Effective next service environment is invalid.' }
  } finally {
    foreach ($name in $affectedNames.Keys) {
      $value = if ($originalPresence[$name]) { [string]$originalValues[$name] } else { $null }
      [Environment]::SetEnvironmentVariable($name, $value, 'Process')
    }
  }
}

function Invoke-DeployGuardAttempt {
  param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('preflight', 'snapshot', 'switch', 'arm', 'barrier', 'barrier_status', 'converge', 'restore', 'verify_active')]
    [string]$Action,
    [Parameter(Mandatory = $true)][hashtable]$Payload,
    [switch]$AllowOperationalFailure,
    [int]$TimeoutSeconds = 180
  )

  $nonce = [Guid]::NewGuid().ToString('D').ToLowerInvariant()
  $taskName = "TradeJournal-DeployGuard-$deploymentId-$nonce"
  $runnerPath = Join-Path $guardStateRoot ("deploy-guard-runner-$nonce.cmd")
  $requestTempPath = $guardRequestPath + ".${nonce}.tmp"
  $taskCreated = $false
  $taskDispatched = $false
  $cleanupFailed = $false
  $resultDocument = $null

  $request = [ordered]@{
    schema_version = 1
    action = $Action
    nonce = $nonce
    deployment_id = $deploymentId
    source_revision = $Revision
    payload = $Payload
  }
  $requestJson = $request | ConvertTo-Json -Depth 8 -Compress
  $utf8WithoutBom = [System.Text.UTF8Encoding]::new($false)

  try {
    Remove-Item -LiteralPath $guardResultPath -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $guardRequestPath -Force -ErrorAction SilentlyContinue
    [IO.File]::WriteAllText($requestTempPath, $requestJson, $utf8WithoutBom)
    Set-DeployGuardFileAcl -Path $requestTempPath
    Move-Item -LiteralPath $requestTempPath -Destination $guardRequestPath -Force

    $bootstrapCode = "import runpy,sys;sys.path.insert(0,sys.argv.pop(1));runpy.run_module('windows_agent.deploy_guard',run_name='__main__')"
    $runner = (
      '@echo off' + "`r`n" +
      '"' + $pythonExe + '" -I -B -c "' + $bootstrapCode + '" "' + $releasePath + '"' + "`r`n" +
      'exit /b %ERRORLEVEL%' + "`r`n"
    )
    [IO.File]::WriteAllText($runnerPath, $runner, $utf8WithoutBom)
    Set-DeployGuardFileAcl -Path $runnerPath

    $scheduleTime = (Get-Date).AddMinutes(2).ToString('HH:mm', [Globalization.CultureInfo]::InvariantCulture)
    $taskCommand = 'cmd.exe /D /S /C ""' + $runnerPath + '""'
    & $schtasksCommand.Source /Create /TN $taskName /TR $taskCommand /SC ONCE /ST $scheduleTime /RU SYSTEM /RL HIGHEST /F 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Deploy guard task creation failed.' }
    $taskCreated = $true
    & $schtasksCommand.Source /Run /TN $taskName 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Deploy guard task start failed.' }
    $taskDispatched = $true

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
      if (Test-Path -LiteralPath $guardResultPath -PathType Leaf) {
        try {
          $candidate = Get-Content -LiteralPath $guardResultPath -Raw -Encoding UTF8 | ConvertFrom-Json
          if (
            $null -ne $candidate -and
            [string]::Equals([string]$candidate.nonce, $nonce, [StringComparison]::Ordinal)
          ) {
            $resultDocument = $candidate
            break
          }
        } catch {
          # The helper publishes with atomic replace. A transient read or a
          # stale nonce is retried until the bounded deadline.
        }
      }
      Start-Sleep -Milliseconds 250
    } while ((Get-Date) -lt $deadline)
    if ($null -eq $resultDocument) { throw 'Deploy guard did not publish a nonce-bound result.' }

    do {
      $scheduledTask = Get-ScheduledTask -TaskName $taskName -ErrorAction Stop
      if ([string]$scheduledTask.State -ne 'Running') { break }
      Start-Sleep -Milliseconds 100
    } while ((Get-Date) -lt $deadline)
    if ([string]$scheduledTask.State -eq 'Running') { throw 'Deploy guard task did not terminate.' }

    $expectedFields = @(
      'schema_version', 'action', 'nonce', 'deployment_id',
      'source_revision', 'success', 'code', 'details'
    )
    $actualFields = @($resultDocument.PSObject.Properties.Name)
    if (
      $actualFields.Count -ne $expectedFields.Count -or
      @(Compare-Object -ReferenceObject $expectedFields -DifferenceObject $actualFields).Count -ne 0
    ) {
      throw 'Deploy guard result schema is invalid.'
    }
    if (
      [int]$resultDocument.schema_version -ne 1 -or
      -not [string]::Equals([string]$resultDocument.action, $Action, [StringComparison]::Ordinal) -or
      -not [string]::Equals([string]$resultDocument.deployment_id, $deploymentId, [StringComparison]::Ordinal) -or
      -not [string]::Equals([string]$resultDocument.source_revision, $Revision, [StringComparison]::Ordinal) -or
      $resultDocument.success -isnot [bool] -or
      [string]$resultDocument.code -notmatch '^[a-z0-9_]{1,80}$' -or
      $null -eq $resultDocument.details
    ) {
      throw 'Deploy guard result binding is invalid.'
    }
    $taskResult = [int64](Get-ScheduledTaskInfo -TaskName $taskName -ErrorAction Stop).LastTaskResult
    if (
      ($resultDocument.success -and $taskResult -ne 0) -or
      (-not $resultDocument.success -and $taskResult -ne 1)
    ) {
      throw 'Deploy guard process result does not match its document.'
    }
    if (-not $resultDocument.success -and -not $AllowOperationalFailure) {
      $boundFailure = [InvalidOperationException]::new(
        "Deploy guard $Action failed with code $($resultDocument.code)."
      )
      $boundFailure.Data['DeployGuardBoundFailure'] = $true
      $boundFailure.Data['DeployGuardCode'] = [string]$resultDocument.code
      throw $boundFailure
    }
    return $resultDocument
  } catch {
    $attemptError = $_
    if (
      $taskDispatched -and
      -not $attemptError.Exception.Data.Contains('DeployGuardBoundFailure')
    ) {
      $attemptError.Exception.Data['DeployGuardCommitAmbiguous'] = $true
    }
    throw $attemptError
  } finally {
    if ($taskCreated) {
      & $schtasksCommand.Source /End /TN $taskName 2>&1 | Out-Null
      & $schtasksCommand.Source /Delete /TN $taskName /F 2>&1 | Out-Null
      if ($LASTEXITCODE -ne 0) { $cleanupFailed = $true }
    }
    foreach ($path in @($runnerPath, $requestTempPath, $guardRequestPath, $guardResultPath)) {
      Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
    }
    if ($cleanupFailed) {
      $cleanupError = [InvalidOperationException]::new('Deploy guard task cleanup failed.')
      if ($taskDispatched) { $cleanupError.Data['DeployGuardCommitAmbiguous'] = $true }
      throw $cleanupError
    }
  }
}

function Invoke-DeployGuard {
  param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('preflight', 'snapshot', 'switch', 'arm', 'barrier', 'barrier_status', 'converge', 'restore', 'verify_active')]
    [string]$Action,
    [Parameter(Mandatory = $true)][hashtable]$Payload,
    [switch]$AllowOperationalFailure,
    [int]$TimeoutSeconds = 180
  )

  $ambiguousCommitObserved = $false
  $maximumAttempts = if ($Action -eq 'converge') { 3 } else { 2 }
  for ($attempt = 1; $attempt -le $maximumAttempts; $attempt++) {
    try {
      $attemptResult = Invoke-DeployGuardAttempt `
        -Action $Action `
        -Payload $Payload `
        -AllowOperationalFailure:$AllowOperationalFailure `
        -TimeoutSeconds $TimeoutSeconds
      return $attemptResult
    } catch {
      $transportError = $_
      if ($transportError.Exception.Data.Contains('DeployGuardCommitAmbiguous')) {
        $ambiguousCommitObserved = $true
      }
      if (
        $transportError.Exception.Data.Contains('DeployGuardBoundFailure') -and
        -not $ambiguousCommitObserved -and
        $Action -ne 'converge'
      ) {
        throw $transportError
      }
      if ($attempt -eq $maximumAttempts) {
        if ($ambiguousCommitObserved) {
          $transportError.Exception.Data['DeployGuardCommitAmbiguous'] = $true
        }
        throw $transportError
      }
      Start-Sleep -Milliseconds 250
    }
  }
  throw 'Deploy guard retry state is invalid.'
}

if (-not (Test-Path -LiteralPath $SourceRoot -PathType Container)) { throw 'Source checkout is missing.' }
$sourceItem = Get-Item -LiteralPath $SourceRoot
if (($sourceItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
  throw 'Source checkout cannot be a reparse point.'
}
$SourceRoot = $sourceItem.FullName
if (-not (Test-Path -LiteralPath $pythonExe -PathType Leaf)) { throw 'Pinned deployment Python is missing.' }
$pythonExe = (Get-Item -LiteralPath $pythonExe -ErrorAction Stop).FullName
if ($pythonExe -match '["&|<>^%!]') { throw 'Pinned deployment Python path is unsafe.' }
if (-not (Test-Path -LiteralPath $goldenExpert -PathType Leaf)) { throw 'Golden bridge binary is missing.' }
New-Item -ItemType Directory -Path $guardStateRoot -Force | Out-Null

$gitCommand = Get-Command git.exe -CommandType Application -ErrorAction Stop
$gitArguments = @('-c', "safe.directory=$SourceRoot", '-C', $SourceRoot)
$scCommand = Get-Command sc.exe -CommandType Application -ErrorAction Stop
$schtasksCommand = Get-Command schtasks.exe -CommandType Application -ErrorAction Stop
Assert-CleanSourceCheckout `
  -GitExe $gitCommand.Source `
  -GitArguments $gitArguments `
  -CheckoutRoot $SourceRoot `
  -ExpectedRevision $Revision `
  -PackagePaths $releaseSourcePaths

$tzdataCheck = 'import importlib.metadata,sys; from datetime import datetime; from zoneinfo import ZoneInfo; zone=ZoneInfo(sys.argv[1]); valid=sys.version_info[:2]==(3,12) and sys.maxsize>2**32 and importlib.metadata.version(sys.argv[2])==sys.argv[3] and datetime(2026,1,15,tzinfo=zone).utcoffset().total_seconds()==3600 and datetime(2026,7,15,tzinfo=zone).utcoffset().total_seconds()==7200; sys.exit(0 if valid else 1)'
& $pythonExe -I -B -c $tzdataCheck 'Europe/Rome' 'tzdata' '2026.3'
if ($LASTEXITCODE -ne 0) {
  throw 'Python 3.12 x64 and tzdata==2026.3 with valid Europe/Rome rules are required.'
}

$service = Get-Service -Name $serviceName -ErrorAction Stop
$service.Refresh()
if ($service.Status -notin @('Running', 'Stopped')) {
  throw 'The current Agent service must be either running or stopped for preflight.'
}
$serviceWasRunning = $service.Status -eq 'Running'
$serviceConfiguration = Get-CimInstance Win32_Service -Filter "Name='$serviceName'" -ErrorAction Stop
if (-not [string]::Equals(
  [string]$serviceConfiguration.StartName,
  'LocalSystem',
  [StringComparison]::OrdinalIgnoreCase
)) {
  throw 'The Agent service must run as LocalSystem.'
}
$oldEnvironment = @((Get-ItemProperty $serviceRegistry -Name Environment -ErrorAction Stop).Environment)
$environmentNames = @{}
foreach ($entry in $oldEnvironment) {
  if (-not $entry) { throw 'Service environment contains an empty entry.' }
  $separator = $entry.IndexOf('=')
  if ($separator -le 0) { throw 'Service environment contains an invalid entry.' }
  $name = $entry.Substring(0, $separator)
  if ($environmentNames.ContainsKey($name)) {
    throw 'Service environment contains duplicate variable names.'
  }
  $environmentNames[$name] = $true
}
$oldCurrentTarget = $null
if (Test-Path -LiteralPath $currentPath) {
  $currentItem = Get-Item -LiteralPath $currentPath -ErrorAction Stop
  $oldCurrentTarget = [string]($currentItem.Target | Select-Object -First 1)
  if (
    ($currentItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -eq 0 -or
    -not $oldCurrentTarget -or
    -not (Test-Path -LiteralPath $oldCurrentTarget -PathType Container)
  ) {
    throw 'Current Agent release junction is invalid.'
  }
}
if (
  $RecoveryConnectionId -and
  $RecoveryConnectionId -notmatch '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
) {
  throw 'Recovery connection id is invalid.'
}
if ($RecoveryConnectionId) {
  $RecoveryConnectionId = $RecoveryConnectionId.ToLowerInvariant()
}

$pythonPathFound = $false
$expertPinFound = $false
$requiredSettings = [ordered]@{
  'TRADEJOURNAL_MT5_MAINTENANCE_ENABLED' = '1'
  'TRADEJOURNAL_MT5_MAINTENANCE_LOCAL_TIME' = '23:30'
  'TRADEJOURNAL_MT5_MAINTENANCE_TIMEZONE' = 'Europe/Rome'
  'TRADEJOURNAL_MT5_MAINTENANCE_GRACE_MINUTES' = '120'
  'TRADEJOURNAL_MT5_MAINTENANCE_STATE_PATH' = 'C:\TradeJournal\state\mt5-maintenance.json'
  'TRADEJOURNAL_AGENT_RELEASE_REVISION' = $Revision
  'TRADEJOURNAL_AGENT_DEPLOYMENT_ID' = $deploymentId
  'TRADEJOURNAL_AGENT_READINESS_PATH' = $readinessPath
  'PYTHONDONTWRITEBYTECODE' = '1'
}
$requiredSettingsFound = @{}
$previousExpertSha256 = (Get-FileHash -LiteralPath $goldenExpert -Algorithm SHA256).Hash.ToLowerInvariant()

$previousPythonPath = $env:PYTHONPATH
Push-Location $SourceRoot
try {
  $env:PYTHONPATH = $SourceRoot
  if ($oldCurrentTarget) {
    $verifyRollbackCode = 'import sys; from windows_agent.release_manifest import verify_release; verify_release(sys.argv[1])'
    & $pythonExe -B -c $verifyRollbackCode $oldCurrentTarget
    if ($LASTEXITCODE -ne 0) { throw 'Current rollback release verification failed.' }
  }

  & $pythonExe -B -m pytest -q `
    (Join-Path $SourceRoot 'tests\windows\test_contract.py') `
    (Join-Path $SourceRoot 'tests\windows\test_atomic_file_retry.py') `
    (Join-Path $SourceRoot 'tests\windows\test_historical_trade_import.py') `
    (Join-Path $SourceRoot 'tests\windows\test_agent_daemon.py') `
    (Join-Path $SourceRoot 'tests\windows\test_mt5_live_update.py') `
    (Join-Path $SourceRoot 'tests\windows\test_native_mt5_runtime.py') `
    (Join-Path $SourceRoot 'tests\windows\test_mt5_template_rotation.py') `
    (Join-Path $SourceRoot 'tests\windows\test_mt5_instance_rotation.py') `
    (Join-Path $SourceRoot 'tests\windows\test_mt5_instance_pool_rotation.py') `
    (Join-Path $SourceRoot 'tests\windows\test_mt5_maintenance.py') `
    (Join-Path $SourceRoot 'tests\windows\test_mt5_maintenance_scheduler.py') `
    (Join-Path $SourceRoot 'tests\windows\test_mt5_lifecycle_coordinator.py') `
    (Join-Path $SourceRoot 'tests\windows\test_mt5_update_store.py') `
    (Join-Path $SourceRoot 'tests\windows\test_mt5_recovery_window.py') `
    (Join-Path $SourceRoot 'tests\windows\test_mt5_event_replay_identity.py') `
    (Join-Path $SourceRoot 'tests\windows\test_interactive_identity.py') `
    (Join-Path $SourceRoot 'tests\windows\test_deploy_guard.py') `
    (Join-Path $SourceRoot 'tests\windows\test_windows_service.py') `
    (Join-Path $SourceRoot 'tests\windows\test_startup_recovery.py') `
    (Join-Path $SourceRoot 'tests\windows\test_runtime_config.py') `
    (Join-Path $SourceRoot 'tests\windows\test_event_supervisor.py') `
    (Join-Path $SourceRoot 'tests\windows\test_real_handlers.py') `
    (Join-Path $SourceRoot 'tests\windows\test_mt5_discovery_contract.py') `
    (Join-Path $SourceRoot 'tests\windows\test_release_manifest.py') `
    (Join-Path $SourceRoot 'tests\test_event_normalizer.py') `
    (Join-Path $SourceRoot 'tests\test_mql5_ea_no_trading.py') `
    ((Join-Path $SourceRoot 'tests\windows\test_windows_smoke.py') + '::test_powershell_scripts_parse')
  if ($LASTEXITCODE -ne 0) { throw 'Windows release tests failed.' }

  & (Join-Path $SourceRoot 'scripts\windows\compile-readonly-ea.ps1')
  $compiledExpert = 'C:\TradeJournal\artifacts\mql5\TradeJournalBridge.ex5'
  if (-not (Test-Path $compiledExpert -PathType Leaf)) { throw 'Compiled bridge is missing.' }
  $expertSha256 = (Get-FileHash $compiledExpert -Algorithm SHA256).Hash.ToLowerInvariant()

  $nextEnvironment = @(
    foreach ($entry in $oldEnvironment) {
      if ($entry -like 'PYTHONPATH=*') {
        $parts = @($entry.Substring('PYTHONPATH='.Length).Split(';'))
        if ($parts.Count -lt 1 -or -not $parts[0].Trim()) { throw 'Service PYTHONPATH is invalid.' }
        $parts[0] = $releasePath
        $pythonPathFound = $true
        'PYTHONPATH=' + ($parts -join ';')
      } elseif ($entry -like 'TRADEJOURNAL_MT5_EXPERT_SHA256=*') {
        $expertPinFound = $true
        'TRADEJOURNAL_MT5_EXPERT_SHA256=' + $expertSha256.ToUpperInvariant()
      } else {
        $separator = $entry.IndexOf('=')
        $name = if ($separator -gt 0) { $entry.Substring(0, $separator) } else { '' }
        if ($requiredSettings.Contains($name)) {
          $canonicalName = @($requiredSettings.Keys | Where-Object {
            [string]::Equals([string]$_, $name, [StringComparison]::OrdinalIgnoreCase)
          })[0]
          $requiredSettingsFound[$canonicalName] = $true
          $canonicalName + '=' + $requiredSettings[$canonicalName]
        } else {
          $entry
        }
      }
    }
    foreach ($name in $requiredSettings.Keys) {
      if (-not $requiredSettingsFound.ContainsKey($name)) {
        $name + '=' + $requiredSettings[$name]
      }
    }
  )
  if (-not $pythonPathFound -or -not $expertPinFound) {
    throw 'Required service environment pins are missing.'
  }
  # Tests and compilation execute checkout code. Revalidate immediately before
  # packaging so their side effects cannot silently enter a published release.
  Assert-CleanSourceCheckout `
    -GitExe $gitCommand.Source `
    -GitArguments $gitArguments `
    -CheckoutRoot $SourceRoot `
    -ExpectedRevision $Revision `
    -PackagePaths $releaseSourcePaths

  if (-not (Test-Path -LiteralPath $releasePath -PathType Container)) {
    $buildCode = 'import sys; from windows_agent.release_manifest import build_release; print(build_release(sys.argv[1], sys.argv[2], revision=sys.argv[3]))'
    & $pythonExe -B -c $buildCode $SourceRoot $releaseRoot $Revision
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $releasePath -PathType Container)) {
      throw 'Immutable Agent release build failed.'
    }
  }
  $verifyCode = 'import sys; from windows_agent.release_manifest import verify_release_matches_source; verify_release_matches_source(sys.argv[1], sys.argv[2], revision=sys.argv[3])'
  & $pythonExe -B -c $verifyCode $releasePath $SourceRoot $Revision
  if ($LASTEXITCODE -ne 0) { throw 'Immutable Agent release does not match the source checkout.' }
  Assert-EffectiveRuntimeConfiguration -ServiceEnvironment $nextEnvironment
} finally {
  $env:PYTHONPATH = $previousPythonPath
  Pop-Location
}

$preflightResult = Invoke-DeployGuard -Action preflight -Payload @{
  release_path = $releasePath
  new_expert_path = $compiledExpert
  next_environment = @($nextEnvironment)
  fpm_connection_id = $RecoveryConnectionId
}
$terminalCountBefore = [int]$preflightResult.details.live_instance_count
$provisionedInstanceCountBefore = [int]$preflightResult.details.provisioned_instance_count
if (
  $terminalCountBefore -lt 0 -or
  $provisionedInstanceCountBefore -lt 0 -or
  $provisionedInstanceCountBefore -lt $terminalCountBefore
) {
  throw 'Deploy guard returned invalid instance counts.'
}
$isBootstrapActivation = (
  $terminalCountBefore -eq 0 -and
  $provisionedInstanceCountBefore -eq 0
)
if (
  ($isBootstrapActivation -and $RecoveryConnectionId) -or
  (-not $isBootstrapActivation -and -not $RecoveryConnectionId)
) {
  throw 'FPM recovery identity does not match the guarded fleet mode.'
}
if ($ExpectedTerminalCount -le 0) {
  $ExpectedTerminalCount = $provisionedInstanceCountBefore
} elseif ($ExpectedTerminalCount -ne $provisionedInstanceCountBefore) {
  throw 'Expected terminal count does not match the provisioned fleet.'
}

if ($PrepareOnly) {
  [pscustomobject]@{
    mode = 'prepared'
    release = $releasePath
    source_revision = $Revision
    deployment_id = $deploymentId
    expert_sha256 = $expertSha256
    bootstrap_activation = $isBootstrapActivation
    live_instance_count = $terminalCountBefore
    provisioned_instance_count = $provisionedInstanceCountBefore
    recovery_connection_id = $RecoveryConnectionId
  } | ConvertTo-Json
  if ($deploymentMutexOwned) {
    $deploymentMutex.ReleaseMutex()
    $deploymentMutexOwned = $false
  }
  $deploymentMutex.Dispose()
  return
}

& $scCommand.Source failure $serviceName reset= 86400 actions= restart/5000/restart/15000/restart/60000
if ($LASTEXITCODE -ne 0) { throw 'Agent service recovery policy update failed.' }
& $scCommand.Source failureflag $serviceName 1
if ($LASTEXITCODE -ne 0) { throw 'Agent service non-crash recovery policy update failed.' }

$snapshotCaptured = $false
$activationBarrierCrossed = $false
$barrierAttempted = $false
$convergenceCompleted = $false
try {
  if ($serviceWasRunning) {
    Stop-Service -Name $serviceName -Force
    (Get-Service -Name $serviceName).WaitForStatus('Stopped', [TimeSpan]::FromSeconds(30))
  }

  [void](Invoke-DeployGuard -Action snapshot -Payload @{})
  $snapshotCaptured = $true
  [void](Invoke-DeployGuard -Action switch -Payload @{
    release_path = $releasePath
    new_expert_path = $compiledExpert
    next_environment = @($nextEnvironment)
    previous_expert_sha256 = $previousExpertSha256
    new_expert_sha256 = $expertSha256
  })
  [void](Invoke-DeployGuard -Action arm -Payload @{})
  $barrierAttempted = $true
  [void](Invoke-DeployGuard -Action barrier -Payload @{})
  $activationBarrierCrossed = $true

  # This barrier-bound path is independent of the daily scheduler state. It
  # converges every managed live instance and pool slot before the new Agent
  # service is allowed to claim jobs.
  [void](Invoke-DeployGuard -Action converge -Payload @{
    fpm_connection_id = $RecoveryConnectionId
  } -TimeoutSeconds 7200)
  $convergenceCompleted = $true

  # Point of no return: this is deliberately the first start of the new
  # release. Every failure below is roll-forward only.
  Start-Service -Name $serviceName
  (Get-Service -Name $serviceName).WaitForStatus('Running', [TimeSpan]::FromSeconds(30))

  $recoveryDeadline = (Get-Date).AddMinutes(6)
  $activeResult = $null
  $stableIdentity = $null
  $stableSince = $null
  $heartbeatSequenceBaseline = $null
  $lastHeartbeatSequence = $null
  do {
    try {
      $candidate = Invoke-DeployGuard -Action verify_active -Payload @{
        fpm_connection_id = $RecoveryConnectionId
        max_heartbeat_age_seconds = 30
      } -AllowOperationalFailure
      if ($candidate.success) {
        $serviceProcessId = [int64]$candidate.details.service_process_id
        $candidateFleetCount = [int]$candidate.details.fleet_count
        $candidatePoolCount = [int]$candidate.details.pool_ready_count
        if (
          $serviceProcessId -le 0 -or
          $candidateFleetCount -ne $ExpectedTerminalCount -or
          $candidatePoolCount -lt 0
        ) {
          throw 'The guarded process identity is invalid.'
        }
        if ($isBootstrapActivation) {
          $candidateIdentity = "$serviceProcessId`:bootstrap`:$candidateFleetCount`:$candidatePoolCount"
          $candidateHeartbeatSequence = $null
        } else {
          $fpmProcessId = [int64]$candidate.details.fpm_process_id
          $fpmProcessCreatedAt = [int64]$candidate.details.fpm_process_creation_time_unix_ms
          $candidateHeartbeatSequence = [int64]$candidate.details.heartbeat_sequence
          if (
            $fpmProcessId -le 0 -or
            $fpmProcessCreatedAt -le 0 -or
            $candidateHeartbeatSequence -le 0
          ) {
            throw 'The guarded FPM identity is invalid.'
          }
          $candidateIdentity = "$serviceProcessId`:$fpmProcessId`:$fpmProcessCreatedAt"
        }
        if (-not [string]::Equals($candidateIdentity, $stableIdentity, [StringComparison]::Ordinal)) {
          $stableIdentity = $candidateIdentity
          $stableSince = Get-Date
          $heartbeatSequenceBaseline = $candidateHeartbeatSequence
          $lastHeartbeatSequence = $candidateHeartbeatSequence
        } elseif (-not $isBootstrapActivation) {
          if ($candidateHeartbeatSequence -lt $lastHeartbeatSequence) {
            throw 'The guarded FPM heartbeat sequence regressed.'
          }
          $lastHeartbeatSequence = $candidateHeartbeatSequence
        }
        $heartbeatAdvanced = (
          $isBootstrapActivation -or
          $lastHeartbeatSequence -gt $heartbeatSequenceBaseline
        )
        if (
          ((Get-Date) - $stableSince).TotalSeconds -ge 30 -and
          $heartbeatAdvanced
        ) {
          $activeResult = $candidate
        }
      } else {
        $stableIdentity = $null
        $stableSince = $null
        $heartbeatSequenceBaseline = $null
        $lastHeartbeatSequence = $null
      }
    } catch {
      $activeResult = $null
      $stableIdentity = $null
      $stableSince = $null
      $heartbeatSequenceBaseline = $null
      $lastHeartbeatSequence = $null
    }
    if ($null -eq $activeResult) { Start-Sleep -Seconds 2 }
  } while ($null -eq $activeResult -and (Get-Date) -lt $recoveryDeadline)
  if ($null -eq $activeResult) {
    throw 'The new Agent did not publish a deployment-bound healthy state.'
  }
  $fleetCountAfter = [int]$activeResult.details.fleet_count
  if ($fleetCountAfter -ne $ExpectedTerminalCount) {
    throw 'The guarded fleet count changed during deployment.'
  }

  [pscustomobject]@{
    release = $releasePath
    source_revision = $Revision
    deployment_id = $deploymentId
    expert_sha256 = $expertSha256
    service = 'running'
    terminal_count_before = $terminalCountBefore
    provisioned_instance_count_before = $provisionedInstanceCountBefore
    terminal_count_expected = $ExpectedTerminalCount
    fleet_count_after = $fleetCountAfter
    pool_ready_count_after = [int]$activeResult.details.pool_ready_count
    recovery_connection_id = [string]$activeResult.details.fpm_connection_id
  } | ConvertTo-Json
} catch {
  $deploymentFailure = $_
  if (
    -not $activationBarrierCrossed -and
    $barrierAttempted -and
    $deploymentFailure.Exception.Data.Contains('DeployGuardCommitAmbiguous')
  ) {
    # Resolve an ambiguous lost result with a nonce-bound LocalSystem probe of
    # the write-once barrier. Never infer the point of no return from transport
    # failure alone.
    try {
      $barrierStatus = Invoke-DeployGuard -Action barrier_status -Payload @{}
      if ($barrierStatus.details.activation_barrier_crossed -isnot [bool]) {
        throw 'The activation barrier probe returned an invalid state.'
      }
      $activationBarrierCrossed = $barrierStatus.details.activation_barrier_crossed
    } catch {
      throw 'The activation barrier state is ambiguous; the Agent service was left stopped.'
    }
  }
  if ($activationBarrierCrossed) {
    # The guarded barrier is write-once. Never restore an old Bridge, marker,
    # environment or junction after new-code activation may have mutated state.
    if (-not $convergenceCompleted) {
      throw 'Deployment convergence is incomplete; the new Agent service was left stopped.'
    }
    try {
      $currentService = Get-Service -Name $serviceName -ErrorAction Stop
      if ($currentService.Status -eq 'Stopped') { Start-Service -Name $serviceName }
    } catch {
      # SCM recovery remains configured for the new release. Operator action
      # is roll-forward only; suppress this secondary error.
    }
    throw 'Deployment crossed the activation barrier and requires roll-forward recovery.'
  }

  $restoreFailed = $false
  if ($snapshotCaptured) {
    try {
      [void](Invoke-DeployGuard -Action restore -Payload @{})
    } catch {
      $restoreFailed = $true
    }
  }
  if (-not $restoreFailed -and $serviceWasRunning) {
    try {
      $currentService = Get-Service -Name $serviceName -ErrorAction Stop
      if ($currentService.Status -ne 'Running') {
        Start-Service -Name $serviceName
        (Get-Service -Name $serviceName).WaitForStatus('Running', [TimeSpan]::FromSeconds(30))
      }
    } catch {
      $restoreFailed = $true
    }
  }
  if ($restoreFailed) {
    throw 'Pre-activation restoration failed; the Agent service was left stopped.'
  }
  throw $deploymentFailure
}

if ($deploymentMutexOwned) {
  $deploymentMutex.ReleaseMutex()
  $deploymentMutexOwned = $false
}
$deploymentMutex.Dispose()
