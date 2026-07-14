#!/usr/bin/env bash
set -euo pipefail

# Compila deterministicamente mt5/experts/TradeJournalBridge.mq5 in .ex5 con MetaEditor sotto
# Wine, per essere installato nel golden template (vedi scripts/create_mt5_runtime_template.sh
# e docs/provisioning.md). Nessun segreto e' coinvolto: la compilazione non richiede alcuna
# credenziale MT5, solo un WINEPREFIX con MetaTrader 5 (e quindi MetaEditor) gia' installati.
#
# Se la compilazione headless di MetaEditor non fosse disponibile o affidabile in un dato
# ambiente CI, il fallback documentato e' la compilazione manuale via GUI: aprire
# TradeJournalBridge.mq5 in MetaEditor sotto Wine, F7 per compilare, verificare "0 error(s)" nel
# pannello Toolbox/Errors, poi copiare il .ex5 prodotto nella stessa cartella da cui questo
# script lo pubblicherebbe (vedi docs/provisioning.md).
#
# NOTA su un limite noto di MetaEditor: alcune versioni restituiscono un codice di uscita 0
# anche in presenza di errori di compilazione. Per questo il segnale di successo primario e'
# la presenza del file .ex5 appena prodotto (rimosso esplicitamente prima di compilare, cosi'
# un eventuale file residuo di una compilazione precedente non puo' mascherare un fallimento),
# non il solo exit code. Il log di MetaEditor viene comunque sempre stampato per revisione
# umana.

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly DEFAULT_MQ5_SOURCE="$SCRIPT_DIR/../mt5/experts/TradeJournalBridge.mq5"
readonly EA_RELATIVE_DIR='MQL5\Experts\TradeJournal'
readonly EA_FILE_STEM="TradeJournalBridge"

WINE_BIN="${WINE_BIN:-wine}"
LOG_TMP=""

usage() {
  cat >&2 <<'EOF'
Usage: compile_mt5_expert.sh WINEPREFIX [MQ5_SOURCE_PATH]
  WINEPREFIX       Wine prefix with MetaTrader 5 (and MetaEditor) already installed
  MQ5_SOURCE_PATH  Defaults to mt5/experts/TradeJournalBridge.mq5 in this repository
EOF
}

fail() {
  echo "compile_mt5_expert: ERROR: $*" >&2
  exit 1
}

log() {
  printf 'compile_mt5_expert: %s\n' "$*"
}

cleanup() {
  if [ -n "$LOG_TMP" ]; then
    rm -f -- "$LOG_TMP"
  fi
}
trap cleanup EXIT

if [ "$#" -lt 1 ] || [ "$#" -gt 2 ]; then
  usage
  exit 64
fi

WINEPREFIX="$(realpath "$1")" || fail "Wine prefix cannot be resolved"
export WINEPREFIX
MQ5_SOURCE="$(realpath "${2:-$DEFAULT_MQ5_SOURCE}")" || fail "MQ5 source path cannot be resolved"
MT5_TERMINAL_PATH="${MT5_TERMINAL_PATH:-C:\\Program Files\\MetaTrader 5\\terminal64.exe}"

[ -d "$WINEPREFIX" ] || fail "WINEPREFIX is not a directory"
[ -f "$MQ5_SOURCE" ] || fail "MQ5 source file does not exist: $MQ5_SOURCE"
command -v "$WINE_BIN" >/dev/null 2>&1 || fail "wine is not available"
command -v iconv >/dev/null 2>&1 || fail "iconv is not available (needed to read MetaEditor's UTF-16 log)"

windows_path_in_prefix() {
  local windows_path="$1"
  local relative_path
  case "$windows_path" in
    [Cc]:\\*) relative_path="${windows_path:3}" ;;
    *) return 1 ;;
  esac
  relative_path="${relative_path//\\//}"
  case "/${relative_path}/" in
    */../*|*/./*) return 1 ;;
  esac
  printf '%s/drive_c/%s\n' "$WINEPREFIX" "$relative_path"
}

unix_path_to_wine_z() {
  local unix_path="$1"
  case "$unix_path" in
    /*) ;;
    *) return 1 ;;
  esac
  printf 'Z:%s\n' "${unix_path//\//\\}"
}

mt5_install_dir_windows="${MT5_TERMINAL_PATH%\\*}"

metaeditor_windows_path="${mt5_install_dir_windows}\\metaeditor64.exe"
metaeditor_unix="$(windows_path_in_prefix "$metaeditor_windows_path")" \
  || fail "MT5_TERMINAL_PATH must be an absolute C:\\ path without traversal"
[ -f "$metaeditor_unix" ] || fail "metaeditor64.exe not found in the Wine prefix: $metaeditor_unix"

ea_dir_windows_path="${mt5_install_dir_windows}\\${EA_RELATIVE_DIR}"
ea_dir_unix="$(windows_path_in_prefix "$ea_dir_windows_path")" \
  || fail "MT5_TERMINAL_PATH must be an absolute C:\\ path without traversal"
mkdir -p -- "$ea_dir_unix"

dest_mq5_unix="$ea_dir_unix/$EA_FILE_STEM.mq5"
dest_ex5_unix="$ea_dir_unix/$EA_FILE_STEM.ex5"
cp -f -- "$MQ5_SOURCE" "$dest_mq5_unix"
# Rimosso esplicitamente: la sua ricomparsa dopo la compilazione e' il segnale di successo
# primario (vedi nota in testa al file sul limite noto di MetaEditor sull'exit code).
rm -f -- "$dest_ex5_unix"

LOG_TMP="$(mktemp)"
log_windows_path="$(unix_path_to_wine_z "$LOG_TMP")"
mq5_windows_path="${ea_dir_windows_path}\\${EA_FILE_STEM}.mq5"

log "compiling ${EA_FILE_STEM}.mq5 with MetaEditor (headless)"
set +e
WINEDEBUG=-all "$WINE_BIN" "$metaeditor_unix" \
  "/compile:${mq5_windows_path}" "/log:${log_windows_path}" \
  >/dev/null 2>&1
compile_exit=$?
set -e

log_text=""
if [ -s "$LOG_TMP" ]; then
  # MetaEditor scrive il log in UTF-16LE con BOM: iconv lo rende leggibile per la revisione
  # umana. Se dovesse fallire (versione/locale diversi), il contenuto grezzo viene comunque
  # stampato piuttosto che nascosto.
  log_text="$(iconv -f UTF-16LE -t UTF-8 -- "$LOG_TMP" 2>/dev/null || true)"
  if [ -z "$log_text" ]; then
    log_text="$(cat -- "$LOG_TMP")"
  fi
fi
if [ -n "$log_text" ]; then
  printf -- '--- MetaEditor log ---\n%s\n----------------------\n' "$log_text"
fi

if [ "$compile_exit" -ne 0 ]; then
  log "WARNING: MetaEditor exited with status $compile_exit"
fi
[ -f "$dest_ex5_unix" ] || fail "compilation did not produce ${EA_FILE_STEM}.ex5 (see log above)"

log "compiled successfully: $dest_ex5_unix"
log "next step: run scripts/create_mt5_runtime_template.sh on this prefix to publish the golden template"
