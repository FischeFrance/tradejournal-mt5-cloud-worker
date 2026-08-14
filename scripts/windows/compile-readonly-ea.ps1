$ErrorActionPreference = 'Stop'

$repo = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$terminalRoot = 'C:\Program Files\MetaTrader 5'
$editor = Join-Path $terminalRoot 'MetaEditor64.exe'
$source = Join-Path $repo 'mt5\experts\TradeJournalBridge.mq5'
$stage = 'C:\TradeJournal\artifacts\mql5'
$target = Join-Path $stage 'TradeJournalBridge.mq5'
$log = 'C:\TradeJournal\logs\TradeJournalBridge-compile.log'
$result = 'C:\TradeJournal\logs\TradeJournalBridge-compile-result.json'

if (-not (Test-Path $editor)) { throw 'MetaEditor64.exe not found.' }
if (-not (Test-Path $source)) { throw 'TradeJournalBridge.mq5 not found.' }
New-Item -ItemType Directory -Force $stage, (Split-Path $log) | Out-Null
Copy-Item $source $target -Force
$binary = [IO.Path]::ChangeExtension($target, '.ex5')
Remove-Item $binary, $log -Force -ErrorAction SilentlyContinue

$null = & $editor "/compile:$target" "/log:$log"
$deadline = (Get-Date).AddSeconds(60)
while ((-not (Test-Path $binary) -or -not (Select-String -Path $log -SimpleMatch 'Result:' -Quiet -ErrorAction SilentlyContinue)) -and (Get-Date) -lt $deadline) { Start-Sleep -Seconds 1 }
if (-not (Test-Path $binary) -or -not (Select-String -Path $log -SimpleMatch 'Result: 0 errors, 0 warnings' -Quiet -ErrorAction SilentlyContinue)) {
    throw "EA compilation failed; inspect $log"
}

@{
    source = $source
    binary = $binary
    sha256 = (Get-FileHash $binary -Algorithm SHA256).Hash
    compile_log = $log
    static_guard = 'tests/test_mql5_ea_no_trading.py'
} | ConvertTo-Json | Set-Content $result -Encoding utf8
