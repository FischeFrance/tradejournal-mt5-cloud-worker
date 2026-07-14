#!/usr/bin/env bash
set -euo pipefail

readonly TEMPLATE_VERSION="tradejournal-mt5-prefix-v1"
readonly STAGED_WINE_STOP_TIMEOUT_SECONDS=5

WINESERVER_BIN="${WINESERVER_BIN:-wineserver}"
WORK_DIR=""
STAGED_PREFIX=""
TEMP_ARCHIVE=""
TEMP_CHECKSUM=""

usage() {
  echo "Usage: create_mt5_runtime_template.sh SOURCE_WINEPREFIX OUTPUT.tar.zst" >&2
}

fail() {
  echo "create_mt5_runtime_template: ERROR: $*" >&2
  exit 1
}

stop_staged_wine() {
  if [ -z "$STAGED_PREFIX" ] || [ ! -d "$STAGED_PREFIX" ]; then
    return
  fi
  if ! command -v "$WINESERVER_BIN" >/dev/null 2>&1; then
    return
  fi
  WINEPREFIX="$STAGED_PREFIX" \
    timeout "${STAGED_WINE_STOP_TIMEOUT_SECONDS}s" "$WINESERVER_BIN" -k \
    >/dev/null 2>&1 || true
  WINEPREFIX="$STAGED_PREFIX" \
    timeout "${STAGED_WINE_STOP_TIMEOUT_SECONDS}s" "$WINESERVER_BIN" -w \
    >/dev/null 2>&1 || true
}

cleanup() {
  stop_staged_wine
  if [ -n "$WORK_DIR" ]; then
    rm -rf -- "$WORK_DIR"
  fi
  if [ -n "$TEMP_ARCHIVE" ]; then
    rm -f -- "$TEMP_ARCHIVE"
  fi
  if [ -n "$TEMP_CHECKSUM" ]; then
    rm -f -- "$TEMP_CHECKSUM"
  fi
}

# Install cleanup before the first mktemp so every partially-created private path is covered.
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if [ "$#" -ne 2 ]; then
  usage
  exit 64
fi
case "$1$2" in
  *$'\n'*|*$'\r'*) fail "source and output paths must each be a single line" ;;
esac

SOURCE_PREFIX="$(realpath "$1")" || fail "source Wine prefix cannot be resolved"
OUTPUT_PATH="$(realpath -m "$2")" || fail "output path cannot be resolved"
OUTPUT_DIR="$(dirname "$OUTPUT_PATH")"
MT5_TERMINAL_PATH="${MT5_TERMINAL_PATH:-C:\\Program Files\\MetaTrader 5\\terminal64.exe}"
# Nessun Python Windows in questa architettura (vedi mt5/experts/TradeJournalBridge.mq5 e
# bridge/files/file_bridge.py): l'unico artefatto aggiuntivo richiesto nel golden template e'
# l'Expert Advisor gia' compilato, prodotto da scripts/compile_mt5_expert.sh.
EA_COMPILED_WINDOWS_PATH="${MT5_TERMINAL_PATH%\\*}\\MQL5\\Experts\\TradeJournal\\TradeJournalBridge.ex5"

