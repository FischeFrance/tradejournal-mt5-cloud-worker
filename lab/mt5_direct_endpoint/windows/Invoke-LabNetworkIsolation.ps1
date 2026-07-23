#requires -Version 5.1

[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'High')]
param(
    [ValidateSet('PlanOnly', 'VerifyOnly', 'Apply', 'Rollback')]
    [string]$Mode = 'PlanOnly',

    [Parameter(Mandatory = $true)]
    [string]$PlanPath,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Fa-f0-9]{64}$')]
    [string]$PlanSha256,

    [string]$SentinelPath,

    [ValidatePattern('^$|^[A-Fa-f0-9]{64}$')]
    [string]$SentinelSha256,

    [string]$GuardAttestationPath,

    [ValidatePattern('^$|^[A-Fa-f0-9]{64}$')]
    [string]$GuardAttestationSha256,

    [string]$AppliedStatePath,

    [ValidatePattern('^$|^[A-Fa-f0-9]{64}$')]
    [string]$AppliedStateSha256,

    [switch]$IUnderstandThisChangesFirewall,

    [switch]$OutOfBandConsoleConfirmed,

    [switch]$NetworkDisconnected,

    [string]$AuthorizationToken
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

Import-Module (Join-Path $PSScriptRoot 'MT5DirectEndpoint.Lab.psm1') -Force

$LabRoot = 'C:\TJLab'
$ConnectionAuditGuid = '{0CCE9226-69AE-11D9-BED3-505054503030}'
$PacketDropAuditGuid = '{0CCE9225-69AE-11D9-BED3-505054503030}'

function Get-RequiredPropertyValue {
    param(
        [Parameter(Mandatory = $true)]
        [object]$Object,

        [Parameter(Mandatory = $true)]
        [string]$Name,

        [Parameter(Mandatory = $true)]
        [string]$Context
    )

    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property) {
        throw "Proprieta obbligatoria '$Name' mancante in $Context."
    }
    return $property.Value
}

function Assert-OnlyProperties {
    param(
        [Parameter(Mandatory = $true)][object]$Object,
        [Parameter(Mandatory = $true)][string[]]$Allowed,
        [Parameter(Mandatory = $true)][string]$Context
    )

    $unexpected = @($Object.PSObject.Properties.Name | Where-Object { $Allowed -notcontains $_ })
    if ($unexpected.Count -gt 0) {
        throw "Proprieta inattese in ${Context}: $($unexpected -join ', ')"
    }
}

function Read-ImmutableJsonDocument {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,

        [Parameter(Mandatory = $true)]
        [string]$ExpectedSha256,

        [Parameter(Mandatory = $true)]
        [string]$Context
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$Context non trovato: $Path"
    }
    $item = Get-Item -LiteralPath $Path -Force
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "$Context su reparse point rifiutato."
    }
    if ($item.Length -lt 2 -or $item.Length -gt 1048576) {
        throw "$Context fuori dal limite 2 byte - 1 MiB."
    }

    $stream = New-Object IO.FileStream(
        $item.FullName,
        [IO.FileMode]::Open,
        [IO.FileAccess]::Read,
        [IO.FileShare]::Read
    )
    $memory = New-Object IO.MemoryStream
    try {
        $stream.CopyTo($memory)
        $bytes = $memory.ToArray()
    }
    finally {
        $memory.Dispose()
        $stream.Dispose()
    }

    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $actualSha256 = ([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-', '')
    }
    finally {
        $sha.Dispose()
    }
    if ($actualSha256 -cne $ExpectedSha256.ToUpperInvariant()) {
        throw "Digest SHA-256 non corrispondente per $Context."
    }
    if ($bytes.Length -ge 3 -and
        $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) {
        throw "BOM UTF-8 non ammesso per $Context; rigenerare il JSON canonico senza BOM."
    }

    $utf8 = New-Object Text.UTF8Encoding($false, $true)
    try {
        $text = $utf8.GetString($bytes)
        $document = $text | ConvertFrom-Json
    }
    catch {
        throw "JSON UTF-8 non valido per $Context. $($_.Exception.Message)"
    }

    return [pscustomobject]@{
        document = $document
        sha256   = $actualSha256
        path     = $item.FullName
    }
}

function Test-WindowsLabPathSyntax {
    param([string]$Path)

    if ([string]::IsNullOrWhiteSpace($Path)) {
        return $false
    }
    if ($Path -cnotmatch '^C:\\TJLab(?:\\|$)' -or
        $Path -match '[\r\n"]' -or
        $Path -match '(?:^|\\)\.\.(?:\\|$)') {
        return $false
    }
    return $true
}

