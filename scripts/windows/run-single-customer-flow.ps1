param([switch]$DisconnectAfterTest)
$ErrorActionPreference='Stop'
throw 'This legacy Python MetaTrader5 IPC flow is disabled. Use run-real-file-bridge-customer-flow.ps1.'
$repo=Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$login=Read-Host 'MT5 DEMO login number'
$server=Read-Host 'MT5 DEMO server'
$secure=Read-Host 'MT5 INVESTOR password (read-only only)' -AsSecureString
$mode=Read-Host 'History mode: new_only, from_date, all_available'
$fromDate=$null
if($mode -eq 'from_date'){$fromDate=Read-Host 'From date ISO-8601'}
$connectionId=[guid]::NewGuid().ToString()
$ptr=[Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try{
  $password=[Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
  $request=@{connection_id=$connectionId;login=$login;server=$server;investor_password=$password;history_mode=$mode}
  if($fromDate){$request.from_date=$fromDate}
  $payload=$request|ConvertTo-Json -Compress
  $sessionId=[Diagnostics.Process]::GetCurrentProcess().SessionId
  if($sessionId -eq 0){
    $payload|& "$repo\.venv\Scripts\python.exe" -m windows_agent.store_credentials --connection-id $connectionId
    if($LASTEXITCODE -ne 0){throw 'DPAPI credential storage failed.'}
    $payload=$null;$password=$null;$request=$null
    $taskName="TradeJournal-MT5-Probe-$connectionId"
    $python="$repo\.venv\Scripts\python.exe"
    $arg="-m windows_agent.customer_flow --resume-connection-id $connectionId --history-mode $mode"
    if($fromDate){$arg+=" --from-date $fromDate"}
    if($DisconnectAfterTest){$arg+=' --disconnect'}
    $action=New-ScheduledTaskAction -Execute $python -Argument $arg -WorkingDirectory $repo
    $principal=New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Highest
    Register-ScheduledTask -TaskName $taskName -Action $action -Principal $principal|Out-Null
    try{
      Remove-Item 'C:\TradeJournal\logs\single-customer-flow-result.json' -ErrorAction SilentlyContinue
      Start-ScheduledTask -TaskName $taskName
      $deadline=(Get-Date).AddMinutes(5)
      while((Get-Date)-lt $deadline -and -not(Test-Path 'C:\TradeJournal\logs\single-customer-flow-result.json')){Start-Sleep 2}
      if(-not(Test-Path 'C:\TradeJournal\logs\single-customer-flow-result.json')){throw 'Interactive agent task did not complete.'}
    }finally{Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue}
  }else{
    $arguments=@('-m','windows_agent.customer_flow')
    if($DisconnectAfterTest){$arguments+='--disconnect'}
    $payload|& "$repo\.venv\Scripts\python.exe" @arguments
    if($LASTEXITCODE -ne 0){throw 'Flow failed; inspect sanitized report.'}
  }
}finally{
  if($ptr -ne [IntPtr]::Zero){[Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr)}
  $password=$null;$payload=$null;$request=$null;$secure.Dispose()
}
Get-Content C:\TradeJournal\logs\single-customer-flow-result.json
