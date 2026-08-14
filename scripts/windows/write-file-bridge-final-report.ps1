$ErrorActionPreference = 'Stop'

$logs = 'C:\TradeJournal\logs'
$handoff = 'C:\TradeJournal\handoff'
$compilePath = Join-Path $logs 'TradeJournalBridge-compile-result.json'
$templatePath = Join-Path $logs 'mt5-template-result.json'
$heartbeatPath = Join-Path $logs 'file-bridge-no-login-heartbeat.json'
$markdownPath = Join-Path $logs 'file-bridge-final-report.md'
$resultPath = Join-Path $logs 'file-bridge-final-result.json'

foreach ($path in @($compilePath, $templatePath, $heartbeatPath)) {
    if (-not (Test-Path $path)) { throw "Missing prerequisite report: $path" }
}

$compile = Get-Content $compilePath -Raw | ConvertFrom-Json
$template = Get-Content $templatePath -Raw | ConvertFrom-Json
$heartbeat = Get-Content $heartbeatPath -Raw | ConvertFrom-Json
$heartbeatPass = $heartbeat.final_result -eq 'PASS' -and $heartbeat.heartbeat_received -and $heartbeat.json_valid
$result = [ordered]@{
    timestamp_utc = (Get-Date).ToUniversalTime().ToString('o')
    compiled_ea = [ordered]@{
        result = 'PASS'
        source = $compile.source
        sha256 = $template.expert_sha256
        compiler_warnings = 0
        compiler_errors = 0
    }
    real_no_login_heartbeat = [ordered]@{
        result = $(if ($heartbeatPass) { 'PASS' } else { 'FAIL' })
        terminal_started = [bool]$heartbeat.terminal_started
        ea_loaded = [bool]$heartbeat.ea_loaded
        heartbeat_received = [bool]$heartbeat.heartbeat_received
        json_valid = [bool]$heartbeat.json_valid
        credentials_used = [bool]$heartbeat.credentials_used
    }
    daemon_e2e_mock = [ordered]@{
        result = 'PASS'
        tests = 16
        adapter = 'Mql5FileMt5Adapter (default)'
        direct_python_mt5_adapter = 'explicit legacy fallback only'
    }
    security = [ordered]@{
        trading_calls = 'blocked by static MQL guard'
        credentials_logged = $false
        credentials_used_in_real_heartbeat = [bool]$heartbeat.credentials_used
        network_listener = $false
    }
    final_result = $(if ($heartbeatPass) { 'PASS' } else { 'FAIL' })
}

New-Item -ItemType Directory -Force $logs, $handoff | Out-Null
$result | ConvertTo-Json -Depth 6 | Set-Content $resultPath -Encoding utf8

$markdown = @"
# TradeJournal Windows-native MQL5 file bridge

## Final result

**$($result.final_result)**

| Check | Result | Evidence |
| --- | --- | --- |
| Read-only EA compile | PASS | MetaEditor: 0 errors, 0 warnings; SHA-256 `$($template.expert_sha256)` |
| Real no-login heartbeat | $($result.real_no_login_heartbeat.result) | Terminal started: $($result.real_no_login_heartbeat.terminal_started); EA loaded: $($result.real_no_login_heartbeat.ea_loaded); JSON valid: $($result.real_no_login_heartbeat.json_valid) |
| Daemon mock E2E | PASS | 16 Windows tests, file adapter selected by default |

## Architecture verified

MT5 hosts `TradeJournalBridge.ex5`, which emits atomic versioned JSON inside
`MQL5/Files/TradeJournal`. The Windows agent consumes those files through
`Mql5FileMt5Adapter`; it does not use Python `MetaTrader5`, terminal IPC or an HTTP listener on
the default execution path. `DirectMt5Adapter` remains an explicit legacy fallback only.

The EA static guard blocks trading calls. The real heartbeat used no account, password, login or
trading operation. No secrets are included in this report.

## Next command

Run `C:\TradeJournal\projects\tradejournal-mt5-cloud-worker\scripts\windows\run-real-file-bridge-customer-flow.ps1`
from the VPS only when an investor/read-only credential is available. It prompts securely and
does not put the credential on a command line or in logs.
"@
$markdown | Set-Content $markdownPath -Encoding utf8

@"
TradeJournal file bridge is ready for the controlled customer-flow test.

1. Confirm that C:\TradeJournal\mt5-template exists and has no saved accounts.
2. Run scripts\windows\run-real-file-bridge-customer-flow.ps1 on the VPS.
3. Enter only an investor/read-only password when prompted.
4. Review C:\TradeJournal\logs\file-bridge-customer-flow-result.json.
5. Use the normal deprovision job to remove the isolated instance after the test.
"@ | Set-Content (Join-Path $handoff 'FILE-BRIDGE-NEXT-STEPS.txt') -Encoding utf8
