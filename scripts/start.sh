#!/usr/bin/env bash
# Avvia il worker via docker compose. Di default usa il compose "mock" (Fase 1, nessuna
# dipendenza da Wine/MT5). Passare --real per usare invece docker-compose.yml (Fase 2, non
# pronto out-of-the-box: vedi README prima di farlo).
set -euo pipefail

cd "$(dirname "$0")/.."

COMPOSE_FILE="docker-compose.mock.yml"
if [ "${1:-}" = "--real" ]; then
  COMPOSE_FILE="docker-compose.yml"
  echo "[start.sh] ATTENZIONE: stai avviando il compose 'reale' (Wine/MT5), non pronto out-of-the-box. Leggi il README." >&2
fi

if [ ! -f .env ]; then
  echo "[start.sh] Nessun .env trovato, copio .env.example -> .env (valori di default: mock + dry-run)."
  cp .env.example .env
fi

docker compose -f "$COMPOSE_FILE" up -d --build
docker compose -f "$COMPOSE_FILE" ps
