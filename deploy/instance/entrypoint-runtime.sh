#!/usr/bin/env bash
set -euo pipefail

readonly RUNTIME_USER="runtime"
readonly RUNTIME_GROUP="runtime"
readonly PREFIX_ROOT="/var/lib/tradejournal/wine-prefix"
readonly TEMPLATE_ARCHIVE="/opt/tradejournal/template/mt5-prefix.tar.zst"
readonly TEMPLATE_VERSION="tradejournal-mt5-prefix-v1"
readonly BRIDGE_SCRIPT_UNIX="/app/bridge/windows/mt5_bridge.py"
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

if [ "$(id -u)" -eq 0 ]; then
  mkdir -p "$PREFIX_ROOT"
  chown "$RUNTIME_USER:$RUNTIME_GROUP" "$PREFIX_ROOT"
  exec gosu "$RUNTIME_USER:$RUNTIME_GROUP" "$0" "$@"
fi

umask 077

MT5_TERMINAL_PATH="${MT5_TERMINAL_PATH:-C:\\Program Files\\MetaTrader 5\\terminal64.exe}"
PYTHON_WINDOWS_PATH="${PYTHON_WINDOWS_PATH:-C:\\Python311Embed\\python.exe}"
TERMINAL_READY_TIMEOUT_SECONDS="${TERMINAL_READY_TIMEOUT_SECONDS:-120}"

case "$TERMINAL_READY_TIMEOUT_SECONDS" in
  ''|*[!0-9]*) fail "TERMINAL_READY_TIMEOUT_SECONDS must be a positive integer" ;;
esac
if [ "$TERMINAL_READY_TIMEOUT_SECONDS" -lt 1 ]; then
  fail "TERMINAL_READY_TIMEOUT_SECONDS must be a positive integer"
fi

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

terminal_unix="$(windows_path_in_prefix "$MT5_TERMINAL_PATH")" \
  || fail "MT5_TERMINAL_PATH must be an absolute C:\\ path without traversal"
python_unix="$(windows_path_in_prefix "$PYTHON_WINDOWS_PATH")" \
  || fail "PYTHON_WINDOWS_PATH must be an absolute C:\\ path without traversal"
[ -f "$terminal_unix" ] || fail "terminal64.exe is missing from the persisted Wine prefix"
[ -f "$python_unix" ] || fail "Windows Python is missing from the persisted Wine prefix"
[ -f "$BRIDGE_SCRIPT_UNIX" ] || fail "Windows bridge script is missing from the image"

# Python Windows needs Wine paths for host-mounted secret files. Only paths are converted and
# exported; secret contents are never read or printed by this entrypoint. Conversion is purely
# lexical so Wine is not started before Xvfb and the explicit wineserver startup below.
[ -e "$WINEPREFIX/dosdevices/z:" ] \
  || fail "golden template does not expose the standard Wine Z: drive"
for secret_var in MT5_PASSWORD_FILE MT5_BRIDGE_TOKEN_FILE; do
  secret_path="${!secret_var:-}"
  [ -n "$secret_path" ] || fail "$secret_var is required"
  [ -f "$secret_path" ] && [ -r "$secret_path" ] || fail "$secret_var is not readable"
  printf -v "$secret_var" '%s' "$(unix_path_to_wine_z "$secret_path")"
  export "$secret_var"
done

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
wine "$MT5_TERMINAL_PATH" > /tmp/terminal.log 2>&1 &
TERMINAL_PID=$!

log "waiting for the terminal IPC endpoint"
deadline=$((SECONDS + TERMINAL_READY_TIMEOUT_SECONDS))
while ! wine "$PYTHON_WINDOWS_PATH" -c \
  'import sys, MetaTrader5 as mt5; ok=mt5.initialize(path=sys.argv[1]); info=mt5.terminal_info() if ok else None; mt5.shutdown() if ok else None; raise SystemExit(0 if info is not None else 1)' \
  "$MT5_TERMINAL_PATH" >/dev/null 2>&1; do
  process_is_live "$TERMINAL_PID" || fail "MetaTrader terminal exited during startup"
  process_is_live "$XVFB_PID" || fail "Xvfb exited during terminal startup"
  if [ "$SECONDS" -ge "$deadline" ]; then
    fail "MetaTrader terminal did not become available before the startup timeout"
  fi
  sleep 1
done

bridge_windows_path="$(unix_path_to_wine_z "$BRIDGE_SCRIPT_UNIX")"
log "starting the read-only MT5 bridge on the internal port ${PORT:-8090}"
wine "$PYTHON_WINDOWS_PATH" "$bridge_windows_path" &
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
