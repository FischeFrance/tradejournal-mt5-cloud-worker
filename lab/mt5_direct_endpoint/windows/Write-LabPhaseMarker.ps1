#requires -Version 5.1

[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Medium')]
param(
    [Parameter(Mandatory = $true)]
    [string]$RunId,

    [Parameter(Mandatory = $true)]
    [ValidateSet(
        'C0_BASELINE',
        'C1_DISCOVERY_NEGATIVE',
        'C1_DISCOVERY_EXACT',
        'C2_LOGIN',
        'C2_CONNECTED',
        'C2_NETWORK_INTERRUPTION',
        'C2_RECONNECT',
        'C3_DIRECT_LOGIN',
        'C3_CONNECTED_STEADY',
        'C4_ENDPOINT_BLOCKED',
        'C5_DIRECT_LOGIN',
        'C5_CONNECTED_STEADY',
        'TEARDOWN'
    )]
    [string]$Phase,

    [Parameter(Mandatory = $true)]
    [ValidateSet('START', 'END')]
    [string]$Boundary,

    [string]$MarkerLogPath = 'C:\TJLab\evidence\phase-markers.jsonl',

    [string]$WprStartStatePath,

    [ValidateSet('PlanOnly', 'Execute')]
    [string]$Mode = 'PlanOnly',

    [string]$AuthorizationToken
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

Import-Module (Join-Path $PSScriptRoot 'MT5DirectEndpoint.Lab.psm1') -Force
Assert-LabRunId -RunId $RunId

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

$markerCode = "${Phase}_${Boundary}"
$markerTextTemplate = "MT5LAB|v2|run=$RunId|code=$markerCode|sequence=<runtime>|timestamp_unix_ms=<runtime>|qpc=<runtime>|qpc_frequency_hz=$([Diagnostics.Stopwatch]::Frequency)"
$markerParent = Split-Path -Path $MarkerLogPath -Parent
if ([string]::IsNullOrWhiteSpace($WprStartStatePath) -and -not [string]::IsNullOrWhiteSpace($markerParent)) {
    $WprStartStatePath = Join-LabPath $markerParent ("$RunId.wpr-start.json")
}
$plan = [pscustomobject]@{
    schema_version = 2
    mode           = 'PlanOnly'
    mutates_host   = $false
    run_id         = $RunId
    phase          = $Phase
    boundary       = $Boundary
    code            = $markerCode
    marker_schema_version = 2
    sequence_policy = 'CONTROL_EXACT_CONTIGUOUS_V1'
    marker_text    = $markerTextTemplate
    marker_log     = $MarkerLogPath
    wpr_start_state = $WprStartStatePath
    wpr_arguments  = @('-marker', $markerTextTemplate, '-flush')
}

if ($Mode -eq 'PlanOnly') {
    $plan
    return
}

if ($AuthorizationToken -cne 'MARK_PHASE_IN_DISPOSABLE_VM') {
    throw 'AuthorizationToken mancante o errato; nessun marker e stato scritto.'
}
if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    throw 'I marker WPR possono essere emessi soltanto su Windows.'
}

$parent = Split-Path -Path $MarkerLogPath -Parent
if ([string]::IsNullOrWhiteSpace($parent) -or -not (Test-Path -LiteralPath $parent -PathType Container)) {
    throw 'La directory del marker log deve esistere; lo script non la crea.'
}
$parentItem = Get-Item -LiteralPath $parent -Force
if (($parentItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
    throw 'La directory del marker log non puo essere un reparse point.'
}
if ([string]::IsNullOrWhiteSpace($WprStartStatePath) -or -not (Test-Path -LiteralPath $WprStartStatePath -PathType Leaf)) {
    throw 'State file WPR START mancante; il marker non puo essere attribuito al run richiesto.'
}
$wprState = Get-Content -LiteralPath $WprStartStatePath -Raw | ConvertFrom-Json
if ([string]$wprState.run_id -cne $RunId) {
    throw 'Lo state file WPR non appartiene al RunId richiesto.'
}
$wprStateItem = Get-Item -LiteralPath $WprStartStatePath -Force
if (($wprStateItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
    throw 'Lo state file WPR non puo essere un reparse point.'
}
$wprStopStatePath = Join-LabPath $parent ("$RunId.wpr-stop.json")
if (Test-Path -LiteralPath $wprStopStatePath -PathType Leaf) {
    throw 'La trace WPR risulta gia fermata; marker tardivo rifiutato.'
}

$existing = @()
if (Test-Path -LiteralPath $MarkerLogPath -PathType Leaf) {
    $existing = @(Get-Content -LiteralPath $MarkerLogPath | Where-Object { -not [string]::IsNullOrWhiteSpace($_) } | ForEach-Object { $_ | ConvertFrom-Json })
}
$runMarkers = @($existing | Where-Object { $_.run_id -ceq $RunId })
$control = Get-LabMarkerControl -PhaseName $Phase
if ($null -eq $control) {
    if ($runMarkers.Count -eq 0) {
        throw 'TEARDOWN richiede una sequenza marker di controllo completa.'
    }
    $control = Get-LabMarkerControl -PhaseName ([string]$runMarkers[0].phase)
}
if ([string]::IsNullOrWhiteSpace($control)) {
    throw 'Impossibile attribuire la sequenza marker a un controllo C0-C5.'
}

$requiredCodes = @(Get-LabControlMarkerCodes -Control $control)
$allowedCodes = @($requiredCodes + @('TEARDOWN_START', 'TEARDOWN_END'))
$requiredMarkerFields = @(
    'schema_version', 'run_id', 'phase', 'boundary', 'code', 'sequence',
    'timestamp_unix_ms', 'qpc', 'qpc_frequency_hz', 'marker_text'
)
$previousTimestampUnixMs = $null
$previousQpc = $null
for ($index = 0; $index -lt $runMarkers.Count; $index++) {
    $existingMarker = $runMarkers[$index]
    $actualFields = @($existingMarker.PSObject.Properties.Name | Sort-Object)
    if (($actualFields -join ',') -cne (($requiredMarkerFields | Sort-Object) -join ',')) {
        throw "Marker v2 con campi inattesi o mancanti alla sequenza $($index + 1)."
    }
    if (-not (Test-LabIntegerValue $existingMarker.schema_version) -or [int64]$existingMarker.schema_version -ne 2) {
        throw "Schema marker non valido alla sequenza $($index + 1)."
    }
    if (-not (Test-LabIntegerValue $existingMarker.sequence) -or [int64]$existingMarker.sequence -ne ($index + 1)) {
        throw 'La sequenza marker deve essere contigua e partire da 1.'
    }
    if ($index -ge $allowedCodes.Count -or [string]$existingMarker.code -cne $allowedCodes[$index]) {
        throw 'L ordine dei marker non coincide con la sequenza esatta del controllo.'
    }
    if ([string]$existingMarker.code -cne "$([string]$existingMarker.phase)_$([string]$existingMarker.boundary)") {
        throw "Codice marker incoerente alla sequenza $($index + 1)."
    }
    if (-not (Test-LabIntegerValue $existingMarker.timestamp_unix_ms) -or [int64]$existingMarker.timestamp_unix_ms -lt 1) {
        throw "timestamp_unix_ms marker non valido alla sequenza $($index + 1)."
    }
    if (-not (Test-LabIntegerValue $existingMarker.qpc) -or [int64]$existingMarker.qpc -lt 0) {
        throw "QPC marker non valido alla sequenza $($index + 1)."
    }
    if (
        -not (Test-LabIntegerValue $existingMarker.qpc_frequency_hz) -or
        [int64]$existingMarker.qpc_frequency_hz -ne [int64][Diagnostics.Stopwatch]::Frequency
    ) {
        throw "Frequenza QPC marker non valida alla sequenza $($index + 1)."
    }
    if (
        $null -ne $previousTimestampUnixMs -and
        [int64]$existingMarker.timestamp_unix_ms -lt [int64]$previousTimestampUnixMs
    ) {
        throw 'I timestamp marker non sono monotoni.'
    }
    if ($null -ne $previousQpc -and [int64]$existingMarker.qpc -le [int64]$previousQpc) {
        throw 'I QPC marker non sono strettamente crescenti.'
    }
    $expectedExistingText = "MT5LAB|v2|run=$RunId|code=$([string]$existingMarker.code)|sequence=$([int64]$existingMarker.sequence)|timestamp_unix_ms=$([int64]$existingMarker.timestamp_unix_ms)|qpc=$([int64]$existingMarker.qpc)|qpc_frequency_hz=$([int64]$existingMarker.qpc_frequency_hz)"
    if ([string]$existingMarker.marker_text -cne $expectedExistingText) {
        throw "Marker text non coerente con il record strutturato alla sequenza $($index + 1)."
    }
    $previousTimestampUnixMs = [int64]$existingMarker.timestamp_unix_ms
    $previousQpc = [int64]$existingMarker.qpc
}

if ($runMarkers.Count -ge $allowedCodes.Count -or $markerCode -cne $allowedCodes[$runMarkers.Count]) {
    $nextCode = if ($runMarkers.Count -lt $allowedCodes.Count) { $allowedCodes[$runMarkers.Count] } else { '<none>' }
    throw "Marker fuori sequenza: atteso $nextCode, ricevuto $markerCode."
}

$wprPath = Get-LabTrustedSystemToolPath -Name 'wpr.exe'
if ($PSCmdlet.ShouldProcess("WPR run $RunId", "Emettere marker $Phase/$Boundary e appendere il record locale")) {
    $sequence = $runMarkers.Count + 1
    $timestampUnixMs = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
    $qpc = [Diagnostics.Stopwatch]::GetTimestamp()
    if ($null -ne $previousTimestampUnixMs -and $timestampUnixMs -lt [int64]$previousTimestampUnixMs) {
        throw 'Clock UTC arretrato rispetto al marker precedente; marker rifiutato.'
    }
    if ($null -ne $previousQpc -and $qpc -le [int64]$previousQpc) {
        throw 'QPC non strettamente crescente rispetto al marker precedente; marker rifiutato.'
    }
    $qpcFrequency = [Diagnostics.Stopwatch]::Frequency
    $markerText = "MT5LAB|v2|run=$RunId|code=$markerCode|sequence=$sequence|timestamp_unix_ms=$timestampUnixMs|qpc=$qpc|qpc_frequency_hz=$qpcFrequency"
    $output = @(& $wprPath '-marker' $markerText '-flush' 2>&1)
    if ($LASTEXITCODE -ne 0) {
        throw "wpr -marker ha restituito exit code $LASTEXITCODE. Output: $($output -join ' | ')"
    }

    $record = [ordered]@{
        schema_version        = 2
        run_id                = $RunId
        phase                 = $Phase
        boundary              = $Boundary
        code                   = $markerCode
        sequence               = $sequence
        timestamp_unix_ms      = $timestampUnixMs
        qpc                    = $qpc
        qpc_frequency_hz       = $qpcFrequency
        marker_text           = $markerText
    }
    ($record | ConvertTo-Json -Compress) | Add-Content -LiteralPath $MarkerLogPath -Encoding UTF8
    [pscustomobject]$record
}
