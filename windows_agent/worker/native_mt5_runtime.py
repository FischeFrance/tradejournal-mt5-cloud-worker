from __future__ import annotations

import gc
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

from worker.atomic_file import durable_replace

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
    requested_server: str | None = None
    effective_server: str | None = None


class NativeMt5Runtime:
    """Launch an isolated MT5 terminal with the read-only MQL5 file bridge.

    This route deliberately does not import the MetaTrader5 Python wheel.  It is
    compatible with terminal builds whose Python IPC is temporarily broken.
    """

    _MANAGED_CHART_PROFILE = "TradeJournal"
    _CACHED_SYMBOL_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{2,63}")
    _BOOTSTRAP_BASE_SYMBOLS = (
        "EURUSD",
        "GBPUSD",
        "USDJPY",
        "USDCHF",
        "AUDUSD",
        "USDCAD",
        "NZDUSD",
        "XAUUSD",
    )
    _DISCOVERY_RESOLUTIONS = frozenset(
        ("exact", "currency_pair", "name_related", "fallback")
    )
    _GENERATED_EXAMPLE_DIRS = (
        Path("MQL5/Experts/Advisors"),
        Path("MQL5/Experts/Examples"),
        Path("MQL5/Experts/Free Robots"),
        Path("MQL5/Indicators/Examples"),
        Path("MQL5/Indicators/Free Indicators"),
        Path("MQL5/Scripts/Examples"),
    )

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
        self._cancel_check: Callable[[], None] | None = None

    def set_cancel_check(self, check: Callable[[], None] | None) -> None:
        self._cancel_check = check

    def _check_cancelled(self) -> None:
        if self._cancel_check is not None:
            self._cancel_check()

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
        source_digest = self._sha256(expert_binary)
        temporary_expert = destination.with_suffix(".ex5.tmp")
        try:
            shutil.copy2(expert_binary, temporary_expert)
            # Windows os.fsync maps to _commit and therefore needs a writable descriptor.
            with temporary_expert.open("r+b") as handle:
                os.fsync(handle.fileno())
            if self._sha256(temporary_expert) != source_digest:
                raise NativeMt5Error("expert_copy_integrity_failed")
            durable_replace(temporary_expert, destination)
        finally:
            temporary_expert.unlink(missing_ok=True)
        discovery = (
            self.terminal_root
            / "MQL5"
            / "Scripts"
            / "TradeJournal"
            / "TradeJournalDiscovery.ex5"
        )
        if not discovery.is_file():
            raise NativeMt5Error("discovery_script_missing")
        self.files.mkdir(parents=True, exist_ok=True)
        connection_tmp = self.files / "connection_id.tmp"
        self._write_text_durable(connection_tmp, self.connection_id, "utf-8")
        durable_replace(connection_tmp, self.files / "connection_id")
        mode_tmp = self.files / "history_mode.tmp"
        self._write_text_durable(mode_tmp, history_mode, "utf-8")
        durable_replace(mode_tmp, self.files / "history_mode")
        return destination

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _write_text_durable(path: Path, content: str, encoding: str) -> None:
        with path.open("w", encoding=encoding, newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())

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
        durable_replace(temporary, destination)
        return destination

    def _write_symbol_preference(self, preferred: str) -> Path:
        if (
            not preferred
            or preferred != preferred.strip()
            or len(preferred) > 64
            or any(ord(character) < 32 for character in preferred)
        ):
            raise NativeMt5Error("invalid_startup_symbol")
        self.files.mkdir(parents=True, exist_ok=True)
        destination = self.files / "symbol-preference.txt"
        temporary = self.files / "symbol-preference.tmp"
        self._write_text_durable(temporary, preferred, "utf-8")
        durable_replace(temporary, destination)
        return destination

    def _probe_broker_symbol(
        self,
        preferred: str,
        login: int,
        server: str,
        timeout: float,
    ) -> str:
        if (
            not isinstance(preferred, str)
            or not preferred
            or preferred != preferred.strip()
            or type(login) is not int
            or login <= 0
            or not isinstance(server, str)
            or not server
            or server != server.strip()
            or timeout <= 0
        ):
            raise NativeMt5Error("broker_symbol_probe_invalid")
        output = self.files / "discovered-symbol.json"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._check_cancelled()
            if output.is_file():
                try:
                    record = json.loads(output.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise NativeMt5Error("broker_symbol_probe_invalid") from exc
                if not isinstance(record, dict):
                    raise NativeMt5Error("broker_symbol_probe_invalid")
                symbol = record.get("symbol")
                if (
                    record.get("schema_version") != 1
                    or record.get("connection_id") != self.connection_id
                    or type(record.get("login")) is not int
                    or record.get("login") != login
                    or not isinstance(record.get("server"), str)
                    or record["server"].casefold() != server.casefold()
                    or record.get("requested_symbol") != preferred
                    or record.get("resolution") not in self._DISCOVERY_RESOLUTIONS
                    or type(record.get("catalog_total")) is not int
                    or record["catalog_total"] <= 0
                    or record.get("synchronized") is not True
                    or type(record.get("terminal_build")) is not int
                    or record["terminal_build"] <= 0
                    or not isinstance(symbol, str)
                    or not symbol
                    or symbol != symbol.strip()
                    or len(symbol) > 64
                    or any(ord(character) < 32 for character in symbol)
                ):
                    raise NativeMt5Error("broker_symbol_probe_invalid")
                return symbol
            if self._process is not None and self._process.poll() is not None:
                raise NativeMt5Error("mt5_process_crashed")
            time.sleep(0.25)
        raise NativeMt5Error("broker_symbol_probe_failed")

    def _cached_broker_symbol(
        self,
        login: int,
        server: str,
        preferred: str,
    ) -> str | None:
        """Read an exact chart symbol from MT5's broker-scoped account cache.

        The first authenticated terminal phase may create ``selected-<login>.dat``.
        This private, broker-dependent cache is used only as a best-effort chart
        hint.  A missing/unrecognizable token is not a failure: the caller falls
        back to the configured symbol and the in-terminal Discovery script is
        always the authoritative verifier before the bridge is installed.
        """
        if not isinstance(login, int) or isinstance(login, bool) or login <= 0:
            raise NativeMt5Error("invalid_login")
        if (
            not server
            or server != server.strip()
            or len(server) > 128
            or any(character in server for character in "\\/\r\n\0")
        ):
            raise NativeMt5Error("invalid_server")
        if (
            not preferred
            or preferred != preferred.strip()
            or len(preferred) > 64
            or any(ord(character) < 32 for character in preferred)
        ):
            raise NativeMt5Error("invalid_startup_symbol")

        bases = self.terminal_root / "Bases"
        if not bases.exists():
            return None
        if self._is_reparse_point(bases) or not bases.is_dir():
            raise NativeMt5Error("broker_symbol_cache_invalid")
        try:
            matching = [
                entry
                for entry in bases.iterdir()
                if entry.name.casefold() == server.casefold()
            ]
        except OSError as exc:
            raise NativeMt5Error("broker_symbol_cache_invalid") from exc
        if len(matching) != 1:
            return None

        broker_root = matching[0]
        symbols = broker_root / "symbols"
        selected = symbols / f"selected-{login}.dat"
        for path in (broker_root, symbols):
            if not path.exists():
                return None
            if self._is_reparse_point(path) or not path.is_dir():
                raise NativeMt5Error("broker_symbol_cache_invalid")
        if not selected.exists():
            return None
        if self._is_reparse_point(selected) or not selected.is_file():
            raise NativeMt5Error("broker_symbol_cache_invalid")
        try:
            size = selected.stat().st_size
            if size <= 0 or size > 32 * 1024 * 1024:
                raise NativeMt5Error("broker_symbol_cache_invalid")
            payload = selected.read_bytes()
        except NativeMt5Error:
            raise
        except OSError as exc:
            raise NativeMt5Error("broker_symbol_cache_invalid") from exc

        decoded = payload.decode("utf-16-le", errors="ignore")
        tokens = set(self._CACHED_SYMBOL_TOKEN.findall(decoded))
        base_symbols = tuple(
            dict.fromkeys((preferred.upper(), *self._BOOTSTRAP_BASE_SYMBOLS))
        )
        candidates: list[tuple[int, int, int, str, str]] = []
        for token in tokens:
            folded = token.upper()
            for priority, base in enumerate(base_symbols):
                if base not in folded:
                    continue
                candidates.append(
                    (
                        priority,
                        0 if folded == base else 1,
                        abs(len(folded) - len(base)),
                        folded,
                        token,
                    )
                )
                break
        if not candidates:
            return None
        return min(candidates)[-1]

    def _publish_bridge_handoff(self) -> Path:
        template = self.files / "TradeJournalBridge.tpl"
        if not template.is_file():
            raise NativeMt5Error("bridge_template_invalid")
        destination = self.files / "bridge-ready"
        temporary = self.files / "bridge-ready.tmp"
        self._write_text_durable(temporary, "ready\n", "ascii")
        durable_replace(temporary, destination)
        return destination

    def _bridge_template_symbol(self) -> str:
        path = self.files / "TradeJournalBridge.tpl"
        try:
            raw = path.read_bytes()
            encoding = "utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
            lines = raw.decode(encoding).splitlines()
        except (OSError, UnicodeDecodeError) as exc:
            raise NativeMt5Error("bridge_template_invalid") from exc
        symbols = [line[len("symbol=") :] for line in lines if line.startswith("symbol=")]
        if (
            len(symbols) != 1
            or not symbols[0]
            or len(symbols[0]) > 64
            or any(character in symbols[0] for character in "\r\n")
        ):
            raise NativeMt5Error("bridge_template_invalid")
        return symbols[0]

    def _reset_managed_chart_profile(self) -> int:
        """Keep the dedicated MT5 profile empty while the isolated terminal is stopped."""
        if self._running_terminal_pids():
            raise NativeMt5Error("chart_profile_in_use")
        profiles = self.terminal_root / "Profiles" / "Charts"
        try:
            profiles_stat = os.lstat(profiles)
            if (
                profiles.is_symlink()
                or not profiles.is_dir()
                or bool(getattr(profiles_stat, "st_file_attributes", 0) & 0x400)
            ):
                raise NativeMt5Error("chart_profile_invalid")
            profile = profiles / self._MANAGED_CHART_PROFILE
            if not profile.exists():
                profile.mkdir()
            profile_stat = os.lstat(profile)
            if (
                profile.is_symlink()
                or not profile.is_dir()
                or bool(getattr(profile_stat, "st_file_attributes", 0) & 0x400)
            ):
                raise NativeMt5Error("chart_profile_invalid")
            entries = tuple(profile.iterdir())
        except NativeMt5Error:
            raise
        except OSError as exc:
            raise NativeMt5Error("chart_profile_invalid") from exc

        removable: list[Path] = []
        for entry in entries:
            if entry.name.casefold() != "order.wnd" and entry.suffix.casefold() != ".chr":
                continue
            try:
                entry_stat = os.lstat(entry)
                if (
                    entry.is_symlink()
                    or not entry.is_file()
                    or bool(getattr(entry_stat, "st_file_attributes", 0) & 0x400)
                ):
                    raise NativeMt5Error("chart_profile_invalid")
            except NativeMt5Error:
                raise
            except OSError as exc:
                raise NativeMt5Error("chart_profile_invalid") from exc
            removable.append(entry)

        try:
            for entry in removable:
                entry.unlink()
        except OSError as exc:
            raise NativeMt5Error("chart_profile_cleanup_failed") from exc
        return len(removable)

    @staticmethod
    def _is_reparse_point(path: Path) -> bool:
        try:
            stat_result = os.lstat(path)
        except OSError:
            return True
        return path.is_symlink() or bool(
            getattr(stat_result, "st_file_attributes", 0) & 0x400
        )

    def _remove_generated_example_code(self) -> tuple[str, ...]:
        """Remove only MT5's known generated examples while the terminal is stopped.

        The first authenticated MT5 start can materialize bundled examples.  Leaving
        them in the isolated instance makes the following Loader start perform a full
        recompilation before the bridge can run.  Every target is fixed, contained in
        this instance, and rejected if it is a reparse point.
        """
        if self._running_terminal_pids():
            raise NativeMt5Error("generated_example_cleanup_in_use")
        removed: list[str] = []
        for relative in self._GENERATED_EXAMPLE_DIRS:
            target = self.terminal_root / relative
            if not target.exists():
                continue
            if self._is_reparse_point(target) or not target.is_dir():
                raise NativeMt5Error("generated_example_cleanup_invalid")
            for directory, names, files in os.walk(target, followlinks=False):
                directory_path = Path(directory)
                if self._is_reparse_point(directory_path):
                    raise NativeMt5Error("generated_example_cleanup_invalid")
                if any(
                    self._is_reparse_point(directory_path / name)
                    for name in (*names, *files)
                ):
                    raise NativeMt5Error("generated_example_cleanup_invalid")
            shutil.rmtree(target)
            removed.append(relative.as_posix())
        return tuple(removed)

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
        charts = [
            "[Charts]",
            f"ProfileLast={self._MANAGED_CHART_PROFILE}",
            "PreloadCharts=0",
            "",
        ]
        if script_name is not None:
            # A script receives OnStart even while MT5 is completing the account switch. It waits
            # for the authorized session and then attaches the real EA through a chart template.
            sections = [
                *charts,
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
                *charts,
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
                *charts,
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
                *charts,
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
        interactive_user = self._setting("TRADEJOURNAL_MT5_INTERACTIVE_USER")
        if (
            interactive_user
            and not interactive_user.replace("-", "").replace("_", "").replace(".", "").isalnum()
        ):
            raise NativeMt5Error("invalid_interactive_user")
        # MT5 needs an interactive desktop for reliable chart/script initialization, but it must
        # never share the operator's Administrator desktop: every terminal window would otherwise
        # be visible during provisioning and live sync. Built-in service identities do not provide
        # the required desktop either. A dedicated, least-privilege local account is mandatory
        # whenever the scheduled-task launch path is configured.
        if interactive_user.casefold() in {
            "administrator",
            "system",
            "localsystem",
            "localservice",
            "networkservice",
        }:
            raise NativeMt5Error("interactive_user_not_dedicated")
        return interactive_user

    def _restrict_private_acl(self, path: Path, interactive_access: str) -> None:
        # The worker normally runs as LocalSystem while MT5 must run in the active desktop
        # session. Keep private artifacts restricted to SYSTEM plus that one configured identity.
        WindowsSecretStore.restrict_acl(path)
        interactive_user = self._interactive_user()
        if not interactive_user:
            return
        completed = subprocess.run(
            [
                "icacls",
                str(path),
                "/grant",
                f"{interactive_user}:{interactive_access}",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise NativeMt5Error("private_artifact_acl_failed")

    def _restrict_startup_acl(self, path: Path) -> None:
        # Startup configuration is read once, then securely removed.
        self._restrict_private_acl(path, "(R)")

    @staticmethod
    def _grant_interactive_acl(
        path: Path,
        interactive_user: str,
        access: str,
    ) -> None:
        completed = subprocess.run(
            [
                "icacls",
                str(path),
                "/grant:r",
                f"{interactive_user}:{access}",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise NativeMt5Error("interactive_runtime_acl_failed")

    def _prepare_interactive_runtime_acl(self, interactive_user: str) -> None:
        # The service publishes instances with a SYSTEM-only DACL. The dedicated desktop
        # identity needs just enough access to traverse the instance, execute the one-shot
        # launcher, and let MT5 mutate its own portable terminal tree. Deliberately do not grant
        # access to instance\secrets, data, worker, or logs.
        self._grant_interactive_acl(self.root, interactive_user, "(RX)")
        self._grant_interactive_acl(self.state, interactive_user, "(RX)")
        self._grant_interactive_acl(
            self.terminal_root,
            interactive_user,
            "(OI)(CI)(M)",
        )

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
        connection_endpoint: str | None = None,
    ) -> str:
        deadline = time.monotonic() + timeout
        seen_process = False
        expected_login = f"'{login}'"
        expected_endpoint = (connection_endpoint or "").strip().casefold()
        while time.monotonic() < deadline:
            self._check_cancelled()
            lines = self._journal_lines_since(checkpoint)
            for line in lines:
                folded = line.casefold()
                if "invalid account" in folded:
                    raise NativeMt5Error("authorization_failed")
                if expected_endpoint and expected_endpoint in folded:
                    if (
                        "connection to" in folded
                        and "failed" in folded
                    ):
                        raise NativeMt5Error("endpoint_connection_failed")
                    if (
                        "connection refused" in folded
                        or "actively refused" in folded
                    ):
                        raise NativeMt5Error("endpoint_connection_refused")
                    if (
                        "protocol mismatch" in folded
                        or "unsupported protocol" in folded
                    ):
                        raise NativeMt5Error(
                            "endpoint_protocol_incompatible"
                        )
                    if (
                        "server not found" in folded
                        or "unknown server" in folded
                    ):
                        raise NativeMt5Error(
                            "endpoint_server_unrecognized"
                        )
                if "authorized on" in folded and expected_login in line:
                    marker = folded.find("authorized on")
                    reported_server = line[
                        marker + len("authorized on") :
                    ].strip()
                    through = reported_server.casefold().find(" through ")
                    if through >= 0:
                        reported_server = reported_server[:through].strip()
                    if not reported_server:
                        continue
                    self._release_interactive_task()
                    return reported_server

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
            self._check_cancelled()
            try:
                if accounts.is_file() and accounts.stat().st_size > 0:
                    # accounts.dat contains MT5's encrypted account material. MT5 must retain
                    # modify access, but no unrelated local identity should inherit access.
                    self._restrict_private_acl(accounts, "(M)")
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
            self._check_cancelled()
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
                self._release_interactive_task()
                return

            pids = self._running_terminal_pids()
            if pids:
                seen_process = True
            if seen_process and not pids:
                raise NativeMt5Error("mt5_process_crashed")
            time.sleep(0.5)
        raise NativeMt5Error("investor_sync_timeout")

    def _remove_readiness_files(self) -> None:
        for name in (
            "account.json",
            "heartbeat.json",
            "discovered-symbol.json",
            "discovered-symbol.tmp",
            "bridge-ready",
            "bridge-ready.tmp",
            "symbol-preference.tmp",
        ):
            try:
                (self.files / name).unlink(missing_ok=True)
            except OSError:
                pass

    def _start_process(
        self,
        config: Path,
        login_hint: int | None = None,
    ) -> subprocess.Popen[bytes] | None:
        login_argument = f" /login:{login_hint}" if login_hint is not None else ""
        interactive_user = self._interactive_user()
        if os.name == "nt" and not interactive_user:
            raise NativeMt5Error("dedicated_interactive_user_required")
        if interactive_user:
            self._wait_for_interactive_session(interactive_user)
            self._prepare_interactive_runtime_acl(interactive_user)
            launcher = self.state / "launch-terminal.cmd"
            launcher_content = (
                "@echo off\r\n"
                f'start "" /b "{self.terminal}" /portable{login_argument} '
                f'/config:"{config}"\r\n'
            )
            launcher.write_text(launcher_content, encoding="utf-8")
            self._grant_interactive_acl(
                launcher,
                interactive_user,
                "(RX)",
            )
            task = f"TradeJournalMT5-{self.connection_id}"
            command = str(launcher)
            create = [
                "schtasks", "/Create", "/TN", task, "/SC", "ONCE", "/ST", "23:59",
                "/RU", interactive_user, "/IT", "/RL", "LIMITED", "/TR", command, "/F",
            ]
            completed = subprocess.run(create, capture_output=True, text=True, check=False)
            if completed.returncode != 0:
                raise NativeMt5Error("interactive_task_create_failed")
            completed = subprocess.run(["schtasks", "/Run", "/TN", task], capture_output=True, text=True, check=False)
            if completed.returncode != 0:
                raise NativeMt5Error("interactive_task_run_failed")
            self._interactive_task = task
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

    @staticmethod
    def _interactive_session_present(interactive_user: str) -> bool:
        if os.name != "nt":
            return False
        try:
            import win32ts
        except ImportError as exc:
            raise NativeMt5Error("interactive_session_probe_unavailable") from exc
        expected = interactive_user.casefold()
        try:
            sessions = win32ts.WTSEnumerateSessions(None, 1, 0)
            for session in sessions:
                if session.get("State") not in (
                    win32ts.WTSActive,
                    win32ts.WTSDisconnected,
                ):
                    continue
                observed = win32ts.WTSQuerySessionInformation(
                    None,
                    int(session["SessionId"]),
                    win32ts.WTSUserName,
                )
                if isinstance(observed, str) and observed.casefold() == expected:
                    return True
        except (KeyError, OSError, TypeError, ValueError) as exc:
            raise NativeMt5Error("interactive_session_probe_failed") from exc
        return False

    def _wait_for_interactive_session(
        self,
        interactive_user: str,
        timeout: float = 90.0,
    ) -> None:
        """Allow secure Windows autologon to finish before a reboot recovery launch."""

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._check_cancelled()
            if self._interactive_session_present(interactive_user):
                return
            time.sleep(0.5)
        raise NativeMt5Error("interactive_session_unavailable")

    def _release_interactive_task(self) -> None:
        """Delete a successful one-shot launcher without stopping its MT5 child."""
        task = self._interactive_task
        if not task:
            return
        completed = subprocess.run(
            ["schtasks", "/Delete", "/TN", task, "/F"],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            # Keep the task name so stop() can still terminate/delete it on the failure path.
            raise NativeMt5Error("interactive_task_cleanup_failed")
        self._interactive_task = None
        try:
            (self.state / "launch-terminal.cmd").unlink(missing_ok=True)
        except OSError:
            pass

    def _terminal_process_identity(self, pid: int) -> tuple[Path, int]:
        try:
            import psutil
        except ImportError as exc:
            raise NativeMt5Error("terminal_window_identity_failed") from exc
        try:
            process = psutil.Process(pid)
            executable = Path(process.exe()).resolve()
            creation_time_unix_ms = int(process.create_time() * 1000)
        except (psutil.Error, OSError, ValueError) as exc:
            raise NativeMt5Error("terminal_window_identity_failed") from exc
        if executable != self.terminal.resolve() or creation_time_unix_ms <= 0:
            raise NativeMt5Error("terminal_window_identity_failed")
        return executable, creation_time_unix_ms

    def set_terminal_window_visibility(
        self,
        pid: int,
        *,
        visible: bool,
        timeout: float = 15.0,
    ) -> dict[str, Any]:
        """Change only this terminal's windows in its dedicated interactive session."""
        interactive_user = self._interactive_user()
        if not interactive_user:
            raise NativeMt5Error("dedicated_interactive_user_required")
        executable, creation_time_unix_ms = self._terminal_process_identity(pid)
        source = (
            Path(__file__).resolve().parents[2]
            / "scripts"
            / "windows"
            / "Set-Mt5WindowVisibility.ps1"
        )
        if not source.is_file():
            raise NativeMt5Error("terminal_window_helper_missing")

        self.state.mkdir(parents=True, exist_ok=True)
        self.files.mkdir(parents=True, exist_ok=True)
        helper = self.state / "set-mt5-window-visibility.ps1"
        helper_temporary = helper.with_suffix(".ps1.tmp")
        request = self.state / "window-visibility-request.json"
        request_temporary = request.with_suffix(".json.tmp")
        result = self.files / "window-visibility-result.json"
        result_temporary = result.with_suffix(".json.tmp")
        launcher = self.state / "set-window-visibility.cmd"
        task = f"TradeJournalMT5-Window-{self.connection_id}"
        action = "show" if visible else "hide"
        task_created = False
        cleanup_failed = False
        try:
            source_digest = self._sha256(source)
            shutil.copy2(source, helper_temporary)
            with helper_temporary.open("r+b") as handle:
                os.fsync(handle.fileno())
            if self._sha256(helper_temporary) != source_digest:
                raise NativeMt5Error("terminal_window_helper_integrity_failed")
            durable_replace(helper_temporary, helper)
            self._grant_interactive_acl(helper, interactive_user, "(RX)")

            payload = {
                "schema_version": 1,
                "process_id": pid,
                "creation_time_unix_ms": creation_time_unix_ms,
                "expected_executable": str(executable),
                "action": action,
            }
            self._write_text_durable(
                request_temporary,
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
                "utf-8",
            )
            durable_replace(request_temporary, request)
            self._restrict_private_acl(request, "(R)")
            result.unlink(missing_ok=True)
            result_temporary.unlink(missing_ok=True)

            launcher_content = (
                "@echo off\r\n"
                "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass "
                f'-File "{helper}" -RequestPath "{request}" -ResultPath "{result}"\r\n'
                "exit /b %ERRORLEVEL%\r\n"
            )
            self._write_text_durable(launcher, launcher_content, "utf-8")
            self._grant_interactive_acl(launcher, interactive_user, "(RX)")

            create = [
                "schtasks",
                "/Create",
                "/TN",
                task,
                "/SC",
                "ONCE",
                "/ST",
                "23:59",
                "/RU",
                interactive_user,
                "/IT",
                "/RL",
                "LIMITED",
                "/TR",
                str(launcher),
                "/F",
            ]
            completed = subprocess.run(
                create, capture_output=True, text=True, check=False
            )
            if completed.returncode != 0:
                raise NativeMt5Error("terminal_window_task_create_failed")
            task_created = True
            completed = subprocess.run(
                ["schtasks", "/Run", "/TN", task],
                capture_output=True,
                text=True,
                check=False,
            )
            if completed.returncode != 0:
                raise NativeMt5Error("terminal_window_task_run_failed")

            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline and not result.is_file():
                self._check_cancelled()
                time.sleep(0.1)
            record = self._read_json(result) if result.is_file() else None
            if (
                record is None
                or record.get("schema_version") != 1
                or record.get("success") is not True
                or record.get("action") != action
                or record.get("process_id") != pid
                or record.get("creation_time_unix_ms")
                != creation_time_unix_ms
                or not isinstance(record.get("windows_matched"), int)
                or record["windows_matched"] < 1
                or not isinstance(record.get("visible_after"), int)
                or (visible and record["visible_after"] < 1)
                or (not visible and record["visible_after"] != 0)
            ):
                raise NativeMt5Error("terminal_window_visibility_failed")
            return record
        finally:
            if task_created:
                subprocess.run(
                    ["schtasks", "/End", "/TN", task],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                completed = subprocess.run(
                    ["schtasks", "/Delete", "/TN", task, "/F"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                cleanup_failed = completed.returncode != 0
            for path in (
                helper_temporary,
                helper,
                request_temporary,
                request,
                result_temporary,
                result,
                launcher,
            ):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    cleanup_failed = True
            if cleanup_failed and sys.exc_info()[0] is None:
                raise NativeMt5Error("terminal_window_cleanup_failed")

    def _ready_status(
        self,
        pid: int,
        account: dict[str, Any],
        heartbeat: dict[str, Any],
    ) -> NativeMt5Status:
        self._release_interactive_task()
        self.set_terminal_window_visibility(pid, visible=False)
        return NativeMt5Status(pid, account, heartbeat, self.files)

    def _wait_for_heartbeat(
        self, timeout: float, login: int | None = None, server: str | None = None
    ) -> NativeMt5Status:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._check_cancelled()
            pid = 0
            if self._process is not None:
                pid = self._process.pid
            else:
                # The one-shot scheduled task is deleted immediately after MT5
                # authorizes, while its terminal child intentionally remains alive.
                # Evidence must bind that exact running child rather than returning
                # PID 0 after the task handle has been released.
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
                return self._ready_status(pid, account or {}, heartbeat)
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
            return self._ready_status(pid, account, heartbeat)
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
        connection_endpoint: str | None = None,
        investor_password: str,
        expert_binary: Path,
        history_mode: str = "new_only",
        symbol: str = "EURUSD",
        # The bounded investor-proof window starts only after authentication succeeded.
        # It is intentionally separate from short boot/readiness checks: a broker may take
        # time to emit its journal state, but we never start the EA without that proof.
        timeout: float = 300.0,
    ) -> NativeMt5Status:
        if not self.terminal.is_file():
            raise NativeMt5Error("terminal_start_failed")
        symbol = self._startup_symbol(symbol)
        self._last_symbol = symbol
        self.install_expert(expert_binary, history_mode)
        bootstrap: Path | None = None
        startup: Path | None = None
        try:
            startup_server = connection_endpoint or server
            # Phase 1: authenticate with the supplied investor password and ask MT5 to persist it
            # in Config/accounts.dat.  No chart or EA is opened during this first-start window.
            bootstrap = self._write_startup_config(
                login,
                startup_server,
                investor_password,
                symbol,
                keep_private=True,
                start_expert=False,
                filename="login-bootstrap.ini",
            )
            checkpoint = self._journal_checkpoint()
            self._start_process(bootstrap)
            observed_server = self._wait_for_authorization(
                checkpoint,
                login,
                server,
                min(timeout, 120.0),
                startup_server,
            )
            effective_server = (
                observed_server.strip()
                if isinstance(observed_server, str)
                and observed_server.strip()
                else server
            )
            self._wait_for_account_database(min(timeout, 15.0))
            # A broker cache token can improve the initial chart choice for suffix-only
            # catalogues, but it is deliberately non-blocking.  If the private .dat format is
            # absent or opaque (as observed with FTMO), start Discovery with the configured
            # symbol and let the official in-terminal MQL5 catalogue resolve the final value.
            bootstrap_symbol = (
                self._cached_broker_symbol(login, effective_server, symbol) or symbol
            )
            self._secure_delete_config(bootstrap)
            bootstrap = None
            if not self.stop():
                raise NativeMt5Error("terminal_stop_failed")
            self._reset_managed_chart_profile()
            self._remove_generated_example_code()

            # Phase 2: re-authenticate from a protected, short-lived configuration and run the
            # in-terminal Discovery script. A cache-derived chart symbol is only an optional hint;
            # Discovery resolves and confirms the final symbol from MT5's official catalogue,
            # synchronizes it, then publishes a result correlated to this account and instance.
            self._remove_readiness_files()
            self._write_symbol_preference(symbol)
            self._last_symbol = bootstrap_symbol
            startup = self._write_startup_config(
                login,
                effective_server,
                investor_password,
                bootstrap_symbol,
                keep_private=True,
                start_expert=False,
                script_name="TradeJournal\\TradeJournalDiscovery",
                filename="startup.ini",
            )
            investor_password = ""
            gc.collect()
            checkpoint = self._journal_checkpoint()
            self._start_process(startup)
            self._wait_for_authorization(
                checkpoint,
                login,
                effective_server,
                min(timeout, 120.0),
            )
            self._wait_for_investor_sync(checkpoint, login, min(timeout, 120.0))
            resolved_symbol = self._probe_broker_symbol(
                symbol,
                login,
                effective_server,
                min(timeout, 120.0),
            )
            self._last_symbol = resolved_symbol
            self._install_bridge_template(resolved_symbol)
            self._publish_bridge_handoff()
            status = self._wait_for_heartbeat(
                min(timeout, 90.0),
                login,
                effective_server,
            )
            if effective_server.casefold() == server.casefold():
                return status
            return replace(
                status,
                requested_server=server,
                effective_server=effective_server,
            )
        except Exception:
            # A failed bootstrap has no consumer yet, so its isolated terminal must not be
            # retained. Successful starts deliberately remain alive for history/live sync.
            self.stop()
            raise
        finally:
            investor_password = ""
            gc.collect()
            self._secure_delete_config(bootstrap)
            self._secure_delete_config(startup)

    def resume(
        self,
        *,
        login: int,
        server: str,
        expert_binary: Path,
        history_mode: str = "new_only",
        timeout: float = 120.0,
    ) -> NativeMt5Status:
        """Resume a previously provisioned account without reusing a plaintext password."""
        if not self.terminal.is_file():
            raise NativeMt5Error("terminal_start_failed")
        symbol = self._bridge_template_symbol()
        self._last_symbol = symbol
        self.install_expert(expert_binary, history_mode)
        self._remove_readiness_files()
        self._reset_managed_chart_profile()
        config = self._write_startup_config(
            login,
            server,
            None,
            symbol,
            keep_private=True,
            start_expert=True,
            filename="resume.ini",
        )
        try:
            checkpoint = self._journal_checkpoint()
            self._start_process(config)
            self._wait_for_authorization(checkpoint, login, server, min(timeout, 90.0))
            self._wait_for_investor_sync(checkpoint, login, min(timeout, 90.0))
            return self._wait_for_heartbeat(min(timeout, 60.0), login, server)
        except Exception:
            self.stop()
            raise
        finally:
            self._secure_delete_config(config)

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
        self._reset_managed_chart_profile()
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
            # A terminal can naturally disappear between the executable scan above and
            # Process(pid).  That is already a successful cleanup, not a failure.
            candidates = []
            for pid in pids:
                try:
                    candidates.append(psutil.Process(pid))
                except psutil.NoSuchProcess:
                    continue

            for candidate in candidates:
                try:
                    candidate.terminate()
                except psutil.NoSuchProcess:
                    continue

            _, alive = psutil.wait_procs(candidates, timeout=timeout)
            kill_candidates = []
            for candidate in alive:
                try:
                    candidate.kill()
                    kill_candidates.append(candidate)
                except psutil.NoSuchProcess:
                    continue

            # kill() is asynchronous on Windows.  Do not immediately rescan and turn
            # that normal termination race into terminal_stop_failed.
            if kill_candidates:
                _, still_alive = psutil.wait_procs(
                    kill_candidates,
                    timeout=min(5.0, timeout),
                )
                if still_alive:
                    return False
            return not self._running_terminal_pids() and not self._running_metaeditor_pids()
        except (psutil.AccessDenied, OSError):
            # Fail closed only when the exact instance process cannot be controlled.
            return False