[ -d "$SOURCE_PREFIX" ] || fail "source Wine prefix is not a directory"
[ "$SOURCE_PREFIX" != "/" ] || fail "the filesystem root cannot be used as a Wine prefix"
[ -d "$OUTPUT_DIR" ] || fail "output directory does not exist"
case "$OUTPUT_PATH" in
  "$SOURCE_PREFIX"|"$SOURCE_PREFIX"/*)
    fail "output archive cannot be placed inside the source prefix"
    ;;
esac

for command_name in \
  realpath pgrep find grep cp tar zstd sha256sum mktemp timeout \
  "$WINESERVER_BIN"; do
  command -v "$command_name" >/dev/null 2>&1 \
    || fail "a required command is unavailable"
done

# Wine creates several helper processes whose command names vary across releases. Exact names
# cover normal Linux task names; full-command patterns also cover preloader name truncation and
# Windows children. The policy is intentionally global and conservative for one-time template
# creation on the dedicated VPS.
ACTIVE_PROCESS_NAMES=(
  wine wine64 wine-preloader wine64-preloader wineserver wineserver64
  terminal.exe terminal64.exe metaeditor.exe metaeditor64.exe
  metatester.exe metatester64.exe
  services.exe explorer.exe winedevice.exe winedevice64.exe
  plugplay.exe rpcss.exe svchost.exe conhost.exe wineboot.exe start.exe cmd.exe
)
ACTIVE_PROCESS_PATTERNS=(
  '(^|/)(wine|wine64|wine-preloader|wine64-preloader)([[:space:]]|$)'
  '(^|/)(wineserver|wineserver64)([[:space:]]|$)'
  '(^|/)(terminal|terminal64|metaeditor|metaeditor64|metatester|metatester64)\.exe([[:space:]]|$)'
  '(^|/)(services|explorer|winedevice|winedevice64|plugplay|rpcss|svchost|conhost|wineboot|start|cmd)\.exe([[:space:]]|$)'
)

assert_runtime_inactive() {
  local process_name process_pattern
  for process_name in "${ACTIVE_PROCESS_NAMES[@]}"; do
    if pgrep -x -- "$process_name" >/dev/null 2>&1; then
      fail "Wine/MetaTrader is active; stop every runtime before creating the template"
    fi
  done
  for process_pattern in "${ACTIVE_PROCESS_PATTERNS[@]}"; do
    if pgrep -f -- "$process_pattern" >/dev/null 2>&1; then
      fail "Wine/MetaTrader is active; stop every runtime before creating the template"
    fi
  done
}

windows_path_in_prefix() {
  local prefix="$1"
  local windows_path="$2"
  local relative_path
  case "$windows_path" in
    [Cc]:\\*) relative_path="${windows_path:3}" ;;
    *) return 1 ;;
  esac
  relative_path="${relative_path//\\//}"
  case "/${relative_path}/" in
    */../*|*/./*) return 1 ;;
  esac
  printf '%s/drive_c/%s\n' "$prefix" "$relative_path"
}

path_is_confined() {
  local prefix="$1"
  local path="$2"
  local resolved
  resolved="$(realpath "$path")" || return 1
  case "$resolved" in
    "$prefix"/*) return 0 ;;
    *) return 1 ;;
  esac
}

# Matches are case-insensitive because Wine applications can create case variants on a
# case-sensitive Linux filesystem. Any object at a denied path (regular file, directory or
# symlink) aborts creation; the script never removes it automatically.
DENYLIST_GLOBS=(
  '*/config/accounts.dat'
  '*/config/account*.dat'
  '*/config/common.ini'
  '*/config/community.ini'
  '*/config/login.ini'
  '*/config/network.ini'
  '*/config/servers.dat'
  '*/config/terminal.ini'
  '*/logs/*'
  '*/mql5/logs/*'
  '*/mql5/profiles/*'
  # Sandbox file dell'EA (mt5/experts/TradeJournalBridge.mq5): un template preparato dopo aver
  # gia' avviato l'EA una volta potrebbe contenere account/positions/deal reali di quella sessione.
  '*/mql5/files/tradejournal/*'
  '*/appdata/roaming/microsoft/credentials/*'
  '*/appdata/roaming/microsoft/protect/*'
)

# Registry files are necessary Wine-prefix infrastructure, so they are top-level allowlisted.
# Their content is separately denied when it contains credential/session value names or an MT5
# account/credential/session registry section. Only stable rule identifiers are logged.
REGISTRY_RULE_NAMES=(
  registry-credential-or-session-value
  registry-mt5-account-section
)
REGISTRY_DENY_PATTERNS=(
  '^[[:space:]]*"(Login|LastLogin|Account|AccountNumber|Password|InvestorPassword|RememberPassword|SavePassword|Token|AccessToken|RefreshToken|Session|SessionId|AutoLogin)"[[:space:]]*='
  '^\[[^]]*(MetaQuotes|MetaTrader)[^]]*(Account|Credential|Session)[^]]*\]'
)

check_top_level_allowlist() {
  local prefix="$1"
  local entry name
  local invalid=0
  while IFS= read -r -d '' entry; do
    name="$(basename "$entry")"
    case "$name" in
      drive_c|dosdevices)
        [ -d "$entry" ] && [ ! -L "$entry" ] || invalid=1
        ;;
      system.reg|user.reg|userdef.reg|.update-timestamp|winetricks.log)
        [ -f "$entry" ] && [ ! -L "$entry" ] || invalid=1
        ;;
      *) invalid=1 ;;
    esac
  done < <(find "$prefix" -mindepth 1 -maxdepth 1 -print0)
  [ "$invalid" -eq 0 ] \
    || fail "source prefix contains a non-allowlisted or invalid top-level entry"
}

check_unexpected_symlinks() {
  local prefix="$1"
  local ignored
  while IFS= read -r -d '' ignored; do
    fail "source prefix contains a symlink outside the allowlisted dosdevices directory"
  done < <(find "$prefix/drive_c" -type l -print0)
}

check_denylist() {
  local prefix="$1"
  local rule ignored matched
  for rule in "${DENYLIST_GLOBS[@]}"; do
    matched=0
    while IFS= read -r -d '' ignored; do
      matched=1
      break
    done < <(find "$prefix" -mindepth 1 -ipath "$prefix/$rule" -print0)
    if [ "$matched" -eq 1 ]; then
      printf 'create_mt5_runtime_template: denylist rule matched: %s\n' "$rule" >&2
      fail "potential account/session artefacts found; inspect and clean the prefix explicitly"
    fi
  done
}

