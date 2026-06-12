"""Launch and acquire the Anvil ACP server binary."""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from collections.abc import Iterator
from pathlib import Path

import httpx

from brokk_code.release_resolver import ReleaseResolverError, latest_github_release_version
from brokk_code.settings import get_global_cache_dir
from brokk_code.workspace import resolve_workspace_dir

logger = logging.getLogger(__name__)

_ANVIL_GITHUB_REPO = "BrokkAi/anvil"
_ANVIL_RELEASE_URL = "https://github.com/BrokkAi/anvil/releases/download"
_ANVIL_DOWNLOAD_TIMEOUT_SECONDS = 300.0
_ANVIL_LOCK_TIMEOUT_SECONDS = 600.0
_ANVIL_VERSION_PROBE_TIMEOUT_SECONDS = 5.0


class AnvilInstallError(Exception):
    """Raised when Anvil cannot be resolved or downloaded."""


def run_anvil_acp_server(
    *,
    workspace_dir: Path,
    binary_override: Path | None = None,
    version: str | None = None,
    passthrough_args: list[str] | None = None,
) -> None:
    """Launch Anvil as the ACP stdio server."""
    resolved_workspace_dir = resolve_workspace_dir(workspace_dir)
    launcher = "anvil"

    try:
        binary = resolve_anvil_binary(
            version=version,
            override=binary_override,
            prefer_local=version is None,
        )
        launcher = str(binary)
        command = [str(binary)]
        if passthrough_args:
            command.extend(passthrough_args)

        os.chdir(resolved_workspace_dir)
        env = _anvil_subprocess_env()
        if sys.platform == "win32":
            result = subprocess.run(command, env=env)
            sys.exit(result.returncode)
        os.execvpe(launcher, command, env)
    except AnvilInstallError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    except FileNotFoundError:
        print(
            f"Error: Unable to launch Anvil via '{launcher}'. "
            "Ensure Anvil is installed or pass --anvil-binary.",
            file=sys.stderr,
        )
        sys.exit(1)
    except OSError as exc:
        print(f"Error: Failed to launch Anvil: {exc}", file=sys.stderr)
        sys.exit(1)


def _anvil_subprocess_env() -> dict[str, str]:
    """Build Anvil's environment without forwarding sensitive auth variables."""
    sensitive_markers = ("TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")
    return {
        key: value
        for key, value in os.environ.items()
        if not any(marker in key.upper() for marker in sensitive_markers)
    }


def resolve_anvil_binary(
    *,
    version: str | None = None,
    override: Path | None = None,
    prefer_local: bool = False,
) -> Path:
    """Resolve the Anvil binary path.

    Order: override > matching $PATH binary > matching cached release > downloaded
    release. When ``version`` is omitted, Brokk always checks GitHub for the latest
    release so newer versions are applied automatically (auto-update). ``prefer_local``
    enables graceful degradation: if the latest-release lookup or the download fails
    (e.g. offline), Brokk falls back to any already-installed local binary instead of
    erroring. Brokk only errors when no version can be resolved/downloaded *and* no
    local binary is present.
    """
    if override is not None:
        return _validate_existing_file(override, "anvil")

    normalized_version = (version or "").strip().removeprefix("v")

    # Always resolve the target version first so a newer release is picked up. When
    # no explicit version was requested this hits GitHub; if that fails (offline),
    # degrade to a local binary rather than blocking startup.
    try:
        resolved_version = _resolve_anvil_version(normalized_version or None)
    except AnvilInstallError:
        if prefer_local and not normalized_version:
            local_binary = _find_local_anvil_binary()
            if local_binary is not None:
                logger.info(
                    "Could not resolve latest Anvil release; using local binary at %s",
                    local_binary,
                )
                return local_binary
        raise

    on_path = shutil.which("anvil")
    if on_path:
        on_path_p = Path(on_path)
        if _anvil_version_matches(on_path_p, resolved_version):
            return on_path_p
        logger.info(
            "Ignoring anvil on $PATH at %s: version does not match requested %s",
            on_path_p,
            resolved_version,
        )

    binary_path = _anvil_cache_binary_path(resolved_version)
    if binary_path.exists() and os.access(binary_path, os.X_OK):
        if _anvil_version_matches(binary_path, resolved_version):
            return binary_path
        logger.info(
            "Ignoring cached anvil at %s: version does not match requested %s",
            binary_path,
            resolved_version,
        )

    # No local binary matches the target version, so download it. If the download
    # fails but a local binary exists, degrade gracefully to it instead of erroring.
    try:
        return _download_anvil(resolved_version)
    except AnvilInstallError:
        if prefer_local:
            local_binary = _find_local_anvil_binary()
            if local_binary is not None:
                logger.warning(
                    "Failed to download Anvil %s; falling back to local binary at %s",
                    resolved_version,
                    local_binary,
                )
                return local_binary
        raise


