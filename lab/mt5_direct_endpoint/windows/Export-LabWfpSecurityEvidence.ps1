#requires -Version 5.1

[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Medium')]
param(
    [Parameter(Mandatory = $true)]
    [string]$RunId,

    [Parameter(Mandatory = $true)]
    [ValidateSet('C3', 'C4', 'C5')]
    [string]$Control,

    [Parameter(Mandatory = $true)]
    [string]$MarkerLogPath,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Fa-f0-9]{64}$')]
    [string]$MarkerLogSha256,

    [Parameter(Mandatory = $true)]
    [string]$IsolationAppliedStatePath,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Fa-f0-9]{64}$')]
    [string]$IsolationAppliedStateSha256,

    [Parameter(Mandatory = $true)]
    [string]$TerminalPath,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Fa-f0-9]{64}$')]
    [string]$TerminalSha256,

    [Parameter(Mandatory = $true)]
    [int[]]$TargetProcessId,

    [Parameter(Mandatory = $true)]
    [string]$Endpoint,

    [Parameter(Mandatory = $true)]
    [ValidateRange(1, 65535)]
    [int]$ApprovedPort,

    [Parameter(Mandatory = $true)]
    [string]$OutputDirectory,

    [ValidateSet('PlanOnly', 'Execute')]
    [string]$Mode = 'PlanOnly',

    [string]$AuthorizationToken
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

Import-Module (Join-Path $PSScriptRoot 'MT5DirectEndpoint.Lab.psm1') -Force
Assert-LabRunId -RunId $RunId

$ConnectionAuditGuid = [Guid]'{0CCE9226-69AE-11D9-BED3-505054503030}'
$PacketDropAuditGuid = [Guid]'{0CCE9225-69AE-11D9-BED3-505054503030}'
$ExpectedPhases = @(switch ($Control) {
    'C3' { 'C3_DIRECT_LOGIN'; 'C3_CONNECTED_STEADY' }
    'C4' { 'C4_ENDPOINT_BLOCKED' }
    'C5' { 'C5_DIRECT_LOGIN'; 'C5_CONNECTED_STEADY' }
})
$ExpectedMarkerCodes = @(
    foreach ($expectedPhase in $ExpectedPhases) {
        "${expectedPhase}_START"
        "${expectedPhase}_END"
    }
)
$PhaseVocabulary = 'PATCH6_EXACT_CONTROL_TIMELINE_V1'
$ExpectedControlWindow = switch ($Control) {
    'C3' { 'DIRECT_LOGIN_AND_CONNECTED_STEADY' }
    'C4' { 'ENDPOINT_BLOCKED' }
    'C5' { 'DIRECT_LOGIN_AND_CONNECTED_STEADY' }
}
$RequiredEventId = if ($Control -eq 'C4') { 5157 } else { 5156 }
$eventsPath = Join-LabPath $OutputDirectory ("$RunId.wfp-security.sanitized.jsonl")
$manifestPath = Join-LabPath $OutputDirectory ("$RunId.wfp-security.export-manifest.json")

$endpointCheck = Test-LabEndpoint -Endpoint $Endpoint -AllowedPort @($ApprovedPort)
if (-not $endpointCheck.ServingEligible -or
    $endpointCheck.HostKind -notin @('IPv4', 'IPv6') -or
    $endpointCheck.Port -ne $ApprovedPort) {
    throw "L endpoint deve essere un IP letterale pubblico con porta approvata: $($endpointCheck.Reasons -join ', ')"
}
if ($TerminalPath -cnotmatch '^C:\\TJLab(?:\\|$)' -or
    $TerminalPath -notmatch '\\terminal64\.exe$' -or
    $TerminalPath -match '[\r\n"]' -or
    $TerminalPath -match '(?:^|\\)\.\.(?:\\|$)') {
    throw 'TerminalPath deve puntare a C:\TJLab\...\terminal64.exe senza traversal.'
}
foreach ($processId in $TargetProcessId) {
    if ($processId -lt 1) {
        throw 'TargetProcessId deve contenere esclusivamente PID positivi.'
    }
}
$uniqueTargetProcessId = @($TargetProcessId | Sort-Object -Unique)
if ($uniqueTargetProcessId.Count -eq 0 -or $uniqueTargetProcessId.Count -ne $TargetProcessId.Count) {
    throw 'TargetProcessId deve essere non vuoto e senza duplicati.'
}

