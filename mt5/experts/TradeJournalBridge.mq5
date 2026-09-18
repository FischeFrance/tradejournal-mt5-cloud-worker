//+------------------------------------------------------------------------+
//| TradeJournalBridge.mq5                                                  |
//|                                                                          |
//| Expert Advisor di sola lettura per il trade journal.                    |
//|                                                                          |
//| ==== PERCHE' QUESTO EA NON PUO' FARE TRADING (leggere prima) ==========  |
//| Questo file non chiama MAI OrderSend, OrderSendAsync, OrderModify,       |
//| OrderClose, PositionOpen, PositionClose, PositionModify o CTrade::*,    |
//| e non contiene alcun blocco #import di una DLL                           |
//| esterna. L'unico scopo e' leggere lo stato gia' presente nel terminale   |
//| (account, posizioni, ordini, storico) e scriverlo su file JSON dentro    |
//| il sandbox MQL5/Files/TradeJournal. Il Windows Agent locale legge solo   |
//| questi file atomici: non usa il pacchetto Python MetaTrader5, IPC o una  |
//| porta HTTP per ottenere i dati. Questo EA mantiene la garanzia di sola   |
//| lettura anche sul lato MT5. tests/test_mql5_ea_no_trading.py verifica    |
//| staticamente l'assenza di queste chiamate a ogni modifica del file.      |
//| =========================================================================|
//+------------------------------------------------------------------------+
#property copyright "TradeJournal"
#property link      ""
#property version   "1.00"
#property strict
#property description "EA read-only: scrive account/posizioni/ordini/eventi/candele su file JSON. Nessuna funzione di trading."

//--- Parametri configurabili del bridge file Windows nativo.
input int InpTimerSeconds     = 2;        // Intervallo OnTimer in secondi (heartbeat + snapshot)
input int InpBackfillHours    = 168;      // Finestra di backfill storico al primo avvio (ore, 0 = disabilita)
input int InpSnapshotHistoryHours = 87600; // Storico pubblicato nei file deals/history_orders (10 anni)
input int InpCandleBars       = 200;      // Barre storiche complete da pubblicare per timeframe

//--- Directory di output, relativa al sandbox MQL5/Files del terminale (portable mode).
#define BASE_DIR "TradeJournal"
#define FILE_BRIDGE_SCHEMA_VERSION 1

//--- Stato di processo persistito su disco per sopravvivere a un riavvio di EA/terminale.
long g_event_seq     = 0;
bool g_backfill_done = false;
bool g_history_snapshot_written = false;
long g_history_snapshot_sequence = 0;

//--- Identificativo della connessione (UUID non sensibile), letto una volta in OnInit da un file
//--- scritto dal Windows Agent PRIMA di avviare il terminale: l'EA non ha altro modo di
//--- conoscere il connection_id, perche' MQL5 non legge variabili d'ambiente del processo.
//--- Incluso in ogni event_id per garantire unicita'
//--- anche fra connessioni/account diversi con ticket numericamente coincidenti.
string g_connection_id = "unknown-connection";
bool   g_new_only      = false;
ulong  g_new_only_started_ms = 0;
const ulong NEW_ONLY_STARTUP_GRACE_MS = 5000;

// OnTradeTransaction puo' arrivare qualche millisecondo prima che il deal sia leggibile tramite
// HistoryDealSelect. Conserviamo i ticket non ancora pubblicabili e li riproviamo dal timer: il
// ticket e' deduplicato in memoria e l'event_id resta deterministico, quindi anche un errore di
// scrittura successivo alla costruzione del payload non puo' creare duplicati remoti.
const int MAX_PENDING_DEAL_EVENTS = 256;
ulong g_pending_deal_tickets[];
int   g_pending_deal_attempts[];

//--- Le 6 timeframe pubblicate in file separati candles/<symbol>-<timeframe>.json.
string           TIMEFRAME_NAMES[6]  = {"M1", "M5", "M15", "H1", "H4", "D1"};
ENUM_TIMEFRAMES  TIMEFRAME_VALUES[6] = {PERIOD_M1, PERIOD_M5, PERIOD_M15, PERIOD_H1, PERIOD_H4, PERIOD_D1};

void WriteInitMarker(const string state)
  {
   int handle = FileOpen(BASE_DIR + "\\init-state.tmp",
                         FILE_WRITE | FILE_TXT | FILE_ANSI, 0, CP_UTF8);
   if(handle == INVALID_HANDLE)
     {
      Print("TradeJournalBridge: init marker non scrivibile, stato=", state,
            ", errore=", GetLastError());
      return;
     }
   FileWriteString(handle, state);
   FileFlush(handle);
   FileClose(handle);
   FileDelete(BASE_DIR + "\\init-state.txt");
   if(!FileMove(BASE_DIR + "\\init-state.tmp", 0,
                BASE_DIR + "\\init-state.txt", FILE_REWRITE))
      Print("TradeJournalBridge: init marker non pubblicato, stato=", state,
            ", errore=", GetLastError());
  }

// Il terminale generico ricrea il profilo Default con piu' grafici quando un profilo
// personalizzato vuoto viene aperto. Il bridge mantiene esclusivamente il grafico sul quale e'
// stato caricato: ChartFirst/ChartNext enumerano soltanto i grafici di questa istanza MT5 e
// ChartClose non coinvolge altri processi o account.
bool CloseOtherCharts()
  {
   long current_chart = ChartID();
   long chart = ChartFirst();
   int requested = 0;
   while(chart >= 0)
     {
      long next_chart = ChartNext(chart);
      if(chart != current_chart)
        {
         ResetLastError();
         if(!ChartClose(chart))
           {
            Print("TradeJournalBridge: chiusura grafico aggiuntivo fallita, errore=",
                  GetLastError());
            return false;
           }
         requested++;
        }
      chart = next_chart;
     }
   if(requested > 0)
      Print("TradeJournalBridge: richiusa area di lavoro a un solo grafico; grafici rimossi=",
            requested, ".");
   return true;
  }

//+------------------------------------------------------------------------+
//| Utility JSON minime (scopo specifico, non un parser/serializzatore     |
//| generico: MQL5 non ha una libreria JSON in standard library e questo   |
//| EA scrive solo un numero fisso di schemi noti).                        |
//+------------------------------------------------------------------------+
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

string JsonString(const string text)
  {
   return "\"" + JsonEscape(text) + "\"";
  }

string JsonNumber(const double value)
  {
   if(!MathIsValidNumber(value))
      return "0"; // difensivo: un numero non finito non deve mai rompere il JSON prodotto
   // Preserve the MT5 double instead of rounding every ledger row to five decimals.  Per-row
   // rounding accumulates when an opening balance is reconstructed backwards over a long
   // history and is especially visible on crypto-denominated accounts.
   return DoubleToString(value, 16);
  }

// Ogni file del protocollo nativo usa la stessa envelope. Login/server sono identita' di
// attribuzione, non credenziali; password e token non sono mai letti ne' serializzati dall'EA.
string BuildEnvelope(const string payload, const long sequence)
  {
   string login = IntegerToString(AccountInfoInteger(ACCOUNT_LOGIN));
   string server = AccountInfoString(ACCOUNT_SERVER);
   string json = "{";
   json += "\"schema_version\":" + IntegerToString(FILE_BRIDGE_SCHEMA_VERSION) + ",";
   json += "\"generated_at\":" + JsonString(Iso8601FromDatetime(TimeGMT())) + ",";
   json += "\"sequence\":" + IntegerToString(sequence) + ",";
   json += "\"account_identity\":{\"login\":" + JsonString(login) + ",\"server\":" + JsonString(server) + "},";
   json += "\"server_identity\":" + JsonString(server) + ",";
   json += "\"payload\":" + payload;
   json += "}";
   return json;
  }

