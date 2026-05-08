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
            # Cobalt v11 only accepts: best | mp3 | ogg | wav | opus.
            assert body["audioFormat"] in {"best", "mp3", "ogg", "wav", "opus"}
            return httpx.Response(
                200,
                json={"status": "tunnel", "url": "http://cobalt:9000/tunnel/xyz", "filename": "sample.mp3"},
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


# ── YouTube bot-block fallback ──────────────────────────────────────


@pytest.mark.asyncio
async def test_youtube_bot_block_falls_back_to_oembed(monkeypatch):
    """When cobalt returns error.api.youtube.login on a YT URL, we fall
    back to oEmbed metadata so the doc still gets a title + author."""
    from src.pipeline.extractors import cobalt_ext

    def _cobalt_handler(request: httpx.Request) -> httpx.Response:
        # Cobalt's exact YT bot-block response.
        return httpx.Response(
            400,
            json={"status": "error", "error": {"code": "error.api.youtube.login"}},
        )

    monkeypatch.setattr(cobalt_ext, "_TEST_TRANSPORT", httpx.MockTransport(_cobalt_handler), raising=False)
    # Stub metadata fetch (irrelevant — both fail in this scenario).
    async def _fail_metadata(url: str) -> None:
        return None
    monkeypatch.setattr(cobalt_ext, "fetch_metadata", _fail_metadata, raising=False)
    # Stub oEmbed to return realistic shape.
    async def _fake_oembed(url: str) -> dict:
        return {
            "title": "How To Build An Agent",
            "author_name": "Anthropic",
            "type": "video",
            "thumbnail_url": "https://i.ytimg.com/vi/x/hqdefault.jpg",
        }
    monkeypatch.setattr(cobalt_ext, "fetch_youtube_oembed", _fake_oembed, raising=False)

    result = await cobalt_ext.extract(
        "https://www.youtube.com/watch?v=abc",
        _platform(),  # platform.id = "youtube"
    )

    assert result.title == "How To Build An Agent"
    assert result.author == "Anthropic"
    assert result.media_kind == MediaKind.VIDEO
    assert result.extra["extractor"] == "youtube_oembed_fallback"
    assert result.extra["transcript_unavailable"] is True
    # Body should include the title, author, source URL, and a note about unavailability
    assert "How To Build An Agent" in result.body_md
    assert "Anthropic" in result.body_md
    assert "## Transcript" in result.body_md
    assert "Unavailable" in result.body_md


@pytest.mark.asyncio
async def test_youtube_bot_block_with_oembed_failure_still_returns(monkeypatch):
    """If oEmbed itself fails (e.g. private video), we still return an
    Extracted — just with title=None. The summarizer can fall back to
    a deterministic title from the URL host."""
    from src.pipeline.extractors import cobalt_ext

    def _cobalt_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"status": "error", "error": {"code": "error.api.youtube.login"}},
        )

    monkeypatch.setattr(cobalt_ext, "_TEST_TRANSPORT", httpx.MockTransport(_cobalt_handler), raising=False)
    async def _fail_metadata(url: str) -> None:
        return None
    monkeypatch.setattr(cobalt_ext, "fetch_metadata", _fail_metadata, raising=False)
    async def _fail_oembed(url: str) -> None:
        return None
    monkeypatch.setattr(cobalt_ext, "fetch_youtube_oembed", _fail_oembed, raising=False)

    result = await cobalt_ext.extract(
        "https://www.youtube.com/watch?v=abc",
        _platform(),
    )

    assert result.title is None
    assert result.author is None
    assert result.extra["extractor"] == "youtube_oembed_fallback"
    assert result.extra["has_metadata"] is False
    assert "Unavailable" in result.body_md


@pytest.mark.asyncio
async def test_non_youtube_bot_block_still_raises(monkeypatch):
    """An IG / TikTok cobalt failure must NOT trigger the YouTube fallback —
    the worker should retry as before."""
    from src.pipeline.extractors import cobalt_ext

    def _cobalt_handler(request: httpx.Request) -> httpx.Response:
        # Same error code shape but on a non-YT platform — fallback shouldn't trigger.
        return httpx.Response(
            400,
            json={"status": "error", "error": {"code": "error.api.youtube.login"}},
        )

    monkeypatch.setattr(cobalt_ext, "_TEST_TRANSPORT", httpx.MockTransport(_cobalt_handler), raising=False)

    with pytest.raises(RuntimeError, match="cobalt http"):
        await cobalt_ext.extract(
            "https://www.instagram.com/reel/x/",
            _platform(id_="instagram"),
        )
