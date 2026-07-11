#!/usr/bin/env bash
# Campiona rapidamente CPU/RAM del worker in esecuzione. Utile per la checklist 24/48 ore del
# README (verificare che il consumo resti stabile nel tempo, senza leak).
set -euo pipefail

cd "$(dirname "$0")/.."

COMPOSE_FILE="docker-compose.mock.yml"
if [ "${1:-}" = "--real" ]; then
  COMPOSE_FILE="docker-compose.yml"
fi

CONTAINER_ID=$(docker compose -f "$COMPOSE_FILE" ps -q worker)
if [ -z "$CONTAINER_ID" ]; then
  echo "[benchmark.sh] Nessun container in esecuzione per $COMPOSE_FILE. Avvialo prima con scripts/start.sh." >&2
  exit 1
fi

SAMPLES="${SAMPLES:-5}"
INTERVAL_SECONDS="${INTERVAL_SECONDS:-2}"

echo "[benchmark.sh] Campiono CPU/RAM per $SAMPLES volte ogni ${INTERVAL_SECONDS}s (container=$CONTAINER_ID)..."
for _ in $(seq 1 "$SAMPLES"); do
  docker stats --no-stream --format "table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}" "$CONTAINER_ID"
  sleep "$INTERVAL_SECONDS"
done

echo
echo "[benchmark.sh] Limiti configurati in $COMPOSE_FILE: vedi le chiavi mem_limit/cpus del servizio."
echo "[benchmark.sh] Stato healthcheck:"
docker inspect --format "{{.State.Health.Status}}" "$CONTAINER_ID" 2>/dev/null || echo "healthcheck non disponibile"
