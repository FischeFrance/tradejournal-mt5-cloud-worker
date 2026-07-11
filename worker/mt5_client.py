"""Interfaccia del client MT5 e implementazione reale (predisposta per il futuro, non
funzionante su questo POC).

`Mt5Client` e' l'interfaccia astratta che sia `mock_mt5_client.MockMt5Client` sia
`RealMt5Client` implementano, cosi' che `main.py` possa scegliere l'una o l'altra in base a
MOCK_MODE senza cambiare nessun'altra riga di codice.

RealMt5Client si appoggia al pacchetto Python ufficiale `MetaTrader5`, che a sua volta funziona
SOLO se il terminale MetaTrader 5 (un eseguibile Windows) e' in esecuzione e raggiungibile --
su Linux questo richiede Wine (+ un display virtuale come Xvfb). Non e' testabile su questo
ambiente (macOS, nessun MT5 installato) ed e' percio' volutamente uno stub che fallisce con un
errore chiaro, in attesa del test reale su Ubuntu con Wine (vedi docker/Dockerfile, stage
`real-mt5`, e il README).
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Optional, TypeVar

from event_sender import mask_value

logger = logging.getLogger("mt5_worker.mt5_client")

T = TypeVar("T")


class Mt5ConnectionError(RuntimeError):
    """Sollevato quando non e' possibile connettersi/comunicare con il terminale MT5."""


class Mt5Client(ABC):
    """Interfaccia che ogni sorgente dati MT5 (reale o mock) deve implementare."""

    @abstractmethod
    def connect(self) -> None:
        """Stabilisce la connessione iniziale al terminale MT5."""

    @abstractmethod
    def reconnect(self) -> None:
        """Ritenta la connessione dopo una disconnessione (es. terminale riavviato)."""

    @abstractmethod
    def health_status(self) -> Dict[str, Any]:
        """Restituisce {'connected': bool, 'detail': str} senza mai includere credenziali."""

    @abstractmethod
    def account_info(self) -> Dict[str, Any]:
        """Restituisce {'login', 'server', 'balance', 'equity', 'currency', 'leverage'}."""

    @abstractmethod
    def get_open_positions(self) -> Dict[str, Dict[str, Any]]:
        """Restituisce le posizioni aperte, chiave = ticket (stringa)."""

    @abstractmethod
    def get_recent_deals(self) -> Dict[str, Dict[str, Any]]:
        """Restituisce i deal recenti (storico chiusure), chiave = deal ticket (stringa)."""

    @abstractmethod
    def get_pending_orders(self) -> Dict[str, Dict[str, Any]]:
        """Restituisce gli ordini pendenti, chiave = ticket (stringa)."""

    def snapshot(self) -> Dict[str, Any]:
        """Helper condiviso: assembla lo snapshot completo atteso da event_detector."""
        return {
            "positions": self.get_open_positions(),
            "orders": self.get_pending_orders(),
            "deals": self.get_recent_deals(),
        }

    def tick(self) -> None:
        """Hook opzionale chiamato una volta per ciclo di poll da main.py.

        No-op per un client reale (lo stato arriva dal terminale MT5 stesso); MockMt5Client lo
        sovrascrive per avanzare la propria macchina a stati.
        """
        return None


