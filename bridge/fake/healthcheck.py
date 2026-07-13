#!/usr/bin/env python3
"""Healthcheck Docker del fake bridge: GET /health con Bearer, richiede status 200. Solo
standard library (nessuna dipendenza da installare solo per l'healthcheck)."""

from __future__ import annotations

import os
import sys
import urllib.request

token = os.environ.get("MT5_BRIDGE_TOKEN", "")
port = os.environ.get("PORT", "8080")
url = f"http://127.0.0.1:{port}/health"

request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
try:
    with urllib.request.urlopen(request, timeout=3) as response:
        sys.exit(0 if response.status == 200 else 1)
except Exception:
    sys.exit(1)
