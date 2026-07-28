from __future__ import annotations

import gc
import hashlib
import json
import logging
import os
import socket
import shutil
import subprocess
import sys
import time
from uuid import UUID
from dataclasses import dataclass
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

    @staticmethod
    def _read_assistant_config(path: Path) -> tuple[str, str]:
        raw = path.read_bytes()
        if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
            encoding = "utf-16"
        elif raw.startswith(b"\xef\xbb\xbf"):
            encoding = "utf-8-sig"
        else:
            encoding = "utf-8"
        return raw.decode(encoding), encoding

    @classmethod
    def _assistant_mcp_ports(cls, path: Path) -> tuple[int, int] | None:
        try:
            content, _ = cls._read_assistant_config(path)
        except (OSError, UnicodeDecodeError):
            return None
        ports: dict[str, int] = {}
        section = ""
        for raw_line in content.splitlines():
            line = raw_line.strip()
            if line.startswith("[") and line.endswith("]"):
                section = line.casefold()
                continue
            if not line.casefold().startswith("endpoint="):
                continue
            if section not in ("[mcp.metaeditor]", "[mcp.metatrader]"):
                continue
            prefix = "endpoint=http://127.0.0.1:"
            suffix = "/mcp"
            if not line.casefold().startswith(prefix) or not line.casefold().endswith(suffix):
                return None
            raw_port = line[len(prefix) : -len(suffix)]
            if not raw_port.isdigit():
                return None
            port = int(raw_port)
            if not 1 <= port <= 65535:
                return None
            ports[section] = port
        if set(ports) != {"[mcp.metaeditor]", "[mcp.metatrader]"}:
            return None
        return ports["[mcp.metaeditor]"], ports["[mcp.metatrader]"]

    @staticmethod
    def _ports_are_bindable(ports: tuple[int, int]) -> bool:
        sockets: list[socket.socket] = []
        try:
            for port in ports:
                candidate = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sockets.append(candidate)
                candidate.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False
        finally:
            for candidate in sockets:
                candidate.close()

    def _reserved_mcp_ports(self) -> set[int]:
        reserved: set[int] = set()
        instances_root = self.root.parent
        try:
            children = tuple(instances_root.iterdir())
        except OSError as exc:
            raise NativeMt5Error("mcp_port_inventory_failed") from exc
        for child in children:
            if child == self.root:
                continue
            try:
                UUID(child.name)
            except ValueError:
                continue
            if child.is_symlink() or not child.is_dir():
                raise NativeMt5Error("mcp_port_inventory_invalid")
            ports = self._assistant_mcp_ports(
                child / "terminal" / "Config" / "assistant.ini"
            )
            if ports is not None:
                reserved.update(ports)
        return reserved

    def _allocate_mcp_ports(self, reserved: set[int]) -> tuple[int, int]:
        pair_count = 15_000
        offset = int(UUID(self.connection_id)) % pair_count
        for step in range(pair_count):
            pair_index = (offset + step) % pair_count
            ports = (30_000 + pair_index * 2, 30_001 + pair_index * 2)
            if any(port in reserved for port in ports):
                continue
            if self._ports_are_bindable(ports):
                return ports
        raise NativeMt5Error("mcp_port_allocation_failed")

    def _ensure_mcp_endpoint_isolation(self) -> tuple[int, int]:
        path = self.terminal_root / "Config" / "assistant.ini"
        if path.is_symlink() or not path.is_file():
            raise NativeMt5Error("assistant_config_invalid")
        reserved = self._reserved_mcp_ports()
        current = self._assistant_mcp_ports(path)
        if (
            current is not None
            and 30_000 <= current[0] <= 59_998
            and current[1] == current[0] + 1
            and not any(port in reserved for port in current)
            and self._ports_are_bindable(current)
        ):
            return current
        ports = self._allocate_mcp_ports(reserved)
        try:
            content, encoding = self._read_assistant_config(path)
        except (OSError, UnicodeDecodeError) as exc:
            raise NativeMt5Error("assistant_config_invalid") from exc
        updated: list[str] = []
        section = ""
        replacements: set[str] = set()
        for raw_line in content.splitlines():
            line = raw_line.strip()
            if line.startswith("[") and line.endswith("]"):
                section = line.casefold()
            if line.casefold().startswith("endpoint="):
                if section == "[mcp.metaeditor]":
                    raw_line = f"Endpoint=http://127.0.0.1:{ports[0]}/mcp"
                    replacements.add(section)
                elif section == "[mcp.metatrader]":
                    raw_line = f"Endpoint=http://127.0.0.1:{ports[1]}/mcp"
                    replacements.add(section)
            updated.append(raw_line)
        if replacements != {"[mcp.metaeditor]", "[mcp.metatrader]"}:
            raise NativeMt5Error("assistant_config_invalid")
        temporary = path.with_suffix(".ini.tmp")
        try:
            self._write_text_durable(
                temporary, "\n".join(updated) + "\n", encoding
            )
            durable_replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return ports

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

    def _reset_default_chart_profile(self) -> int:
        """Remove generated chart state while the isolated terminal is stopped."""
        if self._running_terminal_pids():
            raise NativeMt5Error("chart_profile_in_use")
        profile = self.terminal_root / "Profiles" / "Charts" / "Default"
        try:
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
    ) -> None:
        deadline = time.monotonic() + timeout
        seen_process = False
        expected_login = f"'{login}'"
        expected_server = server.casefold()
        while time.monotonic() < deadline:
            self._check_cancelled()
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
                    self._release_interactive_task()
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
        self._write_text_durable(tmp, preferred, "ascii")
        durable_replace(tmp, self.files / "symbol-preference.txt")

    def _probe_broker_symbol(self, preferred: str, timeout: float = 30.0) -> str:
        # Read the symbol resolved IN-TERMINAL by TradeJournalDiscovery (MQL5), published to the
        # sandbox as discovered-symbol.json. This deliberately does NOT use the MetaTrader5 Python
        # IPC (mt5.initialize): that path is intermittent (-10005 "IPC timeout") and is not designed
        # for multiple terminals on one host -- both fatal for a multi-instance provisioner.
        output = self.files / "discovered-symbol.json"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not output.is_file():
            self._check_cancelled()
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
        if os.name == "nt" and not interactive_user:
            raise NativeMt5Error("dedicated_interactive_user_required")
        if interactive_user:
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
                self._release_interactive_task()
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
            self._release_interactive_task()
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
        connection_endpoint: str | None = None,
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
        self._ensure_mcp_endpoint_isolation()
        symbol = self._startup_symbol(symbol)
        self._last_symbol = symbol
        self.install_expert(expert_binary, history_mode)
        bootstrap: Path | None = None
        discovery: Path | None = None
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
            self._reset_default_chart_profile()

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
            self._reset_default_chart_profile()

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
        self._ensure_mcp_endpoint_isolation()
        symbol = self._bridge_template_symbol()
        self._last_symbol = symbol
        self.install_expert(expert_binary, history_mode)
        self._remove_readiness_files()
        self._reset_default_chart_profile()
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
        self._reset_default_chart_profile()
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