def _resolve_anvil_version(version: str | None) -> str:
    normalized_version = (version or "").strip().removeprefix("v")
    if normalized_version:
        return normalized_version

    try:
        return latest_github_release_version(_ANVIL_GITHUB_REPO)
    except ReleaseResolverError as exc:
        raise AnvilInstallError(f"Failed to resolve latest Anvil release: {exc}") from exc


def _find_local_anvil_binary() -> Path | None:
    on_path = shutil.which("anvil")
    if on_path:
        return Path(on_path)
    return _latest_cached_anvil_binary()


def _latest_cached_anvil_binary() -> Path | None:
    cache_root = get_global_cache_dir() / "anvil"
    if not cache_root.exists():
        return None

    triple = _anvil_triple("unknown")
    filename = _anvil_binary_filename()
    candidates: list[tuple[str, Path]] = []
    for version_dir in cache_root.iterdir():
        if not version_dir.is_dir():
            continue
        candidate = version_dir / triple / filename
        if candidate.exists() and os.access(candidate, os.X_OK):
            candidates.append((version_dir.name, candidate))

    if not candidates:
        return None

    candidates.sort(key=lambda item: _version_sort_key(item[0]), reverse=True)
    return candidates[0][1]


def _version_sort_key(version: str) -> tuple[tuple[int, int | str], ...]:
    tokens = [token for token in re.split(r"[^0-9A-Za-z]+", version) if token]
    if not tokens:
        return ((1, version.lower()),)
    return tuple((0, int(token)) if token.isdigit() else (1, token.lower()) for token in tokens)


def _validate_existing_file(path: Path, name: str) -> Path:
    if not path.exists():
        raise AnvilInstallError(f"{name} binary not found at {path}.")
    if not path.is_file():
        raise AnvilInstallError(f"{name} path {path} is not a regular file.")
    return path.resolve()


def _anvil_version_matches(binary_path: Path, expected_version: str) -> bool:
    try:
        proc = subprocess.run(
            [str(binary_path), "--version"],
            capture_output=True,
            text=True,
            timeout=_ANVIL_VERSION_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.debug("Failed to probe %s --version: %s", binary_path, exc)
        return False

    if proc.returncode != 0:
        logger.debug(
            "%s --version exited %d; stderr=%r",
            binary_path,
            proc.returncode,
            proc.stderr,
        )
        return False

    lines = (proc.stdout or "").strip().splitlines()
    if not lines:
        return False
    tokens = lines[0].split()
    return len(tokens) >= 2 and tokens[0] == "anvil" and tokens[1] == expected_version


def _anvil_triple(version: str) -> str:
    system = platform.system()
    machine = platform.machine().lower()
    if system == "Darwin":
        return "universal-apple-darwin"
    if system == "Android":
        return "aarch64-linux-android"
    if system == "Linux":
        if machine in ("x86_64", "amd64"):
            return "x86_64-unknown-linux-gnu"
        if machine in ("aarch64", "arm64"):
            return "aarch64-unknown-linux-gnu"
        raise AnvilInstallError(f"Unsupported Linux machine: {machine}")
    if system == "Windows":
        if machine in ("amd64", "x86_64"):
            return "x86_64-pc-windows-msvc"
        raise AnvilInstallError(f"anvil v{version} does not ship a Windows binary for {machine}.")
    raise AnvilInstallError(f"Unsupported system: {system} {machine}")


def _anvil_binary_filename() -> str:
    return "anvil.exe" if sys.platform == "win32" else "anvil"


def _anvil_cache_binary_path(version: str) -> Path:
    return (
        get_global_cache_dir()
        / "anvil"
        / version
        / _anvil_triple(version)
        / _anvil_binary_filename()
    )


def _anvil_archive_url(version: str, asset_name: str) -> str:
    return f"{_ANVIL_RELEASE_URL}/v{version}/{asset_name}"


@contextlib.contextmanager
def _anvil_download_lock(version: str) -> Iterator[None]:
    """File-based lock to serialize concurrent downloads of the same platform asset."""
    triple = _anvil_triple(version)
    lock_path = get_global_cache_dir() / "anvil" / f"{version}-{triple}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    deadline = time.monotonic() + _ANVIL_LOCK_TIMEOUT_SECONDS
    acquired = False
    while not acquired:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, str(os.getpid()).encode())
            finally:
                os.close(fd)
            acquired = True
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise AnvilInstallError(
                    f"Could not acquire anvil {version} download lock at {lock_path}"
                )
            time.sleep(0.5)

    try:
        yield
    finally:
        lock_path.unlink(missing_ok=True)


