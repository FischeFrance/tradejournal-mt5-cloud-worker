#requires -Version 5.1

[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Low')]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('C3', 'C4', 'C5')]
    [string]$Control,

    [Parameter(Mandatory = $true)]
    [string]$RunId,

    [Parameter(Mandatory = $true)]
    [string]$TerminalPath,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Fa-f0-9]{64}$')]
    [string]$TerminalSha256,

    [Parameter(Mandatory = $true)]
    [string]$Endpoint,

    [Parameter(Mandatory = $true)]
    [ValidateRange(1, 65535)]
    [int]$ApprovedPort,

    [string]$EvidenceDirectory = 'C:\TJLab\evidence',

    [string]$OutputPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

Import-Module (Join-Path $PSScriptRoot 'MT5DirectEndpoint.Lab.psm1') -Force
Assert-LabRunId -RunId $RunId

if ($TerminalPath -cnotmatch '^C:\\TJLab\\' -or
    $TerminalPath -notmatch '\\terminal64\.exe$' -or
    $TerminalPath -match '[\r\n"]' -or
    $TerminalPath.Substring(3) -match '\\\\' -or
    $TerminalPath -match '(?:^|\\)\.\.(?:\\|$)') {
    throw 'TerminalPath deve essere canonico sotto C:\TJLab, senza traversal/separatori duplicati, e diretto a terminal64.exe.'
}
if ($EvidenceDirectory -cnotmatch '^C:\\TJLab(?:\\|$)' -or
    $EvidenceDirectory -match '[\r\n"]' -or
    $EvidenceDirectory.Substring(3) -match '\\\\' -or
    $EvidenceDirectory -match '(?:^|\\)\.\.(?:\\|$)') {
    throw 'EvidenceDirectory deve essere canonica sotto C:\TJLab, senza traversal o separatori duplicati.'
}
if (-not [string]::IsNullOrWhiteSpace($OutputPath) -and (
    $OutputPath -cnotmatch '^C:\\TJLab\\' -or
    $OutputPath -match '[\r\n"]' -or
    $OutputPath.Substring(3) -match '\\\\' -or
    $OutputPath -match '(?:^|\\)\.\.(?:\\|$)'
)) {
    throw 'OutputPath deve essere canonico sotto C:\TJLab, senza traversal o separatori duplicati.'
}

$endpointResult = Test-LabEndpoint -Endpoint $Endpoint -AllowedPort @($ApprovedPort)
if (-not $endpointResult.SyntaxValid) {
    throw "Endpoint non valido: $($endpointResult.Reasons -join ', ')"
}
if ($endpointResult.Port -ne $ApprovedPort) {
    throw 'ApprovedPort non coincide con la porta dell endpoint.'
}
if ($endpointResult.HostKind -notin @('IPv4', 'IPv6')) {
    throw 'C3/C4/C5 richiedono un IP letterale. Un hostname non offre un egress deterministico.'
}
if (-not $endpointResult.ServingEligible) {
    throw "Endpoint non network-safe: $($endpointResult.Reasons -join ', ')"
}

$groupName = "MT5DirectEndpointLab_$RunId"
$ruleName = "MT5Lab_${RunId}_${Control}_AllowCandidate"
$firewallBackup = Join-LabPath $EvidenceDirectory ("$RunId.firewall-policy.wfw")
$auditBackup = Join-LabPath $EvidenceDirectory ("$RunId.audit-policy.csv")
$firewallLog = Join-LabPath $EvidenceDirectory ("$RunId.pfirewall.log")
$policyInventory = Join-LabPath $EvidenceDirectory ("$RunId.firewall-policy-before.json")
$isolationIntent = Join-LabPath $EvidenceDirectory ("$RunId.network-isolation-intent.json")
$isolationApplied = Join-LabPath $EvidenceDirectory ("$RunId.network-isolation-applied.json")
$isolationFailed = Join-LabPath $EvidenceDirectory ("$RunId.network-isolation-failed.json")
$isolationRollback = Join-LabPath $EvidenceDirectory ("$RunId.network-isolation-rollback.json")
$connectionAuditGuid = '{0CCE9226-69AE-11D9-BED3-505054503030}'
$packetDropAuditGuid = '{0CCE9225-69AE-11D9-BED3-505054503030}'
$allowCandidate = ($Control -in @('C3', 'C5'))

