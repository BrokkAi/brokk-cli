"""Helpers for resolving latest GitHub release versions."""

from __future__ import annotations

import functools

import httpx

_GITHUB_API_BASE_URL = "https://api.github.com/repos"
_RELEASE_RESOLUTION_TIMEOUT_SECONDS = 10.0


class ReleaseResolverError(Exception):
    """Raised when a GitHub release version cannot be resolved."""


@functools.lru_cache(maxsize=16)
def latest_github_release_version(repo: str) -> str:
    """Return the latest release version for ``repo`` without a leading ``v``."""
    url = f"{_GITHUB_API_BASE_URL}/{repo}/releases/latest"
    try:
        response = httpx.get(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "brokk-cli",
            },
            timeout=_RELEASE_RESOLUTION_TIMEOUT_SECONDS,
            follow_redirects=True,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise ReleaseResolverError(f"failed to fetch latest release for {repo}: {exc}") from exc

    try:
        payload = response.json()
    except ValueError as exc:
        raise ReleaseResolverError(
            f"latest release response for {repo} was not valid JSON"
        ) from exc

    tag_name = payload.get("tag_name")
    if not isinstance(tag_name, str) or not tag_name.strip():
        raise ReleaseResolverError(
            f"latest release response for {repo} did not include a usable tag_name"
        )

    version = tag_name.strip().removeprefix("v")
    if not version:
        raise ReleaseResolverError(f"latest release response for {repo} had an empty tag_name")
    return version
