#!/usr/bin/env bash
# Healthcheck Docker: il worker aggiorna un file di heartbeat ad ogni ciclo di poll
# (worker/main.py:HEARTBEAT_FILE). Se il file manca o e' troppo vecchio, il worker e'
# considerato bloccato/morto.
set -euo pipefail

HEARTBEAT_FILE="${HEARTBEAT_FILE:-/tmp/mt5_worker_heartbeat}"
POLL_INTERVAL_SECONDS="${POLL_INTERVAL_SECONDS:-5}"
MAX_AGE_SECONDS=$((POLL_INTERVAL_SECONDS * 4 + 15))

if [ ! -f "$HEARTBEAT_FILE" ]; then
  echo "healthcheck: heartbeat file assente ($HEARTBEAT_FILE)"
  exit 1
fi

NOW=$(date +%s)
MTIME=$(stat -c %Y "$HEARTBEAT_FILE")
AGE=$((NOW - MTIME))

if [ "$AGE" -gt "$MAX_AGE_SECONDS" ]; then
  echo "healthcheck: heartbeat troppo vecchio (${AGE}s, soglia ${MAX_AGE_SECONDS}s)"
  exit 1
fi

echo "healthcheck: ok (heartbeat aggiornato ${AGE}s fa)"
exit 0