$steps = New-Object System.Collections.Generic.List[object]
$steps.Add([pscustomobject]@{
    order = 10; id = 'assert_disposable_vm'; tool = 'operator'; mutates_host = $false
    arguments = @(); rationale = 'Confermare snapshot disposable e console di recupero; un eventuale guard esterno resta defense-in-depth non probatorio.'
})
$steps.Add([pscustomobject]@{
    order = 20; id = 'verify_terminal_hash'; tool = 'Get-FileHash'; mutates_host = $false
    arguments = @('-LiteralPath', $TerminalPath, '-Algorithm', 'SHA256')
    expected = $TerminalSha256.ToUpperInvariant(); rationale = 'Vincola la regola al binario attestato; il firewall da solo non verifica l hash.'
})
$steps.Add([pscustomobject]@{
    order = 30; id = 'inventory_effective_policy'; tool = 'PowerShell'; mutates_host = $false
    arguments = @('Get-NetFirewallProfile; Get-NetFirewallRule -PolicyStore ActiveStore')
    rationale = 'Abortire in presenza di GPO non controllabili o regole che non possono essere disabilitate.'
})
$steps.Add([pscustomobject]@{
    order = 40; id = 'backup_firewall'; tool = 'netsh.exe'; mutates_host = $true
    arguments = @('advfirewall', 'export', $firewallBackup)
    rationale = 'Esporta la policy per recovery; il rollback primario resta la distruzione del clone.'
})
$steps.Add([pscustomobject]@{
    order = 50; id = 'backup_audit'; tool = 'auditpol.exe'; mutates_host = $true
    arguments = @('/backup', "/file:$auditBackup")
    rationale = 'Conserva la policy audit precedente.'
})
$steps.Add([pscustomobject]@{
    order = 60; id = 'enable_wfp_connection_audit'; tool = 'auditpol.exe'; mutates_host = $true
    arguments = @('/set', "/subcategory:$connectionAuditGuid", '/success:enable', '/failure:enable')
    rationale = 'Abilita Security 5156 e 5157 per connessioni consentite/bloccate.'
})
$steps.Add([pscustomobject]@{
    order = 70; id = 'enable_wfp_drop_audit'; tool = 'auditpol.exe'; mutates_host = $true
    arguments = @('/set', "/subcategory:$packetDropAuditGuid", '/success:enable', '/failure:enable')
    rationale = 'Abilita Security 5152/5153 come corroborazione dei drop WFP.'
})
$steps.Add([pscustomobject]@{
    order = 80; id = 'disable_existing_outbound_allows'; tool = 'PowerShell'; mutates_host = $true
    arguments = @('Get-NetFirewallRule -PolicyStore ActiveStore -Direction Outbound -Action Allow -Enabled True | Disable-NetFirewallRule')
    rationale = 'Impedisce che allow preesistenti rendano inefficace il default-deny. Fallimento => test inconcludente.'
})
$steps.Add([pscustomobject]@{
    order = 90; id = 'default_deny_outbound'; tool = 'Set-NetFirewallProfile'; mutates_host = $true
    arguments = @('-Profile', 'Domain,Private,Public', '-DefaultOutboundAction', 'Block', '-Enabled', 'True')
    rationale = 'Blocca l egress non autorizzato nel clone disposable.'
})
$steps.Add([pscustomobject]@{
    order = 100; id = 'enable_firewall_log'; tool = 'Set-NetFirewallProfile'; mutates_host = $true
    arguments = @('-Profile', 'Domain,Private,Public', '-LogAllowed', 'True', '-LogBlocked', 'True', '-LogFileName', $firewallLog, '-LogMaxSizeKilobytes', '32767')
    rationale = 'Corroborazione; Security/WFP ed ETW restano le prove primarie.'
})

if ($allowCandidate) {
    $steps.Add([pscustomobject]@{
        order = 110; id = 'allow_candidate_only'; tool = 'New-NetFirewallRule'; mutates_host = $true
        arguments = @(
            '-DisplayName', $ruleName,
            '-Group', $groupName,
            '-Direction', 'Outbound',
            '-Action', 'Allow',
            '-Program', $TerminalPath,
            '-Protocol', 'TCP',
            '-RemoteAddress', $endpointResult.NormalizedHost,
            '-RemotePort', [string]$endpointResult.Port,
            '-Profile', 'Any',
            '-InterfaceType', 'Any',
            '-Enabled', 'True'
        )
        rationale = 'Unica allow Windows per il terminale nel controllo direct-only.'
    })
}
else {
    $steps.Add([pscustomobject]@{
        order = 110; id = 'no_candidate_allow'; tool = 'operator_assertion'; mutates_host = $false
        arguments = @(); rationale = 'C4 non crea alcuna allow. Deve emergere un evento 5157 verso il candidate.'
    })
}

