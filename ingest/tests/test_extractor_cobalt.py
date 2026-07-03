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
            # 8 KB stub — must be >= _MIN_AUDIO_BYTES (4 KB) so the
            # empty-audio guard in _download_audio doesn't trip on tests.
            return httpx.Response(200, content=b"\x00" * 8192)
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
async def test_youtube_captions_first_skips_cobalt_audio(monkeypatch):
    """Phase 12.5 fix #9: when YT auto-captions are available, the new
    captions-first strategy uses them directly — skipping the cobalt
    audio download AND the Whisper API call. Saves time + ~$0.006/min."""
    from src.pipeline.extractors import cobalt_ext

    cobalt_calls = {"count": 0}

    def _handler(request: httpx.Request) -> httpx.Response:
        cobalt_calls["count"] += 1
        return httpx.Response(500, text="cobalt should not be called when captions exist")

    monkeypatch.setattr(cobalt_ext, "_TEST_TRANSPORT", httpx.MockTransport(_handler), raising=False)

    # Captions return real text — no need to fall back to Whisper.
    async def _captions(url: str, **kwargs) -> str:
        return "Hello and welcome to today's tutorial. We're going to learn about Python."
    monkeypatch.setattr(cobalt_ext, "fetch_youtube_transcript", _captions, raising=False)

    # yt-dlp metadata returns reasonable values
    async def _metadata(url: str) -> dict:
        return {"title": "Python Tutorial", "channel": "TeacherChan", "description": "Learn Python."}
    monkeypatch.setattr(cobalt_ext, "fetch_metadata", _metadata, raising=False)

    # Spy on Whisper — must NOT be called.
    whisper_spy = AsyncMock(return_value="should-not-appear")
    monkeypatch.setattr(cobalt_ext, "_whisper_transcribe", whisper_spy)

    result = await cobalt_ext.extract(
        "https://www.youtube.com/watch?v=abc",
        _platform(),
    )

    assert cobalt_calls["count"] == 0, "cobalt audio API was called even though captions existed"
    whisper_spy.assert_not_awaited()
    assert result.extra["transcript_source"] == "youtube_captions"
    assert "Python Tutorial" == result.title
    assert "## Transcript (YouTube captions)" in result.body_md
    assert "today's tutorial" in result.body_md


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
        return httpx.Response(200, content=b"\x00" * 8192)

    monkeypatch.setattr(cobalt_ext, "_TEST_TRANSPORT", httpx.MockTransport(_handler), raising=False)
    monkeypatch.setattr(cobalt_ext, "_whisper_transcribe", AsyncMock(return_value="ok"))

    result = await cobalt_ext.extract("https://example.com/x", _platform())
    assert "ok" in result.body_md


@pytest.mark.asyncio
async def test_cobalt_empty_audio_raises_descriptive_error(monkeypatch):
    """Phase 12.5 fix #8: cobalt occasionally returns HTTP 200 with an
    empty / HTML body when the upstream YT fetch silently failed.
    Whisper would then choke with cryptic "Invalid file format /
    duration 0" — _download_audio now detects this and raises a clear
    error pointing at the real cause.

    Verified on a NON-YouTube platform: for IG/TikTok the raw error
    propagates so the worker retries normally. (YouTube intercepts the
    same error via _is_youtube_recoverable_failure and falls back to
    oEmbed-only — covered separately by
    test_youtube_cobalt_zero_byte_audio_falls_back_to_oembed.)"""
    from src.pipeline.extractors import cobalt_ext

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                200,
                json={"status": "tunnel", "url": "http://cobalt:9000/tunnel/empty"},
            )
        # 200 OK but only 100 bytes (an HTML error page would be ~300,
        # an empty body would be 0 — both are below the 4 KB threshold)
        return httpx.Response(200, content=b"<html>not audio</html>" + b"\x00" * 50)

    monkeypatch.setattr(cobalt_ext, "_TEST_TRANSPORT", httpx.MockTransport(_handler), raising=False)
    monkeypatch.setattr(cobalt_ext, "_whisper_transcribe", AsyncMock(return_value="ok"))

    with pytest.raises(RuntimeError, match="cobalt audio too small"):
        await cobalt_ext.extract("https://www.instagram.com/reel/x/", _platform(id_="instagram"))


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


