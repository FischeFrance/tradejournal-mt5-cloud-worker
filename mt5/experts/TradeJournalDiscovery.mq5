//+------------------------------------------------------------------+
//| TradeJournalDiscovery.mq5                                        |
//|                                                                  |
//| Discovery del simbolo reale del broker DENTRO il terminale, in   |
//| MQL5, senza IPC Python. Attende la sincronizzazione del conto,   |
//| risolve il simbolo (es. EURUSD.raw) leggendo la preferenza da    |
//| file, lo seleziona in Market Watch e lo scrive su                |
//| MQL5\Files\TradeJournal\discovered-symbol.json (scrittura        |
//| atomica). Non chiama MAI OrderSend/funzioni di trading, non       |
//| importa DLL. Sostituisce la dipendenza da mt5.initialize()       |
//| (IPC Python), intermittente e inadatta al multi-istanza.         |
//+------------------------------------------------------------------+
#property copyright "TradeJournal"
#property version   "1.40"
#property strict

#define BASE_DIR "TradeJournal"

string JsonEscape(const string value)
  {
   string escaped = "";
   for(int i = 0; i < StringLen(value); i++)
     {
      ushort code = StringGetCharacter(value, i);
      if(code == 34)
         escaped += "\\\"";
      else if(code == 92)
         escaped += "\\\\";
      else if(code == 8)
         escaped += "\\b";
      else if(code == 9)
         escaped += "\\t";
      else if(code == 10)
         escaped += "\\n";
      else if(code == 12)
         escaped += "\\f";
      else if(code == 13)
         escaped += "\\r";
      else if(code < 32)
         escaped += StringFormat("\\u%04X", code);
      else
         escaped += StringSubstr(value, i, 1);
     }
   return escaped;
  }

string ReadTextFile(const string path)
  {
   int handle = FileOpen(path, FILE_READ | FILE_TXT | FILE_ANSI | FILE_SHARE_READ, 0, CP_UTF8);
   if(handle == INVALID_HANDLE)
      return "";
   string value = "";
   if(!FileIsEnding(handle))
      value = FileReadString(handle);
   FileClose(handle);
   StringTrimLeft(value);
   StringTrimRight(value);
   return value;
  }

//--- Legge il simbolo preferito scritto dal runtime nel sandbox. Default "EURUSD".
string ReadPreference()
  {
   string value = ReadTextFile(BASE_DIR + "\\symbol-preference.txt");
   return (value == "") ? "EURUSD" : value;
  }

//--- Conferma subito al runtime che il grafico [StartUp] e' valido e che
//--- OnStart e' realmente in esecuzione. Non contiene credenziali.
bool WriteStarted()
  {
   if(!FolderCreate(BASE_DIR))
      ResetLastError();
   string connection_id = ReadTextFile(BASE_DIR + "\\connection_id");
   long terminal_build = TerminalInfoInteger(TERMINAL_BUILD);
   if(connection_id == "" || terminal_build <= 0 || _Symbol == "")
      return false;
   string tmp = BASE_DIR + "\\discovery-started.tmp";
   string dst = BASE_DIR + "\\discovery-started.json";
   int handle = FileOpen(tmp, FILE_WRITE | FILE_TXT | FILE_ANSI | FILE_SHARE_READ, 0, CP_UTF8);
   if(handle == INVALID_HANDLE)
      return false;
   string json = "{";
   json += "\"schema_version\":1,";
   json += "\"connection_id\":\"" + JsonEscape(connection_id) + "\",";
   json += "\"chart_symbol\":\"" + JsonEscape(_Symbol) + "\",";
   json += "\"terminal_build\":" + IntegerToString(terminal_build) + "}";
   FileWriteString(handle, json);
   FileFlush(handle);
   FileClose(handle);
   ResetLastError();
   return FileMove(tmp, 0, dst, FILE_REWRITE);
  }

//--- Scrittura atomica del risultato versionato: .tmp poi rename su .json.
bool WriteSymbol(const string symbol,
                 const string requested_symbol,
                 const string resolution,
                 const int catalog_total)
  {
   if(!FolderCreate(BASE_DIR))
      ResetLastError();
   string connection_id = ReadTextFile(BASE_DIR + "\\connection_id");
   long login = AccountInfoInteger(ACCOUNT_LOGIN);
   string server = AccountInfoString(ACCOUNT_SERVER);
   long terminal_build = TerminalInfoInteger(TERMINAL_BUILD);
   bool synchronized = SymbolIsSynchronized(symbol);
   bool terminal_connected = (bool)TerminalInfoInteger(TERMINAL_CONNECTED);
   bool account_trade_allowed = (bool)AccountInfoInteger(ACCOUNT_TRADE_ALLOWED);
   if(connection_id == "" || login <= 0 || server == "" || terminal_build <= 0 ||
      catalog_total <= 0 || !synchronized || !terminal_connected)
     {
      Print("TradeJournalDiscovery: evidenza incompleta; risultato non pubblicato.");
      return false;
     }

   string tmp = BASE_DIR + "\\discovered-symbol.tmp";
   string dst = BASE_DIR + "\\discovered-symbol.json";
   int handle = FileOpen(tmp, FILE_WRITE | FILE_TXT | FILE_ANSI | FILE_SHARE_READ, 0, CP_UTF8);
   if(handle == INVALID_HANDLE)
     {
      Print("TradeJournalDiscovery: output non apribile, errore=", GetLastError());
      return false;
     }
   string json = "{";
   json += "\"schema_version\":1,";
   json += "\"connection_id\":\"" + JsonEscape(connection_id) + "\",";
   json += "\"login\":" + IntegerToString(login) + ",";
   json += "\"server\":\"" + JsonEscape(server) + "\",";
   json += "\"requested_symbol\":\"" + JsonEscape(requested_symbol) + "\",";
   json += "\"resolution\":\"" + JsonEscape(resolution) + "\",";
   json += "\"catalog_total\":" + IntegerToString(catalog_total) + ",";
   json += "\"synchronized\":true,";
   json += "\"terminal_connected\":true,";
   json += "\"account_trade_allowed\":" + (account_trade_allowed ? "true" : "false") + ",";
   json += "\"terminal_build\":" + IntegerToString(terminal_build) + ",";
   json += "\"symbol\":\"" + JsonEscape(symbol) + "\"}";
   FileWriteString(handle, json);
   FileFlush(handle);
   FileClose(handle);
   ResetLastError();
   if(!FileMove(tmp, 0, dst, FILE_REWRITE))
     {
      Print("TradeJournalDiscovery: output non pubblicabile, errore=", GetLastError());
      return false;
     }
   return true;
  }

