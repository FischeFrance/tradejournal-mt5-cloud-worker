#requires -Version 5.1

[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Medium')]
param(
    [Parameter(Mandatory = $true)]
    [string]$RunId,

    [Parameter(Mandatory = $true)]
    [string]$InputEtlPath,

    [Parameter(Mandatory = $true)]
    [string]$OutputDirectory,

    [string]$MarkerLogPath,

    [string]$WprStopStatePath,

    [string]$TerminalPath,

    [int[]]$TargetProcessId = @(),

    [ValidateSet('PlanOnly', 'Execute')]
    [string]$Mode = 'PlanOnly',

    [string]$AuthorizationToken
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

Import-Module (Join-Path $PSScriptRoot 'MT5DirectEndpoint.Lab.psm1') -Force
Assert-LabRunId -RunId $RunId

$eventsPath = Join-LabPath $OutputDirectory ("$RunId.events.sanitized.jsonl")
$manifestPath = Join-LabPath $OutputDirectory ("$RunId.export-manifest.json")
if ([string]::IsNullOrWhiteSpace($WprStopStatePath)) {
    $etlParent = Split-Path -Path $InputEtlPath -Parent
    if (-not [string]::IsNullOrWhiteSpace($etlParent)) {
        $WprStopStatePath = Join-LabPath $etlParent ("$RunId.wpr-stop.json")
    }
}
$tempToken = [Guid]::NewGuid().ToString('N')
$tempXmlPath = Join-LabPath $OutputDirectory (".$RunId.$tempToken.tracerpt.xml")
$tempSummaryPath = Join-LabPath $OutputDirectory (".$RunId.$tempToken.tracerpt-summary.txt")
$tracerptArguments = @(
    $InputEtlPath,
    '-o', $tempXmlPath,
    '-of', 'XML',
    '-lr',
    '-summary', $tempSummaryPath,
    '-y'
)

if ($Mode -eq 'PlanOnly') {
    [pscustomobject]@{
        schema_version       = 2
        mode                 = 'PlanOnly'
        mutates_host         = $false
        opens_network        = $false
        run_id               = $RunId
        input_etl            = $InputEtlPath
        output_events        = $eventsPath
        output_manifest      = $manifestPath
        tracerpt_arguments   = $tracerptArguments
        target_process_ids   = @($TargetProcessId)
        phase_source         = $MarkerLogPath
        wpr_stop_state       = $WprStopStatePath
        raw_intermediate_kept = $false
        marker_schema_version = 2
        sanitized_event_schema_version = 2
        connection_id_policy = 'HASH_SAFE_PROVIDER_CONNECTION_ID_OR_NULL'
        readiness            = 'NO_GO'
        proof_capable        = $false
        exploratory_dataset_only = $true
        no_go_blockers       = @(
            'manca binding a manifest JobHarness con digest, PID e kernel creation time',
            'marker v2 non ha ancora parsing duplicate-key-safe e binding a digest immutabile',
            'bare TargetProcessId non e sufficiente per attribuzione process-scoped'
        )
        whitelist            = @(
            'provider/event metadata', 'UTC timestamp', 'header and payload PID',
            'process GUIDs', 'executable basename/path digest/terminal-match',
            'local/remote IP and port', 'DNS query and IP-only results',
            'status/failure code', 'phase from the marker log'
        )
        always_excluded      = @(
            'command line', 'environment', 'password', 'account number', 'user name',
            'balance/equity', 'orders/positions/history', 'arbitrary payload fields'
        )
        execution_gate       = 'Richiede -Mode Execute e AuthorizationToken=EXPORT_ETW_OFFLINE.'
    }
    return
}

if ($AuthorizationToken -cne 'EXPORT_ETW_OFFLINE') {
    throw 'AuthorizationToken mancante o errato; nessun file e stato prodotto.'
}
if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    throw 'L export ETL tramite tracerpt richiede Windows.'
}
if (-not (Test-Path -LiteralPath $InputEtlPath -PathType Leaf)) {
    throw "ETL non trovato: $InputEtlPath"
}
$inputEtlItem = Get-Item -LiteralPath $InputEtlPath -Force
if (($inputEtlItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
    throw 'ETL su reparse point rifiutato.'
}
if ([string]::IsNullOrWhiteSpace($WprStopStatePath) -or -not (Test-Path -LiteralPath $WprStopStatePath -PathType Leaf)) {
    throw 'State file WPR STOP mancante; l ETL non puo essere attestato.'
}
$stopStateItem = Get-Item -LiteralPath $WprStopStatePath -Force
if (($stopStateItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
    throw 'State file WPR STOP su reparse point rifiutato.'
}
$stopState = Get-Content -LiteralPath $WprStopStatePath -Raw | ConvertFrom-Json
if ([string]$stopState.run_id -cne $RunId -or $stopState.ready_for_export -ne $true) {
    throw 'State file WPR STOP non valido o trace marcata INCONCLUSIVE.'
}
$preflightInputHash = Get-FileHash -LiteralPath $InputEtlPath -Algorithm SHA256
if ([string]$stopState.etl_sha256 -cne $preflightInputHash.Hash) {
    throw 'SHA-256 dell ETL non coincide con lo state file WPR STOP.'
}
if (-not (Test-Path -LiteralPath $OutputDirectory -PathType Container)) {
    throw 'OutputDirectory deve esistere; lo script non la crea.'
}
$outputItem = Get-Item -LiteralPath $OutputDirectory -Force
if (($outputItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
    throw 'OutputDirectory non puo essere un reparse point.'
}
foreach ($path in @($eventsPath, $manifestPath, $tempXmlPath, $tempSummaryPath)) {
    if (Test-Path -LiteralPath $path) {
        throw "File di output/intermedio gia presente; sovrascrittura rifiutata: $path"
    }
}

function Get-LabMarkerControl {
    param([string]$PhaseName)

    if ($PhaseName -cmatch '^C([0-5])_') {
        return "C$($Matches[1])"
    }
    return $null
}

function Get-LabControlMarkerCodes {
    param([Parameter(Mandatory = $true)][string]$Control)

    switch ($Control) {
        'C0' {
            return @('C0_BASELINE_START', 'C0_BASELINE_END')
        }
        'C1' {
            return @(
                'C1_DISCOVERY_NEGATIVE_START',
                'C1_DISCOVERY_NEGATIVE_END',
                'C1_DISCOVERY_EXACT_START',
                'C1_DISCOVERY_EXACT_END'
            )
        }
        'C2' {
            return @(
                'C2_LOGIN_START',
                'C2_LOGIN_END',
                'C2_CONNECTED_START',
                'C2_CONNECTED_END',
                'C2_NETWORK_INTERRUPTION_START',
                'C2_NETWORK_INTERRUPTION_END',
                'C2_RECONNECT_START',
                'C2_RECONNECT_END'
            )
        }
        'C3' {
            return @(
                'C3_DIRECT_LOGIN_START',
                'C3_DIRECT_LOGIN_END',
                'C3_CONNECTED_STEADY_START',
                'C3_CONNECTED_STEADY_END'
            )
        }
        'C4' {
            return @('C4_ENDPOINT_BLOCKED_START', 'C4_ENDPOINT_BLOCKED_END')
        }
        'C5' {
            return @(
                'C5_DIRECT_LOGIN_START',
                'C5_DIRECT_LOGIN_END',
                'C5_CONNECTED_STEADY_START',
                'C5_CONNECTED_STEADY_END'
            )
        }
        default {
            throw "Controllo marker non supportato: $Control"
        }
    }
}

function Test-LabIntegerValue {
    param($Value)

    return (
        $Value -is [byte] -or $Value -is [sbyte] -or
        $Value -is [int16] -or $Value -is [uint16] -or
        $Value -is [int32] -or $Value -is [uint32] -or
        $Value -is [int64] -or $Value -is [uint64]
    )
}

$intervals = New-Object System.Collections.Generic.List[object]
$phaseMappingComplete = $false
if (-not [string]::IsNullOrWhiteSpace($MarkerLogPath)) {
    if (-not (Test-Path -LiteralPath $MarkerLogPath -PathType Leaf)) {
        throw "Marker log non trovato: $MarkerLogPath"
    }
    $markers = @(Get-Content -LiteralPath $MarkerLogPath | Where-Object { -not [string]::IsNullOrWhiteSpace($_) } | ForEach-Object { $_ | ConvertFrom-Json } | Where-Object { $_.run_id -ceq $RunId })
    $requiredMarkerFields = @(
        'schema_version', 'run_id', 'phase', 'boundary', 'code', 'sequence',
        'timestamp_unix_ms', 'qpc', 'qpc_frequency_hz', 'marker_text'
    )
    $markerControl = $null
    $requiredCodes = @()
    $allowedCodes = @()
    $previousTimestampUnixMs = $null
    $previousQpc = $null
    $markerFrequency = $null
    for ($markerIndex = 0; $markerIndex -lt $markers.Count; $markerIndex++) {
        $marker = $markers[$markerIndex]
        $actualFields = @($marker.PSObject.Properties.Name | Sort-Object)
        if (($actualFields -join ',') -cne (($requiredMarkerFields | Sort-Object) -join ',')) {
            throw "Marker v2 con campi inattesi o mancanti alla sequenza $($markerIndex + 1)."
        }
        if (-not (Test-LabIntegerValue $marker.schema_version) -or [int64]$marker.schema_version -ne 2) {
            throw "Schema marker non valido alla sequenza $($markerIndex + 1)."
        }
        if (-not (Test-LabIntegerValue $marker.sequence) -or [int64]$marker.sequence -ne ($markerIndex + 1)) {
            throw 'La sequenza marker deve essere contigua e partire da 1.'
        }
        if ($markerIndex -eq 0) {
            $markerControl = Get-LabMarkerControl -PhaseName ([string]$marker.phase)
            if ([string]::IsNullOrWhiteSpace($markerControl)) {
                throw 'Il primo marker deve appartenere a un controllo C0-C5.'
            }
            $requiredCodes = @(Get-LabControlMarkerCodes -Control $markerControl)
            $allowedCodes = @($requiredCodes + @('TEARDOWN_START', 'TEARDOWN_END'))
        }
        if ($markerIndex -ge $allowedCodes.Count -or [string]$marker.code -cne $allowedCodes[$markerIndex]) {
            throw 'L ordine dei marker non coincide con la sequenza esatta del controllo.'
        }
        if ([string]$marker.code -cne "$([string]$marker.phase)_$([string]$marker.boundary)") {
            throw "Codice marker incoerente alla sequenza $($markerIndex + 1)."
        }
        if (-not (Test-LabIntegerValue $marker.timestamp_unix_ms) -or [int64]$marker.timestamp_unix_ms -lt 1) {
            throw "timestamp_unix_ms marker non valido alla sequenza $($markerIndex + 1)."
        }
        if (-not (Test-LabIntegerValue $marker.qpc) -or [int64]$marker.qpc -lt 0) {
            throw "QPC marker non valido alla sequenza $($markerIndex + 1)."
        }
        if (
            -not (Test-LabIntegerValue $marker.qpc_frequency_hz) -or
            [int64]$marker.qpc_frequency_hz -lt 1 -or
            [int64]$marker.qpc_frequency_hz -gt 10000000000
        ) {
            throw "Frequenza QPC marker non valida alla sequenza $($markerIndex + 1)."
        }
        if ($null -eq $markerFrequency) {
            $markerFrequency = [int64]$marker.qpc_frequency_hz
        }
        elseif ([int64]$marker.qpc_frequency_hz -ne [int64]$markerFrequency) {
            throw 'La frequenza QPC deve essere identica per tutti i marker del run.'
        }
        if (
            $null -ne $previousTimestampUnixMs -and
            [int64]$marker.timestamp_unix_ms -lt [int64]$previousTimestampUnixMs
        ) {
            throw 'I timestamp marker non sono monotoni.'
        }
        if ($null -ne $previousQpc -and [int64]$marker.qpc -le [int64]$previousQpc) {
            throw 'I QPC marker non sono strettamente crescenti.'
        }
        $expectedMarkerText = "MT5LAB|v2|run=$RunId|code=$([string]$marker.code)|sequence=$([int64]$marker.sequence)|timestamp_unix_ms=$([int64]$marker.timestamp_unix_ms)|qpc=$([int64]$marker.qpc)|qpc_frequency_hz=$([int64]$marker.qpc_frequency_hz)"
        if ([string]$marker.marker_text -cne $expectedMarkerText) {
            throw "Marker text non coerente con il record strutturato alla sequenza $($markerIndex + 1)."
        }
        $previousTimestampUnixMs = [int64]$marker.timestamp_unix_ms
        $previousQpc = [int64]$marker.qpc
    }

    foreach ($start in @($markers | Where-Object { $_.boundary -ceq 'START' })) {
        $end = @($markers | Where-Object {
            [int64]$_.sequence -eq ([int64]$start.sequence + 1) -and
            $_.phase -ceq $start.phase -and
            $_.boundary -ceq 'END'
        })
        if ($end.Count -eq 1) {
            $startUtc = [DateTimeOffset]::FromUnixTimeMilliseconds([int64]$start.timestamp_unix_ms).UtcDateTime
            $endUtc = [DateTimeOffset]::FromUnixTimeMilliseconds([int64]$end[0].timestamp_unix_ms).UtcDateTime
            if ($endUtc -lt $startUtc) {
                throw "Intervallo marker invertito per fase $($start.phase)."
            }
            $intervals.Add([pscustomobject]@{ phase = [string]$start.phase; start_utc = $startUtc; end_utc = $endUtc })
        }
    }
    $actualControlCodes = @($markers | Where-Object { $_.phase -cne 'TEARDOWN' } | ForEach-Object { [string]$_.code })
    $phaseMappingComplete = (
        $markers.Count -gt 0 -and
        ($actualControlCodes -join ',') -ceq ($requiredCodes -join ',') -and
        @($markers | Where-Object { $_.boundary -ceq 'START' }).Count -eq $intervals.Count
    )
    $sortedIntervals = @($intervals | Sort-Object -Property start_utc)
    for ($intervalIndex = 1; $intervalIndex -lt $sortedIntervals.Count; $intervalIndex++) {
        if ($sortedIntervals[$intervalIndex].start_utc -lt $sortedIntervals[$intervalIndex - 1].end_utc) {
            throw "Intervalli fase sovrapposti: $($sortedIntervals[$intervalIndex - 1].phase) e $($sortedIntervals[$intervalIndex].phase)."
        }
        if ($sortedIntervals[$intervalIndex].start_utc -eq $sortedIntervals[$intervalIndex - 1].end_utc) {
            # Unix milliseconds are intentionally coarse.  A shared boundary
            # cannot unambiguously attribute an ETW event at that millisecond.
            $phaseMappingComplete = $false
        }
    }
}

function Get-PhaseForTimestamp {
    param([DateTime]$TimestampUtc)

    $match = @($intervals | Where-Object { $TimestampUtc -ge $_.start_utc -and $TimestampUtc -le $_.end_utc })
    if ($match.Count -eq 1) {
        return [string]$match[0].phase
    }
    return $null
}

function Get-XmlNodeText {
    param(
        [System.Xml.XmlNode]$Node,
        [string]$XPath
    )

    $selected = $Node.SelectSingleNode($XPath)
    if ($null -eq $selected) {
        return $null
    }
    return [string]$selected.InnerText
}

function ConvertTo-NullableInt {
    param([string]$Value)

    $parsed = 0
    if (-not [string]::IsNullOrWhiteSpace($Value) -and [int]::TryParse($Value, [ref]$parsed) -and $parsed -ge 0) {
        return $parsed
    }
    return $null
}

function ConvertTo-NormalizedIp {
    param([string]$Value)

    if ([string]::IsNullOrWhiteSpace($Value)) {
        return $null
    }
    $parsed = $null
    if ([Net.IPAddress]::TryParse($Value.Trim(' ', '[', ']'), [ref]$parsed)) {
        return $parsed.ToString().ToLowerInvariant()
    }
    return $null
}

function Get-Sha256Text {
    param([string]$Value)

    if ([string]::IsNullOrWhiteSpace($Value)) {
        return $null
    }
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [Text.Encoding]::UTF8.GetBytes($Value.ToLowerInvariant())
        return ([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-', '')
    }
    finally {
        $sha.Dispose()
    }
}

function Get-Sha256OpaqueText {
    param([string]$Value)

    if ([string]::IsNullOrWhiteSpace($Value)) {
        return $null
    }
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        # Opaque provider identifiers are byte/case sensitive.  Unlike Windows
        # paths, lower-casing them could merge two distinct connection IDs.
        $bytes = [Text.Encoding]::UTF8.GetBytes($Value)
        return ([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-', '')
    }
    finally {
        $sha.Dispose()
    }
}

function Get-PayloadValue {
    param(
        [hashtable]$Payload,
        [string[]]$Names
    )

    foreach ($name in $Names) {
        if ($Payload.ContainsKey($name) -and -not [string]::IsNullOrWhiteSpace([string]$Payload[$name])) {
            return [string]$Payload[$name]
        }
    }
    return $null
}

function Get-SafeProviderConnectionIdHash {
    param(
        [hashtable]$Payload,
        [string]$ProviderName,
        [string]$ProviderGuid
    )

    # Only explicit connection-id fields emitted by the ETW provider may become
    # a correlation key.  Never synthesize an identifier from endpoint tuples,
    # PID, timestamps, status text, or arbitrary payload content.
    $providerIdentity = if (-not [string]::IsNullOrWhiteSpace($ProviderGuid)) {
        "guid=$($ProviderGuid.Trim().ToLowerInvariant())"
    }
    elseif (-not [string]::IsNullOrWhiteSpace($ProviderName)) {
        "name=$($ProviderName.Trim().ToLowerInvariant())"
    }
    else {
        return $null
    }

    foreach ($fieldName in @('ConnectionId', 'ConnectionID', 'ConnId', 'ConnID')) {
        if (-not $Payload.ContainsKey($fieldName)) {
            continue
        }
        $rawValue = [string]$Payload[$fieldName]
        if ([string]::IsNullOrWhiteSpace($rawValue)) {
            return $null
        }
        $candidate = $rawValue.Trim()
        if (
            $candidate.Length -gt 128 -or
            $candidate -cnotmatch '^[A-Za-z0-9{][A-Za-z0-9{}().:_-]{0,127}$'
        ) {
            return $null
        }
        $digestPreimage = "provider:$($providerIdentity.Length):$providerIdentity;field:$($fieldName.Length):$fieldName;id:$($candidate.Length):$candidate"
        return (Get-Sha256OpaqueText $digestPreimage)
    }
    return $null
}

function ConvertTo-SafeDnsName {
    param([string]$Value)

    if ([string]::IsNullOrWhiteSpace($Value)) {
        return $null
    }
    $candidate = $Value.Trim().TrimEnd('.').ToLowerInvariant()
    if ($candidate.Length -le 253 -and $candidate -cmatch '^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$') {
        return $candidate
    }
    return $null
}

function Convert-EventNode {
    param([System.Xml.XmlNode]$EventNode)

    $system = $EventNode.SelectSingleNode("*[local-name()='System']")
    if ($null -eq $system) {
        return $null
    }

    $providerNode = $system.SelectSingleNode("*[local-name()='Provider']")
    $providerName = if ($null -ne $providerNode -and $null -ne $providerNode.Attributes['Name']) { [string]$providerNode.Attributes['Name'].Value } else { $null }
    $providerGuid = if ($null -ne $providerNode -and $null -ne $providerNode.Attributes['Guid']) { [string]$providerNode.Attributes['Guid'].Value } else { $null }
    $executionNode = $system.SelectSingleNode("*[local-name()='Execution']")
    $headerPidText = if ($null -ne $executionNode -and $null -ne $executionNode.Attributes['ProcessID']) { [string]$executionNode.Attributes['ProcessID'].Value } else { $null }
    $headerTidText = if ($null -ne $executionNode -and $null -ne $executionNode.Attributes['ThreadID']) { [string]$executionNode.Attributes['ThreadID'].Value } else { $null }
    $timeNode = $system.SelectSingleNode("*[local-name()='TimeCreated']")
    $timeText = if ($null -ne $timeNode -and $null -ne $timeNode.Attributes['SystemTime']) { [string]$timeNode.Attributes['SystemTime'].Value } else { $null }
    if ([string]::IsNullOrWhiteSpace($timeText)) {
        return $null
    }
    $timestamp = [DateTime]::Parse($timeText, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::RoundtripKind).ToUniversalTime()

    $payload = @{}
    foreach ($dataNode in @($EventNode.SelectNodes(".//*[local-name()='EventData']/*[local-name()='Data']"))) {
        if ($null -ne $dataNode.Attributes['Name']) {
            $name = [string]$dataNode.Attributes['Name'].Value
            if (-not $payload.ContainsKey($name)) {
                $payload[$name] = [string]$dataNode.InnerText
            }
        }
    }
    foreach ($leafNode in @($EventNode.SelectNodes(".//*[local-name()='UserData']//*[not(*)]"))) {
        $name = [string]$leafNode.LocalName
        if (-not $payload.ContainsKey($name)) {
            $payload[$name] = [string]$leafNode.InnerText
        }
    }

    # Header PID and payload PID are deliberately separate: for kernel TCP/IP,
    # attribution must use the PID carried by the event payload.
    $payloadPid = ConvertTo-NullableInt (Get-PayloadValue -Payload $payload -Names @('ProcessId', 'ProcessID', 'PID', 'pid'))
    $headerPid = ConvertTo-NullableInt $headerPidText
    $headerTid = ConvertTo-NullableInt $headerTidText
    $parentPid = ConvertTo-NullableInt (Get-PayloadValue -Payload $payload -Names @('ParentProcessId', 'ParentProcessID', 'ParentId', 'PPID'))

    $imageValue = Get-PayloadValue -Payload $payload -Names @('Image', 'ImageName', 'ProcessName', 'FileName', 'Application')
    $imageName = if ([string]::IsNullOrWhiteSpace($imageValue)) { $null } else { [IO.Path]::GetFileName($imageValue) }
    $imageMatchesTerminal = $false
    if (-not [string]::IsNullOrWhiteSpace($TerminalPath) -and -not [string]::IsNullOrWhiteSpace($imageValue)) {
        $imageMatchesTerminal = $imageValue.Equals($TerminalPath, [StringComparison]::OrdinalIgnoreCase)
    }

    $remoteAddress = ConvertTo-NormalizedIp (Get-PayloadValue -Payload $payload -Names @('RemoteAddress', 'RemoteAddr', 'daddr', 'DestAddress', 'DestinationAddress', 'DestinationIp', 'DestinationIP'))
    $localAddress = ConvertTo-NormalizedIp (Get-PayloadValue -Payload $payload -Names @('LocalAddress', 'LocalAddr', 'saddr', 'SourceAddress', 'SourceIp', 'SourceIP'))
    $remotePort = ConvertTo-NullableInt (Get-PayloadValue -Payload $payload -Names @('RemotePort', 'dport', 'DestPort', 'DestinationPort'))
    $localPort = ConvertTo-NullableInt (Get-PayloadValue -Payload $payload -Names @('LocalPort', 'sport', 'SourcePort'))

    $queryName = ConvertTo-SafeDnsName (Get-PayloadValue -Payload $payload -Names @('QueryName', 'HostName'))
    $queryResultText = Get-PayloadValue -Payload $payload -Names @('QueryResults', 'QueryResult', 'AddressList', 'Addresses')
    $queryAddresses = New-Object System.Collections.Generic.List[string]
    if (-not [string]::IsNullOrWhiteSpace($queryResultText)) {
        foreach ($token in ($queryResultText -split '[,;\s]+')) {
            $normalized = ConvertTo-NormalizedIp $token
            if ($null -ne $normalized -and -not $queryAddresses.Contains($normalized)) {
                $queryAddresses.Add($normalized)
            }
        }
    }

    $taskText = Get-XmlNodeText -Node $EventNode -XPath ".//*[local-name()='RenderingInfo']/*[local-name()='Task']"
    $opcodeText = Get-XmlNodeText -Node $EventNode -XPath ".//*[local-name()='RenderingInfo']/*[local-name()='Opcode']"
    $providerIdentity = (([string]$providerName) + ' ' + ([string]$providerGuid)).ToLowerInvariant()
    $category = 'OTHER_CAPTURED'
    if ($providerIdentity.Contains('dns') -or $null -ne $queryName) {
        $category = 'DNS'
    }
    elseif ($providerIdentity.Contains('tcpip') -or $providerIdentity.Contains('kernel-network') -or $null -ne $remoteAddress -or $null -ne $remotePort) {
        $category = 'NETWORK'
    }
    elseif ($providerIdentity.Contains('process') -or -not [string]::IsNullOrWhiteSpace($imageName)) {
        $category = 'PROCESS_OR_IMAGE'
    }
    elseif (([string]$taskText).IndexOf('mark', [StringComparison]::OrdinalIgnoreCase) -ge 0) {
        $category = 'MARKER'
    }

    if ($TargetProcessId.Count -gt 0) {
        $pidMatch = (($null -ne $payloadPid -and $TargetProcessId -contains $payloadPid) -or
            ($category -in @('DNS', 'PROCESS_OR_IMAGE') -and $null -ne $headerPid -and $TargetProcessId -contains $headerPid))
        if (-not $pidMatch -and -not $imageMatchesTerminal -and $category -ne 'MARKER') {
            return $null
        }
    }

    return [ordered]@{
        schema_version          = 2
        run_id                  = $RunId
        phase                   = Get-PhaseForTimestamp -TimestampUtc $timestamp
        timestamp_utc           = $timestamp.ToString('o')
        category                = $category
        provider_name           = $providerName
        provider_guid           = $providerGuid
        event_id                = ConvertTo-NullableInt (Get-XmlNodeText -Node $system -XPath "*[local-name()='EventID']")
        task                    = $taskText
        opcode                  = $opcodeText
        header_process_id       = $headerPid
        header_thread_id        = $headerTid
        payload_process_id      = $payloadPid
        parent_process_id       = $parentPid
        process_guid            = Get-PayloadValue -Payload $payload -Names @('ProcessGuid', 'ProcessGUID')
        parent_process_guid     = Get-PayloadValue -Payload $payload -Names @('ParentProcessGuid', 'ParentProcessGUID')
        connection_id_sha256    = Get-SafeProviderConnectionIdHash -Payload $payload -ProviderName $providerName -ProviderGuid $providerGuid
        image_name              = $imageName
        image_path_sha256       = Get-Sha256Text $imageValue
        image_matches_terminal  = $imageMatchesTerminal
        protocol                = Get-PayloadValue -Payload $payload -Names @('Protocol', 'ProtocolName')
        local_address           = $localAddress
        local_port              = $localPort
        remote_address          = $remoteAddress
        remote_port             = $remotePort
        dns_query_name          = $queryName
        dns_result_addresses    = @($queryAddresses.ToArray())
        status                  = Get-PayloadValue -Payload $payload -Names @('Status', 'QueryStatus', 'FailureCode', 'ErrorCode')
    }
}

$tracerptPath = Get-LabTrustedSystemToolPath -Name 'tracerpt.exe'
$writer = $null
$eventsStream = $null
$totalXmlEvents = 0
$exportedEvents = 0
$skippedEvents = 0
$categoryCounts = @{}
$lossStatus = 'UNKNOWN'
$eventsLost = $null
$buffersLost = $null

if ($PSCmdlet.ShouldProcess($InputEtlPath, 'Convertire offline ETL e scrivere solo eventi sanitizzati')) {
    try {
        $tracerptOutput = @(& $tracerptPath @tracerptArguments 2>&1)
        if ($LASTEXITCODE -ne 0) {
            throw "tracerpt ha restituito exit code $LASTEXITCODE. Output: $($tracerptOutput -join ' | ')"
        }
        if (-not (Test-Path -LiteralPath $tempXmlPath -PathType Leaf)) {
            throw 'tracerpt non ha prodotto il dump XML atteso.'
        }

        $utf8WithoutBom = New-Object Text.UTF8Encoding($false)
        $eventsStream = New-Object IO.FileStream(
            $eventsPath,
            [IO.FileMode]::CreateNew,
            [IO.FileAccess]::Write,
            [IO.FileShare]::Read
        )
        $writer = New-Object IO.StreamWriter($eventsStream, $utf8WithoutBom)
        $settings = New-Object Xml.XmlReaderSettings
        $settings.DtdProcessing = [Xml.DtdProcessing]::Prohibit
        $settings.XmlResolver = $null
        $reader = [Xml.XmlReader]::Create($tempXmlPath, $settings)
        try {
            while ($reader.Read()) {
                if ($reader.NodeType -eq [Xml.XmlNodeType]::Element -and $reader.LocalName -eq 'Event') {
                    $outerXml = $reader.ReadOuterXml()
                    if ([string]::IsNullOrWhiteSpace($outerXml)) {
                        continue
                    }
                    $totalXmlEvents++
                    $eventDocument = New-Object Xml.XmlDocument
                    $eventDocument.XmlResolver = $null
                    $eventDocument.LoadXml($outerXml)
                    $normalized = Convert-EventNode -EventNode $eventDocument.DocumentElement
                    if ($null -eq $normalized) {
                        $skippedEvents++
                        continue
                    }
                    $writer.WriteLine(($normalized | ConvertTo-Json -Compress -Depth 8))
                    $exportedEvents++
                    $category = [string]$normalized.category
                    if (-not $categoryCounts.ContainsKey($category)) {
                        $categoryCounts[$category] = 0
                    }
                    $categoryCounts[$category]++
                }
            }
        }
        finally {
            $reader.Dispose()
            $writer.Dispose()
            $writer = $null
            $eventsStream = $null
        }

        if (Test-Path -LiteralPath $tempSummaryPath -PathType Leaf) {
            $summaryText = Get-Content -LiteralPath $tempSummaryPath -Raw
            $eventLossMatch = [regex]::Match($summaryText, '(?im)(?:events?\s+lost|lost\s+events?)\s*[:=]\s*(\d+)')
            $bufferLossMatch = [regex]::Match($summaryText, '(?im)(?:buffers?\s+lost|lost\s+buffers?)\s*[:=]\s*(\d+)')
            if ($eventLossMatch.Success) {
                $eventsLost = [int64]$eventLossMatch.Groups[1].Value
            }
            if ($bufferLossMatch.Success) {
                $buffersLost = [int64]$bufferLossMatch.Groups[1].Value
            }
            if ($null -ne $eventsLost -or $null -ne $buffersLost) {
                $eventLossTotal = [int64]0
                if ($null -ne $eventsLost) { $eventLossTotal += $eventsLost }
                if ($null -ne $buffersLost) { $eventLossTotal += $buffersLost }
                $lossStatus = if ($eventLossTotal -eq 0) { 'ZERO' } else { 'NONZERO' }
            }
        }

        $inputHash = Get-FileHash -LiteralPath $InputEtlPath -Algorithm SHA256
        if ($inputHash.Hash -cne $preflightInputHash.Hash) {
            throw 'L ETL e cambiato durante la normalizzazione; output rifiutato.'
        }
        $eventHash = Get-FileHash -LiteralPath $eventsPath -Algorithm SHA256
        $manifest = [ordered]@{
            schema_version         = 2
            run_id                 = $RunId
            generated_at_utc       = [DateTime]::UtcNow.ToString('o')
            input_etl_sha256       = $inputHash.Hash
            wpr_stop_state_sha256  = (Get-FileHash -LiteralPath $WprStopStatePath -Algorithm SHA256).Hash
            sanitized_events_file  = [IO.Path]::GetFileName($eventsPath)
            sanitized_events_sha256 = $eventHash.Hash
            sanitized_event_schema_version = 2
            marker_schema_version  = 2
            connection_id_policy   = 'HASH_SAFE_PROVIDER_CONNECTION_ID_OR_NULL'
            target_process_ids     = @($TargetProcessId)
            terminal_path_sha256   = Get-Sha256Text $TerminalPath
            total_xml_events       = $totalXmlEvents
            exported_events        = $exportedEvents
            skipped_events         = $skippedEvents
            category_counts        = $categoryCounts
            phase_mapping_complete = $phaseMappingComplete
            event_loss_status      = $lossStatus
            events_lost            = $eventsLost
            buffers_lost           = $buffersLost
            capture_integrity_precheck = ($lossStatus -eq 'ZERO' -and $phaseMappingComplete -and $TargetProcessId.Count -gt 0)
            readiness             = 'NO_GO'
            proof_capable         = $false
            exploratory_dataset_only = $true
            ready_for_analysis    = $false
            no_go_reason          = 'Manca binding immutabile a JobHarness metadata/digest/PID/kernel creation time e parsing marker duplicate-key-safe con digest strict.'
            raw_intermediate_kept  = $false
            excluded_fields        = @('command_line', 'environment', 'credentials', 'account_number', 'arbitrary_payload')
        }
        $manifestJson = $manifest | ConvertTo-Json -Depth 10
        $manifestStream = $null
        $manifestWriter = $null
        try {
            $manifestStream = New-Object IO.FileStream(
                $manifestPath,
                [IO.FileMode]::CreateNew,
                [IO.FileAccess]::Write,
                [IO.FileShare]::Read
            )
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
    finally {
        if ($null -ne $writer) {
            $writer.Dispose()
        }
        elseif ($null -ne $eventsStream) {
            $eventsStream.Dispose()
        }
        # Gli intermedi tracerpt possono contenere payload non filtrati. Sono file
        # creati da questa invocazione con nomi GUID e vengono rimossi sempre.
        foreach ($tempPath in @($tempXmlPath, $tempSummaryPath)) {
            if (Test-Path -LiteralPath $tempPath -PathType Leaf) {
                Remove-Item -LiteralPath $tempPath -Force
            }
        }
    }
}
