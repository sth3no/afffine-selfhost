"""Unit tests for the cobalt extractor.

Mocks at three boundaries:
  - cobalt API (httpx.MockTransport)
  - audio download (httpx.MockTransport, same client)
  - Whisper transcription (monkeypatched _whisper_transcribe)

Real cobalt + real Whisper integration belongs in test_extractors_integration.py
behind the `integration` marker; these tests stay hermetic.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import httpx
import pytest

from src.config import Platform
from src.pipeline.extracted import MediaKind


def _platform(id_: str = "youtube") -> Platform:
    return Platform(
        id=id_,
        group="Socials",
        folder_name="Youtube",
        hosts=["youtube.com"],
        extractor="cobalt",
    )


@pytest.mark.asyncio
async def test_cobalt_happy_path_returns_transcript(monkeypatch):
    from src.pipeline.extractors import cobalt_ext

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/" and request.method == "POST":
            body = json.loads(request.content)
            assert body["url"] == "https://www.youtube.com/watch?v=abc"
            assert body["downloadMode"] == "audio"
            return httpx.Response(
                200,
                json={"status": "tunnel", "url": "http://cobalt:9000/tunnel/xyz", "filename": "sample.m4a"},
            )
        if request.url.path.startswith("/tunnel/"):
            return httpx.Response(200, content=b"\x00" * 32)
        return httpx.Response(404)

    monkeypatch.setattr(cobalt_ext, "_TEST_TRANSPORT", httpx.MockTransport(_handler), raising=False)
    monkeypatch.setattr(
        cobalt_ext,
        "_whisper_transcribe",
        AsyncMock(return_value="hello world this is a transcript"),
    )

    result = await cobalt_ext.extract(
        "https://www.youtube.com/watch?v=abc",
        _platform(),
    )

    assert result.media_kind == MediaKind.VIDEO
    assert "hello world" in result.body_md
    assert result.extra["extractor"] == "cobalt"
    assert result.extra["platform_id"] == "youtube"


@pytest.mark.asyncio
async def test_cobalt_redirect_status_treated_like_tunnel(monkeypatch):
    """cobalt sometimes returns status=redirect (direct CDN URL) — same shape."""
    from src.pipeline.extractors import cobalt_ext

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                200,
                json={"status": "redirect", "url": "http://cdn.example.com/audio.m4a", "filename": "a.m4a"},
            )
        return httpx.Response(200, content=b"\x00" * 16)

    monkeypatch.setattr(cobalt_ext, "_TEST_TRANSPORT", httpx.MockTransport(_handler), raising=False)
    monkeypatch.setattr(cobalt_ext, "_whisper_transcribe", AsyncMock(return_value="ok"))

    result = await cobalt_ext.extract("https://example.com/x", _platform())
    assert "ok" in result.body_md


@pytest.mark.asyncio
async def test_cobalt_error_status_raises_runtime_error(monkeypatch):
    from src.pipeline.extractors import cobalt_ext

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"status": "error", "error": {"code": "fetch.empty", "context": ""}},
        )

    monkeypatch.setattr(cobalt_ext, "_TEST_TRANSPORT", httpx.MockTransport(_handler), raising=False)

    with pytest.raises(RuntimeError, match="cobalt error.*fetch.empty"):
        await cobalt_ext.extract("https://example.com/x", _platform())


@pytest.mark.asyncio
async def test_cobalt_http_error_raises_runtime_error(monkeypatch):
    from src.pipeline.extractors import cobalt_ext

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream down")

    monkeypatch.setattr(cobalt_ext, "_TEST_TRANSPORT", httpx.MockTransport(_handler), raising=False)

    with pytest.raises(RuntimeError, match="cobalt http"):
        await cobalt_ext.extract("https://example.com/x", _platform())
