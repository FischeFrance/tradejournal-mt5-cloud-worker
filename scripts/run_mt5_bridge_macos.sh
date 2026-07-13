#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd -P)"
BRIDGE_PATH="$REPO_ROOT/bridge/windows/mt5_bridge.py"

WINEPREFIX="${WINEPREFIX:-$HOME/Library/Application Support/net.metaquotes.wine.metatrader5}"
WINE_BIN="${WINE_BIN:-/Applications/MetaTrader 5.app/Contents/SharedSupport/wine/bin/wine}"
PYTHON_WINDOWS_PATH="${PYTHON_WINDOWS_PATH:-C:\\Python311Embed\\python.exe}"
WINEPATH_BIN="$(dirname "$WINE_BIN")/winepath"

fail() {
  printf '[mt5-bridge] Error: %s\n' "$*" >&2
  exit 1
}

masked_state() {
  if [[ -n "$1" ]]; then
    printf '<set; hidden>'
  else
    printf '<unset>'
  fi
}

[[ -d "$WINEPREFIX" ]] || fail "Wine prefix not found: $WINEPREFIX"
[[ -x "$WINE_BIN" ]] || fail "Wine executable not found or not executable: $WINE_BIN"
[[ -x "$WINEPATH_BIN" ]] || fail "winepath not found or not executable: $WINEPATH_BIN"
[[ -f "$BRIDGE_PATH" ]] || fail "bridge not found: $BRIDGE_PATH"

export WINEPREFIX

if ! PYTHON_UNIX_PATH="$("$WINEPATH_BIN" -u "$PYTHON_WINDOWS_PATH")"; then
  fail "cannot resolve Windows Python path: $PYTHON_WINDOWS_PATH"
fi
PYTHON_UNIX_PATH="${PYTHON_UNIX_PATH//$'\r'/}"
[[ -f "$PYTHON_UNIX_PATH" ]] || fail "Windows Python not found: $PYTHON_WINDOWS_PATH"

if ! BRIDGE_WINDOWS_PATH="$("$WINEPATH_BIN" -w "$BRIDGE_PATH")"; then
  fail "cannot convert bridge path for Wine: $BRIDGE_PATH"
fi
BRIDGE_WINDOWS_PATH="${BRIDGE_WINDOWS_PATH//$'\r'/}"
[[ -n "$BRIDGE_WINDOWS_PATH" ]] || fail "winepath returned an empty bridge path"

printf '%s\n' \
  "[mt5-bridge] Starting the read-only MT5 bridge" \
  "[mt5-bridge] WINEPREFIX=$WINEPREFIX" \
  "[mt5-bridge] WINE_BIN=$WINE_BIN" \
  "[mt5-bridge] PYTHON_WINDOWS_PATH=$PYTHON_WINDOWS_PATH" \
  "[mt5-bridge] BRIDGE_PATH=$BRIDGE_PATH" \
  "[mt5-bridge] MT5_SESSION_MODE=${MT5_SESSION_MODE:-login}" \
  "[mt5-bridge] MT5_TERMINAL_PATH=${MT5_TERMINAL_PATH:-<unset>}" \
  "[mt5-bridge] HOST=${HOST:-0.0.0.0}" \
  "[mt5-bridge] PORT=${PORT:-8080}" \
  "[mt5-bridge] MT5_LOGIN=$(masked_state "${MT5_LOGIN:-}")" \
  "[mt5-bridge] MT5_PASSWORD=$(masked_state "${MT5_PASSWORD:-}")" \
  "[mt5-bridge] MT5_SERVER=$(masked_state "${MT5_SERVER:-}")" \
  "[mt5-bridge] MT5_EXPECTED_LOGIN=$(masked_state "${MT5_EXPECTED_LOGIN:-}")" \
  "[mt5-bridge] MT5_EXPECTED_SERVER=$(masked_state "${MT5_EXPECTED_SERVER:-}")" \
  "[mt5-bridge] MT5_BRIDGE_TOKEN=$(masked_state "${MT5_BRIDGE_TOKEN:-}")"

exec "$WINE_BIN" "$PYTHON_WINDOWS_PATH" "$BRIDGE_WINDOWS_PATH"
