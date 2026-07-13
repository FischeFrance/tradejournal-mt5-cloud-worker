"""Scaffolding HTTP condiviso tra bridge/fake/fake_bridge.py e bridge/windows/mt5_bridge.py.

Stesso contratto (GET /health, POST /v1/candles, POST /v1/trading/snapshot, autenticazione
Bearer, envelope JSON e validazione richieste) per entrambi: cio' che cambia tra fake e reale e'
solo la sorgente dei dati e lo stato di salute riportato, mai il protocollo HTTP. Solo standard
library: nessuna dipendenza da installare, ne' nell'immagine Docker del fake bridge ne' in un
futuro Windows Python sotto Wine (dove installare pacchetti extra oltre a `MetaTrader5` e' un
passo manuale in piu' da evitare quando non necessario).

Questo modulo non importa mai MetaTrader5 e non sa nulla di Wine: e' puro protocollo HTTP.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler
from typing import Optional, Tuple

#: Durata di una candela per timeframe, in secondi. Stesse sigle usate da MARKET_TIMEFRAMES e da
#: worker/market_data_source.py: EURUSD e' l'unico simbolo nello scope di questa fase.
TIMEFRAME_SECONDS = {
    "M1": 60,
    "M5": 5 * 60,
    "M15": 15 * 60,
    "H1": 60 * 60,
    "H4": 4 * 60 * 60,
    "D1": 24 * 60 * 60,
}

#: Limite massimo di candele per risposta, protetto lato server indipendentemente da cosa chiede
#: il client: un client mal configurato (o compromesso) non puo' costringere il bridge a caricare
#: /restituire una quantita' di dati arbitraria in un colpo solo.
MAX_LIMIT = 1000
DEFAULT_LIMIT = 100

#: Finestra dello storico deal usata da POST /v1/trading/snapshot. Il limite lato server evita
#: che un client mal configurato chieda a MT5 una scansione arbitrariamente ampia; valori sopra
#: il massimo vengono troncati, mentre zero, negativi e tipi diversi da int sono rifiutati.
DEFAULT_DEAL_LOOKBACK_HOURS = 24
MAX_DEAL_LOOKBACK_HOURS = 168

#: Header di test, MAI inviato dal client di produzione (worker/market_data_source.py -
#: Mt5MarketDataSource): permette ai test di fissare deterministicamente il concetto di "adesso"
#: del bridge, per verificare l'esclusione della candela corrente in formazione senza dipendere
#: dall'orologio reale della macchina che esegue i test.
NOW_OVERRIDE_HEADER = "X-Mt5-Bridge-Now-Override"


class BridgeError(Exception):
    """Errore da restituire al client come risposta HTTP strutturata (mai un crash/500 nudo per
    un errore di validazione prevedibile: vedi API_ERRORS nel README per i codici usati)."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class BridgeConfig:
    """Configurazione minima condivisa da fake e reale: token di autenticazione e simbolo broker
    accettato. Deliberatamente NON contiene mai credenziali MT5 (login/password/server/terminal
    path): quelle sono lette solo da bridge/windows/mt5_bridge.py, mai da questo modulo comune."""

    def __init__(self, token: str, broker_symbol: str, port: int = 8080, host: str = "0.0.0.0") -> None:
        if not token:
            raise ValueError("MT5_BRIDGE_TOKEN e' obbligatorio per avviare il bridge.")
        if not broker_symbol:
            raise ValueError("EURUSD_BROKER_SYMBOL non puo' essere vuoto.")
        self.token = token
        self.broker_symbol = broker_symbol
        self.port = port
        self.host = host


