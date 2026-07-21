from __future__ import annotations

import gc
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
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

    def install_expert(self, expert_binary: Path, history_mode: str = "new_only") -> Path:
        if not expert_binary.is_file() or expert_binary.suffix.casefold() != ".ex5":
            raise NativeMt5Error("expert_binary_missing")
        if history_mode not in ("new_only", "from_date", "all_available"):
            raise NativeMt5Error("invalid_history_mode")
        destination = (
            self.terminal_root
            / "MQL5"
            / "Experts"
            / "TradeJournal"
            / "TradeJournalBridge.ex5"
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(expert_binary, destination)
        loader = (
            self.terminal_root
            / "MQL5"
            / "Scripts"
            / "TradeJournal"
            / "TradeJournalLoader.ex5"
        )
        if not loader.is_file():
            raise NativeMt5Error("loader_script_missing")
        if not loader.with_name("TradeJournalDiscovery.ex5").is_file():
            raise NativeMt5Error("discovery_script_missing")
        self.files.mkdir(parents=True, exist_ok=True)
        connection_tmp = self.files / "connection_id.tmp"
        connection_tmp.write_text(self.connection_id, encoding="utf-8")
        connection_tmp.replace(self.files / "connection_id")
        mode_tmp = self.files / "history_mode.tmp"
        mode_tmp.write_text(history_mode, encoding="utf-8")
        mode_tmp.replace(self.files / "history_mode")
        return destination

    def _install_bridge_template(self, symbol: str) -> Path:
        source = self.terminal_root / "Profiles" / "Templates" / "ADX.tpl"
        try:
            raw = source.read_bytes()
            encoding = "utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
            lines = raw.decode(encoding).splitlines()
        except (OSError, UnicodeDecodeError) as exc:
            raise NativeMt5Error("chart_template_missing") from exc
        if "<chart>" not in lines or "<window>" not in lines or "<expert>" in lines:
            raise NativeMt5Error("chart_template_invalid")
        for index, line in enumerate(lines):
            if line.startswith("symbol="):
                lines[index] = f"symbol={symbol}"
                break
        else:
            raise NativeMt5Error("chart_template_invalid")
        expert = [
            "<expert>",
            "name=TradeJournalBridge",
            r"path=Experts\TradeJournal\TradeJournalBridge.ex5",
            "expertmode=0",
            "<inputs>",
            "InpTimerSeconds=2",
            "InpBackfillHours=168",
            "InpSnapshotHistoryHours=87600",
            "InpCandleBars=200",
            "</inputs>",
            "</expert>",
            "",
        ]
        lines[lines.index("<window>") : lines.index("<window>")] = expert
        self.files.mkdir(parents=True, exist_ok=True)
        destination = self.files / "TradeJournalBridge.tpl"
        temporary = destination.with_suffix(".tpl.tmp")
        # MT5 chart templates are Unicode text files and require a BOM when produced outside the
        # terminal. Python's utf-16 codec writes that BOM deterministically.
        # Disable platform newline translation: on Windows, write_text() would otherwise turn
        # every explicit CRLF into CRCRLF and MT5 would silently skip structured template blocks.
        with temporary.open("w", encoding="utf-16", newline="") as handle:
            handle.write("\r\n".join(lines) + "\r\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(destination)
        return destination

    def _write_startup_config(
        self,
        login: int | None,
        server: str | None,
        password: str | None,
        symbol: str,
        *,
        keep_private: bool = False,
        start_expert: bool = True,
        open_chart: bool = False,
        expert_name: str = "TradeJournal\\TradeJournalBridge",
        script_name: str | None = None,
        filename: str = "startup.ini",
    ) -> Path:
        values = (server or "") + (password or "") + symbol + expert_name + (script_name or "")
        if (
            any(c in values for c in "\r\n")
            or Path(filename).name != filename
            or (login is None) != (server is None)
            or (password is not None and (login is None or not password))
            or (start_expert and script_name is not None)
        ):
            raise NativeMt5Error("invalid_startup_value")
        path = self.state / filename
        self.state.mkdir(parents=True, exist_ok=True)
        common = ["[Common]"]
        if login is not None and server:
            common.extend((f"Login={login}", f"Server={server}"))
        if password is not None:
            common.append(f"Password={password}")
        common.extend((f"KeepPrivate={int(keep_private)}", "NewsEnable=0", ""))
        if script_name is not None:
            # A script receives OnStart even while MT5 is completing the account switch. It waits
            # for the authorized session and then attaches the real EA through a chart template.
            sections = [
                "[Charts]",
                "ProfileLast=Default",
                "PreloadCharts=1",
                "",
                "[Experts]",
                "Enabled=1",
                "AllowLiveTrading=0",
                "AllowDllImport=0",
                "Account=0",
                "Profile=0",
                "Chart=0",
                "",
                "[StartUp]",
                f"Script={script_name}",
                f"Symbol={symbol}",
                "Period=M1",
                "ShutdownTerminal=0",
                "",
            ]
        elif start_expert:
            # MetaTrader resolves StartUp.Expert from its own MQL5/Experts directory.
            # Passing an absolute EX5 path leaves the chart open but does not reliably attach
            # the EA in portable installations.
            sections = [
                "[Charts]",
                "ProfileLast=Default",
                "PreloadCharts=1",
                "",
                "[Experts]",
                "Enabled=1",
                "AllowLiveTrading=0",
                "AllowDllImport=0",
                "Account=0",
                "Profile=0",
                "Chart=0",
                "",
                "[StartUp]",
                f"Expert={expert_name}",
                f"Symbol={symbol}",
                "Period=M1",
                "",
            ]
        elif open_chart:
            # A chart forces MT5 to hydrate the broker/account caches, but no Expert is attached
            # during this warm-up phase.
            sections = [
                "[Experts]",
                "Enabled=0",
                "AllowLiveTrading=0",
                "AllowDllImport=0",
                "",
                "[StartUp]",
                f"Symbol={symbol}",
                "Period=M1",
                "",
            ]
        else:
            # The first phase only persists the investor credential.  Loading charts or the EA
            # here reintroduces the build-6032 first-start hang that this two-phase bootstrap
            # deliberately avoids.
            sections = [
                "[Experts]",
                "Enabled=0",
                "AllowLiveTrading=0",
                "AllowDllImport=0",
                "",
            ]
        content = "\n".join((*common, *sections))
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\r\n") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            content = ""
        try:
            self._restrict_startup_acl(path)
        except Exception:
            self._secure_delete_config(path)
            raise
        return path

    def _interactive_user(self) -> str:
        # A single Windows interactive session cannot host two MT5 terminals at once (confirmed
        # live: the second instance hangs mid-init -- ~20 threads, ~570 handles, no window ever
        # created -- until the 120s auth watchdog kills it, reproduced 3/3 times, unrelated to the
        # separate MCP-port bind collision fixed earlier). TRADEJOURNAL_MT5_INTERACTIVE_USERS (new,
        # plural) is an optional comma-separated pool of Windows accounts, each expected to already
        # have its own persistent interactive/disconnected logon session on this host (created once,
        # out-of-band -- Task Scheduler's /IT launch mode requires the target user to already be
        # logged on, it does not log them on itself). Every job for a given connection_id always
        # hashes to the same pool member, so a connection's provision/historical_sync/live_sync/
        # deprovision jobs stay on the same session for its whole lifecycle. Falls back to the
        # original singular TRADEJOURNAL_MT5_INTERACTIVE_USER when the pool isn't configured, so a
        # single-slot deployment is completely unaffected.
        pool_setting = self._setting("TRADEJOURNAL_MT5_INTERACTIVE_USERS")
        pool = [u.strip() for u in pool_setting.split(",") if u.strip()] if pool_setting else []
        if pool:
            for candidate in pool:
                if not candidate.replace("-", "").replace("_", "").replace(".", "").isalnum():
                    raise NativeMt5Error("invalid_interactive_user")
            digest = hashlib.sha256(self.connection_id.encode("utf-8")).digest()
            index = int.from_bytes(digest[:8], "big") % len(pool)
            return pool[index]

        interactive_user = self._setting("TRADEJOURNAL_MT5_INTERACTIVE_USER")
        if (
            interactive_user
            and not interactive_user.replace("-", "").replace("_", "").replace(".", "").isalnum()
        ):
            raise NativeMt5Error("invalid_interactive_user")
        return interactive_user

    def _restrict_startup_acl(self, path: Path) -> None:
        # The worker normally runs as LocalSystem while MT5 must run in the active desktop
        # session. Keep the config private to SYSTEM, but allow that one configured interactive
        # identity to read it. The file is securely removed as soon as MT5 consumes it.
        WindowsSecretStore.restrict_acl(path)
        interactive_user = self._interactive_user()
        if not interactive_user:
            return
        completed = subprocess.run(
            ["icacls", str(path), "/grant", f"{interactive_user}:(R)"],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise NativeMt5Error("startup_acl_failed")

    @staticmethod
    def _secure_delete_config(path: Path | None) -> None:
        if path is None:
            return
        try:
            size = max(path.stat().st_size, 1024)
            with path.open("r+b") as handle:
                handle.seek(0)
                handle.write(b"x" * size)
                handle.truncate(size)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError:
            pass
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

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

    def _journal_checkpoint(self) -> dict[Path, int]:
        logs = self.terminal_root / "logs"
        if not logs.is_dir():
            return {}
        checkpoint: dict[Path, int] = {}
        for path in logs.glob("*.log"):
            try:
                checkpoint[path] = path.stat().st_size
            except OSError:
                continue
        return checkpoint

    def _journal_lines_since(self, checkpoint: dict[Path, int]) -> list[str]:
        logs = self.terminal_root / "logs"
        if not logs.is_dir():
            return []
        lines: list[str] = []
        for path in sorted(logs.glob("*.log")):
            try:
                size = path.stat().st_size
                offset = checkpoint.get(path, 0)
                if size < offset:
                    offset = 0
                elif size == offset:
                    continue
                with path.open("rb") as handle:
                    handle.seek(offset)
                    payload = handle.read()
            except OSError:
                continue
            lines.extend(payload.decode("utf-16-le", errors="replace").splitlines())
        return lines

    def _wait_for_authorization(
        self,
        checkpoint: dict[Path, int],
        login: int,
        server: str,
        timeout: float,
    ) -> None:
        deadline = time.monotonic() + timeout
        seen_process = False
        expected_login = f"'{login}'"
        expected_server = server.casefold()
        while time.monotonic() < deadline:
            lines = self._journal_lines_since(checkpoint)
            for line in lines:
                folded = line.casefold()
                if "invalid account" in folded:
                    raise NativeMt5Error("authorization_failed")
                if (
                    "authorized on" in folded
                    and expected_server in folded
                    and expected_login in line
                ):
                    return

            pids = self._running_terminal_pids()
            if pids:
                seen_process = True
            if self._process is not None:
                if self._process.poll() is None:
                    seen_process = True
                elif seen_process and not pids:
                    raise NativeMt5Error("mt5_process_crashed")
            elif seen_process and not pids:
                raise NativeMt5Error("mt5_process_crashed")
            time.sleep(0.5)
        raise NativeMt5Error("authorization_timeout")

    def _wait_for_account_database(self, timeout: float) -> None:
        accounts = self.terminal_root / "Config" / "accounts.dat"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if accounts.is_file() and accounts.stat().st_size > 0:
                    return
            except OSError:
                pass
            time.sleep(0.25)
        raise NativeMt5Error("account_persistence_failed")

    def _wait_for_investor_sync(
        self,
        checkpoint: dict[Path, int],
        login: int,
        timeout: float,
    ) -> None:
        deadline = time.monotonic() + timeout
        expected_login = f"'{login}'"
        synchronized = False
        investor_only = False
        seen_process = False
        while time.monotonic() < deadline:
            for line in self._journal_lines_since(checkpoint):
                if expected_login not in line:
                    continue
                folded = line.casefold()
                if "trading has been enabled" in folded:
                    raise NativeMt5Error("investor_readonly_not_verified")
                if "terminal synchronized with" in folded:
                    synchronized = True
                if "trading has been disabled - investor mode" in folded:
                    investor_only = True
            if synchronized and investor_only:
                return

            pids = self._running_terminal_pids()
            if pids:
                seen_process = True
            if seen_process and not pids:
                raise NativeMt5Error("mt5_process_crashed")
            time.sleep(0.5)
        raise NativeMt5Error("investor_sync_timeout")

    def _remove_readiness_files(self) -> None:
        for name in ("account.json", "heartbeat.json"):
            try:
                (self.files / name).unlink(missing_ok=True)
            except OSError:
                pass

    def _write_symbol_preference(self, preferred: str) -> None:
        # Published into the terminal's MQL5\Files sandbox BEFORE the discovery script starts, so
        # TradeJournalDiscovery can resolve the real broker symbol (e.g. EURUSD.raw) in-terminal.
        self.files.mkdir(parents=True, exist_ok=True)
        (self.files / "discovered-symbol.json").unlink(missing_ok=True)
        tmp = self.files / "symbol-preference.tmp"
        tmp.write_text(preferred, encoding="ascii")
        tmp.replace(self.files / "symbol-preference.txt")

    def _probe_broker_symbol(self, preferred: str, timeout: float = 30.0) -> str:
        # Read the symbol resolved IN-TERMINAL by TradeJournalDiscovery (MQL5), published to the
        # sandbox as discovered-symbol.json. This deliberately does NOT use the MetaTrader5 Python
        # IPC (mt5.initialize): that path is intermittent (-10005 "IPC timeout") and is not designed
        # for multiple terminals on one host -- both fatal for a multi-instance provisioner.
        output = self.files / "discovered-symbol.json"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not output.is_file():
            time.sleep(0.25)
        record = self._read_json(output) if output.is_file() else None
        symbol = str(record.get("symbol", "")) if record else ""
        output.unlink(missing_ok=True)
        if not symbol or len(symbol) > 64 or any(c in symbol for c in "\r\n"):
            raise NativeMt5Error("broker_symbol_probe_failed")
        return symbol

    def _start_process(
        self,
        config: Path,
        login_hint: int | None = None,
    ) -> subprocess.Popen[bytes] | None:
        login_argument = f" /login:{login_hint}" if login_hint is not None else ""
        interactive_user = self._interactive_user()
        if interactive_user:
            launcher = self.state / "launch-terminal.cmd"
            launcher_content = (
                "@echo off\r\n"
                f'start "" /b "{self.terminal}" /portable{login_argument} '
                f'/config:"{config}"\r\n'
            )
            launcher.write_text(launcher_content, encoding="utf-8")
            task = f"TradeJournalMT5-{self.connection_id}"
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
            self._boost_priority()
            return None
        arguments = [str(self.terminal), "/portable"]
        if login_hint is not None:
            arguments.append(f"/login:{login_hint}")
        arguments.append(f"/config:{config}")
        self._process = subprocess.Popen(
            arguments,
            cwd=self.terminal_root,
            close_fds=True,
        )
        return self._process

    def _boost_priority(self, timeout: float = 10.0) -> None:
        # Task Scheduler assigns /IT-launched processes a below-normal priority class that
        # `start /normal` inside the launched .cmd cannot override (the ceiling is enforced by
        # the task's own job object, not inherited from the launcher); this raises it from here,
        # which runs outside that job object and is not subject to the same ceiling. Investigated
        # (2026-07-21) as a candidate fix for the same-session two-terminal stall documented above:
        # confirmed via live testing that the priority class does change (verified AboveNormal on
        # the affected process) but the second terminal still hangs identically (~20 threads, ~570
        # handles, no window, authorization_timeout), so CPU scheduling is NOT the cause of that
        # stall -- ruled out, alongside a SharedSection desktop-heap/GDIProcessHandleQuota increase
        # tested the same night (also no effect). Kept anyway because raising a background-launched
        # terminal to at least normal-or-above priority is a reasonable improvement on its own with
        # no observed downside; it just doesn't solve the concurrency problem above.
        try:
            import psutil
        except ImportError:
            return
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            pids = self._running_terminal_pids()
            if pids:
                for pid in pids:
                    try:
                        psutil.Process(pid).nice(psutil.ABOVE_NORMAL_PRIORITY_CLASS)
                    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                        pass
                return
            time.sleep(0.2)

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
            observed_login = str(account.get("login", ""))
            if observed_login in ("", "0"):
                time.sleep(1)
                continue
            if observed_login != str(login):
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
        return self._running_executable_pids(self.terminal)

    def _running_metaeditor_pids(self) -> list[int]:
        return self._running_executable_pids(self.terminal_root / "MetaEditor64.exe")

    @staticmethod
    def _running_executable_pids(executable: Path) -> list[int]:
        try:
            import psutil
        except ImportError:
            return []
        result = []
        for process in psutil.process_iter(("pid", "exe")):
            try:
                if (
                    process.info["exe"]
                    and Path(process.info["exe"]).resolve() == executable.resolve()
                ):
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
        history_mode: str = "new_only",
        symbol: str = "EURUSD",
        # The second phase can legitimately spend ~144s on the build-6032 first-run MQL5
        # compilation before StartUp attaches the bridge.
        timeout: float = 240.0,
    ) -> NativeMt5Status:
        if not self.terminal.is_file():
            raise NativeMt5Error("terminal_start_failed")
        symbol = self._startup_symbol(symbol)
        self._last_symbol = symbol
        self.install_expert(expert_binary, history_mode)
        bootstrap: Path | None = None
        discovery: Path | None = None
        startup: Path | None = None
        try:
            # Phase 1: authenticate with the supplied investor password and ask MT5 to persist it
            # in Config/accounts.dat.  No chart or EA is opened during this first-start window.
            bootstrap = self._write_startup_config(
                login,
                server,
                investor_password,
                symbol,
                keep_private=True,
                start_expert=False,
                filename="login-bootstrap.ini",
            )
            investor_password = ""
            gc.collect()
            checkpoint = self._journal_checkpoint()
            self._start_process(bootstrap)
            self._wait_for_authorization(checkpoint, login, server, min(timeout, 120.0))
            self._wait_for_account_database(min(timeout, 15.0))
            self._secure_delete_config(bootstrap)
            bootstrap = None
            if not self.stop():
                raise NativeMt5Error("terminal_stop_failed")

            # Phase 2: open a credential-free discovery chart so MT5 hydrates the broker symbol
            # catalogue. Once investor synchronization is proven, the in-terminal MQL5 script
            # TradeJournalDiscovery selects the real broker symbol (for example EURUSD.raw instead
            # of the generic EURUSD placeholder) and publishes it to the sandbox as
            # discovered-symbol.json. No account password and no Python IPC are used here.
            discovery = self._write_startup_config(
                None,
                None,
                None,
                symbol,
                keep_private=True,
                start_expert=False,
                script_name="TradeJournal\\TradeJournalDiscovery",
                filename="symbol-discovery.ini",
            )
            self._write_symbol_preference(symbol)
            checkpoint = self._journal_checkpoint()
            self._start_process(discovery, login)
            self._wait_for_authorization(checkpoint, login, server, min(timeout, 120.0))
            self._wait_for_investor_sync(checkpoint, login, min(timeout, 120.0))
            symbol = self._probe_broker_symbol(symbol)
            self._last_symbol = symbol
            self._secure_delete_config(discovery)
            discovery = None
            if not self.stop():
                raise NativeMt5Error("terminal_stop_failed")

            # Phase 3: start passwordlessly on the persisted account. A read-only script receives
            # OnStart immediately, waits for the account to be synchronized, then applies the
            # template containing the real TradeJournalBridge EA. This avoids attaching an EA
            # during MT5's account switch, where build 6032 can load it without delivering OnInit.
            self._remove_readiness_files()
            self._install_bridge_template(symbol)
            startup = self._write_startup_config(
                None,
                None,
                None,
                symbol,
                keep_private=True,
                start_expert=False,
                script_name="TradeJournal\\TradeJournalLoader",
                filename="startup.ini",
            )
            checkpoint = self._journal_checkpoint()
            self._start_process(startup, login)
            self._wait_for_authorization(checkpoint, login, server, min(timeout, 120.0))
            self._wait_for_investor_sync(checkpoint, login, min(timeout, 120.0))
            return self._wait_for_heartbeat(min(timeout, 90.0), login, server)
        except Exception:
            # A failed bootstrap has no consumer yet, so its isolated terminal must not be
            # retained. Successful starts deliberately remain alive for history/live sync.
            self.stop()
            raise
        finally:
            investor_password = ""
            gc.collect()
            self._secure_delete_config(bootstrap)
            self._secure_delete_config(discovery)
            self._secure_delete_config(startup)

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
        self.install_expert(expert_binary, "new_only")
        self._install_bridge_template(symbol)
        config = self._write_startup_config(None, None, None, symbol)
        try:
            self._start_process(config)
            return self._wait_for_heartbeat(timeout)
        except Exception:
            self.stop()
            raise
        finally:
            self._secure_delete_config(config)

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
        self._process = None
        pids = list(
            dict.fromkeys(
                (*self._running_terminal_pids(), *self._running_metaeditor_pids())
            )
        )
        if not pids:
            return True
        try:
            import psutil
        except ImportError:
            return False
        try:
            candidates = [psutil.Process(pid) for pid in pids]
            for candidate in candidates:
                candidate.terminate()
            _, alive = psutil.wait_procs(candidates, timeout=timeout)
            for candidate in alive:
                candidate.kill()
            return not self._running_terminal_pids() and not self._running_metaeditor_pids()
        except (psutil.Error, OSError):
            return False