class RealMt5Client(Mt5Client):
    """Implementazione reale basata sul pacchetto `MetaTrader5`.

    NON funzionante fuori da un ambiente Windows/Wine con il terminale MT5 in esecuzione.
    Vedi docker/Dockerfile (stage `real-mt5`) e README per come predisporre il test reale su
    Ubuntu. Qui ci limitiamo a importare il pacchetto in modo lazy e a fallire con un messaggio
    d'errore chiaro, senza mai stampare login/password.

    Sicurezza: usare esclusivamente la password INVESTOR (sola lettura) dell'account MT5, mai
    la password di trading. Questo client non ha bisogno di alcun privilegio di scrittura --
    legge solo posizioni/ordini/deal/account_info -- ma non puo' impedire a livello di codice
    quale delle due password venga fornita: e' una scelta da fare in fase di configurazione
    dell'account (vedi README).
    """

    #: Quante ore di storico deal interrogare a ogni poll per rilevare le chiusure. Una finestra
    #: scorrevole e' sufficiente (non serve un cursore persistente): una volta che una posizione
    #: e' sparita dallo snapshot precedente, event_detector la considera chiusa una sola volta,
    #: indipendentemente da quante volte il deal corrispondente ricompare nella finestra nei poll
    #: successivi.
    DEAL_LOOKBACK_HOURS = 24

    #: DEAL_ENTRY_OUT nell'enum MetaTrader5: 1 = deal di uscita (chiusura), 0 = di ingresso.
    _DEAL_ENTRY_OUT = 1

    def __init__(
        self,
        login: Optional[str],
        password: Optional[str],
        server: Optional[str],
        max_retries: int = 3,
        retry_delay_seconds: float = 1.0,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self._login = login
        self._password = password
        self._server = server
        self._max_retries = max_retries
        self._retry_delay_seconds = retry_delay_seconds
        self._sleep = sleep_fn
        self._mt5 = None
        self._connected = False

    def _import_mt5(self):
        try:
            import MetaTrader5 as mt5  # type: ignore[import-not-found]
        except ImportError as exc:
            raise Mt5ConnectionError(
                "Pacchetto 'MetaTrader5' non disponibile o terminale MT5 non raggiungibile. "
                "Questo client funziona solo su Windows o Linux+Wine con il terminale MT5 "
                "attivo (vedi README, sezione 'Fase 2: MT5 reale + Wine')."
            ) from exc
        return mt5

    def _call_with_retry(self, description: str, fn: Callable[[], T]) -> T:
        """Ritenta un numero limitato di volte le sole operazioni potenzialmente transitorie
        (IPC col terminale non ancora pronto, disconnessione momentanea). Non fa mai trapelare
        credenziali: `fn` deve gia' occuparsi di non includerle nei propri messaggi d'errore."""
        last_error: Optional[BaseException] = None
        for attempt in range(1, self._max_retries + 1):
            try:
                return fn()
            except Exception as exc:  # noqa: BLE001 - qualunque errore del pacchetto MetaTrader5
                last_error = exc
                logger.warning(
                    "Tentativo %s/%s fallito per '%s': %s", attempt, self._max_retries, description, exc
                )
                if attempt < self._max_retries:
                    self._sleep(self._retry_delay_seconds * attempt)
        raise Mt5ConnectionError(f"'{description}' fallita dopo {self._max_retries} tentativi: {last_error}")

    def connect(self) -> None:
        if not self._login or not self._password or not self._server:
            raise Mt5ConnectionError("MT5_LOGIN / MT5_PASSWORD / MT5_SERVER non configurati.")
        try:
            login_id = int(self._login)
        except (TypeError, ValueError) as exc:
            raise Mt5ConnectionError("MT5_LOGIN deve essere un identificativo numerico.") from exc

        def _do_connect() -> None:
            mt5 = self._import_mt5()
            if not mt5.initialize():
                raise RuntimeError(f"initialize() fallito: {mt5.last_error()}")
            authorized = mt5.login(login_id, password=self._password, server=self._server)
            if not authorized:
                raise RuntimeError("login() fallito (credenziali non stampate per sicurezza)")
            self._mt5 = mt5

        self._call_with_retry("connessione al terminale MT5", _do_connect)
        self._connected = True
        logger.info(
            "Connesso al terminale MT5 (server=%s, login=%s).",
            mask_value(self._server),
            mask_value(self._login),
        )

    def reconnect(self) -> None:
        logger.info("Tentativo di riconnessione al terminale MT5...")
        self._connected = False
        self.connect()

    def health_status(self) -> Dict[str, Any]:
        return {"connected": self._connected, "detail": "ok" if self._connected else "non connesso"}

    def account_info(self) -> Dict[str, Any]:
        self._ensure_connected()

        def _fetch() -> Dict[str, Any]:
            info = self._mt5.account_info()
            if info is None:
                raise RuntimeError("account_info() ha restituito None.")
            return {
                "login": str(info.login),
                "server": info.server,
                "balance": info.balance,
                "equity": info.equity,
                "currency": info.currency,
                "leverage": info.leverage,
            }

        return self._call_with_retry("lettura account_info", _fetch)

    def get_open_positions(self) -> Dict[str, Dict[str, Any]]:
        self._ensure_connected()

        def _fetch() -> Dict[str, Dict[str, Any]]:
            positions = self._mt5.positions_get()
            if positions is None:
                raise RuntimeError(f"positions_get() fallito: {self._mt5.last_error()}")
            return {
                str(p.ticket): {
                    "ticket": str(p.ticket),
                    "symbol": p.symbol,
                    "direction": "buy" if p.type == 0 else "sell",
                    "volume": p.volume,
                    "open_price": p.price_open,
                    "stop_loss": p.sl,
                    "take_profit": p.tp,
                    "open_time": _epoch_to_iso(p.time),
                }
                for p in positions
            }

        return self._call_with_retry("lettura posizioni aperte", _fetch)

    def get_recent_deals(self) -> Dict[str, Dict[str, Any]]:
        self._ensure_connected()

        def _fetch() -> Dict[str, Dict[str, Any]]:
            date_to = datetime.now(timezone.utc)
            date_from = date_to - timedelta(hours=self.DEAL_LOOKBACK_HOURS)
            deals = self._mt5.history_deals_get(date_from, date_to)
            if deals is None:
                raise RuntimeError(f"history_deals_get() fallito: {self._mt5.last_error()}")
            result: Dict[str, Dict[str, Any]] = {}
            for d in deals:
                if getattr(d, "entry", self._DEAL_ENTRY_OUT) != self._DEAL_ENTRY_OUT:
                    continue  # solo i deal di uscita rappresentano una chiusura
                result[str(d.ticket)] = {
                    "position_ticket": str(d.position_id),
                    "close_price": d.price,
                    "profit": d.profit,
                    "commission": d.commission,
                    "swap": d.swap,
                    "close_time": _epoch_to_iso(d.time),
                }
            return result

        return self._call_with_retry("lettura storico deal", _fetch)

    def get_pending_orders(self) -> Dict[str, Dict[str, Any]]:
        self._ensure_connected()

        def _fetch() -> Dict[str, Dict[str, Any]]:
            orders = self._mt5.orders_get()
            if orders is None:
                raise RuntimeError(f"orders_get() fallito: {self._mt5.last_error()}")
            return {
                str(o.ticket): {
                    "ticket": str(o.ticket),
                    "symbol": o.symbol,
                    "direction": "buy" if o.type % 2 == 0 else "sell",
                    "volume": o.volume_current,
                    "price": o.price_open,
                    "stop_loss": o.sl,
                    "take_profit": o.tp,
                    "order_type": o.type,
                }
                for o in orders
            }

        return self._call_with_retry("lettura ordini pendenti", _fetch)

    def _ensure_connected(self) -> None:
        if not self._connected or self._mt5 is None:
            raise Mt5ConnectionError("Client MT5 non connesso: chiamare connect() prima.")


def _epoch_to_iso(epoch_seconds: Any) -> Optional[str]:
    if not epoch_seconds:
        return None
    return datetime.fromtimestamp(int(epoch_seconds), tz=timezone.utc).isoformat()
