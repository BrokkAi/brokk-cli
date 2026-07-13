import contextlib
import os
import sys
from pathlib import Path

import pytest

from brokk_code import anvil_launcher


def test_run_anvil_acp_server_uses_override_and_execs_binary(monkeypatch, tmp_path) -> None:
    captured: dict[str, object] = {}

    binary = tmp_path / "anvil"
    binary.write_text("stub")
    binary.chmod(0o755)

    def fake_chdir(path: Path) -> None:
        captured["cwd"] = path

    def fake_execvpe(binary_arg: str, command: list[str], env: dict[str, str]) -> None:
        captured["binary"] = binary_arg
        captured["command"] = command
        captured["env"] = env
        raise RuntimeError("stop")

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(os, "chdir", fake_chdir)
    monkeypatch.setattr(os, "execvpe", fake_execvpe)
    monkeypatch.setenv("BROKK_TEST_SECRET", "secret")
    with pytest.raises(RuntimeError, match="stop"):
        anvil_launcher.run_anvil_acp_server(
            workspace_dir=tmp_path,
            binary_override=binary,
            passthrough_args=["--default-model", "claude-haiku-4-5"],
        )

    assert captured["cwd"] == tmp_path.resolve()
    assert captured["binary"] == str(binary)
    assert captured["command"] == [str(binary), "--default-model", "claude-haiku-4-5"]
    assert "--transient-setup" not in captured["command"]
    assert "BROKK_TEST_SECRET" not in captured["env"]


def test_run_anvil_acp_server_prefers_local_binary_when_version_omitted(
    monkeypatch, tmp_path
) -> None:
    captured: dict[str, object] = {}

    binary = tmp_path / "anvil"
    binary.write_text("stub")
    binary.chmod(0o755)

    def fake_resolve(**kwargs: object) -> Path:
        captured["resolve_kwargs"] = kwargs
        return binary

    monkeypatch.setattr(anvil_launcher, "resolve_anvil_binary", fake_resolve)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(os, "chdir", lambda _path: None)
    monkeypatch.setattr(os, "execvpe", lambda *_args: (_ for _ in ()).throw(RuntimeError("stop")))

    with pytest.raises(RuntimeError, match="stop"):
        anvil_launcher.run_anvil_acp_server(workspace_dir=tmp_path)

    assert captured["resolve_kwargs"] == {
        "version": None,
        "override": None,
        "prefer_local": True,
    }


def test_resolve_anvil_binary_prefers_override(tmp_path) -> None:
    binary = tmp_path / "custom-anvil"
    binary.write_text("")

    resolved = anvil_launcher.resolve_anvil_binary(override=binary)

    assert resolved == binary


def test_resolve_anvil_binary_uses_pinned_release_when_version_omitted(
    monkeypatch, tmp_path
) -> None:
    resolved_binary = tmp_path / "downloaded-anvil"
    resolved_binary.write_text("")

    monkeypatch.setattr(anvil_launcher.shutil, "which", lambda _name: None)

    captured: dict[str, str] = {}

    def fake_download(version: str) -> Path:
        captured["version"] = version
        return resolved_binary

    monkeypatch.setattr(anvil_launcher, "_download_anvil", fake_download)

    resolved = anvil_launcher.resolve_anvil_binary(override=None)

    assert resolved == resolved_binary
    assert captured["version"] == anvil_launcher._ANVIL_PINNED_VERSION
    assert captured["version"] == "0.21.2"


# ---------------------------------------------------------------------------
# Pinning contract: with no explicit version (the normal ACP/headless path)
#   1. pinned release not local  -> pinned release is downloaded and used
#   2. pinned release local      -> existing local binary is used, nothing downloaded
#   3. pinned download fails (offline) -> degrade to local binary, no error
#   4. nothing reachable AND no local binary -> error
# `prefer_local=True` is what run_anvil_acp_server passes when version is None.
# ---------------------------------------------------------------------------


def test_contract1_downloads_pinned_release_when_local_binary_mismatches(
    monkeypatch, tmp_path
) -> None:
    """A local binary that does not match the pinned release triggers a download."""
    stale_local = tmp_path / "stale" / "anvil"
    stale_local.parent.mkdir()
    stale_local.write_text("old")
    stale_local.chmod(0o755)

    downloaded = tmp_path / "downloaded" / "anvil"
    downloaded.parent.mkdir()
    downloaded.write_text("new")
    downloaded.chmod(0o755)

    monkeypatch.setattr(anvil_launcher.shutil, "which", lambda _name: str(stale_local))
    # The on-$PATH binary is a different version, so it does not match the pinned version.
    monkeypatch.setattr(anvil_launcher, "_anvil_version_matches", lambda _path, _version: False)

    captured: dict[str, str] = {}

    def fake_download(version: str) -> Path:
        captured["version"] = version
        return downloaded

    monkeypatch.setattr(anvil_launcher, "_download_anvil", fake_download)

    resolved = anvil_launcher.resolve_anvil_binary(override=None, prefer_local=True)

    assert resolved == downloaded
    assert captured["version"] == anvil_launcher._ANVIL_PINNED_VERSION


