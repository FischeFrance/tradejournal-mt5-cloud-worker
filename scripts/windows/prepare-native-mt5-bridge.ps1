[CmdletBinding()]
param(
    [string]$RepositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path,
    [string]$MetaEditor = 'C:\Program Files\MetaTrader 5\MetaEditor64.exe',
    [string]$OutputDirectory = 'C:\TradeJournal\artifacts\mql5'
)

$ErrorActionPreference = 'Stop'
$source = Join-Path $RepositoryRoot 'mt5\experts\TradeJournalBridge.mq5'
if (-not (Test-Path -LiteralPath $MetaEditor -PathType Leaf)) { throw 'MetaEditor is not installed.' }
if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { throw 'Read-only EA source is missing.' }

New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null
$buildSource = Join-Path $OutputDirectory 'TradeJournalBridge.mq5'
$buildBinary = Join-Path $OutputDirectory 'TradeJournalBridge.ex5'
$buildLog = Join-Path $OutputDirectory 'compile.log'
Copy-Item -LiteralPath $source -Destination $buildSource -Force
Remove-Item -LiteralPath $buildBinary,$buildLog -Force -ErrorAction SilentlyContinue

$process = Start-Process -FilePath $MetaEditor -ArgumentList @(
    "/compile:$buildSource",
    "/log:$buildLog"
) -Wait -PassThru -WindowStyle Hidden

if (-not (Test-Path -LiteralPath $buildBinary -PathType Leaf)) {
    throw "EA compilation failed. Sanitized compiler log: $buildLog"
}
$result = Select-String -LiteralPath $buildLog -Pattern '^Result:' | Select-Object -Last 1
if (-not $result -or $result.Line -notmatch '0 errors') {
    throw "EA compilation reported errors. Sanitized compiler log: $buildLog"
}
Write-Output "Read-only MT5 bridge prepared: $buildBinary"