def _download_anvil(version: str) -> Path:
    triple = _anvil_triple(version)
    asset_name = f"brokk-anvil-v{version}-{triple}.zip"
    archive_url = _anvil_archive_url(version, asset_name)
    sha256_url = f"{archive_url}.sha256"

    target = _anvil_cache_binary_path(version)
    target.parent.mkdir(parents=True, exist_ok=True)

    with _anvil_download_lock(version):
        if target.exists() and os.access(target, os.X_OK):
            if _anvil_version_matches(target, version):
                return target
            logger.info(
                "Replacing cached anvil at %s: version does not match requested %s",
                target,
                version,
            )

        logger.info("Downloading anvil %s for %s from %s", version, triple, archive_url)
        with tempfile.TemporaryDirectory(prefix="brokk-anvil-") as tmpdir:
            tmp_path = Path(tmpdir)
            archive_path = tmp_path / asset_name
            sha_path = tmp_path / f"{asset_name}.sha256"

            try:
                _http_download(archive_url, archive_path)
                _http_download(sha256_url, sha_path)
            except httpx.HTTPError as exc:
                raise AnvilInstallError(
                    f"Failed to download anvil v{version} ({triple}): {exc}"
                ) from exc

            _verify_sha256(archive_path, sha_path)
            _extract_anvil_binary(archive_path, target)

    return target


def _http_download(url: str, target: Path) -> None:
    with httpx.stream(
        "GET",
        url,
        follow_redirects=True,
        timeout=_ANVIL_DOWNLOAD_TIMEOUT_SECONDS,
    ) as response:
        response.raise_for_status()
        with target.open("wb") as fh:
            for chunk in response.iter_bytes():
                fh.write(chunk)


def _verify_sha256(archive_path: Path, sha_path: Path) -> None:
    expected = sha_path.read_text(encoding="utf-8").strip().split()[0].lower()
    digest = hashlib.sha256()
    with archive_path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    actual = digest.hexdigest()
    if actual != expected:
        raise AnvilInstallError(
            f"Downloaded anvil checksum mismatch: expected {expected}, got {actual}"
        )


def _extract_anvil_binary(archive_path: Path, target: Path) -> None:
    filename = _anvil_binary_filename()
    with zipfile.ZipFile(archive_path) as archive:
        binary_info = next(
            (
                info
                for info in archive.infolist()
                if Path(info.filename).name == filename and not info.is_dir()
            ),
            None,
        )
        if binary_info is None:
            raise AnvilInstallError(f"Archive did not contain {filename}")

        tmp_target = target.with_suffix(f"{target.suffix}.tmp")
        with archive.open(binary_info) as src, tmp_target.open("wb") as dst:
            shutil.copyfileobj(src, dst)
        tmp_target.chmod(0o755)
        tmp_target.replace(target)