def test_contract2_uses_local_when_it_matches_pinned_release(monkeypatch, tmp_path) -> None:
    """When the local binary already matches the pinned release, nothing is downloaded."""
    local = tmp_path / "anvil"
    local.write_text("current")
    local.chmod(0o755)

    monkeypatch.setattr(anvil_launcher.shutil, "which", lambda _name: str(local))
    monkeypatch.setattr(anvil_launcher, "_anvil_version_matches", lambda _path, _version: True)
    monkeypatch.setattr(
        anvil_launcher,
        "_download_anvil",
        lambda _version: (_ for _ in ()).throw(AssertionError("must not download")),
    )

    resolved = anvil_launcher.resolve_anvil_binary(override=None, prefer_local=True)

    assert resolved == local


def test_contract3_falls_back_to_path_binary_when_pinned_download_fails(
    monkeypatch, tmp_path
) -> None:
    """Pinned download failure degrades to the local $PATH binary, no error."""
    found = tmp_path / "anvil"
    found.write_text("")

    monkeypatch.setattr(anvil_launcher.shutil, "which", lambda _name: str(found))
    monkeypatch.setattr(anvil_launcher, "_anvil_version_matches", lambda _path, _version: False)
    monkeypatch.setattr(
        anvil_launcher,
        "_download_anvil",
        lambda _version: (_ for _ in ()).throw(anvil_launcher.AnvilInstallError("network down")),
    )

    resolved = anvil_launcher.resolve_anvil_binary(override=None, prefer_local=True)

    assert resolved == found


def test_contract3_falls_back_to_latest_cached_binary_when_pinned_download_fails(
    monkeypatch, tmp_path
) -> None:
    """With no $PATH binary, offline startup degrades to the newest cached binary."""
    cache_root = tmp_path / "cache"
    monkeypatch.setattr(anvil_launcher.shutil, "which", lambda _name: None)
    monkeypatch.setattr("brokk_code.anvil_launcher.get_global_cache_dir", lambda: cache_root)
    monkeypatch.setattr(anvil_launcher, "_anvil_triple", lambda _version: "fake-triple")
    monkeypatch.setattr(
        anvil_launcher,
        "_anvil_cache_binary_path",
        lambda _version: cache_root / "anvil" / _version / "fake-triple" / "anvil",
    )
    monkeypatch.setattr(
        anvil_launcher,
        "_download_anvil",
        lambda _version: (_ for _ in ()).throw(anvil_launcher.AnvilInstallError("network down")),
    )

    binary_name = anvil_launcher._anvil_binary_filename()

    older = cache_root / "anvil" / "1.2.3" / "fake-triple" / binary_name
    older.parent.mkdir(parents=True)
    older.write_text("old")
    older.chmod(0o755)

    newer = cache_root / "anvil" / "1.10.0" / "fake-triple" / binary_name
    newer.parent.mkdir(parents=True)
    newer.write_text("new")
    newer.chmod(0o755)

    resolved = anvil_launcher.resolve_anvil_binary(override=None, prefer_local=True)

    assert resolved == newer


def test_contract3_falls_back_to_local_when_download_fails(monkeypatch, tmp_path) -> None:
    """A failed download of the pinned release degrades to the existing local binary."""
    local = tmp_path / "anvil"
    local.write_text("old")
    local.chmod(0o755)

    monkeypatch.setattr(anvil_launcher.shutil, "which", lambda _name: str(local))
    monkeypatch.setattr(anvil_launcher, "_anvil_version_matches", lambda _path, _version: False)
    monkeypatch.setattr(
        anvil_launcher,
        "_download_anvil",
        lambda _version: (_ for _ in ()).throw(anvil_launcher.AnvilInstallError("network down")),
    )

    resolved = anvil_launcher.resolve_anvil_binary(override=None, prefer_local=True)

    assert resolved == local