if ($Mode -eq 'PlanOnly') {
    [pscustomobject]@{
        schema_version                 = 1
        mode                           = 'PlanOnly'
        executed                       = $false
        mutates_host                   = $false
        opens_network                  = $false
        source                         = 'live Security log via read-only EventLogReader in future Execute mode'
        raw_security_log_exported      = $false
        raw_event_xml_persisted        = $false
        run_id                         = $RunId
        control                        = $Control
        phases                         = $ExpectedPhases
        phase_vocabulary                = $PhaseVocabulary
        control_window                  = $ExpectedControlWindow
        exact_window_source            = $MarkerLogPath
        isolation_state_source         = $IsolationAppliedStatePath
        required_event_id              = $RequiredEventId
        corroborating_event_ids        = if ($Control -eq 'C4') { @(5152, 5153) } else { @() }
        target_process_ids             = $uniqueTargetProcessId
        terminal_path_sha256           = $TerminalSha256.ToUpperInvariant()
        endpoint                       = $endpointCheck.NormalizedEndpoint
        output_events                  = $eventsPath
        output_manifest                = $manifestPath
        readiness                      = 'NO_GO'
        proof_capable                  = $false
        login_success_inference        = 'PROHIBITED: 5156 proves only an allowed network connection'
        c4_scope                       = '5157 path+PID+destination is evidence of a blocked attempt, not proof of login failure by itself'
        execute_capability             = 'HARD_DISABLED'
        hard_disable_reasons           = @(
            'manca binding obbligatorio a manifest Job digest + PID + kernel creation time',
            'manca binding chiuso a piano/clone/sentinel/guard e verifica policy post-window',
            'il filtro deve ancora imporre TCP outbound prima di produrre evidenza utilizzabile',
            'mancano boundary Security attestati a START/END; oldest/newest prova solo retention',
            'il parser deve rifiutare chiavi duplicate e tipi JSON non esatti'
        )
        execution_gate                 = 'Nessun token abilita Execute finche i binding mancanti non sono implementati e validati su Windows.'
    }
    return
}

throw 'CAPABILITY_HARD_DISABLED: export Security/WFP Execute non e ancora autorizzato. Mancano binding Job creation-time/piano/clone/guard, TCP outbound, boundary START/END, parser JSON strict e verifica post-window; nessun evento viene letto o scritto.'

if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    throw 'L export Security/WFP richiede Windows.'
}
if ([Environment]::Is64BitOperatingSystem -and -not [Environment]::Is64BitProcess) {
    throw 'L export richiede PowerShell a 64 bit.'
}
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'La lettura del registro Security richiede una console elevata.'
}
$expectedAuthorizationToken = "EXPORT_WFP_SECURITY_${RunId}_${Control}"
if ($AuthorizationToken -cne $expectedAuthorizationToken) {
    throw 'AuthorizationToken mancante o non legato a RunId/Control.'
}

function Assert-LabActivePath {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [ValidateSet('Leaf', 'Container')][string]$PathType = 'Leaf'
    )

    if ($Path -cnotmatch '^C:\\TJLab(?:\\|$)' -or
        $Path -match '[\r\n"]' -or
        $Path -match '(?:^|\\)\.\.(?:\\|$)') {
        throw "Percorso fuori da C:\TJLab o non sicuro: $Path"
    }
    if (-not (Test-Path -LiteralPath $Path -PathType $PathType)) {
        throw "Percorso richiesto non trovato: $Path"
    }

    $rootFull = [IO.Path]::GetFullPath('C:\TJLab').TrimEnd('\')
    $full = [IO.Path]::GetFullPath($Path)
    if (-not ($full.Equals($rootFull, [StringComparison]::OrdinalIgnoreCase) -or
        $full.StartsWith($rootFull + '\', [StringComparison]::OrdinalIgnoreCase))) {
        throw "Percorso canonico fuori da C:\TJLab: $Path"
    }

    $probe = $full
    while ($probe.Length -ge $rootFull.Length -and (Test-Path -LiteralPath $probe)) {
        $item = Get-Item -LiteralPath $probe -Force
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Reparse point rifiutato: $probe"
        }
        if ($probe.Equals($rootFull, [StringComparison]::OrdinalIgnoreCase)) {
            break
        }
        $parent = Split-Path -Path $probe -Parent
        if ($parent -eq $probe) { break }
        $probe = $parent
    }
    return $full
}

function Assert-FileDigest {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$ExpectedSha256,
        [Parameter(Mandatory = $true)][string]$Context
    )

    $actual = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash
    if ($actual -cne $ExpectedSha256.ToUpperInvariant()) {
        throw "Digest SHA-256 non corrispondente per $Context."
    }
    return $actual
}