def parse_iso_utc(value: object, field_name: str) -> datetime:
    """Analizza un timestamp ISO8601 UTC (suffisso 'Z' o '+00:00'): solleva BridgeError (422) se
    non e' una stringa, non e' analizzabile, o non e' in UTC (offset diverso da zero)."""
    if not isinstance(value, str):
        raise BridgeError(422, f"invalid_{field_name}", f"'{field_name}' deve essere una stringa ISO8601.")
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise BridgeError(
            422, f"invalid_{field_name}", f"'{field_name}' non e' un timestamp ISO8601 valido: '{value}'."
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise BridgeError(422, f"invalid_{field_name}", f"'{field_name}' deve essere in UTC (offset zero): '{value}'.")
    return parsed


def format_iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class BaseBridgeHandler(BaseHTTPRequestHandler):
    """Helper HTTP/auth/parsing condivisi dal contratto mt5-bridge. Le sottoclassi concrete
    (bridge/fake/fake_bridge.py, bridge/windows/mt5_bridge.py) implementano do_GET/do_POST
    usando questi helper e forniscono la propria sorgente di candele/stato di salute: nessuna
    logica di business (dati sintetici o MetaTrader5 reale) vive in questa classe."""

    server_version = "Mt5Bridge/1.0"
    config: BridgeConfig  # impostata dalla sottoclasse concreta prima di avviare il server

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - firma richiesta dalla stdlib
        # Solo indirizzo/metodo/path/status: mai header (Authorization compreso) ne' body.
        sys.stderr.write("[mt5-bridge] %s - %s\n" % (self.address_string(), fmt % args))

    def send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_bridge_error(self, exc: BridgeError) -> None:
        self.send_json(exc.status, {"error": {"code": exc.code, "message": exc.message}})

    def check_auth(self) -> bool:
        if self.headers.get("Authorization") != f"Bearer {self.config.token}":
            self.send_bridge_error(BridgeError(401, "unauthorized", "Token di autenticazione mancante o non valido."))
            return False
        return True

    def read_json_body(self) -> dict:
        content_length = int(self.headers.get("Content-Length", "0") or "0")
        raw_body = self.rfile.read(content_length) if content_length else b""
        try:
            request = json.loads(raw_body.decode("utf-8")) if raw_body else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BridgeError(400, "invalid_json", "Corpo della richiesta non e' JSON valido.") from exc
        if not isinstance(request, dict):
            raise BridgeError(422, "invalid_request", "Il corpo della richiesta deve essere un oggetto JSON.")
        return request

    def parse_candles_request(self, request: dict) -> Tuple[str, str, Optional[datetime], datetime, int]:
        """Valida request/query per POST /v1/candles secondo il contratto (vedi README):
        symbol deve combaciare con il broker_symbol configurato, timeframe uno dei sei
        supportati, limit un intero positivo (troncato a MAX_LIMIT), since opzionale UTC."""
        symbol = request.get("symbol")
        timeframe = request.get("timeframe")
        since_raw = request.get("since")
        limit = request.get("limit", DEFAULT_LIMIT)

        if symbol != self.config.broker_symbol:
            raise BridgeError(422, "unsupported_symbol", f"Simbolo non supportato: {symbol!r}.")
        if timeframe not in TIMEFRAME_SECONDS:
            raise BridgeError(422, "unsupported_timeframe", f"Timeframe non supportato: {timeframe!r}.")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise BridgeError(422, "invalid_limit", "'limit' deve essere un intero positivo.")
        limit = min(limit, MAX_LIMIT)

        since = parse_iso_utc(since_raw, "since") if since_raw is not None else None

        now = datetime.now(timezone.utc)
        now_override = self.headers.get(NOW_OVERRIDE_HEADER)
        if now_override:
            now = parse_iso_utc(now_override, "now")

        return symbol, timeframe, since, now, limit

    def parse_trading_snapshot_request(self, request: dict) -> int:
        """Valida il body facoltativo di POST /v1/trading/snapshot.

        L'unico campo ammesso e' ``deal_lookback_hours``. Il default e' 24 ore; interi positivi
        oltre 168 vengono troncati al massimo sicuro. ``bool`` e' escluso esplicitamente anche se
        in Python e' una sottoclasse di ``int``.
        """
        unexpected = set(request) - {"deal_lookback_hours"}
        if unexpected:
            names = ", ".join(sorted(str(name) for name in unexpected))
            raise BridgeError(422, "invalid_request", f"Campi non supportati nella richiesta: {names}.")

        lookback = request.get("deal_lookback_hours", DEFAULT_DEAL_LOOKBACK_HOURS)
        if isinstance(lookback, bool) or not isinstance(lookback, int) or lookback <= 0:
            raise BridgeError(
                422,
                "invalid_deal_lookback_hours",
                "'deal_lookback_hours' deve essere un intero positivo.",
            )
        return min(lookback, MAX_DEAL_LOOKBACK_HOURS)
