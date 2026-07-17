$ErrorActionPreference = 'Stop'

$task = 'TradeJournal-AgentSecretDiagnostic'
$out = 'C:\TradeJournal\logs\agent-secret-diagnostic.json'
$python = 'C:\TradeJournal\projects\tradejournal-mt5-cloud-worker\.venv\Scripts\python.exe'
$script = 'C:\TradeJournal\projects\tradejournal-mt5-cloud-worker\scripts\windows\diagnose_agent_secret.py'
$launcher = 'C:\TradeJournal\logs\run-agent-secret-diagnostic.cmd'
@"
@echo off
"$python" "$script" > "$out" 2>&1
"@ | Set-Content -Path $launcher -Encoding ascii

schtasks /Create /TN $task /SC ONCE /ST 23:59 /RU SYSTEM /RL HIGHEST /TR $launcher /F | Out-Null
try {
  schtasks /Run /TN $task | Out-Null
  Start-Sleep -Seconds 5
  Get-Content $out -Raw
} finally {
  schtasks /Delete /TN $task /F | Out-Null
  Remove-Item $script -Force -ErrorAction SilentlyContinue
  Remove-Item $launcher -Force -ErrorAction SilentlyContinue
}
