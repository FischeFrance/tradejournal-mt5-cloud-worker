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
#property version   "1.30"
#property strict

#define BASE_DIR "TradeJournal"
#define DISCOVERY_TIMEOUT_MS 120000
#define SYMBOL_SYNC_TIMEOUT_MS 45000

string ReadText(const string path, const string fallback)
  {
   int handle = FileOpen(path,
                         FILE_READ | FILE_TXT | FILE_ANSI | FILE_SHARE_READ,
                         0,
                         CP_UTF8);
   if(handle == INVALID_HANDLE)
      return fallback;
   string value = "";
   if(!FileIsEnding(handle))
      value = FileReadString(handle);
   FileClose(handle);
   StringTrimLeft(value);
   StringTrimRight(value);
   return (value == "") ? fallback : value;
  }

//--- Legge il simbolo preferito scritto dal runtime nel sandbox. Default "EURUSD".
string ReadPreference()
  {
   return ReadText(BASE_DIR + "\\symbol-preference.txt", "EURUSD");
  }

string JsonEscape(const string text)
  {
   string out = "";
   int len = StringLen(text);
   for(int i = 0; i < len; i++)
     {
      ushort c = StringGetCharacter(text, i);
      switch(c)
        {
         case '"':  out += "\\\""; break;
         case '\\': out += "\\\\"; break;
         case '\n': out += "\\n";  break;
         case '\r': out += "\\r";  break;
         case '\t': out += "\\t";  break;
         default:
           if(c < 0x20)
              out += StringFormat("\\u%04x", c);
           else
              out += ShortToString(c);
        }
     }
   return out;
  }

//--- Scrittura atomica del risultato: .tmp poi rename su .json.
bool WriteResult(const string symbol,
                 const string preferred,
                 const string resolution,
                 const int catalog_total)
  {
   string connection_id = ReadText(BASE_DIR + "\\connection_id", "");
   long login = AccountInfoInteger(ACCOUNT_LOGIN);
   string server = AccountInfoString(ACCOUNT_SERVER);
   if(connection_id == "" || login <= 0 || server == "" || catalog_total <= 0)
     {
      Print("TradeJournalDiscovery: identita' risultato incompleta.");
      return false;
     }
   if(!FolderCreate(BASE_DIR))
      ResetLastError();
   string tmp = BASE_DIR + "\\discovered-symbol.tmp";
   string dst = BASE_DIR + "\\discovered-symbol.json";
   int handle = FileOpen(tmp,
                         FILE_WRITE | FILE_TXT | FILE_ANSI | FILE_SHARE_READ,
                         0,
                         CP_UTF8);
   if(handle == INVALID_HANDLE)
     {
      Print("TradeJournalDiscovery: output non apribile, errore=", GetLastError());
      return false;
     }
   string format = "{\"schema_version\":1,\"connection_id\":\"%s\",\"login\":%I64d,";
   format += "\"server\":\"%s\",\"requested_symbol\":\"%s\",\"symbol\":\"%s\",";
   format += "\"resolution\":\"%s\",\"catalog_total\":%d,\"synchronized\":true,";
   format += "\"terminal_build\":%d}";
   string payload = StringFormat(
      format,
      JsonEscape(connection_id),
      login,
      JsonEscape(server),
      JsonEscape(preferred),
      JsonEscape(symbol),
      JsonEscape(resolution),
      catalog_total,
      (int)TerminalInfoInteger(TERMINAL_BUILD));
   FileWriteString(handle, payload);
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

//--- Il runtime pubblica questo marker solo dopo avere creato in modo atomico il
//--- template del Bridge con il simbolo appena risolto.
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
   if(symbol == "" || !SymbolSelect(symbol, true))
      return "";
   long custom = 0;
   if(!SymbolInfoInteger(symbol, SYMBOL_CUSTOM, custom) || custom != 0)
      return "";
   return symbol;
  }

bool PairMatches(const string symbol,
                 const string wanted_base,
                 const string wanted_profit)
  {
   if(SelectResolved(symbol) == "")
      return false;
   string base = "";
   string profit = "";
   if(!SymbolInfoString(symbol, SYMBOL_CURRENCY_BASE, base) ||
      !SymbolInfoString(symbol, SYMBOL_CURRENCY_PROFIT, profit))
      return false;
   StringToUpper(base);
   StringToUpper(profit);
   return base == wanted_base && profit == wanted_profit;
  }

bool WaitForSymbolSynchronization(const string symbol, const ulong timeout_ms)
  {
   ulong started_ms = GetTickCount64();
   while(!IsStopped() && GetTickCount64() - started_ms < timeout_ms)
     {
      if(TerminalInfoInteger(TERMINAL_CONNECTED) &&
         SymbolIsSynchronized(symbol))
         return true;
      Sleep(250);
     }
   return false;
  }

