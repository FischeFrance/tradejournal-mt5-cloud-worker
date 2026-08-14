param([switch]$DisconnectAfterTest)
$ErrorActionPreference = 'Stop'

$repo = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$python = Join-Path $repo '.venv\Scripts\python.exe'
$report = 'C:\TradeJournal\logs\file-bridge-customer-flow-result.json'

$login = Read-Host 'MT5 account number'
$server = Read-Host 'MT5 server'
$secure = Read-Host 'MT5 investor password (read-only only)' -AsSecureString
$mode = Read-Host 'History mode: new_only, from_date, all_available'
$fromDate = $null
if ($mode -eq 'from_date') { $fromDate = Read-Host 'From date ISO-8601' }

$bstr = [IntPtr]::Zero
try {
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    $password = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    $request = @{ login = $login; server = $server; investor_password = $password; history_mode = $mode }
    if ($fromDate) { $request.from_date = $fromDate }
    $arguments = @('-m', 'windows_agent.file_bridge_customer_flow', '--report', $report)
    if ($DisconnectAfterTest) { $arguments += '--disconnect' }
    $request | ConvertTo-Json -Compress | & $python @arguments
    if ($LASTEXITCODE -ne 0) { throw 'File bridge flow failed; inspect the sanitized report.' }
} finally {
    if ($bstr -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
    $password = $null; $request = $null
    if ($secure) { $secure.Dispose() }
}

Get-Content $report