$steps.Add([pscustomobject]@{
    order = 120; id = 'configure_external_guard'; tool = 'hypervisor_or_gateway'; mutates_host = $true
    arguments = if ($allowCandidate) { @('allow', 'tcp', $endpointResult.NormalizedHost, [string]$endpointResult.Port, 'deny', 'all') } else { @('deny', 'all') }
    rationale = 'Difesa operativa addizionale raccomandata; non produce proof binding e non partecipa al verdict C0-C5.'
})
$steps.Add([pscustomobject]@{
    order = 130; id = 'verify_effective_policy'; tool = 'PowerShell'; mutates_host = $false
    arguments = @('Get-NetFirewallProfile; Get-NetFirewallRule -PolicyStore ActiveStore -Enabled True')
    rationale = 'Salvare l inventario e abortire se l enforcement effettivo differisce dal piano.'
})

$rollback = @(
    [pscustomobject]@{ order = 10; tool = 'operator'; arguments = @('disconnect_network'); rationale = 'Isola il clone prima del rollback.' },
    [pscustomobject]@{ order = 20; tool = 'netsh.exe'; arguments = @('advfirewall', 'import', $firewallBackup); rationale = 'Ripristina la policy esportata.' },
    [pscustomobject]@{ order = 30; tool = 'auditpol.exe'; arguments = @('/restore', "/file:$auditBackup"); rationale = 'Ripristina la policy audit.' },
    [pscustomobject]@{ order = 40; tool = 'hypervisor_or_gateway'; arguments = @('deny', 'all'); rationale = 'Rimuove ogni egress del clone.' },
    [pscustomobject]@{ order = 50; tool = 'operator'; arguments = @('destroy_clone_and_disk_key'); rationale = 'Rollback primario e distruzione degli artefatti sensibili.' }
)

$plan = [ordered]@{
    schema_version             = 1
    document_type              = 'windows_firewall_wfp_plan'
    generated_at_utc           = [DateTime]::UtcNow.ToString('o')
    run_id                     = $RunId
    control                    = $Control
    apply_capability_in_script = $false
    current_host_forbidden     = $true
    disposable_vm_only         = $true
    endpoint                   = $endpointResult
    terminal                   = [ordered]@{ path = $TerminalPath; sha256 = $TerminalSha256.ToUpperInvariant() }
    artifacts                  = [ordered]@{
        firewall_backup  = $firewallBackup
        audit_backup     = $auditBackup
        firewall_log     = $firewallLog
        policy_inventory = $policyInventory
        isolation_intent = $isolationIntent
        isolation_applied = $isolationApplied
        isolation_failed = $isolationFailed
        isolation_rollback = $isolationRollback
    }
    required_security_events   = if ($allowCandidate) { @(5156) } else { @(5157) }
    corroborating_events       = if ($allowCandidate) { @() } else { @(5152, 5153) }
    hard_preconditions         = @(
        'VM clone disposable con snapshot e console out-of-band',
        'nessuna credenziale nella command line o nei log',
        'IP e porta coincidono con il candidate approvato',
        'hash Authenticode/SHA-256 del terminale verificato',
        'se previsto dalla procedura operativa, il guard gateway/hypervisor resta defense-in-depth non probatorio',
        'i percorsi di backup/evidenza non esistono e il volume e cifrato',
        'policy non controllabile o event loss rendono il test INCONCLUSIVE'
    )
    steps                      = @($steps.ToArray())
    rollback                   = @($rollback)
}

if (-not [string]::IsNullOrWhiteSpace($OutputPath)) {
    $parent = Split-Path -Path $OutputPath -Parent
    if ([string]::IsNullOrWhiteSpace($parent) -or -not (Test-Path -LiteralPath $parent -PathType Container)) {
        throw 'La directory di OutputPath deve esistere; lo script non la crea.'
    }
    if (Test-Path -LiteralPath $OutputPath) {
        throw 'OutputPath esiste gia; sovrascrittura rifiutata.'
    }
    if ($PSCmdlet.ShouldProcess($OutputPath, 'Scrivere il piano JSON (nessuna modifica firewall)')) {
        $json = $plan | ConvertTo-Json -Depth 12
        $encoding = New-Object Text.UTF8Encoding($false)
        $stream = $null
        $writer = $null
        try {
            $stream = New-Object IO.FileStream(
                $OutputPath,
                [IO.FileMode]::CreateNew,
                [IO.FileAccess]::Write,
                [IO.FileShare]::Read
            )
            $writer = New-Object IO.StreamWriter($stream, $encoding)
            $writer.Write($json)
            $writer.Flush()
            $stream.Flush($true)
        }
        finally {
            if ($null -ne $writer) { $writer.Dispose() }
            elseif ($null -ne $stream) { $stream.Dispose() }
        }
    }
}

[pscustomobject]$plan
