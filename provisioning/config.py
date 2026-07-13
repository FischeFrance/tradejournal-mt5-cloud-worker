"""Configurazione host-side del provisioning agent."""

from __future__ import annotations

import os
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional


def _bool(value: Optional[str], default: bool, name: str) -> bool:
    if value is None or not value.strip():
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(
        f"{name} deve essere un booleano esplicito: true/false, yes/no, on/off oppure 1/0."
    )


def _positive_float(value: Optional[str], default: float, name: str) -> float:
    if value is None or not value.strip():
        return default
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ValueError(f"{name} deve essere numerico.") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise ValueError(f"{name} deve essere finito e positivo.")
    return parsed


def _nonnegative_int(value: Optional[str], default: int, name: str) -> int:
    if value is None or not value.strip():
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{name} deve essere un intero non negativo.") from exc
    if parsed < 0 or str(parsed) != value.strip():
        raise ValueError(f"{name} deve essere un intero non negativo canonico.")
    return parsed


def _positive_int(value: Optional[str], default: int, name: str) -> int:
    parsed = _nonnegative_int(value, default, name)
    if parsed < 1:
        raise ValueError(f"{name} deve essere un intero positivo.")
    return parsed


@dataclass(frozen=True)
class ProvisioningConfig:
    repository_root: Path
    instances_root: Path
    state_root: Path
    locks_root: Path
    secrets_root: Path
    queue_root: Path
    compose_template: Path
    mt5_template_archive: Path
    mt5_template_sha256: str
    secret_owner_uid: int = 1000
    allow_insecure_http: bool = False
    filesystem_poll_seconds: float = 2.0
    docker_timeout_seconds: float = 300.0
    runtime_target: str = "real"
    runtime_cpus: str = "1.50"
    runtime_mem_limit: str = "3g"
    worker_cpus: str = "0.50"
    worker_mem_limit: str = "512m"
    worker_dry_run: bool = True
    worker_poll_seconds: int = 5
    mt5_terminal_path: str = r"C:\Program Files\MetaTrader 5\terminal64.exe"
    python_windows_path: str = r"C:\Python311Embed\python.exe"

    def ensure_host_directories(self) -> None:
        modes = {
            self.instances_root: 0o750,
            self.state_root: 0o750,
            self.locks_root: 0o750,
            self.secrets_root: 0o700,
            self.queue_root: 0o750,
        }
        for path, mode in modes.items():
            path.mkdir(parents=True, exist_ok=True)
            path.chmod(mode)


def load_config(env: Optional[Mapping[str, str]] = None) -> ProvisioningConfig:
    source = env if env is not None else os.environ
    repository_root = Path(
        source.get("TJ_PROVISIONING_REPOSITORY_ROOT")
        or Path(__file__).resolve().parent.parent
    ).resolve()
    base = Path(source.get("TJ_PROVISIONING_ROOT", "/opt/tradejournal")).resolve()
    checksum = source.get("MT5_TEMPLATE_SHA256", "").strip().lower()
    if checksum and (len(checksum) != 64 or any(char not in "0123456789abcdef" for char in checksum)):
        raise ValueError("MT5_TEMPLATE_SHA256 deve essere un digest esadecimale SHA-256.")
    runtime_target = source.get("MT5_RUNTIME_TARGET", "real").strip()
    if runtime_target not in {"real", "mock"}:
        raise ValueError("MT5_RUNTIME_TARGET deve essere real oppure mock.")
    secret_owner_uid = _nonnegative_int(
        source.get("TJ_SECRET_OWNER_UID"), 1000, "TJ_SECRET_OWNER_UID"
    )
    if secret_owner_uid != 1000:
        raise ValueError(
            "TJ_SECRET_OWNER_UID deve essere 1000: le immagini runtime e worker usano UID 1000."
        )
    return ProvisioningConfig(
        repository_root=repository_root,
        instances_root=Path(source.get("TJ_INSTANCES_ROOT", str(base / "instances"))).resolve(),
        state_root=Path(source.get("TJ_STATE_ROOT", str(base / "state"))).resolve(),
        locks_root=Path(source.get("TJ_LOCKS_ROOT", str(base / "locks"))).resolve(),
        secrets_root=Path(source.get("TJ_SECRETS_ROOT", str(base / "secrets"))).resolve(),
        queue_root=Path(source.get("TJ_QUEUE_ROOT", str(base / "queue"))).resolve(),
        compose_template=Path(
            source.get(
                "TJ_COMPOSE_TEMPLATE",
                str(repository_root / "deploy" / "instance" / "compose.yaml"),
            )
        ).resolve(),
        mt5_template_archive=Path(
            source.get(
                "MT5_TEMPLATE_ARCHIVE",
                str(base / "templates" / "mt5-prefix.tar.zst"),
            )
        ).resolve(),
        mt5_template_sha256=checksum,
        secret_owner_uid=secret_owner_uid,
        allow_insecure_http=_bool(
            source.get("TJ_ALLOW_INSECURE_HTTP"), False, "TJ_ALLOW_INSECURE_HTTP"
        ),
        filesystem_poll_seconds=_positive_float(
            source.get("TJ_FILESYSTEM_POLL_SECONDS"), 2.0, "TJ_FILESYSTEM_POLL_SECONDS"
        ),
        docker_timeout_seconds=_positive_float(
            source.get("TJ_DOCKER_TIMEOUT_SECONDS"), 300.0, "TJ_DOCKER_TIMEOUT_SECONDS"
        ),
        runtime_target=runtime_target,
        runtime_cpus=source.get("TJ_RUNTIME_CPUS", "1.50"),
        runtime_mem_limit=source.get("TJ_RUNTIME_MEM_LIMIT", "3g"),
        worker_cpus=source.get("TJ_WORKER_CPUS", "0.50"),
        worker_mem_limit=source.get("TJ_WORKER_MEM_LIMIT", "512m"),
        worker_dry_run=_bool(
            source.get("TJ_WORKER_DRY_RUN"), True, "TJ_WORKER_DRY_RUN"
        ),
        worker_poll_seconds=_positive_int(
            source.get("TJ_WORKER_POLL_SECONDS"), 5, "TJ_WORKER_POLL_SECONDS"
        ),
        mt5_terminal_path=source.get("MT5_TERMINAL_PATH", "").strip()
        or r"C:\Program Files\MetaTrader 5\terminal64.exe",
        python_windows_path=source.get("PYTHON_WINDOWS_PATH", "").strip()
        or r"C:\Python311Embed\python.exe",
    )
