#requires -Version 5.1

<#
.SYNOPSIS
    Descrive il bootstrap privato MT5 senza accedere a credenziali.

.DESCRIPTION
    In questa revisione soltanto PlanOnly e disponibile. Create e Remove sono
    hard-disabled prima di qualunque controllo host, lettura credenziale o
    scrittura/cancellazione. Il file rende esplicito il contratto e i blocker
    che un futuro injector revisionato dovra soddisfare.
#>
[CmdletBinding()]
param(
    [ValidateSet('PlanOnly', 'Create', 'Remove')]
    [string]$Mode = 'PlanOnly',

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$')]
    [string]$ExperimentId,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$')]
    [string]$RunId,

    [Parameter(Mandatory = $true)]
    [ValidateSet('C2', 'C3', 'C4', 'C5')]
    [string]$Control,

    [Parameter(Mandatory = $true)]
    [string]$PrivateDirectory,

    [Parameter(Mandatory = $true)]
    [string]$PortableRoot
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

Import-Module (Join-Path $PSScriptRoot 'MT5DirectEndpoint.Lab.psm1') -Force

$experimentCanonical = ([Guid]$ExperimentId).ToString('D')
$runCanonical = ([Guid]$RunId).ToString('D')
$cohort = if ($Control -eq 'C2') { 'C012' } else { $Control }
$expectedDirectory = "C:\TJLab\$experimentCanonical\runs\$runCanonical\$Control\private"
$expectedPortableRoot = "C:\TJLab\$experimentCanonical\$cohort\terminal"
$probeDirectory = Join-LabPath $expectedPortableRoot 'MQL5\Files\MT5DirectEndpointLab'
$configPath = Join-LabPath $expectedDirectory 'startup.ini'
$expectedAccountPath = Join-LabPath $probeDirectory 'expected-account.txt'

if ($PrivateDirectory -cne $expectedDirectory -or $PortableRoot -cne $expectedPortableRoot) {
    throw 'I path devono coincidere esattamente con il layout canonico experiment/run/control.'
}

if ($Mode -eq 'PlanOnly') {
    [pscustomobject]@{
        schema_version               = 1
        mode                         = 'PlanOnly'
        executed                     = $false
        accesses_credentials         = $false
        writes_private_files         = $false
        deletes_private_files        = $false
        experiment_id                = $experimentCanonical
        run_id                       = $runCanonical
        control                      = $Control
        private_directory            = $expectedDirectory
        portable_root                = $expectedPortableRoot
        future_config_path           = $configPath
        future_expected_account_path = $expectedAccountPath
        command_line_contains_secret = $false
        environment_contains_secret  = $false
        secret_digest_emitted        = $false
        readiness                    = 'NO_GO'
        proof_capable                = $false
        create_capability            = 'HARD_DISABLED'
        remove_capability            = 'HARD_DISABLED'
        blockers                     = @(
            'attestazione firmata del clone e del volume cifrato assente',
            'creazione atomica delle directory con security descriptor non implementata',
            'writer plaintext senza buffer managed residui non implementato',
            'cleanup handle-based/file-ID resistente a reparse e race non implementato',
            'runtime Windows e distruzione del clone non validati end-to-end'
        )
        required_private_settings    = @(
            'ProxyEnable=0',
            'KeepPrivate=0',
            'AllowLiveTrading=0',
            'AllowDllImport=0',
            'Expert=TradeJournal\TradeJournalIdentityProbe'
        )
    }
    return
}

throw "CAPABILITY_HARD_DISABLED: $Mode non e disponibile. Nessuna credenziale e stata letta e nessun file e stato scritto o cancellato."
