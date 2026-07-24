#requires -Version 5.1

[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'High')]
param(
    [ValidateSet('Plan', 'ValidateProfile', 'Status', 'Start', 'Stop')]
    [string]$Action = 'Plan',

    [Parameter(Mandatory = $true)]
    [string]$RunId,

    [string]$EvidenceDirectory = 'C:\TJLab\evidence',

    [string]$ProfilePath = '',

    [switch]$Execute,

    [string]$AuthorizationToken
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if ([string]::IsNullOrWhiteSpace($ProfilePath)) {
    if ([string]::IsNullOrWhiteSpace($PSScriptRoot)) {
        throw 'Impossibile risolvere ProfilePath: PSScriptRoot è assente.'
    }

    $labRoot = Split-Path -Path $PSScriptRoot -Parent
    if ([string]::IsNullOrWhiteSpace($labRoot)) {
        throw 'Impossibile risolvere la directory del laboratorio a partire da PSScriptRoot.'
    }

    if (-not (Test-Path -LiteralPath $labRoot -PathType Container)) {
        throw "Lab root non valido o non disponibile: '$labRoot'."
    }

    $ProfilePath = Join-Path $labRoot 'profiles\mt5-network.wprp'
}

Import-Module (Join-Path $PSScriptRoot 'MT5DirectEndpoint.Lab.psm1') -Force
Assert-LabRunId -RunId $RunId

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

function Invoke-WprCommand {
    param(
        [Parameter(Mandatory = $true)]
        [string]$WprPath,

        [Parameter(Mandatory = $true)]
        [string[]]$Arguments
    )

    $output = @(& $WprPath @Arguments 2>&1)
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        throw "wpr.exe ha restituito exit code $exitCode. Output: $($output -join ' | ')"
    }
    return @($output)
}

$etlPath = Join-LabPath $EvidenceDirectory ("$RunId.etl")
$startStatePath = Join-LabPath $EvidenceDirectory ("$RunId.wpr-start.json")
$stopStatePath = Join-LabPath $EvidenceDirectory ("$RunId.wpr-stop.json")
$profileSelector = "$ProfilePath!MT5DirectEndpoint.Verbose"

$commandPlan = [ordered]@{
    validate_profile = @('-profiles', $ProfilePath)
    status           = @('-status')
    start            = @('-start', $profileSelector, '-filemode', '-recordtempto', $EvidenceDirectory)
    start_marker     = @('-marker', "MT5LAB|v1|run=$RunId|phase=TRACE|boundary=START")
    stop_marker      = @('-marker', "MT5LAB|v1|run=$RunId|phase=TRACE|boundary=END", '-flush')
    stop             = @('-stop', $etlPath, "MT5 direct-endpoint lab run $RunId")
}

if (-not $Execute -or $Action -eq 'Plan') {
    [pscustomobject]@{
        schema_version      = 1
        mode                = 'PlanOnly'
        requested_action    = $Action
        mutates_host        = $false
        execute_requested   = [bool]$Execute
        run_id              = $RunId
        profile_path        = $ProfilePath
        etl_path            = $etlPath
        commands            = $commandPlan
        execution_gate      = 'Richiede -Execute e AuthorizationToken=START_ETW_ON_DISPOSABLE_VM per Start/Stop.'
        current_host_notice = 'Non eseguire sulla workstation corrente; usare esclusivamente la VM disposable del laboratorio.'
    }
    return
}

$isWindows = ([Environment]::OSVersion.Platform -eq [PlatformID]::Win32NT)
if (-not $isWindows) {
    throw 'L esecuzione WPR e disponibile soltanto su Windows.'
}

$wprPath = Get-LabTrustedSystemToolPath -Name 'wpr.exe'

if ($Action -in @('Start', 'Stop')) {
    if ($AuthorizationToken -cne 'START_ETW_ON_DISPOSABLE_VM') {
        throw 'AuthorizationToken mancante o errato. Nessuna sessione ETW e stata modificata.'
    }
    if (-not (Test-IsAdministrator)) {
        throw 'Start/Stop WPR richiedono una console PowerShell elevata nella VM disposable.'
    }
}

if ($Action -ne 'Status' -and -not (Test-Path -LiteralPath $ProfilePath -PathType Leaf)) {
    throw "Profilo WPR non trovato: $ProfilePath"
}

