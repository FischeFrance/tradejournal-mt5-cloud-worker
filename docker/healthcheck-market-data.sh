#!/usr/bin/env bash
# Healthcheck del market-data-worker: stessa logica di heartbeat di docker/healthcheck.sh
# (trade-sync worker), ma file e soglia dedicati perche' il market-data-worker ha un poll
# interval indipendente (MARKET_DATA_POLL_SECONDS, tipicamente piu' lungo di POLL_INTERVAL_SECONDS).
set -euo pipefail

HEARTBEAT_FILE="${MARKET_DATA_HEARTBEAT_FILE:-/tmp/mt5_market_data_worker_heartbeat}"
MARKET_DATA_POLL_SECONDS="${MARKET_DATA_POLL_SECONDS:-60}"
MAX_AGE_SECONDS=$((MARKET_DATA_POLL_SECONDS * 4 + 30))

if [ ! -f "$HEARTBEAT_FILE" ]; then
  echo "healthcheck-market-data: heartbeat file assente ($HEARTBEAT_FILE)"
  exit 1
fi

NOW=$(date +%s)
MTIME=$(stat -c %Y "$HEARTBEAT_FILE")
AGE=$((NOW - MTIME))

if [ "$AGE" -gt "$MAX_AGE_SECONDS" ]; then
  echo "healthcheck-market-data: heartbeat troppo vecchio (${AGE}s, soglia ${MAX_AGE_SECONDS}s)"
  exit 1
fi

echo "healthcheck-market-data: ok (heartbeat aggiornato ${AGE}s fa)"
exit 0
