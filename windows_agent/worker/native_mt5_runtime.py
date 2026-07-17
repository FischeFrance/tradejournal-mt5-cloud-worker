from __future__ import annotations

import gc
import json
import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..provisioning.secret_store import WindowsSecretStore

logger = logging.getLogger(__name__)


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
        self._interactive_task: str | None = None
        self._last_symbol: str | None = None

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
        connection_tmp = self.files / "connection_id.tmp"
        connection_tmp.write_text(self.connection_id, encoding="utf-8")
        connection_tmp.replace(self.files / "connection_id")
        return destination

    def _write_startup_config(
        self,
        login: int | None,
        server: str | None,
        password: str | None,
        symbol: str,
    ) -> Path:
        values = (server or "") + (password or "") + symbol
        if any(c in values for c in "\r\n"):
            raise NativeMt5Error("invalid_startup_value")
        path = self.state / "startup.ini"
        self.state.mkdir(parents=True, exist_ok=True)
        # The portable terminal must retain its per-instance data directory while
        # it bootstraps the StartUp EA.  With this launcher, KeepPrivate=1 accepts
        # the login but prevents the EA's OnInit from running.  Credentials remain
        # in the ACL-restricted config only for the short bootstrap window.
        common = "[Common]\nKeepPrivate=0\n"
        if login is not None and server and password:
            common += f"Login={login}\nServer={server}\nPassword={password}\n"
        content = (
            common + "\n"
            "[Charts]\nProfileLast=Default\nPreloadCharts=1\n\n"
            "[Experts]\nEnabled=1\nAllowLiveTrading=0\nAllowDllImport=0\n"
            "Account=0\nProfile=0\nChart=0\n\n"
            # MetaTrader resolves StartUp.Expert from its own MQL5/Experts directory.
            # Passing an absolute EX5 path leaves the chart open but does not reliably attach
            # the EA in portable installations.
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

    @staticmethod
    def _setting(name: str) -> str:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
            ) as key:
                return str(winreg.QueryValueEx(key, name)[0]).strip()
        except (ImportError, OSError):
            return ""

    @staticmethod
    def _startup_symbol(default: str) -> str:
        # Incident (2026-07-17): an operator set this machine-wide to a broker-specific variant
        # ("EURUSD.raw") while debugging a different connection. That symbol didn't exist in the
        # terminal's Market Watch, so the [StartUp] chart never opened, the EA never attached, and
        # EVERY provision -- including the credential-free no-login smoke test, on a terminal that
        # never even logs into a broker -- failed with terminal_not_ready. There is no reliable way
        # to validate a symbol before the terminal opens (that's the very step this unblocks), so
        # if this override is ever set again, prefer a plain, unsuffixed major-pair name and prove
        # it against `run-no-login-file-bridge-smoke.ps1` before touching any real connection.
        symbol = NativeMt5Runtime._setting("TRADEJOURNAL_MT5_STARTUP_SYMBOL") or default
        if not symbol or any(c in symbol for c in "\r\n"):
            raise NativeMt5Error("invalid_startup_symbol")
        return symbol

    @staticmethod
    def _payload(record: dict[str, Any], name: str) -> dict[str, Any] | None:
        if record.get("schema_version") != 1 or not isinstance(record.get("payload"), dict):
            return None
        return record["payload"]

    def _start_process(self, config: Path) -> subprocess.Popen[bytes] | None:
        interactive_user = self._setting("TRADEJOURNAL_MT5_INTERACTIVE_USER")
        if interactive_user:
            if not interactive_user.replace("-", "").replace("_", "").replace(".", "").isalnum():
                raise NativeMt5Error("invalid_interactive_user")
            task = f"TradeJournalMT5-{self.connection_id}"
            launcher = self.state / "launch-terminal.cmd"
            launcher.write_text(
                "@echo off\r\n"
                f'start "" /b "{self.terminal}" /portable /config:"{config}"\r\n',
                encoding="utf-8",
            )
            command = str(launcher)
            create = [
                "schtasks", "/Create", "/TN", task, "/SC", "ONCE", "/ST", "23:59",
                "/RU", interactive_user, "/IT", "/RL", "HIGHEST", "/TR", command, "/F",
            ]
            completed = subprocess.run(create, capture_output=True, text=True, check=False)
            if completed.returncode != 0:
                raise NativeMt5Error("interactive_task_create_failed")
            completed = subprocess.run(["schtasks", "/Run", "/TN", task], capture_output=True, text=True, check=False)
            if completed.returncode != 0:
                raise NativeMt5Error("interactive_task_run_failed")
            self._interactive_task = task
            return None
        self._process = subprocess.Popen(
            [str(self.terminal), "/portable", f"/config:{config}"],
            cwd=self.terminal_root,
            close_fds=True,
        )
        return self._process

    def _wait_for_heartbeat(
        self, timeout: float, login: int | None = None, server: str | None = None
    ) -> NativeMt5Status:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            pid = 0
            if self._process is not None:
                pid = self._process.pid
            elif self._interactive_task:
                pids = self._running_terminal_pids()
                pid = pids[0] if pids else 0
            if self._process is not None and self._process.poll() is not None:
                # MT5 may detach from the short-lived launcher process after reading /config.
                # Accept only the exact executable inside this isolated instance, never an
                # arbitrary terminal64.exe elsewhere on the host.
                pids = self._running_terminal_pids()
                if not pids:
                    raise NativeMt5Error("mt5_process_crashed")
                pid = pids[0]
            account_raw = self._read_json(self.files / "account.json")
            heartbeat_raw = self._read_json(self.files / "heartbeat.json")
            account = self._payload(account_raw, "account") if account_raw else None
            heartbeat = self._payload(heartbeat_raw, "heartbeat") if heartbeat_raw else None
            if heartbeat is None:
                time.sleep(1)
                continue
            if login is None:
                return NativeMt5Status(pid, account or {}, heartbeat, self.files)
            if account is None:
                time.sleep(1)
                continue
            if str(account.get("login")) != str(login):
                raise NativeMt5Error("identity_mismatch")
            if str(account.get("server", "")).casefold() != str(server).casefold():
                raise NativeMt5Error("server_identity_mismatch")
            if not heartbeat.get("terminal_connected", False):
                time.sleep(1)
                continue
            if bool(account.get("trade_allowed", True)):
                raise NativeMt5Error("investor_readonly_not_verified")
            return NativeMt5Status(pid, account, heartbeat, self.files)
        logger.error(
            "native MT5 runtime: heartbeat.json never appeared within %.0fs "
            "(connection_id=%s, symbol=%s) -- check whether that symbol exists in this "
            "terminal's Market Watch (see TRADEJOURNAL_MT5_STARTUP_SYMBOL)",
            timeout,
            self.connection_id,
            self._last_symbol,
        )
        raise NativeMt5Error("terminal_not_ready")

    def _running_terminal_pids(self) -> list[int]:
        try:
            import psutil
        except ImportError:
            return []
        result = []
        for process in psutil.process_iter(("pid", "exe")):
            try:
                if process.info["exe"] and Path(process.info["exe"]).resolve() == self.terminal.resolve():
                    result.append(int(process.info["pid"]))
            except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
                continue
        return result

    def start(
        self,
        *,
        login: int,
        server: str,
        investor_password: str,
        expert_binary: Path,
        symbol: str = "EURUSD",
        # A diagnostic 600s run on 2026-07-17 showed the post-build-6032 full MQL5
        # recompilation (453 files touched, 131 actually compiled) finishing at ~144s --
        # real, but not the whole story: after it finished, the terminal's own journal
        # logged absolutely nothing else (no chart, no EA attach, no login attempt) for
        # the remaining ~7.5 minutes until the 600s deadline. So this is a genuine hang
        # past the compile step, not just "needs more time" -- raising this further would
        # only mask that hang behind an even longer wait. Left at 240s (compile budget +
        # a working margin) rather than reverting to 180s, since the compile step alone
        # can legitimately eat more than that; still expected to fail here until the
        # actual post-compile hang is diagnosed (needs visual/RDP observation of a live
        # attempt -- the journal produces zero signal during that window).
        timeout: float = 240.0,
    ) -> NativeMt5Status:
        if not self.terminal.is_file():
            raise NativeMt5Error("terminal_start_failed")
        symbol = self._startup_symbol(symbol)
        self._last_symbol = symbol
        self.install_expert(expert_binary)
        config = self._write_startup_config(login, server, investor_password, symbol)
        investor_password = ""
        gc.collect()
        try:
            self._start_process(config)
            return self._wait_for_heartbeat(timeout, login, server)
        except Exception:
            # A failed bootstrap has no consumer yet, so its isolated terminal must not be
            # retained. Successful starts deliberately remain alive for history/live sync.
            self.stop()
            raise
        finally:
            try:
                config.unlink(missing_ok=True)
            except OSError:
                pass

    def start_no_login(
        self,
        *,
        expert_binary: Path,
        symbol: str = "EURUSD",
        timeout: float = 90.0,
    ) -> NativeMt5Status:
        """Verify that a generic terminal loads the EA without credentials or MT5 login."""
        if not self.terminal.is_file():
            raise NativeMt5Error("terminal_start_failed")
        symbol = self._startup_symbol(symbol)
        self._last_symbol = symbol
        self.install_expert(expert_binary)
        config = self._write_startup_config(None, None, None, symbol)
        try:
            self._start_process(config)
            return self._wait_for_heartbeat(timeout)
        except Exception:
            self.stop()
            raise
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
        if self._interactive_task:
            subprocess.run(["schtasks", "/End", "/TN", self._interactive_task], capture_output=True, check=False)
            subprocess.run(["schtasks", "/Delete", "/TN", self._interactive_task, "/F"], capture_output=True, check=False)
            self._interactive_task = None
        try:
            (self.state / "launch-terminal.cmd").unlink(missing_ok=True)
        except OSError:
            pass
        process = self._process
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(5)
        pids = self._running_terminal_pids()
        if not pids:
            return True
        try:
            import psutil
        except ImportError:
            return False
        try:
            for pid in pids:
                candidate = psutil.Process(pid)
                candidate.terminate()
            _, alive = psutil.wait_procs([psutil.Process(pid) for pid in pids], timeout=timeout)
            for candidate in alive:
                candidate.kill()
            return not self._running_terminal_pids()
        except (psutil.Error, OSError):
            return False
