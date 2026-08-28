"""Fetch and inspect the official public MT5 release without touching live MT5.

This module deliberately has no coordinator, scheduler, daemon, template or
pool integration.  One invocation downloads the fixed stable MetaQuotes
installer, verifies it, asks it to materialize a fresh isolated distribution,
and atomically publishes the verified result below a service-owned state root.

The installer exit status is never treated as proof of success.  Publication
requires a quiescent installer, a reparse-free distribution, a valid MetaQuotes
Authenticode signature on both executables, and the same numeric PE build on
``mt5setup.exe`` and the resulting ``terminal64.exe``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal, Protocol, cast
from uuid import uuid4

from worker.atomic_file import durable_replace, fsync_directory

from ..security import canonical_uuid
from ..state_store import atomic_json
from .mt5_instance import InstanceProvisioner
from .secret_store import WindowsSecretStore


MT5_STABLE_INSTALLER_URL = (
    "https://download.terminal.free/cdn/web/metaquotes.ltd/mt5/mt5setup.exe"
)
MT5_STABLE_DOWNLOAD_HOST = "download.terminal.free"
MAX_INSTALLER_BYTES = 256 * 1024 * 1024
MAX_DOWNLOAD_REDIRECTS = 3
DOWNLOAD_TIMEOUT_SECONDS = 120.0
INSTALL_TIMEOUT_SECONDS = 300.0
PROCESS_QUIESCENCE_TIMEOUT_SECONDS = 30.0

_SCHEMA_VERSION = 1
_SHA256 = re.compile(r"[0-9a-f]{64}")
_PROCESS_NAMES = frozenset(
    {
        "mt5setup.exe",
        "terminal.exe",
        "terminal64.exe",
        "metaeditor.exe",
        "metaeditor64.exe",
        "metatester.exe",
        "metatester64.exe",
    }
)
_CLASSIFICATIONS = frozenset(
    {
        "older",
        "current",
        "ahead",
        "same_build_divergent",
        "unverifiable",
    }
)
_RECOVERABLE_PARTIAL_UPDATE_PATHS = frozenset(
    {
        "metaeditor64.exe",
        "metatester64.exe",
    }
)
_MANAGED_RUNTIME_PATHS = frozenset(
    {
        "MQL5/Experts/TradeJournal/TradeJournalBridge.ex5",
        "MQL5/Scripts/TradeJournal/TradeJournalDiscovery.ex5",
        "MQL5/Scripts/TradeJournal/TradeJournalLoader.ex5",
    }
)


class Mt5PublicReleaseError(RuntimeError):
    """Sanitized failure while acquiring or inspecting a public MT5 release."""


@dataclass(frozen=True)
class AuthenticodeIdentity:
    """The certificate identity returned by an Authenticode verifier."""

    subject: str
    common_name: str
    organizations: tuple[str, ...]
    certificate_sha256: str


@dataclass(frozen=True)
class DownloadResult:
    final_url: str
    size_bytes: int


@dataclass(frozen=True)
class Mt5PublicRelease:
    release_id: str
    root: Path
    installer: Path
    terminal_root: Path
    build: int
    installer_sha256: str
    terminal_sha256: str
    code_manifest_sha256: str
    distribution_manifest_sha256: str
    installer_signer: AuthenticodeIdentity
    terminal_signer: AuthenticodeIdentity
    published_at_unix_ms: int


Mt5ReleaseClassification = Literal[
    "older",
    "current",
    "ahead",
    "same_build_divergent",
    "unverifiable",
]


@dataclass(frozen=True)
class Mt5InstanceReleaseInventory:
    connection_id: str
    root: Path
    build: int | None
    terminal_sha256: str | None
    recorded_terminal_sha256: str | None
    state_integrity: bool
    hash_integrity: bool
    code_integrity: bool
    signature_valid: bool
    classification: Mt5ReleaseClassification
    failure: str | None
    actual_code_manifest_sha256: str | None = None
    state_reseal_required: bool = False
    verified_partial_signer: str | None = None


class Downloader(Protocol):
    def __call__(self, destination: Path) -> DownloadResult: ...


class SignatureVerifier(Protocol):
    def __call__(self, path: Path) -> AuthenticodeIdentity: ...


class ProcessGuard(Protocol):
    def snapshot(self) -> object: ...

    def wait_until_quiet(
        self,
        before: object,
        installer: Path,
        destination: Path,
        timeout: float,
    ) -> bool: ...


def _absolute_without_resolving(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _reject_reparse_ancestry(path: Path, *, stop: Path | None = None) -> None:
    """Reject symlinks/reparse points in every existing path component."""

    current = _absolute_without_resolving(path)
    boundary = _absolute_without_resolving(stop) if stop is not None else None
    while True:
        if os.path.lexists(current) and InstanceProvisioner._is_reparse_point(current):
            raise Mt5PublicReleaseError("MT5 path ancestry is unsafe")
        if current == boundary or current == current.parent:
            return
        current = current.parent


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _sha256(path: Path) -> str:
    return InstanceProvisioner._sha256(path)


def _validate_positive_build(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= 0xFFFF:
        raise Mt5PublicReleaseError("MT5 PE build is invalid")
    return value


def read_windows_pe_build(path: Path) -> int:
    """Read the numeric MT5 build from the PE version resource."""

    try:
        import win32api

        version = win32api.GetFileVersionInfo(str(path), "\\")
        build = int(version["FileVersionLS"] & 0xFFFF)
    except (ImportError, KeyError, OSError, TypeError, ValueError) as exc:
        raise Mt5PublicReleaseError("MT5 PE build is unavailable") from exc
    return _validate_positive_build(build)


def _validate_https_download_url(value: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise Mt5PublicReleaseError("MT5 installer download was redirected unsafely") from exc
    if (
        parsed.scheme.casefold() != "https"
        or parsed.hostname is None
        or parsed.hostname.casefold() != MT5_STABLE_DOWNLOAD_HOST
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or bool(parsed.fragment)
    ):
        raise Mt5PublicReleaseError("MT5 installer download was redirected unsafely")
    return value


class _AllowlistedRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, max_redirects: int) -> None:
        super().__init__()
        self._max_redirects = max_redirects
        self._redirects = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        self._redirects += 1
        if self._redirects > self._max_redirects:
            raise Mt5PublicReleaseError("MT5 installer redirected too many times")
        _validate_https_download_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class HttpsMt5InstallerDownloader:
    """Bounded HTTPS downloader whose public API exposes no caller-selected URL."""

    def __init__(
        self,
        *,
        max_bytes: int = MAX_INSTALLER_BYTES,
        max_redirects: int = MAX_DOWNLOAD_REDIRECTS,
        timeout: float = DOWNLOAD_TIMEOUT_SECONDS,
        opener_factory: Callable[..., object] = urllib.request.build_opener,
    ) -> None:
        if max_bytes <= 0 or max_redirects < 0 or timeout <= 0:
            raise ValueError("MT5 downloader limits are invalid")
        self.max_bytes = max_bytes
        self.max_redirects = max_redirects
        self.timeout = timeout
        self._opener_factory = opener_factory

    def __call__(self, destination: Path) -> DownloadResult:
        destination = _absolute_without_resolving(destination)
        parent = destination.parent
        _reject_reparse_ancestry(parent)
        if (
            destination.exists()
            or InstanceProvisioner._is_reparse_point(parent)
            or not parent.is_dir()
        ):
            raise Mt5PublicReleaseError("MT5 installer destination is unsafe")
        _validate_https_download_url(MT5_STABLE_INSTALLER_URL)
        request = urllib.request.Request(
            MT5_STABLE_INSTALLER_URL,
            headers={
                "Accept": "application/octet-stream",
                "Accept-Encoding": "identity",
                "User-Agent": "TradeJournal-MT5-Release-Probe/1",
            },
            method="GET",
        )
        opener = self._opener_factory(
            _AllowlistedRedirectHandler(self.max_redirects)
        )
        written = 0
        descriptor: int | None = None
        try:
            response = opener.open(request, timeout=self.timeout)  # type: ignore[attr-defined]
            with response:
                status = getattr(response, "status", None)
                final_url = str(response.geturl())
                _validate_https_download_url(final_url)
                if status != 200:
                    raise Mt5PublicReleaseError("MT5 installer download failed")
                encoding = str(response.headers.get("Content-Encoding", "identity"))
                if encoding.casefold() != "identity":
                    raise Mt5PublicReleaseError("MT5 installer response encoding is invalid")
                declared = response.headers.get("Content-Length")
                if declared is not None:
                    try:
                        declared_size = int(declared)
                    except (TypeError, ValueError) as exc:
                        raise Mt5PublicReleaseError(
                            "MT5 installer response size is invalid"
                        ) from exc
                    if not 0 < declared_size <= self.max_bytes:
                        raise Mt5PublicReleaseError(
                            "MT5 installer response size is invalid"
                        )
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                descriptor = os.open(destination, flags, 0o600)
                with os.fdopen(descriptor, "wb") as handle:
                    descriptor = None
                    while True:
                        chunk = response.read(min(1024 * 1024, self.max_bytes + 1 - written))
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > self.max_bytes:
                            raise Mt5PublicReleaseError("MT5 installer exceeds size limit")
                        handle.write(chunk)
                    if written <= 0 or (declared is not None and written != declared_size):
                        raise Mt5PublicReleaseError("MT5 installer response is incomplete")
                    handle.flush()
                    os.fsync(handle.fileno())
        except Mt5PublicReleaseError:
            destination.unlink(missing_ok=True)
            raise
        except (OSError, urllib.error.URLError) as exc:
            destination.unlink(missing_ok=True)
            raise Mt5PublicReleaseError("MT5 installer download failed") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
        return DownloadResult(final_url=final_url, size_bytes=written)


def _certificate_identity(certificate_der: bytes) -> AuthenticodeIdentity:
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes
        from cryptography.x509.oid import NameOID

        certificate = x509.load_der_x509_certificate(certificate_der)
        common_names_raw = tuple(
            attribute.value
            for attribute in certificate.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
        )
        organizations_raw = tuple(
            attribute.value
            for attribute in certificate.subject.get_attributes_for_oid(NameOID.ORGANIZATION_NAME)
        )
        certificate_sha256 = certificate.fingerprint(hashes.SHA256()).hex()
        subject = certificate.subject.rfc4514_string()
    except (ImportError, TypeError, ValueError) as exc:
        raise Mt5PublicReleaseError("MT5 Authenticode certificate is invalid") from exc
    if not all(isinstance(value, str) for value in (*common_names_raw, *organizations_raw)):
        raise Mt5PublicReleaseError("MT5 Authenticode certificate is invalid")
    common_names = cast(tuple[str, ...], common_names_raw)
    organizations = cast(tuple[str, ...], organizations_raw)
    if (
        common_names != ("MetaQuotes Ltd.",)
        or not organizations
        or any(value != "MetaQuotes Ltd." for value in organizations)
        or not _is_sha256(certificate_sha256)
        or "\r" in subject
        or "\n" in subject
        or len(subject) > 2048
    ):
        raise Mt5PublicReleaseError("MT5 Authenticode signer is not MetaQuotes")
    return AuthenticodeIdentity(
        subject=subject,
        common_name=common_names[0],
        organizations=organizations,
        certificate_sha256=certificate_sha256,
    )


def verify_metaquotes_authenticode(path: Path) -> AuthenticodeIdentity:
    """Verify Windows chain policy, then parse the actual signer certificate."""

    script = (
        "$ErrorActionPreference='Stop';"
        "$s=Get-AuthenticodeSignature -LiteralPath $env:TRADEJOURNAL_SIGNATURE_PATH;"
        "$raw=if($null -eq $s.SignerCertificate){''}else{"
        "[Convert]::ToBase64String($s.SignerCertificate.RawData)};"
        "[pscustomobject]@{Status=[string]$s.Status;CertificateDer=$raw}"
        "|ConvertTo-Json -Compress"
    )
    environment = os.environ.copy()
    environment["TRADEJOURNAL_SIGNATURE_PATH"] = str(path)
    try:
        completed = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                script,
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=environment,
        )
        payload = json.loads(completed.stdout.strip())
        encoded = payload.get("CertificateDer") if isinstance(payload, dict) else None
        if (
            completed.returncode != 0
            or not isinstance(payload, dict)
            or payload.get("Status") != "Valid"
            or not isinstance(encoded, str)
            or not encoded
        ):
            raise Mt5PublicReleaseError("MT5 Authenticode signature is invalid")
        certificate_der = base64.b64decode(encoded, validate=True)
    except Mt5PublicReleaseError:
        raise
    except (
        OSError,
        subprocess.SubprocessError,
        json.JSONDecodeError,
        ValueError,
    ) as exc:
        raise Mt5PublicReleaseError("MT5 Authenticode signature is invalid") from exc
    return _certificate_identity(certificate_der)


def _run_installer(installer: Path, destination: Path, timeout: float) -> int:
    try:
        completed = subprocess.run(
            [str(installer), "/auto", f"/path:{destination}"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise Mt5PublicReleaseError("MT5 public installer did not complete") from exc
    return completed.returncode


class PsutilMt5ProcessGuard:
    """Reject MT5-family processes created while the isolated installer ran."""

    @staticmethod
    def _processes():
        try:
            import psutil

            return tuple(
                psutil.process_iter(
                    attrs=("pid", "create_time", "name", "exe", "cmdline")
                )
            )
        except (ImportError, OSError) as exc:
            raise Mt5PublicReleaseError("MT5 process inventory is unavailable") from exc

    def snapshot(self) -> object:
        tokens: set[tuple[int, float]] = set()
        for process in self._processes():
            try:
                tokens.add((int(process.info["pid"]), float(process.info["create_time"])))
            except (KeyError, TypeError, ValueError) as exc:
                raise Mt5PublicReleaseError("MT5 process inventory is incomplete") from exc
        return frozenset(tokens)

    @staticmethod
    def _under(path: str, root: Path) -> bool:
        if not path:
            return False
        try:
            candidate = _absolute_without_resolving(Path(path))
            return candidate == root or root in candidate.parents
        except (OSError, ValueError):
            return False

    def _new_mt5_process_exists(
        self,
        before: frozenset[tuple[int, float]],
        installer: Path,
        destination: Path,
    ) -> bool:
        installer_text = os.path.normcase(str(installer))
        destination_text = os.path.normcase(str(destination))
        for process in self._processes():
            try:
                token = (
                    int(process.info["pid"]),
                    float(process.info["create_time"]),
                )
                if token in before:
                    continue
                name = str(process.info.get("name") or "").casefold()
                executable = str(process.info.get("exe") or "")
                command = "\x00".join(str(item) for item in (process.info.get("cmdline") or ()))
            except (KeyError, TypeError, ValueError):
                # A new process that cannot be identified cannot satisfy a
                # fail-closed no-process-left-behind postcondition.
                return True
            normalized_command = os.path.normcase(command)
            if not name and not executable and not command:
                return True
            if (
                name in _PROCESS_NAMES
                or self._under(executable, destination)
                or installer_text in normalized_command
                or destination_text in normalized_command
            ):
                return True
        return False

    def wait_until_quiet(
        self,
        before: object,
        installer: Path,
        destination: Path,
        timeout: float,
    ) -> bool:
        if not isinstance(before, frozenset):
            raise Mt5PublicReleaseError("MT5 process baseline is invalid")
        deadline = time.monotonic() + timeout
        while True:
            if not self._new_mt5_process_exists(before, installer, destination):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))


def _validate_signer(identity: AuthenticodeIdentity) -> None:
    if (
        not isinstance(identity, AuthenticodeIdentity)
        or identity.common_name != "MetaQuotes Ltd."
        or not identity.organizations
        or any(value != "MetaQuotes Ltd." for value in identity.organizations)
        or not _is_sha256(identity.certificate_sha256)
        or not identity.subject
        or "\r" in identity.subject
        or "\n" in identity.subject
        or len(identity.subject) > 2048
    ):
        raise Mt5PublicReleaseError("MT5 Authenticode signer is not MetaQuotes")


def _identity_dict(identity: AuthenticodeIdentity) -> dict[str, object]:
    _validate_signer(identity)
    return {
        "subject": identity.subject,
        "common_name": identity.common_name,
        "organizations": list(identity.organizations),
        "certificate_sha256": identity.certificate_sha256,
    }


def _identity_from_dict(value: object) -> AuthenticodeIdentity:
    if not isinstance(value, dict):
        raise Mt5PublicReleaseError("MT5 public release metadata is invalid")
    organizations = value.get("organizations")
    subject = value.get("subject")
    common_name = value.get("common_name")
    certificate_sha256 = value.get("certificate_sha256")
    identity = AuthenticodeIdentity(
        subject=subject if isinstance(subject, str) else "",
        common_name=common_name if isinstance(common_name, str) else "",
        organizations=(
            tuple(organizations)
            if isinstance(organizations, list)
            and all(isinstance(item, str) for item in organizations)
            else ()
        ),
        certificate_sha256=(certificate_sha256 if isinstance(certificate_sha256, str) else ""),
    )
    _validate_signer(identity)
    return identity


def _release_id(
    build: int,
    installer_sha256: str,
    terminal_sha256: str,
    code_manifest_sha256: str,
    distribution_manifest_sha256: str,
) -> str:
    encoded = json.dumps(
        {
            "build": build,
            "installer_sha256": installer_sha256,
            "terminal_sha256": terminal_sha256,
            "code_manifest_sha256": code_manifest_sha256,
            "distribution_manifest_sha256": distribution_manifest_sha256,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _safe_remove_tree(path: Path) -> None:
    if not path.exists():
        return
    try:
        InstanceProvisioner._validate_source_tree(path)
    except ValueError as exc:
        raise Mt5PublicReleaseError("MT5 staging cleanup path is unsafe") from exc
    shutil.rmtree(path)


def _restrict_shared_acl(path: Path) -> None:
    if os.name == "nt":
        WindowsSecretStore.restrict_shared_service_acl(path)


class Mt5PublicReleaseProbe:
    """Acquire and atomically publish one verified public MT5 baseline."""

    def __init__(
        self,
        state_root: Path,
        *,
        downloader: Downloader | None = None,
        signature_verifier: SignatureVerifier = verify_metaquotes_authenticode,
        build_reader: Callable[[Path], int] = read_windows_pe_build,
        installer_runner: Callable[[Path, Path, float], int] = _run_installer,
        process_guard: ProcessGuard | None = None,
        acl_restrictor: Callable[[Path], None] = _restrict_shared_acl,
        install_timeout: float = INSTALL_TIMEOUT_SECONDS,
        process_timeout: float = PROCESS_QUIESCENCE_TIMEOUT_SECONDS,
        clock_ms: Callable[[], int] = lambda: int(time.time() * 1000),
    ) -> None:
        if install_timeout <= 0 or process_timeout <= 0:
            raise ValueError("MT5 release probe timeout is invalid")
        self.state_root = _absolute_without_resolving(state_root)
        self.releases_root = self.state_root / "releases"
        self.current_path = self.state_root / "current.json"
        self.downloader = downloader or HttpsMt5InstallerDownloader()
        self.signature_verifier = signature_verifier
        self.build_reader = build_reader
        self.installer_runner = installer_runner
        self.process_guard = process_guard or PsutilMt5ProcessGuard()
        self.acl_restrictor = acl_restrictor
        self.install_timeout = install_timeout
        self.process_timeout = process_timeout
        self.clock_ms = clock_ms

    def _ensure_state_root(self) -> None:
        _reject_reparse_ancestry(self.state_root)
        existing = self.state_root
        while not existing.exists() and existing != existing.parent:
            existing = existing.parent
        if InstanceProvisioner._is_reparse_point(existing) or not existing.is_dir():
            raise Mt5PublicReleaseError("MT5 public release state root is unsafe")
        self.state_root.mkdir(parents=True, exist_ok=True)
        self.releases_root.mkdir(exist_ok=True)
        for path in (self.state_root, self.releases_root):
            if InstanceProvisioner._is_reparse_point(path) or not path.is_dir():
                raise Mt5PublicReleaseError("MT5 public release state root is unsafe")
            self.acl_restrictor(path)

    def refresh(self) -> Mt5PublicRelease:
        """Download and publish a fresh independently materialized baseline."""

        self._ensure_state_root()
        stage = Path(
            tempfile.mkdtemp(prefix=f".public-mt5-{uuid4()}-", dir=self.state_root)
        )
        quiescent = True
        try:
            self.acl_restrictor(stage)
            installer = stage / "mt5setup.exe"
            download = self.downloader(installer)
            if download.final_url != MT5_STABLE_INSTALLER_URL:
                _validate_https_download_url(download.final_url)
            if not installer.is_file() or InstanceProvisioner._is_reparse_point(installer):
                raise Mt5PublicReleaseError("MT5 installer publication is invalid")
            if not 0 < download.size_bytes <= MAX_INSTALLER_BYTES:
                raise Mt5PublicReleaseError("MT5 installer response size is invalid")
            if installer.stat().st_size != download.size_bytes:
                raise Mt5PublicReleaseError("MT5 installer response size changed")

            installer_signer = self.signature_verifier(installer)
            _validate_signer(installer_signer)
            installer_build = _validate_positive_build(self.build_reader(installer))
            terminal_root = stage / "terminal"
            before = self.process_guard.snapshot()
            quiescent = False
            runner_error: Exception | None = None
            exit_code: int | None = None
            try:
                exit_code = self.installer_runner(
                    installer,
                    terminal_root,
                    self.install_timeout,
                )
            except Exception as exc:  # process postcondition still runs
                runner_error = exc
            quiescent = self.process_guard.wait_until_quiet(
                before,
                installer,
                terminal_root,
                self.process_timeout,
            )
            if not quiescent:
                raise Mt5PublicReleaseError("MT5 installer left a process running")
            if runner_error is not None:
                raise Mt5PublicReleaseError("MT5 public installer did not complete") from runner_error
            # The official bootstrapper is known to return 1 even after it has
            # successfully materialized a complete build.  Keep the value for
            # diagnostics, but never use it as release evidence: only the
            # independently verified postconditions below authorize publish.
            if isinstance(exit_code, bool) or not isinstance(exit_code, int):
                raise Mt5PublicReleaseError("MT5 public installer result is invalid")

            terminal = terminal_root / "terminal64.exe"
            if (
                InstanceProvisioner._is_reparse_point(terminal_root)
                or not terminal_root.is_dir()
                or InstanceProvisioner._is_reparse_point(terminal)
                or not terminal.is_file()
            ):
                raise Mt5PublicReleaseError("MT5 isolated distribution is incomplete")
            try:
                InstanceProvisioner._validate_source_tree(terminal_root)
            except ValueError as exc:
                raise Mt5PublicReleaseError("MT5 isolated distribution is unsafe") from exc
            terminal_signer = self.signature_verifier(terminal)
            _validate_signer(terminal_signer)
            terminal_build = _validate_positive_build(self.build_reader(terminal))
            if terminal_build != installer_build:
                raise Mt5PublicReleaseError("MT5 installer and terminal builds differ")

            installer_sha256 = _sha256(installer)
            terminal_sha256 = _sha256(terminal)
            code_manifest_sha256 = InstanceProvisioner._code_manifest(terminal_root)
            distribution_manifest_sha256 = InstanceProvisioner._tree_manifest(terminal_root)
            release_id = _release_id(
                installer_build,
                installer_sha256,
                terminal_sha256,
                code_manifest_sha256,
                distribution_manifest_sha256,
            )
            published_at = self.clock_ms()
            if isinstance(published_at, bool) or not isinstance(published_at, int) or published_at <= 0:
                raise Mt5PublicReleaseError("MT5 public release timestamp is invalid")
            metadata = {
                "schema_version": _SCHEMA_VERSION,
                "release_id": release_id,
                "source_url": MT5_STABLE_INSTALLER_URL,
                "published_at_unix_ms": published_at,
                "build": installer_build,
                "installer_sha256": installer_sha256,
                "terminal_sha256": terminal_sha256,
                "code_manifest_sha256": code_manifest_sha256,
                "distribution_manifest_sha256": distribution_manifest_sha256,
                "installer_signer": _identity_dict(installer_signer),
                "terminal_signer": _identity_dict(terminal_signer),
                "installer_exit_code": exit_code,
                "postconditions": {
                    "process_quiescent": True,
                    "terminal_present": True,
                    "signatures_valid": True,
                    "builds_equal": True,
                },
            }
            atomic_json(stage / "release.json", metadata)
            self.acl_restrictor(installer)
            self.acl_restrictor(terminal_root)
            self.acl_restrictor(stage / "release.json")
            InstanceProvisioner._sync_tree(stage)

            destination = self.releases_root / release_id
            if destination.exists():
                existing = self._load_release(destination)
                if existing.release_id != release_id:
                    raise Mt5PublicReleaseError("MT5 release identity collision")
                _safe_remove_tree(stage)
                stage = Path()
                release = existing
            else:
                durable_replace(stage, destination)
                stage = Path()
                fsync_directory(self.releases_root)
                self.acl_restrictor(destination)
                release = self._load_release(destination)

            pointer = {
                "schema_version": _SCHEMA_VERSION,
                "release_id": release.release_id,
                "release_metadata_sha256": _sha256(release.root / "release.json"),
            }
            atomic_json(self.current_path, pointer)
            self.acl_restrictor(self.current_path)
            return release
        finally:
            if stage != Path() and stage.exists() and quiescent:
                _safe_remove_tree(stage)

    def _load_release(self, root: Path) -> Mt5PublicRelease:
        root = _absolute_without_resolving(root)
        if root.parent != self.releases_root or not _is_sha256(root.name):
            raise Mt5PublicReleaseError("MT5 public release path is invalid")
        try:
            InstanceProvisioner._validate_source_tree(root)
            raw = json.loads((root / "release.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise Mt5PublicReleaseError("MT5 public release metadata is invalid") from exc
        required = {
            "schema_version",
            "release_id",
            "source_url",
            "published_at_unix_ms",
            "build",
            "installer_sha256",
            "terminal_sha256",
            "code_manifest_sha256",
            "distribution_manifest_sha256",
            "installer_signer",
            "terminal_signer",
            "installer_exit_code",
            "postconditions",
        }
        if not isinstance(raw, dict) or set(raw) != required:
            raise Mt5PublicReleaseError("MT5 public release metadata is invalid")
        build = _validate_positive_build(raw.get("build"))
        published_at = raw.get("published_at_unix_ms")
        postconditions = raw.get("postconditions")
        if (
            raw.get("schema_version") != _SCHEMA_VERSION
            or raw.get("release_id") != root.name
            or raw.get("source_url") != MT5_STABLE_INSTALLER_URL
            or isinstance(published_at, bool)
            or not isinstance(published_at, int)
            or published_at <= 0
            or isinstance(raw.get("installer_exit_code"), bool)
            or not isinstance(raw.get("installer_exit_code"), int)
            or postconditions
            != {
                "process_quiescent": True,
                "terminal_present": True,
                "signatures_valid": True,
                "builds_equal": True,
            }
        ):
            raise Mt5PublicReleaseError("MT5 public release metadata is invalid")
        installer_sha256 = raw.get("installer_sha256")
        terminal_sha256 = raw.get("terminal_sha256")
        code_manifest_sha256 = raw.get("code_manifest_sha256")
        distribution_manifest_sha256 = raw.get("distribution_manifest_sha256")
        if not all(
            _is_sha256(value)
            for value in (
                installer_sha256,
                terminal_sha256,
                code_manifest_sha256,
                distribution_manifest_sha256,
            )
        ):
            raise Mt5PublicReleaseError("MT5 public release metadata is invalid")
        digests = cast(
            tuple[str, str, str, str],
            (
                installer_sha256,
                terminal_sha256,
                code_manifest_sha256,
                distribution_manifest_sha256,
            ),
        )
        installer = root / "mt5setup.exe"
        terminal_root = root / "terminal"
        terminal = terminal_root / "terminal64.exe"
        if (
            InstanceProvisioner._is_reparse_point(installer)
            or InstanceProvisioner._is_reparse_point(terminal)
            or not installer.is_file()
            or not terminal.is_file()
            or _sha256(installer) != raw["installer_sha256"]
            or _sha256(terminal) != raw["terminal_sha256"]
            or InstanceProvisioner._code_manifest(terminal_root)
            != raw["code_manifest_sha256"]
            or InstanceProvisioner._tree_manifest(terminal_root)
            != raw["distribution_manifest_sha256"]
        ):
            raise Mt5PublicReleaseError("MT5 public release integrity is invalid")
        installer_signer = self.signature_verifier(installer)
        terminal_signer = self.signature_verifier(terminal)
        if (
            installer_signer != _identity_from_dict(raw["installer_signer"])
            or terminal_signer != _identity_from_dict(raw["terminal_signer"])
            or _validate_positive_build(self.build_reader(installer)) != build
            or _validate_positive_build(self.build_reader(terminal)) != build
        ):
            raise Mt5PublicReleaseError("MT5 public release verification changed")
        expected_id = _release_id(build, *digests)
        if expected_id != root.name:
            raise Mt5PublicReleaseError("MT5 public release identity is invalid")
        return Mt5PublicRelease(
            release_id=root.name,
            root=root,
            installer=installer,
            terminal_root=terminal_root,
            build=build,
            installer_sha256=digests[0],
            terminal_sha256=digests[1],
            code_manifest_sha256=digests[2],
            distribution_manifest_sha256=digests[3],
            installer_signer=installer_signer,
            terminal_signer=terminal_signer,
            published_at_unix_ms=published_at,
        )

    def load_current(self) -> Mt5PublicRelease:
        """Read and fully revalidate the atomically selected baseline."""

        try:
            _reject_reparse_ancestry(self.state_root)
            if InstanceProvisioner._is_reparse_point(self.current_path):
                raise Mt5PublicReleaseError("MT5 current release pointer is unsafe")
            pointer = json.loads(self.current_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise Mt5PublicReleaseError("MT5 current release pointer is invalid") from exc
        if (
            not isinstance(pointer, dict)
            or set(pointer)
            != {"schema_version", "release_id", "release_metadata_sha256"}
            or pointer.get("schema_version") != _SCHEMA_VERSION
            or not _is_sha256(pointer.get("release_id"))
            or not _is_sha256(pointer.get("release_metadata_sha256"))
        ):
            raise Mt5PublicReleaseError("MT5 current release pointer is invalid")
        release = self._load_release(self.releases_root / pointer["release_id"])
        if _sha256(release.root / "release.json") != pointer["release_metadata_sha256"]:
            raise Mt5PublicReleaseError("MT5 current release pointer is stale")
        return release


class Mt5ProvisionedReleaseInventory:
    """Read-only comparison of provisioned instances with a verified baseline."""

    def __init__(
        self,
        instances_root: Path,
        *,
        build_reader: Callable[[Path], int] = read_windows_pe_build,
        signature_verifier: SignatureVerifier = verify_metaquotes_authenticode,
        trusted_template_root: Path | None = None,
    ) -> None:
        self.instances_root = _absolute_without_resolving(instances_root)
        self.build_reader = build_reader
        self.signature_verifier = signature_verifier
        self.trusted_template_root = (
            _absolute_without_resolving(trusted_template_root)
            if trusted_template_root is not None
            else None
        )

    def scan(
        self,
        baseline: Mt5PublicRelease,
    ) -> tuple[Mt5InstanceReleaseInventory, ...]:
        _validate_positive_build(baseline.build)
        if not _is_sha256(baseline.terminal_sha256):
            raise ValueError("MT5 inventory baseline is invalid")
        if (
            InstanceProvisioner._is_reparse_point(self.instances_root)
            or not self.instances_root.is_dir()
        ):
            raise Mt5PublicReleaseError("MT5 instances root is unsafe")
        _reject_reparse_ancestry(self.instances_root)
        records: list[Mt5InstanceReleaseInventory] = []
        for root in sorted(self.instances_root.iterdir(), key=lambda item: item.name):
            if not root.is_dir():
                continue
            try:
                connection_id = canonical_uuid(root.name)
            except ValueError:
                continue
            state_path = root / "state" / "instance.json"
            try:
                if InstanceProvisioner._is_reparse_point(root):
                    raise ValueError("root_reparse")
                _reject_reparse_ancestry(state_path, stop=root)
                raw = json.loads(state_path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    raise ValueError("state_invalid")
            except (OSError, ValueError, json.JSONDecodeError):
                records.append(self._unverifiable(connection_id, root, "state_invalid"))
                continue
            if raw.get("status") == "deprovisioned":
                continue
            if raw.get("status") != "provisioned":
                records.append(self._unverifiable(connection_id, root, "state_invalid"))
                continue
            records.append(self._inspect_one(connection_id, root, raw, baseline))
        return tuple(records)

    @staticmethod
    def _unverifiable(
        connection_id: str,
        root: Path,
        failure: str,
        *,
        build: int | None = None,
        terminal_sha256: str | None = None,
        recorded_terminal_sha256: str | None = None,
        state_integrity: bool = False,
        hash_integrity: bool = False,
        code_integrity: bool = False,
        signature_valid: bool = False,
        actual_code_manifest_sha256: str | None = None,
    ) -> Mt5InstanceReleaseInventory:
        return Mt5InstanceReleaseInventory(
            connection_id=connection_id,
            root=root,
            build=build,
            terminal_sha256=terminal_sha256,
            recorded_terminal_sha256=recorded_terminal_sha256,
            state_integrity=state_integrity,
            hash_integrity=hash_integrity,
            code_integrity=code_integrity,
            signature_valid=signature_valid,
            classification="unverifiable",
            failure=failure,
            actual_code_manifest_sha256=actual_code_manifest_sha256,
        )

    def _inspect_one(
        self,
        connection_id: str,
        root: Path,
        state: dict[str, object],
        baseline: Mt5PublicRelease,
    ) -> Mt5InstanceReleaseInventory:
        recorded_terminal = state.get("terminal_sha256")
        recorded_code = state.get("template_code_manifest_sha256")
        state_valid = (
            state.get("connection_id") == connection_id
            and _is_sha256(recorded_terminal)
            and _is_sha256(recorded_code)
        )
        vendor_update = state.get("vendor_update")
        recorded_assets = state.get("runtime_assets_manifest_sha256")
        if recorded_assets is not None:
            state_valid = state_valid and _is_sha256(recorded_assets)
        if vendor_update is not None:
            state_valid = state_valid and (
                isinstance(vendor_update, dict)
                and vendor_update.get("schema_version") == 1
                and vendor_update.get("terminal_sha256") == recorded_terminal
                and vendor_update.get("code_manifest_sha256") == recorded_code
                and isinstance(vendor_update.get("verified_at_unix_ms"), int)
                and not isinstance(vendor_update.get("verified_at_unix_ms"), bool)
                and vendor_update.get("verified_at_unix_ms", 0) > 0
                and "MetaQuotes Ltd." in str(vendor_update.get("signer_subject", ""))
            )
        terminal = root / "terminal" / "terminal64.exe"
        actual_terminal: str | None = None
        actual_code: str | None = None
        build: int | None = None
        signature_valid = False
        hash_valid = False
        code_valid = False
        state_reseal_required = False
        verified_partial_signer: str | None = None
        failure = "state_invalid" if not state_valid else None
        try:
            if (
                InstanceProvisioner._is_reparse_point(root)
                or InstanceProvisioner._is_reparse_point(terminal)
                or not terminal.is_file()
            ):
                raise ValueError("terminal_invalid")
            InstanceProvisioner._validate_source_tree(root / "terminal")
            actual_terminal = _sha256(terminal)
            hash_valid = state_valid and actual_terminal == recorded_terminal
            actual_code = InstanceProvisioner._code_manifest(root / "terminal")
            code_valid = state_valid and actual_code == recorded_code
            if recorded_assets is not None:
                actual_assets = InstanceProvisioner._managed_runtime_assets_manifest(
                    root / "terminal"
                )
                state_valid = state_valid and actual_assets == recorded_assets
                if not state_valid:
                    failure = "state_invalid"
                hash_valid = state_valid and actual_terminal == recorded_terminal
                code_valid = state_valid and actual_code == recorded_code
            identity = self.signature_verifier(terminal)
            _validate_signer(identity)
            signature_valid = True
            build = _validate_positive_build(self.build_reader(terminal))
        except Exception:
            failure = failure or "terminal_unverifiable"
        if (
            state_valid
            and hash_valid
            and not code_valid
            and signature_valid
            and build is not None
            and actual_code is not None
        ):
            verified_partial_signer = self._verified_partial_update_signer(
                root=root,
                state=state,
                baseline=baseline,
                recorded_terminal=cast(str, recorded_terminal),
                recorded_code=cast(str, recorded_code),
            )
            if verified_partial_signer is not None:
                code_valid = True
                state_reseal_required = True
                failure = None
        if not state_valid or not hash_valid or not code_valid or not signature_valid or build is None:
            if failure is None:
                if not hash_valid:
                    failure = "terminal_hash_mismatch"
                elif not code_valid:
                    failure = "code_manifest_mismatch"
                elif not signature_valid:
                    failure = "signature_invalid"
                else:
                    failure = "build_unavailable"
            return self._unverifiable(
                connection_id,
                root,
                failure,
                build=build,
                terminal_sha256=actual_terminal,
                recorded_terminal_sha256=(
                    recorded_terminal if isinstance(recorded_terminal, str) else None
                ),
                state_integrity=state_valid,
                hash_integrity=hash_valid,
                code_integrity=code_valid,
                signature_valid=signature_valid,
                actual_code_manifest_sha256=actual_code,
            )
        if build < baseline.build:
            classification: Mt5ReleaseClassification = "older"
        elif build > baseline.build:
            classification = "ahead"
        elif actual_terminal == baseline.terminal_sha256:
            classification = "current"
        else:
            classification = "same_build_divergent"
        if classification not in _CLASSIFICATIONS:  # defensive type/runtime invariant
            raise AssertionError("unknown MT5 release classification")
        return Mt5InstanceReleaseInventory(
            connection_id=connection_id,
            root=root,
            build=build,
            terminal_sha256=actual_terminal,
            recorded_terminal_sha256=cast(str, recorded_terminal),
            state_integrity=True,
            hash_integrity=True,
            code_integrity=True,
            signature_valid=True,
            classification=classification,
            failure=None,
            actual_code_manifest_sha256=actual_code,
            state_reseal_required=state_reseal_required,
            verified_partial_signer=verified_partial_signer,
        )

    @staticmethod
    def _code_files(root: Path) -> dict[str, str]:
        InstanceProvisioner._validate_source_tree(root)
        files: dict[str, str] = {}
        for path in sorted(root.rglob("*"), key=lambda value: value.as_posix()):
            if not path.is_file() or path.suffix.casefold() not in {
                ".dll",
                ".exe",
                ".ex5",
            }:
                continue
            relative = path.relative_to(root).as_posix()
            files[relative] = _sha256(path)
        return files

    def _verified_partial_update_signer(
        self,
        *,
        root: Path,
        state: dict[str, object],
        baseline: Mt5PublicRelease,
        recorded_terminal: str,
        recorded_code: str,
    ) -> str | None:
        """Recognize only the exact signed MetaQuotes split-build state.

        MetaQuotes can replace ``metaeditor64.exe`` and ``metatester64.exe``
        before ``terminal64.exe``.  That leaves the terminal pin intact but
        makes the aggregate executable manifest stale.  It is recoverable
        only when the instance otherwise equals the current trusted template
        and the changed auxiliary files are byte-for-byte the freshly
        downloaded public baseline, with valid MetaQuotes signatures and the
        same public build.  No EX5, DLL, missing file or arbitrary PE drift is
        accepted.
        """

        trusted = self.trusted_template_root
        recorded_assets = state.get("runtime_assets_manifest_sha256")
        if trusted is None or not _is_sha256(recorded_assets):
            return None
        terminal_root = root / "terminal"
        try:
            _reject_reparse_ancestry(trusted)
            if (
                InstanceProvisioner._is_reparse_point(trusted)
                or not trusted.is_dir()
                or _sha256(trusted / "terminal64.exe") != recorded_terminal
            ):
                return None
            trusted_files = self._code_files(trusted)
            actual_files = self._code_files(terminal_root)
            changed = {
                relative
                for relative in trusted_files.keys() | actual_files.keys()
                if relative not in _MANAGED_RUNTIME_PATHS
                if trusted_files.get(relative) != actual_files.get(relative)
            }
            if not changed or not changed <= _RECOVERABLE_PARTIAL_UPDATE_PATHS:
                return None
            reconstructed = dict(actual_files)
            for relative in changed:
                if relative not in trusted_files or relative not in actual_files:
                    return None
                reconstructed[relative] = trusted_files[relative]
            reconstructed_manifest = hashlib.sha256(
                "\n".join(
                    f"{relative}:{digest}"
                    for relative, digest in sorted(reconstructed.items())
                ).encode("utf-8")
            ).hexdigest()
            if reconstructed_manifest != recorded_code:
                return None
            subjects: set[str] = set()
            for relative in changed:
                actual = terminal_root / Path(relative)
                public = baseline.terminal_root / Path(relative)
                if (
                    not public.is_file()
                    or InstanceProvisioner._is_reparse_point(public)
                    or actual_files[relative] != _sha256(public)
                    or _validate_positive_build(self.build_reader(actual))
                    != baseline.build
                    or _validate_positive_build(self.build_reader(public))
                    != baseline.build
                ):
                    return None
                actual_identity = self.signature_verifier(actual)
                public_identity = self.signature_verifier(public)
                _validate_signer(actual_identity)
                _validate_signer(public_identity)
                if actual_identity != public_identity:
                    return None
                subjects.add(actual_identity.subject)
            if len(subjects) != 1:
                return None
            return subjects.pop()
        except Exception:
            return None
