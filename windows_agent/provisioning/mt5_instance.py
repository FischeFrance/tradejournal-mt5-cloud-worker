from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from pathlib import Path

from worker.atomic_file import durable_replace, fsync_directory

from ..state_store import atomic_json, read_json
from .instance_layout import SUBDIRS, InstanceLayout
from .secret_store import WindowsSecretStore


_MT5_GENERATED_EXAMPLE_DIRS = (
    Path("MQL5/Experts/Advisors"),
    Path("MQL5/Experts/Examples"),
    Path("MQL5/Experts/Free Robots"),
    Path("MQL5/Indicators/Examples"),
    Path("MQL5/Indicators/Free Indicators"),
    Path("MQL5/Scripts/Examples"),
)


class InstanceProvisioner:
    def __init__(self, instances_root: Path, secrets_root: Path) -> None:
        self.instances_root = instances_root
        self.secrets = WindowsSecretStore(secrets_root)

    def provision(
        self,
        connection_id: str,
        source_terminal: Path | None = None,
        expected_terminal_sha256: str | None = None,
    ) -> Path:
        layout = InstanceLayout(self.instances_root, connection_id)
        destination_root = layout.path
        if destination_root.exists():
            return self.validate(connection_id, expected_terminal_sha256)

        self.instances_root.mkdir(parents=True, exist_ok=True)
        stage: Path | None = Path(
            tempfile.mkdtemp(
                prefix=f".{connection_id}.",
                suffix=".staging",
                dir=self.instances_root,
            )
        )
        try:
            for name in SUBDIRS:
                (stage / name).mkdir()
            terminal = stage / "terminal" / "terminal64.exe"
            terminal_sha256: str | None = None
            template_manifest_sha256: str | None = None
            template_code_manifest_sha256: str | None = None
            if source_terminal is not None:
                source_terminal = Path(source_terminal)
                if (
                    self._is_reparse_point(source_terminal)
                    or source_terminal.resolve().name.lower() != "terminal64.exe"
                    or not source_terminal.is_file()
                ):
                    raise ValueError("source terminal invalid")
                source_root = source_terminal.resolve().parent
                self._validate_source_tree(source_root)
                source_manifest_before = self._tree_manifest(source_root)
                source_sha256 = self._sha256(source_terminal)
                if expected_terminal_sha256 is not None:
                    expected = expected_terminal_sha256.strip().lower()
                    if (
                        len(expected) != 64
                        or any(
                            character not in "0123456789abcdef"
                            for character in expected
                        )
                        or source_sha256 != expected
                    ):
                        raise ValueError("source terminal digest mismatch")
                for source in source_root.iterdir():
                    destination = terminal.parent / source.name
                    if source.is_dir():
                        shutil.copytree(
                            source,
                            destination,
                            dirs_exist_ok=True,
                            symlinks=False,
                        )
                    else:
                        shutil.copy2(source, destination)
                source_manifest_after = self._tree_manifest(source_root)
                copied_manifest = self._tree_manifest(terminal.parent)
                if (
                    source_manifest_before != source_manifest_after
                    or copied_manifest != source_manifest_before
                ):
                    raise ValueError("terminal template changed during copy")
                template_manifest_sha256 = copied_manifest
                template_code_manifest_sha256 = self._code_manifest(
                    terminal.parent
                )
                terminal_sha256 = self._sha256(terminal)
                if terminal_sha256 != source_sha256:
                    raise ValueError("copied terminal digest mismatch")
            atomic_json(
                stage / "state" / "instance.json",
                {
                    "connection_id": connection_id,
                    "status": "provisioned",
                    "terminal": str(destination_root / "terminal" / "terminal64.exe"),
                    "terminal_sha256": terminal_sha256,
                    "template_manifest_sha256": template_manifest_sha256,
                    "template_code_manifest_sha256": (
                        template_code_manifest_sha256
                    ),
                },
            )
            self._sync_tree(stage)
            durable_replace(stage, destination_root)
            stage = None
            return destination_root
        finally:
            if stage is not None and stage.exists():
                shutil.rmtree(stage, ignore_errors=True)

    def validate(
        self,
        connection_id: str,
        expected_terminal_sha256: str | None = None,
        *,
        verify_code: bool = True,
    ) -> Path:
        root = InstanceLayout(self.instances_root, connection_id).path
        if not root.exists():
            raise ValueError("published instance missing")
        state = read_json(root / "state" / "instance.json", {})
        if (
            state.get("connection_id") != connection_id
            or state.get("status") != "provisioned"
        ):
            raise ValueError("existing instance is not a completed publication")
        self._validate_published_instance(
            root,
            state,
            expected_terminal_sha256,
            verify_code=verify_code,
        )
        return root

    def remove_generated_example_code(
        self,
        connection_id: str,
    ) -> tuple[str, ...]:
        """Remove only MT5's known bundled examples from an isolated instance.

        MT5 can materialize these signed/default EX5 folders during the
        credential-free broker wizard. They are not part of TradeJournal's
        pinned template and must not survive the subsequent code-manifest gate.
        Unknown executable paths remain untouched so validation still fails
        closed.
        """

        root = self.validate(connection_id, verify_code=False)
        terminal_root = root / "terminal"
        if self._is_reparse_point(terminal_root) or not terminal_root.is_dir():
            raise ValueError("published terminal root invalid")
        removed: list[str] = []
        for relative in _MT5_GENERATED_EXAMPLE_DIRS:
            target = terminal_root / relative
            if not target.exists():
                continue
            if self._is_reparse_point(target) or not target.is_dir():
                raise ValueError("generated example path invalid")
            self._validate_source_tree(target)
            shutil.rmtree(target)
            removed.append(relative.as_posix())
        return tuple(removed)

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @classmethod
    def _tree_manifest(cls, root: Path) -> str:
        cls._validate_source_tree(root)
        entries: list[str] = []
        for path in sorted(root.rglob("*"), key=lambda value: value.as_posix()):
            relative = path.relative_to(root).as_posix()
            if path.is_dir():
                entries.append(f"d:{relative}")
            elif path.is_file():
                entries.append(f"f:{relative}:{cls._sha256(path)}")
            else:
                raise ValueError("source terminal tree contains unsupported entries")
        return hashlib.sha256("\n".join(entries).encode("utf-8")).hexdigest()

    @classmethod
    def _validate_published_instance(
        cls,
        root: Path,
        state: dict,
        expected_terminal_sha256: str | None,
        *,
        verify_code: bool = True,
    ) -> None:
        state_path = root / "state" / "instance.json"
        terminal_root = root / "terminal"
        terminal = terminal_root / "terminal64.exe"
        if cls._is_reparse_point(root) or cls._is_reparse_point(state_path):
            raise ValueError("published instance contains a reparse point")

        recorded_terminal = state.get("terminal_sha256")
        recorded_code_manifest = state.get("template_code_manifest_sha256")
        if expected_terminal_sha256 is not None:
            expected = expected_terminal_sha256.strip().lower()
            if (
                len(expected) != 64
                or any(character not in "0123456789abcdef" for character in expected)
            ):
                raise ValueError("expected terminal digest invalid")
            if recorded_terminal != expected:
                raise ValueError("published terminal digest mismatch")
        if recorded_terminal is not None and verify_code:
            if not terminal.is_file() or cls._sha256(terminal) != recorded_terminal:
                raise ValueError("published terminal digest mismatch")
            if (
                not isinstance(recorded_code_manifest, str)
                or len(recorded_code_manifest) != 64
                or cls._code_manifest(terminal_root) != recorded_code_manifest
            ):
                raise ValueError("published terminal code manifest mismatch")

    @classmethod
    def _code_manifest(cls, root: Path) -> str:
        """Digest executable content while allowing MT5 to create mutable data/log files."""
        cls._validate_source_tree(root)
        entries: list[str] = []
        for path in sorted(root.rglob("*"), key=lambda value: value.as_posix()):
            if not path.is_file() or path.suffix.casefold() not in {
                ".dll",
                ".exe",
                ".ex5",
            }:
                continue
            relative = path.relative_to(root).as_posix()
            entries.append(f"{relative}:{cls._sha256(path)}")
        if not entries:
            raise ValueError("terminal template contains no executable content")
        return hashlib.sha256("\n".join(entries).encode("utf-8")).hexdigest()

    @staticmethod
    def _is_reparse_point(path: Path) -> bool:
        try:
            stat_result = os.lstat(path)
        except OSError:
            return True
        attributes = getattr(stat_result, "st_file_attributes", 0)
        return path.is_symlink() or bool(attributes & 0x400)

    @classmethod
    def _validate_source_tree(cls, source_root: Path) -> None:
        if cls._is_reparse_point(source_root) or not source_root.is_dir():
            raise ValueError("source terminal root invalid")
        for directory, names, files in os.walk(source_root, followlinks=False):
            directory_path = Path(directory)
            if cls._is_reparse_point(directory_path):
                raise ValueError("source terminal tree contains reparse points")
            for name in (*names, *files):
                if cls._is_reparse_point(directory_path / name):
                    raise ValueError("source terminal tree contains reparse points")

    @classmethod
    def _sync_tree(cls, root: Path) -> None:
        """Flush every staged file before the directory publication becomes visible."""
        cls._validate_source_tree(root)
        directories: list[Path] = [root]
        for path in sorted(root.rglob("*"), key=lambda value: value.as_posix()):
            if path.is_dir():
                directories.append(path)
                continue
            if not path.is_file():
                raise ValueError("staged instance contains unsupported entries")
            read_flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
            write_flags = os.O_RDWR | getattr(os, "O_BINARY", 0)
            descriptor = os.open(
                os.fspath(path),
                write_flags if os.name == "nt" else read_flags,
            )
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        for directory in reversed(directories):
            fsync_directory(directory)

    def deprovision(self, connection_id: str) -> None:
        layout = InstanceLayout(self.instances_root, connection_id)
        self.secrets.delete_connection(connection_id)
        root = layout.path
        if not root.exists():
            return
        atomic_json(
            root / "state" / "instance.json",
            {"connection_id": connection_id, "status": "deprovisioned"},
        )
        for child in (root / "terminal", root / "worker", root / "data"):
            if child.exists():
                shutil.rmtree(child)
