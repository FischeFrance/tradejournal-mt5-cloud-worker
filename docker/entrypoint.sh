#!/usr/bin/env bash
# Entrypoint del container: nessuna logica di business qui, solo avvio del worker Python.
set -euo pipefail

if [ "${MOCK_MODE:-true}" != "true" ]; then
  echo "[entrypoint] MOCK_MODE=false: e' richiesto un terminale MT5 raggiungibile (Wine + " \
       "installer fornito manualmente). Vedi README, sezione 'Test reale MT5 + Wine su Ubuntu'." >&2
fi

cd /app/worker
exec python main.py