// Compatibilita' wire: il suffisso Z resta nel formato storico del bridge. Per TimeCurrent e
// DEAL_TIME il valore appartiene pero' al dominio temporale del server broker, non a UTC. Il
// worker conserva quindi time_msc solo per identita'/audit e marca il time_basis come irrisolto.
string Iso8601FromDatetime(const datetime value)
  {
   string s = TimeToString(value, TIME_DATE | TIME_SECONDS); // "yyyy.mm.dd hh:mi:ss"
   StringReplace(s, ".", "-");
   StringReplace(s, " ", "T");
   return s + "Z";
  }

string EntryToString(const long entry_raw)
  {
   if(entry_raw == (long)DEAL_ENTRY_IN)    return "IN";
   if(entry_raw == (long)DEAL_ENTRY_OUT)   return "OUT";
   if(entry_raw == (long)DEAL_ENTRY_INOUT) return "INOUT";
   if(entry_raw == (long)DEAL_ENTRY_OUT_BY)return "OUT_BY";
   return "UNKNOWN";
  }

string DirectionFromType(const long mt5_type)
  {
   // Enum MT5: BUY/BUY_LIMIT/BUY_STOP/BUY_STOP_LIMIT sono pari, i corrispondenti SELL sono
   // dispari (0/2/4/6 vs 1/3/5/7).
   return (mt5_type % 2 == 0) ? "buy" : "sell";
  }

// Il file connection_id e' scritto dal runtime Windows (contenuto non sensibile: solo un UUID
// di connessione) sotto BASE_DIR PRIMA che il terminale venga avviato, cosi' e' gia' presente al
// primo OnInit. Un fallback esplicito ("unknown-connection", mai vuoto) evita che un file
// mancante o illeggibile produca un event_id con un campo vuoto/ambiguo.
string ReadConnectionId()
  {
   string path = BASE_DIR + "\\connection_id";
   if(!FileIsExist(path))
     {
      Print("TradeJournalBridge: connection_id non trovato, uso 'unknown-connection'.");
      return "unknown-connection";
     }
   int handle = FileOpen(path, FILE_READ | FILE_TXT | FILE_ANSI | FILE_SHARE_READ, 0, CP_UTF8);
   if(handle == INVALID_HANDLE)
     {
      Print("TradeJournalBridge: connection_id illeggibile, uso 'unknown-connection'.");
      return "unknown-connection";
     }
   string content = "";
   while(!FileIsEnding(handle))
      content += FileReadString(handle);
   FileClose(handle);
   StringTrimLeft(content);
   StringTrimRight(content);
   return (content == "") ? "unknown-connection" : content;
  }

// La modalita' new_only deve diventare pronta senza scaricare storico. Il Windows Agent scrive
// questo flag non sensibile prima dell'avvio; le altre modalita' mantengono il comportamento
// storico completo e verranno ottimizzate separatamente.
bool ReadNewOnlyMode()
  {
   string path = BASE_DIR + "\\history_mode";
   if(!FileIsExist(path))
      return false;
   int handle = FileOpen(path, FILE_READ | FILE_TXT | FILE_ANSI | FILE_SHARE_READ, 0, CP_UTF8);
   if(handle == INVALID_HANDLE)
      return false;
   string content = "";
   while(!FileIsEnding(handle))
      content += FileReadString(handle);
   FileClose(handle);
   StringTrimLeft(content);
   StringTrimRight(content);
   return content == "new_only";
  }

//+------------------------------------------------------------------------+
//| Scrittura atomica: file.tmp poi rename sul nome finale. Nessuno dei    |
//| lettori Windows (Mql5FileMt5Adapter) puo' mai osservare un file a      |
//| meta'.                                                                  |
//+------------------------------------------------------------------------+
bool WriteJsonAtomic(const string relative_name, const string json_text)
  {
   string tmp_path   = BASE_DIR + "\\" + relative_name + ".tmp";
   string final_path = BASE_DIR + "\\" + relative_name;

   int handle = FileOpen(tmp_path, FILE_WRITE | FILE_TXT | FILE_ANSI | FILE_SHARE_READ, 0, CP_UTF8);
   if(handle == INVALID_HANDLE)
     {
      Print("TradeJournalBridge: impossibile aprire ", tmp_path, " errore=", GetLastError());
      return false;
     }
   FileWriteString(handle, json_text);
   FileFlush(handle);
   FileClose(handle);

   if(!FileMove(tmp_path, 0, final_path, FILE_REWRITE))
     {
      Print("TradeJournalBridge: rename atomico fallito per ", final_path, " errore=", GetLastError());
      return false;
     }
   return true;
  }

// Ogni evento e' un file indipendente in events/. A differenza di un log append-only puo'
// essere pubblicato con lo stesso rename atomico degli snapshot: il lettore non osserva mai una
// riga parziale e il nome basato sulla sequenza resta stabile dopo il riavvio grazie a cursor.json.
bool WriteEventAtomic(const string payload)
  {
   if(payload == "")
      return false; // la sequenza non e' stata riservata in modo durevole
   string relative_name = "events\\event-" + IntegerToString(g_event_seq) + ".json";
   return WriteJsonAtomic(relative_name, BuildEnvelope(payload, g_event_seq));
  }

//+------------------------------------------------------------------------+
//| Cursore persistente (event_seq, backfill_done): schema fisso a due     |
//| campi, quindi un estrattore ad-hoc e' preferibile a un parser JSON      |
//| generico che l'MQL5 standard non fornisce.                             |
//+------------------------------------------------------------------------+
long ExtractJsonLong(const string json, const string key, const long default_value)
  {
   string needle = "\"" + key + "\":";
   int pos = StringFind(json, needle);
   if(pos < 0)
      return default_value;
   pos += StringLen(needle);
   int len = StringLen(json);
   int start = pos;
   while(pos < len)
     {
      ushort c = StringGetCharacter(json, pos);
      if((c >= '0' && c <= '9') || c == '-')
        {
         pos++;
         continue;
        }
      break;
     }
   if(pos == start)
      return default_value;
   return StringToInteger(StringSubstr(json, start, pos - start));
  }

bool ExtractJsonBool(const string json, const string key, const bool default_value)
  {
   if(StringFind(json, "\"" + key + "\":true") >= 0)
      return true;
   if(StringFind(json, "\"" + key + "\":false") >= 0)
      return false;
   return default_value;
  }

void LoadCursorState()
  {
   string path = BASE_DIR + "\\cursor.json";
   if(!FileIsExist(path))
      return; // primo avvio in assoluto: restano i default (event_seq=0, backfill_done=false)

   int handle = FileOpen(path, FILE_READ | FILE_TXT | FILE_ANSI | FILE_SHARE_READ, 0, CP_UTF8);
   if(handle == INVALID_HANDLE)
      return;
   string content = "";
   while(!FileIsEnding(handle))
      content += FileReadString(handle);
   FileClose(handle);

   g_event_seq     = ExtractJsonLong(content, "event_seq", 0);
   g_backfill_done = ExtractJsonBool(content, "backfill_done", false);
  }

bool SaveCursorState()
  {
   string json = "{\"event_seq\":" + IntegerToString(g_event_seq) +
                 ",\"backfill_done\":" + (g_backfill_done ? "true" : "false") + "}";
   return WriteJsonAtomic("cursor.json", json);
  }

