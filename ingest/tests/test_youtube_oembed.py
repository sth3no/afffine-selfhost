"""Unit tests for the YouTube oEmbed fallback fetcher."""

import json

import httpx
import pytest

from src.pipeline.extractors import _youtube_oembed


@pytest.mark.asyncio
async def test_oembed_returns_payload_on_success(monkeypatch):
    captured_url: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured_url["url"] = str(request.url)
        return httpx.Response(
            200,
            json={
                "title": "Sample video",
                "author_name": "Test Channel",
                "author_url": "https://www.youtube.com/@test",
                "thumbnail_url": "https://i.ytimg.com/vi/x/hqdefault.jpg",
                "type": "video",
            },
        )

    monkeypatch.setattr(_youtube_oembed, "_TEST_TRANSPORT", httpx.MockTransport(_handler), raising=False)

    payload = await _youtube_oembed.fetch_youtube_oembed("https://www.youtube.com/watch?v=abc")
    assert payload is not None
    assert payload["title"] == "Sample video"
    assert payload["author_name"] == "Test Channel"
    assert "youtube.com/oembed" in captured_url["url"]


@pytest.mark.asyncio
async def test_oembed_returns_none_on_404(monkeypatch):
    """Private/unlisted videos return 404 — fallback should swallow + return None."""
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="Not Found")

    monkeypatch.setattr(_youtube_oembed, "_TEST_TRANSPORT", httpx.MockTransport(_handler), raising=False)

    payload = await _youtube_oembed.fetch_youtube_oembed("https://www.youtube.com/watch?v=private")
    assert payload is None


@pytest.mark.asyncio
async def test_oembed_returns_none_on_invalid_json(monkeypatch):
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    monkeypatch.setattr(_youtube_oembed, "_TEST_TRANSPORT", httpx.MockTransport(_handler), raising=False)

    payload = await _youtube_oembed.fetch_youtube_oembed("https://www.youtube.com/watch?v=x")
    assert payload is None


@pytest.mark.asyncio
async def test_oembed_returns_none_on_network_error(monkeypatch):
    def _handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no DNS")

    monkeypatch.setattr(_youtube_oembed, "_TEST_TRANSPORT", httpx.MockTransport(_handler), raising=False)

    payload = await _youtube_oembed.fetch_youtube_oembed("https://www.youtube.com/watch?v=x")
    assert payload is None