bool BridgeHandoffReady()
  {
   return FileIsExist(BASE_DIR + "\\bridge-ready");
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

string FindMatchingSymbol(const string preferred,
                          const bool selected_only,
                          string &resolution)
  {
   int total = SymbolsTotal(selected_only);
   string low_pref = preferred;
   StringToLower(low_pref);
   string exact = "";
   string pair_match = "";
   string related = "";
   string wanted_base = "";
   string wanted_profit = "";
   bool has_pair = PreferredPair(preferred, wanted_base, wanted_profit);
   for(int i = 0; i < total; i++)
     {
      string name = SymbolName(i, selected_only);
      string low = name;
      StringToLower(low);
      if(low == low_pref)
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
      if(related == "" && StringFind(low, low_pref) >= 0)
         related = name;
     }
   string chosen = SelectResolved(exact);
   if(chosen != "")
     {
      resolution = "exact";
      return chosen;
     }
   chosen = SelectResolved(pair_match);
   if(chosen != "")
     {
      resolution = "currency_pair";
      return chosen;
     }
   chosen = SelectResolved(related);
   if(chosen != "")
      resolution = "name_related";
   return chosen;
  }

string FirstSelectable(const bool selected_only)
  {
   int total = SymbolsTotal(selected_only);
   for(int i = 0; i < total; i++)
     {
      string chosen = SelectResolved(SymbolName(i, selected_only));
      if(chosen != "")
         return chosen;
     }
   return "";
  }

string ResolveSymbol(const string preferred, string &resolution)
  {
   string chosen = FindMatchingSymbol(preferred, true, resolution);
   if(chosen == "")
      chosen = FindMatchingSymbol(preferred, false, resolution);
   if(chosen == "")
     {
      chosen = FirstSelectable(true);
      if(chosen == "")
         chosen = FirstSelectable(false);
      if(chosen != "")
         resolution = "fallback";
     }
   return chosen;
  }

void OnStart()
  {
   ulong started_ms = GetTickCount64();
   string resolved_symbol = "";
   string resolution = "";
   string preferred = ReadPreference();
   if(!WriteStarted())
     {
      Print("TradeJournalDiscovery: marker di avvio non pubblicabile, errore=", GetLastError());
      return;
     }
   Print("TradeJournalDiscovery: avviato; preferenza=", preferred, "; attendo sync account.");
   while(!IsStopped() && GetTickCount64() - started_ms < 120000)
     {
      if(TerminalInfoInteger(TERMINAL_CONNECTED) &&
         AccountInfoInteger(ACCOUNT_LOGIN) > 0 &&
         SymbolsTotal(false) > 0)
        {
         resolved_symbol = ResolveSymbol(preferred, resolution);
         if(resolved_symbol != "" && SymbolIsSynchronized(resolved_symbol))
           {
            if(WriteSymbol(resolved_symbol, preferred, resolution, SymbolsTotal(false)))
              {
               Print("TradeJournalDiscovery: simbolo risolto=", resolved_symbol,
                     " (risultato versionato pubblicato); attendo handoff Bridge.");
               break;
              }
            resolved_symbol = "";
           }
        }
      Sleep(500);
     }
   if(resolved_symbol == "")
     {
      Print("TradeJournalDiscovery: nessun simbolo sincronizzato entro il timeout.");
      return;
     }

   while(!IsStopped() && GetTickCount64() - started_ms < 120000)
     {
      if(BridgeHandoffReady())
        {
         ResetLastError();
         if(ChartApplyTemplate(0, "\\Files\\TradeJournal\\TradeJournalBridge.tpl"))
           {
            Print("TradeJournalDiscovery: handoff al TradeJournalBridge EA richiesto.");
            return;
           }
         Print("TradeJournalDiscovery: handoff fallito, errore=", GetLastError(),
               "; nuovo tentativo tra 3 secondi.");
         Sleep(3000);
         continue;
        }
      Sleep(250);
     }
   Print("TradeJournalDiscovery: handoff Bridge non ricevuto entro il timeout.");
  }
