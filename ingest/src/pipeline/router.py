"""URL host → Platform identity.

Reads a TopicsConfig once and answers `detect(url) -> Platform`. Match is
exact-host-equality with the lists in `Platform.hosts`. Subdomains not listed
fall through to the catch-all `hosts: ["*"]` entry. A config without a
catch-all causes `detect()` to raise on unmatched URLs — that's a
misconfiguration we want to surface loudly.

Public API:
    router = PlatformRouter(config)
    plat = router.detect("https://www.instagram.com/p/abc")
    path = router.initial_path(plat)   # → ["Sources", "Socials", "Instagram"]
"""

from __future__ import annotations

from urllib.parse import urlparse

from src.config import Platform, TopicsConfig


class PlatformRouter:
    def __init__(self, config: TopicsConfig) -> None:
        self._platforms = list(config.platforms)
        # Pre-build a host → platform index for fast lookup.
        self._by_host: dict[str, Platform] = {}
        self._catch_all: Platform | None = None
        for p in self._platforms:
            for host in p.hosts:
                if host == "*":
                    self._catch_all = p
                else:
                    self._by_host[host.lower()] = p

    def detect(self, url: str) -> Platform:
        host = self._extract_host(url)
        match = self._by_host.get(host)
        if match is not None:
            return match
        if self._catch_all is None:
            raise LookupError(
                f"no catch-all platform configured (host={host!r})"
            )
        return self._catch_all

    @property
    def catch_all(self) -> Platform | None:
        """The catch-all platform (hosts: ["*"]) or None if none configured."""
        return self._catch_all

    @staticmethod
    def initial_path(platform: Platform) -> list[str]:
        return ["Sources", platform.group, platform.folder_name]

    @staticmethod
    def _extract_host(url: str) -> str:
        if "://" not in url:
            raise ValueError(f"cannot extract host from URL: {url!r}")
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if not host:
            raise ValueError(f"cannot extract host from URL: {url!r}")
        return host