function Assert-ActivePathSafety {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,

        [switch]$MayNotExist
    )

    if (-not (Test-WindowsLabPathSyntax -Path $Path)) {
        throw "Percorso fuori da C:\TJLab o sintatticamente non sicuro: $Path"
    }

    $rootFull = [IO.Path]::GetFullPath($LabRoot).TrimEnd('\')
    $full = [IO.Path]::GetFullPath($Path)
    if (-not ($full.Equals($rootFull, [StringComparison]::OrdinalIgnoreCase) -or
        $full.StartsWith($rootFull + '\', [StringComparison]::OrdinalIgnoreCase))) {
        throw "Percorso canonico fuori da C:\TJLab: $Path"
    }

    $probe = $full
    if ($MayNotExist -and -not (Test-Path -LiteralPath $probe)) {
        $probe = Split-Path -Path $probe -Parent
    }
    while (-not [string]::IsNullOrWhiteSpace($probe) -and
        $probe.Length -ge $rootFull.Length -and
        (Test-Path -LiteralPath $probe)) {
        $item = Get-Item -LiteralPath $probe -Force
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Reparse point nella catena del percorso: $probe"
        }
        if ($probe.Equals($rootFull, [StringComparison]::OrdinalIgnoreCase)) {
            break
        }
        $parent = Split-Path -Path $probe -Parent
        if ($parent -eq $probe) {
            break
        }
        $probe = $parent
    }

    if (-not $MayNotExist -and -not (Test-Path -LiteralPath $full)) {
        throw "Percorso obbligatorio non trovato: $full"
    }
    return $full
}

function Get-Sha256Text {
    param([Parameter(Mandatory = $true)][string]$Value)

    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [Text.Encoding]::UTF8.GetBytes($Value)
        return ([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-', '')
    }
    finally {
        $sha.Dispose()
    }
}

function ConvertTo-UtcDate {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Value,
        [Parameter(Mandatory = $true)]
        [string]$Context
    )

    try {
        return [DateTime]::Parse(
            $Value,
            [Globalization.CultureInfo]::InvariantCulture,
            [Globalization.DateTimeStyles]::RoundtripKind
        ).ToUniversalTime()
    }
    catch {
        throw "Timestamp non valido in $Context."
    }
}

function Write-NewJsonDocument {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [object]$Document
    )

    [void](Assert-ActivePathSafety -Path $Path -MayNotExist)
    if (Test-Path -LiteralPath $Path) {
        throw "Output gia presente; sovrascrittura rifiutata: $Path"
    }
    $json = $Document | ConvertTo-Json -Depth 12
    $encoding = New-Object Text.UTF8Encoding($false)
    $stream = New-Object IO.FileStream(
        $Path,
        [IO.FileMode]::CreateNew,
        [IO.FileAccess]::Write,
        [IO.FileShare]::Read
    )
    $writer = New-Object IO.StreamWriter($stream, $encoding)
    try {
        $writer.Write($json)
        $writer.Flush()
        $stream.Flush($true)
    }
    finally {
        $writer.Dispose()
        $stream.Dispose()
    }
}

function Invoke-TrustedNativeTool {
    param(
        [Parameter(Mandatory = $true)]
        [ValidateSet('netsh.exe', 'auditpol.exe')]
        [string]$Name,
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments
    )

    $toolPath = Get-LabTrustedSystemToolPath -Name $Name
    $output = @(& $toolPath @Arguments 2>&1)
    if ($LASTEXITCODE -ne 0) {
        throw "$Name exit code $LASTEXITCODE. Output: $($output -join ' | ')"
    }
    return @($output)
}

function Assert-WindowsAdministrator {
    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
        throw 'La modalita attiva richiede Windows.'
    }
    if ([Environment]::Is64BitOperatingSystem -and -not [Environment]::Is64BitProcess) {
        throw 'La modalita attiva richiede PowerShell a 64 bit.'
    }
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'La modalita attiva richiede una console elevata.'
    }
    if ([string]$env:SESSIONNAME -match '^RDP-') {
        throw 'Sessione RDP rifiutata: e obbligatoria una console out-of-band.'
    }
}

function Assert-TerminalBinary {
    param(
        [Parameter(Mandatory = $true)]
        [object]$Terminal,
        [switch]$RequireNotRunning
    )

    $terminalPath = [string](Get-RequiredPropertyValue -Object $Terminal -Name 'path' -Context 'plan.terminal')
    $terminalSha256 = [string](Get-RequiredPropertyValue -Object $Terminal -Name 'sha256' -Context 'plan.terminal')
    $terminalFull = Assert-ActivePathSafety -Path $terminalPath
    if ($terminalFull -notmatch '\\terminal64\.exe$') {
        throw 'Il piano non punta a terminal64.exe.'
    }
    $actualHash = (Get-FileHash -LiteralPath $terminalFull -Algorithm SHA256).Hash
    if ($actualHash -cne $terminalSha256.ToUpperInvariant()) {
        throw 'SHA-256 del terminale non corrispondente al piano.'
    }
    $signature = Get-AuthenticodeSignature -LiteralPath $terminalFull
    $subject = if ($null -ne $signature.SignerCertificate) { [string]$signature.SignerCertificate.Subject } else { '' }
    if ($signature.Status -ne 'Valid' -or $subject -notmatch 'MetaQuotes') {
        throw 'Firma Authenticode MetaQuotes del terminale non valida.'
    }
    if ($RequireNotRunning -and @(Get-Process -Name 'terminal64' -ErrorAction SilentlyContinue).Count -gt 0) {
        throw 'terminal64.exe e gia in esecuzione; Apply deve precedere il lancio.'
    }
}

