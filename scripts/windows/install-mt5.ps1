#Requires -Version 5.1
$ErrorActionPreference = 'Stop'
$installer = 'C:\TradeJournal\installers\mt5setup.exe'
Invoke-WebRequest -UseBasicParsing 'https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe' -OutFile $installer
$signature = Get-AuthenticodeSignature $installer
if ($signature.Status -ne 'Valid') { throw "Invalid MetaQuotes Authenticode signature: $($signature.Status)" }
$process = Start-Process $installer -ArgumentList '/auto' -Wait -PassThru -WindowStyle Hidden
if ($process.ExitCode -ne 0) { throw "MT5 installer exit code $($process.ExitCode)" }
Get-ChildItem "$env:ProgramFiles","${env:ProgramFiles(x86)}",$env:LOCALAPPDATA -Filter terminal64.exe -Recurse -ErrorAction SilentlyContinue | Select-Object -ExpandProperty FullName

