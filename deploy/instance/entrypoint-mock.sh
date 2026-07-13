#!/usr/bin/env bash
set -euo pipefail

# The fake bridge uses the same *_FILE contract as the real bridge. Keeping the file path in
# the process environment means the secret itself never appears in Compose, docker inspect or
# startup logs.
if [ -n "${MT5_BRIDGE_TOKEN:-}" ] && [ -n "${MT5_BRIDGE_TOKEN_FILE:-}" ]; then
  echo "[mt5-runtime-mock] ambiguous bridge token configuration" >&2
  exit 64
fi
if [ -z "${MT5_BRIDGE_TOKEN:-}" ] && [ -z "${MT5_BRIDGE_TOKEN_FILE:-}" ]; then
  echo "[mt5-runtime-mock] bridge token file is required" >&2
  exit 64
fi

echo "[mt5-runtime-mock] starting isolated fake bridge on the internal port ${PORT:-8090}"
cd /app/bridge/fake
exec python fake_bridge.py
