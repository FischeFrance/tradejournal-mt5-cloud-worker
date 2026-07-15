//+------------------------------------------------------------------------+
//| TradeJournalBridge.mq5                                                  |
//|                                                                          |
//| Expert Advisor di sola lettura per il trade journal.                    |
//|                                                                          |
//| ==== PERCHE' QUESTO EA NON PUO' FARE TRADING (leggere prima) ==========  |
//| Questo file non chiama MAI OrderSend, OrderSendAsync, OrderModify,       |
//| OrderClose o CTrade::*, e non contiene alcun blocco #import di una DLL   |
//| esterna. L'unico scopo e' leggere lo stato gia' presente nel terminale   |
//| (account, posizioni, ordini, storico) e scriverlo su file JSON dentro    |
//| il sandbox MQL5/Files/TradeJournal, cosi' che un processo Linux esterno  |
//| del tutto separato (bridge/files/file_bridge.py) possa esporlo in sola   |
//| lettura al worker via HTTP. Il worker stesso non ha mai potuto inviare   |
//| ordini (vedi worker/bridge_mt5_client.py): questo EA mantiene la stessa  |
//| garanzia anche sul lato MT5. tests/test_mql5_ea_no_trading.py verifica   |
//| staticamente l'assenza di queste chiamate a ogni modifica del file.      |
//| =========================================================================|
//+------------------------------------------------------------------------+
#property copyright "TradeJournal"
#property link      ""
#property version   "1.00"
#property strict
#property description "EA read-only: scrive account/posizioni/ordini/eventi/candele su file JSON. Nessuna funzione di trading."

//--- Parametri configurabili (vedi anche startup.ini / deploy/instance/entrypoint-runtime.sh)
input int InpTimerSeconds     = 2;        // Intervallo OnTimer in secondi (heartbeat + snapshot)
input int InpBackfillHours    = 168;      // Finestra di backfill storico al primo avvio (ore, 0 = disabilita)
input int InpCandleBars       = 200;      // Barre storiche complete da pubblicare per timeframe in candles.json
input int InpEventsMaxBytes   = 10000000; // Soglia di rotazione di events.jsonl (byte, 0 = mai ruotare)

//--- Directory di output, relativa al sandbox MQL5/Files del terminale (portable mode).
#define BASE_DIR "TradeJournal"

//--- Stato di processo persistito su disco per sopravvivere a un riavvio di EA/terminale.
long g_event_seq     = 0;
bool g_backfill_done = false;

//--- Identificativo della connessione (UUID non sensibile), letto una volta in OnInit da un file
//--- scritto dall'entrypoint PRIMA di avviare il terminale (deploy/instance/entrypoint-runtime.sh):
//--- l'EA non ha altro modo di conoscere il connection_id, perche' MQL5 non legge variabili
//--- d'ambiente del processo Linux ospitante. Incluso in ogni event_id per garantire unicita'
//--- anche fra connessioni/account diversi con ticket numericamente coincidenti.
string g_connection_id = "unknown-connection";

//--- Le 6 timeframe pubblicate in candles.json: stesse sigle di bridge/common.py:TIMEFRAME_SECONDS
//--- sul lato Python, cosi' il bridge Linux puo' servire POST /v1/candles senza reinterpretarle.
string           TIMEFRAME_NAMES[6]  = {"M1", "M5", "M15", "H1", "H4", "D1"};
ENUM_TIMEFRAMES  TIMEFRAME_VALUES[6] = {PERIOD_M1, PERIOD_M5, PERIOD_M15, PERIOD_H1, PERIOD_H4, PERIOD_D1};

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
   return DoubleToString(value, 5);
  }

// datetime MQL5 e' gia' un timestamp Unix (secondi UTC dal 1970-01-01), esattamente come i
// campi letti da worker/mt5_client.py e dal vecchio bridge/windows/mt5_bridge.py: nessuna
// conversione di fuso orario e' necessaria, solo la formattazione ISO8601 con suffisso Z.
string Iso8601FromDatetime(const datetime value)
  {
   string s = TimeToString(value, TIME_DATE | TIME_SECONDS); // "yyyy.mm.dd hh:mi:ss"
   StringReplace(s, ".", "-");
   StringReplace(s, " ", "T");
   return s + "Z";
  }

string EntryToString(const long entry_raw)
  {
   switch(entry_raw)
     {
      case DEAL_ENTRY_IN:     return "IN";
      case DEAL_ENTRY_OUT:    return "OUT";
      case DEAL_ENTRY_INOUT:  return "INOUT";
      case DEAL_ENTRY_OUT_BY: return "OUT_BY";
      default:                return "UNKNOWN";
     }
  }

