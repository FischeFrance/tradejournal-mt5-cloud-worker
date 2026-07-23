#requires -Version 5.1

[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Low')]
param(
    [ValidateSet('PlanOnly', 'ReadOnlyChecks')]
    [string]$Mode = 'PlanOnly',

    [string]$TerminalPath,

    [string]$EvidenceRoot,

    [string]$ProfilePath = (Join-Path (Split-Path $PSScriptRoot -Parent) 'profiles\mt5-network.wprp'),

    [string]$ReportPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function New-CheckResult {
    param(
        [string]$Name,
        [string]$Status,
        [string]$Detail,
        [bool]$Blocking
    )

    return [pscustomobject]@{
        name     = $Name
        status   = $Status
        blocking = $Blocking
        detail   = $Detail
    }
}

function Test-IsAdministrator {
    try {
        $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
        $principal = New-Object Security.Principal.WindowsPrincipal($identity)
        return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    }
    catch {
        return $false
    }
}

$plannedChecks = @(
    'Windows host e privilegi amministrativi',
    'wpr.exe, tracerpt.exe, auditpol.exe e cmdlet NetSecurity',
    'profilo WPR versionato e leggibile',
    'terminale Authenticode valido e SHA-256 (se TerminalPath fornito)',
    'portable root non reparse-point (se TerminalPath fornito)',
    'assenza di terminal64.exe gia avviato',
    'EvidenceRoot locale e non reparse-point (se fornita)',
    'servizi EventLog, BFE e MpsSvc presenti',
    'nessuna scrittura, installazione o modifica di audit/firewall'
)

if ($Mode -eq 'PlanOnly') {
    [pscustomobject]@{
        schema_version = 1
        mode           = 'PlanOnly'
        mutates_host   = $false
        planned_checks = $plannedChecks
        note           = 'Usare -Mode ReadOnlyChecks per eseguire esclusivamente ispezioni locali non mutanti.'
    }
    return
}

$checks = New-Object System.Collections.Generic.List[object]
$isWindows = ([Environment]::OSVersion.Platform -eq [PlatformID]::Win32NT)
$checks.Add((New-CheckResult -Name 'windows_host' -Status $(if ($isWindows) { 'PASS' } else { 'FAIL' }) -Detail ([Environment]::OSVersion.VersionString) -Blocking $true))

if (-not $isWindows) {
    $checks.Add((New-CheckResult -Name 'windows_only_checks' -Status 'SKIPPED' -Detail 'Le verifiche Windows richiedono una VM Windows dedicata.' -Blocking $true))
}
else {
    $isAdmin = Test-IsAdministrator
    $checks.Add((New-CheckResult -Name 'administrator' -Status $(if ($isAdmin) { 'PASS' } else { 'FAIL' }) -Detail 'WPR kernel e la futura policy firewall richiedono elevazione.' -Blocking $true))
    $isNativeBitness = (-not [Environment]::Is64BitOperatingSystem -or [Environment]::Is64BitProcess)
    $checks.Add((New-CheckResult -Name 'native_process_bitness' -Status $(if ($isNativeBitness) { 'PASS' } else { 'FAIL' }) -Detail $(if ([Environment]::Is64BitProcess) { 'PowerShell 64-bit' } else { 'PowerShell 32-bit' }) -Blocking $true))

    foreach ($tool in @('wpr.exe', 'tracerpt.exe', 'auditpol.exe')) {
        $command = Get-Command $tool -ErrorAction SilentlyContinue
        $checks.Add((New-CheckResult -Name ("tool_$($tool.Replace('.exe', ''))") -Status $(if ($null -ne $command) { 'PASS' } else { 'FAIL' }) -Detail $(if ($null -ne $command) { $command.Source } else { 'Non trovato nel PATH.' }) -Blocking $true))
    }

    foreach ($cmdlet in @('Get-NetFirewallProfile', 'Get-NetFirewallRule', 'New-NetFirewallRule')) {
        $command = Get-Command $cmdlet -ErrorAction SilentlyContinue
        $checks.Add((New-CheckResult -Name ("cmdlet_$cmdlet") -Status $(if ($null -ne $command) { 'PASS' } else { 'FAIL' }) -Detail $(if ($null -ne $command) { $command.Source } else { 'Modulo NetSecurity non disponibile.' }) -Blocking $true))
    }

    foreach ($serviceName in @('EventLog', 'BFE', 'MpsSvc')) {
        $service = Get-Service -Name $serviceName -ErrorAction SilentlyContinue
        $status = if ($null -eq $service) { 'FAIL' } elseif ($service.Status -eq 'Running') { 'PASS' } else { 'WARN' }
        $detail = if ($null -eq $service) { 'Servizio non trovato.' } else { [string]$service.Status }
        $checks.Add((New-CheckResult -Name "service_$serviceName" -Status $status -Detail $detail -Blocking $true))
    }

    $runningTerminal = @(Get-Process -Name 'terminal64' -ErrorAction SilentlyContinue)
    $checks.Add((New-CheckResult -Name 'no_running_terminal64' -Status $(if ($runningTerminal.Count -eq 0) { 'PASS' } else { 'FAIL' }) -Detail $(if ($runningTerminal.Count -eq 0) { 'Nessun processo rilevato.' } else { "$($runningTerminal.Count) processo/i rilevato/i." }) -Blocking $true))
}

$profileExists = Test-Path -LiteralPath $ProfilePath -PathType Leaf
$checks.Add((New-CheckResult -Name 'wpr_profile_exists' -Status $(if ($profileExists) { 'PASS' } else { 'FAIL' }) -Detail $ProfilePath -Blocking $true))

if ([string]::IsNullOrWhiteSpace($TerminalPath)) {
    $checks.Add((New-CheckResult -Name 'terminal_binary' -Status 'SKIPPED' -Detail 'TerminalPath non fornito; nessun binario e stato ispezionato.' -Blocking $true))
}
elseif (-not (Test-Path -LiteralPath $TerminalPath -PathType Leaf)) {
    $checks.Add((New-CheckResult -Name 'terminal_binary' -Status 'FAIL' -Detail 'File non trovato.' -Blocking $true))
}
elseif ($isWindows) {
    $terminalItem = Get-Item -LiteralPath $TerminalPath -Force
    $terminalHash = Get-FileHash -LiteralPath $TerminalPath -Algorithm SHA256
    $signature = Get-AuthenticodeSignature -LiteralPath $TerminalPath
    $checks.Add((New-CheckResult -Name 'terminal_sha256' -Status 'PASS' -Detail $terminalHash.Hash -Blocking $true))
    $checks.Add((New-CheckResult -Name 'terminal_authenticode' -Status $(if ($signature.Status -eq 'Valid') { 'PASS' } else { 'FAIL' }) -Detail ([string]$signature.Status) -Blocking $true))
    $signerSubject = if ($null -ne $signature.SignerCertificate) { [string]$signature.SignerCertificate.Subject } else { '' }
    $checks.Add((New-CheckResult -Name 'terminal_signer_metaquotes' -Status $(if ($signature.Status -eq 'Valid' -and $signerSubject -match 'MetaQuotes') { 'PASS' } else { 'FAIL' }) -Detail $signerSubject -Blocking $true))
    $isReparsePoint = ((($terminalItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) -or
        (($terminalItem.Directory.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0))
    $checks.Add((New-CheckResult -Name 'terminal_root_not_reparse_point' -Status $(if ($isReparsePoint) { 'FAIL' } else { 'PASS' }) -Detail $terminalItem.Directory.FullName -Blocking $true))
}

if ([string]::IsNullOrWhiteSpace($EvidenceRoot)) {
    $checks.Add((New-CheckResult -Name 'evidence_root' -Status 'SKIPPED' -Detail 'EvidenceRoot non fornita.' -Blocking $true))
}
elseif (-not (Test-Path -LiteralPath $EvidenceRoot -PathType Container)) {
    $checks.Add((New-CheckResult -Name 'evidence_root' -Status 'FAIL' -Detail 'Directory non esistente; lo script non la crea.' -Blocking $true))
}
else {
    $evidenceItem = Get-Item -LiteralPath $EvidenceRoot -Force
    $isReparsePoint = (($evidenceItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)
    $checks.Add((New-CheckResult -Name 'evidence_root' -Status $(if ($isReparsePoint) { 'FAIL' } else { 'PASS' }) -Detail $evidenceItem.FullName -Blocking $true))
}

$blockingFailures = @($checks | Where-Object { $_.blocking -and $_.status -eq 'FAIL' })
$report = [pscustomobject]@{
    schema_version    = 1
    generated_at_utc = [DateTime]::UtcNow.ToString('o')
    mode              = 'ReadOnlyChecks'
    mutates_host      = $false
    ready             = ($blockingFailures.Count -eq 0)
    checks            = @($checks.ToArray())
}

if (-not [string]::IsNullOrWhiteSpace($ReportPath)) {
    $parent = Split-Path -Path $ReportPath -Parent
    if ([string]::IsNullOrWhiteSpace($parent) -or -not (Test-Path -LiteralPath $parent -PathType Container)) {
        throw 'La directory padre di ReportPath deve esistere; questo script non la crea.'
    }
    if (Test-Path -LiteralPath $ReportPath) {
        throw 'ReportPath esiste gia; sovrascrittura rifiutata.'
    }
    if ($PSCmdlet.ShouldProcess($ReportPath, 'Scrivere il report JSON read-only')) {
        $report | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $ReportPath -Encoding UTF8 -NoNewline
    }
}

$report
