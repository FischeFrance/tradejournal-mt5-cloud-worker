//+------------------------------------------------------------------+
//| TradeJournalDiscovery.mq5                                        |
//|                                                                  |
//| Mantiene aperto il grafico durante la sola discovery del simbolo.|
//| Non chiama MAI OrderSend o altre funzioni di trading e non       |
//| importa DLL.                                                     |
//+------------------------------------------------------------------+
#property copyright "TradeJournal"
#property version   "1.00"
#property strict

void OnStart()
  {
   ulong started_ms = GetTickCount64();
   bool connected_logged = false;
   Print("TradeJournalDiscovery: avviato; attendo sincronizzazione account.");
   while(!IsStopped() && GetTickCount64() - started_ms < 120000)
     {
      if(!connected_logged && TerminalInfoInteger(TERMINAL_CONNECTED) &&
         AccountInfoInteger(ACCOUNT_LOGIN) > 0)
        {
         connected_logged = true;
         Print("TradeJournalDiscovery: account connesso; mantengo il grafico attivo.");
        }
      Sleep(500);
     }
   Print("TradeJournalDiscovery: terminato.");
  }