//+------------------------------------------------------------------------+
//| Costruzione degli snapshot completi (account/posizioni/ordini/candele) |
//+------------------------------------------------------------------------+
string BuildHeartbeatJson(const long sequence)
  {
   string json = "{";
   json += "\"generated_at\":" + JsonString(Iso8601FromDatetime(TimeCurrent())) + ",";
   json += "\"sequence\":" + IntegerToString(sequence) + ",";
   json += "\"history_mode\":" + JsonString(g_new_only ? "new_only" : "history") + ",";
   json += "\"terminal_connected\":" + (TerminalInfoInteger(TERMINAL_CONNECTED) ? "true" : "false") + ",";
   json += "\"account_trade_allowed\":" + (AccountInfoInteger(ACCOUNT_TRADE_ALLOWED) ? "true" : "false");
   json += "}";
   return json;
  }

string BuildAccountJson()
  {
   long   login    = AccountInfoInteger(ACCOUNT_LOGIN);
   string server   = AccountInfoString(ACCOUNT_SERVER);
   double balance  = AccountInfoDouble(ACCOUNT_BALANCE);
   double credit   = AccountInfoDouble(ACCOUNT_CREDIT);
   double equity   = AccountInfoDouble(ACCOUNT_EQUITY);
   string currency = AccountInfoString(ACCOUNT_CURRENCY);
   long   leverage = AccountInfoInteger(ACCOUNT_LEVERAGE);

   // NB: login/server qui NON sono mascherati: il file rimane nella directory portable privata
   // dell'istanza ed e' letto localmente dall'Agent, che richiede entrambi i campi per verificare
   // l'identita' dell'account prima di attribuire eventi.
   string json = "{";
   json += "\"login\":" + JsonString(IntegerToString(login)) + ",";
   json += "\"server\":" + JsonString(server) + ",";
   json += "\"balance\":" + JsonNumber(balance) + ",";
   json += "\"credit\":" + JsonNumber(credit) + ",";
   json += "\"equity\":" + JsonNumber(equity) + ",";
   json += "\"currency\":" + JsonString(currency) + ",";
   json += "\"leverage\":" + IntegerToString(leverage) + ",";
   json += "\"trade_allowed\":" + (AccountInfoInteger(ACCOUNT_TRADE_ALLOWED) ? "true" : "false");
   json += "}";
   return json;
  }

string BuildPositionsJson()
  {
   string json = "[";
   int total = PositionsTotal();
   bool first = true;
   for(int i = 0; i < total; i++)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0)
         continue;
      long   position_id  = PositionGetInteger(POSITION_IDENTIFIER);
      string symbol       = PositionGetString(POSITION_SYMBOL);
      long   type         = PositionGetInteger(POSITION_TYPE);
      double volume       = PositionGetDouble(POSITION_VOLUME);
      double open_price   = PositionGetDouble(POSITION_PRICE_OPEN);
      double sl           = PositionGetDouble(POSITION_SL);
      double tp           = PositionGetDouble(POSITION_TP);
      double current_price = type == POSITION_TYPE_BUY
                             ? SymbolInfoDouble(symbol, SYMBOL_BID)
                             : SymbolInfoDouble(symbol, SYMBOL_ASK);
      double floating_profit = PositionGetDouble(POSITION_PROFIT);
      double point         = SymbolInfoDouble(symbol, SYMBOL_POINT);
      long   digits        = SymbolInfoInteger(symbol, SYMBOL_DIGITS);
      datetime open_time  = (datetime)PositionGetInteger(POSITION_TIME);

      if(!first)
         json += ",";
      first = false;
      json += "{";
      json += "\"ticket\":" + JsonString(IntegerToString((long)ticket)) + ",";
      json += "\"position_id\":" + JsonString(IntegerToString(position_id)) + ",";
      json += "\"symbol\":" + JsonString(symbol) + ",";
      json += "\"direction\":" + JsonString(DirectionFromType(type)) + ",";
      json += "\"volume\":" + JsonNumber(volume) + ",";
      json += "\"open_price\":" + JsonNumber(open_price) + ",";
      json += "\"stop_loss\":" + JsonNumber(sl) + ",";
      json += "\"take_profit\":" + JsonNumber(tp) + ",";
      json += "\"current_price\":" + JsonNumber(current_price) + ",";
      json += "\"floating_profit\":" + JsonNumber(floating_profit) + ",";
      json += "\"point\":" + JsonNumber(point) + ",";
      json += "\"digits\":" + IntegerToString(digits) + ",";
      json += "\"open_time\":" + JsonString(Iso8601FromDatetime(open_time));
      json += "}";
     }
   json += "]";
   return json;
  }

string BuildOrdersJson()
  {
   string json = "[";
   int total = OrdersTotal();
   bool first = true;
   for(int i = 0; i < total; i++)
     {
      ulong ticket = OrderGetTicket(i);
      if(ticket == 0)
         continue;
      string symbol = OrderGetString(ORDER_SYMBOL);
      long   type   = OrderGetInteger(ORDER_TYPE);
      double volume = OrderGetDouble(ORDER_VOLUME_CURRENT);
      double price  = OrderGetDouble(ORDER_PRICE_OPEN);
      double sl     = OrderGetDouble(ORDER_SL);
      double tp     = OrderGetDouble(ORDER_TP);

      if(!first)
         json += ",";
      first = false;
      json += "{";
      json += "\"ticket\":" + JsonString(IntegerToString((long)ticket)) + ",";
      json += "\"symbol\":" + JsonString(symbol) + ",";
      json += "\"direction\":" + JsonString(DirectionFromType(type)) + ",";
      json += "\"volume\":" + JsonNumber(volume) + ",";
      json += "\"price\":" + JsonNumber(price) + ",";
      json += "\"stop_loss\":" + JsonNumber(sl) + ",";
      json += "\"take_profit\":" + JsonNumber(tp) + ",";
      json += "\"order_type\":" + IntegerToString(type);
      json += "}";
     }
   json += "]";
   return json;
  }

// Snapshot storico separato dagli ordini attivi: HistorySync lo usa per importare anche ordini
// chiusi senza confondere lo stato live letto da LiveSync.
string BuildHistoryOrdersJson()
  {
   datetime to_time = TimeCurrent();
   datetime from_time = 0; // la finestra richiesta viene applicata dall'Agent
   string json = "[";
   bool first = true;
   if(!HistorySelect(from_time, to_time))
      return json + "]";
   int total = HistoryOrdersTotal();
   for(int i = 0; i < total; i++)
     {
      ulong ticket = HistoryOrderGetTicket(i);
      if(ticket == 0)
         continue;
      if(!first)
         json += ",";
      first = false;
      json += "{";
      json += "\"ticket\":" + JsonString(IntegerToString((long)ticket)) + ",";
      json += "\"symbol\":" + JsonString(HistoryOrderGetString(ticket, ORDER_SYMBOL)) + ",";
      json += "\"volume_current\":" + JsonNumber(HistoryOrderGetDouble(ticket, ORDER_VOLUME_CURRENT)) + ",";
      json += "\"type\":" + IntegerToString(HistoryOrderGetInteger(ticket, ORDER_TYPE)) + ",";
      json += "\"price_open\":" + JsonNumber(HistoryOrderGetDouble(ticket, ORDER_PRICE_OPEN)) + ",";
      json += "\"sl\":" + JsonNumber(HistoryOrderGetDouble(ticket, ORDER_SL)) + ",";
      json += "\"tp\":" + JsonNumber(HistoryOrderGetDouble(ticket, ORDER_TP)) + ",";
      json += "\"time\":" + JsonString(Iso8601FromDatetime((datetime)HistoryOrderGetInteger(ticket, ORDER_TIME_DONE)));
      json += "}";
     }
   return json + "]";
  }

