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
#property version   "1.10"
#property strict

#define BASE_DIR "TradeJournal"

//--- Legge il simbolo preferito scritto dal runtime nel sandbox. Default "EURUSD".
string ReadPreference()
  {
   string path = BASE_DIR + "\\symbol-preference.txt";
   int handle = FileOpen(path, FILE_READ | FILE_TXT | FILE_ANSI | FILE_SHARE_READ);
   if(handle == INVALID_HANDLE)
      return "EURUSD";
   string value = "";
   if(!FileIsEnding(handle))
      value = FileReadString(handle);
   FileClose(handle);
   StringTrimLeft(value);
   StringTrimRight(value);
   return (value == "") ? "EURUSD" : value;
  }

//--- Scrittura atomica del risultato: .tmp poi rename su .json.
void WriteSymbol(const string symbol)
  {
   string tmp = BASE_DIR + "\\discovered-symbol.tmp";
   string dst = BASE_DIR + "\\discovered-symbol.json";
   int handle = FileOpen(tmp, FILE_WRITE | FILE_TXT | FILE_ANSI | FILE_SHARE_READ);
   if(handle == INVALID_HANDLE)
      return;
   FileWriteString(handle, "{\"symbol\":\"" + symbol + "\"}");
   FileClose(handle);
   FileMove(tmp, 0, dst, FILE_REWRITE);
  }

//--- Risolve il simbolo reale replicando la logica del vecchio probe Python:
//--- match esatto -> match "contiene" -> primo simbolo selezionabile.
string ResolveSymbol(const string preferred)
  {
   int total = SymbolsTotal(false);
   if(total <= 0)
      return "";
   string low_pref = preferred;
   StringToLower(low_pref);
   string exact = "";
   string related = "";
   for(int i = 0; i < total && exact == ""; i++)
     {
      string name = SymbolName(i, false);
      string low = name;
      StringToLower(low);
      if(low == low_pref)
         exact = name;
      else if(related == "" && StringFind(low, low_pref) >= 0)
         related = name;
     }
   string chosen = (exact != "") ? exact : related;
   if(chosen == "")
      chosen = SymbolName(0, false);
   if(chosen != "" && !SymbolSelect(chosen, true))
     {
      // se il preferito non e' selezionabile, ripiega sul primo che lo e'.
      for(int i = 0; i < total; i++)
        {
         string name = SymbolName(i, false);
         if(SymbolSelect(name, true))
            return name;
        }
      return "";
     }
   return chosen;
  }

void OnStart()
  {
   ulong started_ms = GetTickCount64();
   string preferred = ReadPreference();
   Print("TradeJournalDiscovery: avviato; preferenza=", preferred, "; attendo sync account.");
   while(!IsStopped() && GetTickCount64() - started_ms < 120000)
     {
      if(TerminalInfoInteger(TERMINAL_CONNECTED) &&
         AccountInfoInteger(ACCOUNT_LOGIN) > 0 &&
         SymbolsTotal(false) > 0)
        {
         string symbol = ResolveSymbol(preferred);
         if(symbol != "")
           {
            WriteSymbol(symbol);
            Print("TradeJournalDiscovery: simbolo risolto=", symbol, " (scritto su discovered-symbol.json).");
            return;
           }
        }
      Sleep(500);
     }
   Print("TradeJournalDiscovery: nessun simbolo risolto entro il timeout.");
  }
