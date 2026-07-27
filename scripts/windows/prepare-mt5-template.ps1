$ErrorActionPreference = 'Stop'

$source = 'C:\Program Files\MetaTrader 5'
$target = 'C:\TradeJournal\mt5-template'
$repo = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$compileBridgeScript = Join-Path $PSScriptRoot 'compile-readonly-ea.ps1'
$compileLoaderScript = Join-Path $PSScriptRoot 'compile-loader-script.ps1'
$compileDiscoveryScript = Join-Path $PSScriptRoot 'compile-discovery-script.ps1'
$compiledExpert = 'C:\TradeJournal\artifacts\mql5\TradeJournalBridge.ex5'
$compiledLoader = 'C:\TradeJournal\artifacts\mql5\TradeJournalLoader.ex5'
$compiledDiscovery = 'C:\TradeJournal\artifacts\mql5\TradeJournalDiscovery.ex5'
$compileLog = 'C:\TradeJournal\logs\TradeJournalBridge-compile.log'

if (-not (Test-Path "$source\terminal64.exe")) { throw 'MT5 installation not found.' }
if (-not (Test-Path $compileBridgeScript)) { throw 'compile-readonly-ea.ps1 not found.' }
if (-not (Test-Path $compileLoaderScript)) { throw 'compile-loader-script.ps1 not found.' }
if (-not (Test-Path $compileDiscoveryScript)) { throw 'compile-discovery-script.ps1 not found.' }
if (Test-Path $target) { throw 'Template exists; refusing overwrite.' }

& $compileBridgeScript
& $compileLoaderScript
& $compileDiscoveryScript
# MetaEditor may return a non-zero launcher code even after producing a successful compile log.
# Each compile script is authoritative and throws on any compiler warning/error.
if (-not (Test-Path $compiledExpert)) { throw "MetaEditor compilation failed; inspect $compileLog" }
if (-not (Test-Path $compiledLoader)) { throw 'Loader script compilation failed.' }
if (-not (Test-Path $compiledDiscovery)) { throw 'Discovery script compilation failed.' }

Copy-Item $source $target -Recurse
$destination = Join-Path $target 'MQL5\Experts\TradeJournal\TradeJournalBridge.ex5'
$scriptDestination = Join-Path $target 'MQL5\Scripts\TradeJournal'
New-Item -ItemType Directory -Force (Split-Path $destination) | Out-Null
New-Item -ItemType Directory -Force $scriptDestination | Out-Null
Copy-Item $compiledExpert $destination -Force
Copy-Item $compiledLoader (Join-Path $scriptDestination 'TradeJournalLoader.ex5') -Force
Copy-Item $compiledDiscovery (Join-Path $scriptDestination 'TradeJournalDiscovery.ex5') -Force

# The portable template must never inherit a saved customer account. The per-connection
# protected startup.ini is created only by NativeMt5Runtime and deleted after bootstrap.
Remove-Item "$target\config\accounts.dat", "$target\config\accounts.ini" -Force -ErrorAction SilentlyContinue
Remove-Item "$target\MQL5\Files\TradeJournal" -Recurse -Force -ErrorAction SilentlyContinue

$hash = (Get-FileHash $destination -Algorithm SHA256).Hash
@{
    expert_path = $destination
    expert_sha256 = $hash
    loader_sha256 = (Get-FileHash (Join-Path $scriptDestination 'TradeJournalLoader.ex5') -Algorithm SHA256).Hash
    discovery_sha256 = (Get-FileHash (Join-Path $scriptDestination 'TradeJournalDiscovery.ex5') -Algorithm SHA256).Hash
    compile_log = $compileLog
    python_metatrader5_required = $false
} | ConvertTo-Json | Set-Content 'C:\TradeJournal\logs\mt5-template-result.json' -Encoding utf8

Write-Host "Read-only MT5 template ready. EX5 SHA256: $hash"
