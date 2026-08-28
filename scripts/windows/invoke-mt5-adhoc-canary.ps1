#Requires -Version 5.1
#Requires -RunAsAdministrator
param(
  [Parameter(Mandatory = $true)]
  [ValidatePattern('^[0-9a-f]{40}$')]
  [string]$Revision,

  [Parameter(Mandatory = $true)]
  [string]$ReleasePath,

  [Parameter(Mandatory = $true)]
  [ValidatePattern('^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$')]
  [string]$ConnectionId,

  [Parameter(Mandatory = $true)]
  [ValidatePattern('^[A-Za-z0-9._ -]{1,128}$')]
  [string]$ExpectedServer,

  [Parameter(Mandatory = $true)]
  [ValidateRange(1, 1000)]
  [int]$ExpectedTerminalCount,

  [string]$DeploymentPython = 'C:\TradeJournal\releases\pool71a\.venv\Scripts\python.exe'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$serviceName = 'TradeJournalMT5Agent'
$serviceRegistry = "HKLM:\SYSTEM\CurrentControlSet\Services\$serviceName"
$stateRoot = 'C:\TradeJournal\state'
$logRoot = 'C:\TradeJournal\logs'
$defaultInstancesRoot = 'C:\TradeJournal\instances'
$defaultSecretsRoot = 'C:\TradeJournal\secrets'
$defaultMaintenanceStatePath = 'C:\TradeJournal\state\mt5-maintenance.json'
$defaultMaintenanceLocalTime = '23:30'
$defaultMaintenanceTimezone = 'Europe/Rome'
$defaultMaintenanceGraceMinutes = 120
$readinessPath = 'C:\TradeJournal\state\agent-readiness.json'
$releaseRoot = 'C:\TradeJournal\releases'
$fpmTestConnectionId = '2f1647b4-035e-41be-b634-0cf785a70b07'
$fpmTestServer = 'FPMTrading-Live'
$legacyAdoptionRevision = '4ebb771581c464b0e14656f0532df4f55e830955'
$connectionId = $ConnectionId.ToLowerInvariant()
if (
  $connectionId -ne $fpmTestConnectionId -or
  -not [string]::Equals(
    $ExpectedServer,
    $fpmTestServer,
    [StringComparison]::Ordinal
  )
) {
  throw 'The temporary ad-hoc rollout is restricted to the FPM test account.'
}
$instancesRoot = $null
$secretsRoot = $null
$maintenanceStatePath = $null
$activeReleasePath = $null
$activeSourceRevision = $null
$deploymentMutex = [Threading.Mutex]::new(
  $false,
  'Global\TradeJournalMT5AgentDeployment'
)
$mutexOwned = $false
$serviceWasStopped = $false
$taskCreated = $false
$helperMayBeActive = $false
$restartAgentAllowed = $true
$taskName = $null
$runnerPath = $null
$transcriptStarted = $false
$resultDocument = $null
$operationError = $null
$after = $null
$serviceStateAfter = $null
$probeSucceeded = $false
$legacyServiceThreadFloor = 0
$originalServiceStartMode = $null
$autostartTemporarilyDisabled = $false
$nonce = [Guid]::NewGuid().ToString('D').ToLowerInvariant()
$resultPath = Join-Path $stateRoot "mt5-adhoc-results\$nonce.json"
$logPath = Join-Path $logRoot "mt5-adhoc-$nonce.log"

function Set-SharedOperatorAcl {
  param([Parameter(Mandatory = $true)][string]$Path)

  $acl = New-Object System.Security.AccessControl.FileSecurity
  $acl.SetAccessRuleProtection($true, $false)
  foreach ($sidValue in @('S-1-5-18', 'S-1-5-32-544')) {
    $sid = [Security.Principal.SecurityIdentifier]::new($sidValue)
    $rule = [Security.AccessControl.FileSystemAccessRule]::new(
      $sid,
      [Security.AccessControl.FileSystemRights]::FullControl,
      [Security.AccessControl.AccessControlType]::Allow
    )
    [void]$acl.AddAccessRule($rule)
  }
  Set-Acl -LiteralPath $Path -AclObject $acl
}

function Get-EffectiveServiceSetting {
  param([Parameter(Mandatory = $true)][string]$Name)

  $value = [Environment]::GetEnvironmentVariable($Name, 'Machine')
  $matches = @()
  $serviceEnvironment = @(
    (Get-ItemProperty -LiteralPath $serviceRegistry -Name Environment -ErrorAction Stop).Environment
  )
  foreach ($entry in $serviceEnvironment) {
    $separator = $entry.IndexOf('=')
    if ($separator -le 0) {
      throw 'The Agent service environment is invalid.'
    }
    if ([string]::Equals(
      $entry.Substring(0, $separator),
      $Name,
      [StringComparison]::OrdinalIgnoreCase
    )) {
      $matches += $entry.Substring($separator + 1)
    }
  }
  if ($matches.Count -gt 1) {
    throw 'The Agent service environment contains duplicate settings.'
  }
  if ($matches.Count -eq 1) {
    $value = [string]$matches[0]
  }
  return $value
}

function Resolve-EffectiveInstancesRoot {
  $configured = Get-EffectiveServiceSetting -Name 'TRADEJOURNAL_INSTANCES_ROOT'
  if ([string]::IsNullOrWhiteSpace($configured)) {
    $configured = $defaultInstancesRoot
  }
  $configured = $configured.Trim()
  if (-not [IO.Path]::IsPathRooted($configured)) {
    throw 'The effective MT5 instances root is invalid.'
  }
  $item = Get-Item -LiteralPath ([IO.Path]::GetFullPath($configured)) -ErrorAction Stop
  if (
    -not $item.PSIsContainer -or
    ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0
  ) {
    throw 'The effective MT5 instances root is unsafe.'
  }
  return $item.FullName.TrimEnd('\')
}

function Resolve-EffectiveSecretsRoot {
  $configured = Get-EffectiveServiceSetting -Name 'TRADEJOURNAL_SECRETS_ROOT'
  if ([string]::IsNullOrWhiteSpace($configured)) {
    $configured = $defaultSecretsRoot
  }
  $configured = $configured.Trim()
  if (-not [IO.Path]::IsPathRooted($configured)) {
    throw 'The effective Agent secrets root is invalid.'
  }
  $item = Get-Item -LiteralPath ([IO.Path]::GetFullPath($configured)) -ErrorAction Stop
  if (
    -not $item.PSIsContainer -or
    ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0
  ) {
    throw 'The effective Agent secrets root is unsafe.'
  }
  return $item.FullName.TrimEnd('\')
}

function Resolve-EffectiveMaintenanceStatePath {
  $configured = Get-EffectiveServiceSetting -Name 'TRADEJOURNAL_MT5_MAINTENANCE_STATE_PATH'
  if ([string]::IsNullOrWhiteSpace($configured)) {
    $configured = $defaultMaintenanceStatePath
  }
  $configured = $configured.Trim()
  if (-not [IO.Path]::IsPathRooted($configured)) {
    throw 'The effective MT5 maintenance state path is invalid.'
  }
  $fullPath = [IO.Path]::GetFullPath($configured)
  return $fullPath
}

function Assert-OutsideScheduledMaintenanceWindow {
  param([Parameter(Mandatory = $true)][string]$PythonExe)

  $scheduledTime = Get-EffectiveServiceSetting `
    -Name 'TRADEJOURNAL_MT5_MAINTENANCE_LOCAL_TIME'
  if ([string]::IsNullOrWhiteSpace($scheduledTime)) {
    $scheduledTime = $defaultMaintenanceLocalTime
  }
  $scheduledTime = $scheduledTime.Trim()
  if ($scheduledTime -notmatch '^(?:[01][0-9]|2[0-3]):[0-5][0-9]$') {
    throw 'The effective MT5 maintenance time is invalid.'
  }

  $timezoneName = Get-EffectiveServiceSetting `
    -Name 'TRADEJOURNAL_MT5_MAINTENANCE_TIMEZONE'
  if ([string]::IsNullOrWhiteSpace($timezoneName)) {
    $timezoneName = $defaultMaintenanceTimezone
  }
  $timezoneName = $timezoneName.Trim()
  if ($timezoneName -notmatch '^[A-Za-z0-9._+/-]{1,128}$') {
    throw 'The effective MT5 maintenance timezone is invalid.'
  }

  $graceText = Get-EffectiveServiceSetting `
    -Name 'TRADEJOURNAL_MT5_MAINTENANCE_GRACE_MINUTES'
  if ([string]::IsNullOrWhiteSpace($graceText)) {
    $graceText = [string]$defaultMaintenanceGraceMinutes
  }
  $graceText = $graceText.Trim()
  if ($graceText -notmatch '^[0-9]{1,4}$') {
    throw 'The effective MT5 maintenance grace window is invalid.'
  }
  $graceMinutes = [int]$graceText
  if ($graceMinutes -lt 5 -or $graceMinutes -gt (12 * 60)) {
    throw 'The effective MT5 maintenance grace window is invalid.'
  }

  $gateCode = (
    'import sys;from datetime import datetime,time,timedelta,timezone;' +
    'from zoneinfo import ZoneInfo;' +
    'hour,minute=(int(v) for v in sys.argv[1].split('':''));' +
    'zone=ZoneInfo(sys.argv[2]);slot=time(hour,minute);' +
    'now=datetime.now(timezone.utc).astimezone(zone);' +
    'start=datetime.combine(now.date(),slot,tzinfo=zone);' +
    'start=datetime.combine(now.date()-timedelta(days=1),slot,tzinfo=zone)' +
    ' if now<start else start;' +
    'end=start+timedelta(minutes=int(sys.argv[3]));' +
    'sys.exit(23 if start<=now<=end else 0)'
  )
  & $PythonExe -I -B -c $gateCode `
    $scheduledTime $timezoneName ([string]$graceMinutes)
  $gateExitCode = $LASTEXITCODE
  if ($gateExitCode -eq 23) {
    throw 'The ad-hoc MT5 probe is not allowed during scheduled maintenance.'
  }
  if ($gateExitCode -ne 0) {
    throw 'The effective MT5 maintenance window could not be evaluated.'
  }
}

function Get-ActiveReleaseIdentity {
  $pythonPath = Get-EffectiveServiceSetting -Name 'PYTHONPATH'
  if ([string]::IsNullOrWhiteSpace($pythonPath)) {
    throw 'The active Agent PYTHONPATH is unavailable.'
  }
  $firstEntry = @($pythonPath.Split(';'))[0].Trim()
  $item = Get-Item -LiteralPath $firstEntry -ErrorAction Stop
  if (
    -not $item.PSIsContainer -or
    ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0
  ) {
    throw 'The active Agent release path is unsafe.'
  }
  $manifest = Get-Content -Raw -LiteralPath (
    Join-Path $item.FullName 'release-manifest.json'
  ) | ConvertFrom-Json
  $sourceRevision = [string]$manifest.source_revision
  if (
    $sourceRevision -notmatch '^[0-9a-f]{40}$' -or
    -not [string]::Equals(
      $item.FullName,
      (Join-Path $releaseRoot ('agent-' + $sourceRevision.Substring(0, 12))),
      [StringComparison]::OrdinalIgnoreCase
    )
  ) {
    throw 'The active Agent release identity is invalid.'
  }
  return [pscustomobject]@{
    path = $item.FullName
    source_revision = $sourceRevision
  }
}

function Assert-NoActiveAgentWork {
  $jobStatePath = Join-Path $stateRoot 'agent-job.json'
  if (Test-Path -LiteralPath $jobStatePath -PathType Leaf) {
    $jobState = Get-Content -Raw -LiteralPath $jobStatePath | ConvertFrom-Json
    if ([string]$jobState.status -in @('claimed', 'running')) {
      throw 'The Agent is processing a control-plane job.'
    }
  }
  if (Test-Path -LiteralPath $maintenanceStatePath -PathType Leaf) {
    $maintenanceState = Get-Content -Raw -LiteralPath $maintenanceStatePath |
      ConvertFrom-Json
    if ([string]$maintenanceState.status -eq 'running') {
      throw 'The scheduled MT5 maintenance pass is running.'
    }
  }
}

function Get-ProvisionedInstanceCount {
  $count = 0
  foreach ($entry in Get-ChildItem -LiteralPath $instancesRoot -Directory -Force) {
    if ($entry.Name -notmatch '^[0-9a-fA-F-]{36}$') { continue }
    try {
      $entryId = ([Guid]$entry.Name).ToString('D').ToLowerInvariant()
    } catch {
      continue
    }
    if (($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
      throw 'A provisioned MT5 instance root is unsafe.'
    }
    $state = Get-Content -Raw -LiteralPath (
      Join-Path (Join-Path $entry.FullName 'state') 'instance.json'
    ) | ConvertFrom-Json
    $status = [string]$state.status
    if (
      [string]$state.connection_id -ne $entryId -or
      $status -notin @('provisioned', 'deprovisioned')
    ) {
      throw 'An MT5 instance state is invalid.'
    }
    if ($status -eq 'provisioned') {
      $count += 1
    }
  }
  return $count
}

function Remove-AdHocTaskSafely {
  if (-not $taskCreated) { return }
  $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
  if ($null -eq $task) { return }
  if ([string]$task.State -in @('Running', 'Queued')) {
    throw 'The isolated MT5 helper is still active.'
  }
  Disable-ScheduledTask -TaskName $taskName -ErrorAction Stop | Out-Null
  Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction Stop
  if ($null -ne (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue)) {
    throw 'The isolated MT5 helper task cleanup could not be verified.'
  }
}

function Start-AgentAndAssertStable {
  if ((Get-Service -Name $serviceName).Status -ne 'Running') {
    Start-Service -Name $serviceName
  }
  (Get-Service -Name $serviceName).WaitForStatus(
    'Running',
    [TimeSpan]::FromSeconds(60)
  )
  $initial = Get-CimInstance Win32_Service -Filter "Name='$serviceName'"
  if ([string]$initial.State -ne 'Running' -or [int64]$initial.ProcessId -le 0) {
    throw 'The Agent service process did not start.'
  }
  $servicePid = [int64]$initial.ProcessId
  $stableUntil = (Get-Date).AddSeconds(10)
  do {
    Start-Sleep -Milliseconds 500
    $observed = Get-CimInstance Win32_Service -Filter "Name='$serviceName'"
    if (
      [string]$observed.State -ne 'Running' -or
      [int64]$observed.ProcessId -ne $servicePid
    ) {
      throw 'The Agent service process did not remain stable.'
    }
  } while ((Get-Date) -lt $stableUntil)

  if ($activeSourceRevision -eq $Revision) {
    $readyDeadline = (Get-Date).AddSeconds(60)
    $readiness = $null
    do {
      if (Test-Path -LiteralPath $readinessPath -PathType Leaf) {
        try {
          $candidate = Get-Content -Raw -LiteralPath $readinessPath |
            ConvertFrom-Json
          if (
            [int]$candidate.schema_version -eq 1 -and
            [string]$candidate.source_revision -eq $activeSourceRevision -and
            [int64]$candidate.service_process_id -eq $servicePid
          ) {
            $readiness = $candidate
            break
          }
        } catch {
          # Retry until the atomically-published readiness document is complete.
        }
      }
      Start-Sleep -Milliseconds 250
    } while ((Get-Date) -lt $readyDeadline)
    if ($null -eq $readiness) {
      throw 'The Agent did not publish bound readiness.'
    }
  } else {
    if ($activeSourceRevision -ne $legacyAdoptionRevision) {
      throw 'The active Agent release is not approved for canary adoption.'
    }
    # The legacy worker performs its startup reconciliation before opening the
    # control-plane channel.  A fresh MT5 distribution can make that phase
    # exceed one minute even though the service process is stable and healthy.
    $workerDeadline = (Get-Date).AddSeconds(180)
    $legacyWorkerHealthy = $false
    do {
      $process = Get-Process -Id $servicePid -ErrorAction Stop
      $connections = @(
        Get-NetTCPConnection -OwningProcess $servicePid -ErrorAction SilentlyContinue |
          Where-Object {
            [string]$_.State -eq 'Established' -and
            [int]$_.RemotePort -eq 443
          }
      )
      if (
        $process.Threads.Count -ge $legacyServiceThreadFloor -and
        $connections.Count -ge 1
      ) {
        $legacyWorkerHealthy = $true
        break
      }
      Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $workerDeadline)
    if (-not $legacyWorkerHealthy) {
      throw 'The legacy Agent worker did not recover its control-plane channel.'
    }
  }
  return $observed
}

function Get-ManagedTerminalSnapshot {
  $escapedRoot = [Regex]::Escape($instancesRoot.TrimEnd('\'))
  $pattern = (
    '^' + $escapedRoot +
    '\\(?<connection>[0-9a-fA-F-]{36})\\terminal\\terminal64\.exe$'
  )
  return @(
    Get-CimInstance Win32_Process -Filter "Name='terminal64.exe'" |
      ForEach-Object {
        $path = [string]$_.ExecutablePath
        if ($path -match $pattern) {
          $canonicalConnection = ([Guid]$Matches.connection).ToString('D').ToLowerInvariant()
          $created = ([DateTime]$_.CreationDate).ToUniversalTime()
          [pscustomobject]@{
            connection_id = $canonicalConnection
            process_id = [int64]$_.ProcessId
            created_at_unix_ms = [int64]([DateTimeOffset]$created).ToUnixTimeMilliseconds()
            executable = $path
          }
        }
      } |
      Sort-Object connection_id
  )
}

function Assert-OtherTerminalsUnchanged {
  param(
    [Parameter(Mandatory = $true)][object[]]$Before,
    [Parameter(Mandatory = $true)][object[]]$After
  )

  $beforeOther = @($Before | Where-Object { $_.connection_id -ne $connectionId })
  $afterOther = @($After | Where-Object { $_.connection_id -ne $connectionId })
  if ($beforeOther.Count -ne $afterOther.Count) {
    throw 'A non-target MT5 process count changed during the ad-hoc probe.'
  }
  foreach ($expected in $beforeOther) {
    $observed = @(
      $afterOther |
        Where-Object { $_.connection_id -eq $expected.connection_id }
    )
    if (
      $observed.Count -ne 1 -or
      $observed[0].process_id -ne $expected.process_id -or
      $observed[0].created_at_unix_ms -ne $expected.created_at_unix_ms -or
      -not [string]::Equals(
        [string]$observed[0].executable,
        [string]$expected.executable,
        [StringComparison]::OrdinalIgnoreCase
      )
    ) {
      throw 'A non-target MT5 process changed during the ad-hoc probe.'
    }
  }
}

try {
  try {
    $mutexOwned = $deploymentMutex.WaitOne(0)
  } catch [Threading.AbandonedMutexException] {
    $mutexOwned = $true
  }
  if (-not $mutexOwned) {
    throw 'Another Agent deployment or MT5 maintenance operation is running.'
  }

  $releasePathItem = Get-Item -LiteralPath $ReleasePath -ErrorAction Stop
  $expectedReleasePath = Join-Path $releaseRoot (
    'agent-' + $Revision.Substring(0, 12)
  )
  if (
    -not $releasePathItem.PSIsContainer -or
    ($releasePathItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or
    -not [string]::Equals(
      $releasePathItem.FullName,
      $expectedReleasePath,
      [StringComparison]::OrdinalIgnoreCase
    )
  ) {
    throw 'The immutable Agent release path is invalid.'
  }
  $releasePath = $releasePathItem.FullName
  if (-not (Test-Path -LiteralPath $DeploymentPython -PathType Leaf)) {
    throw 'Pinned deployment Python is missing.'
  }
  $pythonExe = (Get-Item -LiteralPath $DeploymentPython).FullName
  if (
    $releasePath -match '["&|<>^%!]' -or
    $pythonExe -match '["&|<>^%!]' -or
    $ExpectedServer -ne $ExpectedServer.Trim()
  ) {
    throw 'The ad-hoc probe arguments are unsafe.'
  }

  $verifyCode = (
    'import sys;from windows_agent.release_manifest import verify_release;' +
    'd=verify_release(sys.argv[1]);' +
    'sys.exit(0 if d.get(''source_revision'')==sys.argv[2] else 1)'
  )
  $previousPythonPath = $env:PYTHONPATH
  try {
    $env:PYTHONPATH = $releasePath
    & $pythonExe -I -B -c (
      'import sys;sys.path.insert(0,sys.argv[1]);' +
      $verifyCode
    ) $releasePath $Revision
  } finally {
    $env:PYTHONPATH = $previousPythonPath
  }
  if ($LASTEXITCODE -ne 0) {
    throw 'The immutable Agent release verification failed.'
  }

  Assert-OutsideScheduledMaintenanceWindow -PythonExe $pythonExe
  $instancesRoot = Resolve-EffectiveInstancesRoot
  $secretsRoot = Resolve-EffectiveSecretsRoot
  $maintenanceStatePath = Resolve-EffectiveMaintenanceStatePath
  $activeIdentity = Get-ActiveReleaseIdentity
  $activeReleasePath = [string]$activeIdentity.path
  $activeSourceRevision = [string]$activeIdentity.source_revision
  if ($activeSourceRevision -ne $legacyAdoptionRevision) {
    throw 'The temporary FPM canary requires the approved legacy Agent release.'
  }
  $verifyActiveCode = (
    'import sys;sys.path.insert(0,sys.argv[1]);' +
    'from windows_agent.release_manifest import verify_release;' +
    'd=verify_release(sys.argv[2]);' +
    'sys.exit(0 if d.get(''source_revision'')==sys.argv[3] else 1)'
  )
  & $pythonExe -I -B -c $verifyActiveCode `
    $releasePath $activeReleasePath $activeSourceRevision
  if ($LASTEXITCODE -ne 0) {
    throw 'The active Agent release verification failed.'
  }

  $scheduledTasks = @(Get-ScheduledTask -ErrorAction Stop)
  $conflictingTasks = @(
    $scheduledTasks | Where-Object {
      $_.TaskName -like 'TradeJournal-Deploy-*' -and
      [string]$_.State -in @('Running', 'Queued')
    }
  )
  if ($conflictingTasks.Count -ne 0) {
    throw 'A running full-fleet deployment conflicts with the canary-only probe.'
  }
  $incompleteDeployGuardTasks = @(
    $scheduledTasks | Where-Object {
      $_.TaskName -like 'TradeJournal-DeployGuard-*'
    }
  )
  if ($incompleteDeployGuardTasks.Count -ne 0) {
    throw 'An earlier guarded deployment helper requires operator review.'
  }
  $incompleteCanaryTasks = @(
    $scheduledTasks | Where-Object {
      $_.TaskName -like 'TradeJournal-MT5-AdHoc-*'
    }
  )
  if ($incompleteCanaryTasks.Count -ne 0) {
    throw 'An earlier ad-hoc canary task requires operator review.'
  }

  Assert-NoActiveAgentWork

  $instanceStatePath = Join-Path (
    Join-Path (Join-Path $instancesRoot $connectionId) 'state'
  ) 'instance.json'
  $instanceState = Get-Content -Raw -LiteralPath $instanceStatePath | ConvertFrom-Json
  if (
    [string]$instanceState.connection_id -ne $connectionId -or
    [string]$instanceState.status -ne 'provisioned'
  ) {
    throw 'The requested MT5 canary is not provisioned.'
  }

  $before = @(Get-ManagedTerminalSnapshot)
  $provisionedCount = Get-ProvisionedInstanceCount
  if (
    $provisionedCount -ne $ExpectedTerminalCount -or
    $before.Count -ne $ExpectedTerminalCount
  ) {
    throw 'The live MT5 fleet count does not match the expected count.'
  }
  if (@($before | Where-Object { $_.connection_id -eq $connectionId }).Count -ne 1) {
    throw 'The requested MT5 canary process is unavailable or ambiguous.'
  }

  $service = Get-Service -Name $serviceName -ErrorAction Stop
  if ($service.Status -ne 'Running') {
    throw 'The Agent service must be running before the ad-hoc probe.'
  }
  $serviceBefore = Get-CimInstance Win32_Service -Filter "Name='$serviceName'"
  if (
    [string]$serviceBefore.State -ne 'Running' -or
    [int64]$serviceBefore.ProcessId -le 0 -or
    [string]$serviceBefore.StartMode -notin @('Auto', 'Manual')
  ) {
    throw 'The Agent service identity is invalid before the ad-hoc probe.'
  }
  $originalServiceStartMode = [string]$serviceBefore.StartMode
  if ($activeSourceRevision -eq $legacyAdoptionRevision) {
    $legacyProcess = Get-Process -Id $serviceBefore.ProcessId -ErrorAction Stop
    $legacyConnections = @(
      Get-NetTCPConnection -OwningProcess $serviceBefore.ProcessId -ErrorAction SilentlyContinue |
        Where-Object {
          [string]$_.State -eq 'Established' -and
          [int]$_.RemotePort -eq 443
        }
    )
    if ($legacyProcess.Threads.Count -lt 2 -or $legacyConnections.Count -lt 1) {
      throw 'The legacy Agent worker is not healthy before the ad-hoc probe.'
    }
    $legacyServiceThreadFloor = $legacyProcess.Threads.Count
  }

  New-Item -ItemType Directory -Path $logRoot -Force | Out-Null
  Start-Transcript -LiteralPath $logPath -NoClobber | Out-Null
  $transcriptStarted = $true

  $runnerPath = Join-Path $stateRoot "mt5-adhoc-runner-$nonce.cmd"
  $taskName = "TradeJournal-MT5-AdHoc-$nonce"
  $bootstrapCode = (
    "import runpy,sys;sys.path.insert(0,sys.argv.pop(1));" +
    "runpy.run_module('windows_agent.mt5_adhoc_probe',run_name='__main__')"
  )
  $runner = (
    '@echo off' + "`r`n" +
    '"' + $pythonExe + '" -I -B -c "' + $bootstrapCode + '" ' +
    '"' + $releasePath + '" --revision ' + $Revision +
    ' --nonce ' + $nonce +
    ' --connection-id ' + $connectionId +
    ' --expected-server "' + $ExpectedServer + '"' + "`r`n" +
    'exit /b %ERRORLEVEL%' + "`r`n"
  )
  [IO.File]::WriteAllText(
    $runnerPath,
    $runner,
    [Text.UTF8Encoding]::new($false)
  )
  Set-SharedOperatorAcl -Path $runnerPath

  $action = New-ScheduledTaskAction `
    -Execute 'C:\Windows\System32\cmd.exe' `
    -Argument ('/D /S /C ""' + $runnerPath + '""')
  # Manual dispatch is used below.  Keeping the inert fallback trigger one day
  # away prevents an imminent replay even if Windows delays task cleanup.
  $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddDays(1)
  $principal = New-ScheduledTaskPrincipal `
    -UserId 'SYSTEM' `
    -LogonType ServiceAccount `
    -RunLevel Highest
  $settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable:$false `
    -DisallowHardTerminate
  Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings | Out-Null
  $taskCreated = $true

  Assert-OutsideScheduledMaintenanceWindow -PythonExe $pythonExe
  Assert-NoActiveAgentWork
  $serviceWasStopped = $true
  if ($originalServiceStartMode -eq 'Auto') {
    Set-Service -Name $serviceName -StartupType Manual
    $guardedService = Get-CimInstance Win32_Service -Filter "Name='$serviceName'"
    if ([string]$guardedService.StartMode -ne 'Manual') {
      throw 'The Agent reboot safety guard could not be verified.'
    }
    $autostartTemporarilyDisabled = $true
  }
  Stop-Service -Name $serviceName
  (Get-Service -Name $serviceName).WaitForStatus(
    'Stopped',
    [TimeSpan]::FromSeconds(60)
  )
  Assert-NoActiveAgentWork

  $startRequestedAt = Get-Date
  Start-ScheduledTask -TaskName $taskName
  # From dispatch until a terminal task state is positively observed, assume
  # the helper may be alive.  Any polling failure therefore leaves Agent down.
  $helperMayBeActive = $true
  $restartAgentAllowed = $false
  $executionObserved = $false
  $startDeadline = $startRequestedAt.AddMinutes(1)
  $deadline = (Get-Date).AddMinutes(30)
  do {
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction Stop
    $taskInfo = Get-ScheduledTaskInfo -TaskName $taskName -ErrorAction Stop
    $taskState = [string]$task.State
    $activeState = $taskState -in @('Running', 'Queued')
    if (
      $activeState -or
      $taskInfo.LastRunTime -ge $startRequestedAt.AddSeconds(-2)
    ) {
      $executionObserved = $true
    }
    if ($activeState) {
      $helperMayBeActive = $true
    }
    if (Test-Path -LiteralPath $resultPath -PathType Leaf) {
      try {
        $candidate = Get-Content -Raw -LiteralPath $resultPath | ConvertFrom-Json
        if ([string]$candidate.nonce -eq $nonce) {
          $resultDocument = $candidate
          $executionObserved = $true
        }
      } catch {
        # The result is published atomically. Retry any transient read.
      }
    }
    if ($executionObserved -and -not $activeState) {
      $helperMayBeActive = $false
      break
    }
    if (-not $executionObserved -and (Get-Date) -ge $startDeadline) { break }
    Start-Sleep -Milliseconds 250
  } while ((Get-Date) -lt $deadline)

  if (-not $executionObserved) {
    throw 'The isolated MT5 helper start is uncertain; the Agent remains stopped.'
  }
  if ($helperMayBeActive) {
    throw 'The isolated MT5 helper did not finish; the Agent remains stopped.'
  }
  if ($null -eq $resultDocument) {
    throw 'The isolated MT5 helper did not publish a bound result.'
  }
  $expectedResultFields = @(
    'schema_version', 'nonce', 'source_revision', 'mode', 'connection_id',
    'expected_server', 'finished_at_unix_ms', 'success', 'code', 'details'
  )
  $actualResultFields = @($resultDocument.PSObject.Properties.Name)
  if (
    $actualResultFields.Count -ne $expectedResultFields.Count -or
    @(Compare-Object $expectedResultFields $actualResultFields).Count -ne 0 -or
    [int]$resultDocument.schema_version -ne 1 -or
    [string]$resultDocument.nonce -ne $nonce -or
    [string]$resultDocument.source_revision -ne $Revision -or
    [string]$resultDocument.mode -ne 'canary_only' -or
    [string]$resultDocument.connection_id -ne $connectionId -or
    [string]$resultDocument.expected_server -ne $ExpectedServer -or
    $resultDocument.success -isnot [bool] -or
    $resultDocument.code -notmatch '^[a-z0-9_]{1,80}$' -or
    $resultDocument.finished_at_unix_ms -isnot [int64] -or
    [int64]$resultDocument.finished_at_unix_ms -le 0
  ) {
    throw 'The isolated MT5 helper result is invalid.'
  }
  $taskResult = [int64](
    Get-ScheduledTaskInfo -TaskName $taskName -ErrorAction Stop
  ).LastTaskResult
  if (
    ($resultDocument.success -and $taskResult -ne 0) -or
    (-not $resultDocument.success -and $taskResult -ne 1)
  ) {
    throw 'The isolated MT5 helper exit code does not match its result.'
  }
  if (-not $resultDocument.success) {
    throw "The isolated MT5 helper failed with code $($resultDocument.code)."
  }
  $expectedDetailFields = @(
    'connection_id', 'server', 'update_captured',
    'pending_update_receipt_ids', 'public_build',
    'observed_build_before', 'observed_build_after',
    'classification_before', 'classification_after', 'updated',
    'inventory_counts'
  )
  $actualDetailFields = @($resultDocument.details.PSObject.Properties.Name)
  $receiptIds = @($resultDocument.details.pending_update_receipt_ids)
  $invalidReceiptIds = @(
    $receiptIds | Where-Object {
      $_ -isnot [string] -or $_ -notmatch '^[0-9a-f]{64}$'
    }
  )
  $classificationNames = @(
    'older', 'current', 'ahead', 'same_build_divergent', 'unverifiable'
  )
  $inventoryFields = @(
    $resultDocument.details.inventory_counts.PSObject.Properties.Name
  )
  $inventoryTotal = 0
  $inventoryInvalid = $false
  foreach ($name in $classificationNames) {
    $value = $resultDocument.details.inventory_counts.$name
    if (
      ($value -isnot [int] -and $value -isnot [int64]) -or
      [int64]$value -lt 0
    ) {
      $inventoryInvalid = $true
    } else {
      $inventoryTotal += [int64]$value
    }
  }
  $publicBuild = $resultDocument.details.public_build
  $observedBuildBefore = $resultDocument.details.observed_build_before
  $observedBuildAfter = $resultDocument.details.observed_build_after
  $classificationBefore = [string]$resultDocument.details.classification_before
  $classificationAfter = [string]$resultDocument.details.classification_after
  $updated = $resultDocument.details.updated
  if (
    [string]$resultDocument.code -ne 'ok' -or
    $actualDetailFields.Count -ne $expectedDetailFields.Count -or
    @(Compare-Object $expectedDetailFields $actualDetailFields).Count -ne 0 -or
    [string]$resultDocument.details.connection_id -ne $connectionId -or
    [string]$resultDocument.details.server -ne $ExpectedServer -or
    $resultDocument.details.update_captured -isnot [bool] -or
    $invalidReceiptIds.Count -ne 0 -or
    @($receiptIds | Select-Object -Unique).Count -ne $receiptIds.Count -or
    [bool]$resultDocument.details.update_captured -ne ($receiptIds.Count -gt 0) -or
    ($publicBuild -isnot [int] -and $publicBuild -isnot [int64]) -or
    [int64]$publicBuild -le 0 -or
    [int64]$publicBuild -gt 65535 -or
    ($observedBuildBefore -isnot [int] -and $observedBuildBefore -isnot [int64]) -or
    [int64]$observedBuildBefore -le 0 -or
    ($observedBuildAfter -isnot [int] -and $observedBuildAfter -isnot [int64]) -or
    [int64]$observedBuildAfter -le 0 -or
    $updated -isnot [bool] -or
    $classificationBefore -notin @('older', 'current') -or
    $classificationAfter -ne 'current' -or
    $inventoryFields.Count -ne $classificationNames.Count -or
    @(Compare-Object $classificationNames $inventoryFields).Count -ne 0 -or
    $inventoryInvalid -or
    $inventoryTotal -ne $ExpectedTerminalCount -or
    (
      [bool]$updated -and (
        $classificationBefore -ne 'older' -or
        [int64]$observedBuildBefore -ge [int64]$publicBuild -or
        [int64]$observedBuildAfter -ne [int64]$publicBuild
      )
    ) -or
    (
      -not [bool]$updated -and (
        $classificationBefore -ne 'current' -or
        [int64]$observedBuildBefore -ne [int64]$publicBuild -or
        [int64]$observedBuildAfter -ne [int64]$publicBuild
      )
    )
  ) {
    throw 'The isolated MT5 helper details are invalid.'
  }
  $finishedAt = [int64]$resultDocument.finished_at_unix_ms
  $startUnixMs = [int64]([DateTimeOffset]$startRequestedAt).ToUnixTimeMilliseconds()
  $nowUnixMs = [int64][DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
  if ($finishedAt -lt ($startUnixMs - 2000) -or $finishedAt -gt ($nowUnixMs + 2000)) {
    throw 'The isolated MT5 helper result timestamp is invalid.'
  }
  # Exercise the exact publication-validation call used by the currently
  # active Agent before allowing that release to adopt the updated process.
  $adoptionCode = (
    'import sys;from pathlib import Path;sys.path.insert(0,sys.argv[1]);' +
    'from windows_agent.provisioning.mt5_instance import InstanceProvisioner;' +
    'p=InstanceProvisioner(Path(sys.argv[2]),Path(sys.argv[3]));' +
    'r=p.validate(sys.argv[4],verify_code=False);' +
    'e=(Path(sys.argv[2])/sys.argv[4]).resolve();' +
    'sys.exit(0 if str(r.resolve()).casefold()==str(e).casefold() else 1)'
  )
  & $pythonExe -I -B -c $adoptionCode `
    $activeReleasePath $instancesRoot $secretsRoot $connectionId
  if ($LASTEXITCODE -ne 0) {
    throw 'The active Agent cannot safely adopt the canary publication.'
  }
  $probeSucceeded = $true
  $restartAgentAllowed = $true
} catch {
  $operationError = $_
} finally {
  $taskCleanupVerified = -not $taskCreated
  if ($taskCreated -and -not $helperMayBeActive) {
    try {
      Remove-AdHocTaskSafely
      $taskCleanupVerified = $true
    } catch {
      $restartAgentAllowed = $false
      if ($null -eq $operationError) {
        $operationError = $_
      }
    }
  }
  if ($runnerPath -and -not $helperMayBeActive -and $taskCleanupVerified) {
    try {
      Remove-Item -LiteralPath $runnerPath -Force -ErrorAction Stop
      if (Test-Path -LiteralPath $runnerPath) {
        throw 'The isolated MT5 helper runner cleanup could not be verified.'
      }
    } catch {
      if ($null -eq $operationError) {
        $operationError = $_
      }
    }
  }
  $serviceRestarted = $false
  if (
    $serviceWasStopped -and
    -not $helperMayBeActive -and
    $restartAgentAllowed -and
    $taskCleanupVerified
  ) {
    try {
      if ($autostartTemporarilyDisabled) {
        Set-Service -Name $serviceName -StartupType Automatic
        $restoredService = Get-CimInstance Win32_Service -Filter "Name='$serviceName'"
        if ([string]$restoredService.StartMode -ne 'Auto') {
          throw 'The Agent automatic startup mode was not restored.'
        }
        $autostartTemporarilyDisabled = $false
      }
      $serviceStateAfter = Start-AgentAndAssertStable
      $serviceRestarted = $true
    } catch {
      # Service availability is more important than an earlier diagnostic.
      $operationError = $_
    }
  }
  if ($probeSucceeded -and $serviceRestarted) {
    try {
      $after = @(Get-ManagedTerminalSnapshot)
      if ($after.Count -ne $ExpectedTerminalCount) {
        throw 'The live MT5 fleet count changed during the ad-hoc probe.'
      }
      Assert-OtherTerminalsUnchanged -Before $before -After $after
      if (@($after | Where-Object { $_.connection_id -eq $connectionId }).Count -ne 1) {
        throw 'The target MT5 canary did not return to one healthy process.'
      }
      $finalService = Get-CimInstance Win32_Service -Filter "Name='$serviceName'"
      if (
        [string]$finalService.State -ne 'Running' -or
        [int64]$finalService.ProcessId -ne [int64]$serviceStateAfter.ProcessId
      ) {
        throw 'The Agent service process changed after the postconditions.'
      }
      $serviceStateAfter = $finalService
    } catch {
      $operationError = $_
    }
  }
  if ($transcriptStarted) {
    try {
      Stop-Transcript | Out-Null
      Set-SharedOperatorAcl -Path $logPath
    } catch {
      if ($null -eq $operationError) {
        $operationError = $_
      }
    }
  }
  if ($mutexOwned) {
    try {
      $deploymentMutex.ReleaseMutex()
      $mutexOwned = $false
    } catch {
      if ($null -eq $operationError) {
        $operationError = $_
      }
    }
  }
  $deploymentMutex.Dispose()
}

if ($null -ne $operationError) { throw $operationError }
if (-not $probeSucceeded -or $null -eq $after -or $null -eq $serviceStateAfter) {
  throw 'The isolated MT5 canary did not produce complete postconditions.'
}

[pscustomobject]@{
  mode = 'canary_only'
  source_revision = $Revision
  connection_id = $connectionId
  expected_server = $ExpectedServer
  update_captured = [bool]$resultDocument.details.update_captured
  updated = [bool]$resultDocument.details.updated
  public_build = [int64]$resultDocument.details.public_build
  observed_build_before = [int64]$resultDocument.details.observed_build_before
  observed_build_after = [int64]$resultDocument.details.observed_build_after
  classification_before = [string]$resultDocument.details.classification_before
  classification_after = [string]$resultDocument.details.classification_after
  inventory_counts = $resultDocument.details.inventory_counts
  pending_update_receipt_ids = @(
    $resultDocument.details.pending_update_receipt_ids
  )
  terminal_count = $after.Count
  non_target_processes_unchanged = $true
  service = ([string]$serviceStateAfter.State).ToLowerInvariant()
  result_path = $resultPath
  log_path = $logPath
} | ConvertTo-Json -Depth 4
