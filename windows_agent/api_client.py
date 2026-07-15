from __future__ import annotations

import random
import time
from typing import Callable
from urllib.parse import urljoin, urlparse

import httpx

MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 0.5
BACKOFF_MAX_SECONDS = 5.0


class AgentApiClient:
    API_VERSION = "1"
    def __init__(
        self,
        base_url: str,
        token: str,
        transport: httpx.BaseTransport | None = None,
        max_retries: int = MAX_RETRIES,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        parsed = urlparse(base_url)
        if parsed.scheme != "https" and parsed.hostname not in (
            "localhost",
            "127.0.0.1",
            "::1",
        ):
            raise ValueError("HTTPS required except loopback tests")
        self.base_url, self._origin = (
            base_url.rstrip("/") + "/",
            (parsed.scheme, parsed.hostname, parsed.port),
        )
        self.client = httpx.Client(
            transport=transport,
            timeout=10,
            follow_redirects=False,
            headers={"Authorization": f"Bearer {token}"},
        )
        self._max_retries = max_retries
        self._sleep = sleep_fn

    def request(self, method: str, path: str, payload: dict | None = None) -> dict:
        url = urljoin(self.base_url, path.lstrip("/"))
        attempt = 0
        while True:
            attempt += 1
            try:
                response = self.client.request(method, url, json=payload)
            except httpx.TransportError:
                if attempt > self._max_retries:
                    raise
                self._sleep(self._backoff_delay(attempt))
                continue
            if response.is_redirect:
                location = response.headers.get("location", "")
                target = urlparse(urljoin(url, location))
                if (target.scheme, target.hostname, target.port) != self._origin:
                    raise RuntimeError("cross-host redirect refused")
                raise RuntimeError("redirect refused")
            if response.status_code == 204:
                return {}
            if response.status_code == 409:
                # lease_lost is a structured, expected response for heartbeat/transition
                # per contracts/mt5-agent-v1/schema.json, not a transport-level error.
                return response.json()
            if response.status_code >= 500 and attempt <= self._max_retries:
                self._sleep(self._backoff_delay(attempt))
                continue
            response.raise_for_status()
            return response.json()

    def _backoff_delay(self, attempt: int) -> float:
        capped = min(BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)), BACKOFF_MAX_SECONDS)
        return capped + random.uniform(0, capped * 0.25)

    def claim(self) -> dict:
        return self.request("POST", "claim", {"api_version": self.API_VERSION})

    def heartbeat(self, job_id: str, lease_id: str) -> dict:
        return self.request("POST", f"jobs/{job_id}/heartbeat", {"api_version": self.API_VERSION, "lease_id": lease_id})

    def transition(self, job_id: str, lease_id: str, status: str, result: dict | None = None) -> dict:
        if status not in ("running", "complete", "fail"):
            raise ValueError("invalid transition")
        payload = {"api_version": self.API_VERSION, "lease_id": lease_id, **(result or {})}
        return self.request("POST", f"jobs/{job_id}/{status}", payload)
