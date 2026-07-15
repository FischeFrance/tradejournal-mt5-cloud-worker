param([Parameter(Mandatory)][ValidatePattern('^[0-9a-fA-F-]{36}$')][string]$ConnectionId)
$ErrorActionPreference = 'Stop'
$login = Read-Host 'MT5 DEMO login number'
$server = Read-Host 'MT5 DEMO server'
$secure = Read-Host 'MT5 INVESTOR password (read-only only)' -AsSecureString
$ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try {
  $password = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
  $payload = @{ login=$login; server=$server; investor_password=$password } | ConvertTo-Json -Compress
  $repo = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
  $payload | & "$repo\.venv\Scripts\python.exe" -m windows_agent.store_credentials --connection-id $ConnectionId
  if ($LASTEXITCODE -ne 0) { throw 'Protected credential storage failed.' }
} finally {
  if ($ptr -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
  $password = $null; $payload = $null; $secure.Dispose()
}
Write-Host 'Credentials protected with DPAPI; password was not displayed.'