function Assert-PlanDocument {
    param([Parameter(Mandatory = $true)][object]$Plan)

    Assert-OnlyProperties -Object $Plan -Allowed @(
        'schema_version', 'document_type', 'generated_at_utc', 'run_id', 'control',
        'apply_capability_in_script', 'current_host_forbidden', 'disposable_vm_only',
        'endpoint', 'terminal', 'artifacts', 'required_security_events',
        'corroborating_events', 'hard_preconditions', 'steps', 'rollback'
    ) -Context 'plan'

    if ([int](Get-RequiredPropertyValue -Object $Plan -Name 'schema_version' -Context 'plan') -ne 1 -or
        [string](Get-RequiredPropertyValue -Object $Plan -Name 'document_type' -Context 'plan') -cne 'windows_firewall_wfp_plan') {
        throw 'Schema o document_type del piano non supportato.'
    }
    [void](ConvertTo-UtcDate -Value ([string](Get-RequiredPropertyValue -Object $Plan -Name 'generated_at_utc' -Context 'plan')) -Context 'plan.generated_at_utc')
    if (@((Get-RequiredPropertyValue -Object $Plan -Name 'hard_preconditions' -Context 'plan')).Count -lt 1) {
        throw 'hard_preconditions vuote nel piano.'
    }
    if ((Get-RequiredPropertyValue -Object $Plan -Name 'apply_capability_in_script' -Context 'plan') -ne $false -or
        (Get-RequiredPropertyValue -Object $Plan -Name 'current_host_forbidden' -Context 'plan') -ne $true -or
        (Get-RequiredPropertyValue -Object $Plan -Name 'disposable_vm_only' -Context 'plan') -ne $true) {
        throw 'Il piano non impone plan-only/current_host_forbidden/disposable_vm_only.'
    }

    $runId = [string](Get-RequiredPropertyValue -Object $Plan -Name 'run_id' -Context 'plan')
    Assert-LabRunId -RunId $runId
    $control = [string](Get-RequiredPropertyValue -Object $Plan -Name 'control' -Context 'plan')
    if ($control -notin @('C3', 'C4', 'C5')) {
        throw 'L executor accetta esclusivamente C3, C4 o C5.'
    }

    $endpoint = Get-RequiredPropertyValue -Object $Plan -Name 'endpoint' -Context 'plan'
    Assert-OnlyProperties -Object $endpoint -Allowed @(
        'SchemaVersion', 'InputEndpoint', 'NormalizedEndpoint', 'HostKind',
        'NormalizedHost', 'Port', 'SyntaxValid', 'PortApproved', 'SafetyStatus',
        'ServingEligible', 'AddressEvidence', 'Reasons'
    ) -Context 'plan.endpoint'
    $normalizedEndpoint = [string](Get-RequiredPropertyValue -Object $endpoint -Name 'NormalizedEndpoint' -Context 'plan.endpoint')
    $port = [int](Get-RequiredPropertyValue -Object $endpoint -Name 'Port' -Context 'plan.endpoint')
    $endpointCheck = Test-LabEndpoint -Endpoint $normalizedEndpoint -AllowedPort @($port)
    if (-not $endpointCheck.ServingEligible -or $endpointCheck.HostKind -notin @('IPv4', 'IPv6')) {
        throw "Endpoint del piano non sicuro: $($endpointCheck.Reasons -join ', ')"
    }

    $terminal = Get-RequiredPropertyValue -Object $Plan -Name 'terminal' -Context 'plan'
    Assert-OnlyProperties -Object $terminal -Allowed @('path', 'sha256') -Context 'plan.terminal'
    $terminalPath = [string](Get-RequiredPropertyValue -Object $terminal -Name 'path' -Context 'plan.terminal')
    $terminalHash = [string](Get-RequiredPropertyValue -Object $terminal -Name 'sha256' -Context 'plan.terminal')
    if (-not (Test-WindowsLabPathSyntax -Path $terminalPath) -or
        $terminalPath -notmatch '\\terminal64\.exe$' -or
        $terminalHash -cnotmatch '^[A-Fa-f0-9]{64}$') {
        throw 'Binding del terminale nel piano non valido.'
    }

    $artifacts = Get-RequiredPropertyValue -Object $Plan -Name 'artifacts' -Context 'plan'
    Assert-OnlyProperties -Object $artifacts -Allowed @(
        'firewall_backup', 'audit_backup', 'firewall_log', 'policy_inventory',
        'isolation_intent', 'isolation_applied', 'isolation_failed', 'isolation_rollback'
    ) -Context 'plan.artifacts'
    $artifactPaths = New-Object System.Collections.Generic.List[string]
    foreach ($artifactName in @(
        'firewall_backup', 'audit_backup', 'firewall_log', 'policy_inventory',
        'isolation_intent', 'isolation_applied', 'isolation_failed', 'isolation_rollback'
    )) {
        $artifactPath = [string](Get-RequiredPropertyValue -Object $artifacts -Name $artifactName -Context 'plan.artifacts')
        if (-not (Test-WindowsLabPathSyntax -Path $artifactPath)) {
            throw "Artifact path non sicuro: $artifactName"
        }
        $artifactPaths.Add($artifactPath)
    }
    $distinctArtifactPaths = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    foreach ($artifactPath in $artifactPaths) {
        if (-not $distinctArtifactPaths.Add($artifactPath)) {
            throw "Tutti gli artifact path devono essere distinti; duplicato: $artifactPath"
        }
    }

    $requiredEvents = @((Get-RequiredPropertyValue -Object $Plan -Name 'required_security_events' -Context 'plan'))
    $expectedRequiredEvents = if ($control -in @('C3', 'C5')) { @(5156) } else { @(5157) }
    if (($requiredEvents -join ',') -cne ($expectedRequiredEvents -join ',')) {
        throw 'required_security_events non coerenti con il controllo.'
    }
    $corroboratingEvents = @((Get-RequiredPropertyValue -Object $Plan -Name 'corroborating_events' -Context 'plan'))
    $expectedCorroborating = if ($control -in @('C3', 'C5')) { @() } else { @(5152, 5153) }
    if (($corroboratingEvents -join ',') -cne ($expectedCorroborating -join ',')) {
        throw 'corroborating_events non coerenti con il controllo.'
    }

    foreach ($step in @((Get-RequiredPropertyValue -Object $Plan -Name 'steps' -Context 'plan'))) {
        Assert-OnlyProperties -Object $step -Allowed @('order', 'id', 'tool', 'mutates_host', 'arguments', 'rationale', 'expected') -Context 'plan.steps[]'
    }
    foreach ($rollbackStep in @((Get-RequiredPropertyValue -Object $Plan -Name 'rollback' -Context 'plan'))) {
        Assert-OnlyProperties -Object $rollbackStep -Allowed @('order', 'tool', 'arguments', 'rationale') -Context 'plan.rollback[]'
    }

    return [pscustomobject]@{
        run_id              = $runId
        control             = $control
        endpoint            = $endpointCheck
        terminal            = $terminal
        artifacts           = $artifacts
        group_name          = "MT5DirectEndpointLab_$runId"
        candidate_rule_name = "MT5Lab_${runId}_${control}_AllowCandidate"
        allow_candidate     = ($control -in @('C3', 'C5'))
    }
}

