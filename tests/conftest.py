from pathlib import Path
import urllib.request

import httpx
import pytest


@pytest.fixture(autouse=True)
def isolate_home(tmp_path, monkeypatch):
    """Prevent tests from reading/writing real ~/.brokk state and current directory."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.setenv("HOME", str(tmp_path))

    # Provide a fake API key so tests that don't test the API key modal don't trigger it.
    # monkeypatch reverts this after each test, so the real env var is never touched.
    monkeypatch.setenv("BROKK_API_KEY", "test-key")

    # Isolate CWD to prevent accidental writes to the repo root
    cwd = tmp_path / "cwd"
    cwd.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(cwd)


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    """Fail fast on unmocked outbound network access in tests."""

    def _blocked(*_args, **_kwargs):
        raise AssertionError("Tests must not perform outbound network calls. Mock the request.")

    monkeypatch.setattr(httpx, "get", _blocked)
    monkeypatch.setattr(httpx, "request", _blocked)
    monkeypatch.setattr(httpx.Client, "request", _blocked)
    monkeypatch.setattr(urllib.request, "urlopen", _blocked)
