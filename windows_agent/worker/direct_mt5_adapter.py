from __future__ import annotations

import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator


class Mt5Error(RuntimeError):
    pass


class IdentityMismatch(Mt5Error):
    pass


class Mt5IpcError(Mt5Error):
    pass


class Mt5ProcessCrashed(Mt5Error):
    pass


class Mt5VersionMismatch(Mt5IpcError):
    pass


class DirectMt5Adapter:
    """A deliberately narrow facade exposing read operations only."""

    def __init__(
        self,
        terminal: Path,
        expected_login: int,
        expected_server: str,
        module: Any = None,
        retries: int = 2,
        retry_delay: float = 0.5,
    ) -> None:
        self.terminal = Path(terminal).resolve()
        self.expected_login = int(expected_login)
        self.expected_server = expected_server
        self.retries = max(1, min(retries, 3))
        self.retry_delay = retry_delay
        if module is None:
            import MetaTrader5 as imported_module  # type: ignore[import-untyped]

            module = imported_module
        self._mt5 = module

    def last_error(self) -> tuple[int, str]:
        error = self._mt5.last_error()
        return int(error[0]), str(error[1])[:160].replace("\r", " ").replace("\n", " ")

    def initialize(self) -> None:
        """Let the Python package launch/attach MT5; never pre-start a second terminal."""
        self.verify_ipc_compatibility()
        last_code, last_message = 0, "unknown"
        for attempt in range(self.retries):
            if self._mt5.initialize(str(self.terminal), timeout=60_000, portable=True):
                return
            last_code, last_message = self.last_error()
            self._mt5.shutdown()
            if attempt + 1 < self.retries:
                time.sleep(self.retry_delay * (attempt + 1))
        if last_code in (-10003, -10005):
            raise Mt5IpcError(f"MT5 IPC failed ({last_code}): {last_message}")
        raise Mt5Error(f"MT5 initialization failed ({last_code}): {last_message}")

    def verify_ipc_compatibility(self) -> None:
        """Fail fast for the confirmed terminal 5833+ / wheel 5735 IPC break."""
        try:
            import win32api

            info = win32api.GetFileVersionInfo(str(self.terminal), "\\")
            terminal_build = int(info["FileVersionLS"] & 0xFFFF)
            package_build = int(str(self._mt5.__version__).rsplit(".", 1)[-1])
        except Exception:
            return
        if terminal_build >= 5833 and package_build <= 5735:
            raise Mt5VersionMismatch(
                f"MT5 IPC version mismatch: terminal build {terminal_build}, "
                f"Python package build {package_build}"
            )

    @contextmanager
    def session(self, investor_password: str) -> Iterator["DirectMt5Adapter"]:
        if (
            not self.terminal.is_file()
            or self.terminal.name.lower() != "terminal64.exe"
        ):
            raise Mt5Error("validated terminal64.exe path required")
        initialized = False
        try:
            self.initialize()
            initialized = True
            if not self._mt5.login(
                self.expected_login,
                password=investor_password,
                server=self.expected_server,
                timeout=20_000,
            ):
                raise Mt5Error("MT5 authorization failed")
            self.verify_identity()
            yield self
        finally:
            if initialized:
                self._mt5.shutdown()

    def verify_identity(self) -> dict[str, Any]:
        account = self._mt5.account_info()
        if account is None:
            raise IdentityMismatch("account unavailable")
        login = int(getattr(account, "login", -1))
        server = str(getattr(account, "server", ""))
        if (
            login != self.expected_login
            or server.casefold() != self.expected_server.casefold()
        ):
            raise IdentityMismatch(
                "connected account identity does not match expected identity"
            )
        return {"login": str(login), "server": server}

    @staticmethod
    def _dict(item: Any) -> dict[str, Any]:
        return item._asdict() if hasattr(item, "_asdict") else dict(item)

    def terminal_info(self) -> Any:
        return self._mt5.terminal_info()

    def account_info(self) -> Any:
        return self._mt5.account_info()

    def positions(self) -> tuple[Any, ...]:
        return tuple(self._mt5.positions_get() or ())

    def orders(self) -> tuple[Any, ...]:
        return tuple(self._mt5.orders_get() or ())

    def history_orders(self, start: datetime, end: datetime) -> tuple[Any, ...]:
        return tuple(self._mt5.history_orders_get(start, end) or ())

    def history_deals(self, start: datetime, end: datetime) -> tuple[Any, ...]:
        return tuple(self._mt5.history_deals_get(start, end) or ())

    def rates(
        self, symbol: str, timeframe: int, start: datetime, end: datetime
    ) -> tuple[Any, ...]:
        return tuple(self._mt5.copy_rates_range(symbol, timeframe, start, end) or ())

    def ticks(self, symbol: str, start: datetime, end: datetime) -> tuple[Any, ...]:
        return tuple(
            self._mt5.copy_ticks_range(symbol, start, end, self._mt5.COPY_TICKS_ALL)
            or ()
        )

    def symbol_info(self, symbol: str) -> Any:
        return self._mt5.symbol_info(symbol)

    def symbol_tick(self, symbol: str) -> Any:
        return self._mt5.symbol_info_tick(symbol)

    def snapshot(self, lookback_hours: int = 72) -> dict[str, Any]:
        self.verify_identity()
        now = datetime.now(timezone.utc)
        positions = {
            str(getattr(x, "ticket")): self._position(x) for x in self.positions()
        }
        orders = {str(getattr(x, "ticket")): self._order(x) for x in self.orders()}
        deals = {
            str(getattr(x, "ticket")): self._deal(x)
            for x in self.history_deals(now - timedelta(hours=lookback_hours), now)
        }
        return {"positions": positions, "orders": orders, "deals": deals}

    def _position(self, item: Any) -> dict[str, Any]:
        d = self._dict(item)
        return {
            "ticket": str(d["ticket"]),
            "symbol": d.get("symbol"),
            "direction": "buy" if d.get("type") == 0 else "sell",
            "volume": d.get("volume"),
            "open_price": d.get("price_open"),
            "stop_loss": d.get("sl"),
            "take_profit": d.get("tp"),
            "open_time": datetime.fromtimestamp(
                d.get("time", 0), timezone.utc
            ).isoformat(),
        }

    def _order(self, item: Any) -> dict[str, Any]:
        d = self._dict(item)
        return {
            "ticket": str(d["ticket"]),
            "symbol": d.get("symbol"),
            "direction": "buy" if int(d.get("type", 0)) in (0, 2, 4, 6) else "sell",
            "volume": d.get("volume_current"),
            "price": d.get("price_open"),
            "stop_loss": d.get("sl"),
            "take_profit": d.get("tp"),
            "order_type": d.get("type"),
        }

    def _deal(self, item: Any) -> dict[str, Any]:
        d = self._dict(item)
        return {
            "deal_ticket": str(d["ticket"]),
            "position_ticket": str(d.get("position_id", "")),
            "symbol": d.get("symbol"),
            "volume": d.get("volume"),
            "close_price": d.get("price"),
            "profit": d.get("profit"),
            "commission": d.get("commission"),
            "swap": d.get("swap"),
            "close_time": datetime.fromtimestamp(
                d.get("time", 0), timezone.utc
            ).isoformat(),
        }