// Il ledger storico e' il confine di consistenza del bundle history. Lo catturiamo prima di
// account/posizioni/ordini e lo confrontiamo nuovamente subito prima del heartbeat: se MT5
// cambia mentre gli altri file vengono pubblicati, il nuovo bundle resta senza commit e il
// Windows Agent continua a leggere l'ultimo heartbeat coerente.
struct HistoryLedgerAnchor
  {
   bool     coherent;
   int      deal_count;
   long     last_deal_ticket;
   long     last_deal_time_msc;
   double   balance;
   double   credit;
   datetime as_of;
  };

void ResetHistoryLedgerAnchor(HistoryLedgerAnchor &anchor)
  {
   anchor.coherent           = false;
   anchor.deal_count         = 0;
   anchor.last_deal_ticket   = 0;
   anchor.last_deal_time_msc = 0;
   anchor.balance            = 0.0;
   anchor.credit             = 0.0;
   anchor.as_of              = 0;
  }

bool RevalidateHistoryLedgerAnchor(HistoryLedgerAnchor &anchor)
  {
   if(!anchor.coherent)
      return false;

   // La coppia (ultimo time_msc, ultimo ticket) e il count sono l'high-water mark del ledger
   // nel suo ordine MT5. Rieseguiamo HistorySelect dopo la scrittura degli altri snapshot,
   // cosi' nessuna variazione economica puo' ricevere il nuovo heartbeat per errore.
   if(!HistorySelect(0, TimeCurrent()))
      return false;

   int total = HistoryDealsTotal();
   int count = 0;
   long last_ticket = 0;
   long last_time_msc = 0;
   for(int i = 0; i < total; i++)
     {
      ulong ticket = HistoryDealGetTicket(i);
      if(ticket == 0)
         return false;
      count++;
      last_ticket = (long)ticket;
      last_time_msc = HistoryDealGetInteger(ticket, DEAL_TIME_MSC);
     }

   double balance = AccountInfoDouble(ACCOUNT_BALANCE);
   double credit = AccountInfoDouble(ACCOUNT_CREDIT);
   return count == total &&
          count == anchor.deal_count &&
          last_ticket == anchor.last_deal_ticket &&
          last_time_msc == anchor.last_deal_time_msc &&
          MathAbs(balance - anchor.balance) < 0.000001 &&
          MathAbs(credit - anchor.credit) < 0.000001;
  }

string BuildDealsJson(HistoryLedgerAnchor &anchor)
  {
   ResetHistoryLedgerAnchor(anchor);
   datetime to_time = TimeCurrent();
   datetime from_time = 0; // ledger completo: cash flow e posizioni sovrapposte inclusi
   double balance_before = AccountInfoDouble(ACCOUNT_BALANCE);
   double credit_before = AccountInfoDouble(ACCOUNT_CREDIT);
   string deals = "[";
   bool first = true;
   if(!HistorySelect(from_time, to_time))
      return "{\"anchor\":{\"balance\":" + JsonNumber(balance_before) +
             ",\"credit\":" + JsonNumber(credit_before) +
             ",\"as_of\":" + JsonString(Iso8601FromDatetime(to_time)) +
             ",\"coherent\":false,\"order_basis\":\"mt5_history_index_v1\"" +
             ",\"time_basis\":\"broker_server_unresolved\",\"deal_count\":0" +
             "},\"deals\":[]}";
   int total = HistoryDealsTotal();
   int exported_total = 0;
   long selected_last_ticket = 0;
   long selected_last_time_msc = 0;
   for(int i = 0; i < total; i++)
     {
      ulong ticket = HistoryDealGetTicket(i);
      if(ticket == 0)
         continue;
      long deal_time_msc = HistoryDealGetInteger(ticket, DEAL_TIME_MSC);
      selected_last_time_msc = deal_time_msc;
      selected_last_ticket = (long)ticket;
      if(!first)
         deals += ",";
      first = false;
      deals += "{";
      deals += "\"history_index\":" + IntegerToString(exported_total) + ",";
      deals += "\"ticket\":" + JsonString(IntegerToString((long)ticket)) + ",";
      deals += "\"position_id\":" + JsonString(IntegerToString((long)HistoryDealGetInteger(ticket, DEAL_POSITION_ID))) + ",";
      deals += "\"order_id\":" + JsonString(IntegerToString((long)HistoryDealGetInteger(ticket, DEAL_ORDER))) + ",";
      deals += "\"symbol\":" + JsonString(HistoryDealGetString(ticket, DEAL_SYMBOL)) + ",";
      long deal_type = HistoryDealGetInteger(ticket, DEAL_TYPE);
      long entry_raw = HistoryDealGetInteger(ticket, DEAL_ENTRY);
      deals += "\"deal_type\":" + IntegerToString(deal_type) + ",";
      deals += "\"direction\":" + JsonString(deal_type == DEAL_TYPE_SELL ? "sell" : "buy") + ",";
      deals += "\"entry\":" + JsonString(EntryToString(entry_raw)) + ",";
      deals += "\"volume\":" + JsonNumber(HistoryDealGetDouble(ticket, DEAL_VOLUME)) + ",";
      deals += "\"price\":" + JsonNumber(HistoryDealGetDouble(ticket, DEAL_PRICE)) + ",";
      deals += "\"profit\":" + JsonNumber(HistoryDealGetDouble(ticket, DEAL_PROFIT)) + ",";
      deals += "\"commission\":" + JsonNumber(HistoryDealGetDouble(ticket, DEAL_COMMISSION)) + ",";
      deals += "\"swap\":" + JsonNumber(HistoryDealGetDouble(ticket, DEAL_SWAP)) + ",";
      deals += "\"fee\":" + JsonNumber(HistoryDealGetDouble(ticket, DEAL_FEE)) + ",";
      deals += "\"time_msc\":" + IntegerToString(deal_time_msc) + ",";
      deals += "\"time\":" + JsonString(Iso8601FromDatetime((datetime)HistoryDealGetInteger(ticket, DEAL_TIME)));
      deals += "}";
      exported_total++;
     }
   deals += "]";
   double balance_mid = AccountInfoDouble(ACCOUNT_BALANCE);
   double credit_mid = AccountInfoDouble(ACCOUNT_CREDIT);
   datetime verified_at = TimeCurrent();
   bool verified = HistorySelect(from_time, verified_at);
   int verified_total = verified ? HistoryDealsTotal() : -1;
   int verified_count = 0;
   long verified_last_ticket = 0;
   long verified_last_time_msc = 0;
   if(verified && verified_total > 0)
     {
      for(int verify_index = 0; verify_index < verified_total; verify_index++)
        {
         ulong verified_ticket = HistoryDealGetTicket(verify_index);
         if(verified_ticket == 0)
            continue;
         long verified_time_msc = HistoryDealGetInteger(verified_ticket, DEAL_TIME_MSC);
         verified_last_ticket = (long)verified_ticket;
         verified_last_time_msc = verified_time_msc;
         verified_count++;
        }
     }
   double balance_after = AccountInfoDouble(ACCOUNT_BALANCE);
   double credit_after = AccountInfoDouble(ACCOUNT_CREDIT);
   bool coherent = verified && exported_total == total &&
                   verified_count == verified_total && verified_count == exported_total &&
                   verified_last_ticket == selected_last_ticket &&
                   verified_last_time_msc == selected_last_time_msc &&
                   MathAbs(balance_before - balance_mid) < 0.000001 &&
                   MathAbs(balance_mid - balance_after) < 0.000001 &&
                   MathAbs(credit_before - credit_mid) < 0.000001 &&
                   MathAbs(credit_mid - credit_after) < 0.000001;
   anchor.coherent = coherent;
   anchor.deal_count = exported_total;
   anchor.last_deal_ticket = selected_last_ticket;
   anchor.last_deal_time_msc = selected_last_time_msc;
   anchor.balance = balance_after;
   anchor.credit = credit_after;
   anchor.as_of = verified_at;
   return "{\"anchor\":{\"balance\":" + JsonNumber(anchor.balance) +
          ",\"credit\":" + JsonNumber(anchor.credit) +
          ",\"as_of\":" + JsonString(Iso8601FromDatetime(anchor.as_of)) +
          ",\"coherent\":" + (anchor.coherent ? "true" : "false") +
          ",\"order_basis\":\"mt5_history_index_v1\"" +
          ",\"time_basis\":\"broker_server_unresolved\"" +
          ",\"deal_count\":" + IntegerToString(anchor.deal_count) +
          ",\"last_deal_ticket\":" + JsonString(IntegerToString(anchor.last_deal_ticket)) +
          ",\"last_deal_time_msc\":" + IntegerToString(anchor.last_deal_time_msc) +
          "},\"deals\":" + deals + "}";
  }