# ── Transcription cost guard (MAX_TRANSCRIPT_MIN + Whisper 25 MB cap) ──


@pytest.mark.asyncio
async def test_duration_over_cap_skips_download_and_whisper(monkeypatch):
    """A 90-min captionless video must not download audio or call Whisper —
    the doc gets a 'transcript skipped' note instead (same contract as the
    legacy ytdlp extractor, which is where the guard used to live even
    though no platform routes through it anymore)."""
    from src.pipeline.extractors import cobalt_ext

    tunnel_gets = {"count": 0}

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/" and request.method == "POST":
            return httpx.Response(
                200,
                json={"status": "tunnel", "url": "http://cobalt:9000/tunnel/xyz", "filename": "a.mp3"},
            )
        if request.url.path.startswith("/tunnel/"):
            tunnel_gets["count"] += 1
            return httpx.Response(200, content=b"\x00" * 8192)
        return httpx.Response(404)

    monkeypatch.setattr(cobalt_ext, "_TEST_TRANSPORT", httpx.MockTransport(_handler), raising=False)

    async def _metadata(url: str) -> dict:
        return {"title": "Long Live", "channel": "Ch", "description": "d",
                "duration": 90 * 60}
    monkeypatch.setattr(cobalt_ext, "fetch_metadata", _metadata, raising=False)

    whisper_spy = AsyncMock(return_value="should-not-appear")
    monkeypatch.setattr(cobalt_ext, "_whisper_transcribe", whisper_spy)

    result = await cobalt_ext.extract(
        "https://www.instagram.com/reel/long/",
        _platform(id_="instagram"),
    )

    whisper_spy.assert_not_awaited()
    assert tunnel_gets["count"] == 0, "audio was downloaded despite the duration cap"
    assert "transcript skipped" in result.body_md.lower()
    assert result.extra["transcript_source"] == "skipped_too_long"
    assert result.extra["transcript_unavailable"] is True
    assert result.title == "Long Live"


@pytest.mark.asyncio
async def test_unknown_duration_oversize_audio_skips_whisper(monkeypatch):
    """Metadata failed (duration unknown — common under bot-blocks) and the
    audio stream blows past the Whisper upload cap: abort the download
    mid-stream and finish the capture with a 'transcript skipped' note —
    instead of letting OpenAI 413 the upload, which failed extraction
    before the snapshot was saved and re-paid the full download on every
    retry."""
    from src.pipeline.extractors import cobalt_ext

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/" and request.method == "POST":
            return httpx.Response(
                200,
                json={"status": "tunnel", "url": "http://cobalt:9000/tunnel/big", "filename": "a.mp3"},
            )
        if request.url.path.startswith("/tunnel/"):
            return httpx.Response(200, content=b"\x00" * (16 * 1024))
        return httpx.Response(404)

    monkeypatch.setattr(cobalt_ext, "_TEST_TRANSPORT", httpx.MockTransport(_handler), raising=False)
    # Shrink the cap so the 16 KB stub trips it without a 25 MB fixture.
    monkeypatch.setattr(cobalt_ext, "_WHISPER_MAX_UPLOAD_BYTES", 8 * 1024)

    async def _no_metadata(url: str) -> None:
        return None
    monkeypatch.setattr(cobalt_ext, "fetch_metadata", _no_metadata, raising=False)

    whisper_spy = AsyncMock(return_value="should-not-appear")
    monkeypatch.setattr(cobalt_ext, "_whisper_transcribe", whisper_spy)

    result = await cobalt_ext.extract(
        "https://www.instagram.com/reel/big/",
        _platform(id_="instagram"),
    )

    whisper_spy.assert_not_awaited()
    assert result.extra["extractor"] == "cobalt"  # NOT the YT fallback path
    assert "transcript skipped" in result.body_md.lower()
    assert result.extra["transcript_source"] == "skipped_too_long"
    assert result.extra["transcript_unavailable"] is True