// Cerca prima nei simboli gia' visibili in Market Watch, poi nell'intero catalogo broker.
// La coppia base/profit permette di risolvere suffissi e prefissi senza una lista specifica
// per broker (EURUSD.raw, EURUSD.x, mEURUSD, ecc.).
string FindMatchingSymbol(const string preferred,
                          const bool selected_only,
                          string &resolution)
  {
   int total = SymbolsTotal(selected_only);
   if(total <= 0)
      return "";
   string low_pref = preferred;
   StringToLower(low_pref);
   string exact = "";
   string wanted_base = "";
   string wanted_profit = "";
   bool has_pair = PreferredPair(preferred, wanted_base, wanted_profit);

   // Il nome esatto non richiede la lettura di proprieta' e viene preferito sempre.
   for(int i = 0; i < total; i++)
     {
      string name = SymbolName(i, selected_only);
      string low = name;
      StringToLower(low);
      if(low == low_pref)
        {
         exact = SelectResolved(name);
         if(exact != "")
           {
            resolution = "exact";
            return exact;
           }
        }
     }

   // Prima restringiamo ai nomi correlati. Solo dopo SymbolSelect leggiamo le proprieta':
   // SymbolInfoString sui simboli non selezionati puo' restituire ERR_MARKET_NOT_SELECTED.
   if(has_pair)
     {
      for(int i = 0; i < total; i++)
        {
         string name = SymbolName(i, selected_only);
         string low = name;
         StringToLower(low);
         if(StringFind(low, low_pref) >= 0 &&
            PairMatches(name, wanted_base, wanted_profit))
           {
            resolution = "currency_pair";
            return name;
           }
        }

      // Copre broker che usano nomi non riconducibili testualmente a EURUSD.
      for(int i = 0; i < total; i++)
        {
         string name = SymbolName(i, selected_only);
         if(PairMatches(name, wanted_base, wanted_profit))
           {
            resolution = "currency_pair";
            return name;
           }
        }
     }

   for(int i = 0; i < total; i++)
     {
      string name = SymbolName(i, selected_only);
      string low = name;
      StringToLower(low);
      if(StringFind(low, low_pref) >= 0)
        {
         string related = SelectResolved(name);
         if(related != "")
           {
            resolution = "name_related";
            return related;
           }
        }
     }
   return "";
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
   // Soltanto se la coppia richiesta non esiste in nessun catalogo usiamo un simbolo neutro
   // per avviare il Bridge; non scegliamo prematuramente il primo elemento di Market Watch.
   if(chosen == "")
     {
      chosen = FirstSelectable(true);
      if(chosen != "")
         resolution = "fallback";
     }
   if(chosen == "")
     {
      chosen = FirstSelectable(false);
      if(chosen != "")
         resolution = "fallback";
     }
   return chosen;
  }

int OnStart()
  {
   ulong started_ms = GetTickCount64();
   string resolved_symbol = "";
   string resolution = "";
   int catalog_total = 0;
   string preferred = ReadPreference();
   Print("TradeJournalDiscovery: avviato; preferenza=", preferred, "; attendo sync account.");
   while(!IsStopped() && GetTickCount64() - started_ms < DISCOVERY_TIMEOUT_MS)
     {
      if(TerminalInfoInteger(TERMINAL_CONNECTED) &&
         AccountInfoInteger(ACCOUNT_LOGIN) > 0)
        {
         catalog_total = SymbolsTotal(false);
         resolved_symbol = ResolveSymbol(preferred, resolution);
         if(resolved_symbol != "")
           {
            if(!WaitForSymbolSynchronization(resolved_symbol,
                                             SYMBOL_SYNC_TIMEOUT_MS))
              {
               Print("TradeJournalDiscovery: simbolo non sincronizzato=",
                     resolved_symbol);
               return 12;
              }
            if(WriteResult(resolved_symbol, preferred, resolution, catalog_total))
              {
               Print("TradeJournalDiscovery: simbolo risolto=", resolved_symbol,
                     "; metodo=", resolution,
                     "; catalogo=", catalog_total,
                     " (scritto su discovered-symbol.json); attendo handoff Bridge.");
               break;
              }
            return 13;
           }
        }
      Sleep(500);
     }
   if(resolved_symbol == "")
     {
      Print("TradeJournalDiscovery: nessun simbolo risolto entro il timeout.");
      return 11;
     }

   // La sincronizzazione investor e' confermata dal runtime prima del marker.
   // Rimaniamo sullo stesso chart per evitare un riavvio MT5 aggiuntivo.
   ulong handoff_started_ms = GetTickCount64();
   while(!IsStopped() &&
         GetTickCount64() - handoff_started_ms < DISCOVERY_TIMEOUT_MS)
     {
      if(BridgeHandoffReady())
        {
         ResetLastError();
         if(ChartApplyTemplate(0, "\\Files\\TradeJournal\\TradeJournalBridge.tpl"))
           {
            Print("TradeJournalDiscovery: handoff al TradeJournalBridge EA richiesto.");
            return 0;
           }
         Print("TradeJournalDiscovery: handoff fallito, errore=", GetLastError(),
               "; nuovo tentativo tra 3 secondi.");
         Sleep(3000);
         continue;
        }
      Sleep(250);
     }
   Print("TradeJournalDiscovery: handoff Bridge non ricevuto entro il timeout.");
   return 14;
  }