string BuildCandlesJson(const int timeframe_index)
  {
   string symbol = _Symbol; // simbolo del grafico su cui e' agganciato l'EA (Symbol= in startup.ini)
   datetime now = TimeCurrent();
   MqlRates rates[];
   int copied = CopyRates(symbol, TIMEFRAME_VALUES[timeframe_index], 0, InpCandleBars + 1, rates);
   int period_seconds = PeriodSeconds(TIMEFRAME_VALUES[timeframe_index]);
   string json = "[";
   bool first = true;
   for(int i = 0; i < copied; i++)
     {
      if(rates[i].time + period_seconds > now)
         continue; // candela ancora in formazione: mai pubblicata
      if(!first)
         json += ",";
      first = false;
      json += "{";
      json += "\"open_time\":" + JsonString(Iso8601FromDatetime(rates[i].time)) + ",";
      json += "\"open\":" + JsonString(DoubleToString(rates[i].open, 5)) + ",";
      json += "\"high\":" + JsonString(DoubleToString(rates[i].high, 5)) + ",";
      json += "\"low\":" + JsonString(DoubleToString(rates[i].low, 5)) + ",";
      json += "\"close\":" + JsonString(DoubleToString(rates[i].close, 5)) + ",";
      json += "\"tick_volume\":" + IntegerToString((long)rates[i].tick_volume) + ",";
      json += "\"spread\":" + IntegerToString(rates[i].spread) + ",";
      json += "\"source\":\"mt5\"";
      json += "}";
     }
   json += "]";
   return json;
  }

//+------------------------------------------------------------------------+
//| Un unico payload evento. Campi non applicabili al tipo restano null,   |
//| mai omessi (schema stabile); WriteEventAtomic aggiunge l'envelope.     |
//+------------------------------------------------------------------------+
string BuildEventJson(const string event_type, const long ticket, const long position_id,
                       const long order_id, const long deal_id, const string symbol,
                       const string direction, const double volume, const double price,
                       const double stop_loss, const double take_profit, const double profit,
                       const double commission, const double swap, const long magic,
                       const string comment, const string entry, const datetime event_time,
                       long timestamp_msc)
  {
   // Riserva la sequenza su disco PRIMA di pubblicare event-N.json. In caso di crash fra le due
   // operazioni resta al massimo un gap (accettato dal consumer), mai il riuso di N con
   // sovrascrittura di un evento non ancora acquisito.
   g_event_seq++;
   if(!SaveCursorState())
     {
      g_event_seq--;
      Print("TradeJournalBridge: prenotazione durevole sequenza evento fallita.");
      return "";
     }
   if(timestamp_msc <= 0)
      timestamp_msc = (long)event_time * 1000; // fallback se la proprieta' _MSC non e' disponibile

   long   login  = AccountInfoInteger(ACCOUNT_LOGIN);
   string server = AccountInfoString(ACCOUNT_SERVER);
   double account_balance = AccountInfoDouble(ACCOUNT_BALANCE);
   double account_equity = AccountInfoDouble(ACCOUNT_EQUITY);
   string account_currency = AccountInfoString(ACCOUNT_CURRENCY);
   long   account_leverage = AccountInfoInteger(ACCOUNT_LEVERAGE);
   double balance_before_open = account_balance - profit - commission - swap;

   // Composito e deterministico: connection_id + login + server + tipo + ticket + timestamp_msc.
   // Due connessioni/account diversi non possono mai produrre lo stesso event_id anche con
   // ticket numericamente coincidenti (broker/demo differenti): questo e' il requisito che
   // sostituisce la vecchia deduplica "solo per deal_ticket": l'adapter Windows usa
   // (connection_id, login, server, ticket) come identita', non il solo ticket.
   string event_id = g_connection_id + "|" + IntegerToString(login) + "|" + server + "|" +
                      event_type + "|" + IntegerToString(ticket) + "|" +
                      IntegerToString(timestamp_msc) + "|" + IntegerToString(g_event_seq);

   string json = "{";
   json += "\"event_id\":" + JsonString(event_id) + ",";
   json += "\"connection_id\":" + JsonString(g_connection_id) + ",";
   json += "\"login\":" + JsonString(IntegerToString(login)) + ",";
   json += "\"server\":" + JsonString(server) + ",";
   json += "\"timestamp_msc\":" + IntegerToString(timestamp_msc) + ",";
   json += "\"event_type\":" + JsonString(event_type) + ",";
   json += "\"ticket\":" + JsonString(IntegerToString(ticket)) + ",";
   json += "\"position_id\":" + (position_id > 0 ? JsonString(IntegerToString(position_id)) : "null") + ",";
   json += "\"order_id\":" + (order_id > 0 ? JsonString(IntegerToString(order_id)) : "null") + ",";
   json += "\"deal_id\":" + (deal_id > 0 ? JsonString(IntegerToString(deal_id)) : "null") + ",";
   json += "\"symbol\":" + JsonString(symbol) + ",";
   json += "\"direction\":" + (direction == "" ? "null" : JsonString(direction)) + ",";
   json += "\"volume\":" + JsonNumber(volume) + ",";
   json += "\"price\":" + JsonNumber(price) + ",";
   json += "\"stop_loss\":" + JsonNumber(stop_loss) + ",";
   json += "\"take_profit\":" + JsonNumber(take_profit) + ",";
   json += "\"profit\":" + JsonNumber(profit) + ",";
   json += "\"commission\":" + JsonNumber(commission) + ",";
   json += "\"swap\":" + JsonNumber(swap) + ",";
   json += "\"balance\":" + JsonNumber(account_balance) + ",";
   json += "\"equity\":" + JsonNumber(account_equity) + ",";
   json += "\"currency\":" + JsonString(account_currency) + ",";
   json += "\"leverage\":" + IntegerToString(account_leverage) + ",";
   json += "\"balance_before_open\":" +
           (event_type == "DEAL_ADD" && entry == "IN" && balance_before_open > 0.0
              ? JsonNumber(balance_before_open)
              : "null") + ",";
   json += "\"magic\":" + IntegerToString(magic) + ",";
   json += "\"comment\":" + JsonString(comment) + ",";
   json += "\"entry\":" + (entry == "" ? "null" : JsonString(entry)) + ",";
   json += "\"time\":" + JsonString(Iso8601FromDatetime(event_time));
   json += "}";
   return json;
  }

