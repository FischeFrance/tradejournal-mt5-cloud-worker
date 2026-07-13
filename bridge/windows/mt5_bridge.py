"""mt5-bridge reale per il runtime Python Windows/MetaTrader5 sotto Wine.

L'integrazione di base (import del package, initialize, letture account/terminale/posizioni/ordini
e shutdown) e' stata validata anche con il Wine dell'app ufficiale MetaTrader 5 su macOS Apple
Silicon. Restano separati i test automatici di questo modulo, che usano un doppio MetaTrader5 e
non dipendono mai da Wine, da un terminale reale o da credenziali.

Pensato per girare come Python Windows sotto Wine (`wine python.exe bridge\\windows\\mt5_bridge.py`),
nello stesso WINEPREFIX del terminale MetaTrader 5, perche' il pacchetto Python `MetaTrader5' e'
un'estensione nativa Windows: non e' importabile da un interprete Linux (nemmeno sotto lo stesso
container Wine, se il processo Python che lo importa non e' anch'esso Windows -- stesso limite
gia' documentato in worker/mt5_client.py:RealMt5Client per il trade-sync worker).

Implementa lo stesso contratto HTTP di bridge/fake/fake_bridge.py (vedi bridge/common.py):
GET /health, POST /v1/candles e POST /v1/trading/snapshot. Nessun endpoint di trading e nessuna
chiamata a order_send in questo file, deliberatamente: questo servizio e' di sola lettura.

Configurazione della sessione MT5 letta solo da qui, mai dal market-data-worker Linux (vedi
worker/config.py, che non la legge affatto). ``MT5_SESSION_MODE=login`` conserva il login
esplicito con MT5_LOGIN / MT5_PASSWORD / MT5_SERVER; ``MT5_SESSION_MODE=existing`` si collega
invece alla sessione gia' aperta nel terminale indicato da MT5_TERMINAL_PATH, senza mai chiamare
``mt5.login()``. Login, server, password e token non vengono mai stampati in chiaro: i valori
identificativi sono mascherati e i segreti redatti (vedi _mask e _Mt5Session._sanitize_error).

Sicurezza: usare esclusivamente la password INVESTOR (sola lettura) dell'account MT5, mai quella
di trading (stesso principio di worker/mt5_client.py e README).
"""

from __future__ import annotations

import os
import signal
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from http.server import ThreadingHTTPServer
from typing import Any, Callable, Optional, TypeVar

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common import (  # noqa: E402
    TIMEFRAME_SECONDS,
    BaseBridgeHandler,
    BridgeConfig,
    BridgeError,
    format_iso_utc,
    read_secret_from_env,
)

T = TypeVar("T")

#: Mapping sigla -> costante MetaTrader5. Valorizzato in modo lazy (vedi _import_mt5) perche' il
#: modulo MetaTrader5 non e' importabile fuori da Windows: leggere questo dict richiede aver gia'
#: chiamato _import_mt5() almeno una volta nel processo corrente.
_TIMEFRAME_MT5_CONSTANT_NAMES = {
    "M1": "TIMEFRAME_M1",
    "M5": "TIMEFRAME_M5",
    "M15": "TIMEFRAME_M15",
    "H1": "TIMEFRAME_H1",
    "H4": "TIMEFRAME_H4",
    "D1": "TIMEFRAME_D1",
}

MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 1.0
SESSION_MODE_LOGIN = "login"
SESSION_MODE_EXISTING = "existing"
SESSION_MODES = {SESSION_MODE_LOGIN, SESSION_MODE_EXISTING}


def _epoch_to_utc_z(epoch_seconds: Any) -> str:
    return format_iso_utc(datetime.fromtimestamp(int(epoch_seconds), tz=timezone.utc))


def _order_direction(order_type: Any) -> str:
    # Enum MT5: BUY/BUY_LIMIT/BUY_STOP/BUY_STOP_LIMIT = 0/2/4/6; i corrispondenti SELL sono
    # 1/3/5/7. Tenere il mapping esplicito rende evidente che non si sta eseguendo alcuna azione.
    return "buy" if int(order_type) in {0, 2, 4, 6} else "sell"


def _mask(value: Optional[str]) -> str:
    if not value:
        return "<vuoto>"
    text = str(value)
    if len(text) <= 4:
        return "*" * len(text)
    return f"{text[:2]}{'*' * (len(text) - 4)}{text[-2:]}"