check_registry_policy() {
  local prefix="$1"
  local registry_file rule_index
  for registry_file in system.reg user.reg userdef.reg; do
    [ -e "$prefix/$registry_file" ] || continue
    [ -f "$prefix/$registry_file" ] && [ ! -L "$prefix/$registry_file" ] \
      || fail "a Wine registry entry has an invalid file type"
    for rule_index in "${!REGISTRY_DENY_PATTERNS[@]}"; do
      if grep -Eiq -- "${REGISTRY_DENY_PATTERNS[$rule_index]}" "$prefix/$registry_file"; then
        printf 'create_mt5_runtime_template: registry deny rule matched: %s\n' \
          "${REGISTRY_RULE_NAMES[$rule_index]}" >&2
        fail "potential account/session registry state found; inspect and clean it explicitly"
      fi
    done
  done
}

check_prefix_policy() {
  local prefix="$1"
  check_top_level_allowlist "$prefix"
  check_denylist "$prefix"
  check_unexpected_symlinks "$prefix"
  check_registry_policy "$prefix"
}

assert_runtime_inactive

source_terminal="$(windows_path_in_prefix "$SOURCE_PREFIX" "$MT5_TERMINAL_PATH")" \
  || fail "MT5_TERMINAL_PATH must be an absolute C:\\ path without traversal"
source_ea="$(windows_path_in_prefix "$SOURCE_PREFIX" "$EA_COMPILED_WINDOWS_PATH")" \
  || fail "MT5_TERMINAL_PATH must be an absolute C:\\ path without traversal"
[ -f "$source_terminal" ] && path_is_confined "$SOURCE_PREFIX" "$source_terminal" \
  || fail "terminal64.exe is missing or escapes the source prefix"
[ -f "$source_ea" ] && path_is_confined "$SOURCE_PREFIX" "$source_ea" \
  || fail "compiled TradeJournalBridge.ex5 is missing or escapes the source prefix (see scripts/compile_mt5_expert.sh)"
check_prefix_policy "$SOURCE_PREFIX"

WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/tj-mt5-template.XXXXXXXX")"
WORK_DIR="$(realpath "$WORK_DIR")" || fail "temporary work directory cannot be resolved"
STAGED_PREFIX="$WORK_DIR/prefix"
TEMP_ARCHIVE="$(mktemp "$OUTPUT_DIR/.mt5-prefix-archive.tmp.XXXXXXXX")"
TEMP_CHECKSUM="$(mktemp "$OUTPUT_DIR/.mt5-prefix-checksum.tmp.XXXXXXXX")"
mkdir -m 0700 "$STAGED_PREFIX"

# The source is read only. Runtime activity is checked immediately around the copy so a process
# that appears during the snapshot invalidates the result before Wine is ever run on staging.
assert_runtime_inactive
cp -a --reflink=auto "$SOURCE_PREFIX/." "$STAGED_PREFIX/"
assert_runtime_inactive

staged_ea="$(windows_path_in_prefix "$STAGED_PREFIX" "$EA_COMPILED_WINDOWS_PATH")" \
  || fail "staged Expert Advisor path is invalid"
[ -f "$staged_ea" ] && path_is_confined "$STAGED_PREFIX" "$staged_ea" \
  || fail "staged TradeJournalBridge.ex5 is missing or escapes the staging prefix"
assert_runtime_inactive

check_prefix_policy "$STAGED_PREFIX"
printf '%s\n' "$TEMPLATE_VERSION" > "$STAGED_PREFIX/.tradejournal-template-version"

tar --zstd -cf "$TEMP_ARCHIVE" -C "$STAGED_PREFIX" .
archive_sha="$(sha256sum "$TEMP_ARCHIVE" | awk '{print $1}')"
printf '%s  %s\n' "$archive_sha" "$(basename "$OUTPUT_PATH")" > "$TEMP_CHECKSUM"
chmod 0640 "$TEMP_ARCHIVE" "$TEMP_CHECKSUM"

# Each published file appears through an atomic rename in the destination filesystem. Consumers
# always verify the archive digest, so a missing/stale sidecar is rejected rather than trusted.
mv -f -- "$TEMP_ARCHIVE" "$OUTPUT_PATH"
TEMP_ARCHIVE=""
mv -f -- "$TEMP_CHECKSUM" "$OUTPUT_PATH.sha256"
TEMP_CHECKSUM=""

echo "create_mt5_runtime_template: template created successfully"
echo "create_mt5_runtime_template: SHA-256: $archive_sha"