//+------------------------------------------------------------------------+
//| Emettitori per singolo tipo di transazione. Ognuno legge lo stato gia' |
//| disponibile via le funzioni Get* (nessuna chiamata di trading, nessuna |
//| scrittura bloccante oltre a un append con FileFlush) e ognuno e'       |
//| autosufficiente: OnTradeTransaction non assume alcun ordine di arrivo  |
//| tra i tipi di evento, ogni emettitore rilegge lo stato corrente dal    |
//| ticket ricevuto invece di fidarsi di uno stato accumulato in memoria.  |
//+------------------------------------------------------------------------+
bool EmitDealAddEvent(const ulong deal_ticket)
  {
   if(!HistoryDealSelect(deal_ticket))
      return false; // il deal potrebbe non essere ancora visibile nella cache storica

   long     position_id = (long)HistoryDealGetInteger(deal_ticket, DEAL_POSITION_ID);
   long     order_id     = (long)HistoryDealGetInteger(deal_ticket, DEAL_ORDER);
   string   symbol       = HistoryDealGetString(deal_ticket, DEAL_SYMBOL);
   long     deal_type    = HistoryDealGetInteger(deal_ticket, DEAL_TYPE);
   long     entry_raw    = HistoryDealGetInteger(deal_ticket, DEAL_ENTRY);
   double   volume       = HistoryDealGetDouble(deal_ticket, DEAL_VOLUME);
   double   price        = HistoryDealGetDouble(deal_ticket, DEAL_PRICE);
   double   profit       = HistoryDealGetDouble(deal_ticket, DEAL_PROFIT);
   // Il contratto remoto espone un solo costo: somma commissione e DEAL_FEE.
   // Lo snapshot storico mantiene comunque entrambi i valori originali per audit.
   double   commission   = HistoryDealGetDouble(deal_ticket, DEAL_COMMISSION) +
                           HistoryDealGetDouble(deal_ticket, DEAL_FEE);
   double   swap         = HistoryDealGetDouble(deal_ticket, DEAL_SWAP);
   long     magic        = HistoryDealGetInteger(deal_ticket, DEAL_MAGIC);
   string   comment      = HistoryDealGetString(deal_ticket, DEAL_COMMENT);
   datetime event_time   = (datetime)HistoryDealGetInteger(deal_ticket, DEAL_TIME);
   long     timestamp_msc = HistoryDealGetInteger(deal_ticket, DEAL_TIME_MSC);

   // direction qui e' solo informativo (il tipo di deal, buy/sell): non e' usato dal bridge per
   // filtrare, che si basa esclusivamente su "entry" per ricostruire i deal di chiusura.
   string direction = (deal_type == DEAL_TYPE_SELL) ? "sell" : "buy";
   string entry = EntryToString(entry_raw);

   string line = BuildEventJson("DEAL_ADD", (long)deal_ticket, position_id, order_id, (long)deal_ticket,
                                 symbol, direction, volume, price, 0.0, 0.0,
                                 profit, commission, swap, magic, comment, entry, event_time,
                                 timestamp_msc);
   return WriteEventAtomic(line);
  }

bool QueuePendingDealEvent(const ulong deal_ticket)
  {
   if(deal_ticket == 0)
      return false;

   int pending_total = ArraySize(g_pending_deal_tickets);
   for(int i = 0; i < pending_total; i++)
     {
      if(g_pending_deal_tickets[i] == deal_ticket)
         return true;
     }

   if(pending_total >= MAX_PENDING_DEAL_EVENTS)
     {
      PrintFormat("TradeJournalBridge: coda retry deal piena, ticket %I64u non accodato.",
                  deal_ticket);
      return false;
     }

   if(ArrayResize(g_pending_deal_tickets, pending_total + 1) != pending_total + 1 ||
      ArrayResize(g_pending_deal_attempts, pending_total + 1) != pending_total + 1)
     {
      PrintFormat("TradeJournalBridge: memoria insufficiente per accodare il deal %I64u.",
                  deal_ticket);
      ArrayResize(g_pending_deal_tickets, pending_total);
      ArrayResize(g_pending_deal_attempts, pending_total);
      return false;
     }

   g_pending_deal_tickets[pending_total] = deal_ticket;
   g_pending_deal_attempts[pending_total] = 0;
   PrintFormat("TradeJournalBridge: deal %I64u non ancora disponibile, retry accodato.",
               deal_ticket);
   return true;
  }

void RemovePendingDealEvent(const int index)
  {
   int pending_total = ArraySize(g_pending_deal_tickets);
   if(index < 0 || index >= pending_total)
      return;

   for(int i = index; i < pending_total - 1; i++)
     {
      g_pending_deal_tickets[i] = g_pending_deal_tickets[i + 1];
      g_pending_deal_attempts[i] = g_pending_deal_attempts[i + 1];
     }
   ArrayResize(g_pending_deal_tickets, pending_total - 1);
   ArrayResize(g_pending_deal_attempts, pending_total - 1);
  }

void RetryPendingDealEvents()
  {
   // Iteriamo al contrario per poter rimuovere in-place senza saltare elementi.
   for(int i = ArraySize(g_pending_deal_tickets) - 1; i >= 0; i--)
     {
      ulong deal_ticket = g_pending_deal_tickets[i];
      g_pending_deal_attempts[i]++;
      if(EmitDealAddEvent(deal_ticket))
        {
         PrintFormat("TradeJournalBridge: deal %I64u pubblicato al retry %d.",
                     deal_ticket, g_pending_deal_attempts[i]);
         RemovePendingDealEvent(i);
         continue;
        }

      if(g_pending_deal_attempts[i] == 1 || g_pending_deal_attempts[i] % 30 == 0)
         PrintFormat("TradeJournalBridge: deal %I64u ancora non disponibile dopo %d retry.",
                     deal_ticket, g_pending_deal_attempts[i]);
     }
  }

void EmitOrderEvent(const string event_type, const ulong order_ticket)
  {
   string   symbol;
   long     type;
   double   volume, price, sl, tp;
   long     magic;
   string   comment;
   datetime event_time;
   long     timestamp_msc;

   if(OrderSelect(order_ticket))
     {
      symbol      = OrderGetString(ORDER_SYMBOL);
      type        = OrderGetInteger(ORDER_TYPE);
      volume      = OrderGetDouble(ORDER_VOLUME_CURRENT);
      price       = OrderGetDouble(ORDER_PRICE_OPEN);
      sl          = OrderGetDouble(ORDER_SL);
      tp          = OrderGetDouble(ORDER_TP);
      magic       = OrderGetInteger(ORDER_MAGIC);
      comment     = OrderGetString(ORDER_COMMENT);
      event_time  = (datetime)OrderGetInteger(ORDER_TIME_SETUP);
      timestamp_msc = OrderGetInteger(ORDER_TIME_SETUP_MSC);
     }
   else if(HistoryOrderSelect(order_ticket))
     {
      // ORDER_DELETE arriva spesso quando l'ordine e' gia' passato allo storico (eseguito,
      // scaduto o cancellato): in quel caso non e' piu' selezionabile tra gli attivi.
      symbol      = HistoryOrderGetString(order_ticket, ORDER_SYMBOL);
      type        = HistoryOrderGetInteger(order_ticket, ORDER_TYPE);
      volume      = HistoryOrderGetDouble(order_ticket, ORDER_VOLUME_CURRENT);
      price       = HistoryOrderGetDouble(order_ticket, ORDER_PRICE_OPEN);
      sl          = HistoryOrderGetDouble(order_ticket, ORDER_SL);
      tp          = HistoryOrderGetDouble(order_ticket, ORDER_TP);
      magic       = HistoryOrderGetInteger(order_ticket, ORDER_MAGIC);
      comment     = HistoryOrderGetString(order_ticket, ORDER_COMMENT);
      event_time  = (datetime)HistoryOrderGetInteger(order_ticket, ORDER_TIME_DONE);
      timestamp_msc = HistoryOrderGetInteger(order_ticket, ORDER_TIME_DONE_MSC);
     }
   else
      return; // ticket non (piu') selezionabile ne' tra gli attivi ne' nello storico: evento ignorato

   string line = BuildEventJson(event_type, (long)order_ticket, 0, (long)order_ticket, 0,
                                 symbol, DirectionFromType(type), volume, price, sl, tp,
                                 0.0, 0.0, 0.0, magic, comment, "", event_time, timestamp_msc);
   WriteEventAtomic(line);
  }