class Mt5BridgeConfig(BridgeConfig):
    def __init__(self) -> None:
        super().__init__(
            token=read_secret_from_env("MT5_BRIDGE_TOKEN"),
            broker_symbol=os.environ.get("EURUSD_BROKER_SYMBOL") or "EURUSD",
            port=int(os.environ.get("PORT", "8080")),
            host=os.environ.get("HOST", "0.0.0.0"),
        )
        self.mt5_login = os.environ.get("MT5_LOGIN", "")
        self.mt5_password = read_secret_from_env("MT5_PASSWORD")
        self.mt5_server = os.environ.get("MT5_SERVER", "")
        self.mt5_terminal_path = os.environ.get("MT5_TERMINAL_PATH", "")
        self.mt5_session_mode = os.environ.get("MT5_SESSION_MODE", SESSION_MODE_LOGIN)
        self.mt5_expected_login = os.environ.get("MT5_EXPECTED_LOGIN", "")
        self.mt5_expected_server = os.environ.get("MT5_EXPECTED_SERVER", "")

        if self.mt5_session_mode not in SESSION_MODES:
            raise ValueError(
                "MT5_SESSION_MODE non valido: i valori ammessi sono 'login' ed 'existing'."
            )

        if self.mt5_session_mode == SESSION_MODE_EXISTING and not self.mt5_terminal_path:
            raise ValueError(
                "MT5_TERMINAL_PATH e' obbligatorio quando MT5_SESSION_MODE=existing."
            )

        if self.mt5_session_mode == SESSION_MODE_LOGIN and (
            not self.mt5_login or not self.mt5_password or not self.mt5_server
        ):
            raise ValueError(
                "MT5_LOGIN / MT5_PASSWORD / MT5_SERVER sono obbligatori quando "
                "MT5_SESSION_MODE=login. MT5_PASSWORD deve essere la password INVESTOR "
                "(sola lettura), mai quella di trading (vedi README)."
            )
        if self.mt5_session_mode == SESSION_MODE_LOGIN:
            try:
                int(self.mt5_login)
            except ValueError:
                raise ValueError(
                    "MT5_LOGIN deve essere un identificativo numerico quando "
                    "MT5_SESSION_MODE=login."
                ) from None


