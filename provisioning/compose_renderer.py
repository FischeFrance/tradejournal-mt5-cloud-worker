"""Materializzazione sicura del template Compose e della configurazione non sensibile."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Mapping

from .config import ProvisioningConfig
from .models import ProvisioningJob
from .naming import project_name


class ComposeRenderError(ValueError):
    pass


@dataclass(frozen=True)
class RenderedCompose:
    project_name: str
    instance_dir: Path
    compose_file: Path
    env_file: Path


def _atomic_write_text(path: Path, content: str, mode: int = 0o640) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temporary_path = Path(temporary)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def _dotenv_value(value: object, name: str) -> str:
    text = str(value)
    if not text or "\n" in text or "\r" in text or "\x00" in text:
        raise ComposeRenderError(f"Valore non valido per {name}.")
    if "'" in text:
        raise ComposeRenderError(f"Il valore di {name} contiene un apice non supportato.")
    return f"'{text}'"


class ComposeRenderer:
    FORBIDDEN_FRAGMENTS = (
        "ports:",
        "privileged: true",
        "privileged: \"true\"",
        "/var/run/docker.sock",
        "network_mode: host",
        "network_mode: \"host\"",
    )

    def __init__(self, config: ProvisioningConfig) -> None:
        self.config = config

    def _read_and_audit_template(self) -> str:
        path = self.config.compose_template
        if path.is_symlink() or not path.is_file():
            raise ComposeRenderError(f"Template Compose assente o non regolare: {path}.")
        text = path.read_text(encoding="utf-8")
        lowered = text.lower()
        for fragment in self.FORBIDDEN_FRAGMENTS:
            if fragment in lowered:
                raise ComposeRenderError(
                    f"Template Compose non sicuro: contiene il frammento vietato {fragment!r}."
                )
        required = (
            "expose:",
            "internal: true",
            "MT5_PASSWORD_FILE",
            "MT5_BRIDGE_TOKEN_FILE",
            "TRADEJOURNAL_BRIDGE_TOKEN_FILE",
        )
        for fragment in required:
            if fragment not in text:
                raise ComposeRenderError(f"Template Compose incompleto: manca {fragment!r}.")
        return text

    def render(
        self,
        job: ProvisioningJob,
        secret_paths: Mapping[str, Path],
        *,
        template_sha256: str,
    ) -> RenderedCompose:
        if not job.account_number or not job.server or not job.tradejournal_api_url:
            raise ComposeRenderError("Il rendering richiede un job provision completo.")
        template = self._read_and_audit_template()
        project = project_name(job.connection_id)
        root = self.config.instances_root.resolve()
        candidate = root / project
        if os.path.lexists(candidate) and (candidate.is_symlink() or not candidate.is_dir()):
            raise ComposeRenderError("Instance path esistente non sicuro.")
        candidate.mkdir(parents=True, exist_ok=True, mode=0o750)
        if candidate.is_symlink() or not candidate.is_dir():
            raise ComposeRenderError("Instance path non regolare.")
        instance_dir = candidate.resolve()
        if root not in instance_dir.parents:
            raise ComposeRenderError("Instance path non confinato nella directory prevista.")
        instance_dir.chmod(0o750)

        compose_file = instance_dir / "compose.yaml"
        env_file = instance_dir / "instance.env"
        _atomic_write_text(compose_file, template, 0o640)

        values: Dict[str, object] = {
            "REPOSITORY_ROOT": self.config.repository_root,
            "TJ_CONNECTION_ID": job.connection_id,
            "TJ_PROJECT_NAME": project,
            "TJ_SECRET_DIR": Path(secret_paths["mt5_password"]).parent,
            "MT5_TEMPLATE_ARCHIVE": (
                Path("/dev/null")
                if self.config.runtime_target == "mock"
                else self.config.mt5_template_archive
            ),
            "MT5_TEMPLATE_SHA256": template_sha256,
            "MT5_RUNTIME_TARGET": self.config.runtime_target,
            "MT5_LOGIN": job.account_number,
            "MT5_SERVER": job.server,
            "MT5_TERMINAL_PATH": self.config.mt5_terminal_path,
            "PYTHON_WINDOWS_PATH": self.config.python_windows_path,
            "TRADEJOURNAL_API_URL": job.tradejournal_api_url,
            "DRY_RUN": "true" if self.config.worker_dry_run else "false",
            "POLL_INTERVAL_SECONDS": self.config.worker_poll_seconds,
            "RUNTIME_CPUS": self.config.runtime_cpus,
            "RUNTIME_MEM_LIMIT": self.config.runtime_mem_limit,
            "WORKER_CPUS": self.config.worker_cpus,
            "WORKER_MEM_LIMIT": self.config.worker_mem_limit,
        }
        lines = [f"{name}={_dotenv_value(value, name)}" for name, value in sorted(values.items())]
        _atomic_write_text(env_file, "\n".join(lines) + "\n", 0o640)
        return RenderedCompose(project, instance_dir, compose_file, env_file)