void EmitPositionEvent(const ulong position_ticket)
  {
   if(!PositionSelectByTicket(position_ticket))
      return; // la posizione puo' essere gia' stata chiusa quando arriva la notifica

   string   symbol      = PositionGetString(POSITION_SYMBOL);
   long     type        = PositionGetInteger(POSITION_TYPE);
   double   volume      = PositionGetDouble(POSITION_VOLUME);
   double   price       = PositionGetDouble(POSITION_PRICE_OPEN);
   double   sl          = PositionGetDouble(POSITION_SL);
   double   tp          = PositionGetDouble(POSITION_TP);
   long     magic       = PositionGetInteger(POSITION_MAGIC);
   string   comment     = PositionGetString(POSITION_COMMENT);
   datetime event_time  = (datetime)PositionGetInteger(POSITION_TIME_UPDATE);
   long     timestamp_msc = PositionGetInteger(POSITION_TIME_UPDATE_MSC);
   long     position_id = PositionGetInteger(POSITION_IDENTIFIER);

   string line = BuildEventJson("POSITION", (long)position_ticket, position_id, 0, 0,
                                 symbol, DirectionFromType(type), volume, price, sl, tp,
                                 0.0, 0.0, 0.0, magic, comment, "", event_time, timestamp_msc);
   WriteEventAtomic(line);
  }

void EmitHistoryOrderEvent(const ulong order_ticket)
  {
   if(!HistoryOrderSelect(order_ticket))
      return;

   string   symbol      = HistoryOrderGetString(order_ticket, ORDER_SYMBOL);
   long     type        = HistoryOrderGetInteger(order_ticket, ORDER_TYPE);
   double   volume      = HistoryOrderGetDouble(order_ticket, ORDER_VOLUME_CURRENT);
   double   price       = HistoryOrderGetDouble(order_ticket, ORDER_PRICE_OPEN);
   double   sl          = HistoryOrderGetDouble(order_ticket, ORDER_SL);
   double   tp          = HistoryOrderGetDouble(order_ticket, ORDER_TP);
   long     magic       = HistoryOrderGetInteger(order_ticket, ORDER_MAGIC);
   string   comment     = HistoryOrderGetString(order_ticket, ORDER_COMMENT);
   datetime event_time  = (datetime)HistoryOrderGetInteger(order_ticket, ORDER_TIME_DONE);
   long     timestamp_msc = HistoryOrderGetInteger(order_ticket, ORDER_TIME_DONE_MSC);

   string line = BuildEventJson("HISTORY_ADD", (long)order_ticket, 0, (long)order_ticket, 0,
                                 symbol, DirectionFromType(type), volume, price, sl, tp,
                                 0.0, 0.0, 0.0, magic, comment, "", event_time, timestamp_msc);
   WriteEventAtomic(line);
  }

//+------------------------------------------------------------------------+
//| Il backfill storico non entra nella coda live. L'Agent legge gli       |
//| snapshot e li consegna solo alla route /history vincolata alla lease:  |
//| nessun rate limit live e nessun saldo corrente attribuito al passato.  |
//+------------------------------------------------------------------------+
void RunBackfill()
  {
   if(InpBackfillHours <= 0)
     {
      Print("TradeJournalBridge: backfill disabilitato (InpBackfillHours<=0).");
      return;
     }

   datetime to_time   = TimeCurrent();
   datetime from_time = to_time - InpBackfillHours * 3600;
   if(!HistorySelect(from_time, to_time))
     {
      Print("TradeJournalBridge: HistorySelect fallita per il backfill, errore=", GetLastError());
      return;
     }

   int deals_total = HistoryDealsTotal();
   int orders_total = HistoryOrdersTotal();

   PrintFormat("TradeJournalBridge: backfill completato (%d deal, %d ordini storici, finestra %dh).",
               deals_total, orders_total, InpBackfillHours);
  }

//+------------------------------------------------------------------------+
//| Scrittura di tutti gli snapshot per un ciclo OnTimer. Tutti usano la    |
//| stessa envelope versionata; l'heartbeat e' scritto per ULTIMO, quindi   |
//| un heartbeat fresco implica che i dati dello stesso ciclo esistono gia'.|
//+------------------------------------------------------------------------+
void WriteAllSnapshots()
  {
   if(!g_new_only && g_history_snapshot_written)
     {
      // Lo storico deve restare immutabile e coerente con il balance anchor acquisito nello
      // stesso ciclo. La sequenza degli eventi live puo' continuare ad avanzare, mentre
      // l'heartbeat storico mantiene la sequence immutabile della fotografia certificata.
      WriteJsonAtomic("heartbeat.json", BuildEnvelope(
         BuildHeartbeatJson(g_history_snapshot_sequence), g_history_snapshot_sequence));
      return;
     }
   g_event_seq++;
   long sequence = g_event_seq;
   if(g_new_only)
     {
      // new_only parte da "adesso": nessun HistorySelect e nessun download candele deve
      // ritardare account/heartbeat o il primo snapshot live.
      bool snapshot_ok = true;
      if(!WriteJsonAtomic("account.json", BuildEnvelope(BuildAccountJson(), sequence)))
         snapshot_ok = false;
      if(!WriteJsonAtomic("positions.json", BuildEnvelope(BuildPositionsJson(), sequence)))
         snapshot_ok = false;
      if(!WriteJsonAtomic("orders.json", BuildEnvelope(BuildOrdersJson(), sequence)))
         snapshot_ok = false;
      if(!WriteJsonAtomic("history_orders.json", BuildEnvelope("[]", sequence)))
         snapshot_ok = false;
      datetime anchor_time = TimeCurrent();
      string empty_deals = "{\"anchor\":{\"balance\":" +
                           JsonNumber(AccountInfoDouble(ACCOUNT_BALANCE)) +
                           ",\"credit\":" + JsonNumber(AccountInfoDouble(ACCOUNT_CREDIT)) +
                           ",\"as_of\":" + JsonString(Iso8601FromDatetime(anchor_time)) +
                           ",\"coherent\":false,\"deal_count\":0" +
                           "},\"deals\":[]}";
      if(!WriteJsonAtomic("deals.json", BuildEnvelope(empty_deals, sequence)))
         snapshot_ok = false;
      // Anche in new_only l'heartbeat resta il marker di commit: mai pubblicare una sequence
      // che potrebbe riferirsi a file parzialmente aggiornati.
      if(snapshot_ok)
         WriteJsonAtomic("heartbeat.json", BuildEnvelope(BuildHeartbeatJson(sequence), sequence));
      return;
     }

   // Storico: il ledger certificato e il suo anchor vengono costruiti e pubblicati per primi.
   // Account/posizioni/ordini/candele possono richiedere tempo; prima del commit heartbeat il
   // ledger viene quindi confrontato di nuovo con MT5. Un cambiamento lascia questo tentativo
   // senza heartbeat e il consumer conserva l'ultimo bundle completo.
   HistoryLedgerAnchor ledger_anchor;
   string ledger_json = BuildDealsJson(ledger_anchor);
   if(!ledger_anchor.coherent)
     {
      Print("TradeJournalBridge: ledger storico non coerente, heartbeat non pubblicato.");
      return;
     }

   bool snapshot_ok = true;
   if(!WriteJsonAtomic("deals.json", BuildEnvelope(ledger_json, sequence)))
      snapshot_ok = false;
   if(!WriteJsonAtomic("history_orders.json", BuildEnvelope(BuildHistoryOrdersJson(), sequence)))
      snapshot_ok = false;
   if(!WriteJsonAtomic("account.json", BuildEnvelope(BuildAccountJson(), sequence)))
      snapshot_ok = false;
   if(!WriteJsonAtomic("positions.json", BuildEnvelope(BuildPositionsJson(), sequence)))
      snapshot_ok = false;
   if(!WriteJsonAtomic("orders.json", BuildEnvelope(BuildOrdersJson(), sequence)))
      snapshot_ok = false;
   for(int t = 0; t < 6; t++)
      if(!WriteJsonAtomic("candles\\" + _Symbol + "-" + TIMEFRAME_NAMES[t] + ".json",
                          BuildEnvelope(BuildCandlesJson(t), sequence)))
         snapshot_ok = false;
   // L'heartbeat e' il marker di commit del bundle: non pubblicarlo mai quando un file
   // richiesto e' fallito o l'anchor del ledger e' cambiato, altrimenti l'Agent potrebbe
   // associare dati vecchi a un ciclo nuovo.
   if(snapshot_ok)
     {
      if(!RevalidateHistoryLedgerAnchor(ledger_anchor))
        {
         Print("TradeJournalBridge: ledger storico cambiato prima del commit, heartbeat non pubblicato.");
         return;
        }
      if(!WriteJsonAtomic("heartbeat.json", BuildEnvelope(BuildHeartbeatJson(sequence), sequence)))
         snapshot_ok = false;
     }
   if(snapshot_ok)
     {
      g_history_snapshot_sequence = sequence;
      g_history_snapshot_written = true;
     }
  }

