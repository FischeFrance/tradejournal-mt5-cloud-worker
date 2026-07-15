Get-Service TradeJournalMT5Agent -ErrorAction SilentlyContinue | Format-List Name,Status,StartType
Get-Process terminal64 -ErrorAction SilentlyContinue | Select-Object Id,Path

