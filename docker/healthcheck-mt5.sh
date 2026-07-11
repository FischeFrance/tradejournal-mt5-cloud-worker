#!/usr/bin/env bash
# Healthcheck per lo stage real-mt5. Oltre all'heartbeat del worker (stessa logica dello stage
# mock, vedi docker/healthcheck.sh), verifica anche che il display virtuale Xvfb sia ancora in
# esecuzione: se Xvfb muore, Wine (e quindi il terminale MT5) smette di funzionare anche se il
# processo Python worker e' ancora vivo e continua ad aggiornare l'heartbeat.
set -euo pipefail

HEARTBEAT_FILE="${HEARTBEAT_FILE:-/tmp/mt5_worker_heartbeat}"
POLL_INTERVAL_SECONDS="${POLL_INTERVAL_SECONDS:-5}"
MAX_AGE_SECONDS=$((POLL_INTERVAL_SECONDS * 4 + 15))

if ! pgrep -x Xvfb >/dev/null 2>&1; then
  echo "healthcheck-mt5: Xvfb non in esecuzione"
  exit 1
fi

if [ ! -f "$HEARTBEAT_FILE" ]; then
  echo "healthcheck-mt5: heartbeat file assente ($HEARTBEAT_FILE)"
  exit 1
fi

NOW=$(date +%s)
MTIME=$(stat -c %Y "$HEARTBEAT_FILE")
AGE=$((NOW - MTIME))

if [ "$AGE" -gt "$MAX_AGE_SECONDS" ]; then
  echo "healthcheck-mt5: heartbeat troppo vecchio (${AGE}s, soglia ${MAX_AGE_SECONDS}s)"
  exit 1
fi

echo "healthcheck-mt5: ok (Xvfb attivo, heartbeat aggiornato ${AGE}s fa)"
exit 0