function Assert-SentinelDocument {
    param(
        [Parameter(Mandatory = $true)][object]$Sentinel,
        [Parameter(Mandatory = $true)][object]$Context,
        [switch]$RequireFresh
    )

    Assert-OnlyProperties -Object $Sentinel -Allowed @(
        'schema_version', 'document_type', 'disposable_vm', 'firewall_mutation_authorized',
        'out_of_band_console', 'lab_root', 'run_id', 'control', 'computer_name',
        'machine_guid_sha256', 'nonce', 'created_at_utc', 'expires_at_utc'
    ) -Context 'sentinel'

    if ([int](Get-RequiredPropertyValue -Object $Sentinel -Name 'schema_version' -Context 'sentinel') -ne 1 -or
        [string](Get-RequiredPropertyValue -Object $Sentinel -Name 'document_type' -Context 'sentinel') -cne 'mt5_direct_endpoint_disposable_vm_attestation' -or
        (Get-RequiredPropertyValue -Object $Sentinel -Name 'disposable_vm' -Context 'sentinel') -ne $true -or
        (Get-RequiredPropertyValue -Object $Sentinel -Name 'firewall_mutation_authorized' -Context 'sentinel') -ne $true -or
        (Get-RequiredPropertyValue -Object $Sentinel -Name 'out_of_band_console' -Context 'sentinel') -ne $true) {
        throw 'Sentinel non autorizza esplicitamente un clone disposable con console out-of-band.'
    }
    if ([string](Get-RequiredPropertyValue -Object $Sentinel -Name 'lab_root' -Context 'sentinel') -cne $LabRoot -or
        [string](Get-RequiredPropertyValue -Object $Sentinel -Name 'run_id' -Context 'sentinel') -cne $Context.run_id -or
        [string](Get-RequiredPropertyValue -Object $Sentinel -Name 'control' -Context 'sentinel') -cne $Context.control -or
        [string](Get-RequiredPropertyValue -Object $Sentinel -Name 'computer_name' -Context 'sentinel') -cne [Environment]::MachineName) {
        throw 'Sentinel non legato a lab root, run, controllo o computer corrente.'
    }
    $nonce = [string](Get-RequiredPropertyValue -Object $Sentinel -Name 'nonce' -Context 'sentinel')
    if ($nonce -cnotmatch '^[A-Za-z0-9_-]{32,128}$') {
        throw 'Nonce sentinel non valido.'
    }

    $machineGuid = [string](Get-ItemPropertyValue -LiteralPath 'HKLM:\SOFTWARE\Microsoft\Cryptography' -Name 'MachineGuid')
    $machineDigest = Get-Sha256Text -Value $machineGuid.ToLowerInvariant()
    if ([string](Get-RequiredPropertyValue -Object $Sentinel -Name 'machine_guid_sha256' -Context 'sentinel') -cne $machineDigest) {
        throw 'Sentinel non legato al MachineGuid corrente.'
    }

    $created = ConvertTo-UtcDate -Value ([string](Get-RequiredPropertyValue -Object $Sentinel -Name 'created_at_utc' -Context 'sentinel')) -Context 'sentinel.created_at_utc'
    $expires = ConvertTo-UtcDate -Value ([string](Get-RequiredPropertyValue -Object $Sentinel -Name 'expires_at_utc' -Context 'sentinel')) -Context 'sentinel.expires_at_utc'
    if ($expires -le $created -or ($expires - $created).TotalHours -gt 24) {
        throw 'Finestra temporale sentinel non valida o superiore a 24 ore.'
    }
    if ($RequireFresh) {
        $now = [DateTime]::UtcNow
        if ($created -gt $now.AddMinutes(5) -or $created -lt $now.AddHours(-24) -or $expires -le $now) {
            throw 'Sentinel scaduto o non ancora valido.'
        }
    }
}

function Assert-GuardDocument {
    param(
        [Parameter(Mandatory = $true)][object]$Guard,
        [Parameter(Mandatory = $true)][object]$Context,
        [switch]$ForRollback
    )

    Assert-OnlyProperties -Object $Guard -Allowed @(
        'schema_version', 'document_type', 'run_id', 'control', 'state',
        'default_deny', 'allowed_endpoints', 'provider', 'policy_id',
        'attested_at_utc', 'expires_at_utc'
    ) -Context 'guard'

    if ([int](Get-RequiredPropertyValue -Object $Guard -Name 'schema_version' -Context 'guard') -ne 1 -or
        [string](Get-RequiredPropertyValue -Object $Guard -Name 'document_type' -Context 'guard') -cne 'mt5_direct_endpoint_external_guard_attestation' -or
        (Get-RequiredPropertyValue -Object $Guard -Name 'default_deny' -Context 'guard') -ne $true -or
        [string](Get-RequiredPropertyValue -Object $Guard -Name 'run_id' -Context 'guard') -cne $Context.run_id -or
        [string](Get-RequiredPropertyValue -Object $Guard -Name 'control' -Context 'guard') -cne $Context.control) {
        throw 'Attestazione guard esterno non valida o non legata al run.'
    }
    foreach ($requiredText in @('provider', 'policy_id')) {
        if ([string]::IsNullOrWhiteSpace([string](Get-RequiredPropertyValue -Object $Guard -Name $requiredText -Context 'guard'))) {
            throw "Campo guard vuoto: $requiredText"
        }
    }

    $state = [string](Get-RequiredPropertyValue -Object $Guard -Name 'state' -Context 'guard')
    $allowed = @((Get-RequiredPropertyValue -Object $Guard -Name 'allowed_endpoints' -Context 'guard'))
    if ($ForRollback) {
        if ($state -cne 'DENY_ALL_APPLIED_AND_VERIFIED' -or $allowed.Count -ne 0) {
            throw 'Rollback richiede guard esterno attestato in DENY_ALL.'
        }
    }
    else {
        if ($state -cne 'APPLIED_AND_VERIFIED') {
            throw 'Apply/Verify richiede guard esterno APPLIED_AND_VERIFIED.'
        }
        if ($Context.allow_candidate) {
            if ($allowed.Count -ne 1 -or [string]$allowed[0] -cne $Context.endpoint.NormalizedEndpoint) {
                throw 'Guard esterno non consente esclusivamente il candidate previsto.'
            }
        }
        elseif ($allowed.Count -ne 0) {
            throw 'C4 richiede zero endpoint consentiti dal guard esterno.'
        }
    }

    $attested = ConvertTo-UtcDate -Value ([string](Get-RequiredPropertyValue -Object $Guard -Name 'attested_at_utc' -Context 'guard')) -Context 'guard.attested_at_utc'
    $expires = ConvertTo-UtcDate -Value ([string](Get-RequiredPropertyValue -Object $Guard -Name 'expires_at_utc' -Context 'guard')) -Context 'guard.expires_at_utc'
    $now = [DateTime]::UtcNow
    if ($attested -gt $now.AddMinutes(5) -or $attested -lt $now.AddHours(-24) -or $expires -le $now -or ($expires - $attested).TotalHours -gt 24) {
        throw 'Attestazione guard scaduta o temporalmente non valida.'
    }
}

