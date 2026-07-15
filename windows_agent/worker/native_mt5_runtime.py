from __future__ import annotations

import gc
import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..provisioning.secret_store import WindowsSecretStore


class NativeMt5Error(RuntimeError):
    """Sanitized native-terminal failure; never contains credentials."""


@dataclass(frozen=True)
class NativeMt5Status:
    pid: int
    account: dict[str, Any]
    heartbeat: dict[str, Any]
    files_path: Path


class NativeMt5Runtime:
    """Launch an isolated MT5 terminal with the read-only MQL5 file bridge.

    This route deliberately does not import the MetaTrader5 Python wheel.  It is
    compatible with terminal builds whose Python IPC is temporarily broken.
    """

    def __init__(self, instance_root: Path, connection_id: str) -> None:
        self.root = instance_root.resolve()
        self.connection_id = connection_id
        self.terminal_root = self.root / "terminal"
        self.terminal = self.terminal_root / "terminal64.exe"
        self.files = self.terminal_root / "MQL5" / "Files" / "TradeJournal"
        self.state = self.root / "state"
        self._process: subprocess.Popen[bytes] | None = None

    def install_expert(self, expert_binary: Path) -> Path:
        if not expert_binary.is_file() or expert_binary.suffix.casefold() != ".ex5":
            raise NativeMt5Error("expert_binary_missing")
        destination = (
            self.terminal_root
            / "MQL5"
            / "Experts"
            / "TradeJournal"
            / "TradeJournalBridge.ex5"
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(expert_binary, destination)
        self.files.mkdir(parents=True, exist_ok=True)
        (self.files / "connection_id").write_text(
            self.connection_id, encoding="utf-8"
        )
        return destination

    def _write_startup_config(
        self, login: int, server: str, password: str, symbol: str
    ) -> Path:
        if any(c in server + password + symbol for c in "\r\n"):
            raise NativeMt5Error("invalid_startup_value")
        path = self.state / "startup.ini"
        self.state.mkdir(parents=True, exist_ok=True)
        content = (
            "[Common]\n"
            f"Login={login}\nServer={server}\nPassword={password}\nKeepPrivate=0\n\n"
            "[Experts]\nEnabled=1\nAllowLiveTrading=0\nAllowDllImport=0\n"
            "Account=0\nProfile=0\n\n"
            "[StartUp]\nExpert=TradeJournal\\TradeJournalBridge\n"
            f"Symbol={symbol}\nPeriod=M1\n"
        )
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\r\n") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            content = ""
        WindowsSecretStore.restrict_acl(path)
        return path

    def start(
        self,
        *,
        login: int,
        server: str,
        investor_password: str,
        expert_binary: Path,
        symbol: str = "EURUSD",
        timeout: float = 90.0,
    ) -> NativeMt5Status:
        if not self.terminal.is_file():
            raise NativeMt5Error("terminal_start_failed")
        self.install_expert(expert_binary)
        config = self._write_startup_config(login, server, investor_password, symbol)
        investor_password = ""
        gc.collect()
        try:
            self._process = subprocess.Popen(
                [str(self.terminal), "/portable", f"/config:{config}"],
                cwd=self.terminal_root,
                close_fds=True,
            )
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if self._process.poll() is not None:
                    raise NativeMt5Error("mt5_process_crashed")
                account = self._read_json(self.files / "account.json")
                heartbeat = self._read_json(self.files / "heartbeat.json")
                if account is not None and heartbeat is not None:
                    if str(account.get("login")) != str(login):
                        raise NativeMt5Error("identity_mismatch")
                    if str(account.get("server", "")).casefold() != server.casefold():
                        raise NativeMt5Error("server_identity_mismatch")
                    if not heartbeat.get("terminal_connected", False):
                        time.sleep(1)
                        continue
                    if bool(account.get("trade_allowed", True)):
                        raise NativeMt5Error("investor_readonly_not_verified")
                    return NativeMt5Status(
                        self._process.pid, account, heartbeat, self.files
                    )
                time.sleep(1)
            raise NativeMt5Error("terminal_not_ready")
        finally:
            try:
                config.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any] | None:
        try:
            value = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def stop(self, timeout: float = 15.0) -> bool:
        process = self._process
        if process is None or process.poll() is not None:
            return True
        process.terminate()
        try:
            process.wait(timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(5)
        return process.poll() is not None
