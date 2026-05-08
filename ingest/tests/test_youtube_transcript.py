"""Tests for the YouTube auto-captions fetcher."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from src.pipeline.extractors._youtube_transcript import (
    extract_video_id,
    fetch_youtube_transcript,
)


# ── extract_video_id ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://youtube.com/watch?v=Ln11hm7jieM&si=8RTalpJlgMACrJBb", "Ln11hm7jieM"),
        ("https://youtu.be/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/embed/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/shorts/abc12345DEF", "abc12345DEF"),
        ("https://www.youtube.com/live/Ln11hm7jieM", "Ln11hm7jieM"),
    ],
)
def test_extract_video_id_finds_11char_ids(url, expected):
    assert extract_video_id(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/",
        "https://example.com/foo",
        "https://www.youtube.com/watch?v=tooshort",
        "",
    ],
)
def test_extract_video_id_returns_none_for_invalid(url):
    assert extract_video_id(url) is None


# ── fetch_youtube_transcript ────────────────────────────────────────


class _FakeSnippet:
    """Mimics youtube_transcript_api's FetchedTranscriptSnippet."""

    def __init__(self, text: str, start: float = 0.0):
        self.text = text
        self.start = start


def _fake_transcript(snippets: list[tuple[str, float]]) -> list[_FakeSnippet]:
    return [_FakeSnippet(t, s) for t, s in snippets]


@pytest.mark.asyncio
async def test_fetch_returns_joined_text_on_success():
    """Mock the API to return three snippets — they should be joined with newlines."""
    fake_data = _fake_transcript([
        ("Hello world", 0.0),
        ("This is a test", 1.5),
        ("Final line", 3.0),
    ])

    class _FakeApi:
        def fetch(self, video_id, languages=None):
            return fake_data

    with patch("youtube_transcript_api.YouTubeTranscriptApi", _FakeApi):
        result = await fetch_youtube_transcript("https://youtu.be/abcdefghijk")

    assert result == "Hello world\nThis is a test\nFinal line"


@pytest.mark.asyncio
async def test_fetch_dedupes_consecutive_duplicates():
    """Auto-captions repeat lines on overlap — collapse them."""
    fake_data = _fake_transcript([
        ("first", 0.0),
        ("first", 1.0),  # duplicate of previous
        ("second", 2.0),
        ("second", 3.0),  # duplicate of previous
        ("third", 4.0),
    ])

    class _FakeApi:
        def fetch(self, video_id, languages=None):
            return fake_data

    with patch("youtube_transcript_api.YouTubeTranscriptApi", _FakeApi):
        result = await fetch_youtube_transcript("https://youtu.be/abcdefghijk")

    assert result == "first\nsecond\nthird"


@pytest.mark.asyncio
async def test_fetch_returns_none_for_url_without_video_id():
    result = await fetch_youtube_transcript("https://www.youtube.com/")
    assert result is None


@pytest.mark.asyncio
async def test_fetch_returns_none_when_transcripts_disabled():
    """TranscriptsDisabled is the most common path — common video without captions."""
    from youtube_transcript_api import TranscriptsDisabled

    class _FakeApi:
        def fetch(self, video_id, languages=None):
            raise TranscriptsDisabled(video_id)

    with patch("youtube_transcript_api.YouTubeTranscriptApi", _FakeApi):
        result = await fetch_youtube_transcript("https://youtu.be/abcdefghijk")

    assert result is None


@pytest.mark.asyncio
async def test_fetch_returns_none_when_no_transcript_in_languages():
    from youtube_transcript_api import NoTranscriptFound

    class _FakeApi:
        def fetch(self, video_id, languages=None):
            raise NoTranscriptFound(video_id, languages or [], None)

    with patch("youtube_transcript_api.YouTubeTranscriptApi", _FakeApi):
        result = await fetch_youtube_transcript("https://youtu.be/abcdefghijk")

    assert result is None


@pytest.mark.asyncio
async def test_fetch_returns_none_when_video_unavailable():
    from youtube_transcript_api import VideoUnavailable

    class _FakeApi:
        def fetch(self, video_id, languages=None):
            raise VideoUnavailable(video_id)

    with patch("youtube_transcript_api.YouTubeTranscriptApi", _FakeApi):
        result = await fetch_youtube_transcript("https://youtu.be/abcdefghijk")

    assert result is None


@pytest.mark.asyncio
async def test_fetch_returns_none_on_unexpected_exception():
    """Any other exception must be swallowed — fallback is best-effort."""
    class _FakeApi:
        def fetch(self, video_id, languages=None):
            raise RuntimeError("network melted")

    with patch("youtube_transcript_api.YouTubeTranscriptApi", _FakeApi):
        result = await fetch_youtube_transcript("https://youtu.be/abcdefghijk")

    assert result is None


@pytest.mark.asyncio
async def test_fetch_passes_languages_in_priority_order():
    """The Czech-then-English order matters — we want CS first when available."""
    captured: dict = {}

    class _FakeApi:
        def fetch(self, video_id, languages=None):
            captured["languages"] = list(languages or [])
            captured["video_id"] = video_id
            return _fake_transcript([("ahoj", 0.0)])

    with patch("youtube_transcript_api.YouTubeTranscriptApi", _FakeApi):
        await fetch_youtube_transcript(
            "https://youtu.be/abcdefghijk",
            languages=("cs", "en"),
        )

    assert captured["video_id"] == "abcdefghijk"
    assert captured["languages"] == ["cs", "en"]


@pytest.mark.asyncio
async def test_fetch_returns_none_when_all_snippets_empty():
    """Edge case: API returned snippets but all of their .text was empty/whitespace."""
    fake_data = _fake_transcript([("", 0.0), ("   ", 1.0)])

    class _FakeApi:
        def fetch(self, video_id, languages=None):
            return fake_data

    with patch("youtube_transcript_api.YouTubeTranscriptApi", _FakeApi):
        result = await fetch_youtube_transcript("https://youtu.be/abcdefghijk")

    assert result is None
