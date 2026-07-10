import hashlib
import io
import os
import sys
import tarfile
from pathlib import Path

import pytest

from brokk_code import bifrost_launcher, rust_acp_install


def test_run_bifrost_server_uses_override_and_execs_bifrost(monkeypatch, tmp_path) -> None:
    captured: dict[str, object] = {}

    binary = tmp_path / "bifrost"
    binary.write_text("stub")
    binary.chmod(0o755)

    def fake_chdir(path: Path) -> None:
        captured["cwd"] = path

    def fake_execvpe(binary_arg: str, command: list[str], env: dict[str, str]) -> None:
        captured["binary"] = binary_arg
        captured["command"] = command
        raise RuntimeError("stop")

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(os, "chdir", fake_chdir)
    monkeypatch.setattr(os, "execvpe", fake_execvpe)
    monkeypatch.setattr(bifrost_launcher, "resolve_bifrost_binary", lambda **_kwargs: binary)
    with pytest.raises(RuntimeError, match="stop"):
        bifrost_launcher.run_bifrost_server(
            workspace_dir=tmp_path,
        )

    assert captured["cwd"] == tmp_path.resolve()
    assert captured["binary"] == str(binary)
    assert captured["command"] == [str(binary)]


def test_run_bifrost_server_prefers_local_binary_when_version_omitted(
    monkeypatch, tmp_path
) -> None:
    captured: dict[str, object] = {}

    binary = tmp_path / "bifrost"
    binary.write_text("stub")
    binary.chmod(0o755)

    def fake_resolve(**kwargs: object) -> Path:
        captured["resolve_kwargs"] = kwargs
        return binary

    monkeypatch.setattr(bifrost_launcher, "resolve_bifrost_binary", fake_resolve)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(os, "chdir", lambda _p: None)
    monkeypatch.setattr(os, "execvpe", lambda *_a: (_ for _ in ()).throw(RuntimeError("stop")))

    with pytest.raises(RuntimeError, match="stop"):
        bifrost_launcher.run_bifrost_server(workspace_dir=tmp_path)

    assert captured["resolve_kwargs"] == {"version": None, "override": None, "prefer_local": True}


def test_run_bifrost_server_uses_explicit_version_without_local_fallback(
    monkeypatch, tmp_path
) -> None:
    captured: dict[str, object] = {}

    binary = tmp_path / "bifrost"
    binary.write_text("stub")
    binary.chmod(0o755)

    def fake_resolve(**kwargs: object) -> Path:
        captured["resolve_kwargs"] = kwargs
        return binary

    monkeypatch.setattr(bifrost_launcher, "resolve_bifrost_binary", fake_resolve)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(os, "chdir", lambda _p: None)
    monkeypatch.setattr(os, "execvpe", lambda *_a: (_ for _ in ()).throw(RuntimeError("stop")))

    with pytest.raises(RuntimeError, match="stop"):
        bifrost_launcher.run_bifrost_server(workspace_dir=tmp_path, version="0.7.2")

    assert captured["resolve_kwargs"] == {
        "version": "0.7.2",
        "override": None,
        "prefer_local": False,
    }


def test_run_bifrost_server_forwards_passthrough_args(monkeypatch, tmp_path) -> None:
    captured: dict[str, object] = {}

    binary = tmp_path / "bifrost"
    binary.write_text("stub")
    binary.chmod(0o755)

    def fake_execvpe(_b: str, command: list[str], _env: dict[str, str]) -> None:
        captured["command"] = command
        raise RuntimeError("stop")

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(os, "chdir", lambda _p: None)
    monkeypatch.setattr(os, "execvpe", fake_execvpe)
    monkeypatch.setattr(bifrost_launcher, "resolve_bifrost_binary", lambda **_kwargs: binary)
    with pytest.raises(RuntimeError, match="stop"):
        bifrost_launcher.run_bifrost_server(
            workspace_dir=tmp_path,
            passthrough_args=["--debug", "--log-level", "trace"],
        )

    assert captured["command"] == [str(binary), "--debug", "--log-level", "trace"]