@pytest.mark.asyncio
async def test_duration_under_cap_still_transcribes(monkeypatch):
    """Guard must not fire for a normal short video with known duration."""
    from src.pipeline.extractors import cobalt_ext

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/" and request.method == "POST":
            return httpx.Response(
                200,
                json={"status": "tunnel", "url": "http://cobalt:9000/tunnel/ok", "filename": "a.mp3"},
            )
        if request.url.path.startswith("/tunnel/"):
            return httpx.Response(200, content=b"\x00" * 8192)
        return httpx.Response(404)

    monkeypatch.setattr(cobalt_ext, "_TEST_TRANSPORT", httpx.MockTransport(_handler), raising=False)

    async def _metadata(url: str) -> dict:
        return {"title": "Short Reel", "channel": "Ch", "duration": 45}
    monkeypatch.setattr(cobalt_ext, "fetch_metadata", _metadata, raising=False)

    monkeypatch.setattr(
        cobalt_ext, "_whisper_transcribe", AsyncMock(return_value="short transcript"),
    )

    result = await cobalt_ext.extract(
        "https://www.instagram.com/reel/short/",
        _platform(id_="instagram"),
    )

    assert "short transcript" in result.body_md
    assert result.extra["transcript_source"] == "whisper"
    assert result.extra["transcript_unavailable"] is False


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
    # Captions also unavailable for this test
    async def _no_captions(url: str, **kwargs) -> None:
        return None
    monkeypatch.setattr(cobalt_ext, "fetch_youtube_transcript", _no_captions, raising=False)

    result = await cobalt_ext.extract(
        "https://www.youtube.com/watch?v=abc",
        _platform(),  # platform.id = "youtube"
    )

    assert result.title == "How To Build An Agent"
    assert result.author == "Anthropic"
    assert result.media_kind == MediaKind.VIDEO
    assert result.extra["extractor"] == "youtube_oembed_fallback"
    assert result.extra["transcript_unavailable"] is True
    assert result.extra["transcript_source"] == "unavailable"
    # Body should include the title, author, source URL, and a note about unavailability
    assert "How To Build An Agent" in result.body_md
    assert "Anthropic" in result.body_md
    assert "## Transcript" in result.body_md
    assert "Unavailable" in result.body_md


@pytest.mark.asyncio
async def test_youtube_bot_block_recovers_captions_via_transcript_api(monkeypatch):
    """The big win: cobalt blocked, but YT's caption URL is unauthenticated
    so we still get a real transcript. Body should have the captions, not
    the 'unavailable' placeholder."""
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

    async def _fake_oembed(url: str) -> dict:
        return {"title": "Tutorial", "author_name": "ChannelX", "type": "video"}
    monkeypatch.setattr(cobalt_ext, "fetch_youtube_oembed", _fake_oembed, raising=False)

    async def _fake_captions(url: str, **kwargs) -> str:
        return "Welcome to the tutorial.\nIn this video we will build a thing.\nLet's get started."
    monkeypatch.setattr(cobalt_ext, "fetch_youtube_transcript", _fake_captions, raising=False)

    result = await cobalt_ext.extract(
        "https://www.youtube.com/watch?v=abc",
        _platform(),
    )

    assert result.title == "Tutorial"
    assert result.author == "ChannelX"
    assert result.extra["transcript_source"] == "youtube_captions"
    assert result.extra["transcript_unavailable"] is False
    # Body has the captions block heading + the real transcript
    assert "## Transcript (YouTube captions)" in result.body_md
    assert "Welcome to the tutorial." in result.body_md
    assert "build a thing" in result.body_md
    # And NOT the "Unavailable" placeholder
    assert "Unavailable" not in result.body_md


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
    async def _no_captions(url: str, **kwargs) -> None:
        return None
    monkeypatch.setattr(cobalt_ext, "fetch_youtube_transcript", _no_captions, raising=False)

    result = await cobalt_ext.extract(
        "https://www.youtube.com/watch?v=abc",
        _platform(),
    )

    assert result.title is None
    assert result.author is None
    assert result.extra["extractor"] == "youtube_oembed_fallback"
    assert result.extra["has_metadata"] is False
    assert result.extra["transcript_source"] == "unavailable"
    assert "Unavailable" in result.body_md


