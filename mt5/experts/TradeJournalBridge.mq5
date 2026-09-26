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
// Source-recovery v2 has deliberately separate owners.  The Windows Agent is
// the only writer of REQUEST; this EA is the only writer of STATE and ACK.
// Keeping those paths distinct means a recovery cleanup can never delete a
// concurrently published Windows full-history request.
#define SOURCE_RECOVERY_PROTOCOL_VERSION 2
#define SOURCE_RECOVERY_STATE_V2 "source-recovery-state-v2.json"
#define SOURCE_RECOVERY_REQUEST_V2 "source-recovery-request-v2.json"
#define SOURCE_RECOVERY_ACK_V2 "source-recovery-ack-v2.json"
#define SOURCE_RECOVERY_LEGACY_V1 "source-recovery-required.json"

//--- Stato di processo persistito su disco per sopravvivere a un riavvio di EA/terminale.
long g_event_seq     = 0;
bool g_backfill_done = false;
// A historical ledger must remain bound to the exact balance/credit anchor that
// certified it.  The snapshot is intentionally process-local: restarting the
// terminal takes a fresh immutable ledger boundary, while the live event cursor
// remains durable in cursor.json.
bool g_history_snapshot_written = false;
long g_history_snapshot_sequence = 0;

//--- Identificativo della connessione (UUID non sensibile), letto una volta in OnInit da un file
//--- scritto dal Windows Agent PRIMA di avviare il terminale: l'EA non ha altro modo di
//--- conoscere il connection_id, perche' MQL5 non legge variabili d'ambiente del processo.
//--- Incluso in ogni event_id per garantire unicita'
//--- anche fra connessioni/account diversi con ticket numericamente coincidenti.
string g_connection_id = "unknown-connection";
bool   g_new_only      = false;
datetime g_history_from = 0;
ulong  g_new_only_started_ms = 0;
bool   g_new_only_recovery_pending = false;
bool   g_source_recovery_required = false;
datetime g_source_recovery_from = 0;
// A new_only process is healthy only when it can prove a durable source
// continuity boundary.  The watermark is advanced after every durable source
// drain; on a cold restart the EA replays from it before it can report a
// normal live heartbeat.  ``0`` deliberately means "authoritative full
// history required", never "use a guessed recent window".
bool   g_cursor_state_present = false;
bool   g_cursor_state_valid = false;
bool   g_cursor_state_corrupt = false;
bool   g_source_watermark_valid = false;
datetime g_source_watermark = 0;
bool   g_source_recovery_marker_persisted = false;
// The request generation is allocated by the Windows Agent under its
// per-connection lifecycle lock.  Generation zero means that no v2 request
// has ever been published; normal requests start at one.
long   g_source_recovery_request_generation = 0;
long   g_source_recovery_ack_generation = 0;
bool   g_source_recovery_request_present = false;
bool   g_source_recovery_request_valid = true;
bool   g_source_recovery_ack_valid = true;
bool   g_source_recovery_request_regressed = false;
bool   g_pending_events_load_failed = false;
// Even an empty durable journal proves this is not a pristine directory.  A
// restart must not invent a fresh continuity watermark after losing cursor
// state beside that evidence.
bool   g_pending_events_file_present = false;
bool   g_source_marker_load_failed = false;
bool   g_existing_bridge_files_found = false;
const ulong NEW_ONLY_STARTUP_GRACE_MS = 5000;

// A terminal callback can precede the corresponding MT5 cache record by a few
// milliseconds, and a transient cursor write can prevent reserving its output
// sequence.  Keep every source callback in a bounded retry queue rather than
// silently losing it.  Full history/recovery remains the restart fallback.
enum PendingEventKind
  {
   PENDING_DEAL_ADD = 1,
   PENDING_ORDER_ADD = 2,
   PENDING_ORDER_UPDATE = 3,
   PENDING_POSITION = 4,
   PENDING_HISTORY_ORDER = 5
  };
const int MAX_PENDING_EVENTS = 256;
ulong g_pending_event_tickets[];
int g_pending_event_kinds[];
int g_pending_event_attempts[];
const int NEW_ONLY_RECOVERY_MAX_SECONDS = 21600; // massimo 6h: recupero gap, non reimport storico

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
   // The historical balance walk is a ledger computation. Rounding each row to
   // five decimals before it reaches the worker accumulates drift, especially on
   // crypto/cent accounts, so retain the native MT5 double on the wire.
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
   if(entry_raw == (long)DEAL_ENTRY_IN)    return "IN";
   if(entry_raw == (long)DEAL_ENTRY_OUT)   return "OUT";
   if(entry_raw == (long)DEAL_ENTRY_INOUT) return "INOUT";
   if(entry_raw == (long)DEAL_ENTRY_OUT_BY)return "OUT_BY";
   return "UNKNOWN";
  }

string DirectionFromType(const long mt5_type)
  {
   // Enum MT5: BUY/BUY_LIMIT/BUY_STOP/BUY_STOP_LIMIT sono pari, i corrispondenti SELL sono
   // dispari (0/2/4/6 vs 1/3/5/7) — stesso mapping esplicito gia' usato in
   // worker/mt5_client.py e bridge/windows/mt5_bridge.py:_order_direction.
   return (mt5_type % 2 == 0) ? "buy" : "sell";
  }

