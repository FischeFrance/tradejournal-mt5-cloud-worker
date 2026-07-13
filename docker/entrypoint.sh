#!/usr/bin/env bash
# Entrypoint del container: nessuna logica di business qui, solo un messaggio operativo sulla
# sorgente selezionata e l'avvio del worker Python. Lo stage Docker `mock` e' riusato anche dal
# client bridge perche' e' il runtime Linux leggero senza il pacchetto MetaTrader5.
set -euo pipefail

MT5_CLIENT_SOURCE="${MT5_CLIENT_SOURCE:-}"
if [ -z "$MT5_CLIENT_SOURCE" ]; then
  if [ "${MOCK_MODE:-true}" = "true" ]; then
    MT5_CLIENT_SOURCE="mock"
  else
    MT5_CLIENT_SOURCE="bridge"
  fi
fi

case "$MT5_CLIENT_SOURCE" in
  bridge)
    echo "[entrypoint] trade-sync via mt5-bridge HTTP; nessun MetaTrader5 nel Python Linux."
    ;;
  direct)
    echo "[entrypoint] ATTENZIONE: client MetaTrader5 diretto predisposto ma non validato. " \
         "Il runtime raccomandato con MT5 reale e' MT5_CLIENT_SOURCE=bridge." >&2
    ;;
esac

cd /app/worker
exec python main.py