function Get-NetworkIsolationVerification {
    param([Parameter(Mandatory = $true)][object]$Context)

    $profiles = @(Get-NetFirewallProfile -PolicyStore ActiveStore -ErrorAction Stop)
    $profilesBlocked = ($profiles.Count -ge 3 -and @($profiles | Where-Object {
        -not $_.Enabled -or [string]$_.DefaultOutboundAction -ne 'Block'
    }).Count -eq 0)

    $enabledAllows = @(Get-NetFirewallRule -PolicyStore ActiveStore -Direction Outbound -Action Allow -Enabled True -ErrorAction Stop)
    $groupRules = @($enabledAllows | Where-Object { [string]$_.Group -ceq $Context.group_name })
    $outsideAllows = @($enabledAllows | Where-Object { [string]$_.Group -cne $Context.group_name })
    $candidateRuleExact = $false

    if ($Context.allow_candidate -and $groupRules.Count -eq 1) {
        $rule = $groupRules[0]
        $applicationFilters = @($rule | Get-NetFirewallApplicationFilter -ErrorAction Stop)
        $portFilters = @($rule | Get-NetFirewallPortFilter -ErrorAction Stop)
        $addressFilters = @($rule | Get-NetFirewallAddressFilter -ErrorAction Stop)
        $programs = if ($applicationFilters.Count -eq 1) { @([string[]]$applicationFilters[0].Program) } else { @() }
        $protocols = if ($portFilters.Count -eq 1) { @([string[]]$portFilters[0].Protocol) } else { @() }
        $remotePorts = if ($portFilters.Count -eq 1) { @([string[]]$portFilters[0].RemotePort) } else { @() }
        $remoteAddresses = if ($addressFilters.Count -eq 1) { @([string[]]$addressFilters[0].RemoteAddress) } else { @() }
        $normalizedRemoteAddress = $null
        if ($remoteAddresses.Count -eq 1) {
            $parsedRemoteAddress = $null
            if ([Net.IPAddress]::TryParse($remoteAddresses[0], [ref]$parsedRemoteAddress)) {
                $normalizedRemoteAddress = $parsedRemoteAddress.ToString().ToLowerInvariant()
            }
        }
        $candidateRuleExact = (
            [string]$rule.DisplayName -ceq $Context.candidate_rule_name -and
            $applicationFilters.Count -eq 1 -and
            $programs.Count -eq 1 -and
            $programs[0] -ieq [string]$Context.terminal.path -and
            $portFilters.Count -eq 1 -and
            $protocols.Count -eq 1 -and
            $protocols[0] -in @('6', 'TCP') -and
            $remotePorts.Count -eq 1 -and
            $remotePorts[0] -ceq [string]$Context.endpoint.Port -and
            $addressFilters.Count -eq 1 -and
            $remoteAddresses.Count -eq 1 -and
            $normalizedRemoteAddress -ceq $Context.endpoint.NormalizedHost
        )
    }
    elseif (-not $Context.allow_candidate -and $groupRules.Count -eq 0) {
        $candidateRuleExact = $true
    }

    $auditConnection = Invoke-TrustedNativeTool -Name 'auditpol.exe' -Arguments @('/get', "/subcategory:$ConnectionAuditGuid", '/r')
    $auditDrop = Invoke-TrustedNativeTool -Name 'auditpol.exe' -Arguments @('/get', "/subcategory:$PacketDropAuditGuid", '/r')
    $auditConnectionFlags = Get-LabAuditPolicyFlags -SubcategoryGuid ([Guid]$ConnectionAuditGuid)
    $auditDropFlags = Get-LabAuditPolicyFlags -SubcategoryGuid ([Guid]$PacketDropAuditGuid)
    $auditConnectionVerified = (($auditConnectionFlags -band 3) -eq 3)
    $auditDropVerified = (($auditDropFlags -band 3) -eq 3)
    $auditPolicyVerified = ($auditConnectionVerified -and $auditDropVerified)
    $verified = (
        $profilesBlocked -and
        $outsideAllows.Count -eq 0 -and
        $candidateRuleExact -and
        $auditPolicyVerified
    )

    return [pscustomobject]@{
        verified                      = $verified
        profiles_default_deny         = $profilesBlocked
        enabled_outbound_allow_count  = $enabledAllows.Count
        unexpected_allow_count        = $outsideAllows.Count
        candidate_rule_exact          = $candidateRuleExact
        candidate_rule_count          = $groupRules.Count
        audit_policy_verified          = $auditPolicyVerified
        audit_connection_flags         = $auditConnectionFlags
        audit_connection_success       = (($auditConnectionFlags -band 1) -eq 1)
        audit_connection_failure       = (($auditConnectionFlags -band 2) -eq 2)
        audit_drop_flags               = $auditDropFlags
        audit_drop_success             = (($auditDropFlags -band 1) -eq 1)
        audit_drop_failure             = (($auditDropFlags -band 2) -eq 2)
        audit_connection_query_sha256 = Get-Sha256Text -Value ($auditConnection -join "`n")
        audit_drop_query_sha256       = Get-Sha256Text -Value ($auditDrop -join "`n")
    }
}

$planRecord = Read-ImmutableJsonDocument -Path $PlanPath -ExpectedSha256 $PlanSha256 -Context 'piano firewall'
$context = Assert-PlanDocument -Plan $planRecord.document

