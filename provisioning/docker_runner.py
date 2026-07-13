"""Adapter ristretto alla Docker CLI locale; nessuna shell e nessun input libero nei comandi."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .compose_renderer import RenderedCompose

_PROJECT_RE = re.compile(r"^tjmt5-[0-9a-f]{32}$")


class DockerRunnerError(RuntimeError):
    pass


class DockerRunner:
    def __init__(
        self,
        *,
        run_fn: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        timeout_seconds: float = 300.0,
        docker_binary: str = "docker",
    ) -> None:
        self._run = run_fn
        self.timeout_seconds = timeout_seconds
        self.docker_binary = docker_binary

    def _base_command(self, rendered: RenderedCompose) -> List[str]:
        if not _PROJECT_RE.fullmatch(rendered.project_name):
            raise DockerRunnerError("Nome progetto Docker non valido.")
        for path in (rendered.compose_file, rendered.env_file):
            if path.is_symlink() or not path.is_file():
                raise DockerRunnerError(f"File Compose non valido: {path}.")
        return [
            self.docker_binary,
            "compose",
            "--project-name",
            rendered.project_name,
            "--env-file",
            str(rendered.env_file),
            "--file",
            str(rendered.compose_file),
        ]

    def _execute(
        self,
        rendered: RenderedCompose,
        action: str,
        arguments: List[str],
        *,
        capture_output: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        command = self._base_command(rendered) + arguments
        try:
            result = self._run(
                command,
                shell=False,
                check=False,
                text=True,
                capture_output=capture_output,
                timeout=self.timeout_seconds,
                cwd=str(rendered.instance_dir),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DockerRunnerError(f"Docker action '{action}' non eseguibile.") from exc
        if result.returncode != 0:
            raise DockerRunnerError(
                f"Docker action '{action}' fallita (exit code {result.returncode}); "
                "output omesso per evitare esposizione accidentale di configurazione."
            )
        return result

    def provision(self, rendered: RenderedCompose) -> None:
        self._execute(rendered, "provision", ["up", "--detach", "--build", "--wait"])

    def start(self, rendered: RenderedCompose) -> None:
        self._execute(rendered, "start", ["start"])

    def stop(self, rendered: RenderedCompose) -> None:
        self._execute(rendered, "stop", ["stop"])

    def restart(self, rendered: RenderedCompose) -> None:
        self._execute(rendered, "restart", ["restart"])

    def deprovision(self, rendered: RenderedCompose) -> None:
        self._execute(
            rendered,
            "deprovision",
            ["down", "--volumes", "--remove-orphans", "--timeout", "30"],
        )

    def status(self, rendered: RenderedCompose) -> Dict[str, Any]:
        result = self._execute(rendered, "status", ["ps", "--format", "json"])
        raw = (result.stdout or "").strip()
        if not raw:
            return {"services": []}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            # Alcune versioni Compose producono un oggetto JSON per riga.
            try:
                parsed = [json.loads(line) for line in raw.splitlines() if line.strip()]
            except json.JSONDecodeError as exc:
                raise DockerRunnerError("Output Docker status non interpretabile.") from exc
        return {"services": parsed if isinstance(parsed, list) else [parsed]}