class _Mt5Session:
    """Incapsula lo stato di connessione al terminale MT5 (lazy import, selezione della sessione
    e shutdown), con un numero limitato di retry sulle operazioni potenzialmente transitorie
    (IPC col terminale non ancora pronto). Stesso schema di
    worker/mt5_client.py:RealMt5Client, non condiviso via import per la stessa ragione di _mask
    (bridge/ indipendente da worker/)."""

    def __init__(
        self,
        config: Mt5BridgeConfig,
        sleep_fn: Callable[[float], None] = time.sleep,
        mt5_module: Any = None,
    ) -> None:
        self._config = config
        self._sleep = sleep_fn
        self._injected_mt5 = mt5_module
        self._mt5 = None
        self._connected = False
        # MetaTrader5 condivide una singola sessione IPC. ThreadingHTTPServer puo' servire
        # /candles, /health e /trading/snapshot in parallelo: serializziamo ogni operazione MT5
        # completa, in particolare le quattro letture che compongono uno snapshot.
        self._operation_lock = threading.RLock()

    def _import_mt5(self):
        if self._injected_mt5 is not None:
            return self._injected_mt5
        try:
            import MetaTrader5 as mt5  # type: ignore[import-not-found]
        except ImportError as exc:
            raise BridgeError(
                503, "mt5_unavailable",
                "Pacchetto 'MetaTrader5' non disponibile: questo bridge deve girare come Python "
                "Windows sotto Wine con il pacchetto installato (vedi bridge/windows/requirements.txt "
                "e README).",
            ) from exc
        return mt5

    def _sanitize_error(self, error: BaseException) -> str:
        """Rimuove dalla diagnostica qualunque valore sensibile noto alla configurazione.

        Anche un'eccezione sollevata direttamente dall'estensione MetaTrader5 potrebbe includere
        gli argomenti della chiamata nel proprio testo. La sanitizzazione viene quindi applicata
        sia ai messaggi di retry sia all'errore finale restituito dal bridge.
        """
        text = str(error)
        replacements = [
            (getattr(self._config, "mt5_password", ""), "<redacted>"),
            (getattr(self._config, "token", ""), "<redacted>"),
            (
                getattr(self._config, "mt5_login", ""),
                _mask(getattr(self._config, "mt5_login", "")),
            ),
            (
                getattr(self._config, "mt5_server", ""),
                _mask(getattr(self._config, "mt5_server", "")),
            ),
            (
                getattr(self._config, "mt5_expected_login", ""),
                _mask(getattr(self._config, "mt5_expected_login", "")),
            ),
            (
                getattr(self._config, "mt5_expected_server", ""),
                _mask(getattr(self._config, "mt5_expected_server", "")),
            ),
        ]
        # Sostituire prima i valori piu' lunghi evita che un identificativo contenuto in un
        # altro valore impedisca la redazione completa di quest'ultimo.
        for raw_value, replacement in sorted(replacements, key=lambda item: len(item[0]), reverse=True):
            if raw_value:
                text = text.replace(raw_value, replacement)
        return text

    def _call_with_retry(self, description: str, fn: Callable[[], T]) -> T:
        last_error = "errore sconosciuto"
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                return fn()
            except Exception as exc:  # noqa: BLE001 - qualunque errore del pacchetto MetaTrader5
                last_error = self._sanitize_error(exc)
                sys.stderr.write(
                    f"[mt5-bridge] tentativo {attempt}/{MAX_RETRIES} fallito per "
                    f"'{description}': {last_error}\n"
                )
                if attempt < MAX_RETRIES:
                    self._sleep(RETRY_DELAY_SECONDS * attempt)
        raise BridgeError(502, "mt5_error", f"'{description}' fallita dopo {MAX_RETRIES} tentativi: {last_error}")

    def connect(self) -> None:
        session_mode = self._config.mt5_session_mode
        login_id = int(self._config.mt5_login) if session_mode == SESSION_MODE_LOGIN else None
        connected_identity = {
            "login": _mask(self._config.mt5_login),
            "server": _mask(self._config.mt5_server),
        }

        def _do_connect() -> None:
            mt5 = self._import_mt5()
            init_kwargs = {}
            if self._config.mt5_terminal_path:
                init_kwargs["path"] = self._config.mt5_terminal_path
            if not mt5.initialize(**init_kwargs):
                raise RuntimeError(f"initialize() fallito: {mt5.last_error()}")

            if session_mode == SESSION_MODE_LOGIN:
                authorized = mt5.login(
                    login_id,
                    password=self._config.mt5_password,
                    server=self._config.mt5_server,
                )
                if not authorized:
                    raise RuntimeError(
                        f"login() fallito: {mt5.last_error()} "
                        "(credenziali non stampate per sicurezza)"
                    )

            # La connessione e la health non dipendono da alcun simbolo. Un broker puo' usare
            # suffissi (per esempio EURUSD.a) oppure rifiutare temporaneamente symbol_select()
            # pur mantenendo terminale e account perfettamente disponibili. La disponibilita'
            # del simbolo viene quindi verificata solo quando /v1/candles lo richiede.
            account = mt5.account_info()
            if account is None:
                raise RuntimeError(
                    f"account_info() dopo la connessione ha restituito None: {mt5.last_error()}"
                )

            actual_login = str(account.login)
            actual_server = str(account.server)
            expected_login = self._config.mt5_expected_login
            expected_server = self._config.mt5_expected_server
            if expected_login and actual_login != expected_login:
                raise RuntimeError(
                    "MT5_EXPECTED_LOGIN non coincide con l'account della sessione "
                    f"connessa (atteso={_mask(expected_login)}, "
                    f"effettivo={_mask(actual_login)})."
                )
            if expected_server and actual_server != expected_server:
                raise RuntimeError(
                    "MT5_EXPECTED_SERVER non coincide con il server della sessione "
                    f"connessa (atteso={_mask(expected_server)}, "
                    f"effettivo={_mask(actual_server)})."
                )
            connected_identity["login"] = _mask(actual_login)
            connected_identity["server"] = _mask(actual_server)
            self._mt5 = mt5

        self._call_with_retry("connessione al terminale MT5", _do_connect)
        self._connected = True
        sys.stderr.write(
            f"[mt5-bridge] Connesso al terminale MT5 (session_mode={session_mode}, "
            f"server={connected_identity['server']}, login={connected_identity['login']}).\n"
        )

    def health(self) -> dict:
        with self._operation_lock:
            return self._health_unlocked()

    def _health_unlocked(self) -> dict:
        if not self._connected or self._mt5 is None:
            server_hint = getattr(self._config, "mt5_server", "") or getattr(
                self._config, "mt5_expected_server", ""
            )
            return {
                "status": "degraded",
                "terminal_connected": False,
                "account_connected": False,
                "server": _mask(server_hint),
                "version": "unknown",
            }
        info = self._mt5.account_info()
        terminal = self._mt5.terminal_info()
        return {
            "status": "ok" if info is not None and terminal is not None else "degraded",
            "terminal_connected": terminal is not None,
            "account_connected": info is not None,
            # info.server e' gia' il nome server MT5: mascherato comunque, coerente con la
            # sanitizzazione applicata ovunque in questo repository ai nomi server (vedi
            # worker/event_sender.py:mask_value).
            "server": _mask(info.server) if info is not None else _mask(
                getattr(self._config, "mt5_server", "")
                or getattr(self._config, "mt5_expected_server", "")
            ),
            "version": str(self._mt5.version()) if hasattr(self._mt5, "version") else "unknown",
        }

    def get_candles(self, broker_symbol: str, timeframe: str, since: Optional[datetime], now: datetime, limit: int) -> list:
        with self._operation_lock:
            return self._get_candles_unlocked(broker_symbol, timeframe, since, now, limit)

    def _get_candles_unlocked(
        self,
        broker_symbol: str,
        timeframe: str,
        since: Optional[datetime],
        now: datetime,
        limit: int,
    ) -> list:
        """Legge candele storiche reali. Usa copy_rates_from_pos quando 'since' e' assente (le
        `limit` candele piu' recenti disponibili -- NON l'intero storico dall'inizio: a
        differenza del mock/fake bridge, MT5 non ha un'epoca sintetica fissa nota a priori, vedi
        README), copy_rates_range quando 'since' e' presente. In entrambi i casi filtra
        esplicitamente per escludere la candela ancora in formazione e per rispettare 'since'
        come limite esclusivo, invece di fidarsi ciecamente di cio' che restituisce il
        pacchetto MetaTrader5."""
        if not self._connected or self._mt5 is None:
            raise BridgeError(503, "mt5_not_connected", "mt5-bridge non e' connesso al terminale MT5.")

        mt5 = self._mt5
        tf_constant = getattr(mt5, _TIMEFRAME_MT5_CONSTANT_NAMES[timeframe])
        step_seconds = TIMEFRAME_SECONDS[timeframe]

        try:
            symbol_info = mt5.symbol_info(broker_symbol)
        except Exception as exc:  # noqa: BLE001 - errore dell'estensione MetaTrader5
            raise BridgeError(
                502,
                "mt5_error",
                f"Verifica del simbolo MT5 richiesto {broker_symbol!r} fallita: "
                f"{self._sanitize_error(exc)}",
            ) from None
        if symbol_info is None:
            detail = self._sanitize_error(
                RuntimeError(f"symbol_info() ha restituito None: {mt5.last_error()}")
            )
            raise BridgeError(
                422,
                "symbol_not_found",
                f"Il simbolo MT5 richiesto {broker_symbol!r} non esiste o non e' disponibile "
                f"nel terminale. Dettaglio MT5: {detail}",
            )

        if not bool(getattr(symbol_info, "visible", False)):
            try:
                selected = mt5.symbol_select(broker_symbol, True)
            except Exception as exc:  # noqa: BLE001 - errore dell'estensione MetaTrader5
                raise BridgeError(
                    502,
                    "symbol_not_selectable",
                    f"Il simbolo MT5 richiesto {broker_symbol!r} non e' selezionabile: "
                    f"{self._sanitize_error(exc)}",
                ) from None
            if not selected:
                detail = self._sanitize_error(
                    RuntimeError(f"symbol_select() fallito: {mt5.last_error()}")
                )
                raise BridgeError(
                    502,
                    "symbol_not_selectable",
                    f"Il simbolo MT5 richiesto {broker_symbol!r} non e' selezionabile. "
                    f"Dettaglio MT5: {detail}",
                )

        def _fetch():
            if since is None:
                rates = mt5.copy_rates_from_pos(broker_symbol, tf_constant, 0, limit)
            else:
                rates = mt5.copy_rates_range(broker_symbol, tf_constant, since, now)
            if rates is None:
                raise RuntimeError(f"lettura candele fallita: {mt5.last_error()}")
            return rates

        rates = self._call_with_retry(f"lettura candele {broker_symbol}/{timeframe}", _fetch)

        last_complete_epoch = int(now.timestamp()) - step_seconds
        candles = []
        for rate in rates:
            open_epoch = int(rate["time"])
            if open_epoch > last_complete_epoch:
                continue  # candela ancora in formazione: mai restituita (vedi docstring)
            open_time = datetime.fromtimestamp(open_epoch, tz=timezone.utc)
            if since is not None and open_time <= since:
                continue  # since esclusivo: non ci fidiamo che copy_rates_range lo garantisca gia'
            candles.append({
                "open_time": format_iso_utc(open_time),
                "open": str(Decimal(str(round(float(rate["open"]), 5)))),
                "high": str(Decimal(str(round(float(rate["high"]), 5)))),
                "low": str(Decimal(str(round(float(rate["low"]), 5)))),
                "close": str(Decimal(str(round(float(rate["close"]), 5)))),
                "tick_volume": int(rate["tick_volume"]),
                "spread": int(rate["spread"]),
                "source": "mt5",
            })

        candles.sort(key=lambda c: c["open_time"])
        return candles[:limit]

    def get_trading_snapshot(self, deal_lookback_hours: int) -> dict:
        with self._operation_lock:
            return self._get_trading_snapshot_unlocked(deal_lookback_hours)

    def _get_trading_snapshot_unlocked(self, deal_lookback_hours: int) -> dict:
        """Legge account, posizioni, ordini e deal di uscita usando esclusivamente API MT5
        read-only. Ogni ``None`` del package viene trasformato in un BridgeError strutturato dopo
        i retry limitati di ``_call_with_retry``.
        """
        if not self._connected or self._mt5 is None:
            raise BridgeError(503, "mt5_not_connected", "mt5-bridge non e' connesso al terminale MT5.")

        mt5 = self._mt5

        def _fetch_account() -> dict:
            info = mt5.account_info()
            if info is None:
                raise RuntimeError(f"account_info() fallito: {mt5.last_error()}")
            return {
                "login": str(info.login),
                "server": str(info.server),
                "balance": float(info.balance),
                "equity": float(info.equity),
                "currency": str(info.currency),
                "leverage": int(info.leverage),
            }

        def _fetch_positions() -> list:
            positions = mt5.positions_get()
            if positions is None:
                raise RuntimeError(f"positions_get() fallito: {mt5.last_error()}")
            return [
                {
                    "ticket": str(position.ticket),
                    "symbol": str(position.symbol),
                    "direction": "buy" if int(position.type) == 0 else "sell",
                    "volume": float(position.volume),
                    "open_price": float(position.price_open),
                    "stop_loss": float(position.sl),
                    "take_profit": float(position.tp),
                    "open_time": _epoch_to_utc_z(position.time),
                }
                for position in positions
            ]

        def _fetch_orders() -> list:
            orders = mt5.orders_get()
            if orders is None:
                raise RuntimeError(f"orders_get() fallito: {mt5.last_error()}")
            return [
                {
                    "ticket": str(order.ticket),
                    "symbol": str(order.symbol),
                    "direction": _order_direction(order.type),
                    "volume": float(order.volume_current),
                    "price": float(order.price_open),
                    "stop_loss": float(order.sl),
                    "take_profit": float(order.tp),
                    "order_type": int(order.type),
                }
                for order in orders
            ]

        account = self._call_with_retry("lettura account_info snapshot", _fetch_account)
        positions = self._call_with_retry("lettura posizioni snapshot", _fetch_positions)
        orders = self._call_with_retry("lettura ordini snapshot", _fetch_orders)

        date_to = datetime.now(timezone.utc)
        date_from = date_to - timedelta(hours=deal_lookback_hours)

        def _fetch_deals() -> list:
            deals = mt5.history_deals_get(date_from, date_to)
            if deals is None:
                raise RuntimeError(f"history_deals_get() fallito: {mt5.last_error()}")
            exit_entries = {
                int(getattr(mt5, "DEAL_ENTRY_OUT", 1)),
                int(getattr(mt5, "DEAL_ENTRY_OUT_BY", 3)),
            }
            return [
                {
                    "deal_ticket": str(deal.ticket),
                    "position_ticket": str(deal.position_id),
                    "close_price": float(deal.price),
                    "profit": float(deal.profit),
                    "commission": float(deal.commission),
                    "swap": float(deal.swap),
                    "close_time": _epoch_to_utc_z(deal.time),
                }
                for deal in deals
                if int(getattr(deal, "entry", -1)) in exit_entries
            ]

        deals = self._call_with_retry("lettura storico deal snapshot", _fetch_deals)
        return {
            "account": account,
            "positions": positions,
            "orders": orders,
            "deals": deals,
            "generated_at": format_iso_utc(datetime.now(timezone.utc)),
        }

    def shutdown(self) -> None:
        with self._operation_lock:
            # Rendere lo shutdown idempotente evita una seconda chiamata all'estensione MT5 se
            # arrivano piu' segnali o se il cleanup viene richiesto nuovamente dopo il finally.
            mt5 = self._mt5
            self._mt5 = None
            self._connected = False
            if mt5 is not None:
                try:
                    mt5.shutdown()
                except Exception as exc:  # noqa: BLE001 - shutdown deve essere best-effort
                    sys.stderr.write(
                        "[mt5-bridge] shutdown() ha sollevato un errore (ignorato): "
                        f"{self._sanitize_error(exc)}\n"
                    )