if ($Mode -eq 'PlanOnly') {
    [pscustomobject]@{
        schema_version        = 1
        mode                  = 'PlanOnly'
        executed              = $false
        mutates_host          = $false
        run_id                = $context.run_id
        control               = $context.control
        plan_sha256           = $planRecord.sha256
        readiness             = 'NO_GO'
        proof_capable         = $false
        available_capabilities = @('PlanOnly', 'VerifyOnly')
        hard_disabled_capabilities = @('Apply', 'Rollback')
        hard_disable_reason   = 'Nessun verifier di autorizzazione esterno e firmato e ancora integrato; digest/token/sentinel self-asserted non autenticavano il clone.'
        no_go_blockers        = @(
            'verifier esterno firmato e allowlist clone assenti',
            'parser JSON duplicate-key/type-strict assente',
            'capability attive non validate su golden image Windows'
        )
        apply_requirements    = @(
            'Windows PowerShell 64-bit elevato',
            'clone disposable non domain-joined e console out-of-band',
            'piano, sentinel e attestazione guard immutabili con digest espliciti',
            'terminal64.exe sotto C:\TJLab con hash e firma MetaQuotes esatti',
            'switch di consenso, token legato a run/control e ShouldProcess',
            'guard esterno default-deny gia applicato e verificato'
        )
        rollback_requirements = @(
            'NIC disconnesse',
            'guard esterno DENY_ALL attestato',
            'applied-state e backup con digest corrispondenti',
            'distruzione clone come rollback primario'
        )
    }
    return
}

if ($Mode -in @('Apply', 'Rollback')) {
    throw 'CAPABILITY_HARD_DISABLED: Apply/Rollback richiedono un verifier esterno di autorizzazione firmata e una allowlist clone non ancora implementati. Nessuna mutazione e consentita.'
}

Assert-WindowsAdministrator
if (-not $OutOfBandConsoleConfirmed) {
    throw 'Conferma console out-of-band mancante.'
}

foreach ($requiredPath in @($PlanPath, $SentinelPath, $GuardAttestationPath)) {
    if ([string]::IsNullOrWhiteSpace($requiredPath)) {
        throw 'PlanPath, SentinelPath e GuardAttestationPath sono obbligatori in modalita attiva.'
    }
    [void](Assert-ActivePathSafety -Path $requiredPath)
}
if ([string]::IsNullOrWhiteSpace($SentinelSha256) -or [string]::IsNullOrWhiteSpace($GuardAttestationSha256)) {
    throw 'Digest sentinel/guard obbligatori in modalita attiva.'
}

$computerSystem = Get-CimInstance -ClassName Win32_ComputerSystem -ErrorAction Stop
if ($computerSystem.PartOfDomain) {
    throw 'Host domain-joined rifiutato: usare esclusivamente clone disposable isolato.'
}

$sentinelRecord = Read-ImmutableJsonDocument -Path $SentinelPath -ExpectedSha256 $SentinelSha256 -Context 'sentinel clone'
$guardRecord = Read-ImmutableJsonDocument -Path $GuardAttestationPath -ExpectedSha256 $GuardAttestationSha256 -Context 'attestazione guard esterno'
Assert-SentinelDocument -Sentinel $sentinelRecord.document -Context $context -RequireFresh:($Mode -ne 'Rollback')
Assert-GuardDocument -Guard $guardRecord.document -Context $context -ForRollback:($Mode -eq 'Rollback')
Assert-TerminalBinary -Terminal $context.terminal -RequireNotRunning:($Mode -eq 'Apply')

foreach ($artifactName in @(
    'firewall_backup', 'audit_backup', 'firewall_log', 'policy_inventory',
    'isolation_intent', 'isolation_applied', 'isolation_failed', 'isolation_rollback'
)) {
    $artifactPath = [string](Get-RequiredPropertyValue -Object $context.artifacts -Name $artifactName -Context 'plan.artifacts')
    [void](Assert-ActivePathSafety -Path $artifactPath -MayNotExist)
}

$expectedToken = switch ($Mode) {
    'VerifyOnly' { "VERIFY_MT5LAB_$($context.run_id)_$($context.control)" }
    'Apply' { "APPLY_MT5LAB_$($context.run_id)_$($context.control)" }
    'Rollback' { "ROLLBACK_MT5LAB_$($context.run_id)_$($context.control)" }
}
if ($AuthorizationToken -cne $expectedToken) {
    throw 'AuthorizationToken mancante o non legato a Mode/RunId/Control.'
}

if ($Mode -eq 'VerifyOnly') {
    $verification = Get-NetworkIsolationVerification -Context $context
    [pscustomobject]@{
        schema_version                 = 1
        mode                           = 'VerifyOnly'
        executed                       = $true
        mutates_host                   = $false
        readiness                      = 'NO_GO'
        proof_capable                  = $false
        external_authorization_verified = $false
        evidence_eligible              = $false
        firewall_policy_verified       = $false
        diagnostic_policy_matches_plan = [bool]$verification.verified
        run_id                         = $context.run_id
        control                        = $context.control
        plan_sha256                    = $planRecord.sha256
        sentinel_sha256                = $sentinelRecord.sha256
        guard_sha256                   = $guardRecord.sha256
        note                           = 'DIAGNOSTIC_ONLY: sentinel e guard sono self-asserted; verification.verified non e evidenza firewall_policy_verified.'
        verification                   = $verification
    }
    return
}

if (-not $IUnderstandThisChangesFirewall) {
    throw 'Switch -IUnderstandThisChangesFirewall obbligatorio per Apply/Rollback.'
}

