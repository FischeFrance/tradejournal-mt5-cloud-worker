//+------------------------------------------------------------------+
//| TradeJournalIdentityProbe.mq5                                    |
//|                                                                  |
//| Probe one-shot di identita' read-only per il laboratorio C0-C5.  |
//| Attende un risultato terminale, pubblica un solo JSON atomico e  |
//| poi si disattiva. Non usa API di trading, rete, DLL o FILE_COMMON.|
//+------------------------------------------------------------------+
#property copyright "TradeJournal"
#property link      ""
#property version   "3.00"
#property strict
#property description "Probe one-shot read-only e sanitizzato per MT5 direct endpoint."

#define PROBE_SCHEMA_VERSION 3
#define PROBE_VERSION "3.0.0"
#define PROBE_DIRECTORY "MT5DirectEndpointLab"
#define EXPECTED_ACCOUNT_FILE PROBE_DIRECTORY + "\\expected-account.txt"
#define RUN_ID_FILE PROBE_DIRECTORY + "\\run-id.txt"
#define OUTPUT_TEMP_FILE PROBE_DIRECTORY + "\\identity-probe.json.tmp"
#define OUTPUT_FINAL_FILE PROBE_DIRECTORY + "\\identity-probe.json"
#define UNBOUND_RUN_ID "00000000-0000-4000-8000-000000000000"

#define RESULT_CONNECTED_IDENTITY_AVAILABLE "CONNECTED_IDENTITY_AVAILABLE"
#define RESULT_IDENTITY_MISMATCH "IDENTITY_MISMATCH"
#define RESULT_TIMEOUT "TIMEOUT"
#define RESULT_NOT_CONNECTED "NOT_CONNECTED"
#define RESULT_INPUT_INVALID "INPUT_INVALID"
#define RESULT_OUTPUT_FAILURE "OUTPUT_FAILURE"

#define MIN_TIMEOUT_SECONDS 1
#define MAX_TIMEOUT_SECONDS 3600

input uint InpTimeoutSeconds = 120;

// Il login atteso e' un identificatore sensibile. Non viene mai convertito in
// stringa, scritto nel JSON o inviato a Print().
long   g_expected_account = 0;
bool   g_expected_login_loaded = false;
string g_run_id = UNBOUND_RUN_ID;
ulong  g_started_monotonic_ms = 0;
ulong  g_timeout_ms = 0;
bool   g_terminal_decided = false;
bool   g_terminal_published = false;
bool   g_finalizing = false;

struct ProbeSnapshot
  {
   bool   terminal_connected;
   bool   identity_available;
   bool   account_match;
   string account_server;
   string account_company;
   string account_trade_mode;
   bool   account_trade_allowed;
   bool   account_trade_expert;
   bool   terminal_trade_allowed;
   long   terminal_build;
   string terminal_path;
   string terminal_data_path;
  };

string JsonEscape(const string value)
  {
   string escaped = "";
   int length = StringLen(value);
   for(int index = 0; index < length; index++)
     {
      ushort character = StringGetCharacter(value, index);
      switch(character)
        {
         case '"':  escaped += "\\\""; break;
         case '\\': escaped += "\\\\"; break;
         case 8:    escaped += "\\b";  break;
         case 12:   escaped += "\\f";  break;
         case '\n': escaped += "\\n";  break;
         case '\r': escaped += "\\r";  break;
         case '\t': escaped += "\\t";  break;
         default:
           if(character < 0x20)
              escaped += StringFormat("\\u%04x", character);
           else
              escaped += ShortToString(character);
        }
     }
   return escaped;
  }

string JsonString(const string value)
  {
   return "\"" + JsonEscape(value) + "\"";
  }

string JsonBoolean(const bool value)
  {
   return value ? "true" : "false";
  }

string AccountTradeModeName(const long raw_mode)
  {
   if(raw_mode == (long)ACCOUNT_TRADE_MODE_DEMO)
      return "DEMO";
   if(raw_mode == (long)ACCOUNT_TRADE_MODE_CONTEST)
      return "CONTEST";
   if(raw_mode == (long)ACCOUNT_TRADE_MODE_REAL)
      return "REAL";
   return "UNKNOWN";
  }

bool IsKnownTradeMode(const string value)
  {
   return value == "DEMO" || value == "CONTEST" || value == "REAL";
  }