bool IsPendingOrderType(const long order_type)
  {
   return order_type == ORDER_TYPE_BUY_LIMIT ||
          order_type == ORDER_TYPE_SELL_LIMIT ||
          order_type == ORDER_TYPE_BUY_STOP ||
          order_type == ORDER_TYPE_SELL_STOP ||
          order_type == ORDER_TYPE_BUY_STOP_LIMIT ||
          order_type == ORDER_TYPE_SELL_STOP_LIMIT;
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

datetime ReadHistoryFrom()
  {
   string path = BASE_DIR + "\\history_from_unix";
   if(!FileIsExist(path))
      return 0;
   int handle = FileOpen(path, FILE_READ | FILE_TXT | FILE_ANSI | FILE_SHARE_READ, 0, CP_UTF8);
   if(handle == INVALID_HANDLE)
      return 0;
   string content = "";
   while(!FileIsEnding(handle))
      content += FileReadString(handle);
   FileClose(handle);
   StringTrimLeft(content);
   StringTrimRight(content);
   long unix_time = StringToInteger(content);
   return unix_time > 0 ? (datetime)unix_time : 0;
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

// Ogni evento e' un file indipendente in events/. A differenza di un log append-only puo'
// essere pubblicato con lo stesso rename atomico degli snapshot: il lettore non osserva mai una
// riga parziale e il nome basato sulla sequenza resta stabile dopo il riavvio grazie a cursor.json.
bool WriteEventAtomic(const string payload)
  {
   // A failed durable sequence reservation deliberately returns an empty
   // payload.  Never wrap that sentinel into event-N.json: an empty envelope
   // would acknowledge a predecessor without a broker event and make a later
   // retry ambiguous.
   if(StringLen(payload) == 0)
     {
      Print("TradeJournalBridge: payload evento vuoto rifiutato.");
      return false;
     }
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

bool TryExtractJsonLong(const string json, const string key, long &value)
  {
   string needle = "\"" + key + "\":";
   int pos = StringFind(json, needle);
   if(pos < 0)
      return false;
   pos += StringLen(needle);
   int start = pos;
   while(pos < StringLen(json))
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
      return false;
   value = StringToInteger(StringSubstr(json, start, pos - start));
   return true;
  }

// Control-plane generations are security/continuity fences, not permissive
// display numbers.  Do not let ``1.5`` or a numeric suffix parse as generation
// 1: an altered request/ack must remain fail-closed until its owner replaces it
// with the fixed v2 schema.
bool TryExtractStrictJsonLong(const string json, const string key, long &value)
  {
   string needle = "\"" + key + "\":";
   int pos = StringFind(json, needle);
   if(pos < 0)
      return false;
   pos += StringLen(needle);
   int start = pos;
   if(pos < StringLen(json) && StringGetCharacter(json, pos) == '-')
      pos++;
   int digits_start = pos;
   while(pos < StringLen(json))
     {
      ushort c = StringGetCharacter(json, pos);
      if(c < '0' || c > '9')
         break;
      pos++;
     }
   if(pos == digits_start)
      return false;
   if(pos < StringLen(json))
     {
      ushort delimiter = StringGetCharacter(json, pos);
      if(delimiter != ',' && delimiter != '}' && delimiter != ' ' &&
         delimiter != '\t' && delimiter != '\r' && delimiter != '\n')
         return false;
     }
   string token = StringSubstr(json, start, pos - start);
   value = StringToInteger(token);
   // This also rejects a value outside MQL5's signed long range instead of
   // accepting a saturated/truncated result.
   return IntegerToString(value) == token;
  }

// The control-plane writers both emit compact, atomic JSON.  Parsing just the
// three numbers would still accept a damaged object such as
// ``{"generation":1 broken,...}``, duplicate keys, or trailing content.  Keep
// the accepted v2 shapes intentionally closed: the two orders below are the
// Windows ``atomic_json(sort_keys=True)`` request and the EA acknowledgement.
// Leading/trailing line endings are tolerated because atomic_json appends one.
bool IsExactSourceRecoveryV2ControlJson(const string json, const long generation)
  {
   if(generation < 0)
      return false;
   string compact = json;
   StringTrimLeft(compact);
   StringTrimRight(compact);
   string generation_text = IntegerToString(generation);
   string windows_request = "{\"from_unix\":0,\"generation\":" +
                            generation_text + ",\"protocol_version\":2}";
   string ea_ack = "{\"protocol_version\":2,\"generation\":" +
                   generation_text + ",\"from_unix\":0}";
   return compact == windows_request || compact == ea_ack;
  }

bool IsExactSourceRecoveryStateV2Json(const string json, const long from_unix)
  {
   if(from_unix < 0)
      return false;
   string compact = json;
   StringTrimLeft(compact);
   StringTrimRight(compact);
   return compact == "{\"from_unix\":" + IntegerToString(from_unix) +
                     ",\"protocol_version\":2,\"continuity_version\":2}";
  }

// These are the compact v1 forms emitted by the old EA and by the prior
// Windows atomic writer.  Accepting only known legacy shapes lets migration
// preserve a valid cutoff while malformed/ambiguous v1 data still widens to a
// safe full-history replay.
bool IsExactLegacySourceRecoveryMarkerJson(const string json, const long from_unix)
  {
   if(from_unix < 0)
      return false;
   string compact = json;
   StringTrimLeft(compact);
   StringTrimRight(compact);
   string cutoff = IntegerToString(from_unix);
   return compact == "{\"from_unix\":" + cutoff + ",\"continuity_version\":1}" ||
          compact == "{\"continuity_version\":1,\"from_unix\":" + cutoff + "}" ||
          compact == "{\"from_unix\":" + cutoff + "}";
  }

bool ReadBridgeTextFile(const string relative_name, string &content)
  {
   string path = BASE_DIR + "\\" + relative_name;
   int handle = FileOpen(path, FILE_READ | FILE_TXT | FILE_ANSI | FILE_SHARE_READ, 0, CP_UTF8);
   if(handle == INVALID_HANDLE)
      return false;
   content = "";
   while(!FileIsEnding(handle))
      content += FileReadString(handle);
   FileClose(handle);
   return true;
  }

// An upgrade may inherit snapshot/event files written by an EA whose cursor
// did not reserve every snapshot generation.  Seed from every observable
// sequence before this version reserves a new one, so it can only publish
// N+1 or later and never splice a new account/ledger into an old N bundle.
void SeedEventSequenceFromExistingFiles()
  {
   string snapshot_files[6] = {
      "heartbeat.json", "account.json", "positions.json", "orders.json",
      "history_orders.json", "deals.json"
   };
   for(int i = 0; i < ArraySize(snapshot_files); i++)
     {
      string path = BASE_DIR + "\\" + snapshot_files[i];
      if(!FileIsExist(path))
         continue;
      g_existing_bridge_files_found = true;
      string content = "";
      long sequence = -1;
      if(ReadBridgeTextFile(snapshot_files[i], content) &&
         TryExtractJsonLong(content, "sequence", sequence) && sequence >= 0)
        {
         if(sequence > g_event_seq)
            g_event_seq = sequence;
        }
      else
        {
         // A malformed old snapshot is not a trusted continuity proof.  It
         // will be replaced only by a fully new, higher generation bundle.
         g_cursor_state_corrupt = true;
        }
     }

   string found = "";
   long finder = FileFindFirst(BASE_DIR + "\\events\\event-*.json", found);
   if(finder == INVALID_HANDLE)
      return;
   g_existing_bridge_files_found = true;
   do
     {
      int prefix = StringFind(found, "event-");
      int suffix = StringFind(found, ".json");
      if(prefix == 0 && suffix > 6)
        {
         long sequence = StringToInteger(StringSubstr(found, 6, suffix - 6));
         if(sequence > g_event_seq)
            g_event_seq = sequence;
        }
     }
   while(FileFindNext(finder, found));
   FileFindClose(finder);
  }

void LoadCursorState()
  {
   string path = BASE_DIR + "\\cursor.json";
   if(!FileIsExist(path))
      return; // bootstrap is made durable before the first normal heartbeat

   g_cursor_state_present = true;
   string content = "";
   long loaded_sequence = -1;
   if(!ReadBridgeTextFile("cursor.json", content) ||
      !TryExtractJsonLong(content, "event_seq", loaded_sequence) || loaded_sequence < 0)
     {
      // Never reset to zero after an unreadable cursor: the sequence seed and
      // full recovery path below keep any previously published predecessor
      // protected until an authoritative handoff succeeds.
      g_cursor_state_corrupt = true;
      return;
     }

   g_event_seq = loaded_sequence;
   g_backfill_done = ExtractJsonBool(content, "backfill_done", false);
   long watermark = 0;
   if(TryExtractJsonLong(content, "source_watermark_unix", watermark) && watermark > 0)
     {
      g_source_watermark = (datetime)watermark;
      g_source_watermark_valid = true;
     }
   else if(StringFind(content, "\"source_watermark_unix\":null") < 0)
     {
      // An old/malformed cursor can still seed the sequence, but cannot prove
      // where a live replay may safely start.
      g_cursor_state_corrupt = true;
     }
   g_cursor_state_valid = !g_cursor_state_corrupt;
  }

bool SaveCursorState()
  {
   string json = "{\"event_seq\":" + IntegerToString(g_event_seq) +
                 ",\"backfill_done\":" + (g_backfill_done ? "true" : "false") +
                 ",\"source_watermark_unix\":" +
                 (g_source_watermark_valid
                    ? IntegerToString((long)g_source_watermark)
                    : "null") +
                 ",\"continuity_version\":1}";
   bool saved = WriteJsonAtomic("cursor.json", json);
   if(saved)
     {
      g_cursor_state_present = true;
      g_cursor_state_valid = true;
      g_cursor_state_corrupt = false;
     }
   return saved;
  }

// The pending native callbacks have their own durable journal.  They cannot
// be folded into cursor.json because a failed cursor reservation is precisely
// the failure that must not erase the source callback.  The queue is small,
// atomically replaced and replayed after an EA/terminal restart.
string BuildPendingEventsJson()
  {
   string json = "{\"events\":[";
   int total = ArraySize(g_pending_event_tickets);
   for(int i = 0; i < total; i++)
     {
      if(i > 0)
         json += ",";
      json += "{\"kind\":" + IntegerToString(g_pending_event_kinds[i]) +
              ",\"ticket\":" + JsonString(IntegerToString((long)g_pending_event_tickets[i])) +
              ",\"attempts\":" + IntegerToString(g_pending_event_attempts[i]) + "}";
     }
   return json + "]}";
  }

bool SavePendingEvents()
  {
   return WriteJsonAtomic("pending-events.json", BuildPendingEventsJson());
  }

// A source loss must replay from the *oldest known safe point*.  Zero is a
// deliberate full-history sentinel; never replace it with a moving six-hour
// guess, because an outage longer than that would lose its first predecessor.
datetime EarliestRecoveryFrom(const datetime requested)
  {
   datetime earliest = requested;
   if(earliest < 0)
      earliest = 0;
   // Zero is the explicit full-history sentinel. A prior live watermark is
   // useful for a known callback omission, but cannot narrow recovery after
   // a corrupt/truncated queue whose oldest source is unknowable.
   if(earliest == 0)
      return 0;
   if(g_source_recovery_required)
     {
      if(g_source_recovery_from <= 0)
         return 0;
      if(earliest <= 0 || g_source_recovery_from < earliest)
         earliest = g_source_recovery_from;
     }
   if(g_source_watermark_valid)
     {
      if(g_source_watermark <= 0)
         return 0;
      if(earliest <= 0 || g_source_watermark < earliest)
         earliest = g_source_watermark;
     }
   return earliest;
  }

bool PersistSourceRecoveryMarker()
  {
   string json = "{\"from_unix\":" + IntegerToString((long)g_source_recovery_from) +
                 ",\"protocol_version\":" + IntegerToString(SOURCE_RECOVERY_PROTOCOL_VERSION) +
                 ",\"continuity_version\":2}";
   if(!WriteJsonAtomic(SOURCE_RECOVERY_STATE_V2, json))
     {
      Print("TradeJournalBridge: impossibile persistere marker recovery sorgente.");
      return false;
     }
   g_source_recovery_marker_persisted = true;
   return true;
  }

// Latch the failure in memory *before* attempting any marker write.  If both
// the queue and marker writes are transiently unavailable, this process can
// never emit a healthy heartbeat; after a crash its previous cursor watermark
// forces the same replay rather than silently clearing the gap.
void RequireSourceRecovery(const datetime requested_from, const bool persist_marker = true)
  {
   datetime earliest = EarliestRecoveryFrom(requested_from);
   g_source_recovery_required = true;
   g_source_recovery_from = earliest;
   if(g_new_only)
     {
      g_new_only_recovery_pending = true;
      g_history_from = g_source_recovery_from;
     }
   if(persist_marker)
      PersistSourceRecoveryMarker();
  }

// If the bounded callback journal cannot accept another source, do not merely
// log and forget it.  The current durable source watermark, not a sliding
// wall-clock window, is the recovery cutoff.
void MarkSourceRecoveryRequired()
  {
   RequireSourceRecovery(g_source_watermark_valid ? g_source_watermark : 0, true);
  }

bool EnsureSourceRecoveryMarker()
  {
   if(!g_source_recovery_required || g_source_recovery_marker_persisted)
      return true;
   return PersistSourceRecoveryMarker();
  }

// Read a Windows-owned request or an EA-owned acknowledgement.  Both files
// share a deliberately tiny, fixed schema so this EA can validate them without
// a general JSON library.  ``from_unix=0`` is mandatory: an uncertified close
// never gets a guessed recent cutoff.
bool ReadSourceRecoveryV2Control(const string relative_name,
                                 long &generation,
                                 bool &present)
  {
   present = false;
   generation = 0;
   string path = BASE_DIR + "\\" + relative_name;
   if(!FileIsExist(path))
      return true;
   present = true;
   string content = "";
   long protocol_version = 0;
   long from_unix = -1;
   if(!ReadBridgeTextFile(relative_name, content) ||
      !TryExtractStrictJsonLong(content, "protocol_version", protocol_version) ||
      !TryExtractStrictJsonLong(content, "generation", generation) ||
      !TryExtractStrictJsonLong(content, "from_unix", from_unix) ||
      protocol_version != SOURCE_RECOVERY_PROTOCOL_VERSION ||
      generation < 0 || from_unix != 0 ||
      !IsExactSourceRecoveryV2ControlJson(content, generation))
      return false;
   return true;
  }

bool PersistSourceRecoveryAck(const long generation)
  {
   if(generation < 0)
      return false;
   string json = "{\"protocol_version\":" + IntegerToString(SOURCE_RECOVERY_PROTOCOL_VERSION) +
                 ",\"generation\":" + IntegerToString(generation) +
                 ",\"from_unix\":0}";
   if(!WriteJsonAtomic(SOURCE_RECOVERY_ACK_V2, json))
     {
      Print("TradeJournalBridge: impossibile persistere acknowledgement recovery sorgente.");
      return false;
     }
   g_source_recovery_ack_generation = generation;
   g_source_recovery_ack_valid = true;
   return true;
  }

bool ExternalSourceRecoveryPending()
  {
   if(!g_source_recovery_request_valid || !g_source_recovery_ack_valid ||
      g_source_recovery_request_regressed)
      return true;
   return g_source_recovery_request_present &&
          g_source_recovery_request_generation != g_source_recovery_ack_generation;
  }

void LoadSourceRecoveryState()
  {
   string path = BASE_DIR + "\\" + SOURCE_RECOVERY_STATE_V2;
   if(!FileIsExist(path))
      return;
   string content = "";
   long from_unix = 0;
   long protocol_version = 0;
   if(!ReadBridgeTextFile(SOURCE_RECOVERY_STATE_V2, content) ||
      !TryExtractStrictJsonLong(content, "protocol_version", protocol_version) ||
      !TryExtractStrictJsonLong(content, "from_unix", from_unix) ||
      protocol_version != SOURCE_RECOVERY_PROTOCOL_VERSION || from_unix < 0 ||
      !IsExactSourceRecoveryStateV2Json(content, from_unix))
     {
      g_source_marker_load_failed = true;
      RequireSourceRecovery(0, false);
      return;
     }
   g_source_recovery_marker_persisted = true;
   RequireSourceRecovery((datetime)from_unix, false);
  }

// The guarded deployment stops the legacy Agent before it converges this EA
// binary, so the v1 path can be migrated only after v2 local evidence exists.
// The Windows v2 adapter never writes this name.  Persist-first is important:
// an interrupted migration can repeat a full recovery but cannot forget it.
void MigrateLegacySourceRecoveryMarker()
  {
   string path = BASE_DIR + "\\" + SOURCE_RECOVERY_LEGACY_V1;
   if(!FileIsExist(path))
      return;
   string content = "";
   long from_unix = 0;
   bool valid = ReadBridgeTextFile(SOURCE_RECOVERY_LEGACY_V1, content) &&
                TryExtractStrictJsonLong(content, "from_unix", from_unix) && from_unix >= 0 &&
                IsExactLegacySourceRecoveryMarkerJson(content, from_unix);
   if(!valid)
     {
      g_source_marker_load_failed = true;
      from_unix = 0;
     }
   RequireSourceRecovery((datetime)from_unix, false);
   if(!PersistSourceRecoveryMarker())
      return;
   // This is the sole v1 cleanup exception.  It is never used for a v2
   // Windows request, whose distinct pathname is intentionally never deleted
   // by this EA.
   if(!FileDelete(path))
     {
      g_source_marker_load_failed = true;
      return;
     }
  }

void LoadExternalSourceRecoveryRequest()
  {
   bool ack_present = false;
   long ack_generation = 0;
   g_source_recovery_ack_valid = ReadSourceRecoveryV2Control(
      SOURCE_RECOVERY_ACK_V2, ack_generation, ack_present);
   if(!g_source_recovery_ack_valid)
     {
      g_source_marker_load_failed = true;
      g_source_recovery_ack_generation = 0;
      RequireSourceRecovery(0, false);
     }
   else
      g_source_recovery_ack_generation = ack_present ? ack_generation : 0;

   bool request_present = false;
   long request_generation = 0;
   g_source_recovery_request_valid = ReadSourceRecoveryV2Control(
      SOURCE_RECOVERY_REQUEST_V2, request_generation, request_present);
   g_source_recovery_request_present = request_present;
   g_source_recovery_request_regressed = false;
   if(!g_source_recovery_request_valid || (request_present && request_generation <= 0))
     {
      g_source_marker_load_failed = true;
      g_source_recovery_request_generation = request_generation;
      RequireSourceRecovery(0, false);
      return;
     }
   if(!request_present)
     {
      if(g_source_recovery_ack_generation > 0)
        {
         // REQUEST is Windows-owned and deliberately retained after an ACK.
         // Its disappearance beside a nonzero acknowledgement is an
         // integrity fault, not permission to reset the generation.  Keep
         // the source fail-closed until the owning Agent publishes a newer
         // valid request.
         g_source_recovery_request_valid = false;
         g_source_marker_load_failed = true;
         g_source_recovery_request_generation = 0;
         RequireSourceRecovery(0, false);
         return;
        }
      // No request is pending.  Mirror the durable acknowledgement in the
      // heartbeat so a freshly upgraded bridge starts with a coherent 0/0
      // pair and never advertises a phantom mismatch.
      g_source_recovery_request_generation = g_source_recovery_ack_generation;
      return;
     }
   g_source_recovery_request_generation = request_generation;
   if(g_source_recovery_ack_valid &&
      g_source_recovery_request_generation < g_source_recovery_ack_generation)
     {
      // A regressing Windows generation is not an old request to silently
      // accept: it is evidence of a broken/mixed control plane.  Keep the EA
      // fail-closed until a newer valid request is published.
      g_source_recovery_request_regressed = true;
      g_source_marker_load_failed = true;
      RequireSourceRecovery(0, false);
      return;
     }
   if(ExternalSourceRecoveryPending())
      RequireSourceRecovery(0, false);
  }

// Ack only the exact request observed before the replay, and immediately
// re-read the Windows-owned request afterwards.  If another generation lands
// while MT5 is selecting/emitting history, it remains pending for a second
// full replay rather than being deleted by a cleanup path.
bool AcknowledgeExternalSourceRecoveryAfterReplay()
  {
   if(!g_source_recovery_request_valid || g_source_recovery_request_regressed)
      return false;
   if(!g_source_recovery_ack_valid)
     {
      // ACK is EA-owned.  Once the replay/cursor below have succeeded, a
      // corrupt old acknowledgement may be replaced by the exact request we
      // just certified (or the empty generation zero when no request exists).
      long repaired_generation = g_source_recovery_request_present
                                ? g_source_recovery_request_generation : 0;
      if(!PersistSourceRecoveryAck(repaired_generation))
         return false;
     }
   if(g_source_recovery_request_present &&
      g_source_recovery_request_generation > g_source_recovery_ack_generation)
     {
      if(!PersistSourceRecoveryAck(g_source_recovery_request_generation))
         return false;
     }
   LoadExternalSourceRecoveryRequest();
   return !ExternalSourceRecoveryPending();
  }

void LoadSourceRecoveryRequired()
  {
   LoadSourceRecoveryState();
   MigrateLegacySourceRecoveryMarker();
   LoadExternalSourceRecoveryRequest();
  }

long ExtractJsonLongAt(const string json, const string key, int &cursor, const long default_value)
  {
   int pos = StringFind(json, "\"" + key + "\":", cursor);
   if(pos < 0)
      return default_value;
   pos += StringLen(key) + 3;
   if(pos < StringLen(json) && StringGetCharacter(json, pos) == '"')
      pos++;
   int start = pos;
   while(pos < StringLen(json))
     {
      ushort c = StringGetCharacter(json, pos);
      if((c >= '0' && c <= '9') || c == '-')
        {
         pos++;
         continue;
        }
      break;
     }
   cursor = pos;
   if(pos == start)
      return default_value;
   return StringToInteger(StringSubstr(json, start, pos - start));
  }

void LoadPendingEvents()
  {
   string path = BASE_DIR + "\\pending-events.json";
   if(!FileIsExist(path))
      return;
   g_pending_events_file_present = true;
   string content = "";
   if(!ReadBridgeTextFile("pending-events.json", content))
     {
      // A locked/corrupt durable callback journal is not an empty queue.  The
      // boot path will require an authoritative replay before health can be
      // asserted, preserving the predecessor even if the terminal restarts.
      g_pending_events_load_failed = true;
      return;
     }
   StringTrimLeft(content);
   StringTrimRight(content);
   if(StringFind(content, "{\"events\":[") != 0 ||
      StringLen(content) < 12 ||
      StringSubstr(content, StringLen(content) - 2) != "]}")
     {
      g_pending_events_load_failed = true;
      return;
     }

   int cursor = 0;
   bool malformed = false;
   while(ArraySize(g_pending_event_tickets) < MAX_PENDING_EVENTS)
     {
      int next_kind = StringFind(content, "\"kind\":", cursor);
      if(next_kind < 0)
         break;
      long kind = ExtractJsonLongAt(content, "kind", cursor, 0);
      if(kind <= 0)
        {
         malformed = true;
         break;
        }
      long ticket = ExtractJsonLongAt(content, "ticket", cursor, 0);
      long attempts = ExtractJsonLongAt(content, "attempts", cursor, 0);
      if(ticket <= 0 || kind < PENDING_DEAL_ADD || kind > PENDING_HISTORY_ORDER)
        {
         malformed = true;
         break;
        }
      int total = ArraySize(g_pending_event_tickets);
      if(ArrayResize(g_pending_event_tickets, total + 1) != total + 1 ||
         ArrayResize(g_pending_event_kinds, total + 1) != total + 1 ||
         ArrayResize(g_pending_event_attempts, total + 1) != total + 1)
        {
         ArrayResize(g_pending_event_tickets, total);
         ArrayResize(g_pending_event_kinds, total);
         ArrayResize(g_pending_event_attempts, total);
         malformed = true;
         break;
        }
      g_pending_event_tickets[total] = (ulong)ticket;
      g_pending_event_kinds[total] = (int)kind;
      g_pending_event_attempts[total] = (int)MathMax(0, attempts);
     }
   if(malformed || StringFind(content, "\"kind\":", cursor) >= 0)
     {
      // Do not retain a valid-looking prefix from a truncated/over-capacity
      // journal: its next bogus ticket could permanently block FIFO recovery.
      // First latch an authoritative full-history replay (the original file
      // remains on disk as forensic evidence), then quarantine the in-memory
      // prefix so it cannot poison the recovery that repairs it.
      g_pending_events_load_failed = true;
      RequireSourceRecovery(0, true);
      ArrayResize(g_pending_event_tickets, 0);
      ArrayResize(g_pending_event_kinds, 0);
      ArrayResize(g_pending_event_attempts, 0);
     }
  }

bool AdvanceSourceWatermarkAfterLiveDrain()
  {
   if(g_source_recovery_required || ArraySize(g_pending_event_tickets) != 0)
      return false;
   datetime candidate = TimeGMT();
   if(candidate <= 0)
     {
      MarkSourceRecoveryRequired();
      return false;
     }
   datetime old_watermark = g_source_watermark;
   bool old_valid = g_source_watermark_valid;
   g_source_watermark = candidate;
   g_source_watermark_valid = true;
   if(SaveCursorState())
      return true;
   // Preserve the last durable cutoff in memory too.  Its cursor copy remains
   // available after a crash, so replay may duplicate a little but cannot
   // advance beyond an unresolved source.
   g_source_watermark = old_watermark;
   g_source_watermark_valid = old_valid;
   MarkSourceRecoveryRequired();
   return false;
  }

// A full, revalidated historical ledger is an authoritative recovery source.
// Commit its fresh watermark before clearing an existing loss marker; a failed
// cursor or marker deletion leaves the bridge explicitly unhealthy instead of
// claiming that a local callback gap disappeared.
bool EstablishSourceContinuityFromHistory()
  {
   if(ArraySize(g_pending_event_tickets) != 0)
     {
      MarkSourceRecoveryRequired();
      return false;
     }
   datetime candidate = TimeGMT();
   if(candidate <= 0)
     {
      MarkSourceRecoveryRequired();
      return false;
     }
   datetime old_watermark = g_source_watermark;
   bool old_watermark_valid = g_source_watermark_valid;
   bool old_recovery = g_source_recovery_required;
   datetime old_recovery_from = g_source_recovery_from;
   bool old_marker_persisted = g_source_recovery_marker_persisted;
   g_source_watermark = candidate;
   g_source_watermark_valid = true;
   g_source_recovery_required = false;
   g_source_recovery_from = 0;
   if(!SaveCursorState())
     {
      g_source_watermark = old_watermark;
      g_source_watermark_valid = old_watermark_valid;
      g_source_recovery_required = old_recovery;
      g_source_recovery_from = old_recovery_from;
      g_source_recovery_marker_persisted = old_marker_persisted;
      MarkSourceRecoveryRequired();
      return false;
     }
   // STATE is EA-owned.  Never clean the Windows-owned REQUEST pathname here:
   // a request published while this replay was in flight must survive for the
   // post-ack re-read below.
   string marker = BASE_DIR + "\\" + SOURCE_RECOVERY_STATE_V2;
   if(FileIsExist(marker) && !FileDelete(marker))
     {
      // The cursor is now newer (safe), but the unresolved-marker evidence is
      // still durable. Keep reporting recovery until a later cycle removes it.
      g_source_recovery_required = true;
      g_source_recovery_from = old_recovery_from;
      g_source_recovery_marker_persisted = true;
      return false;
     }
   g_source_recovery_marker_persisted = false;
   // Acknowledging comes strictly after the event replay and durable cursor /
   // watermark.  It re-reads REQUEST after the write, so a newer generation
   // cannot be hidden by this cleanup cycle.
   if(!AcknowledgeExternalSourceRecoveryAfterReplay())
     {
      RequireSourceRecovery(0, true);
      return false;
     }
   g_source_marker_load_failed = false;
   g_pending_events_load_failed = false;
   return true;
  }

void PrepareSourceContinuityAtBoot()
  {
   // Always take the observed producer high-water before reserving a new
   // sequence. This covers migration from an old EA whose cursor only tracked
   // callback events, not snapshot generations.
   SeedEventSequenceFromExistingFiles();

   if(g_cursor_state_corrupt || g_pending_events_load_failed ||
      g_source_marker_load_failed)
     {
      RequireSourceRecovery(0, true);
      return;
     }
   if(!g_new_only)
      return; // the frozen full ledger below becomes the authoritative proof

   if(!g_cursor_state_present && !g_existing_bridge_files_found &&
      !g_pending_events_file_present &&
      !g_pending_events_load_failed && !g_source_marker_load_failed &&
      !g_source_recovery_required)
     {
      // A genuinely new new_only instance has no predecessor to recover. It
      // still establishes its durable continuity watermark before emitting a
      // normal heartbeat, but must not pay a pointless HistorySelect(0, now).
      datetime bootstrap = TimeGMT();
      if(bootstrap > 0)
        {
         g_source_watermark = bootstrap;
         g_source_watermark_valid = true;
         if(SaveCursorState())
            return;
         g_source_watermark_valid = false;
        }
      RequireSourceRecovery(0, true);
      return;
     }

   if(!g_cursor_state_valid || !g_source_watermark_valid)
     {
      RequireSourceRecovery(0, true);
      return;
     }
   // Every cold new_only start intentionally replays from the last durable
   // watermark. It is cheap in the normal case and closes the crash interval
   // between a source callback and its local event-N file.
   if(!g_source_recovery_required)
      RequireSourceRecovery(g_source_watermark, true);
   else
      EnsureSourceRecoveryMarker();
  }

//+------------------------------------------------------------------------+
//| Costruzione degli snapshot completi (account/posizioni/ordini/candele) |
//+------------------------------------------------------------------------+
string BuildHeartbeatJson(const long sequence)
  {
   // A durable FIFO prefix is still unresolved source work even when its
   // journal is intact (for example, a transient deal-cache race). Do not
   // escalate that normal retry to full-history recovery, but never advertise
   // it as a clean bridge: the Windows supervisor and a V2 handoff must leave
   // every event file untouched until the prefix drains.
   bool source_recovery_required = g_source_recovery_required ||
                                   ExternalSourceRecoveryPending() ||
                                   ArraySize(g_pending_event_tickets) > 0;
   string json = "{";
   json += "\"generated_at\":" + JsonString(Iso8601FromDatetime(TimeCurrent())) + ",";
   json += "\"sequence\":" + IntegerToString(sequence) + ",";
   json += "\"history_mode\":" + JsonString(g_new_only ? "new_only" : "history") + ",";
   json += "\"terminal_connected\":" + (TerminalInfoInteger(TERMINAL_CONNECTED) ? "true" : "false") + ",";
   json += "\"account_trade_allowed\":" + (AccountInfoInteger(ACCOUNT_TRADE_ALLOWED) ? "true" : "false") + ",";
   json += "\"source_recovery_required\":" + (source_recovery_required ? "true" : "false") + ",";
   // These fields form the v2 capability and acknowledgement fence.  The
   // Windows adapter refuses to publish a connected transition for a legacy
   // heartbeat or while the durable request and acknowledgement differ.
   json += "\"source_recovery_protocol_version\":" +
           IntegerToString(SOURCE_RECOVERY_PROTOCOL_VERSION) + ",";
   json += "\"source_recovery_request_generation\":" +
           IntegerToString(g_source_recovery_request_generation) + ",";
   json += "\"source_recovery_ack_generation\":" +
           IntegerToString(g_source_recovery_ack_generation) + ",";
   json += "\"source_watermark_unix\":" +
           (g_source_watermark_valid ? IntegerToString((long)g_source_watermark) : "null");
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

   // NB: login/server qui NON sono mascherati (a differenza dei log/Print e di /health): questo
   // file viaggia solo sulla rete Docker interna verso il worker, che richiede questi due campi
   // non vuoti per attribuire correttamente le operazioni (stesso comportamento del vecchio
   // bridge/windows/mt5_bridge.py:_fetch_account, che restituiva il valore reale nel payload).
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
      long position_id     = PositionGetInteger(POSITION_IDENTIFIER);
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
      json += "\"position_id\":" +
              (position_id > 0 ? JsonString(IntegerToString(position_id)) : "null") + ",";
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
      datetime setup_time = (datetime)OrderGetInteger(ORDER_TIME_SETUP);

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
      json += "\"order_type\":" + IntegerToString(type) + ",";
      json += "\"placed_at\":" + JsonString(Iso8601FromDatetime(setup_time));
      json += "}";
     }
   json += "]";
   return json;
  }

// Snapshot storico separato dagli ordini attivi: HistorySync lo usa per importare anche ordini
// chiusi senza confondere lo stato live letto da LiveSync. Its own high-water
// is retained with the deal ledger so an order mutation cannot be committed
// under the same bundle sequence after the archive boundary was sampled.
struct HistoryOrdersAnchor
  {
   bool coherent;
   int count;
   long last_ticket;
   long last_time_msc;
  };

void ResetHistoryOrdersAnchor(HistoryOrdersAnchor &anchor)
  {
   anchor.coherent = false;
   anchor.count = 0;
   anchor.last_ticket = 0;
   anchor.last_time_msc = 0;
  }

string BuildHistoryOrdersJson(HistoryOrdersAnchor &anchor)
  {
   ResetHistoryOrdersAnchor(anchor);
   datetime to_time = TimeCurrent();
   datetime from_time = g_history_from;
   string json = "[";
   bool first = true;
   if(!HistorySelect(from_time, to_time))
      return json + "]";
   int total = HistoryOrdersTotal();
   int exported = 0;
   long last_ticket = 0;
   long last_time_msc = 0;
   for(int i = 0; i < total; i++)
     {
      ulong ticket = HistoryOrderGetTicket(i);
      if(ticket == 0)
        {
         ResetHistoryOrdersAnchor(anchor);
         return json + "]";
        }
      last_ticket = (long)ticket;
      last_time_msc = HistoryOrderGetInteger(ticket, ORDER_TIME_DONE_MSC);
      if(!first)
         json += ",";
      first = false;
      json += "{";
      json += "\"ticket\":" + JsonString(IntegerToString((long)ticket)) + ",";
      json += "\"symbol\":" + JsonString(HistoryOrderGetString(ticket, ORDER_SYMBOL)) + ",";
      json += "\"volume_current\":" + JsonNumber(HistoryOrderGetDouble(ticket, ORDER_VOLUME_CURRENT)) + ",";
      json += "\"volume_initial\":" + JsonNumber(HistoryOrderGetDouble(ticket, ORDER_VOLUME_INITIAL)) + ",";
      json += "\"type\":" + IntegerToString(HistoryOrderGetInteger(ticket, ORDER_TYPE)) + ",";
      json += "\"state\":" + IntegerToString(HistoryOrderGetInteger(ticket, ORDER_STATE)) + ",";
      json += "\"price_open\":" + JsonNumber(HistoryOrderGetDouble(ticket, ORDER_PRICE_OPEN)) + ",";
      json += "\"sl\":" + JsonNumber(HistoryOrderGetDouble(ticket, ORDER_SL)) + ",";
      json += "\"tp\":" + JsonNumber(HistoryOrderGetDouble(ticket, ORDER_TP)) + ",";
      json += "\"time_setup\":" + JsonString(Iso8601FromDatetime((datetime)HistoryOrderGetInteger(ticket, ORDER_TIME_SETUP))) + ",";
      json += "\"time_done\":" + JsonString(Iso8601FromDatetime((datetime)HistoryOrderGetInteger(ticket, ORDER_TIME_DONE))) + ",";
      json += "\"time\":" + JsonString(Iso8601FromDatetime((datetime)HistoryOrderGetInteger(ticket, ORDER_TIME_DONE)));
      json += "}";
      exported++;
     }
   datetime verified_at = TimeCurrent();
   if(!HistorySelect(from_time, verified_at))
      return json + "]";
   int verified_total = HistoryOrdersTotal();
   int verified_count = 0;
   long verified_last_ticket = 0;
   long verified_last_time_msc = 0;
   for(int verify_index = 0; verify_index < verified_total; verify_index++)
     {
      ulong verified_ticket = HistoryOrderGetTicket(verify_index);
      if(verified_ticket == 0)
         return json + "]";
      verified_count++;
      verified_last_ticket = (long)verified_ticket;
      verified_last_time_msc = HistoryOrderGetInteger(verified_ticket, ORDER_TIME_DONE_MSC);
     }
   anchor.coherent = exported == total && verified_count == verified_total &&
                     verified_count == exported &&
                     verified_last_ticket == last_ticket &&
                     verified_last_time_msc == last_time_msc;
   anchor.count = exported;
   anchor.last_ticket = last_ticket;
   anchor.last_time_msc = last_time_msc;
   return json + "]";
  }

// The historical deal ledger is the only basis for certifying a balance before
// an old trade opened.  It therefore has its own anchor and sequence: cash
// movements, credits and overlapping positions must be included even when the
// requested import window is only one day.
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

bool RevalidateHistoryLedgerAnchor(HistoryLedgerAnchor &anchor,
                                   HistoryOrdersAnchor &orders_anchor)
  {
   if(!anchor.coherent || !orders_anchor.coherent)
      return false;

   // MT5 history order is the authoritative ledger order.  Re-read the
   // high-water mark immediately before committing the heartbeat so the
   // worker never combines a changed ledger with an old balance anchor.
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
   // The immutable deal ledger always begins at account inception, while
   // historical pending orders follow the requested import window. Re-select
   // that exact order window before comparing its separate high-water.
   if(!HistorySelect(g_history_from, TimeCurrent()))
      return false;
   int orders_total = HistoryOrdersTotal();
   int orders_count = 0;
   long orders_last_ticket = 0;
   long orders_last_time_msc = 0;
   for(int order_index = 0; order_index < orders_total; order_index++)
     {
      ulong order_ticket = HistoryOrderGetTicket(order_index);
      if(order_ticket == 0)
         return false;
      orders_count++;
      orders_last_ticket = (long)order_ticket;
      orders_last_time_msc = HistoryOrderGetInteger(order_ticket, ORDER_TIME_DONE_MSC);
     }
   return count == total &&
          count == anchor.deal_count &&
          last_ticket == anchor.last_deal_ticket &&
          last_time_msc == anchor.last_deal_time_msc &&
          orders_count == orders_total &&
          orders_count == orders_anchor.count &&
          orders_last_ticket == orders_anchor.last_ticket &&
          orders_last_time_msc == orders_anchor.last_time_msc &&
          MathAbs(balance - anchor.balance) < 0.000001 &&
          MathAbs(credit - anchor.credit) < 0.000001;
  }

string BuildDealsJson(HistoryLedgerAnchor &anchor)
  {
   ResetHistoryLedgerAnchor(anchor);
   datetime to_time = TimeCurrent();
   // Always export the complete balance-affecting ledger. The worker will
   // filter projected trade events after reconstructing the opening balances.
   datetime from_time = 0;
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
      deals += "\"deal_type\":" + IntegerToString(deal_type) + ",";
      deals += "\"direction\":" + JsonString(deal_type == DEAL_TYPE_SELL ? "sell" : "buy") + ",";
      deals += "\"entry\":" + JsonString(EntryToString(HistoryDealGetInteger(ticket, DEAL_ENTRY))) + ",";
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
         verified_last_ticket = (long)verified_ticket;
         verified_last_time_msc = HistoryDealGetInteger(verified_ticket, DEAL_TIME_MSC);
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
                       const double commission, const double fee, const double swap, const long magic,
                       const string comment, const string entry, const datetime event_time,
                       long timestamp_msc, const long order_type = -1,
                       const long order_state = -1)
  {
   // Reserve event-N durably before publishing it.  A crash or a failed file
   // write may leave a harmless sequence gap, but can never reuse N and
   // overwrite an unacknowledged predecessor after restart.  Do not decrement
   // after a failed reservation: the next successful SaveCursorState persists
   // a strictly greater sequence; if the terminal dies first, no event-N was
   // ever published and replaying it is safe.
   g_event_seq++;
   if(!SaveCursorState())
     {
      Print("TradeJournalBridge: prenotazione durevole sequenza evento fallita.");
      return "";
     }
   if(timestamp_msc <= 0)
      timestamp_msc = (long)event_time * 1000; // fallback se la proprieta' _MSC non e' disponibile

   long   login  = AccountInfoInteger(ACCOUNT_LOGIN);
   string server = AccountInfoString(ACCOUNT_SERVER);
   // AccountInfoDouble(ACCOUNT_BALANCE) is observed when the callback is
   // serialized, not atomically at the broker's deal boundary.  A queued or
   // replayed callback can therefore observe later fills/cash movements.  Do
   // not manufacture a ``balance_before_open`` from that current value: only
   // the frozen full historical ledger can certify the denominator used for a
   // percentage P&L.  ``commission`` remains the native raw component here;
   // the worker folds DEAL_FEE into its effective commission exactly once.
   double account_balance = AccountInfoDouble(ACCOUNT_BALANCE);
   double account_equity = AccountInfoDouble(ACCOUNT_EQUITY);
   string account_currency = AccountInfoString(ACCOUNT_CURRENCY);
   long account_leverage = AccountInfoInteger(ACCOUNT_LEVERAGE);

   // Composito e deterministico: identita' account/evento broker piu' stato rilevante. I campi
   // di stato distinguono, per esempio, due ORDER_UPDATE dello stesso ordine (il loro tempo di
   // setup MT5 resta invariato), senza dipendere dalla sequenza locale del file.
   // La sequenza appartiene solo al nome/cursore locale: includerla qui renderebbe diverso lo
   // stesso evento broker quando il recovery rilegge deliberatamente la finestra di overlap.
   // Due connessioni/account diversi non possono mai produrre lo stesso event_id anche con
   // ticket numericamente coincidenti (broker/demo differenti): questo e' il requisito che
   // sostituisce la vecchia deduplica "solo per deal_ticket" (vedi
   // bridge/files/file_bridge.py:_EventsCursor, che usa (connection_id, login, server, ticket)
   // come chiave, non il solo ticket).
   string event_id = g_connection_id + "|" + IntegerToString(login) + "|" + server + "|" +
                      event_type + "|" + IntegerToString(ticket) + "|" +
                      IntegerToString(timestamp_msc) + "|" + symbol + "|" + direction + "|" +
                      DoubleToString(volume, 8) + "|" + DoubleToString(price, 8) + "|" +
                      DoubleToString(stop_loss, 8) + "|" + DoubleToString(take_profit, 8) + "|" +
                      DoubleToString(profit, 8) + "|" + DoubleToString(commission, 8) + "|" +
                      DoubleToString(swap, 8) + "|" + IntegerToString(magic) + "|" + entry;

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
   json += "\"fee\":" + JsonNumber(fee) + ",";
   json += "\"swap\":" + JsonNumber(swap) + ",";
   json += "\"balance\":" + JsonNumber(account_balance) + ",";
   json += "\"equity\":" + JsonNumber(account_equity) + ",";
   json += "\"currency\":" + JsonString(account_currency) + ",";
   json += "\"leverage\":" + IntegerToString(account_leverage) + ",";
   // Live callback timing is not certified ledger provenance.  Preserve the
   // field as an explicit null for protocol compatibility; the normalizer
   // intentionally never promotes it to a persisted percentage denominator.
   json += "\"balance_before_open\":null,";
   json += "\"magic\":" + IntegerToString(magic) + ",";
   json += "\"comment\":" + JsonString(comment) + ",";
   json += "\"entry\":" + (entry == "" ? "null" : JsonString(entry)) + ",";
   json += "\"order_type\":" + (order_type >= 0 ? IntegerToString(order_type) : "null") + ",";
   json += "\"order_state\":" + (order_state >= 0 ? IntegerToString(order_state) : "null") + ",";
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
   double   commission   = HistoryDealGetDouble(deal_ticket, DEAL_COMMISSION);
   double   fee          = HistoryDealGetDouble(deal_ticket, DEAL_FEE);
   double   swap         = HistoryDealGetDouble(deal_ticket, DEAL_SWAP);
   long     magic        = HistoryDealGetInteger(deal_ticket, DEAL_MAGIC);
   string   comment      = HistoryDealGetString(deal_ticket, DEAL_COMMENT);
   datetime event_time   = (datetime)HistoryDealGetInteger(deal_ticket, DEAL_TIME);
   long     timestamp_msc = HistoryDealGetInteger(deal_ticket, DEAL_TIME_MSC);

   // Balance/credit/commission accounting callbacks belong to the immutable
   // history ledger, not the live position lifecycle.  Returning success is
   // intentional: putting a zero-volume, symbol-less callback ahead of a real
   // close would make the preflight queue block forever.
   if(deal_type != DEAL_TYPE_BUY && deal_type != DEAL_TYPE_SELL)
      return true;
   if(symbol == "" || volume <= 0.0 || !MathIsValidNumber(volume))
      return false; // a real BUY/SELL may be visible before its cache fields

   // direction qui e' solo informativo (il tipo di deal, buy/sell): non e' usato dal bridge per
   // filtrare, che si basa esclusivamente su "entry" per ricostruire i deal di chiusura.
   string direction = (deal_type == DEAL_TYPE_SELL) ? "sell" : "buy";
   string entry = EntryToString(entry_raw);

   string line = BuildEventJson("DEAL_ADD", (long)deal_ticket, position_id, order_id, (long)deal_ticket,
                                 symbol, direction, volume, price, 0.0, 0.0,
                                 profit, commission, fee, swap, magic, comment, entry, event_time,
                                 timestamp_msc);
   if(line == "")
      return false;
   return WriteEventAtomic(line);
  }

bool QueuePendingEvent(const int kind, const ulong ticket)
  {
   if(ticket == 0)
     {
      // A missing source ticket cannot be journaled or replayed precisely.
      // Latch the authoritative recovery requirement before returning so it
      // can never turn into a log-only loss.
      MarkSourceRecoveryRequired();
      return false;
     }

   int pending_total = ArraySize(g_pending_event_tickets);
   for(int i = 0; i < pending_total; i++)
      if(g_pending_event_tickets[i] == ticket && g_pending_event_kinds[i] == kind)
         return true;

   if(pending_total >= MAX_PENDING_EVENTS)
     {
      PrintFormat("TradeJournalBridge: coda retry eventi piena, ticket %I64u non accodato.",
                  ticket);
      MarkSourceRecoveryRequired();
      return false;
     }
   if(ArrayResize(g_pending_event_tickets, pending_total + 1) != pending_total + 1 ||
      ArrayResize(g_pending_event_kinds, pending_total + 1) != pending_total + 1 ||
      ArrayResize(g_pending_event_attempts, pending_total + 1) != pending_total + 1)
     {
      ArrayResize(g_pending_event_tickets, pending_total);
      ArrayResize(g_pending_event_kinds, pending_total);
      ArrayResize(g_pending_event_attempts, pending_total);
      PrintFormat("TradeJournalBridge: memoria insufficiente per accodare l'evento %I64u.",
                  ticket);
      MarkSourceRecoveryRequired();
      return false;
     }
   g_pending_event_tickets[pending_total] = ticket;
   g_pending_event_kinds[pending_total] = kind;
   g_pending_event_attempts[pending_total] = 0;
   if(!SavePendingEvents())
     {
      // Do not claim a durable retry when the queue journal itself failed.
      // Roll back only this in-memory append; the next history/recovery pass
      // remains the fail-closed fallback and the terminal log exposes it.
      ArrayResize(g_pending_event_tickets, pending_total);
      ArrayResize(g_pending_event_kinds, pending_total);
      ArrayResize(g_pending_event_attempts, pending_total);
      PrintFormat("TradeJournalBridge: coda retry non persistita per evento %I64u.", ticket);
      MarkSourceRecoveryRequired();
      return false;
     }
   return true;
  }

int FindPendingEvent(const int kind, const ulong ticket)
  {
   int total = ArraySize(g_pending_event_tickets);
   for(int i = 0; i < total; i++)
      if(g_pending_event_tickets[i] == ticket && g_pending_event_kinds[i] == kind)
         return i;
   return -1;
  }

bool RemovePendingEvent(const int index)
  {
   int pending_total = ArraySize(g_pending_event_tickets);
   if(index < 0 || index >= pending_total)
      return false;
   ulong ticket = g_pending_event_tickets[index];
   int kind = g_pending_event_kinds[index];
   int attempts = g_pending_event_attempts[index];
   for(int i = index; i < pending_total - 1; i++)
     {
      g_pending_event_tickets[i] = g_pending_event_tickets[i + 1];
      g_pending_event_kinds[i] = g_pending_event_kinds[i + 1];
      g_pending_event_attempts[i] = g_pending_event_attempts[i + 1];
     }
   ArrayResize(g_pending_event_tickets, pending_total - 1);
   ArrayResize(g_pending_event_kinds, pending_total - 1);
   ArrayResize(g_pending_event_attempts, pending_total - 1);
   if(SavePendingEvents())
      return true;

   // Keeping the source in the durable journal is more important than a tidy
   // in-memory queue. Restore it so the next timer retries idempotently.
   ArrayResize(g_pending_event_tickets, pending_total);
   ArrayResize(g_pending_event_kinds, pending_total);
   ArrayResize(g_pending_event_attempts, pending_total);
   for(int j = pending_total - 1; j > index; j--)
     {
      g_pending_event_tickets[j] = g_pending_event_tickets[j - 1];
      g_pending_event_kinds[j] = g_pending_event_kinds[j - 1];
      g_pending_event_attempts[j] = g_pending_event_attempts[j - 1];
     }
   g_pending_event_tickets[index] = ticket;
   g_pending_event_kinds[index] = kind;
   g_pending_event_attempts[index] = attempts;
   PrintFormat("TradeJournalBridge: rimozione retry non persistita per evento %I64u.", ticket);
   return false;
  }

void RetryPendingEvents()
  {
   // Drain the durable source prefix in FIFO order. A close must never
   // overtake its queued open just because its cache record becomes readable
   // first; stop at the first unavailable predecessor and retry it later.
   while(ArraySize(g_pending_event_tickets) > 0)
     {
      int i = 0;
      ulong ticket = g_pending_event_tickets[i];
      int kind = g_pending_event_kinds[i];
      bool emitted = false;
      g_pending_event_attempts[i]++;
      switch(kind)
        {
         case PENDING_DEAL_ADD:
            emitted = EmitDealAddEvent(ticket);
            break;
         case PENDING_ORDER_ADD:
            emitted = EmitOrderEvent("ORDER_ADD", ticket);
            break;
         case PENDING_ORDER_UPDATE:
            emitted = EmitOrderEvent("ORDER_UPDATE", ticket);
            break;
         case PENDING_POSITION:
            emitted = EmitPositionEvent(ticket);
            break;
         case PENDING_HISTORY_ORDER:
            emitted = EmitHistoryOrderEvent(ticket);
            break;
         default:
            emitted = true; // corrupted in-memory kind: discard rather than spin forever
            break;
        }
      if(emitted)
        {
         // If journal removal itself fails, RemovePendingEvent restores the
         // head. Stop here: retrying a later source would violate causality.
         if(!RemovePendingEvent(i))
            return;
         continue;
        }
      if(g_pending_event_attempts[i] == 1 || g_pending_event_attempts[i] % 30 == 0)
         PrintFormat("TradeJournalBridge: evento %I64u ancora non pubblicabile dopo %d retry.",
                     ticket, g_pending_event_attempts[i]);
      return;
     }
  }

// Persist the source callback before reserving/writing event-N.  Therefore a
// crash in the narrow interval after SaveCursorState but before event-N.json
// cannot lose a trade: OnInit reloads this journal and retries with a later
// (never reused) sequence.  Successful writes are removed only after the
// removal itself is durable; a failed removal deliberately replays an
// idempotent duplicate rather than dropping the predecessor.
bool PublishNativeEvent(const int kind, const ulong ticket)
  {
   if(!QueuePendingEvent(kind, ticket))
      return false;
   // Never directly publish a tail callback: a previously queued open may be
   // temporarily unselectable while this close is already readable. The one
   // FIFO drain owns both immediate and timer retries.
   RetryPendingEvents();
   return FindPendingEvent(kind, ticket) < 0;
  }

bool EmitOrderEvent(const string event_type, const ulong order_ticket)
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
      return false; // ticket non ancora selezionabile: ritenta il callback nativo

   if(!IsPendingOrderType(type))
      return true; // ORDER_ADD/UPDATE viene emesso anche per BUY/SELL a mercato

   string line = BuildEventJson(event_type, (long)order_ticket, 0, (long)order_ticket, 0,
                                 symbol, DirectionFromType(type), volume, price, sl, tp,
                                 0.0, 0.0, 0.0, 0.0, magic, comment, "", event_time, timestamp_msc,
                                 type);
   if(line == "")
      return false;
   return WriteEventAtomic(line);
  }

bool EmitPositionEvent(const ulong position_ticket)
  {
   if(!PositionSelectByTicket(position_ticket))
      // A TRADE_TRANSACTION_POSITION commonly follows the authoritative
      // DEAL_ADD close after MT5 has already removed the position. Position
      // updates are snapshot hints, not an independent source ledger; no
      // retry must keep the durable source queue or recovery marker stuck.
      return true;

   string   symbol      = PositionGetString(POSITION_SYMBOL);
   long     type        = PositionGetInteger(POSITION_TYPE);
   double   volume      = PositionGetDouble(POSITION_VOLUME);
   double   price       = PositionGetDouble(POSITION_PRICE_OPEN);
   double   sl          = PositionGetDouble(POSITION_SL);
   double   tp          = PositionGetDouble(POSITION_TP);
   long     magic       = PositionGetInteger(POSITION_MAGIC);
   long     position_id = PositionGetInteger(POSITION_IDENTIFIER);
   string   comment     = PositionGetString(POSITION_COMMENT);
   datetime event_time  = (datetime)PositionGetInteger(POSITION_TIME_UPDATE);
   long     timestamp_msc = PositionGetInteger(POSITION_TIME_UPDATE_MSC);

   string line = BuildEventJson("POSITION", (long)position_ticket, position_id, 0, 0,
                                 symbol, DirectionFromType(type), volume, price, sl, tp,
                                 0.0, 0.0, 0.0, 0.0, magic, comment, "", event_time, timestamp_msc);
   if(line == "")
      return false;
  return WriteEventAtomic(line);
  }

void PublishBestEffortPositionSnapshot(const ulong position_ticket)
  {
   if(position_ticket == 0)
      return;
   // Position snapshots are re-exported by OnTimer and a close is carried by
   // its DEAL_ADD.  Never enqueue this non-source convenience notification:
   // an already-gone position would otherwise survive every restart.
   EmitPositionEvent(position_ticket);
  }

bool EmitHistoryOrderEvent(const ulong order_ticket)
  {
   if(!HistoryOrderSelect(order_ticket))
      return false;

   long state = HistoryOrderGetInteger(order_ticket, ORDER_STATE);
   long type = HistoryOrderGetInteger(order_ticket, ORDER_TYPE);
   string event_kind = "";
   if(state == ORDER_STATE_FILLED && IsPendingOrderType(type))
      event_kind = "HISTORY_FILLED";
   else if(IsPendingOrderType(type) &&
           (state == ORDER_STATE_CANCELED || state == ORDER_STATE_EXPIRED ||
            state == ORDER_STATE_REJECTED))
      event_kind = "HISTORY_ADD";
   else
      return true; // un'esecuzione parziale resta un ordine attivo fino al suo stato terminale

   string   symbol      = HistoryOrderGetString(order_ticket, ORDER_SYMBOL);
   double   volume      = HistoryOrderGetDouble(order_ticket, ORDER_VOLUME_CURRENT);
   double   price       = HistoryOrderGetDouble(order_ticket, ORDER_PRICE_OPEN);
   double   sl          = HistoryOrderGetDouble(order_ticket, ORDER_SL);
   double   tp          = HistoryOrderGetDouble(order_ticket, ORDER_TP);
   long     magic       = HistoryOrderGetInteger(order_ticket, ORDER_MAGIC);
   string   comment     = HistoryOrderGetString(order_ticket, ORDER_COMMENT);
   datetime event_time  = (datetime)HistoryOrderGetInteger(order_ticket, ORDER_TIME_DONE);
   long     timestamp_msc = HistoryOrderGetInteger(order_ticket, ORDER_TIME_DONE_MSC);

   string line = BuildEventJson(event_kind, (long)order_ticket, 0, (long)order_ticket, 0,
                                 symbol, DirectionFromType(type), volume, price, sl, tp,
                                 0.0, 0.0, 0.0, 0.0, magic, comment, "", event_time, timestamp_msc,
                                 type, state);
   if(line == "")
      return false;
   return WriteEventAtomic(line);
  }

//+------------------------------------------------------------------------+
//| Backfill una tantum al primo avvio: rilegge lo storico deal/ordini     |
//| nella finestra configurata e lo trascrive con lo stesso schema evento  |
//| usato in tempo reale, cosi' il bridge non deve distinguere backfill da |
//| eventi live. Marcato completato nel cursore persistente: un riavvio    |
//| successivo dell'EA non lo ripete. Se venisse riletto dopo un crash,    |
//| l'event_id basato sui campi immutabili del broker resterebbe identico, |
//| consentendo al worker di deduplicare il replay.                         |
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

// Un riavvio rolling resta in modalita' new_only, ma puo' ricevere dal Windows Agent un cutoff
// pre-stop in history_from_unix. Rileggiamo una sola finestra strettamente limitata per non
// perdere deal/chiusure avvenuti mentre il terminale era fermo. L'identita' evento deriva dai
// campi immutabili del broker; un crash prima della cancellazione del cutoff puo' quindi ripetere
// la finestra senza creare duplicati remoti, anche se lo snapshot corrente e' gia' cambiato.
bool RunNewOnlyRecovery()
  {
   if(!g_new_only_recovery_pending)
      return true;

   // Do not consume a recovery demand until its durable marker exists. If the
   // local filesystem is still failing, report unhealthy and retry later;
   // clearing a memory-only gap would lose it on the next process restart.
   if(g_source_recovery_required && !EnsureSourceRecoveryMarker())
      return false;

   datetime to_time = TimeCurrent();
   // history_from_unix e' UTC, mentre HistorySelect richiede il tempo del trade server.
   // Convertiamo il cutoff in una durata UTC e sottraiamo quella durata da TimeCurrent:
   // in questo modo il recupero resta corretto anche per broker con fuso negativo.
   long elapsed_seconds = g_history_from > 0
                          ? (long)TimeGMT() - (long)g_history_from
                          : 0;
   if(elapsed_seconds < 0)
      elapsed_seconds = 0;
   // A normal rolling restart remains bounded. A durable source-loss marker
   // instead replays from its earliest watermark without a sliding cap: MT5
   // deduplication makes the wider overlap safe, whereas trimming it would
   // permanently lose the predecessor that forced this recovery.
   if(!g_source_recovery_required && elapsed_seconds > NEW_ONLY_RECOVERY_MAX_SECONDS)
      elapsed_seconds = NEW_ONLY_RECOVERY_MAX_SECONDS;
   datetime from_time = g_history_from > 0
                        ? (datetime)((long)to_time - elapsed_seconds)
                        : (datetime)0;
   if(!HistorySelect(from_time, to_time))
     {
      Print("TradeJournalBridge: recovery new_only rinviata, errore=", GetLastError());
      return false;
     }

   // HistoryDealSelect/HistoryOrderSelect, usate dagli emettitori, restringono la rispettiva
   // lista selezionata a un solo elemento. Copiamo quindi tutti i ticket prima di emetterli.
   int deals_total = HistoryDealsTotal();
   ulong deal_tickets[];
   if(ArrayResize(deal_tickets, deals_total) != deals_total)
     {
      Print("TradeJournalBridge: memoria insufficiente per i deal del recovery.");
      return false;
     }
   for(int i = 0; i < deals_total; i++)
      deal_tickets[i] = HistoryDealGetTicket(i);

   int orders_total = HistoryOrdersTotal();
   ulong order_tickets[];
   if(ArrayResize(order_tickets, orders_total) != orders_total)
     {
      Print("TradeJournalBridge: memoria insufficiente per gli ordini del recovery.");
      return false;
     }
   for(int i = 0; i < orders_total; i++)
      order_tickets[i] = HistoryOrderGetTicket(i);

   bool all_events_written = true;
   for(int i = 0; i < deals_total; i++)
     {
      if(deal_tickets[i] == 0 || !PublishNativeEvent(PENDING_DEAL_ADD, deal_tickets[i]))
         all_events_written = false;
     }
   for(int i = 0; i < orders_total; i++)
     {
      if(order_tickets[i] == 0 || !PublishNativeEvent(PENDING_HISTORY_ORDER, order_tickets[i]))
         all_events_written = false;
     }
   if(!all_events_written)
     {
      Print("TradeJournalBridge: recovery new_only non persistita, verra' riprovata.");
      return false;
     }

   // The selected broker ledger is authoritative for this replay. It must
   // persist a new watermark and remove the durable loss marker before the
   // next heartbeat can be healthy.
   if(g_source_recovery_required && !EstablishSourceContinuityFromHistory())
     {
      Print("TradeJournalBridge: recovery sorgente non ancora confermata su disco.");
      return false;
     }
   if(!g_source_recovery_required && !AdvanceSourceWatermarkAfterLiveDrain())
      return false;

   g_new_only_recovery_pending = false;
   g_history_from = 0;
   if(!FileDelete(BASE_DIR + "\\history_from_unix"))
      Print("TradeJournalBridge: cutoff recovery gia' applicato ma non cancellato, errore=",
            GetLastError());
   PrintFormat("TradeJournalBridge: recovery new_only completata (%d deal, %d ordini).",
               deals_total, orders_total);
   return true;
  }

//+------------------------------------------------------------------------+
//| Scrittura di tutti gli snapshot per un ciclo OnTimer. Tutti usano la    |
//| stessa envelope versionata; l'heartbeat e' scritto per ULTIMO, quindi   |
//| un heartbeat fresco implica che i dati dello stesso ciclo esistono gia'.|
//+------------------------------------------------------------------------+
void WriteAllSnapshots()
  {
   // A historical ledger is frozen only until the Windows Agent atomically
   // publishes history_mode=new_only.  OnTimer clears this marker before it
   // writes live snapshots, so an on-demand history run can never leak an old
   // balance anchor into the resumed live stream.
   if(!g_new_only && g_history_snapshot_written)
     {
      // Keep the commit marker fresh without advancing its sequence.  Account,
      // deals and heartbeat therefore remain one immutable, certified bundle
      // while the worker uploads/retries the historical archive.
      WriteJsonAtomic("heartbeat.json", BuildEnvelope(
         BuildHeartbeatJson(g_history_snapshot_sequence), g_history_snapshot_sequence));
      return;
     }

   // Snapshot files share the event cursor namespace. Reserve the generation
   // durably *before* exposing account/deals/heartbeat: a crash after a
   // heartbeat but before a later timer must never restart at the same N and
   // let a reader splice an old ledger to a new account under one sequence.
   g_event_seq++;
   if(!SaveCursorState())
     {
      Print("TradeJournalBridge: prenotazione durevole snapshot fallita, nessun file pubblicato.");
      return;
     }
   long sequence = g_event_seq;
   if(g_new_only)
     {
      // new_only parte da "adesso": nessun HistorySelect e nessun download candele deve
      // ritardare account/heartbeat o il primo snapshot live.  The heartbeat is
      // still the commit marker: never expose its sequence after a partial write.
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
                           ",\"coherent\":false,\"deal_count\":0},\"deals\":[]}";
      if(!WriteJsonAtomic("deals.json", BuildEnvelope(empty_deals, sequence)))
         snapshot_ok = false;
      if(snapshot_ok)
         WriteJsonAtomic("heartbeat.json", BuildEnvelope(BuildHeartbeatJson(sequence), sequence));
      return;
     }

   // Build the full ledger before every other history file.  The worker may
   // subsequently project only the requested date range, but opening balances
   // require deposits, credits and overlapping positions from the whole ledger.
   HistoryLedgerAnchor ledger_anchor;
   string ledger_json = BuildDealsJson(ledger_anchor);
   HistoryOrdersAnchor orders_anchor;
   string history_orders_json = BuildHistoryOrdersJson(orders_anchor);
   if(!ledger_anchor.coherent || !orders_anchor.coherent)
     {
      Print("TradeJournalBridge: ledger/ordini storici non coerenti, heartbeat non pubblicato.");
      return;
     }

   bool snapshot_ok = true;
   if(!WriteJsonAtomic("deals.json", BuildEnvelope(ledger_json, sequence)))
      snapshot_ok = false;
   if(!WriteJsonAtomic("history_orders.json", BuildEnvelope(history_orders_json, sequence)))
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

   // No heartbeat is a deliberately fail-closed transaction: consumers retain
   // the last complete sequence until the full ledger/balance anchor can be
   // revalidated immediately before commit.
   if(snapshot_ok)
     {
      if(!RevalidateHistoryLedgerAnchor(ledger_anchor, orders_anchor))
        {
         Print("TradeJournalBridge: ledger storico cambiato prima del commit, heartbeat non pubblicato.");
         return;
        }
      // The frozen all-history ledger is the only source that can discharge
      // an unknown/corrupt live callback boundary. Persist its continuity
      // watermark (and clear any marker) before committing a healthy heartbeat.
      if(!EstablishSourceContinuityFromHistory())
        {
         Print("TradeJournalBridge: continuita' sorgente non confermata, heartbeat non pubblicato.");
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

   g_connection_id = ReadConnectionId();
   WriteInitMarker("connection-id-ready");
   g_new_only = ReadNewOnlyMode();
   g_history_from = ReadHistoryFrom();
   g_new_only_recovery_pending = g_new_only && g_history_from > 0;
   WriteInitMarker(g_new_only ? "mode-new-only" : "mode-history");
   g_new_only_started_ms = GetTickCount64();
   LoadCursorState();
   LoadPendingEvents();
   LoadSourceRecoveryRequired();
   PrepareSourceContinuityAtBoot();
   // A brand-new bridge with neither cursor/watermark nor a persisted loss
   // marker has no durable evidence from which a later process can recover a
   // callback. Keep the timer alive solely to retry the marker/recovery, but
   // never allow it to publish a healthy heartbeat or advance a watermark
   // past the unresolved source event.
   bool continuity_storage_unavailable =
      g_new_only && !g_cursor_state_present && !g_cursor_state_valid &&
      g_source_recovery_required && !g_source_recovery_marker_persisted;
   if(continuity_storage_unavailable)
     {
      // The in-memory latch remains fail-closed and every timer retries the
      // durable marker.  Returning INIT_FAILED here would tear down the only
      // process that can publish that diagnostic/retry after a transient
      // filesystem lock, and surfaces upstream as a false MT5 init failure.
      WriteInitMarker("continuity-storage-unavailable");
      Print("TradeJournalBridge: continuita' sorgente non persistibile al bootstrap; retry timer mantenuto.");
     }
   else
      WriteInitMarker("cursor-ready");
   // Lo storico viene ricostruito dagli snapshot e inviato in batch dal Windows Agent.
   // Non generare migliaia di event-*.json, che appartengono esclusivamente al flusso live.

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

   WriteInitMarker(continuity_storage_unavailable
                   ? "timer-ready-source-recovery"
                   : "timer-ready");
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
   // The control-plane handoff deliberately happens in the same EA process:
   // stopping/restarting MT5 between the frozen history archive and live mode
   // creates a blind window for DEAL_ADD callbacks.  history_mode is the
   // durable commit flag (written last by NativeMt5Runtime); reset all frozen
   // history state before the next heartbeat advances its sequence.
   if(!g_new_only && ReadNewOnlyMode())
     {
      g_new_only = true;
      g_history_snapshot_written = false;
      g_history_snapshot_sequence = 0;
      g_history_from = 0;
      g_new_only_recovery_pending = false;
      if(g_source_recovery_required)
        {
         g_new_only_recovery_pending = true;
         g_history_from = g_source_recovery_from;
        }
      // This is an already-authenticated process, not a cold startup: avoid
      // withholding the handoff heartbeat for the initial five-second grace.
      g_new_only_started_ms = 0;
      WriteInitMarker("mode-new-only");
      Print("TradeJournalBridge: transizione in-place history -> new_only.");
     }
   // The local Windows worker can observe a snapshot-only position reduction
   // before its corresponding DEAL_ADD is available.  It writes its distinct
   // v2 REQUEST; this EA keeps its own STATE/ACK files, so source recovery
   // never requires a stop/restart of MT5 or a delete of a Windows request.
   // Re-read on every timer even while recovery is already latched.  A later
   // worker request may deliberately widen a bounded cutoff to from_unix=0
   // after observing an uncertified close; ignoring that stronger marker
   // would replay only the older, narrower window.
   if(g_new_only)
      LoadSourceRecoveryRequired();
   if(g_new_only && GetTickCount64() - g_new_only_started_ms < NEW_ONLY_STARTUP_GRACE_MS)
      return;
   RetryPendingEvents();
   if(g_new_only && g_source_recovery_required && !EnsureSourceRecoveryMarker())
     {
      // A local storage fault is observable as an unhealthy bridge, never as
      // a normal heartbeat while the callback gap is only in memory.
      WriteAllSnapshots();
      return;
     }
   if(g_new_only_recovery_pending && !RunNewOnlyRecovery())
     {
      WriteAllSnapshots();
      return;
     }
   if(g_new_only && !g_source_recovery_required &&
      !AdvanceSourceWatermarkAfterLiveDrain())
     {
      // The helper latches source_recovery_required. Publish that explicit
      // unhealthy state if the snapshot files remain writable.
      WriteAllSnapshots();
      return;
     }
   WriteAllSnapshots();
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
         PublishNativeEvent(PENDING_DEAL_ADD, trans.deal);
         break;
      case TRADE_TRANSACTION_ORDER_ADD:
         PublishNativeEvent(PENDING_ORDER_ADD, trans.order);
         break;
      case TRADE_TRANSACTION_ORDER_UPDATE:
         PublishNativeEvent(PENDING_ORDER_UPDATE, trans.order);
         break;
      case TRADE_TRANSACTION_ORDER_DELETE:
         // ORDER_DELETE scatta sia per cancellazioni sia quando un ordine viene
         // eseguito e passa allo storico. Aspettiamo HISTORY_ADD, che legge e
         // filtra ORDER_STATE_* e usa la stessa identita' del recovery overlap.
         break;
      case TRADE_TRANSACTION_POSITION:
         PublishBestEffortPositionSnapshot(trans.position);
         break;
      case TRADE_TRANSACTION_HISTORY_ADD:
         PublishNativeEvent(PENDING_HISTORY_ORDER, trans.order);
         break;
      default:
         break; // altri tipi di transazione (es. richieste rifiutate) non producono un evento
     }
  }
//+------------------------------------------------------------------------+