def test_run_bifrost_server_reports_install_error(monkeypatch, tmp_path, capsys) -> None:
    def fake_resolve(**_kwargs: object) -> Path:
        raise rust_acp_install.BifrostInstallError("no asset for platform")

    monkeypatch.setattr(bifrost_launcher, "resolve_bifrost_binary", fake_resolve)
    with pytest.raises(SystemExit) as excinfo:
        bifrost_launcher.run_bifrost_server(
            workspace_dir=tmp_path,
        )

    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert "no asset for platform" in err


def test_resolve_bifrost_binary_prefers_override(tmp_path) -> None:
    binary = tmp_path / "custom-bifrost"
    binary.write_text("")
    resolved = rust_acp_install.resolve_bifrost_binary(override=binary)
    assert resolved == binary


def test_resolve_bifrost_binary_uses_pinned_release_when_version_omitted(
    monkeypatch, tmp_path
) -> None:
    resolved_binary = tmp_path / "downloaded-bifrost"
    resolved_binary.write_text("")

    monkeypatch.setattr(rust_acp_install.shutil, "which", lambda _name: None)

    captured: dict[str, str] = {}

    def fake_download(version: str) -> Path:
        captured["version"] = version
        return resolved_binary

    monkeypatch.setattr(rust_acp_install, "_download_bifrost", fake_download)

    resolved = rust_acp_install.resolve_bifrost_binary(override=None)

    assert resolved == resolved_binary
    assert captured["version"] == rust_acp_install._BIFROST_PINNED_VERSION
    assert captured["version"] == "0.7.4"


# ---------------------------------------------------------------------------
# Pinning contract. `brokk mcp` resolves bifrost with prefer_local=True and no
# explicit version:
#   1. pinned release not local  -> pinned release downloaded and used
#   2. pinned release local      -> existing local binary used, nothing downloaded
#   3. pinned download fails (offline) -> degrade to local binary, no error
#   4. nothing reachable AND no local binary -> error
# ---------------------------------------------------------------------------


def test_contract1_downloads_pinned_release_when_local_binary_mismatches(
    monkeypatch, tmp_path
) -> None:
    """A local binary that does not match the pinned release triggers a download."""
    stale_local = tmp_path / "stale" / "bifrost"
    stale_local.parent.mkdir()
    stale_local.write_text("old")
    stale_local.chmod(0o755)

    downloaded = tmp_path / "downloaded" / "bifrost"
    downloaded.parent.mkdir()
    downloaded.write_text("new")
    downloaded.chmod(0o755)

    monkeypatch.setattr(rust_acp_install.shutil, "which", lambda _name: str(stale_local))
    monkeypatch.setattr(rust_acp_install, "_bifrost_version_matches", lambda _path, _version: False)
    # No cached binary for the resolved version.
    monkeypatch.setattr(
        rust_acp_install, "_bifrost_cache_binary_path", lambda _version: tmp_path / "missing"
    )

    captured: dict[str, str] = {}

    def fake_download(version: str) -> Path:
        captured["version"] = version
        return downloaded

    monkeypatch.setattr(rust_acp_install, "_download_bifrost", fake_download)

    resolved = rust_acp_install.resolve_bifrost_binary(override=None, prefer_local=True)

    assert resolved == downloaded
    assert captured["version"] == rust_acp_install._BIFROST_PINNED_VERSION


def test_contract2_uses_local_when_it_matches_pinned_release(monkeypatch, tmp_path) -> None:
    """When the local binary already matches the pinned release, nothing is downloaded."""
    local = tmp_path / "bifrost"
    local.write_text("current")
    local.chmod(0o755)

    monkeypatch.setattr(rust_acp_install.shutil, "which", lambda _name: str(local))
    monkeypatch.setattr(rust_acp_install, "_bifrost_version_matches", lambda _path, _version: True)
    monkeypatch.setattr(
        rust_acp_install,
        "_download_bifrost",
        lambda _version: (_ for _ in ()).throw(AssertionError("must not download")),
    )

    resolved = rust_acp_install.resolve_bifrost_binary(override=None, prefer_local=True)

    assert resolved == local