bool IsUnsignedDecimal(const string value)
  {
   int length = StringLen(value);
   if(length < 1 || length > 19)
      return false;
   for(int index = 0; index < length; index++)
     {
      ushort character = StringGetCharacter(value, index);
      if(character < '0' || character > '9')
         return false;
     }
   return true;
  }

bool IsLowerHex(const ushort character)
  {
   return (character >= '0' && character <= '9') ||
          (character >= 'a' && character <= 'f');
  }

bool IsCanonicalRunId(const string value)
  {
   if(StringLen(value) != 36 || value == UNBOUND_RUN_ID)
      return false;

   for(int index = 0; index < 36; index++)
     {
      ushort character = StringGetCharacter(value, index);
      bool separator = (index == 8 || index == 13 || index == 18 || index == 23);
      if(separator)
        {
         if(character != '-')
            return false;
        }
      else if(!IsLowerHex(character))
         return false;
     }

   ushort version = StringGetCharacter(value, 14);
   ushort variant = StringGetCharacter(value, 19);
   if(version < '1' || version > '5')
      return false;
   if(variant != '8' && variant != '9' && variant != 'a' && variant != 'b')
      return false;
   return true;
  }

bool DeleteIfPresent(const string path)
  {
   if(!FileIsExist(path))
      return true;
   return FileDelete(path);
  }

bool ReadSingleTextRecord(const string path, string &value)
  {
   value = "";
   int handle = FileOpen(path, FILE_READ | FILE_TXT | FILE_ANSI, 0, CP_UTF8);
   if(handle == INVALID_HANDLE)
      return false;

   int records = 0;
   while(!FileIsEnding(handle))
     {
      string current = FileReadString(handle);
      records++;
      if(records == 1)
         value = current;
      else
         value = "";
      current = "";
      if(records > 1)
         break;
     }
   bool single_record = (records == 1 && FileIsEnding(handle));
   FileClose(handle);

   StringTrimLeft(value);
   StringTrimRight(value);
   if(!single_record)
      value = "";
   return single_record;
  }

// Il run ID non e' una credenziale, ma viene consumato per impedire il riuso
// accidentale di input appartenenti a un run precedente.
bool ConsumeRunId()
  {
   bool existed = FileIsExist(RUN_ID_FILE);
   string value = "";
   bool read_ok = existed && ReadSingleTextRecord(RUN_ID_FILE, value);
   bool valid = read_ok && IsCanonicalRunId(value);
   bool deleted = existed && FileDelete(RUN_ID_FILE);

   if(valid)
      g_run_id = value;
   else
      g_run_id = UNBOUND_RUN_ID;
   value = "";
   return valid && deleted;
  }

// Il file account viene cancellato immediatamente dopo la lettura, prima della
// validazione e prima di produrre qualunque evidenza.
bool ConsumeExpectedAccount()
  {
   bool existed = FileIsExist(EXPECTED_ACCOUNT_FILE);
   string value = "";
   bool read_ok = existed && ReadSingleTextRecord(EXPECTED_ACCOUNT_FILE, value);
   bool deleted = existed && FileDelete(EXPECTED_ACCOUNT_FILE);
   bool valid = read_ok && IsUnsignedDecimal(value);
   long parsed = valid ? StringToInteger(value) : 0;
   value = "";

   if(!valid || !deleted || parsed <= 0)
     {
      parsed = 0;
      g_expected_account = 0;
      g_expected_login_loaded = false;
      return false;
     }

   g_expected_account = parsed;
   g_expected_login_loaded = true;
   parsed = 0;
   return true;
  }

void CaptureSnapshot(ProbeSnapshot &snapshot)
  {
   snapshot.terminal_connected =
      (TerminalInfoInteger(TERMINAL_CONNECTED) != 0);
   snapshot.identity_available = false;
   snapshot.account_match = false;
   snapshot.account_server = "";
   snapshot.account_company = "";
   snapshot.account_trade_mode = "UNKNOWN";
   snapshot.account_trade_allowed = false;
   snapshot.account_trade_expert = false;

   if(snapshot.terminal_connected)
     {
      // ACCOUNT_LOGIN esiste soltanto in questa variabile numerica temporanea e
      // viene usato esclusivamente per il confronto in memoria.
      long observed_account = AccountInfoInteger(ACCOUNT_LOGIN);
      snapshot.account_server = AccountInfoString(ACCOUNT_SERVER);
      snapshot.account_company = AccountInfoString(ACCOUNT_COMPANY);
      snapshot.account_trade_mode =
         AccountTradeModeName(AccountInfoInteger(ACCOUNT_TRADE_MODE));
      snapshot.account_trade_allowed =
         (AccountInfoInteger(ACCOUNT_TRADE_ALLOWED) != 0);
      snapshot.account_trade_expert =
         (AccountInfoInteger(ACCOUNT_TRADE_EXPERT) != 0);
      snapshot.identity_available =
         observed_account > 0 &&
         StringLen(snapshot.account_server) > 0 &&
         StringLen(snapshot.account_company) > 0 &&
         IsKnownTradeMode(snapshot.account_trade_mode);
      snapshot.account_match =
         snapshot.identity_available &&
         g_expected_login_loaded &&
         observed_account == g_expected_account;
      observed_account = 0;
     }

   snapshot.terminal_trade_allowed =
      (TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) != 0);
   snapshot.terminal_build = TerminalInfoInteger(TERMINAL_BUILD);
   snapshot.terminal_path = TerminalInfoString(TERMINAL_PATH);
   snapshot.terminal_data_path = TerminalInfoString(TERMINAL_DATA_PATH);
  }

