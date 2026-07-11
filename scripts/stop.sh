#!/usr/bin/env bash
# Ferma il worker avviato con scripts/start.sh. Passare --real per fermare il compose "reale".
set -euo pipefail

cd "$(dirname "$0")/.."

COMPOSE_FILE="docker-compose.mock.yml"
if [ "${1:-}" = "--real" ]; then
  COMPOSE_FILE="docker-compose.yml"
fi

docker compose -f "$COMPOSE_FILE" down