def test_contract3_falls_back_to_path_binary_when_pinned_download_fails(
    monkeypatch, tmp_path
) -> None:
    """Pinned download failure degrades to the local $PATH binary, no error."""
    found = tmp_path / "bifrost"
    found.write_text("")

    monkeypatch.setattr(rust_acp_install.shutil, "which", lambda _name: str(found))
    monkeypatch.setattr(rust_acp_install, "_bifrost_version_matches", lambda _path, _version: False)
    monkeypatch.setattr(
        rust_acp_install, "_bifrost_cache_binary_path", lambda _version: tmp_path / "missing"
    )
    monkeypatch.setattr(
        rust_acp_install,
        "_download_bifrost",
        lambda _version: (_ for _ in ()).throw(
            rust_acp_install.BifrostInstallError("network down")
        ),
    )

    resolved = rust_acp_install.resolve_bifrost_binary(override=None, prefer_local=True)

    assert resolved == found


def test_contract3_falls_back_to_latest_cached_binary_when_pinned_download_fails(
    monkeypatch, tmp_path
) -> None:
    """With no $PATH binary, offline startup degrades to the newest cached binary."""
    cache_root = tmp_path / "cache"
    monkeypatch.setattr(rust_acp_install.shutil, "which", lambda _name: None)
    monkeypatch.setattr("brokk_code.rust_acp_install.get_global_cache_dir", lambda: cache_root)
    monkeypatch.setattr(rust_acp_install, "_bifrost_triple", lambda _version: "fake-triple")
    monkeypatch.setattr(
        rust_acp_install,
        "_bifrost_cache_binary_path",
        lambda _version: cache_root / "bifrost" / _version / "fake-triple" / "bifrost",
    )
    monkeypatch.setattr(
        rust_acp_install,
        "_download_bifrost",
        lambda _version: (_ for _ in ()).throw(
            rust_acp_install.BifrostInstallError("network down")
        ),
    )

    binary_name = rust_acp_install._bifrost_binary_filename()

    older = cache_root / "bifrost" / "1.2.3" / "fake-triple" / binary_name
    older.parent.mkdir(parents=True)
    older.write_text("old")
    older.chmod(0o755)

    newer = cache_root / "bifrost" / "1.10.0" / "fake-triple" / binary_name
    newer.parent.mkdir(parents=True)
    newer.write_text("new")
    newer.chmod(0o755)

    resolved = rust_acp_install.resolve_bifrost_binary(override=None, prefer_local=True)

    assert resolved == newer


def test_contract3_falls_back_to_local_when_download_fails(monkeypatch, tmp_path) -> None:
    """A failed download of the pinned release degrades to the existing local binary."""
    local = tmp_path / "bifrost"
    local.write_text("old")
    local.chmod(0o755)

    monkeypatch.setattr(rust_acp_install.shutil, "which", lambda _name: str(local))
    monkeypatch.setattr(rust_acp_install, "_bifrost_version_matches", lambda _path, _version: False)
    monkeypatch.setattr(
        rust_acp_install, "_bifrost_cache_binary_path", lambda _version: tmp_path / "missing"
    )
    monkeypatch.setattr(
        rust_acp_install,
        "_download_bifrost",
        lambda _version: (_ for _ in ()).throw(
            rust_acp_install.BifrostInstallError("network down")
        ),
    )

    resolved = rust_acp_install.resolve_bifrost_binary(override=None, prefer_local=True)

    assert resolved == local


def test_contract4_errors_when_nothing_reachable_and_no_local_binary(monkeypatch, tmp_path) -> None:
    """No reachable pinned download and no local binary is the only case that errors."""
    cache_root = tmp_path / "cache"
    monkeypatch.setattr(rust_acp_install.shutil, "which", lambda _name: None)
    monkeypatch.setattr("brokk_code.rust_acp_install.get_global_cache_dir", lambda: cache_root)
    monkeypatch.setattr(rust_acp_install, "_bifrost_triple", lambda _version: "fake-triple")
    monkeypatch.setattr(
        rust_acp_install,
        "_download_bifrost",
        lambda _version: (_ for _ in ()).throw(
            rust_acp_install.BifrostInstallError("network down")
        ),
    )

    with pytest.raises(rust_acp_install.BifrostInstallError):
        rust_acp_install.resolve_bifrost_binary(override=None, prefer_local=True)


