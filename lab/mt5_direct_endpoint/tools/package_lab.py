from __future__ import annotations

import argparse
import os
import stat
import tempfile
import zipfile
from pathlib import Path
from typing import Iterable

if os.name == "nt":
    import msvcrt  # type: ignore


LAB_RELATIVE_ROOT = Path("lab/mt5_direct_endpoint")
EXCLUDED_DIRECTORY_NAMES = frozenset(
    {"__pycache__", ".pytest_cache", "private", "raw", "sanitized"}
)
EXCLUDED_SUFFIXES = frozenset(
    {
        ".etl",
        ".evtx",
        ".ex5",
        ".pcap",
        ".pcapng",
        ".pml",
        ".pyc",
        ".pyd",
        ".pyo",
        ".wfw",
        ".zip",
    }
)
EXCLUDED_FILE_NAMES = frozenset({".DS_Store"})
ALLOWED_SOURCE_SUFFIXES = frozenset(
    {
        ".cs",
        ".csproj",
        ".json",
        ".md",
        ".mq5",
        ".ps1",
        ".psm1",
        ".py",
        ".wprp",
    }
)
ALLOWED_SOURCE_FILE_NAMES = frozenset({".gitignore"})
REQUIRED_ARCHIVE_MEMBERS = frozenset(
    {
        "lab/mt5_direct_endpoint/README.md",
        "lab/mt5_direct_endpoint/RUNBOOK.md",
        "lab/mt5_direct_endpoint/IMPLEMENTATION_REPORT.md",
        "lab/mt5_direct_endpoint/tools/lab_model.py",
        "lab/mt5_direct_endpoint/tools/package_lab.py",
        "lab/mt5_direct_endpoint/schemas/experiment-config.schema.json",
        "lab/mt5_direct_endpoint/schemas/experiment-manifest.schema.json",
        "lab/mt5_direct_endpoint/schemas/control-plan.schema.json",
        "lab/mt5_direct_endpoint/schemas/evidence.schema.json",
        "lab/mt5_direct_endpoint/schemas/direct-campaign-manifest.schema.json",
        "lab/mt5_direct_endpoint/schemas/candidate-handoff.schema.json",
    }
)
WINDOWS_RESERVED_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }
)


def repository_root() -> Path:
    return Path(__file__).resolve().parents[3]


def validate_archive_relative_parts(parts: tuple[str, ...]) -> None:
    for component in parts:
        if (
            not component
            or component in {".", ".."}
            or component.endswith((" ", "."))
            or any(
                character in {"\\", ":"}
                or character in {"<", ">", '"', "|", "?", "*"}
                or ord(character) < 32
                or ord(character) == 127
                for character in component
            )
            or component.split(".", 1)[0].upper() in WINDOWS_RESERVED_NAMES
        ):
            raise ValueError(
                "archive member is not extraction-safe on Windows: "
                + "/".join(parts)
            )


def validate_archive_name_set(names: Iterable[str]) -> None:
    windows_names: dict[str, str] = {}
    for name in names:
        windows_key = name.casefold()
        prior = windows_names.get(windows_key)
        if prior is not None:
            raise ValueError(
                "archive members collide during case-insensitive Windows "
                f"extraction: {prior}, {name}"
            )
        windows_names[windows_key] = name


def archive_members(repo_root: Path | None = None) -> tuple[tuple[Path, str], ...]:
    root = repository_root() if repo_root is None else repo_root.resolve()
    lab_root = (root / LAB_RELATIVE_ROOT).resolve()
    if not lab_root.is_dir():
        raise FileNotFoundError(f"lab directory not found: {lab_root}")
    members: list[tuple[Path, str]] = []
    for source in sorted(lab_root.rglob("*")):
        relative_parts = source.relative_to(lab_root).parts
        if any(part in EXCLUDED_DIRECTORY_NAMES for part in relative_parts):
            continue
        validate_archive_relative_parts(relative_parts)
        if source.is_symlink():
            raise ValueError(f"symlink is not allowed in review archive: {source}")
        if not source.is_file():
            continue
        if source.name in EXCLUDED_FILE_NAMES:
            continue
        if source.suffix.casefold() in EXCLUDED_SUFFIXES:
            continue
        if (
            source.name not in ALLOWED_SOURCE_FILE_NAMES
            and source.suffix.casefold() not in ALLOWED_SOURCE_SUFFIXES
        ):
            raise ValueError(
                f"unapproved source file type in review archive: {source}"
            )
        archive_name = (LAB_RELATIVE_ROOT / Path(*relative_parts)).as_posix()
        if not archive_name.startswith("lab/mt5_direct_endpoint/"):
            raise ValueError("archive member escaped the repository-relative lab root")
        members.append((source, archive_name))
    if not members:
        raise ValueError("lab archive would be empty")
    validate_archive_name_set(archive_name for _, archive_name in members)
    archive_names = {archive_name for _, archive_name in members}
    missing = sorted(REQUIRED_ARCHIVE_MEMBERS - archive_names)
    if missing:
        raise ValueError(
            "lab archive is missing required members: " + ", ".join(missing)
        )
    return tuple(members)


def build_archive(
    destination: Path, repo_root: Path | None = None
) -> tuple[str, ...]:
    requested_target = Path(os.path.abspath(os.fspath(destination)))
    if requested_target.suffix.casefold() != ".zip":
        raise ValueError("review archive destination must use the .zip suffix")
    if os.path.lexists(requested_target):
        raise FileExistsError(
            f"review archive already exists: {requested_target}"
        )
    requested_target.parent.mkdir(parents=True, exist_ok=True)
    target = requested_target.parent.resolve(strict=True) / requested_target.name
    if os.path.lexists(target):
        raise FileExistsError(f"review archive already exists: {target}")
    members = archive_members(repo_root)
    expected_names = tuple(archive_name for _, archive_name in members)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=target.parent,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with zipfile.ZipFile(
            temporary,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as archive:
            for source, archive_name in members:
                info = zipfile.ZipInfo(
                    archive_name,
                    date_time=(1980, 1, 1, 0, 0, 0),
                )
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = (
                    stat.S_IFREG | (source.stat().st_mode & 0o777)
                ) << 16
                archive.writestr(info, source.read_bytes())

        with zipfile.ZipFile(temporary, "r") as archive:
            actual_names = tuple(archive.namelist())
            if len(actual_names) != len(set(actual_names)):
                raise ValueError("review archive contains duplicate members")
            if actual_names != expected_names:
                raise ValueError("review archive membership verification failed")
            corrupt_member = archive.testzip()
            if corrupt_member is not None:
                raise ValueError(
                    f"review archive integrity failure: {corrupt_member}"
                )

            read_flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
            write_flags = os.O_RDWR | getattr(os, "O_BINARY", 0)
            descriptor = os.open(
                os.fspath(temporary),
                write_flags if os.name == "nt" else read_flags,
            )
            try:
                if os.name == "nt":
                    msvcrt._commit(descriptor)
                else:
                    os.fsync(descriptor)
            finally:
                os.close(descriptor)

            # Same-directory hard-link publication is atomic and create-only.
            # It cannot replace an existing file or follow a final symlink.
            os.link(temporary, target)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return expected_names


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Create a deterministic offline review ZIP rooted at "
            "lab/mt5_direct_endpoint/."
        )
    )
    parser.add_argument("destination", type=Path)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _parser().parse_args(list(argv) if argv is not None else None)
    names = build_archive(args.destination)
    print(f"WROTE {args.destination.resolve()} ({len(names)} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