if ($Mode -eq 'Apply') {
    $existingArtifacts = @(
        [string]$context.artifacts.firewall_backup,
        [string]$context.artifacts.audit_backup,
        [string]$context.artifacts.firewall_log,
        [string]$context.artifacts.policy_inventory,
        [string]$context.artifacts.isolation_intent,
        [string]$context.artifacts.isolation_applied,
        [string]$context.artifacts.isolation_failed,
        [string]$context.artifacts.isolation_rollback
    ) | Where-Object { Test-Path -LiteralPath $_ }
    if ($existingArtifacts.Count -gt 0) {
        throw "Artifact gia presenti; Apply non idempotente rifiutato: $($existingArtifacts -join ', ')"
    }

    $existingGroupRules = @(Get-NetFirewallRule -Group $context.group_name -ErrorAction SilentlyContinue)
    if ($existingGroupRules.Count -gt 0) {
        throw 'Regole del gruppo lab gia presenti; Apply rifiutato.'
    }
    $existingAllows = @(Get-NetFirewallRule -PolicyStore ActiveStore -Direction Outbound -Action Allow -Enabled True -ErrorAction Stop)
    $nonLocalAllows = @($existingAllows | Where-Object {
        [string]::IsNullOrWhiteSpace([string]$_.PolicyStoreSourceType) -or
        [string]$_.PolicyStoreSourceType -ne 'Local'
    })
    if ($nonLocalAllows.Count -gt 0) {
        throw 'Allow outbound non locali/GPO rilevati; impossibile garantire isolamento.'
    }

    if ($PSCmdlet.ShouldProcess(
        "clone disposable $([Environment]::MachineName), run $($context.run_id)",
        'Applicare audit WFP e isolamento outbound fail-closed'
    )) {
        $mutationStarted = $false
        try {
            $inventory = [pscustomobject]@{
                schema_version = 1
                captured_at_utc = [DateTime]::UtcNow.ToString('o')
                profiles = @(Get-NetFirewallProfile -PolicyStore ActiveStore | Select-Object Name, Enabled, DefaultInboundAction, DefaultOutboundAction, AllowLocalFirewallRules)
                outbound_allows = @($existingAllows | Select-Object Name, DisplayName, Group, Enabled, Direction, Action, Profile, PolicyStoreSourceType, PolicyStoreSource)
            }
            Write-NewJsonDocument -Path ([string]$context.artifacts.policy_inventory) -Document $inventory
            [void](Invoke-TrustedNativeTool -Name 'netsh.exe' -Arguments @('advfirewall', 'export', [string]$context.artifacts.firewall_backup))
            [void](Invoke-TrustedNativeTool -Name 'auditpol.exe' -Arguments @('/backup', "/file:$([string]$context.artifacts.audit_backup)"))

            $intent = [pscustomobject]@{
                schema_version    = 1
                document_type     = 'mt5_direct_endpoint_network_isolation_intent'
                created_at_utc    = [DateTime]::UtcNow.ToString('o')
                machine_name      = [Environment]::MachineName
                run_id            = $context.run_id
                control           = $context.control
                plan_sha256       = $planRecord.sha256
                sentinel_sha256   = $sentinelRecord.sha256
                guard_sha256      = $guardRecord.sha256
                firewall_backup_sha256 = (Get-FileHash -LiteralPath ([string]$context.artifacts.firewall_backup) -Algorithm SHA256).Hash
                audit_backup_sha256 = (Get-FileHash -LiteralPath ([string]$context.artifacts.audit_backup) -Algorithm SHA256).Hash
            }
            Write-NewJsonDocument -Path ([string]$context.artifacts.isolation_intent) -Document $intent

            $mutationStarted = $true
            [void](Invoke-TrustedNativeTool -Name 'auditpol.exe' -Arguments @('/set', "/subcategory:$ConnectionAuditGuid", '/success:enable', '/failure:enable'))
            [void](Invoke-TrustedNativeTool -Name 'auditpol.exe' -Arguments @('/set', "/subcategory:$PacketDropAuditGuid", '/success:enable', '/failure:enable'))

            if ($existingAllows.Count -gt 0) {
                $existingAllows | Disable-NetFirewallRule -ErrorAction Stop | Out-Null
            }
            Set-NetFirewallProfile `
                -Profile Domain, Private, Public `
                -Enabled True `
                -DefaultOutboundAction Block `
                -LogAllowed True `
                -LogBlocked True `
                -LogFileName ([string]$context.artifacts.firewall_log) `
                -LogMaxSizeKilobytes 32767 `
                -ErrorAction Stop

            if ($context.allow_candidate) {
                New-NetFirewallRule `
                    -DisplayName $context.candidate_rule_name `
                    -Group $context.group_name `
                    -Direction Outbound `
                    -Action Allow `
                    -Program ([string]$context.terminal.path) `
                    -Protocol TCP `
                    -RemoteAddress $context.endpoint.NormalizedHost `
                    -RemotePort $context.endpoint.Port `
                    -Profile Any `
                    -InterfaceType Any `
                    -Enabled True `
                    -ErrorAction Stop | Out-Null
            }

            $verification = Get-NetworkIsolationVerification -Context $context
            if (-not $verification.verified) {
                throw 'Verifica post-Apply fallita; il clone deve restare isolato e va distrutto.'
            }

            $appliedState = [pscustomobject]@{
                schema_version    = 1
                document_type     = 'mt5_direct_endpoint_network_isolation_applied'
                applied_at_utc    = [DateTime]::UtcNow.ToString('o')
                machine_name      = [Environment]::MachineName
                run_id            = $context.run_id
                control           = $context.control
                plan_sha256       = $planRecord.sha256
                sentinel_sha256   = $sentinelRecord.sha256
                guard_sha256      = $guardRecord.sha256
                firewall_backup_sha256 = (Get-FileHash -LiteralPath ([string]$context.artifacts.firewall_backup) -Algorithm SHA256).Hash
                audit_backup_sha256 = (Get-FileHash -LiteralPath ([string]$context.artifacts.audit_backup) -Algorithm SHA256).Hash
                verification      = $verification
                fail_closed       = $true
            }
            Write-NewJsonDocument -Path ([string]$context.artifacts.isolation_applied) -Document $appliedState
            [pscustomobject]@{
                mode = 'Apply'; executed = $true; mutates_host = $true
                run_id = $context.run_id; control = $context.control
                state_path = [string]$context.artifacts.isolation_applied
                verification = $verification
            }
        }
        catch {
            $applyError = $_.Exception.Message
            if (-not (Test-Path -LiteralPath ([string]$context.artifacts.isolation_failed))) {
                try {
                    Write-NewJsonDocument -Path ([string]$context.artifacts.isolation_failed) -Document ([pscustomobject]@{
                        schema_version = 1
                        document_type = 'mt5_direct_endpoint_network_isolation_failure'
                        failed_at_utc = [DateTime]::UtcNow.ToString('o')
                        run_id = $context.run_id
                        control = $context.control
                        plan_sha256 = $planRecord.sha256
                        mutation_started = $mutationStarted
                        fail_closed_required = $true
                        error = $applyError
                    })
                }
                catch {
                    # Non mascherare l errore originale se anche l evidence write fallisce.
                }
            }
            throw "Apply non completato. NON riconnettere la VM; mantenerla isolata e distruggere il clone. $applyError"
        }
        return
    }

    [pscustomobject]@{ mode = 'Apply'; executed = $false; mutates_host = $false; reason = 'ShouldProcess/WhatIf' }
    return
}