def test_resolve_bifrost_binary_uses_path_when_available(monkeypatch, tmp_path) -> None:
    found = tmp_path / "bifrost"
    found.write_text("")
    monkeypatch.setattr(rust_acp_install.shutil, "which", lambda _name: str(found))
    monkeypatch.setattr(
        rust_acp_install,
        "_bifrost_version_matches",
        lambda _path, _version: True,
    )
    resolved = rust_acp_install.resolve_bifrost_binary(override=None)
    assert resolved == found


def test_resolve_bifrost_binary_skips_path_on_version_mismatch(monkeypatch, tmp_path) -> None:
    """A `$PATH` bifrost with a non-matching version is ignored; cache is used instead."""
    cache_root = tmp_path / "cache"

    def fake_cache_path(version: str) -> Path:
        triple_dir = cache_root / "bifrost" / version / "fake-triple"
        triple_dir.mkdir(parents=True, exist_ok=True)
        return triple_dir / "bifrost"

    on_path = tmp_path / "stale" / "bifrost"
    on_path.parent.mkdir()
    on_path.write_text("")

    monkeypatch.setattr(rust_acp_install.shutil, "which", lambda _name: str(on_path))
    monkeypatch.setattr(
        rust_acp_install,
        "_bifrost_version_matches",
        lambda _path, _version: False,
    )
    monkeypatch.setattr(rust_acp_install, "_bifrost_cache_binary_path", fake_cache_path)

    cached = fake_cache_path("8.8.8")
    cached.write_text("stub")
    cached.chmod(0o755)

    def fail_download(_version: str) -> Path:
        raise AssertionError("must not download when cache hit is available")

    monkeypatch.setattr(rust_acp_install, "_download_bifrost", fail_download)

    resolved = rust_acp_install.resolve_bifrost_binary(version="8.8.8", override=None)
    assert resolved == cached


def test_bifrost_version_matches_parses_expected_format(monkeypatch, tmp_path) -> None:
    binary = tmp_path / "bifrost"
    binary.write_text("")

    class FakeCompleted:
        returncode = 0
        stdout = "bifrost 0.1.2\n"
        stderr = ""

    def fake_run(*_args: object, **_kwargs: object) -> FakeCompleted:
        return FakeCompleted()

    monkeypatch.setattr(rust_acp_install.subprocess, "run", fake_run)

    assert rust_acp_install._bifrost_version_matches(binary, "0.1.2") is True
    assert rust_acp_install._bifrost_version_matches(binary, "0.1.1") is False


def test_bifrost_version_matches_rejects_unrelated_tool(monkeypatch, tmp_path) -> None:
    binary = tmp_path / "bifrost"
    binary.write_text("")

    class FakeCompleted:
        returncode = 0
        stdout = "some-other-tool 0.1.2\n"
        stderr = ""

    monkeypatch.setattr(rust_acp_install.subprocess, "run", lambda *_a, **_k: FakeCompleted())
    assert rust_acp_install._bifrost_version_matches(binary, "0.1.2") is False


def test_bifrost_version_matches_handles_failures(monkeypatch, tmp_path) -> None:
    binary = tmp_path / "bifrost"
    binary.write_text("")

    def raising_run(*_a: object, **_k: object) -> object:
        raise OSError("boom")

    monkeypatch.setattr(rust_acp_install.subprocess, "run", raising_run)
    assert rust_acp_install._bifrost_version_matches(binary, "0.1.2") is False

    class NonZero:
        returncode = 1
        stdout = ""
        stderr = "nope"

    monkeypatch.setattr(rust_acp_install.subprocess, "run", lambda *_a, **_k: NonZero())
    assert rust_acp_install._bifrost_version_matches(binary, "0.1.2") is False


def test_resolve_bifrost_binary_uses_cache_without_download(monkeypatch, tmp_path) -> None:
    cache_root = tmp_path / "cache"

    def fake_cache_path(version: str) -> Path:
        triple_dir = cache_root / "bifrost" / version / "fake-triple"
        triple_dir.mkdir(parents=True, exist_ok=True)
        return triple_dir / "bifrost"

    monkeypatch.setattr(rust_acp_install.shutil, "which", lambda _n: None)
    monkeypatch.setattr(rust_acp_install, "_bifrost_cache_binary_path", fake_cache_path)

    cached = fake_cache_path("8.8.8")
    cached.write_text("stub")
    cached.chmod(0o755)

    def fail_download(_version: str) -> Path:
        raise AssertionError("must not download when cache hit is available")

    monkeypatch.setattr(rust_acp_install, "_download_bifrost", fail_download)

    resolved = rust_acp_install.resolve_bifrost_binary(version="8.8.8", override=None)
    assert resolved == cached