function Read-ImmutableUtf8Text {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$ExpectedSha256,
        [Parameter(Mandatory = $true)][string]$Context,
        [Parameter(Mandatory = $true)][int64]$MaximumBytes
    )

    $item = Get-Item -LiteralPath $Path -Force
    if ($item.Length -lt 2 -or $item.Length -gt $MaximumBytes) {
        throw "$Context fuori dal limite dimensionale consentito."
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
        $actual = ([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-', '')
    }
    finally { $sha.Dispose() }
    if ($actual -cne $ExpectedSha256.ToUpperInvariant()) {
        throw "Digest SHA-256 non corrispondente per $Context."
    }
    $utf8 = New-Object Text.UTF8Encoding($false, $true)
    try { $text = $utf8.GetString($bytes) }
    catch { throw "UTF-8 non valido per $Context." }
    if ($text.Length -gt 0 -and $text[0] -eq [char]0xFEFF) {
        $text = $text.Substring(1)
    }
    return [pscustomobject]@{ text = $text; sha256 = $actual }
}

function ConvertTo-UtcDate {
    param([Parameter(Mandatory = $true)][string]$Value, [string]$Context)

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

function Get-Sha256Text {
    param([string]$Value)

    if ([string]::IsNullOrWhiteSpace($Value)) { return $null }
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        return ([BitConverter]::ToString(
            $sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($Value.ToLowerInvariant()))
        )).Replace('-', '')
    }
    finally {
        $sha.Dispose()
    }
}

function Get-EventPayload {
    param([Parameter(Mandatory = $true)][string]$EventXml)

    $document = New-Object Xml.XmlDocument
    $document.XmlResolver = $null
    $document.LoadXml($EventXml)
    $payload = @{}
    foreach ($node in @($document.SelectNodes("/*[local-name()='Event']/*[local-name()='EventData']/*[local-name()='Data']"))) {
        if ($null -ne $node.Attributes['Name']) {
            $name = [string]$node.Attributes['Name'].Value
            if (-not $payload.ContainsKey($name)) {
                $payload[$name] = [string]$node.InnerText
            }
        }
    }
    return $payload
}

function Get-PayloadValue {
    param([hashtable]$Payload, [string[]]$Name)

    foreach ($candidate in $Name) {
        if ($Payload.ContainsKey($candidate) -and
            -not [string]::IsNullOrWhiteSpace([string]$Payload[$candidate])) {
            return [string]$Payload[$candidate]
        }
    }
    return $null
}

function ConvertTo-NullableInteger {
    param([string]$Value, [int64]$Maximum = [int64]::MaxValue)

    if ([string]::IsNullOrWhiteSpace($Value)) { return $null }
    $parsed = [int64]0
    $styles = [Globalization.NumberStyles]::Integer
    $text = $Value.Trim()
    if ($text.StartsWith('0x', [StringComparison]::OrdinalIgnoreCase)) {
        $styles = [Globalization.NumberStyles]::HexNumber
        $text = $text.Substring(2)
    }
    if ([int64]::TryParse($text, $styles, [Globalization.CultureInfo]::InvariantCulture, [ref]$parsed) -and
        $parsed -ge 0 -and $parsed -le $Maximum) {
        return $parsed
    }
    return $null
}

function ConvertTo-NormalizedIp {
    param([string]$Value)

    if ([string]::IsNullOrWhiteSpace($Value)) { return $null }
    $parsed = $null
    if ([Net.IPAddress]::TryParse($Value.Trim(' ', '[', ']'), [ref]$parsed)) {
        return $parsed.ToString().ToLowerInvariant()
    }
    return $null
}