if ($Mode -eq 'Rollback') {
    if (-not $NetworkDisconnected) {
        throw 'Rollback richiede -NetworkDisconnected.'
    }
    $upAdapters = @(Get-NetAdapter -ErrorAction Stop | Where-Object { [string]$_.Status -eq 'Up' })
    if ($upAdapters.Count -gt 0) {
        throw 'Rollback rifiutato: esistono adapter di rete in stato Up.'
    }
    if ([string]::IsNullOrWhiteSpace($AppliedStatePath) -or [string]::IsNullOrWhiteSpace($AppliedStateSha256)) {
        throw 'AppliedStatePath e AppliedStateSha256 obbligatori per Rollback.'
    }
    [void](Assert-ActivePathSafety -Path $AppliedStatePath)
    $appliedRecord = Read-ImmutableJsonDocument -Path $AppliedStatePath -ExpectedSha256 $AppliedStateSha256 -Context 'applied state'
    $applied = $appliedRecord.document
    Assert-OnlyProperties -Object $applied -Allowed @(
        'schema_version', 'document_type', 'applied_at_utc', 'machine_name',
        'run_id', 'control', 'plan_sha256', 'sentinel_sha256', 'guard_sha256',
        'firewall_backup_sha256', 'audit_backup_sha256', 'verification', 'fail_closed'
    ) -Context 'applied state'
    if ([string](Get-RequiredPropertyValue -Object $applied -Name 'document_type' -Context 'applied state') -cne 'mt5_direct_endpoint_network_isolation_applied' -or
        [string](Get-RequiredPropertyValue -Object $applied -Name 'run_id' -Context 'applied state') -cne $context.run_id -or
        [string](Get-RequiredPropertyValue -Object $applied -Name 'control' -Context 'applied state') -cne $context.control -or
        [string](Get-RequiredPropertyValue -Object $applied -Name 'plan_sha256' -Context 'applied state') -cne $planRecord.sha256) {
        throw 'Applied state non legato al piano/run/control.'
    }

    foreach ($backup in @(
        [pscustomobject]@{ path = [string]$context.artifacts.firewall_backup; expected = [string]$applied.firewall_backup_sha256 },
        [pscustomobject]@{ path = [string]$context.artifacts.audit_backup; expected = [string]$applied.audit_backup_sha256 }
    )) {
        [void](Assert-ActivePathSafety -Path $backup.path)
        if ((Get-FileHash -LiteralPath $backup.path -Algorithm SHA256).Hash -cne $backup.expected) {
            throw "Backup hash mismatch: $($backup.path)"
        }
    }
    if (Test-Path -LiteralPath ([string]$context.artifacts.isolation_rollback)) {
        throw 'Rollback state gia presente; secondo rollback rifiutato.'
    }

    if ($PSCmdlet.ShouldProcess(
        "clone disconnesso $([Environment]::MachineName), run $($context.run_id)",
        'Rimuovere la regola lab e ripristinare firewall/audit dai backup attestati'
    )) {
        try {
            $groupRules = @(Get-NetFirewallRule -Group $context.group_name -ErrorAction SilentlyContinue)
            if ($groupRules.Count -gt 0) {
                $groupRules | Remove-NetFirewallRule -ErrorAction Stop
            }
            [void](Invoke-TrustedNativeTool -Name 'netsh.exe' -Arguments @('advfirewall', 'import', [string]$context.artifacts.firewall_backup))
            [void](Invoke-TrustedNativeTool -Name 'auditpol.exe' -Arguments @('/restore', "/file:$([string]$context.artifacts.audit_backup)"))

            $rollbackState = [pscustomobject]@{
                schema_version = 1
                document_type = 'mt5_direct_endpoint_network_isolation_rollback'
                rolled_back_at_utc = [DateTime]::UtcNow.ToString('o')
                machine_name = [Environment]::MachineName
                run_id = $context.run_id
                control = $context.control
                plan_sha256 = $planRecord.sha256
                applied_state_sha256 = $appliedRecord.sha256
                deny_all_guard_sha256 = $guardRecord.sha256
                network_disconnected = $true
                destroy_clone_required = $true
            }
            Write-NewJsonDocument -Path ([string]$context.artifacts.isolation_rollback) -Document $rollbackState
            [pscustomobject]@{
                mode = 'Rollback'; executed = $true; mutates_host = $true
                run_id = $context.run_id; control = $context.control
                state_path = [string]$context.artifacts.isolation_rollback
                destroy_clone_required = $true
            }
        }
        catch {
            throw "Rollback non completato. Mantenere NIC disconnesse e distruggere il clone. $($_.Exception.Message)"
        }
        return
    }

    [pscustomobject]@{ mode = 'Rollback'; executed = $false; mutates_host = $false; reason = 'ShouldProcess/WhatIf' }
}