//+------------------------------------------------------------------------+
//| Handler standard MT5 richiesti da questo EA: OnInit, OnDeinit,         |
//| OnTimer, OnTradeTransaction. Nessun altro handler e' necessario        |
//| (in particolare nessun OnTick di trading).                             |
//+------------------------------------------------------------------------+
int OnInit()
  {
   WriteInitMarker("entered");
   if(!FolderCreate(BASE_DIR))
     {
      // FolderCreate restituisce true anche se la cartella esiste gia': un false qui indica un
      // problema piu' serio (sandbox non scrivibile), che verra' comunque rilevato dai
      // successivi WriteJsonAtomic falliti e riportato in heartbeat/log.
     Print("TradeJournalBridge: FolderCreate(", BASE_DIR, ") ha restituito false, errore=", GetLastError());
     }
   FolderCreate(BASE_DIR + "\\candles");
   FolderCreate(BASE_DIR + "\\events");
   WriteInitMarker("folders-ready");
   if(!CloseOtherCharts())
     {
      WriteInitMarker("chart-cleanup-failed");
      return(INIT_FAILED);
     }
   WriteInitMarker("single-chart-ready");

   g_connection_id = ReadConnectionId();
   WriteInitMarker("connection-id-ready");
   g_new_only = ReadNewOnlyMode();
   WriteInitMarker(g_new_only ? "mode-new-only" : "mode-history");
   g_new_only_started_ms = GetTickCount64();
   LoadCursorState();
   WriteInitMarker("cursor-ready");
   if(!g_new_only && !g_backfill_done)
     {
      RunBackfill();
      g_backfill_done = true;
     SaveCursorState();
     }

   // Su un'istanza live appena creata, le AccountInfo*/PositionsTotal/OrdersTotal invocate
   // durante OnInit possono attendere la cache account e impedire allo stesso terminale di
   // completare la sincronizzazione. new_only non ha backfill da anticipare: registra subito il
   // timer e lascia una breve finestra priva di letture account. Il primo snapshot arriva al
   // primo timer successivo alla finestra, senza dipendere da tick di mercato.
   if(!g_new_only)
      WriteAllSnapshots();

   if(InpTimerSeconds <= 0 || !EventSetTimer(InpTimerSeconds))
     {
      Print("TradeJournalBridge: EventSetTimer fallito (InpTimerSeconds=", InpTimerSeconds,
            "), errore=", GetLastError());
     return(INIT_FAILED);
     }

   WriteInitMarker("timer-ready");
   Print("TradeJournalBridge: EA di sola lettura avviato, timer=", InpTimerSeconds, "s.");
   return(INIT_SUCCEEDED);
  }

void OnDeinit(const int reason)
  {
   EventKillTimer();
   SaveCursorState();
  }

void OnTimer()
  {
   // Handoff history -> live senza fermare MT5. OnTradeTransaction resta attivo per tutta
   // l'importazione, quindi ogni deal successivo alla fotografia storica rimane nella coda
   // eventi e non esiste alcuna finestra cieca fra stop e riavvio del terminale.
   if(!g_new_only && ReadNewOnlyMode())
     {
      g_new_only = true;
      g_history_snapshot_written = false;
      WriteInitMarker("mode-new-only");
      Print("TradeJournalBridge: passaggio atomico a new_only completato.");
     }
   if(g_new_only && GetTickCount64() - g_new_only_started_ms < NEW_ONLY_STARTUP_GRACE_MS)
      return;
   RetryPendingDealEvents();
   WriteAllSnapshots();
   SaveCursorState();
  }

// SICUREZZA: questo e' l'unico punto in cui l'EA reagisce a transazioni. Legge soltanto lo
// stato del ticket coinvolto (funzioni Get*/History*Get*) e pubblica un file evento atomico.
// Nessun ramo chiama funzioni di trading. Ogni chiamata e' O(1) rispetto al volume di
// account/posizioni/ordini (nessuna scansione completa), per non bloccare a lungo il thread
// dei trade transaction del terminale.
void OnTradeTransaction(const MqlTradeTransaction &trans,
                        const MqlTradeRequest &request,
                        const MqlTradeResult &result)
  {
   switch(trans.type)
     {
      case TRADE_TRANSACTION_DEAL_ADD:
         if(!EmitDealAddEvent(trans.deal))
            QueuePendingDealEvent(trans.deal);
         break;
      case TRADE_TRANSACTION_ORDER_ADD:
         EmitOrderEvent("ORDER_ADD", trans.order);
         break;
      case TRADE_TRANSACTION_ORDER_UPDATE:
         EmitOrderEvent("ORDER_UPDATE", trans.order);
         break;
      case TRADE_TRANSACTION_ORDER_DELETE:
         EmitOrderEvent("ORDER_DELETE", trans.order);
         break;
      case TRADE_TRANSACTION_POSITION:
         EmitPositionEvent(trans.position);
         break;
      case TRADE_TRANSACTION_HISTORY_ADD:
         EmitHistoryOrderEvent(trans.order);
         break;
      default:
         break; // altri tipi di transazione (es. richieste rifiutate) non producono un evento
     }
  }
//+------------------------------------------------------------------------+