function Initialize-NativePathMapper {
    if ($null -eq ('MT5Lab.NativePathMapper' -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;

namespace MT5Lab
{
    public static class NativePathMapper
    {
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern uint QueryDosDevice(
            string lpDeviceName, StringBuilder lpTargetPath, int ucchMax);

        public static string Query(string drive)
        {
            StringBuilder buffer = new StringBuilder(32768);
            uint length = QueryDosDevice(drive, buffer, buffer.Capacity);
            if (length == 0)
            {
                int error = Marshal.GetLastWin32Error();
                if (error == 2 || error == 3) return null;
                throw new Win32Exception(error, "QueryDosDevice failed");
            }
            string value = buffer.ToString();
            int separator = value.IndexOf('\0');
            return separator >= 0 ? value.Substring(0, separator) : value;
        }
    }
}
'@
    }
}

function ConvertTo-NormalizedApplicationPath {
    param([string]$Value)

    if ([string]::IsNullOrWhiteSpace($Value)) { return $null }
    $path = $Value.Trim()
    if ($path.StartsWith('\??\', [StringComparison]::OrdinalIgnoreCase)) {
        $path = $path.Substring(4)
    }
    elseif ($path.StartsWith('\Device\', [StringComparison]::OrdinalIgnoreCase)) {
        Initialize-NativePathMapper
        foreach ($letter in [char[]]'ABCDEFGHIJKLMNOPQRSTUVWXYZ') {
            $drive = "${letter}:"
            $device = [MT5Lab.NativePathMapper]::Query($drive)
            if (-not [string]::IsNullOrWhiteSpace($device) -and
                $path.StartsWith($device + '\', [StringComparison]::OrdinalIgnoreCase)) {
                $path = $drive + $path.Substring($device.Length)
                break
            }
        }
    }
    if ($path -cmatch '^[A-Za-z]:\\') {
        try { $path = [IO.Path]::GetFullPath($path) } catch { return $null }
    }
    return $path.ToLowerInvariant()
}

function Get-LogBoundary {
    param([switch]$Newest)

    $query = New-Object System.Diagnostics.Eventing.Reader.EventLogQuery(
        'Security',
        [System.Diagnostics.Eventing.Reader.PathType]::LogName,
        '*'
    )
    $query.ReverseDirection = [bool]$Newest
    $reader = New-Object System.Diagnostics.Eventing.Reader.EventLogReader($query)
    try {
        $record = $reader.ReadEvent()
        if ($null -eq $record) { return $null }
        try {
            return [pscustomobject]@{
                record_id = [int64]$record.RecordId
                timestamp_utc = $record.TimeCreated.ToUniversalTime()
            }
        }
        finally { $record.Dispose() }
    }
    finally { $reader.Dispose() }
}

$markerFull = Assert-LabActivePath -Path $MarkerLogPath
$isolationFull = Assert-LabActivePath -Path $IsolationAppliedStatePath
$terminalFull = Assert-LabActivePath -Path $TerminalPath
$outputFull = Assert-LabActivePath -Path $OutputDirectory -PathType Container
$markerRecord = Read-ImmutableUtf8Text -Path $markerFull -ExpectedSha256 $MarkerLogSha256 -Context 'marker log' -MaximumBytes 10485760
$isolationRecord = Read-ImmutableUtf8Text -Path $isolationFull -ExpectedSha256 $IsolationAppliedStateSha256 -Context 'isolation applied state' -MaximumBytes 1048576
$markerHash = $markerRecord.sha256
$isolationHash = $isolationRecord.sha256
$terminalHash = Assert-FileDigest -Path $terminalFull -ExpectedSha256 $TerminalSha256 -Context 'terminal64.exe'
$signature = Get-AuthenticodeSignature -LiteralPath $terminalFull
$signerSubject = if ($null -ne $signature.SignerCertificate) { [string]$signature.SignerCertificate.Subject } else { '' }
if ($signature.Status -ne 'Valid' -or $signerSubject -notmatch 'MetaQuotes') {
    throw 'Firma Authenticode MetaQuotes del terminale non valida.'
}
foreach ($outputPath in @($eventsPath, $manifestPath)) {
    if (Test-Path -LiteralPath $outputPath) {
        throw "Output gia presente; sovrascrittura rifiutata: $outputPath"
    }
}

$runMarkers = @($markerRecord.text -split "`r?`n" | Where-Object {
    -not [string]::IsNullOrWhiteSpace($_)
} | ForEach-Object { $_ | ConvertFrom-Json } | Where-Object {
    [string]$_.run_id -ceq $RunId
})
$markers = @($runMarkers | Where-Object {
    $ExpectedPhases -ccontains [string]$_.phase
})
$actualMarkerCodes = @($markers | ForEach-Object { [string]$_.code })
if (($actualMarkerCodes -join ',') -cne ($ExpectedMarkerCodes -join ',')) {
    throw 'I marker Security/WFP non coincidono con la timeline Patch 6 del controllo.'
}
$phaseWindows = New-Object System.Collections.Generic.List[object]
foreach ($expectedPhase in $ExpectedPhases) {
    $startMarkers = @($markers | Where-Object {
        [string]$_.phase -ceq $expectedPhase -and [string]$_.boundary -ceq 'START'
    })
    $endMarkers = @($markers | Where-Object {
        [string]$_.phase -ceq $expectedPhase -and [string]$_.boundary -ceq 'END'
    })
    if ($startMarkers.Count -ne 1 -or $endMarkers.Count -ne 1) {
        throw "La fase $expectedPhase richiede esattamente un marker START e un marker END."
    }
    if ([int64]$startMarkers[0].schema_version -ne 2 -or [int64]$endMarkers[0].schema_version -ne 2) {
        throw "La fase $expectedPhase richiede marker schema v2."
    }
    $phaseStart = [DateTimeOffset]::FromUnixTimeMilliseconds(
        [int64]$startMarkers[0].timestamp_unix_ms
    ).UtcDateTime
    $phaseEnd = [DateTimeOffset]::FromUnixTimeMilliseconds(
        [int64]$endMarkers[0].timestamp_unix_ms
    ).UtcDateTime
    if ($phaseEnd -le $phaseStart) {
        throw "La finestra della fase $expectedPhase e invertita o vuota."
    }
    $phaseWindows.Add([pscustomobject]@{
        phase     = $expectedPhase
        start_utc = $phaseStart
        end_utc   = $phaseEnd
    })
}
$windowStart = $phaseWindows[0].start_utc
$windowEnd = $phaseWindows[$phaseWindows.Count - 1].end_utc
$now = [DateTime]::UtcNow
if ($windowEnd -le $windowStart -or
    $windowEnd -gt $now.AddMinutes(5) -or
    $windowStart -lt $now.AddHours(-24)) {
    throw 'Finestra fase invertita, futura o piu vecchia di 24 ore.'
}

$isolationState = $isolationRecord.text | ConvertFrom-Json
if ([string]$isolationState.document_type -cne 'mt5_direct_endpoint_network_isolation_applied' -or
    [string]$isolationState.run_id -cne $RunId -or
    [string]$isolationState.control -cne $Control -or
    $isolationState.fail_closed -ne $true -or
    $isolationState.verification.verified -ne $true -or
    $isolationState.verification.audit_policy_verified -ne $true -or
    $isolationState.verification.profiles_default_deny -ne $true -or
    [int]$isolationState.verification.unexpected_allow_count -ne 0) {
    throw 'Isolation applied state non valido, non fail-closed o non legato al run.'
}
$isolationAppliedAt = ConvertTo-UtcDate -Value ([string]$isolationState.applied_at_utc) -Context 'isolation applied state'
if ($isolationAppliedAt -gt $windowStart) {
    throw 'La policy di isolamento risulta applicata dopo START della fase.'
}

$terminalNormalized = ([IO.Path]::GetFullPath($terminalFull)).ToLowerInvariant()
$auditConnectionFlags = Get-LabAuditPolicyFlags -SubcategoryGuid $ConnectionAuditGuid
$auditDropFlags = Get-LabAuditPolicyFlags -SubcategoryGuid $PacketDropAuditGuid
$auditPolicyVerifiedAfterWindow = (
    (($auditConnectionFlags -band 3) -eq 3) -and
    (($auditDropFlags -band 3) -eq 3)
)
if (-not $auditPolicyVerifiedAfterWindow) {
    throw 'Audit WFP non risulta Success+Failure al momento dell export; assenza eventi non interpretabile.'
}

$oldestBoundary = Get-LogBoundary
$newestBoundary = Get-LogBoundary -Newest
$logWindowCovered = (
    $null -ne $oldestBoundary -and
    $null -ne $newestBoundary -and
    $oldestBoundary.timestamp_utc -le $windowStart -and
    $newestBoundary.timestamp_utc -ge $windowEnd
)

$ageMilliseconds = [Math]::Ceiling(($now - $windowStart).TotalMilliseconds + 60000)
$queryText = "*[System[(EventID=5152 or EventID=5153 or EventID=5156 or EventID=5157) and TimeCreated[timediff(@SystemTime) <= $ageMilliseconds]]]"
$sanitizedEvents = New-Object System.Collections.Generic.List[object]
$rawRecordsExamined = 0
$discardedUnrelated = 0
$maximumSanitizedEvents = 200000

if ($PSCmdlet.ShouldProcess(
    "Security log, run $RunId, $($windowStart.ToString('o')) - $($windowEnd.ToString('o'))",
    'Leggere eventi WFP e persistere esclusivamente campi sanitizzati'
)) {
    $query = New-Object System.Diagnostics.Eventing.Reader.EventLogQuery(
        'Security',
        [System.Diagnostics.Eventing.Reader.PathType]::LogName,
        $queryText
    )
    $reader = New-Object System.Diagnostics.Eventing.Reader.EventLogReader($query)
    try {
        while ($null -ne ($record = $reader.ReadEvent())) {
            try {
                $timestamp = $record.TimeCreated.ToUniversalTime()
                if ($timestamp -lt $windowStart -or $timestamp -gt $windowEnd) { continue }
                $eventPhases = @($phaseWindows | Where-Object {
                    $timestamp -ge $_.start_utc -and $timestamp -le $_.end_utc
                })
                if ($eventPhases.Count -ne 1) { continue }
                $eventPhase = [string]$eventPhases[0].phase
                $rawRecordsExamined++
                $eventId = [int]$record.Id
                $payload = Get-EventPayload -EventXml $record.ToXml()
                $processId = ConvertTo-NullableInteger -Value (Get-PayloadValue -Payload $payload -Name @('ProcessID', 'ProcessId')) -Maximum ([uint32]::MaxValue)
                $application = ConvertTo-NormalizedApplicationPath (Get-PayloadValue -Payload $payload -Name @('Application', 'ApplicationName'))
                $pathMatches = ($null -ne $application -and $application.Equals($terminalNormalized, [StringComparison]::OrdinalIgnoreCase))
                $pidMatches = ($null -ne $processId -and $uniqueTargetProcessId -contains [int]$processId)
                $sourceAddress = ConvertTo-NormalizedIp (Get-PayloadValue -Payload $payload -Name @('SourceAddress'))
                $destinationAddress = ConvertTo-NormalizedIp (Get-PayloadValue -Payload $payload -Name @('DestAddress', 'DestinationAddress'))
                $sourcePort = ConvertTo-NullableInteger -Value (Get-PayloadValue -Payload $payload -Name @('SourcePort')) -Maximum 65535
                $destinationPort = ConvertTo-NullableInteger -Value (Get-PayloadValue -Payload $payload -Name @('DestPort', 'DestinationPort')) -Maximum 65535
                $protocol = ConvertTo-NullableInteger -Value (Get-PayloadValue -Payload $payload -Name @('Protocol')) -Maximum 255
                $candidateDestinationMatches = (
                    $destinationAddress -ceq $endpointCheck.NormalizedHost -and
                    $destinationPort -eq $endpointCheck.Port
                )

                $targetBindable = ($eventId -in @(5156, 5157) -and ($pathMatches -or $pidMatches))
                $packetCorroboration = ($eventId -in @(5152, 5153) -and $candidateDestinationMatches)
                if (-not $targetBindable -and -not $packetCorroboration) {
                    $discardedUnrelated++
                    continue
                }

                $attribution = if ($pathMatches -and $pidMatches) {
                    'PATH_AND_PID'
                }
                elseif ($pathMatches) { 'PATH_ONLY' }
                elseif ($pidMatches) { 'PID_ONLY' }
                else { 'ENDPOINT_ONLY_CORROBORATION' }
                $decision = switch ($eventId) {
                    5156 { 'PERMITTED_CONNECTION' }
                    5157 { 'BLOCKED_CONNECTION' }
                    5152 { 'DROPPED_PACKET' }
                    5153 { 'DROPPED_PACKET' }
                }
                $directionValue = Get-PayloadValue -Payload $payload -Name @('Direction')
                if ($directionValue -cnotmatch '^(?:%%)?[0-9]+$') { $directionValue = $null }

                $sanitizedEvents.Add([ordered]@{
                    schema_version                = 1
                    run_id                        = $RunId
                    control                       = $Control
                    phase                         = $eventPhase
                    timestamp_utc                 = $timestamp.ToString('o')
                    record_id                     = [int64]$record.RecordId
                    event_id                      = $eventId
                    network_decision              = $decision
                    attribution                   = $attribution
                    process_id                    = $processId
                    application_path_sha256       = Get-Sha256Text $application
                    application_matches_terminal  = $pathMatches
                    process_id_in_job_set          = $pidMatches
                    direction_code                = $directionValue
                    protocol_number               = $protocol
                    source_address                = $sourceAddress
                    source_port                   = $sourcePort
                    destination_address           = $destinationAddress
                    destination_port              = $destinationPort
                    candidate_destination_matches = $candidateDestinationMatches
                })
                if ($sanitizedEvents.Count -gt $maximumSanitizedEvents) {
                    throw "Limite di $maximumSanitizedEvents eventi sanitizzati superato; output rifiutato."
                }
            }
            finally {
                $record.Dispose()
            }
        }
    }
    finally {
        $reader.Dispose()
    }

    $exactRequiredEvents = @($sanitizedEvents | Where-Object {
        $_.event_id -eq $RequiredEventId -and
        $_.attribution -ceq 'PATH_AND_PID' -and
        $_.candidate_destination_matches
    })
    $corroboratingDrops = @($sanitizedEvents | Where-Object {
        $_.event_id -in @(5152, 5153) -and $_.candidate_destination_matches
    })
    $unexpectedTerminalDestinations = @($sanitizedEvents | Where-Object {
        $_.event_id -in @(5156, 5157) -and
        $_.attribution -ceq 'PATH_AND_PID' -and
        -not $_.candidate_destination_matches
    })
    $permittedTerminalEvents = @($sanitizedEvents | Where-Object {
        $_.event_id -eq 5156 -and $_.attribution -ceq 'PATH_AND_PID'
    })

    $networkObservation = 'INCONCLUSIVE_REQUIRED_EVENT_NOT_OBSERVED'
    if ($Control -eq 'C4' -and $permittedTerminalEvents.Count -gt 0) {
        $networkObservation = 'C4_EGRESS_PERMITTED_OBSERVED'
    }
    elseif ($exactRequiredEvents.Count -gt 0) {
        $networkObservation = if ($Control -eq 'C4') {
            'CANDIDATE_BLOCKED_CONNECTION_OBSERVED'
        }
        else {
            'CANDIDATE_PERMITTED_CONNECTION_OBSERVED'
        }
    }
    if (-not $logWindowCovered) {
        $networkObservation = 'INCONCLUSIVE_SECURITY_LOG_WINDOW_NOT_COVERED'
    }

    try {
        $utf8WithoutBom = New-Object Text.UTF8Encoding($false)
        $eventsStream = $null
        $eventsWriter = $null
        try {
            $eventsStream = New-Object IO.FileStream($eventsPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::Read)
            $eventsWriter = New-Object IO.StreamWriter($eventsStream, $utf8WithoutBom)
            foreach ($event in $sanitizedEvents) {
                $eventsWriter.WriteLine(($event | ConvertTo-Json -Compress -Depth 6))
            }
            $eventsWriter.Flush()
            $eventsStream.Flush($true)
        }
        finally {
            if ($null -ne $eventsWriter) { $eventsWriter.Dispose() }
            elseif ($null -ne $eventsStream) { $eventsStream.Dispose() }
        }

        $eventsHash = (Get-FileHash -LiteralPath $eventsPath -Algorithm SHA256).Hash
        $manifest = [ordered]@{
            schema_version                         = 1
            document_type                          = 'mt5_direct_endpoint_wfp_security_export'
            generated_at_utc                       = [DateTime]::UtcNow.ToString('o')
            run_id                                 = $RunId
            control                                = $Control
            phases                                 = $ExpectedPhases
            phase_vocabulary                       = $PhaseVocabulary
            control_window                         = $ExpectedControlWindow
            window_start_utc                       = $windowStart.ToString('o')
            window_end_utc                         = $windowEnd.ToString('o')
            marker_log_sha256                      = $markerHash
            isolation_applied_state_sha256         = $isolationHash
            terminal_sha256                        = $terminalHash
            target_process_ids                     = $uniqueTargetProcessId
            candidate_endpoint                     = $endpointCheck.NormalizedEndpoint
            source_log                             = 'Security'
            source_log_oldest_record_id            = if ($null -ne $oldestBoundary) { $oldestBoundary.record_id } else { $null }
            source_log_oldest_timestamp_utc         = if ($null -ne $oldestBoundary) { $oldestBoundary.timestamp_utc.ToString('o') } else { $null }
            source_log_newest_record_id            = if ($null -ne $newestBoundary) { $newestBoundary.record_id } else { $null }
            source_log_newest_timestamp_utc         = if ($null -ne $newestBoundary) { $newestBoundary.timestamp_utc.ToString('o') } else { $null }
            security_log_window_covered             = $logWindowCovered
            audit_policy_verified_at_apply          = $true
            audit_policy_verified_after_window      = $auditPolicyVerifiedAfterWindow
            audit_connection_flags_after_window     = $auditConnectionFlags
            audit_drop_flags_after_window           = $auditDropFlags
            raw_records_examined                    = $rawRecordsExamined
            discarded_unrelated_records             = $discardedUnrelated
            sanitized_event_count                   = $sanitizedEvents.Count
            required_event_id                       = $RequiredEventId
            exact_required_event_count              = $exactRequiredEvents.Count
            corroborating_drop_count                = $corroboratingDrops.Count
            unexpected_terminal_destination_count  = $unexpectedTerminalDestinations.Count
            permitted_terminal_event_count          = $permittedTerminalEvents.Count
            network_observation                     = $networkObservation
            sanitized_events_file                   = [IO.Path]::GetFileName($eventsPath)
            sanitized_events_sha256                 = $eventsHash
            raw_security_log_exported               = $false
            raw_event_xml_persisted                 = $false
            login_success_inference_allowed         = $false
            identity_or_login_conclusion            = 'NOT_IN_SCOPE: combine with the independent MQL5 identity evidence'
            absence_conclusion                      = if ($exactRequiredEvents.Count -eq 0) { 'INCONCLUSIVE: absence of one WFP event is not proof of no attempt' } else { 'NOT_APPLICABLE' }
        }
        $manifestJson = $manifest | ConvertTo-Json -Depth 10
        $manifestStream = $null
        $manifestWriter = $null
        try {
            $manifestStream = New-Object IO.FileStream($manifestPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::Read)
            $manifestWriter = New-Object IO.StreamWriter($manifestStream, $utf8WithoutBom)
            $manifestWriter.Write($manifestJson)
            $manifestWriter.Flush()
            $manifestStream.Flush($true)
        }
        finally {
            if ($null -ne $manifestWriter) { $manifestWriter.Dispose() }
            elseif ($null -ne $manifestStream) { $manifestStream.Dispose() }
        }
        [pscustomobject]$manifest
    }
    catch {
        if (Test-Path -LiteralPath $eventsPath -PathType Leaf) {
            Remove-Item -LiteralPath $eventsPath -Force
        }
        if (Test-Path -LiteralPath $manifestPath -PathType Leaf) {
            Remove-Item -LiteralPath $manifestPath -Force
        }
        throw
    }
}
else {
    [pscustomobject]@{
        schema_version = 1
        mode           = 'Execute'
        executed       = $false
        mutates_host   = $false
        reason         = 'ShouldProcess/WhatIf'
    }
}