@pytest.mark.asyncio
async def test_youtube_bot_block_oembed_fails_but_captions_succeed(monkeypatch):
    """Edge case: oEmbed 404 (e.g. unlisted video) but captions still work.
    Title is None but body still has the transcript content."""
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
    async def _fake_captions(url: str, **kwargs) -> str:
        return "Captions content here."
    monkeypatch.setattr(cobalt_ext, "fetch_youtube_transcript", _fake_captions, raising=False)

    result = await cobalt_ext.extract(
        "https://www.youtube.com/watch?v=abc",
        _platform(),
    )

    assert result.title is None
    assert result.extra["transcript_source"] == "youtube_captions"
    assert result.extra["has_metadata"] is False
    assert "Captions content here." in result.body_md


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


@pytest.mark.asyncio
async def test_youtube_cobalt_zero_byte_audio_falls_back_to_oembed(monkeypatch):
    """Cobalt's tunnel can return HTTP 200 with a 0-byte body when the
    upstream YouTube fetch silently failed. _download_audio raises
    `cobalt audio too small: 0 bytes` — that error must trigger the same
    oEmbed-only fallback as the explicit error.api.youtube.login response.

    Without this fallback, captures with no captions get pinned in the
    failed/retry loop forever even though oEmbed-only would produce a
    usable doc with title + author + 'transcript unavailable' note."""
    from src.pipeline.extractors import cobalt_ext

    def _cobalt_handler(request: httpx.Request) -> httpx.Response:
        # POST → 200 with a tunnel URL (looks like success).
        if request.url.path == "/" and request.method == "POST":
            return httpx.Response(
                200,
                json={"status": "tunnel", "url": "http://cobalt:9000/tunnel/empty", "filename": "x.mp3"},
            )
        # GET tunnel → 200 with 0 bytes (cobalt's silent upstream failure).
        if request.url.path.startswith("/tunnel/"):
            return httpx.Response(200, content=b"")
        return httpx.Response(404)

    monkeypatch.setattr(cobalt_ext, "_TEST_TRANSPORT", httpx.MockTransport(_cobalt_handler), raising=False)

    async def _no_captions(url: str, **kwargs) -> None:
        return None
    monkeypatch.setattr(cobalt_ext, "fetch_youtube_transcript", _no_captions, raising=False)

    async def _fail_metadata(url: str) -> None:
        return None
    monkeypatch.setattr(cobalt_ext, "fetch_metadata", _fail_metadata, raising=False)

    async def _fake_oembed(url: str) -> dict:
        return {"title": "Some Talk", "author_name": "Conf", "type": "video"}
    monkeypatch.setattr(cobalt_ext, "fetch_youtube_oembed", _fake_oembed, raising=False)

    # Must NOT raise — must fall through to oEmbed-only Extracted.
    result = await cobalt_ext.extract(
        "https://www.youtube.com/watch?v=-wDnwHX02fU",
        _platform(),
    )

    assert result.title == "Some Talk"
    assert result.author == "Conf"
    assert result.extra["extractor"] == "youtube_oembed_fallback"
    assert result.extra["transcript_unavailable"] is True
    assert "Unavailable" in result.body_md


@pytest.mark.asyncio
async def test_non_youtube_cobalt_zero_byte_audio_still_raises(monkeypatch):
    """The 0-byte fallback is YouTube-specific — IG/TikTok captures must
    still raise so the worker retries them on the normal backoff schedule.
    (No oEmbed equivalent for those platforms.)"""
    from src.pipeline.extractors import cobalt_ext

    def _cobalt_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/" and request.method == "POST":
            return httpx.Response(
                200,
                json={"status": "tunnel", "url": "http://cobalt:9000/tunnel/empty", "filename": "x.mp3"},
            )
        if request.url.path.startswith("/tunnel/"):
            return httpx.Response(200, content=b"")
        return httpx.Response(404)

    monkeypatch.setattr(cobalt_ext, "_TEST_TRANSPORT", httpx.MockTransport(_cobalt_handler), raising=False)

    with pytest.raises(RuntimeError, match="cobalt audio too small"):
        await cobalt_ext.extract(
            "https://www.instagram.com/reel/x/",
            _platform(id_="instagram"),
        )
