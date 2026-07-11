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
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

logger = logging.getLogger("mt5_worker.mt5_client")


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
    """

    def __init__(self, login: Optional[str], password: Optional[str], server: Optional[str]) -> None:
        self._login = login
        self._password = password
        self._server = server
        self._mt5 = None
        self._connected = False

    def _import_mt5(self):
        try:
            import MetaTrader5 as mt5  # type: ignore[import-not-found]
        except ImportError as exc:
            raise Mt5ConnectionError(
                "Pacchetto 'MetaTrader5' non disponibile o terminale MT5 non raggiungibile. "
                "Questo client funziona solo su Windows o Linux+Wine con il terminale MT5 "
                "attivo (vedi README, sezione 'Test reale MT5 + Wine su Ubuntu')."
            ) from exc
        return mt5

    def connect(self) -> None:
        if not self._login or not self._password or not self._server:
            raise Mt5ConnectionError("MT5_LOGIN / MT5_PASSWORD / MT5_SERVER non configurati.")
        mt5 = self._import_mt5()
        if not mt5.initialize():
            raise Mt5ConnectionError("Inizializzazione del terminale MT5 fallita.")
        authorized = mt5.login(int(self._login), password=self._password, server=self._server)
        if not authorized:
            raise Mt5ConnectionError("Login MT5 fallito (credenziali non stampate per sicurezza).")
        self._mt5 = mt5
        self._connected = True
        logger.info("Connesso al terminale MT5 (server=%s).", self._server)

    def reconnect(self) -> None:
        logger.info("Tentativo di riconnessione al terminale MT5...")
        self._connected = False
        self.connect()

    def health_status(self) -> Dict[str, Any]:
        return {"connected": self._connected, "detail": "ok" if self._connected else "non connesso"}

    def account_info(self) -> Dict[str, Any]:
        self._ensure_connected()
        info = self._mt5.account_info()
        if info is None:
            raise Mt5ConnectionError("account_info() ha restituito None.")
        return {
            "login": str(info.login),
            "server": info.server,
            "balance": info.balance,
            "equity": info.equity,
            "currency": info.currency,
            "leverage": info.leverage,
        }

    def get_open_positions(self) -> Dict[str, Dict[str, Any]]:
        self._ensure_connected()
        positions = self._mt5.positions_get() or ()
        return {
            str(p.ticket): {
                "ticket": str(p.ticket),
                "symbol": p.symbol,
                "direction": "buy" if p.type == 0 else "sell",
                "volume": p.volume,
                "open_price": p.price_open,
                "stop_loss": p.sl,
                "take_profit": p.tp,
                "open_time": p.time,
            }
            for p in positions
        }

    def get_recent_deals(self) -> Dict[str, Dict[str, Any]]:
        self._ensure_connected()
        return {}

    def get_pending_orders(self) -> Dict[str, Dict[str, Any]]:
        self._ensure_connected()
        orders = self._mt5.orders_get() or ()
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

    def _ensure_connected(self) -> None:
        if not self._connected or self._mt5 is None:
            raise Mt5ConnectionError("Client MT5 non connesso: chiamare connect() prima.")
