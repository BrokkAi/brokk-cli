import httpx
import pytest

from brokk_code import release_resolver


class _FakeResponse:
    def __init__(self, *, payload=None, json_error: Exception | None = None) -> None:
        self._payload = payload
        self._json_error = json_error

    def raise_for_status(self) -> None:
        return None

    def json(self):
        if self._json_error is not None:
            raise self._json_error
        return self._payload


def test_latest_github_release_version_strips_v_prefix(monkeypatch) -> None:
    release_resolver.latest_github_release_version.cache_clear()
    monkeypatch.setattr(
        release_resolver.httpx,
        "get",
        lambda *_a, **_k: _FakeResponse(payload={"tag_name": "v1.2.3"}),
    )

    assert release_resolver.latest_github_release_version("BrokkAi/anvil") == "1.2.3"


def test_latest_github_release_version_rejects_missing_tag_name(monkeypatch) -> None:
    release_resolver.latest_github_release_version.cache_clear()
    monkeypatch.setattr(
        release_resolver.httpx,
        "get",
        lambda *_a, **_k: _FakeResponse(payload={}),
    )

    with pytest.raises(release_resolver.ReleaseResolverError, match="tag_name"):
        release_resolver.latest_github_release_version("BrokkAi/anvil")


def test_latest_github_release_version_wraps_http_errors(monkeypatch) -> None:
    release_resolver.latest_github_release_version.cache_clear()

    def fake_get(*_a, **_k):
        raise httpx.HTTPError("boom")

    monkeypatch.setattr(release_resolver.httpx, "get", fake_get)

    with pytest.raises(release_resolver.ReleaseResolverError, match="failed to fetch"):
        release_resolver.latest_github_release_version("BrokkAi/anvil")