string DirectionFromType(const long mt5_type)
  {
   // Enum MT5: BUY/BUY_LIMIT/BUY_STOP/BUY_STOP_LIMIT sono pari, i corrispondenti SELL sono
   // dispari (0/2/4/6 vs 1/3/5/7) — stesso mapping esplicito gia' usato in
   // worker/mt5_client.py e bridge/windows/mt5_bridge.py:_order_direction.
   return (mt5_type % 2 == 0) ? "buy" : "sell";
  }

// Il file connection_id e' scritto dall'entrypoint (contenuto non sensibile: solo un UUID di
// connessione) sotto BASE_DIR PRIMA che il terminale venga avviato, cosi' e' gia' presente al
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

//+------------------------------------------------------------------------+
//| Scrittura atomica: file.tmp poi rename sul nome finale. Nessuno dei    |
//| lettori (bridge/files/file_bridge.py) puo' mai osservare un file a     |
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

//--- Rotazione dimensionale di events.jsonl: oltre InpEventsMaxBytes, il file corrente viene
//--- rinominato in events.jsonl.1 (sovrascrivendo un'eventuale rotazione precedente: una sola
//--- generazione storica e' conservata) e la prossima AppendEventLine ne ricrea uno vuoto. Il
//--- bridge Linux riconosce la rotazione dal cambio di identita' (device+inode) del file
//--- "events.jsonl" e, se events.jsonl.1 e' esattamente il file che stava leggendo, ne consuma
//--- la coda rimasta prima di passare al nuovo file (vedi
//--- bridge/files/file_bridge.py:_EventsCursor, e docs/provisioning.md per il limite noto:
//--- una sola rotazione "non lette" fra due letture del bridge e' garantita senza perdita).
void RotateEventsLogIfNeeded()
  {
   if(InpEventsMaxBytes <= 0)
      return;
   string path = BASE_DIR + "\\events.jsonl";
   if(!FileIsExist(path))
      return;
   int handle = FileOpen(path, FILE_READ | FILE_TXT | FILE_ANSI | FILE_SHARE_READ, 0, CP_UTF8);
   if(handle == INVALID_HANDLE)
      return;
   ulong size = (ulong)FileSize(handle);
   FileClose(handle);
   if(size < (ulong)InpEventsMaxBytes)
      return;

   string rotated_path = BASE_DIR + "\\events.jsonl.1";
   if(!FileMove(path, 0, rotated_path, FILE_REWRITE))
      Print("TradeJournalBridge: rotazione di events.jsonl fallita, errore=", GetLastError());
  }

