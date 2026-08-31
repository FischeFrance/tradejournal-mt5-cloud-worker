//+------------------------------------------------------------------+
//| TradeJournalLoader.mq5                                           |
//|                                                                  |
//| Expert di bootstrap: attende che il conto sia connesso, quindi   |
//| applica il template che contiene il vero TradeJournalBridge EA.  |
//| Non chiama MAI OrderSend o altre funzioni di trading e non       |
//| importa DLL.                                                     |
//+------------------------------------------------------------------+
#property copyright "TradeJournal"
#property version   "1.21"
#property strict

#define BASE_DIR "TradeJournal"

void WriteLoaderState(const string state)
  {
   if(!FolderCreate(BASE_DIR))
      ResetLastError();
   int handle = FileOpen(BASE_DIR + "\\loader-state.tmp",
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
   FileDelete(BASE_DIR + "\\loader-state.txt");
   if(!FileMove(BASE_DIR + "\\loader-state.tmp", 0,
                BASE_DIR + "\\loader-state.txt", FILE_REWRITE))
      Print("TradeJournalLoader: marker non pubblicato, stato=", state,
            ", errore=", GetLastError());
  }

bool PreferredPair(const string preferred, string &base, string &profit)
  {
   string normalized = preferred;
   StringToUpper(normalized);
   if(StringLen(normalized) < 6)
      return false;
   base = StringSubstr(normalized, 0, 3);
   profit = StringSubstr(normalized, 3, 3);
   return true;
  }

string SelectResolved(const string symbol)
  {
   if(symbol != "" && SymbolSelect(symbol, true))
      return symbol;
   return "";
  }

// Resolve the chart symbol inside the authenticated terminal. Search Market Watch first, then
// the complete broker catalogue. Base/profit metadata covers broker prefixes and suffixes
// without maintaining a fragile list such as .raw, .x or mEURUSD.
string FindMatchingSymbol(const string preferred, const bool selected_only)
  {
   int total = SymbolsTotal(selected_only);
   if(total <= 0)
      return "";
   string low_preferred = preferred;
   StringToLower(low_preferred);
   string exact = "";
   string pair_match = "";
   string related = "";
   string wanted_base = "";
   string wanted_profit = "";
   bool has_pair = PreferredPair(preferred, wanted_base, wanted_profit);
   for(int index = 0; index < total; index++)
     {
      string name = SymbolName(index, selected_only);
      string lower = name;
      StringToLower(lower);
      if(lower == low_preferred)
         exact = name;
      if(has_pair && pair_match == "")
        {
         string base = SymbolInfoString(name, SYMBOL_CURRENCY_BASE);
         string profit = SymbolInfoString(name, SYMBOL_CURRENCY_PROFIT);
         StringToUpper(base);
         StringToUpper(profit);
         if(base == wanted_base && profit == wanted_profit)
            pair_match = name;
        }
      if(related == "" && StringFind(lower, low_preferred) >= 0)
         related = name;
     }
   string selected = SelectResolved(exact);
   if(selected == "")
      selected = SelectResolved(pair_match);
   if(selected == "")
      selected = SelectResolved(related);
   return selected;
  }

string FirstSelectable(const bool selected_only)
  {
   int total = SymbolsTotal(selected_only);
   for(int index = 0; index < total; index++)
     {
      string selected = SelectResolved(SymbolName(index, selected_only));
      if(selected != "")
         return selected;
     }
   return "";
  }

string ResolveBrokerSymbol(const string preferred)
  {
   string selected = FindMatchingSymbol(preferred, true);
   if(selected == "")
      selected = FindMatchingSymbol(preferred, false);
   if(selected == "")
      selected = FirstSelectable(true);
   if(selected == "")
      selected = FirstSelectable(false);
   return selected;
  }

// The Python runtime supplies a reviewed, read-only template skeleton.  Only the broker's
// in-terminal symbol is substituted here; no account data, command line or credential is read.
bool ApplyBridgeTemplate(const string symbol)
  {
   string source = BASE_DIR + "\\TradeJournalBridge.tpl";
   string temporary = BASE_DIR + "\\TradeJournalBridge.resolved.tmp";
   string destination = BASE_DIR + "\\TradeJournalBridge.resolved.tpl";
   int reader = FileOpen(source, FILE_READ | FILE_TXT | FILE_UNICODE | FILE_SHARE_READ);
   if(reader == INVALID_HANDLE)
     {
      Print("TradeJournalLoader: template non leggibile, errore=", GetLastError());
      return false;
     }
   string lines[];
   int count = 0;
   while(!FileIsEnding(reader))
     {
      ArrayResize(lines, count + 1);
      lines[count++] = FileReadString(reader);
     }
   FileClose(reader);
   bool replaced = false;
   for(int index = 0; index < count; index++)
     {
      if(StringFind(lines[index], "symbol=") == 0)
        {
         lines[index] = "symbol=" + symbol;
         replaced = true;
         break;
        }
     }
   if(!replaced)
     {
      Print("TradeJournalLoader: template senza simbolo.");
      return false;
     }
   int writer = FileOpen(temporary, FILE_WRITE | FILE_TXT | FILE_UNICODE | FILE_SHARE_READ);
   if(writer == INVALID_HANDLE)
     {
      Print("TradeJournalLoader: template non scrivibile, errore=", GetLastError());
      return false;
     }
   for(int index = 0; index < count; index++)
      FileWriteString(writer, lines[index] + "\r\n");
   FileFlush(writer);
   FileClose(writer);
   FileDelete(destination);
   ResetLastError();
   if(!FileMove(temporary, 0, destination, FILE_REWRITE))
     {
      Print("TradeJournalLoader: template non pubblicabile, errore=", GetLastError());
      return false;
     }
   ResetLastError();
   if(!ChartApplyTemplate(0, "\\Files\\TradeJournal\\TradeJournalBridge.resolved.tpl"))
     {
      Print("TradeJournalLoader: handoff fallito, errore=", GetLastError());
      return false;
     }
   return true;
  }

const ulong LOADER_TIMEOUT_MS = 120000;
const ulong CONNECTION_GRACE_MS = 3000;

ulong loader_started_ms = 0;
ulong connected_since_ms = 0;
ulong last_wait_log_ms = 0;

int OnInit()
  {
   loader_started_ms = GetTickCount64();
   WriteLoaderState("started");
   Print("TradeJournalLoader: avviato; attendo account connesso.");
   EventSetTimer(1);
   return INIT_SUCCEEDED;
  }

void OnDeinit(const int reason)
  {
   EventKillTimer();
  }

void OnTimer()
  {
   if(IsStopped())
     {
      WriteLoaderState("stopped");
      EventKillTimer();
      return;
     }
   ulong now_ms = GetTickCount64();
   if(now_ms - loader_started_ms >= LOADER_TIMEOUT_MS)
     {
      WriteLoaderState("timeout");
      Print("TradeJournalLoader: nessun handoff completato entro il timeout.");
      EventKillTimer();
      return;
     }

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
      if(now_ms - connected_since_ms < CONNECTION_GRACE_MS)
         return;

      string symbol = ResolveBrokerSymbol(_Symbol);
      if(symbol == "")
        {
         Print("TradeJournalLoader: catalogo broker non ancora pronto.");
         connected_since_ms = now_ms;
         return;
        }
      // AllowLiveTrading=0 cannot be elevated by the template.
      if(ApplyBridgeTemplate(symbol))
        {
         WriteLoaderState("handoff-requested");
         Print("TradeJournalLoader: handoff al TradeJournalBridge EA richiesto, simbolo=", symbol, ".");
         EventKillTimer();
         return;
        }
      WriteLoaderState("handoff-retry");
      connected_since_ms = now_ms;
      return;
     }

   connected_since_ms = 0;
   if(now_ms - last_wait_log_ms >= 5000)
     {
      last_wait_log_ms = now_ms;
      Print("TradeJournalLoader: attesa connessione, connected=", connected,
            ", login_present=", login > 0, ".");
     }
  }