class Handler(BaseBridgeHandler):
    session: _Mt5Session  # impostata dinamicamente da make_server()

    def do_GET(self) -> None:
        if self.path != "/health":
            self.send_bridge_error(BridgeError(404, "not_found", f"Path non trovato: {self.path}"))
            return
        if not self.check_auth():
            return
        self.send_json(200, self.session.health())

    def do_POST(self) -> None:
        if self.path not in ("/v1/candles", "/v1/trading/snapshot"):
            self.send_bridge_error(BridgeError(404, "not_found", f"Path non trovato: {self.path}"))
            return
        if not self.check_auth():
            return
        try:
            request = self.read_json_body()
            if self.path == "/v1/trading/snapshot":
                deal_lookback_hours = self.parse_trading_snapshot_request(request)
                snapshot = self.session.get_trading_snapshot(deal_lookback_hours)
            else:
                symbol, timeframe, since, now, limit = self.parse_candles_request(request)
                candles = self.session.get_candles(symbol, timeframe, since, now, limit)
        except BridgeError as exc:
            self.send_bridge_error(exc)
            return
        if self.path == "/v1/trading/snapshot":
            self.send_json(200, snapshot)
        else:
            self.send_json(200, {"symbol": symbol, "timeframe": timeframe, "candles": candles})


