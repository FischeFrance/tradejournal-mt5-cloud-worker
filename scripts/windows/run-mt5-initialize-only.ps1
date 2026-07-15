param(
  [string]$TerminalPath='C:\Program Files\MetaTrader 5\terminal64.exe',
  [string]$ReportPath='C:\TradeJournal\logs\mt5-initialize-only-result.json'
)
$ErrorActionPreference='Stop'
$repo=Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$python="$repo\.venv\Scripts\python.exe"
$arguments="-m windows_agent.initialize_probe --terminal `"$TerminalPath`" --report `"$ReportPath`""
Remove-Item $ReportPath -ErrorAction SilentlyContinue
if([Diagnostics.Process]::GetCurrentProcess().SessionId -eq 0){
  $taskName='TradeJournal-MT5-Initialize-Probe'
  $action=New-ScheduledTaskAction -Execute $python -Argument $arguments -WorkingDirectory $repo
  $principal=New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Highest
  Register-ScheduledTask -TaskName $taskName -Action $action -Principal $principal|Out-Null
  try{
    Start-ScheduledTask -TaskName $taskName
    $deadline=(Get-Date).AddMinutes(3)
    while((Get-Date)-lt $deadline -and -not(Test-Path $ReportPath)){Start-Sleep 2}
  }finally{Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue}
}else{
  & $python -m windows_agent.initialize_probe --terminal $TerminalPath --report $ReportPath
}
if(-not(Test-Path $ReportPath)){throw 'Interactive initialize probe did not produce a report.'}
Get-Content $ReportPath