//--- events.jsonl e' l'unico file append-only: FileOpen con FILE_READ|FILE_WRITE non tronca il
//--- file esistente (a differenza del solo FILE_WRITE), FileSeek all'inizio della scrittura
//--- posiziona sempre alla fine. FileFlush dopo ogni riga garantisce che il bridge Linux, che
//--- legge in modo incrementale, non veda mai una riga a meta' oltre il confine dell'ultimo
//--- carattere di newline scritto.
bool AppendEventLine(const string json_line)
  {
   RotateEventsLogIfNeeded();
   string path = BASE_DIR + "\\events.jsonl";
   int handle = FileOpen(path, FILE_READ | FILE_WRITE | FILE_TXT | FILE_ANSI | FILE_SHARE_READ, 0, CP_UTF8);
   if(handle == INVALID_HANDLE)
     {
      Print("TradeJournalBridge: impossibile aprire events.jsonl errore=", GetLastError());
      return false;
     }
   FileSeek(handle, 0, SEEK_END);
   FileWriteString(handle, json_line + "\n");
   FileFlush(handle);
   FileClose(handle);
   return true;
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

void SaveCursorState()
  {
   string json = "{\"event_seq\":" + IntegerToString(g_event_seq) +
                 ",\"backfill_done\":" + (g_backfill_done ? "true" : "false") + "}";
   WriteJsonAtomic("cursor.json", json);
  }

//+------------------------------------------------------------------------+
//| Costruzione degli snapshot completi (account/posizioni/ordini/candele) |
//+------------------------------------------------------------------------+
string BuildHeartbeatJson()
  {
   string json = "{";
   json += "\"generated_at\":" + JsonString(Iso8601FromDatetime(TimeCurrent())) + ",";
   json += "\"sequence\":" + IntegerToString(g_event_seq) + ",";
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
   double equity   = AccountInfoDouble(ACCOUNT_EQUITY);
   string currency = AccountInfoString(ACCOUNT_CURRENCY);
   long   leverage = AccountInfoInteger(ACCOUNT_LEVERAGE);

   // NB: login/server qui NON sono mascherati (a differenza dei log/Print e di /health): questo
   // file viaggia solo sulla rete Docker interna verso il worker, che richiede questi due campi
   // non vuoti per attribuire correttamente le operazioni (stesso comportamento del vecchio
   // bridge/windows/mt5_bridge.py:_fetch_account, che restituiva il valore reale nel payload).
   string json = "{";
   json += "\"login\":" + JsonString(IntegerToString(login)) + ",";
   json += "\"server\":" + JsonString(server) + ",";
   json += "\"balance\":" + JsonNumber(balance) + ",";
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
      string symbol       = PositionGetString(POSITION_SYMBOL);
      long   type         = PositionGetInteger(POSITION_TYPE);
      double volume       = PositionGetDouble(POSITION_VOLUME);
      double open_price   = PositionGetDouble(POSITION_PRICE_OPEN);
      double sl           = PositionGetDouble(POSITION_SL);
      double tp           = PositionGetDouble(POSITION_TP);
      datetime open_time  = (datetime)PositionGetInteger(POSITION_TIME);

      if(!first)
         json += ",";
      first = false;
      json += "{";
      json += "\"ticket\":" + JsonString(IntegerToString((long)ticket)) + ",";
      json += "\"symbol\":" + JsonString(symbol) + ",";
      json += "\"direction\":" + JsonString(DirectionFromType(type)) + ",";
      json += "\"volume\":" + JsonNumber(volume) + ",";
      json += "\"open_price\":" + JsonNumber(open_price) + ",";
      json += "\"stop_loss\":" + JsonNumber(sl) + ",";
      json += "\"take_profit\":" + JsonNumber(tp) + ",";
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

string BuildCandlesJson()
  {
   string symbol = _Symbol; // simbolo del grafico su cui e' agganciato l'EA (Symbol= in startup.ini)
   string json = "{" + JsonString(symbol) + ":{";
   datetime now = TimeCurrent();

   for(int t = 0; t < 6; t++)
     {
      MqlRates rates[];
      int copied = CopyRates(symbol, TIMEFRAME_VALUES[t], 0, InpCandleBars + 1, rates);
      int period_seconds = PeriodSeconds(TIMEFRAME_VALUES[t]);

      json += JsonString(TIMEFRAME_NAMES[t]) + ":[";
      bool first = true;
      for(int i = 0; i < copied; i++)
        {
         if(rates[i].time + period_seconds > now)
            continue; // candela ancora in formazione: mai pubblicata (stessa regola del vecchio bridge)
         if(!first)
            json += ",";
         first = false;
         json += "{";
         json += "\"open_time\":" + JsonString(Iso8601FromDatetime(rates[i].time)) + ",";
         // Prezzi come stringa decimale, non come numero JSON: stesso formato del vecchio
         // bridge/windows/mt5_bridge.py (str(Decimal(...))), per compatibilita' col client.
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
      if(t < 5)
         json += ",";
     }
   json += "}}";
   return json;
  }

//+------------------------------------------------------------------------+
//| Un unico record evento per events.jsonl. Campi non applicabili al tipo |
//| di evento restano null, mai omessi (schema stabile riga per riga).     |
//+------------------------------------------------------------------------+
string BuildEventJson(const string event_type, const long ticket, const long position_id,
                       const long order_id, const long deal_id, const string symbol,
                       const string direction, const double volume, const double price,
                       const double stop_loss, const double take_profit, const double profit,
                       const double commission, const double swap, const long magic,
                       const string comment, const string entry, const datetime event_time,
                       long timestamp_msc)
  {
   g_event_seq++; // coda di unicita' entro lo stesso millisecondo, mai riusato
   if(timestamp_msc <= 0)
      timestamp_msc = (long)event_time * 1000; // fallback se la proprieta' _MSC non e' disponibile

   long   login  = AccountInfoInteger(ACCOUNT_LOGIN);
   string server = AccountInfoString(ACCOUNT_SERVER);

   // Composito e deterministico: connection_id + login + server + tipo + ticket + timestamp_msc.
   // Due connessioni/account diversi non possono mai produrre lo stesso event_id anche con
   // ticket numericamente coincidenti (broker/demo differenti): questo e' il requisito che
   // sostituisce la vecchia deduplica "solo per deal_ticket" (vedi
   // bridge/files/file_bridge.py:_EventsCursor, che usa (connection_id, login, server, ticket)
   // come chiave, non il solo ticket).
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
void EmitDealAddEvent(const ulong deal_ticket)
  {
   if(!HistoryDealSelect(deal_ticket))
      return; // il deal potrebbe non essere ancora visibile nella cache storica: evento perso una
              // tantum, ma posizioni/ordini/account restano comunque corretti al prossimo OnTimer

   long     position_id = (long)HistoryDealGetInteger(deal_ticket, DEAL_POSITION_ID);
   long     order_id     = (long)HistoryDealGetInteger(deal_ticket, DEAL_ORDER);
   string   symbol       = HistoryDealGetString(deal_ticket, DEAL_SYMBOL);
   long     deal_type    = HistoryDealGetInteger(deal_ticket, DEAL_TYPE);
   long     entry_raw    = HistoryDealGetInteger(deal_ticket, DEAL_ENTRY);
   double   volume       = HistoryDealGetDouble(deal_ticket, DEAL_VOLUME);
   double   price        = HistoryDealGetDouble(deal_ticket, DEAL_PRICE);
   double   profit       = HistoryDealGetDouble(deal_ticket, DEAL_PROFIT);
   double   commission   = HistoryDealGetDouble(deal_ticket, DEAL_COMMISSION);
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
   AppendEventLine(line);
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
   AppendEventLine(line);
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

   string line = BuildEventJson("POSITION", (long)position_ticket, (long)position_ticket, 0, 0,
                                 symbol, DirectionFromType(type), volume, price, sl, tp,
                                 0.0, 0.0, 0.0, magic, comment, "", event_time, timestamp_msc);
   AppendEventLine(line);
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
   AppendEventLine(line);
  }

//+------------------------------------------------------------------------+
//| Backfill una tantum al primo avvio: rilegge lo storico deal/ordini     |
//| nella finestra configurata e lo trascrive con lo stesso schema evento  |
//| usato in tempo reale, cosi' il bridge non deve distinguere backfill da |
//| eventi live. Marcato completato nel cursore persistente: un riavvio    |
//| successivo dell'EA non lo ripete (e anche se lo ripetesse, gli         |
//| event_id verrebbero rigenerati con una nuova sequenza: la deduplica    |
//| finale dei deal nel bridge e' comunque per (connection_id, login,      |
//| server, deal_ticket), non per event_id, quindi resta corretta in ogni  |
//| caso).                                                                  |
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
   for(int i = 0; i < deals_total; i++)
     {
      ulong ticket = HistoryDealGetTicket(i);
      if(ticket != 0)
         EmitDealAddEvent(ticket);
     }

   int orders_total = HistoryOrdersTotal();
   for(int i = 0; i < orders_total; i++)
     {
      ulong ticket = HistoryOrderGetTicket(i);
      if(ticket != 0)
         EmitHistoryOrderEvent(ticket);
     }

   PrintFormat("TradeJournalBridge: backfill completato (%d deal, %d ordini storici, finestra %dh).",
               deals_total, orders_total, InpBackfillHours);
  }

//+------------------------------------------------------------------------+
//| Scrittura di tutti gli snapshot per un ciclo OnTimer. L'heartbeat e'   |
//| scritto per ULTIMO: il bridge Linux considera "freschi" i dati solo se |
//| heartbeat.json e' recente, quindi una heartbeat fresca implica che     |
//| account/positions/orders/candles di questo stesso giro sono gia' stati |
//| scritti.                                                                |
//+------------------------------------------------------------------------+
void WriteAllSnapshots()
  {
   WriteJsonAtomic("account.json", BuildAccountJson());
   WriteJsonAtomic("positions.json", BuildPositionsJson());
   WriteJsonAtomic("orders.json", BuildOrdersJson());
   WriteJsonAtomic("candles.json", BuildCandlesJson());
   WriteJsonAtomic("heartbeat.json", BuildHeartbeatJson());
  }

//+------------------------------------------------------------------------+
//| Handler standard MT5 richiesti da questo EA: OnInit, OnDeinit,         |
//| OnTimer, OnTradeTransaction. Nessun altro handler e' necessario        |
//| (in particolare nessun OnTick di trading).                             |
//+------------------------------------------------------------------------+
int OnInit()
  {
   if(!FolderCreate(BASE_DIR))
     {
      // FolderCreate restituisce true anche se la cartella esiste gia': un false qui indica un
      // problema piu' serio (sandbox non scrivibile), che verra' comunque rilevato dai
      // successivi WriteJsonAtomic falliti e riportato in heartbeat/log.
      Print("TradeJournalBridge: FolderCreate(", BASE_DIR, ") ha restituito false, errore=", GetLastError());
     }

   g_connection_id = ReadConnectionId();
   LoadCursorState();
   if(!g_backfill_done)
     {
      RunBackfill();
      g_backfill_done = true;
      SaveCursorState();
     }

   WriteAllSnapshots(); // primo snapshot immediato, senza attendere il primo tick del timer

   if(InpTimerSeconds <= 0 || !EventSetTimer(InpTimerSeconds))
     {
      Print("TradeJournalBridge: EventSetTimer fallito (InpTimerSeconds=", InpTimerSeconds,
            "), errore=", GetLastError());
      return(INIT_FAILED);
     }

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
   WriteAllSnapshots();
   SaveCursorState();
  }

// SICUREZZA: questo e' l'unico punto in cui l'EA reagisce a transazioni. Legge soltanto lo
// stato del ticket coinvolto (funzioni Get*/History*Get*) e appende una riga a events.jsonl.
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
         EmitDealAddEvent(trans.deal);
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
