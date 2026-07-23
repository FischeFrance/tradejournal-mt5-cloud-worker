#requires -Version 5.1

[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$windowsRoot = Split-Path $PSScriptRoot -Parent
Import-Module (Join-Path $windowsRoot 'MT5DirectEndpoint.Lab.psm1') -Force

$failures = New-Object System.Collections.Generic.List[string]
$assertionCount = 0

function Assert-True {
    param([bool]$Condition, [string]$Message)
    $script:assertionCount++
    if (-not $Condition) {
        $script:failures.Add($Message)
    }
}

function Assert-Equal {
    param($Expected, $Actual, [string]$Message)
    $script:assertionCount++
    if ($Expected -ne $Actual) {
        $script:failures.Add("$Message (expected=$Expected actual=$Actual)")
    }
}

$publicV4 = Test-LabEndpoint -Endpoint '8.8.8.8:443' -AllowedPort 443
Assert-Equal 'SAFE' $publicV4.SafetyStatus 'IPv4 pubblico con porta approvata deve essere SAFE'
Assert-True $publicV4.ServingEligible 'IPv4 pubblico deve essere serving eligible'

$privateV4 = Test-LabEndpoint -Endpoint '10.1.2.3:443' -AllowedPort 443
Assert-Equal 'UNSAFE' $privateV4.SafetyStatus 'RFC1918 deve essere rifiutato'
Assert-True ($privateV4.Reasons -contains 'IPV4_PRIVATE') 'RFC1918 deve avere reason esplicita'

$loopbackV4 = Test-LabEndpoint -Endpoint '127.0.0.1:443' -AllowedPort 443
Assert-True (-not $loopbackV4.ServingEligible) 'Loopback IPv4 deve essere rifiutato'

$publicV6 = Test-LabEndpoint -Endpoint '[2606:4700:4700::1111]:443' -AllowedPort 443
Assert-Equal 'SAFE' $publicV6.SafetyStatus 'IPv6 globale bracketed deve essere SAFE'

$localV6 = Test-LabEndpoint -Endpoint '[fe80::1]:443' -AllowedPort 443
Assert-True ($localV6.Reasons -contains 'IPV6_LINK_LOCAL') 'IPv6 link-local deve essere rifiutato'

$mappedV6 = Test-LabEndpoint -Endpoint '[::ffff:127.0.0.1]:443' -AllowedPort 443
Assert-True ($mappedV6.Reasons -contains 'IPV4_MAPPED_IPV6_NOT_ALLOWED') 'IPv4-mapped IPv6 deve essere rifiutato'

$dnsNoEvidence = Test-LabEndpoint -Endpoint 'one.one.one.one:443' -AllowedPort 443
Assert-Equal 'REQUIRES_DNS_EVIDENCE' $dnsNoEvidence.SafetyStatus 'DNS senza RRset deve restare non eleggibile'

$dnsMixed = Test-LabEndpoint -Endpoint 'one.one.one.one:443' -AllowedPort 443 -ResolvedAddress @('8.8.8.8', '10.0.0.1')
Assert-Equal 'UNSAFE' $dnsMixed.SafetyStatus 'Basta un indirizzo DNS privato per rifiutare l intero RRset'

$unapprovedPort = Test-LabEndpoint -Endpoint '8.8.8.8:444' -AllowedPort 443
Assert-Equal 'REQUIRES_PORT_APPROVAL' $unapprovedPort.SafetyStatus 'Porte non approvate non devono essere eleggibili'

$nonCanonicalPort = Test-LabEndpoint -Endpoint '8.8.8.8:0443' -AllowedPort 443
Assert-True (-not $nonCanonicalPort.SyntaxValid) 'Porta con zero iniziale deve essere rifiutata come ambigua'

$invalidDnsDots = Test-LabEndpoint -Endpoint 'one.one.one.one..:443' -AllowedPort 443 -ResolvedAddress '8.8.8.8'
Assert-True (-not $invalidDnsDots.SyntaxValid) 'DNS con piu trailing dot deve essere rifiutato'

Assert-True (Test-LabRunId -RunId 'C3_run-001') 'RunId sicuro deve essere accettato'
Assert-True (-not (Test-LabRunId -RunId '..\escape')) 'Path traversal nel RunId deve essere rifiutato'

$wprPlan = & (Join-Path $windowsRoot 'Invoke-LabWprCapture.ps1') -RunId 'dryrun001'
Assert-Equal 'PlanOnly' $wprPlan.mode 'WPR deve essere PlanOnly per default'
Assert-True (-not $wprPlan.mutates_host) 'Il piano WPR non deve mutare l host'

$phasePlan = & (Join-Path $windowsRoot 'Write-LabPhaseMarker.ps1') -RunId 'dryrun001' -Phase 'C0_BASELINE' -Boundary 'START'
Assert-Equal 'PlanOnly' $phasePlan.mode 'Marker deve essere PlanOnly per default'
Assert-Equal 2 $phasePlan.schema_version 'Piano marker deve esporre il contratto Patch 6 v2'
Assert-Equal 2 $phasePlan.marker_schema_version 'Record marker deve essere v2'
Assert-Equal 'C0_BASELINE_START' $phasePlan.code 'Marker deve esporre il code strutturato'
Assert-Equal 'CONTROL_EXACT_CONTIGUOUS_V1' $phasePlan.sequence_policy 'Marker deve dichiarare sequenza esatta e contigua'
Assert-True ($phasePlan.marker_text -cmatch '^MT5LAB\|v2\|run=dryrun001\|code=C0_BASELINE_START\|sequence=<runtime>\|timestamp_unix_ms=<runtime>\|qpc=<runtime>\|qpc_frequency_hz=\d+$') 'Template marker v2 deve includere sequence, UTC millisecondi e QPC'

$patch6Phases = @(
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
)
foreach ($patch6Phase in $patch6Phases) {
    $patch6PhasePlan = & (Join-Path $windowsRoot 'Write-LabPhaseMarker.ps1') `
        -RunId 'dryrun001' `
        -Phase $patch6Phase `
        -Boundary 'START'
    Assert-Equal "${patch6Phase}_START" $patch6PhasePlan.code "Marker PlanOnly deve accettare la fase Patch 6 $patch6Phase"
}

$hash = ('A' * 64)
$firewallC3 = & (Join-Path $windowsRoot 'New-LabFirewallPlan.ps1') `
    -Control C3 `
    -RunId dryrun001 `
    -TerminalPath 'C:\TJLab\dryrun001\terminal\terminal64.exe' `
    -TerminalSha256 $hash `
    -Endpoint '8.8.8.8:443' `
    -ApprovedPort 443
Assert-True (-not $firewallC3.apply_capability_in_script) 'Il planner firewall non deve avere capacita di apply'
Assert-True (@($firewallC3.steps | Where-Object { $_.id -eq 'allow_candidate_only' }).Count -eq 1) 'C3 deve contenere una allow candidate nel piano'

$firewallC4 = & (Join-Path $windowsRoot 'New-LabFirewallPlan.ps1') `
    -Control C4 `
    -RunId dryrun002 `
    -TerminalPath 'C:\TJLab\dryrun002\terminal\terminal64.exe' `
    -TerminalSha256 $hash `
    -Endpoint '8.8.8.8:443' `
    -ApprovedPort 443
Assert-True (@($firewallC4.steps | Where-Object { $_.id -eq 'allow_candidate_only' }).Count -eq 0) 'C4 non deve contenere allow candidate'

foreach ($unsafePlannerPathCase in @(
    [pscustomobject]@{ terminal = 'D:\terminal\terminal64.exe'; evidence = 'C:\TJLab\evidence'; output = $null },
    [pscustomobject]@{ terminal = 'C:\TJLab\terminal\terminal64.exe'; evidence = 'D:\evidence'; output = $null },
    [pscustomobject]@{ terminal = 'C:\TJLab\terminal\terminal64.exe'; evidence = 'C:\TJLab\evidence'; output = 'D:\plan.json' },
    [pscustomobject]@{ terminal = 'C:\TJLab\\terminal\terminal64.exe'; evidence = 'C:\TJLab\evidence'; output = $null }
)) {
    $unsafePlannerPathRejected = $false
    try {
        & (Join-Path $windowsRoot 'New-LabFirewallPlan.ps1') `
            -Control C3 `
            -RunId dryrun_path_reject `
            -TerminalPath $unsafePlannerPathCase.terminal `
            -TerminalSha256 $hash `
            -Endpoint '8.8.8.8:443' `
            -ApprovedPort 443 `
            -EvidenceDirectory $unsafePlannerPathCase.evidence `
            -OutputPath $unsafePlannerPathCase.output | Out-Null
    }
    catch {
        $unsafePlannerPathRejected = ($_.Exception.Message -match 'sotto C:\\TJLab')
    }
    Assert-True $unsafePlannerPathRejected "Planner deve rifiutare path fuori/non canonici: $($unsafePlannerPathCase | ConvertTo-Json -Compress)"
}

$exportPlan = & (Join-Path $windowsRoot 'Export-LabEtwEvidence.ps1') `
    -RunId dryrun001 `
    -InputEtlPath 'C:\TJLab\evidence\dryrun001.etl' `
    -OutputDirectory 'C:\TJLab\evidence'
Assert-Equal 'PlanOnly' $exportPlan.mode 'Exporter ETL deve essere PlanOnly per default'
Assert-True (-not $exportPlan.opens_network) 'Exporter ETL non deve aprire rete'
Assert-Equal 'NO_GO' $exportPlan.readiness 'Exporter ETL deve dichiarare NO-GO senza binding JobHarness forte'
Assert-True (-not $exportPlan.proof_capable) 'Exporter ETL non deve presentare i bare PID come prova process-scoped'
Assert-True $exportPlan.exploratory_dataset_only 'Exporter ETL puo produrre al massimo un dataset esplorativo'
Assert-Equal 2 $exportPlan.schema_version 'Piano exporter ETL deve essere v2'
Assert-Equal 2 $exportPlan.marker_schema_version 'Exporter deve richiedere marker v2'
Assert-Equal 2 $exportPlan.sanitized_event_schema_version 'Exporter deve produrre sanitized-event v2'
Assert-Equal 'HASH_SAFE_PROVIDER_CONNECTION_ID_OR_NULL' $exportPlan.connection_id_policy 'Exporter deve dichiarare la policy connection-id fail-closed'

$wfpSecurityExportPlan = & (Join-Path $windowsRoot 'Export-LabWfpSecurityEvidence.ps1') `
    -RunId dryrun001 `
    -Control C4 `
    -MarkerLogPath 'C:\TJLab\evidence\phase-markers.jsonl' `
    -MarkerLogSha256 $hash `
    -IsolationAppliedStatePath 'C:\TJLab\evidence\dryrun001.network-isolation-applied.json' `
    -IsolationAppliedStateSha256 $hash `
    -TerminalPath 'C:\TJLab\dryrun001\terminal\terminal64.exe' `
    -TerminalSha256 $hash `
    -TargetProcessId 4242 `
    -Endpoint '8.8.8.8:443' `
    -ApprovedPort 443 `
    -OutputDirectory 'C:\TJLab\evidence'
Assert-Equal 'PlanOnly' $wfpSecurityExportPlan.mode 'Exporter Security/WFP deve essere PlanOnly per default'
Assert-True (-not $wfpSecurityExportPlan.executed) 'Exporter Security/WFP PlanOnly non deve leggere Security'
Assert-True (-not $wfpSecurityExportPlan.raw_security_log_exported) 'Exporter WFP non deve mai esportare Security.evtx'
Assert-Equal 5157 $wfpSecurityExportPlan.required_event_id 'C4 deve richiedere 5157'
Assert-True (-not $wfpSecurityExportPlan.opens_network) 'Exporter Security/WFP non deve aprire rete'
Assert-Equal 'HARD_DISABLED' $wfpSecurityExportPlan.execute_capability 'Execute Security/WFP deve restare bloccato finche mancano i binding forti'
Assert-Equal 'NO_GO' $wfpSecurityExportPlan.readiness 'Exporter WFP deve dichiarare NO-GO finche Execute e hard-disabled'
Assert-True (-not $wfpSecurityExportPlan.proof_capable) 'Exporter WFP PlanOnly non deve presentarsi come proof-capable'
$wfpExecuteHardDisabled = $false
try {
    & (Join-Path $windowsRoot 'Export-LabWfpSecurityEvidence.ps1') `
        -RunId dryrun001 `
        -Control C4 `
        -MarkerLogPath 'C:\TJLab\evidence\phase-markers.jsonl' `
        -MarkerLogSha256 $hash `
        -IsolationAppliedStatePath 'C:\TJLab\evidence\dryrun001.network-isolation-applied.json' `
        -IsolationAppliedStateSha256 $hash `
        -TerminalPath 'C:\TJLab\dryrun001\terminal\terminal64.exe' `
        -TerminalSha256 $hash `
        -TargetProcessId 4242 `
        -Endpoint '8.8.8.8:443' `
        -ApprovedPort 443 `
        -OutputDirectory 'C:\TJLab\evidence' `
        -Mode Execute | Out-Null
}
catch {
    $wfpExecuteHardDisabled = ($_.Exception.Message -match 'CAPABILITY_HARD_DISABLED')
}
Assert-True $wfpExecuteHardDisabled 'Execute WFP deve fermarsi prima di leggere Security o scrivere output'

$prereqPlan = & (Join-Path $windowsRoot 'Test-LabPrerequisites.ps1')
Assert-Equal 'PlanOnly' $prereqPlan.mode 'Prerequisiti devono essere PlanOnly per default'

$bootstrapExperimentId = '11111111-1111-4111-8111-111111111111'
$bootstrapRunId = '22222222-2222-4222-8222-222222222222'
$bootstrapPlan = & (Join-Path $windowsRoot 'Invoke-LabPrivateBootstrap.ps1') `
    -ExperimentId $bootstrapExperimentId `
    -RunId $bootstrapRunId `
    -Control C3 `
    -PrivateDirectory "C:\TJLab\$bootstrapExperimentId\runs\$bootstrapRunId\C3\private" `
    -PortableRoot "C:\TJLab\$bootstrapExperimentId\C3\terminal"
Assert-Equal 'PlanOnly' $bootstrapPlan.mode 'Bootstrap privato deve essere PlanOnly per default'
Assert-Equal 'NO_GO' $bootstrapPlan.readiness 'Bootstrap privato deve dichiarare NO-GO'
Assert-True (-not $bootstrapPlan.executed) 'Bootstrap PlanOnly non deve eseguire operazioni'
Assert-True (-not $bootstrapPlan.accesses_credentials) 'Bootstrap PlanOnly non deve accedere a credenziali'
Assert-True (-not $bootstrapPlan.writes_private_files) 'Bootstrap PlanOnly non deve scrivere file privati'
Assert-True (-not $bootstrapPlan.deletes_private_files) 'Bootstrap PlanOnly non deve cancellare file privati'
Assert-Equal 'HARD_DISABLED' $bootstrapPlan.create_capability 'Create bootstrap deve essere hard-disabled'
Assert-Equal 'HARD_DISABLED' $bootstrapPlan.remove_capability 'Remove bootstrap deve essere hard-disabled'

$fixtureRoot = Join-Path $PSScriptRoot 'fixtures'
$networkPlanFixture = Join-Path $fixtureRoot 'network-isolation-plan.c3.json'
$networkPlanHash = (Get-FileHash -LiteralPath $networkPlanFixture -Algorithm SHA256).Hash
$networkIsolationPlan = & (Join-Path $windowsRoot 'Invoke-LabNetworkIsolation.ps1') `
    -PlanPath $networkPlanFixture `
    -PlanSha256 $networkPlanHash
Assert-Equal 'PlanOnly' $networkIsolationPlan.mode 'Executor isolamento deve essere PlanOnly per default'
Assert-True (-not $networkIsolationPlan.executed) 'PlanOnly non deve eseguire capability attive'
Assert-True (-not $networkIsolationPlan.mutates_host) 'PlanOnly isolamento non deve mutare l host'
Assert-Equal 'NO_GO' $networkIsolationPlan.readiness 'Executor firewall deve dichiarare NO-GO finche i mutatori sono hard-disabled'
Assert-True (-not $networkIsolationPlan.proof_capable) 'PlanOnly firewall non deve presentarsi come proof-capable'
Assert-True ($networkIsolationPlan.hard_disabled_capabilities -contains 'Apply') 'Apply deve essere hard-disabled senza verifier esterno firmato'
Assert-True ($networkIsolationPlan.hard_disabled_capabilities -contains 'Rollback') 'Rollback deve essere hard-disabled senza verifier esterno firmato'

foreach ($hardDisabledMode in @('Apply', 'Rollback')) {
    $hardDisableObserved = $false
    try {
        & (Join-Path $windowsRoot 'Invoke-LabNetworkIsolation.ps1') `
            -Mode $hardDisabledMode `
            -PlanPath $networkPlanFixture `
            -PlanSha256 $networkPlanHash | Out-Null
    }
    catch {
        $hardDisableObserved = ($_.Exception.Message -match 'CAPABILITY_HARD_DISABLED')
    }
    Assert-True $hardDisableObserved "$hardDisabledMode deve fermarsi prima di qualunque gate o mutazione host"
}

$digestRejected = $false
try {
    & (Join-Path $windowsRoot 'Invoke-LabNetworkIsolation.ps1') `
        -PlanPath $networkPlanFixture `
        -PlanSha256 ('0' * 64) | Out-Null
}
catch {
    $digestRejected = ($_.Exception.Message -match 'Digest SHA-256')
}
Assert-True $digestRejected 'Executor deve rifiutare un digest piano non corrispondente'

$unexpectedFixture = Join-Path $fixtureRoot 'network-isolation-plan.unexpected-field.json'
$unexpectedHash = (Get-FileHash -LiteralPath $unexpectedFixture -Algorithm SHA256).Hash
$unexpectedRejected = $false
try {
    & (Join-Path $windowsRoot 'Invoke-LabNetworkIsolation.ps1') `
        -PlanPath $unexpectedFixture `
        -PlanSha256 $unexpectedHash | Out-Null
}
catch {
    $unexpectedRejected = ($_.Exception.Message -match 'Proprieta inattese')
}
Assert-True $unexpectedRejected 'Executor deve rifiutare campi JSON inattesi'

$duplicateArtifactFixture = Join-Path $fixtureRoot 'network-isolation-plan.duplicate-artifact.json'
$duplicateArtifactHash = (Get-FileHash -LiteralPath $duplicateArtifactFixture -Algorithm SHA256).Hash
$duplicateArtifactRejected = $false
try {
    & (Join-Path $windowsRoot 'Invoke-LabNetworkIsolation.ps1') `
        -PlanPath $duplicateArtifactFixture `
        -PlanSha256 $duplicateArtifactHash | Out-Null
}
catch {
    $duplicateArtifactRejected = ($_.Exception.Message -match 'artifact path devono essere distinti')
}
Assert-True $duplicateArtifactRejected 'Executor deve rifiutare artifact path duplicati anche con case-insensitive Windows'

$bomFixture = Join-Path $fixtureRoot 'network-isolation-plan.utf8-bom.json'
$bomHash = (Get-FileHash -LiteralPath $bomFixture -Algorithm SHA256).Hash
$bomRejected = $false
try {
    & (Join-Path $windowsRoot 'Invoke-LabNetworkIsolation.ps1') `
        -PlanPath $bomFixture `
        -PlanSha256 $bomHash | Out-Null
}
catch {
    $bomRejected = ($_.Exception.Message -match 'BOM UTF-8 non ammesso')
}
Assert-True $bomRejected 'Reader ed executor devono rifiutare esplicitamente piani UTF-8 con BOM'

# Il controllo AST impedisce regressioni nei file production: possono descrivere
# comandi firewall/WFP, ma i CommandAst dinamici devono coincidere con la piccola
# allowlist dei wrapper di tool trusted. Nessuno script puo avviare MT5 o aprire
# rete; i test sono esclusi soltanto dal confronto delle invocazioni dinamiche.
$forbiddenCommands = @(
    'New-NetFirewallRule', 'Set-NetFirewallRule', 'Remove-NetFirewallRule',
    'Enable-NetFirewallRule', 'Disable-NetFirewallRule', 'Set-NetFirewallProfile',
    'netsh.exe', 'netsh', 'auditpol.exe', 'auditpol',
    'Get-WinEvent', 'wevtutil.exe', 'wevtutil',
    'Resolve-DnsName', 'Test-NetConnection', 'Invoke-WebRequest', 'Invoke-RestMethod',
    'Set-NetFirewallSetting', 'Set-NetIPInterface', 'Set-NetAdapter',
    'curl.exe', 'curl', 'terminal64.exe', 'terminal.exe', 'MetaEditor64.exe',
    'Invoke-Expression', 'Start-Process', 'Start-Job', 'schtasks.exe', 'schtasks',
    'cmd.exe', 'powershell.exe', 'pwsh.exe', 'Add-Type'
)
$executorAllowedCommands = @(
    'Disable-NetFirewallRule',
    'Set-NetFirewallProfile',
    'New-NetFirewallRule',
    'Remove-NetFirewallRule'
)
$executorCommandsFound = New-Object System.Collections.Generic.List[string]
$allowedAddTypeFiles = @('MT5DirectEndpoint.Lab.psm1', 'Export-LabWfpSecurityEvidence.ps1')
$addTypeFilesFound = New-Object System.Collections.Generic.List[string]
$allowedDynamicProductionCommands = @{
    'Export-LabEtwEvidence.ps1' = @('& $tracerptPath @tracerptArguments 2>&1')
    'Invoke-LabNetworkIsolation.ps1' = @('& $toolPath @Arguments 2>&1')
    'Invoke-LabWprCapture.ps1' = @('& $WprPath @Arguments 2>&1')
    'Write-LabPhaseMarker.ps1' = @("& `$wprPath '-marker' `$markerText '-flush' 2>&1")
}
$dynamicProductionCommandsFound = New-Object System.Collections.Generic.List[string]
$testRootFull = (Resolve-Path -LiteralPath $PSScriptRoot).Path.TrimEnd([IO.Path]::DirectorySeparatorChar)
$scripts = @(Get-ChildItem -LiteralPath $windowsRoot -File -Recurse | Where-Object { $_.Extension -in @('.ps1', '.psm1') })
foreach ($scriptFile in $scripts) {
    $tokens = $null
    $parseErrors = $null
    $ast = [Management.Automation.Language.Parser]::ParseFile($scriptFile.FullName, [ref]$tokens, [ref]$parseErrors)
    Assert-True ($parseErrors.Count -eq 0) "PowerShell parse error in $($scriptFile.Name): $($parseErrors -join '; ')"
    $commands = @($ast.FindAll({ param($node) $node -is [Management.Automation.Language.CommandAst] }, $true))
    foreach ($command in $commands) {
        $commandName = $command.GetCommandName()
        if ([string]::IsNullOrWhiteSpace($commandName)) {
            $scriptDirectory = $scriptFile.Directory.FullName
            $isTestScript = (
                $scriptDirectory.Equals($testRootFull, [StringComparison]::OrdinalIgnoreCase) -or
                $scriptDirectory.StartsWith($testRootFull + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)
            )
            if (-not $isTestScript) {
                $extent = $command.Extent.Text.Trim()
                $allowedExtents = @($allowedDynamicProductionCommands[$scriptFile.Name])
                $dynamicAllowed = ($allowedExtents.Count -gt 0 -and $allowedExtents -ccontains $extent)
                Assert-True $dynamicAllowed "CommandAst dinamico non allowlisted in $($scriptFile.Name): $extent"
                if ($dynamicAllowed) {
                    $identity = "$($scriptFile.Name)::$extent"
                    if (-not $dynamicProductionCommandsFound.Contains($identity)) {
                        $dynamicProductionCommandsFound.Add($identity)
                    }
                }
            }
            continue
        }

        $isExecutorException = (
            $scriptFile.Name -ceq 'Invoke-LabNetworkIsolation.ps1' -and
            $executorAllowedCommands -contains $commandName
        )
        $isAddTypeException = ($commandName -ceq 'Add-Type' -and $allowedAddTypeFiles -ccontains $scriptFile.Name)
        if ($isExecutorException) {
            if (-not $executorCommandsFound.Contains($commandName)) {
                $executorCommandsFound.Add($commandName)
            }
        }
        elseif ($isAddTypeException) {
            Assert-True ($command.CommandElements.Count -ge 3 -and $command.Extent.Text -match 'Add-Type\s+-TypeDefinition\s+@''') "Add-Type deve usare soltanto TypeDefinition inline revisionabile in $($scriptFile.Name)"
            if (-not $addTypeFilesFound.Contains($scriptFile.Name)) {
                $addTypeFilesFound.Add($scriptFile.Name)
            }
        }
        else {
            Assert-True ($forbiddenCommands -notcontains $commandName) "Comando vietato realmente invocabile in $($scriptFile.Name): $commandName"
        }
    }
}
Assert-Equal (($executorAllowedCommands | Sort-Object) -join ',') (($executorCommandsFound | Sort-Object) -join ',') 'Executor deve contenere solo il set minimo di mutatori firewall approvato'
Assert-Equal (($allowedAddTypeFiles | Sort-Object) -join ',') (($addTypeFilesFound | Sort-Object) -join ',') 'Add-Type production deve apparire soltanto nei due wrapper P/Invoke revisionati'
$expectedDynamicProductionCommands = @(
    foreach ($fileName in $allowedDynamicProductionCommands.Keys) {
        foreach ($extent in $allowedDynamicProductionCommands[$fileName]) {
            "${fileName}::$extent"
        }
    }
)
Assert-Equal (($expectedDynamicProductionCommands | Sort-Object) -join "`n") (($dynamicProductionCommandsFound | Sort-Object) -join "`n") 'Ogni CommandAst dinamico production deve coincidere esattamente con l allowlist minima'

$moduleSource = Get-Content -LiteralPath (Join-Path $windowsRoot 'MT5DirectEndpoint.Lab.psm1') -Raw
Assert-True ($moduleSource -notmatch 'Marshal\.ReadIntPtr\s*\(\s*buffer\s*\)') 'AuditQuerySystemPolicy non deve dereferenziare una seconda volta ppAuditPolicy'
Assert-True ($moduleSource -match 'Marshal\.PtrToStructure\s*\(\s*\r?\n?\s*buffer\s*,') 'AuditQuerySystemPolicy deve mappare direttamente il buffer restituito'
Assert-True ($moduleSource -match 'AuditFree\s*\(\s*buffer\s*\)') 'Il buffer AuditQuerySystemPolicy deve essere liberato con AuditFree'
Assert-True ($moduleSource -match 'extern\s+void\s+AuditFree\s*\(') 'La firma P/Invoke AuditFree deve rispettare il ritorno VOID nativo'
Assert-True ($moduleSource -notmatch '!\s*AuditFree\s*\(') 'Il ritorno VOID di AuditFree non deve essere interpretato come booleano'

$firewallPlannerSource = Get-Content -LiteralPath (Join-Path $windowsRoot 'New-LabFirewallPlan.ps1') -Raw
Assert-True ($firewallPlannerSource -notmatch 'Set-Content[^\r\n]*-Encoding\s+UTF8') 'Il planner non deve produrre BOM UTF-8 su Windows PowerShell 5.1'
Assert-True ($firewallPlannerSource -match 'UTF8Encoding\s*\(\s*\$false\s*\)') 'Il planner deve usare UTF-8 esplicitamente senza BOM'
Assert-True ($firewallPlannerSource -match '\[IO\.FileMode\]::CreateNew') 'Il planner deve creare il piano in modo non-overwrite/race-safe'

$networkExecutorSource = Get-Content -LiteralPath (Join-Path $windowsRoot 'Invoke-LabNetworkIsolation.ps1') -Raw
foreach ($requiredVerifyOnlyFailClosedField in @(
    "readiness                      = 'NO_GO'",
    'proof_capable                  = $false',
    'external_authorization_verified = $false',
    'firewall_policy_verified       = $false'
)) {
    Assert-True ($networkExecutorSource.Contains($requiredVerifyOnlyFailClosedField)) "VerifyOnly deve includere il marker fail-closed: $requiredVerifyOnlyFailClosedField"
}
Assert-True ($networkExecutorSource -match '\$remoteAddresses\.Count\s+-eq\s+1') 'VerifyOnly deve richiedere una sola RemoteAddress'
Assert-True ($networkExecutorSource -match '\$remotePorts\.Count\s+-eq\s+1') 'VerifyOnly deve richiedere una sola RemotePort'
Assert-True ($networkExecutorSource -match '\$protocols\.Count\s+-eq\s+1') 'VerifyOnly deve richiedere un solo Protocol'
Assert-True ($networkExecutorSource -match '\$programs\.Count\s+-eq\s+1') 'VerifyOnly deve richiedere un solo Program'

if ($failures.Count -gt 0) {
    throw "Self-test fallito ($($failures.Count)/$assertionCount): $($failures -join ' | ')"
}

[pscustomobject]@{
    status     = 'PASS'
    assertions = $assertionCount
    mutations  = 0
    network    = 0
}
