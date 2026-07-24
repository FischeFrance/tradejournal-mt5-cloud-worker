#requires -Version 5.1

[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Assert-True {
    param([Parameter(Mandatory = $true)][bool]$Condition, [Parameter(Mandatory = $true)][string]$Message)
    if (-not $Condition) { throw ('ASSERTION_FAILED_' + $Message) }
}

$scanner = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\Get-LabPreState.ps1'))
$scannerArguments = @(
    '-NoProfile',
    '-ExecutionPolicy', 'Bypass',
    '-File', $scanner,
    '-Mode', 'PlanOnly',
    '-PortableRoot', 'C:\TJLab\DRYRUN_ONLY\terminal'
)

# Get-LabPreState writes its CLI JSON directly to Console.Out. Execute it as a
# child Windows PowerShell process so stdout can be captured exactly as an
# external caller would observe it, without changing the scanner's output
# contract or mixing PowerShell success-stream objects into the JSON.
$rawLines = & powershell.exe @scannerArguments
$scannerExitCode = $LASTEXITCODE
if ($scannerExitCode -ne 0) {
    throw ('SCANNER_PROCESS_FAILED_' + $scannerExitCode)
}

$raw = [string]::Join([Environment]::NewLine, [string[]]@($rawLines))
if ([string]::IsNullOrWhiteSpace($raw)) {
    throw 'SCANNER_STDOUT_EMPTY'
}
$report = $raw | ConvertFrom-Json

Assert-True ($report.schema_version -eq 1) 'SCHEMA_VERSION'
Assert-True ($report.mode -ceq 'PLANONLY') 'MODE'
Assert-True ($report.overall_status -ceq 'UNKNOWN') 'OVERALL_UNKNOWN'
Assert-True ($report.check_count -eq 16) 'CHECK_COUNT'
Assert-True ($report.pass_count -eq 0) 'PASS_COUNT'
Assert-True ($report.fail_count -eq 0) 'FAIL_COUNT'
Assert-True ($report.unknown_count -eq 16) 'UNKNOWN_COUNT'
Assert-True (-not $report.network_operations_performed) 'NO_NETWORK'
Assert-True (-not $report.system_mutations_performed) 'NO_MUTATIONS'
Assert-True (-not $report.output_requested) 'NO_OUTPUT_REQUESTED'
Assert-True (-not $report.output_written) 'NO_OUTPUT_WRITTEN'
Assert-True (-not $report.privacy.raw_paths_exported) 'NO_RAW_PATHS'
Assert-True (-not $report.privacy.credential_names_exported) 'NO_CREDENTIAL_NAMES'
Assert-True (-not $report.privacy.proxy_values_exported) 'NO_PROXY_VALUES'
Assert-True (@($report.checks | Where-Object { $_.status -cne 'UNKNOWN' }).Count -eq 0) 'PLAN_ONLY_CHECKS_UNKNOWN'
Assert-True ($raw -notmatch [regex]::Escape('C:\TJLab\DRYRUN_ONLY\terminal')) 'RAW_PATH_NOT_SERIALIZED'
Assert-True ($raw -notmatch '(?i)password|bearer|account_number|proxyserver') 'NO_SECRET_FIELDS'

[Console]::Out.WriteLine('PASS: pre-state scanner PlanOnly dry-run is non-mutating and sanitized.')