string BuildTerminalEvidence(const string terminal_result,
                             const ProbeSnapshot &snapshot)
  {
   // I due path raw restano confinati nell'artefatto locale del probe. Il
   // consumer fidato deve calcolarne i digest SHA-256, calcolare separatamente
   // il digest dei byte esatti di questo artefatto e non copiare i path raw
   // nell'evidence normalizzata. L'output non puo' auto-attestare il proprio
   // digest senza introdurre una dipendenza circolare.
   string json = "{";
   json += "\"schema_version\":" + IntegerToString(PROBE_SCHEMA_VERSION) + ",";
   json += "\"probe_version\":" + JsonString(PROBE_VERSION) + ",";
   json += "\"run_id\":" + JsonString(g_run_id) + ",";
   json += "\"generated_at_unix\":" + IntegerToString((long)TimeGMT()) + ",";
   json += "\"terminal_result\":" + JsonString(terminal_result) + ",";
   json += "\"expected_login_loaded\":" + JsonBoolean(g_expected_login_loaded) + ",";
   json += "\"terminal_connected\":" + JsonBoolean(snapshot.terminal_connected) + ",";
   json += "\"account_match\":" + JsonBoolean(snapshot.account_match) + ",";
   json += "\"account_server\":" + JsonString(snapshot.account_server) + ",";
   json += "\"account_company\":" + JsonString(snapshot.account_company) + ",";
   json += "\"account_trade_mode\":" + JsonString(snapshot.account_trade_mode) + ",";
   json += "\"account_trade_allowed\":" + JsonBoolean(snapshot.account_trade_allowed) + ",";
   json += "\"account_trade_expert\":" + JsonBoolean(snapshot.account_trade_expert) + ",";
   json += "\"terminal_trade_allowed\":" + JsonBoolean(snapshot.terminal_trade_allowed) + ",";
   json += "\"terminal_build\":" + IntegerToString(snapshot.terminal_build) + ",";
   json += "\"terminal_path\":" + JsonString(snapshot.terminal_path) + ",";
   json += "\"terminal_data_path\":" + JsonString(snapshot.terminal_data_path);
   json += "}";
   return json;
  }

// Un solo punto del sorgente puo' rendere visibile il file finale. La
// destinazione non viene sovrascritta: un risultato gia' presente rende il run
// inconcludente invece di permettere una seconda pubblicazione.
bool PublishAtomically(const string json)
  {
   if(FileIsExist(OUTPUT_FINAL_FILE))
      return false;
   if(!DeleteIfPresent(OUTPUT_TEMP_FILE))
      return false;

   int handle = FileOpen(OUTPUT_TEMP_FILE,
                         FILE_WRITE | FILE_TXT | FILE_ANSI,
                         0, CP_UTF8);
   if(handle == INVALID_HANDLE)
      return false;

   uint written = FileWriteString(handle, json);
   FileFlush(handle);
   FileClose(handle);

   // FileWriteString restituisce byte, mentre StringLen conta code unit UTF-16:
   // confrontarli renderebbe falsamente KO path/company/server non ASCII.
   if(written == 0)
     {
      FileDelete(OUTPUT_TEMP_FILE);
      return false;
     }
   if(!FileMove(OUTPUT_TEMP_FILE, 0, OUTPUT_FINAL_FILE, 0))
     {
      FileDelete(OUTPUT_TEMP_FILE);
      return false;
     }
   return true;
  }

