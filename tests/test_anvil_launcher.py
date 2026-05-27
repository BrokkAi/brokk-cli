import os
import sys
from pathlib import Path

import pytest

from brokk_code import anvil_launcher, mcp_launcher


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
    monkeypatch.setattr(mcp_launcher, "git_toplevel_for", lambda _path: None)

    with pytest.raises(RuntimeError, match="stop"):
        anvil_launcher.run_anvil_acp_server(
            workspace_dir=tmp_path,
            binary_override=binary,
            passthrough_args=["--default-model", "claude-haiku-4-5"],
        )

    assert captured["cwd"] == tmp_path.resolve()
    assert captured["binary"] == str(binary)
    assert captured["command"] == [str(binary), "--default-model", "claude-haiku-4-5"]


def test_resolve_anvil_binary_prefers_override(tmp_path) -> None:
    binary = tmp_path / "custom-anvil"
    binary.write_text("")

    resolved = anvil_launcher.resolve_anvil_binary(override=binary)

    assert resolved == binary


def test_resolve_anvil_binary_uses_path_when_version_matches(monkeypatch, tmp_path) -> None:
    found = tmp_path / "anvil"
    found.write_text("")
    monkeypatch.setattr(anvil_launcher.shutil, "which", lambda _name: str(found))
    monkeypatch.setattr(anvil_launcher, "_anvil_version_matches", lambda _path, _version: True)

    resolved = anvil_launcher.resolve_anvil_binary(override=None)

    assert resolved == found


def test_resolve_anvil_binary_skips_path_on_version_mismatch(monkeypatch, tmp_path) -> None:
    cache_root = tmp_path / "cache"

    def fake_cache_path(version: str) -> Path:
        triple_dir = cache_root / "anvil" / version / "fake-triple"
        triple_dir.mkdir(parents=True, exist_ok=True)
        return triple_dir / "anvil"

    on_path = tmp_path / "stale" / "anvil"
    on_path.parent.mkdir()
    on_path.write_text("")

    monkeypatch.setattr(anvil_launcher.shutil, "which", lambda _name: str(on_path))
    monkeypatch.setattr(anvil_launcher, "_anvil_version_matches", lambda _path, _version: False)
    monkeypatch.setattr(anvil_launcher, "_anvil_cache_binary_path", fake_cache_path)

    cached = fake_cache_path(anvil_launcher.BUNDLED_ANVIL_VERSION)
    cached.write_text("stub")
    cached.chmod(0o755)

    def fail_download(_version: str) -> Path:
        raise AssertionError("must not download when cache hit is available")

    monkeypatch.setattr(anvil_launcher, "_download_anvil", fail_download)

    resolved = anvil_launcher.resolve_anvil_binary(override=None)

    assert resolved == cached


def test_anvil_version_matches_parses_expected_format(monkeypatch, tmp_path) -> None:
    binary = tmp_path / "anvil"
    binary.write_text("")

    class FakeCompleted:
        returncode = 0
        stdout = "anvil 0.8.1\n"
        stderr = ""

    monkeypatch.setattr(anvil_launcher.subprocess, "run", lambda *_a, **_k: FakeCompleted())

    assert anvil_launcher._anvil_version_matches(binary, "0.8.1") is True
    assert anvil_launcher._anvil_version_matches(binary, "0.8.0") is False
