#!/usr/bin/env bash
set -euo pipefail

readonly RUNTIME_USER="runtime"
readonly RUNTIME_GROUP="runtime"
readonly PREFIX_ROOT="/var/lib/tradejournal/wine-prefix"
readonly TEMPLATE_ARCHIVE="/opt/tradejournal/template/mt5-prefix.tar.zst"
readonly TEMPLATE_VERSION="tradejournal-mt5-prefix-v1"
readonly BRIDGE_SCRIPT_UNIX="/app/bridge/files/file_bridge.py"
readonly RUNTIME_HOME="/home/runtime"
readonly STARTUP_INI_PATH="${RUNTIME_HOME}/startup.ini"
readonly CHILD_STOP_TIMEOUT_SECONDS=5
readonly WINESERVER_STOP_TIMEOUT_SECONDS=3

log() {
  printf '[mt5-runtime] %s\n' "$*"
}

fail() {
  printf '[mt5-runtime] ERROR: %s\n' "$*" >&2
  exit 1
}

windows_path_in_prefix() {
  local windows_path="$1"
  local relative_path
  case "$windows_path" in
    [Cc]:\\*)
      relative_path="${windows_path:3}"
      ;;
    *)
      return 1
      ;;
  esac
  relative_path="${relative_path//\\//}"
  case "/${relative_path}/" in
    */../*|*/./*) return 1 ;;
  esac
  printf '%s/drive_c/%s\n' "$WINEPREFIX" "$relative_path"
}

unix_path_to_wine_z() {
  local unix_path="$1"
  local windows_path
  case "$unix_path" in
    /*) ;;
    *) return 1 ;;
  esac
  windows_path="${unix_path//\//\\}"
  printf 'Z:%s\n' "$windows_path"
}

# Genera lo startup.ini letto da terminal64.exe via /config (vedi piu' sotto). Isolata in una
# funzione cosi' da poter essere testata (in un test dedicato, sourcing solo questa funzione)
# senza dover avviare Wine/Xvfb: verifica che il contenuto non venga mai stampato e che il file
# risulti sempre 0600. La password viene letta una sola volta dal file gia' validato dal
# chiamante e non lascia mai questa funzione se non scritta nel file di destinazione.
generate_startup_ini() {
  local login="$1" server="$2" password_file="$3" symbol="$4" ini_path="$5"
  local password

  password="$(cat -- "$password_file")"
  case "$password" in
    *$'\n'*|*$'\r'*) fail "MT5_PASSWORD_FILE must contain a single-line secret" ;;
  esac

  install -m 0600 /dev/null "$ini_path"
  {
    printf '[Common]\n'
    printf 'Login=%s\n' "$login"
    printf 'Server=%s\n' "$server"
    printf 'Password=%s\n' "$password"
    printf 'KeepPrivate=0\n'
    printf '\n'
    printf '[Experts]\n'
    printf 'Enabled=1\n'
    printf 'AllowLiveTrading=0\n'
    printf 'AllowDllImport=0\n'
    printf 'Account=0\n'
    printf 'Profile=0\n'
    printf '\n'
    printf '[StartUp]\n'
    printf 'Expert=TradeJournal\\TradeJournalBridge\n'
    printf 'Symbol=%s\n' "$symbol"
    printf 'Period=M1\n'
  } > "$ini_path"
  chmod 0600 "$ini_path"
  unset password
}

if [ "$(id -u)" -eq 0 ]; then
  mkdir -p "$PREFIX_ROOT"
  chown "$RUNTIME_USER:$RUNTIME_GROUP" "$PREFIX_ROOT"
  exec gosu "$RUNTIME_USER:$RUNTIME_GROUP" "$0" "$@"
fi

umask 077

MT5_TERMINAL_PATH="${MT5_TERMINAL_PATH:-C:\\Program Files\\MetaTrader 5\\terminal64.exe}"
EURUSD_BROKER_SYMBOL="${EURUSD_BROKER_SYMBOL:-EURUSD}"
TERMINAL_READY_TIMEOUT_SECONDS="${TERMINAL_READY_TIMEOUT_SECONDS:-120}"

case "$TERMINAL_READY_TIMEOUT_SECONDS" in
  ''|*[!0-9]*) fail "TERMINAL_READY_TIMEOUT_SECONDS must be a positive integer" ;;
esac
if [ "$TERMINAL_READY_TIMEOUT_SECONDS" -lt 1 ]; then
  fail "TERMINAL_READY_TIMEOUT_SECONDS must be a positive integer"
fi
for value_name in MT5_LOGIN MT5_SERVER EURUSD_BROKER_SYMBOL TJ_CONNECTION_ID \
  TJ_EXPECTED_MT5_LOGIN TJ_EXPECTED_MT5_SERVER; do
  value="${!value_name:-}"
  case "$value" in
    *$'\n'*|*$'\r'*) fail "$value_name must be a single line value" ;;
  esac
done
# TJ_EXPECTED_MT5_LOGIN/SERVER sono il controllo obbligatorio di identita' dell'account, applicato
# lato bridge (vedi bridge/files/file_bridge.py): l'entrypoint si limita a garantirne la presenza
# qui, cosi' una configurazione incompleta fallisce subito con un errore chiaro invece che
# lasciare il bridge servire dati senza mai poter verificare a quale account appartengono.
[ -n "${TJ_CONNECTION_ID:-}" ] || fail "TJ_CONNECTION_ID is required"
[ -n "${TJ_EXPECTED_MT5_LOGIN:-}" ] || fail "TJ_EXPECTED_MT5_LOGIN is required"
[ -n "${TJ_EXPECTED_MT5_SERVER:-}" ] || fail "TJ_EXPECTED_MT5_SERVER is required"

if [ ! -f "$TEMPLATE_ARCHIVE" ] || [ ! -r "$TEMPLATE_ARCHIVE" ]; then
  fail "golden template archive is missing or unreadable"
fi
if ! printf '%s' "${MT5_TEMPLATE_SHA256:-}" | grep -Eq '^[0-9a-fA-F]{64}$'; then
  fail "MT5_TEMPLATE_SHA256 must contain the expected SHA-256 digest"
fi

expected_sha="$(printf '%s' "$MT5_TEMPLATE_SHA256" | tr 'A-F' 'a-f')"
actual_sha="$(sha256sum "$TEMPLATE_ARCHIVE" | awk '{print $1}')"
if [ "$actual_sha" != "$expected_sha" ]; then
  fail "golden template checksum mismatch"
fi

if [ ! -d "$WINEPREFIX" ]; then
  shopt -s nullglob dotglob
  existing_entries=("$PREFIX_ROOT"/*)
  shopt -u nullglob dotglob
  for entry in "${existing_entries[@]}"; do
    case "$(basename "$entry")" in
      lost+found|.initializing.*) ;;
      *) fail "Wine prefix volume is not empty and has no valid current prefix" ;;
    esac
  done

  # A crash can only leave a private .initializing.* tree. The final rename is atomic because
  # staging and current live in the same Docker volume.
  find "$PREFIX_ROOT" -mindepth 1 -maxdepth 1 -name '.initializing.*' -exec rm -rf -- {} +
  staging_prefix="$PREFIX_ROOT/.initializing.$$"
  mkdir -m 0700 "$staging_prefix"
  log "initializing the dedicated Wine prefix from the verified golden template"
  tar --zstd -xf "$TEMPLATE_ARCHIVE" -C "$staging_prefix" --no-same-owner --no-same-permissions
  if [ "$(cat "$staging_prefix/.tradejournal-template-version" 2>/dev/null || true)" != "$TEMPLATE_VERSION" ]; then
    rm -rf -- "$staging_prefix"
    fail "golden template version marker is missing or unsupported"
  fi
  printf '%s\n' "$expected_sha" > "$staging_prefix/.tradejournal-template-sha256"
  mv "$staging_prefix" "$WINEPREFIX"
fi

if [ "$(cat "$WINEPREFIX/.tradejournal-template-version" 2>/dev/null || true)" != "$TEMPLATE_VERSION" ]; then
  fail "persisted Wine prefix has an unsupported version marker"
fi
if [ "$(cat "$WINEPREFIX/.tradejournal-template-sha256" 2>/dev/null || true)" != "$expected_sha" ]; then
  fail "persisted Wine prefix was created from a different golden template"
fi

# Cartella di installazione del terminale (senza assumere un nome fisso per l'eseguibile):
# usata sia per individuare l'EA compilato sia per calcolare il sandbox file dell'EA piu' sotto.
mt5_install_dir="${MT5_TERMINAL_PATH%\\*}"
ea_compiled_windows_path="${mt5_install_dir}\\MQL5\\Experts\\TradeJournal\\TradeJournalBridge.ex5"

terminal_unix="$(windows_path_in_prefix "$MT5_TERMINAL_PATH")" \
  || fail "MT5_TERMINAL_PATH must be an absolute C:\\ path without traversal"
ea_compiled_unix="$(windows_path_in_prefix "$ea_compiled_windows_path")" \
  || fail "MT5_TERMINAL_PATH must be an absolute C:\\ path without traversal"
[ -f "$terminal_unix" ] || fail "terminal64.exe is missing from the persisted Wine prefix"
[ -f "$ea_compiled_unix" ] || fail "compiled TradeJournalBridge.ex5 is missing from the persisted Wine prefix (see scripts/compile_mt5_expert.sh)"
[ -f "$BRIDGE_SCRIPT_UNIX" ] || fail "Linux file-bridge script is missing from the image"

[ -e "$WINEPREFIX/dosdevices/z:" ] \
  || fail "golden template does not expose the standard Wine Z: drive"

[ -n "${MT5_PASSWORD_FILE:-}" ] || fail "MT5_PASSWORD_FILE is required"
[ -f "$MT5_PASSWORD_FILE" ] && [ -r "$MT5_PASSWORD_FILE" ] || fail "MT5_PASSWORD_FILE is not readable"
[ -n "${MT5_BRIDGE_TOKEN_FILE:-}" ] || fail "MT5_BRIDGE_TOKEN_FILE is required"
[ -f "$MT5_BRIDGE_TOKEN_FILE" ] && [ -r "$MT5_BRIDGE_TOKEN_FILE" ] || fail "MT5_BRIDGE_TOKEN_FILE is not readable"

# Solo MT5_PASSWORD_FILE deve diventare un path Wine (Z:): lo legge esclusivamente startup.ini
# sotto Wine, generato piu' sotto. MT5_BRIDGE_TOKEN_FILE resta un path Unix: lo legge solo il
# processo bridge nativo Linux avviato in fondo a questo script, che non sa nulla di Wine (vedi
# bridge/files/file_bridge.py) e non viene mai eseguito sotto wine.
mt5_password_file_wine="$(unix_path_to_wine_z "$MT5_PASSWORD_FILE")"

# Sandbox file dell'EA (MQL5/Files/TradeJournal), condiviso in sola lettura col processo bridge
# nativo Linux tramite MT5_EA_FILES_DIR: un semplice path Unix, calcolato qui una sola volta,
# perche' file_bridge.py non deve mai conoscere convenzioni Wine/WINEPREFIX (vedi il docstring
# di quel modulo).
ea_files_windows_path="${mt5_install_dir}\\MQL5\\Files\\TradeJournal"
ea_files_unix="$(windows_path_in_prefix "$ea_files_windows_path")" \
  || fail "MT5_TERMINAL_PATH must be an absolute C:\\ path without traversal"
mkdir -p -- "$ea_files_unix" || log "WARNING: could not pre-create the EA sandbox directory; the EA will create it on first run"

# connection_id non e' un segreto (solo un UUID di connessione, gia' un label Docker non
# sensibile): scritto qui perche' MQL5 non puo' leggere le variabili d'ambiente del container
# ospitante. Deve esistere PRIMA che il terminale (e quindi l'EA) venga avviato, cosi' e' gia'
# presente al primo OnInit (vedi mt5/experts/TradeJournalBridge.mq5:ReadConnectionId).
printf '%s' "$TJ_CONNECTION_ID" > "$ea_files_unix/connection_id"

XVFB_PID=""
TERMINAL_PID=""
BRIDGE_PID=""
cleanup_started=0

process_is_live() {
  local pid="$1"
  local process_state
  [ -n "$pid" ] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  process_state="$(ps -o stat= -p "$pid" 2>/dev/null | awk 'NR == 1 { print $1 }' || true)"
  case "$process_state" in
    ''|Z*) return 1 ;;
  esac
  return 0
}

terminate_child() {
  local pid="$1"
  local role="$2"
  local timeout_seconds="$3"
  local deadline
  if [ -z "$pid" ]; then
    return
  fi
  if process_is_live "$pid"; then
    kill -TERM "$pid" 2>/dev/null || true
    deadline=$((SECONDS + timeout_seconds))
    while process_is_live "$pid" && [ "$SECONDS" -lt "$deadline" ]; do
      sleep 0.2
    done
    if process_is_live "$pid"; then
      log "$role did not stop before its deadline; forcing termination"
      kill -KILL "$pid" 2>/dev/null || true
    fi
  fi
  wait "$pid" 2>/dev/null || true
}

cleanup() {
  local exit_code="${1:-0}"
  if [ "$cleanup_started" -eq 1 ]; then
    return
  fi
  cleanup_started=1
  # Ignore repeated stop signals while the ordered cleanup is already in progress.
  trap '' INT TERM
  trap - EXIT
  log "stopping bridge, terminal, wineserver and Xvfb"
  terminate_child "$BRIDGE_PID" "bridge" "$CHILD_STOP_TIMEOUT_SECONDS"
  terminate_child "$TERMINAL_PID" "terminal" "$CHILD_STOP_TIMEOUT_SECONDS"
  timeout "${WINESERVER_STOP_TIMEOUT_SECONDS}s" wineserver -k >/dev/null 2>&1 || true
  timeout "${WINESERVER_STOP_TIMEOUT_SECONDS}s" wineserver -w >/dev/null 2>&1 || true
  terminate_child "$XVFB_PID" "Xvfb" "$CHILD_STOP_TIMEOUT_SECONDS"
  exit "$exit_code"
}

on_signal() {
  cleanup 0
}

trap on_signal INT TERM
trap 'cleanup $?' EXIT

# Lo startup.ini vive solo in /home/runtime, un tmpfs (vedi deploy/instance/compose.yaml):
# non tocca mai il volume Wine persistente ne' il golden template, e sparisce con il container.
generate_startup_ini \
  "$MT5_LOGIN" "$MT5_SERVER" "$MT5_PASSWORD_FILE" "$EURUSD_BROKER_SYMBOL" "$STARTUP_INI_PATH"
startup_ini_wine="$(unix_path_to_wine_z "$STARTUP_INI_PATH")"

Xvfb "$DISPLAY" -screen 0 1024x768x16 -nolisten tcp > /tmp/xvfb.log 2>&1 &
XVFB_PID=$!
for _ in $(seq 1 50); do
  if xdpyinfo -display "$DISPLAY" >/dev/null 2>&1; then
    break
  fi
  kill -0 "$XVFB_PID" 2>/dev/null || fail "Xvfb exited before becoming ready"
  sleep 0.2
done
xdpyinfo -display "$DISPLAY" >/dev/null 2>&1 || fail "Xvfb did not become ready"

wineserver -p
# /portable rende deterministico il sandbox file dell'EA (niente hash %APPDATA%, vedi
# ea_files_unix sopra). /config punta a startup.ini per login, avvio automatico dell'Expert
# Advisor sul simbolo/timeframe richiesti e disabilitazione esplicita del trading live
# ([Experts] AllowLiveTrading=0/AllowDllImport=0): nessuna delle due chiavi e' necessaria a un
# EA read-only, ma la loro presenza documenta esplicitamente l'intento anche a chi ispeziona la
# configurazione runtime senza leggere il sorgente MQL5.
wine "$MT5_TERMINAL_PATH" /portable /config:"$startup_ini_wine" > /tmp/terminal.log 2>&1 &
TERMINAL_PID=$!

# La readiness del container NON dipende piu' da questa attesa (a differenza della precedente
# mt5.initialize() sotto Python Windows, il meccanismo che falliva con IPC timeout): e' solo
# diagnostica, bounded da TERMINAL_READY_TIMEOUT_SECONDS, e non fa mai fallire l'avvio. Il vero
# gate di readiness resta l'healthcheck Docker (heartbeat.json recente + GET /health positivo,
# vedi healthcheck-runtime.sh e il relativo start_period/retries in compose.yaml).
log "waiting up to ${TERMINAL_READY_TIMEOUT_SECONDS}s for the EA's first heartbeat (diagnostic only)"
heartbeat_path="$ea_files_unix/heartbeat.json"
deadline=$((SECONDS + TERMINAL_READY_TIMEOUT_SECONDS))
while [ ! -f "$heartbeat_path" ] && [ "$SECONDS" -lt "$deadline" ]; do
  process_is_live "$TERMINAL_PID" || fail "MetaTrader terminal exited during startup"
  process_is_live "$XVFB_PID" || fail "Xvfb exited during startup"
  sleep 1
done
if [ -f "$heartbeat_path" ]; then
  log "EA heartbeat detected"
else
  log "WARNING: no EA heartbeat yet after ${TERMINAL_READY_TIMEOUT_SECONDS}s; starting the file-bridge anyway, the Docker healthcheck will keep the container unhealthy until one appears"
fi

export MT5_EA_FILES_DIR="$ea_files_unix"
log "starting the read-only Linux file-bridge on the internal port ${PORT:-8090}"
python3 "$BRIDGE_SCRIPT_UNIX" &
BRIDGE_PID=$!

# The container is healthy only while all three long-lived children are alive. Reap the first
# process that exits and shut down the remaining processes in the same bounded order used for a
# stop signal. Bash 5.2 in the Ubuntu 24.04 runtime supports wait -n/-p.
EXITED_CHILD=""
set +e
wait -n -p EXITED_CHILD "$BRIDGE_PID" "$TERMINAL_PID" "$XVFB_PID"
child_exit=$?
set -e
case "$EXITED_CHILD" in
  "$BRIDGE_PID") BRIDGE_PID="" ;;
  "$TERMINAL_PID") TERMINAL_PID="" ;;
  "$XVFB_PID") XVFB_PID="" ;;
esac
if [ "$child_exit" -eq 0 ]; then
  child_exit=1
fi
cleanup "$child_exit"