void CleanupSensitiveState()
  {
   EventKillTimer();
   g_expected_account = 0;
   g_expected_login_loaded = false;
   g_run_id = "";
   g_started_monotonic_ms = 0;
   g_timeout_ms = 0;
   FileDelete(EXPECTED_ACCOUNT_FILE);
   FileDelete(RUN_ID_FILE);
   FileDelete(OUTPUT_TEMP_FILE);
  }

void CompleteProbeWithSnapshot(const string terminal_result,
                               const ProbeSnapshot &snapshot)
  {
   if(g_terminal_decided || g_finalizing)
      return;

   g_terminal_decided = true;
   g_finalizing = true;
   EventKillTimer();

   string json = BuildTerminalEvidence(terminal_result, snapshot);
   bool published = PublishAtomically(json);
   json = "";

   // OUTPUT_FAILURE e' pubblicato soltanto se il risultato richiesto non e'
   // diventato visibile. PublishAtomically rifiuta sempre di sovrascrivere il
   // finale, quindi al massimo un risultato puo' essere osservato.
   if(!published && terminal_result != RESULT_OUTPUT_FAILURE)
     {
      string failure_json =
         BuildTerminalEvidence(RESULT_OUTPUT_FAILURE, snapshot);
      published = PublishAtomically(failure_json);
      failure_json = "";
     }

   g_terminal_published = published;
   CleanupSensitiveState();
   g_finalizing = false;
   ExpertRemove();
  }

void CompleteProbe(const string terminal_result)
  {
   ProbeSnapshot snapshot;
   CaptureSnapshot(snapshot);
   CompleteProbeWithSnapshot(terminal_result, snapshot);
  }

int OnInit()
  {
   FolderCreate(PROBE_DIRECTORY);

   // Un finale precedente e' evidenza immutabile: non cancellarlo e non
   // sostituirlo. Il consumer lo rifiutera' per run_id mismatch e questa nuova
   // invocazione terminera' senza pubblicare, quindi in modo fail-closed.
   bool stale_clean = DeleteIfPresent(OUTPUT_TEMP_FILE) &&
                      !FileIsExist(OUTPUT_FINAL_FILE);
   bool run_id_valid = ConsumeRunId();
   bool account_valid = ConsumeExpectedAccount();
   bool timeout_valid =
      (InpTimeoutSeconds >= MIN_TIMEOUT_SECONDS &&
       InpTimeoutSeconds <= MAX_TIMEOUT_SECONDS);

   if(!stale_clean)
     {
      CompleteProbe(RESULT_OUTPUT_FAILURE);
      return INIT_SUCCEEDED;
     }
   if(!run_id_valid || !account_valid || !timeout_valid)
     {
      CompleteProbe(RESULT_INPUT_INVALID);
      return INIT_SUCCEEDED;
     }

   g_timeout_ms = ((ulong)InpTimeoutSeconds) * 1000;
   g_started_monotonic_ms = GetTickCount64();
   if(!EventSetTimer(1))
     {
      CompleteProbe(RESULT_OUTPUT_FAILURE);
      return INIT_SUCCEEDED;
     }

   Print("TradeJournalIdentityProbe: attesa one-shot avviata.");
   return INIT_SUCCEEDED;
  }

void OnTimer()
  {
   if(g_terminal_decided || g_finalizing)
      return;

   ProbeSnapshot snapshot;
   CaptureSnapshot(snapshot);
   if(snapshot.identity_available)
     {
      string terminal_result = snapshot.account_match ?
         RESULT_CONNECTED_IDENTITY_AVAILABLE : RESULT_IDENTITY_MISMATCH;
      CompleteProbeWithSnapshot(terminal_result, snapshot);
      return;
     }

   ulong elapsed_ms = GetTickCount64() - g_started_monotonic_ms;
   if(elapsed_ms < g_timeout_ms)
      return;

   // Distinzione non ambigua al deadline:
   // - NOT_CONNECTED: TERMINAL_CONNECTED e' false al campione finale;
   // - TIMEOUT: il terminale e' connesso, ma login/server/company/mode non
   //   formano ancora una identita' completa.
   string timeout_result = snapshot.terminal_connected ?
      RESULT_TIMEOUT : RESULT_NOT_CONNECTED;
   CompleteProbeWithSnapshot(timeout_result, snapshot);
  }

void OnDeinit(const int reason)
  {
   CleanupSensitiveState();
   Print("TradeJournalIdentityProbe: probe one-shot arrestato.");
  }