def test_bifrost_triple_maps_known_platforms(monkeypatch) -> None:
    cases = [
        ("Darwin", "arm64", "universal-apple-darwin"),
        ("Darwin", "x86_64", "universal-apple-darwin"),
        ("Linux", "x86_64", "x86_64-unknown-linux-gnu"),
        ("Linux", "aarch64", "aarch64-unknown-linux-gnu"),
        ("Windows", "AMD64", "x86_64-pc-windows-msvc"),
        ("Windows", "ARM64", "aarch64-pc-windows-msvc"),
    ]
    for system, machine, expected in cases:
        monkeypatch.setattr(rust_acp_install.platform, "system", lambda s=system: s)
        monkeypatch.setattr(rust_acp_install.platform, "machine", lambda m=machine: m)
        assert rust_acp_install._bifrost_triple("8.8.8") == expected


def test_bifrost_triple_no_longer_rejects_intel_mac(monkeypatch) -> None:
    monkeypatch.setattr(rust_acp_install.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(rust_acp_install.platform, "machine", lambda: "x86_64")

    assert rust_acp_install._bifrost_triple("8.8.8") == "universal-apple-darwin"


def test_parse_sha256_handles_both_formats(tmp_path) -> None:
    digest = "a" * 64

    just_hash = tmp_path / "a.sha256"
    just_hash.write_text(digest + "\n")
    assert rust_acp_install._parse_sha256_file(just_hash, "a") == digest

    hash_with_name = tmp_path / "b.sha256"
    hash_with_name.write_text(f"{digest}  payload.tar.gz\n")
    assert rust_acp_install._parse_sha256_file(hash_with_name, "payload.tar.gz") == digest


def test_parse_sha256_rejects_malformed(tmp_path) -> None:
    bad = tmp_path / "bad.sha256"
    bad.write_text("not-a-hash\n")
    with pytest.raises(rust_acp_install.BifrostInstallError, match="Malformed sha256"):
        rust_acp_install._parse_sha256_file(bad, "asset")


def test_download_bifrost_extracts_and_caches(monkeypatch, tmp_path) -> None:
    """End-to-end exercise of _download_bifrost with a stubbed httpx layer."""
    cache_root = tmp_path / "cache"
    monkeypatch.setattr("brokk_code.rust_acp_install.get_global_cache_dir", lambda: cache_root)
    monkeypatch.setattr(rust_acp_install.platform, "system", lambda: "Linux")
    monkeypatch.setattr(rust_acp_install.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(sys, "platform", "linux")

    # Build a tar.gz containing a single 'bifrost' file at the root.
    archive_buffer = io.BytesIO()
    with tarfile.open(fileobj=archive_buffer, mode="w:gz") as tf:
        payload = b"#!/bin/sh\necho bifrost-stub\n"
        info = tarfile.TarInfo(name="bifrost")
        info.size = len(payload)
        info.mode = 0o755
        tf.addfile(info, io.BytesIO(payload))
    archive_bytes = archive_buffer.getvalue()
    archive_sha = hashlib.sha256(archive_bytes).hexdigest()

    def fake_http_download(url: str, dest: Path) -> None:
        if url.endswith(".sha256"):
            dest.write_text(f"{archive_sha}  bifrost.tar.gz\n")
        else:
            dest.write_bytes(archive_bytes)

    monkeypatch.setattr(rust_acp_install, "_http_download", fake_http_download)

    resolved = rust_acp_install._download_bifrost("8.8.8")

    assert resolved.exists()
    assert resolved.name == "bifrost"
    assert os.access(resolved, os.X_OK)
    assert "8.8.8" in resolved.parts and "x86_64-unknown-linux-gnu" in resolved.parts
