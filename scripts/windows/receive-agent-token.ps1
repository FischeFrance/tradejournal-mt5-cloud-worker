param(
  [ValidateSet('agent_token', 'mt5_provisioning_key')]
  [string]$SecretName = 'agent_token'
)
$ErrorActionPreference = 'Stop'

# Interactive-only by design: never accept the secret as a script parameter (would land in
# process listings / shell history) and never Write-Host/echo it back. Mirrors
# prepare-demo-credentials.ps1's SecureString handling for MT5 investor passwords.
$label = if ($SecretName -eq 'agent_token') { 'Windows Agent bearer token (from mt5-agent-token.mjs issue)' } else { 'MT5 provisioning encryption key (base64, from Supabase MT5_PROVISIONING_ENCRYPTION_KEY secret)' }
$secure = Read-Host "Paste the $label" -AsSecureString
$ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try {
  $value = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
  $repo = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
  $value | & "$repo\.venv\Scripts\python.exe" -m windows_agent.store_agent_secret --name $SecretName
  if ($LASTEXITCODE -ne 0) { throw 'Protected secret storage failed.' }
} finally {
  if ($ptr -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
  # Best-effort plaintext cleanup: PowerShell strings are immutable and cannot be zeroed in
  # place, but dropping every reference lets the value become garbage-collectable immediately
  # instead of living for the rest of the session.
  $value = $null
  $secure.Dispose()
  [System.GC]::Collect()
}
