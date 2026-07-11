#!/usr/bin/env bash
# Entrypoint dello stage real-mt5 (Fase 2). Diverso da docker/entrypoint.sh (usato dallo stage
# mock): qui bisogna anche avviare un display virtuale (Xvfb) prima del worker, perche' Wine e
# il terminale MT5 lo richiedono anche in esecuzione headless. Nessuna logica di business qui
# oltre a questo: il worker Python resta l'unico responsabile del poll/invio eventi.
#
# NOTA IMPORTANTE (vedi README, "Limiti noti Wine/MT5"): questo script avvia Xvfb e verifica la
# presenza di un terminale MT5 in MT5_TERMINAL_DIR, ma NON installa ne' avvia automaticamente il
# terminale stesso -- va predisposto manualmente (vedi README, "Preparazione runtime MT5") prima
# che RealMt5Client riesca a connettersi.
set -uo pipefail

MT5_TERMINAL_DIR="${MT5_TERMINAL_DIR:-/opt/mt5}"
DISPLAY_NUM="${DISPLAY:-:99}"

echo "[entrypoint-real] MOCK_MODE=${MOCK_MODE:-false}. Avvio display virtuale Xvfb su ${DISPLAY_NUM}..."
Xvfb "${DISPLAY_NUM}" -screen 0 1024x768x16 -nolisten tcp &
XVFB_PID=$!

# Attende che il display sia effettivamente pronto prima di proseguire (max ~5s), senza bloccare
# l'avvio all'infinito se xdpyinfo non e' disponibile per qualche motivo.
for _ in $(seq 1 25); do
  if xdpyinfo -display "${DISPLAY_NUM}" >/dev/null 2>&1; then
    echo "[entrypoint-real] Xvfb pronto su ${DISPLAY_NUM}."
    break
  fi
  sleep 0.2
done

if [ -f "${MT5_TERMINAL_DIR}/terminal64.exe" ]; then
  echo "[entrypoint-real] Terminale MT5 trovato in ${MT5_TERMINAL_DIR}/terminal64.exe."
else
  echo "[entrypoint-real] ATTENZIONE: nessun terminal64.exe in ${MT5_TERMINAL_DIR}. Il worker" \
       "partira' comunque ma RealMt5Client non riuscira' a connettersi finche' non viene" \
       "fornito un terminale MT5 valido (vedi README, sezione 'Preparazione runtime MT5')." >&2
fi

cd /app/worker
python main.py &
WORKER_PID=$!

_shutdown() {
  echo "[entrypoint-real] Segnale di arresto ricevuto, inoltro al worker (pid ${WORKER_PID})..."
  kill -TERM "${WORKER_PID}" 2>/dev/null || true
}
trap _shutdown TERM INT

wait "${WORKER_PID}"
WORKER_EXIT=$?

echo "[entrypoint-real] Worker terminato (exit=${WORKER_EXIT}). Arresto Xvfb (pid ${XVFB_PID})..."
kill "${XVFB_PID}" 2>/dev/null || true
wait "${XVFB_PID}" 2>/dev/null || true

exit "${WORKER_EXIT}"
