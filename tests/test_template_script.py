"""Executable safety-policy tests for the MT5 golden-template creator."""

from __future__ import annotations

import hashlib
import os
import shlex
import stat
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "create_mt5_runtime_template.sh"


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


@pytest.fixture
def fake_toolchain(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    python = shlex.quote(sys.executable)

    _write_executable(
        bin_dir / "realpath",
        f"""#!/bin/sh
if [ "${{1:-}}" = "-m" ]; then shift; fi
exec {python} -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$1"
""",
    )
    _write_executable(
        bin_dir / "pgrep",
        """#!/bin/sh
if [ "${FAKE_PGREP_ACTIVE:-0}" = "1" ]; then exit 0; fi
exit 1
""",
    )
    for command in ("wine", "wineserver", "zstd"):
        _write_executable(bin_dir / command, "#!/bin/sh\nexit 0\n")
    _write_executable(
        bin_dir / "timeout",
        """#!/bin/sh
shift
exec "$@"
""",
    )
    _write_executable(
        bin_dir / "cp",
        f"""#!/bin/sh
exec {python} - "$@" <<'PY'
import os
import shutil
import sys

source = os.path.normpath(sys.argv[-2])
destination = os.path.normpath(sys.argv[-1])
shutil.copytree(source, destination, dirs_exist_ok=True, symlinks=True)
PY
""",
    )
    _write_executable(
        bin_dir / "tar",
        f"""#!/bin/sh
exec {python} - "$@" <<'PY'
import sys
import tarfile

args = sys.argv[1:]
output = args[args.index('-cf') + 1]
source = args[args.index('-C') + 1]
with tarfile.open(output, 'w', dereference=False) as archive:
    archive.add(source, arcname='.')
PY
""",
    )
    _write_executable(
        bin_dir / "sha256sum",
        f"""#!/bin/sh
exec {python} - "$1" <<'PY'
import hashlib
import sys

path = sys.argv[1]
with open(path, 'rb') as handle:
    digest = hashlib.sha256(handle.read()).hexdigest()
print(f'{{digest}}  {{path}}')
PY
""",
    )
    return bin_dir


def _clean_prefix(tmp_path: Path) -> Path:
    prefix = tmp_path / "source-prefix"
    terminal = prefix / "drive_c" / "Program Files" / "MetaTrader 5" / "terminal64.exe"
    compiled_ea = (
        prefix / "drive_c" / "Program Files" / "MetaTrader 5"
        / "MQL5" / "Experts" / "TradeJournal" / "TradeJournalBridge.ex5"
    )
    terminal.parent.mkdir(parents=True)
    compiled_ea.parent.mkdir(parents=True)
    terminal.write_bytes(b"fake-terminal")
    compiled_ea.write_bytes(b"fake-compiled-ea")
    dosdevices = prefix / "dosdevices"
    dosdevices.mkdir()
    (dosdevices / "z:").symlink_to("/")
    for registry in ("system.reg", "user.reg", "userdef.reg"):
        (prefix / registry).write_text("WINE REGISTRY Version 2\n", encoding="utf-8")
    return prefix


def _snapshot(root: Path) -> tuple:
    entries = []
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in sorted(directories + files):
            path = current_path / name
            relative = str(path.relative_to(root))
            mode = stat.S_IMODE(path.lstat().st_mode)
            if path.is_symlink():
                entries.append((relative, "link", os.readlink(path), mode))
            elif path.is_file():
                entries.append((relative, "file", hashlib.sha256(path.read_bytes()).hexdigest(), mode))
            else:
                entries.append((relative, "dir", mode))
    return tuple(entries)


def _run(prefix: Path, output: Path, fake_toolchain: Path, **extra_env: str):
    env = os.environ.copy()
    env.update(extra_env)
    env["PATH"] = f"{fake_toolchain}{os.pathsep}{env['PATH']}"
    return subprocess.run(
        ["bash", str(SCRIPT), str(prefix), str(output)],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )


def test_clean_prefix_creates_atomic_outputs_without_mutating_source(tmp_path, fake_toolchain):
    prefix = _clean_prefix(tmp_path)
    output = tmp_path / "published" / "mt5-prefix.tar.zst"
    output.parent.mkdir()
    before = _snapshot(prefix)

    completed = _run(prefix, output, fake_toolchain)

    assert completed.returncode == 0, completed.stderr
    assert output.is_file()
    assert Path(f"{output}.sha256").is_file()
    assert _snapshot(prefix) == before
    assert not (prefix / ".tradejournal-template-version").exists()
    assert str(output) not in completed.stdout
    assert str(output) not in completed.stderr
    assert stat.S_IMODE(output.stat().st_mode) == 0o640


@pytest.mark.parametrize("as_symlink", [False, True])
def test_denylist_is_case_insensitive_and_rejects_symlinks(
    tmp_path, fake_toolchain, as_symlink
):
    prefix = _clean_prefix(tmp_path)
    sensitive_dir = prefix / "drive_c" / "users" / "runtime" / "BrokerSecret-123456" / "Config"
    sensitive_dir.mkdir(parents=True)
    artifact = sensitive_dir / "Accounts.DAT"
    if as_symlink:
        target = tmp_path / "external-account-state"
        target.write_text("placeholder", encoding="utf-8")
        artifact.symlink_to(target)
    else:
        artifact.write_text("placeholder", encoding="utf-8")
    output = tmp_path / "mt5-prefix.tar.zst"

    completed = _run(prefix, output, fake_toolchain)

    assert completed.returncode != 0
    assert not output.exists()
    assert "denylist rule matched" in completed.stderr
    assert "BrokerSecret-123456" not in completed.stderr
    assert "external-account-state" not in completed.stderr


def test_registry_policy_rejects_credentials_without_printing_values(tmp_path, fake_toolchain):
    prefix = _clean_prefix(tmp_path)
    secret_value = "SensitiveLogin-987654"
    (prefix / "user.reg").write_text(
        f'WINE REGISTRY Version 2\n"Login"="{secret_value}"\n', encoding="utf-8"
    )
    output = tmp_path / "mt5-prefix.tar.zst"

    completed = _run(prefix, output, fake_toolchain)

    assert completed.returncode != 0
    assert "registry deny rule matched: registry-credential-or-session-value" in completed.stderr
    assert secret_value not in completed.stderr
    assert "user.reg" not in completed.stderr
    assert not output.exists()


def test_active_runtime_is_rejected_without_process_details(tmp_path, fake_toolchain):
    prefix = _clean_prefix(tmp_path)
    output = tmp_path / "mt5-prefix.tar.zst"

    completed = _run(prefix, output, fake_toolchain, FAKE_PGREP_ACTIVE="1")

    assert completed.returncode != 0
    assert "Wine/MetaTrader is active" in completed.stderr
    assert "terminal64.exe" not in completed.stderr
    assert not output.exists()


def test_missing_compiled_ea_is_rejected(tmp_path, fake_toolchain):
    prefix = _clean_prefix(tmp_path)
    compiled_ea = (
        prefix / "drive_c" / "Program Files" / "MetaTrader 5"
        / "MQL5" / "Experts" / "TradeJournal" / "TradeJournalBridge.ex5"
    )
    compiled_ea.unlink()
    output = tmp_path / "mt5-prefix.tar.zst"

    completed = _run(prefix, output, fake_toolchain)

    assert completed.returncode != 0
    assert "TradeJournalBridge.ex5" in completed.stderr
    assert not output.exists()


def test_stale_ea_sandbox_files_are_rejected(tmp_path, fake_toolchain):
    prefix = _clean_prefix(tmp_path)
    stale_dir = (
        prefix / "drive_c" / "Program Files" / "MetaTrader 5"
        / "MQL5" / "Files" / "TradeJournal"
    )
    stale_dir.mkdir(parents=True)
    (stale_dir / "account.json").write_text('{"login": "12345678"}', encoding="utf-8")
    output = tmp_path / "mt5-prefix.tar.zst"

    completed = _run(prefix, output, fake_toolchain)

    assert completed.returncode != 0
    assert "denylist rule matched" in completed.stderr
    assert not output.exists()


def test_cleanup_and_activity_checks_bracket_all_temporary_work():
    source = SCRIPT.read_text(encoding="utf-8")
    assert source.index("trap cleanup EXIT") < source.index('WORK_DIR="$(mktemp')
    copy_index = source.index('cp -a --reflink=auto')
    assert source.rfind("assert_runtime_inactive", 0, copy_index) > 0
    assert source.find("assert_runtime_inactive", copy_index) > copy_index
    for process_name in (
        "wine64-preloader",
        "terminal64.exe",
        "rpcss.exe",
        "plugplay.exe",
    ):
        assert process_name in source
