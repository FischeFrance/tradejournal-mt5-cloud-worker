#!/usr/bin/env python3
"""Healthcheck standalone del file-bridge: GET /health con Bearer, richiede status 200 e
`status: "ok"` nel corpo (un 200 con stato "degraded" indica un heartbeat scaduto o l'EA non
ancora pronto, non deve risultare healthy). Solo standard library.

Non usato direttamente da Docker HEALTHCHECK (quello resta deploy/instance/healthcheck-runtime.sh,
che gira come utente non privilegiato e non stampa mai la risposta): utile per debug manuale e
per i test che non vogliono dipendere da bash/curl/jq.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common import read_secret_from_env  # noqa: E402

try:
    token = read_secret_from_env("MT5_BRIDGE_TOKEN")
except ValueError:
    sys.exit(1)
if not token:
    sys.exit(1)

port = os.environ.get("PORT", "8080")
url = f"http://127.0.0.1:{port}/health"

request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
try:
    with urllib.request.urlopen(request, timeout=3) as response:
        if response.status != 200:
            sys.exit(1)
        payload = json.loads(response.read().decode("utf-8"))
except Exception:
    sys.exit(1)

sys.exit(0 if isinstance(payload, dict) and payload.get("status") == "ok" else 1)
