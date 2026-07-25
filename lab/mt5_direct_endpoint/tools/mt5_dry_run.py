"""Generate an MT5 endpoint configuration without launching MT5."""
from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .endpoint_registry import RegistryError, resolve_verified


class DryRunError(ValueError):
    """Raised when a safe MT5 dry-run plan cannot be produced."""


@dataclass(frozen=True)
class DryRunPlan:
    server: str
    config_path: Path
    command: tuple[str, ...]


def resolve_mt5_endpoint(
    registry_path: str | Path,
    broker_label: str,
    *,
    now_unix_ms: int | None = None,
    artifact_root: str | Path | None = None,
    artifact_manifest: str | Path | None = None,
) -> dict[str, object]:
    """Resolve exactly one current VERIFIED endpoint, failing on ambiguity."""
    try:
        records = resolve_verified(
            registry_path,
            broker_label=broker_label,
            now_unix_ms=now_unix_ms,
            artifact_root=artifact_root,
            artifact_manifest=artifact_manifest,
        )
    except RegistryError as exc:
        raise DryRunError(str(exc)) from exc
    if len(records) != 1:
        raise DryRunError("exactly one current VERIFIED endpoint is required")
    return records[0]


def _server(record: dict[str, object]) -> str:
    host = record.get("host")
    port = record.get("port")
    if not isinstance(host, str) or not isinstance(port, int):
        raise DryRunError("resolved endpoint is malformed")
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


@contextmanager
def mt5_config_dry_run(
    registry_path: str | Path,
    broker_label: str,
    *,
    terminal_path: str | Path = "terminal64.exe",
    directory: str | Path | None = None,
    now_unix_ms: int | None = None,
    artifact_root: str | Path | None = None,
    artifact_manifest: str | Path | None = None,
) -> Iterator[DryRunPlan]:
    """Yield an atomic, credential-free config and remove it on exit.

    This function only creates a plan; it never invokes ``terminal64.exe``.
    """
    record = resolve_mt5_endpoint(
        registry_path,
        broker_label,
        now_unix_ms=now_unix_ms,
        artifact_root=artifact_root,
        artifact_manifest=artifact_manifest,
    )
    server = _server(record)
    target_dir = Path(directory) if directory is not None else Path(tempfile.mkdtemp(prefix="mt5-dry-run-"))
    target_dir.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".mt5-config-", suffix=".ini", dir=target_dir)
    config_path = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-16") as handle:
            handle.write(f"[Common]\r\nServer={server}\r\nKeepPrivate=0\r\nNewsEnable=0\r\n")
            handle.flush()
            os.fsync(handle.fileno())
        plan = DryRunPlan(
            server=server,
            config_path=config_path,
            command=(str(terminal_path), "/portable", f"/config:{config_path}"),
        )
        yield plan
    finally:
        try:
            config_path.unlink()
        except FileNotFoundError:
            pass
        if directory is None:
            try:
                target_dir.rmdir()
            except OSError:
                pass


def dry_run_json(plan: DryRunPlan) -> str:
    """Return safe machine-readable output; no credentials are accepted."""
    return json.dumps(
        {"server": plan.server, "config_path": str(plan.config_path), "command": list(plan.command), "launched": False},
        sort_keys=True,
    )
