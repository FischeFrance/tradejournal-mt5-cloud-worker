//+------------------------------------------------------------------+
//| TradeJournalLoader.mq5                                           |
//|                                                                  |
//| Script di bootstrap: attende che il conto sia connesso, quindi   |
//| applica il template che contiene il vero TradeJournalBridge EA.  |
//| Non chiama MAI OrderSend o altre funzioni di trading e non       |
//| importa DLL.                                                     |
//+------------------------------------------------------------------+
#property copyright "TradeJournal"
#property version   "1.00"
#property strict

void WriteLoaderState(const string state)
  {
   if(!FolderCreate("TradeJournal"))
      ResetLastError();
   int handle = FileOpen("TradeJournal\\loader-state.tmp",
                         FILE_WRITE | FILE_TXT | FILE_ANSI, 0, CP_UTF8);
   if(handle == INVALID_HANDLE)
     {
      Print("TradeJournalLoader: marker non scrivibile, stato=", state,
            ", errore=", GetLastError());
      return;
     }
   FileWriteString(handle, state);
   FileFlush(handle);
   FileClose(handle);
   FileDelete("TradeJournal\\loader-state.txt");
   if(!FileMove("TradeJournal\\loader-state.tmp", 0,
                "TradeJournal\\loader-state.txt", FILE_REWRITE))
      Print("TradeJournalLoader: marker non pubblicato, stato=", state,
            ", errore=", GetLastError());
  }

void OnStart()
  {
   const ulong timeout_ms = 120000;
   const ulong connection_grace_ms = 3000;
   ulong started_ms = GetTickCount64();
   ulong connected_since_ms = 0;
   ulong last_wait_log_ms = 0;
   WriteLoaderState("started");
   Print("TradeJournalLoader: avviato; attendo account connesso.");

   while(!IsStopped() && GetTickCount64() - started_ms < timeout_ms)
     {
      ulong now_ms = GetTickCount64();
      bool connected = (bool)TerminalInfoInteger(TERMINAL_CONNECTED);
      long login = AccountInfoInteger(ACCOUNT_LOGIN);

      if(connected && login > 0)
        {
         if(connected_since_ms == 0)
           {
            connected_since_ms = now_ms;
            WriteLoaderState("connected");
            Print("TradeJournalLoader: account connesso; attendo stabilizzazione.");
           }
         if(now_ms - connected_since_ms >= connection_grace_ms)
           {
            ResetLastError();
            // Il runtime ha gia' sostituito il placeholder col simbolo reale del broker.
            // AllowLiveTrading=0 non puo' essere elevato dal template.
            if(ChartApplyTemplate(0, "\\Files\\TradeJournal\\TradeJournalBridge.tpl"))
              {
               WriteLoaderState("handoff-requested");
               Print("TradeJournalLoader: handoff al TradeJournalBridge EA richiesto.");
               return;
              }
            Print("TradeJournalLoader: handoff fallito, errore=", GetLastError(),
                  "; nuovo tentativo tra 3 secondi.");
            WriteLoaderState("handoff-retry");
            connected_since_ms = now_ms;
           }
        }
      else
        {
         connected_since_ms = 0;
         if(now_ms - last_wait_log_ms >= 5000)
           {
            last_wait_log_ms = now_ms;
            Print("TradeJournalLoader: attesa connessione, connected=", connected,
                  ", login_present=", login > 0, ".");
           }
        }
      Sleep(500);
     }

   WriteLoaderState(IsStopped() ? "stopped" : "timeout");
   Print("TradeJournalLoader: nessun handoff completato entro il timeout.");
  }