if ($Action -in @('Start', 'Stop')) {
    if (-not (Test-Path -LiteralPath $EvidenceDirectory -PathType Container)) {
        throw 'EvidenceDirectory deve esistere; lo script rifiuta di crearla implicitamente.'
    }
    $evidenceItem = Get-Item -LiteralPath $EvidenceDirectory -Force
    if (($evidenceItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw 'EvidenceDirectory non puo essere un reparse point.'
    }
}

switch ($Action) {
    'ValidateProfile' {
        if ($PSCmdlet.ShouldProcess($ProfilePath, 'Validare il profilo WPR senza avviare una registrazione')) {
            $output = Invoke-WprCommand -WprPath $wprPath -Arguments $commandPlan.validate_profile
            [pscustomobject]@{ action = 'ValidateProfile'; exit_code = 0; output = @($output) }
        }
    }
    'Status' {
        if ($PSCmdlet.ShouldProcess('WPR', 'Leggere lo stato della sessione')) {
            $output = Invoke-WprCommand -WprPath $wprPath -Arguments $commandPlan.status
            [pscustomobject]@{ action = 'Status'; exit_code = 0; output = @($output) }
        }
    }
    'Start' {
        if (Test-Path -LiteralPath $startStatePath) {
            throw "State file gia presente: $startStatePath"
        }
        if (Test-Path -LiteralPath $etlPath) {
            throw "ETL gia presente; sovrascrittura rifiutata: $etlPath"
        }

        if ($PSCmdlet.ShouldProcess("WPR run $RunId", 'Avviare ETW kernel TCP/IP/process/DNS nella VM disposable')) {
            [void](Invoke-WprCommand -WprPath $wprPath -Arguments $commandPlan.validate_profile)
            $startedAt = [DateTime]::UtcNow.ToString('o')
            # Scriviamo un intent immutabile prima di avviare WPR. In questo modo
            # Stop resta disponibile anche se il marker iniziale fallisce.
            $state = [pscustomobject]@{
                schema_version         = 1
                run_id                 = $RunId
                start_requested_at_utc = $startedAt
                profile_path           = (Resolve-Path -LiteralPath $ProfilePath).Path
                profile_sha256         = (Get-FileHash -LiteralPath $ProfilePath -Algorithm SHA256).Hash
                expected_etl_path      = $etlPath
                wpr_path               = $wprPath
            }
            $state | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $startStatePath -Encoding UTF8 -NoNewline
            [void](Invoke-WprCommand -WprPath $wprPath -Arguments $commandPlan.start)
            try {
                [void](Invoke-WprCommand -WprPath $wprPath -Arguments $commandPlan.start_marker)
            }
            catch {
                $startMarkerError = $_.Exception.Message
                $emergencyStopError = $null
                try {
                    # La Start appena riuscita ci attribuisce la sessione. Salviamo
                    # comunque l ETL invece di lasciare una trace attiva; il run non
                    # potra mai essere promosso perche ready_for_export=false.
                    [void](Invoke-WprCommand -WprPath $wprPath -Arguments $commandPlan.stop)
                    if (Test-Path -LiteralPath $etlPath -PathType Leaf) {
                        $failedEtl = Get-Item -LiteralPath $etlPath
                        $failedState = [pscustomobject]@{
                            schema_version        = 1
                            run_id                = $RunId
                            stopped_at_utc        = [DateTime]::UtcNow.ToString('o')
                            etl_path              = $failedEtl.FullName
                            etl_size_bytes        = $failedEtl.Length
                            etl_sha256            = (Get-FileHash -LiteralPath $etlPath -Algorithm SHA256).Hash
                            start_marker_succeeded = $false
                            start_marker_error    = $startMarkerError
                            ready_for_export      = $false
                        }
                        $failedState | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $stopStatePath -Encoding UTF8 -NoNewline
                    }
                }
                catch {
                    $emergencyStopError = $_.Exception.Message
                }
                if ($null -ne $emergencyStopError) {
                    throw "Marker START fallito e stop di emergenza fallito. Verificare subito WPR. Marker: $startMarkerError Stop: $emergencyStopError"
                }
                throw "Marker START fallito; WPR e stato fermato e il run e INCONCLUSIVE. $startMarkerError"
            }

            [pscustomobject]@{ action = 'Start'; run_id = $RunId; state_path = $startStatePath; expected_etl_path = $etlPath }
        }
    }
    'Stop' {
        if (-not (Test-Path -LiteralPath $startStatePath -PathType Leaf)) {
            throw "State file START mancante: $startStatePath"
        }
        $startStateItem = Get-Item -LiteralPath $startStatePath -Force
        if (($startStateItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw 'State file START non puo essere un reparse point.'
        }
        if (Test-Path -LiteralPath $stopStatePath) {
            throw "State file STOP gia presente: $stopStatePath"
        }
        if (Test-Path -LiteralPath $etlPath) {
            throw "ETL gia presente; sovrascrittura rifiutata: $etlPath"
        }

        $startState = Get-Content -LiteralPath $startStatePath -Raw | ConvertFrom-Json
        if ([string]$startState.run_id -cne $RunId -or [string]$startState.expected_etl_path -cne $etlPath) {
            throw 'State file START non corrisponde al RunId o al percorso ETL richiesto.'
        }

        if ($PSCmdlet.ShouldProcess("WPR run $RunId", 'Scrivere il marker finale e fermare/salvare la trace ETW')) {
            $stopMarkerSucceeded = $true
            $stopMarkerError = $null
            try {
                [void](Invoke-WprCommand -WprPath $wprPath -Arguments $commandPlan.stop_marker)
            }
            catch {
                # Fermare WPR e piu importante del marker: lasciarlo attivo puo
                # consumare il disco. La trace sara marcata INCONCLUSIVE.
                $stopMarkerSucceeded = $false
                $stopMarkerError = $_.Exception.Message
            }
            [void](Invoke-WprCommand -WprPath $wprPath -Arguments $commandPlan.stop)
            if (-not (Test-Path -LiteralPath $etlPath -PathType Leaf)) {
                throw 'wpr -stop non ha prodotto il file ETL atteso.'
            }
            $etlItem = Get-Item -LiteralPath $etlPath
            $state = [pscustomobject]@{
                schema_version = 1
                run_id         = $RunId
                stopped_at_utc = [DateTime]::UtcNow.ToString('o')
                etl_path       = $etlItem.FullName
                etl_size_bytes = $etlItem.Length
                etl_sha256     = (Get-FileHash -LiteralPath $etlPath -Algorithm SHA256).Hash
                stop_marker_succeeded = $stopMarkerSucceeded
                stop_marker_error = $stopMarkerError
                ready_for_export = $stopMarkerSucceeded
            }
            $state | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $stopStatePath -Encoding UTF8 -NoNewline
            if (-not $stopMarkerSucceeded) {
                Write-Warning 'WPR e stato fermato, ma il marker finale e fallito: il run e INCONCLUSIVE.'
            }
            [pscustomobject]@{ action = 'Stop'; run_id = $RunId; etl_path = $etlItem.FullName; state_path = $stopStatePath; ready_for_export = $stopMarkerSucceeded }
        }
    }
}
