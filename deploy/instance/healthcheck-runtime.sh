#!/usr/bin/env bash
set -euo pipefail

# The real image must keep Config.User=root so its main entrypoint can prepare a fresh named
# volume. Docker therefore starts healthchecks as root too. Drop to the same unprivileged UID as
# the bridge before opening the host-owned 0400 secret; the mock image already runs as that UID.
if [ "$(id -u)" -eq 0 ]; then
  exec gosu runtime:runtime "$0" "$@"
fi

TOKEN_FILE="${MT5_BRIDGE_TOKEN_FILE:-}"
TOKEN="${MT5_BRIDGE_TOKEN:-}"

if [ -n "$TOKEN" ] && [ -n "$TOKEN_FILE" ]; then
  echo "healthcheck: ambiguous bridge token configuration" >&2
  exit 1
fi
if [ -n "$TOKEN_FILE" ]; then
  if [ ! -f "$TOKEN_FILE" ] || [ ! -r "$TOKEN_FILE" ]; then
    echo "healthcheck: bridge token file is not readable" >&2
    exit 1
  fi
  TOKEN="$(cat -- "$TOKEN_FILE")"
fi
if [ -z "$TOKEN" ]; then
  echo "healthcheck: bridge token is missing" >&2
  exit 1
fi
case "$TOKEN" in
  *$'\n'*|*$'\r'*)
    echo "healthcheck: bridge token must contain a single line" >&2
    exit 1
    ;;
esac

# Escape the only two special characters in curl's double-quoted config syntax. Newlines are
# rejected above, so the secret cannot inject another curl option into the stdin configuration.
TOKEN="${TOKEN//\\/\\\\}"
TOKEN="${TOKEN//\"/\\\"}"

# Supplying curl configuration on stdin keeps the bearer token out of the process argument
# list. jq accepts only a JSON object whose explicit runtime status is "ok"; both the response
# body and parser/curl diagnostics stay out of Docker health logs.
if ! curl --config - 2>/dev/null <<EOF | jq -e 'type == "object" and .status == "ok"' >/dev/null 2>&1
url = "http://127.0.0.1:${PORT:-8090}/health"
header = "Authorization: Bearer ${TOKEN}"
fail
silent
connect-timeout = 2
max-time = 4
EOF
then
  echo "healthcheck: runtime is not healthy" >&2
  exit 1
fi