def make_server(config: Mt5BridgeConfig, session: _Mt5Session) -> ThreadingHTTPServer:
    handler_cls = type("_Mt5BridgeHandler", (Handler,), {"config": config, "session": session})
    return ThreadingHTTPServer((config.host, config.port), handler_cls)


def _serve_server(server: ThreadingHTTPServer, session: _Mt5Session) -> None:
    """Esegue il server e ne coordina uno shutdown idempotente e privo di deadlock.

    ``HTTPServer.shutdown()`` deve essere invocato da un thread diverso da quello che sta
    eseguendo ``serve_forever()``. I signal handler Python vengono eseguiti nel thread principale,
    che in questo processo e' anche quello del loop HTTP: il primo segnale avvia quindi un thread
    daemon dedicato. Il cleanup di socket e sessione MT5 resta nel ``finally`` del loop ed e'
    eseguito una sola volta anche quando arrivano segnali ripetuti.
    """

    shutdown_requested = threading.Event()

    def _request_shutdown(_signum, _frame) -> None:
        if shutdown_requested.is_set():
            return
        shutdown_requested.set()
        sys.stderr.write("[mt5-bridge] Segnale di arresto ricevuto, chiusura in corso...\n")
        threading.Thread(
            target=server.shutdown,
            name="mt5-bridge-http-shutdown",
            daemon=True,
        ).start()

    try:
        signal.signal(signal.SIGTERM, _request_shutdown)
        signal.signal(signal.SIGINT, _request_shutdown)
        sys.stderr.write(
            f"[mt5-bridge] in ascolto su {server.server_address[0]}:"
            f"{server.server_address[1]}\n"
        )
        server.serve_forever()
    finally:
        # Blocca l'avvio di nuovi thread di shutdown mentre il cleanup e' gia' in corso.
        shutdown_requested.set()
        try:
            server.server_close()
        finally:
            session.shutdown()


def main() -> None:
    config = Mt5BridgeConfig()
    session = _Mt5Session(config)
    session.connect()

    try:
        server = make_server(config, session)
    except Exception:
        session.shutdown()
        raise

    _serve_server(server, session)


if __name__ == "__main__":
    main()
