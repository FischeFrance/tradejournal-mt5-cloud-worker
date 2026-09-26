$ErrorActionPreference = 'Stop'

$repo = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$selectionScript = Join-Path $PSScriptRoot 'mt5-compiler-selection.ps1'
if (-not (Test-Path -LiteralPath $selectionScript -PathType Leaf)) {
    throw 'MT5 compiler selection gate is missing.'
}
. $selectionScript
$compiler = Resolve-TradeJournalMt5Compiler
$editor = $compiler.Editor
$source = Join-Path $repo 'mt5\experts\TradeJournalBridge.mq5'
$stage = 'C:\TradeJournal\artifacts\mql5'
$target = Join-Path $stage 'TradeJournalBridge.mq5'
$log = 'C:\TradeJournal\logs\TradeJournalBridge-compile.log'
$result = 'C:\TradeJournal\logs\TradeJournalBridge-compile-result.json'

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
    compiler_source = $compiler.Source
    compiler_version = $compiler.Version
    compiler_editor_sha256 = $compiler.EditorSha256
    compiler_terminal_sha256 = $compiler.TerminalSha256
} | ConvertTo-Json | Set-Content $result -Encoding utf8
