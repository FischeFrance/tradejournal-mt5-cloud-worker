"""Invio degli eventi normalizzati all'ingestion API di TradeJournal.

Regole di sicurezza (vedi README):
- non stampa MAI password o token, ne' in chiaro ne' troncati;
- maschera sempre account_number e server nei log (il payload HTTP reale, cifrato in transito
  da TLS, contiene i valori veri perche' l'API li usa per validare la connessione);
- in DRY_RUN=true non esegue alcuna chiamata di rete: stampa solo il payload sanitizzato;
- esegue un numero limitato di retry con backoff esponenziale sugli errori transitori
  (rete, 408, 425, 429 e 5xx); 401/403 restano recuperabili tramite rotazione token, mentre gli
  altri 4xx sono rifiuti permanenti e non vengono ritentati.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

import requests

logger = logging.getLogger("mt5_worker.event_sender")

_SECRET_KEY_MARKERS = ("password", "token", "secret")
_TRANSIENT_HTTP_STATUSES = frozenset((408, 425, 429))
_RECOVERABLE_AUTH_STATUSES = frozenset((401, 403))


def mask_value(value: Optional[str]) -> str:
    """Maschera un valore per il logging, mantenendo solo un piccolo indizio ai due estremi."""
    if not value:
        return "<vuoto>"
    text = str(value)
    if len(text) <= 4:
        return "*" * len(text)
    return f"{text[:2]}{'*' * (len(text) - 4)}{text[-2:]}"


def sanitize_payload_for_log(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Copia il payload per il logging, mascherando account_number/server e rimuovendo
    difensivamente qualunque campo che assomigli a una credenziale (non dovrebbe mai essercene,
    ma un payload non e' mai sicuro finche' non lo si e' verificato)."""
    sanitized: Dict[str, Any] = {}
    account_number = payload.get("account_number")
    masked_account = mask_value(None if account_number is None else str(account_number))
    for key, value in payload.items():
        lowered = key.lower()
        if any(marker in lowered for marker in _SECRET_KEY_MARKERS):
            sanitized[key] = "<redacted>"
        elif key in ("account_number", "server"):
            sanitized[key] = mask_value(value if value is None else str(value))
        elif key == "event_id" and account_number:
            sanitized[key] = str(value).replace(str(account_number), masked_account)
        else:
            sanitized[key] = value
    return sanitized


@dataclass
class SendResult:
    status: str  # "sent" | "dry_run" | "failed"
    http_status: Optional[int] = None
    error: Optional[str] = None
    attempts: int = 0
    failure_type: Optional[str] = None  # None | "transient" | "permanent"

    @property
    def retryable(self) -> bool:
        return self.failure_type == "transient"


class EventSender:
    def __init__(
        self,
        api_url: Optional[str],
        bridge_token: Optional[str],
        dry_run: bool,
        max_retries: int = 3,
        backoff_base_seconds: float = 0.5,
        max_backoff_seconds: float = 8.0,
        timeout_seconds: float = 5.0,
        sleep_fn=time.sleep,
    ) -> None:
        self.api_url = api_url
        self.bridge_token = bridge_token
        self.dry_run = dry_run
        self.max_retries = max_retries
        self.backoff_base_seconds = backoff_base_seconds
        self.max_backoff_seconds = max_backoff_seconds
        self.timeout_seconds = timeout_seconds
        self._sleep = sleep_fn

    def send(self, payload: Dict[str, Any]) -> SendResult:
        sanitized = sanitize_payload_for_log(payload)
        sanitized_event_id = sanitized.get("event_id")

        if self.dry_run:
            logger.info("DRY_RUN attivo, evento NON inviato. Payload sanitizzato: %s", sanitized)
            return SendResult(status="dry_run", attempts=0)

        if not self.api_url or not self.bridge_token:
            logger.error(
                "Impossibile inviare l'evento: TRADEJOURNAL_API_URL o TRADEJOURNAL_BRIDGE_TOKEN "
                "non configurati. Payload sanitizzato: %s",
                sanitized,
            )
            # E' una configurazione correggibile al riavvio: l'outbox deve conservare l'evento,
            # non archiviarlo definitivamente in dead-letter.
            return SendResult(
                status="failed",
                error="missing_api_target",
                attempts=0,
                failure_type="transient",
            )

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.bridge_token}",
        }

        last_error: Optional[str] = None
        last_http_status: Optional[int] = None
        for attempt in range(1, self.max_retries + 1):
            try:
                response = requests.post(
                    self.api_url,
                    json=payload,
                    headers=headers,
                    timeout=self.timeout_seconds,
                )
            except requests.RequestException as exc:
                # Alcune eccezioni requests includono gli header (Authorization compreso) nella
                # propria stringa. Registrare soltanto il tipo mantiene i secret fuori dai log.
                last_error = type(exc).__name__
                logger.warning(
                    "Tentativo %s/%s fallito (errore %s) per event_id=%s.",
                    attempt,
                    self.max_retries,
                    type(exc).__name__,
                    sanitized_event_id,
                )
            else:
                if response.status_code < 300:
                    logger.info(
                        "Evento inviato con successo (status=%s, event_id=%s). Payload: %s",
                        response.status_code,
                        sanitized_event_id,
                        sanitized,
                    )
                    return SendResult(status="sent", http_status=response.status_code, attempts=attempt)

                if response.status_code in _RECOVERABLE_AUTH_STATUSES:
                    logger.error(
                        "Autenticazione API rifiutata (status=%s), event_id=%s; evento "
                        "conservato per una rotazione token.",
                        response.status_code,
                        sanitized_event_id,
                    )
                    return SendResult(
                        status="failed",
                        http_status=response.status_code,
                        error="authentication_failed",
                        attempts=attempt,
                        failure_type="transient",
                    )

                is_transient_http = (
                    response.status_code in _TRANSIENT_HTTP_STATUSES
                    or 500 <= response.status_code <= 599
                )
                if not is_transient_http:
                    # Un 4xx non recuperabile (422 payload rifiutato, ecc.) non puo' migliorare
                    # ritentando lo stesso payload. Anche una risposta
                    # HTTP inattesa fuori da 2xx/4xx/5xx viene classificata fail-closed come
                    # permanente, invece di creare un retry infinito.
                    logger.error(
                        "Evento rifiutato dall'API (status=%s, non ritentabile), event_id=%s. "
                        "Payload sanitizzato: %s",
                        response.status_code,
                        sanitized_event_id,
                        sanitized,
                    )
                    return SendResult(
                        status="failed",
                        http_status=response.status_code,
                        error="rejected_by_api",
                        attempts=attempt,
                        failure_type="permanent",
                    )

                last_error = f"http_{response.status_code}"
                last_http_status = response.status_code
                logger.warning(
                    "Tentativo %s/%s fallito (status=%s) per event_id=%s",
                    attempt,
                    self.max_retries,
                    response.status_code,
                    sanitized_event_id,
                )

            if attempt < self.max_retries:
                backoff = min(self.backoff_base_seconds * (2 ** (attempt - 1)), self.max_backoff_seconds)
                self._sleep(backoff)

        logger.error(
            "Invio definitivamente fallito dopo %s tentativi per event_id=%s: %s",
            self.max_retries,
            sanitized_event_id,
            last_error,
        )
        return SendResult(
            status="failed",
            http_status=last_http_status,
            error=last_error,
            attempts=self.max_retries,
            failure_type="transient",
        )
