#!/usr/bin/env bash
# Entrypoint del market-data-worker: processo separato dal trade-sync worker (docker/entrypoint.sh
# avvia main.py, questo avvia market_data_main.py). Nessuna logica di business qui.
set -euo pipefail

if [ "${ENABLE_MARKET_DATA:-false}" != "true" ]; then
  echo "[entrypoint-market-data] ATTENZIONE: ENABLE_MARKET_DATA=${ENABLE_MARKET_DATA:-false}. " \
       "Questo container esiste solo per la raccolta dati di mercato in modalita' research: " \
       "market_data_main.py si fermera' subito con un errore di configurazione esplicito." >&2
fi

cd /app/worker
exec python market_data_main.py
