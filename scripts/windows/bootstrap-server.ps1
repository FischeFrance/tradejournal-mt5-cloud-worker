#Requires -Version 5.1
$ErrorActionPreference = 'Stop'
$roots = 'handoff','logs','installers','instances','secrets','state'
foreach ($name in $roots) { New-Item -ItemType Directory -Force "C:\TradeJournal\$name" | Out-Null }
Write-Host 'TradeJournal Windows directory layout is ready.'