def test_contract4_errors_when_nothing_reachable_and_no_local_binary(monkeypatch, tmp_path) -> None:
    """No reachable pinned download and no local binary is the only case that errors."""
    cache_root = tmp_path / "cache"
    monkeypatch.setattr(anvil_launcher.shutil, "which", lambda _name: None)
    monkeypatch.setattr("brokk_code.anvil_launcher.get_global_cache_dir", lambda: cache_root)
    monkeypatch.setattr(anvil_launcher, "_anvil_triple", lambda _version: "fake-triple")
    monkeypatch.setattr(
        anvil_launcher,
        "_download_anvil",
        lambda _version: (_ for _ in ()).throw(anvil_launcher.AnvilInstallError("network down")),
    )

    with pytest.raises(anvil_launcher.AnvilInstallError):
        anvil_launcher.resolve_anvil_binary(override=None, prefer_local=True)


def test_resolve_anvil_binary_redownloads_invalid_cached_binary(monkeypatch, tmp_path) -> None:
    cached = tmp_path / "cache" / "anvil"
    cached.parent.mkdir(parents=True)
    cached.write_text("corrupted")
    cached.chmod(0o755)

    replacement = tmp_path / "downloaded" / "anvil"
    replacement.parent.mkdir()
    replacement.write_text("replacement")
    replacement.chmod(0o755)

    monkeypatch.setattr(anvil_launcher.shutil, "which", lambda _name: None)
    monkeypatch.setattr(anvil_launcher, "_anvil_cache_binary_path", lambda _version: cached)
    monkeypatch.setattr(anvil_launcher, "_anvil_version_matches", lambda _path, _version: False)
    monkeypatch.setattr(anvil_launcher, "_download_anvil", lambda _version: replacement)

    resolved = anvil_launcher.resolve_anvil_binary(override=None)

    assert resolved == replacement


def test_download_anvil_redownloads_invalid_cache_after_lock(monkeypatch, tmp_path) -> None:
    target = tmp_path / "cache" / "anvil" / "0.9.2" / "fake-triple" / "anvil"
    target.parent.mkdir(parents=True)
    target.write_text("corrupted")
    target.chmod(0o755)

    monkeypatch.setattr(anvil_launcher, "_anvil_triple", lambda _version: "fake-triple")
    monkeypatch.setattr(anvil_launcher, "_anvil_cache_binary_path", lambda _version: target)
    monkeypatch.setattr(
        anvil_launcher, "_anvil_download_lock", lambda _version: contextlib.nullcontext()
    )

    version_checks: list[Path] = []

    def fake_version_matches(path: Path, _version: str) -> bool:
        version_checks.append(path)
        return False

    def fake_http_download(url: str, destination: Path) -> None:
        destination.write_text(url)

    def fake_verify_sha256(_archive_path: Path, _sha_path: Path) -> None:
        return None

    def fake_extract(_archive_path: Path, output_path: Path) -> None:
        output_path.write_text("downloaded")
        output_path.chmod(0o755)

    monkeypatch.setattr(anvil_launcher, "_anvil_version_matches", fake_version_matches)
    monkeypatch.setattr(anvil_launcher, "_http_download", fake_http_download)
    monkeypatch.setattr(anvil_launcher, "_verify_sha256", fake_verify_sha256)
    monkeypatch.setattr(anvil_launcher, "_extract_anvil_binary", fake_extract)

    resolved = anvil_launcher._download_anvil("0.9.2")

    assert resolved == target
    assert target.read_text() == "downloaded"
    assert version_checks == [target]


def test_anvil_download_lock_includes_platform_triple(monkeypatch, tmp_path) -> None:
    cache_root = tmp_path / "cache"
    monkeypatch.setattr("brokk_code.anvil_launcher.get_global_cache_dir", lambda: cache_root)
    monkeypatch.setattr(anvil_launcher, "_anvil_triple", lambda _version: "fake-triple")

    with anvil_launcher._anvil_download_lock("1.2.3"):
        lock_path = cache_root / "anvil" / "1.2.3-fake-triple.lock"
        assert lock_path.exists()
        assert not (cache_root / "anvil" / "1.2.3.lock").exists()

    assert not lock_path.exists()


def test_anvil_version_matches_parses_expected_format(monkeypatch, tmp_path) -> None:
    binary = tmp_path / "anvil"
    binary.write_text("")

    class FakeCompleted:
        returncode = 0
        stdout = "anvil 0.9.2\n"
        stderr = ""

    monkeypatch.setattr(anvil_launcher.subprocess, "run", lambda *_a, **_k: FakeCompleted())

    assert anvil_launcher._anvil_version_matches(binary, "0.9.2") is True
    assert anvil_launcher._anvil_version_matches(binary, "0.9.0") is False
