"""Shared test fixtures.

The suite must pass identically on dev machines, CI runners, and proxied
sandboxes. Production code branches on proxy env vars (the YouTube
transcript fetcher passes an `http_client=` to YouTubeTranscriptApi when
HTTP(S)_PROXY is set — see _youtube_transcript._fetch_sync), so an
inherited proxy environment changes code paths under test. Strip the
vars for every test; tests that exercise the proxy branch explicitly set
them back with monkeypatch.setenv.
"""

import pytest


@pytest.fixture(autouse=True)
def _clean_proxy_env(monkeypatch):
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.delenv(var, raising=False)
