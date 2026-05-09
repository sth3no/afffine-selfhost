"""Verify that yt-dlp metadata fetcher + youtube-transcript-api both
honor the cookies file when it's present and skip the flag when it isn't."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_ytdlp_metadata_passes_cookies_when_file_exists(tmp_path, monkeypatch):
    """When the cookies file is on disk and non-empty, yt-dlp gets
    `--cookies <path>` injected into its args."""
    from src.pipeline.extractors import _ytdlp_metadata

    cookies = tmp_path / "youtube.txt"
    cookies.write_text("# header\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tabc\n", encoding="utf-8")

    from src.config import settings
    monkeypatch.setattr(settings, "youtube_cookies_path", str(cookies))

    captured_args: list[str] = []

    async def _fake_subprocess_exec(*args, **kwargs):
        captured_args.extend(args)
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(b"", b""))
        proc.returncode = 1  # we don't care about success here
        return proc

    monkeypatch.setattr(
        _ytdlp_metadata.asyncio,
        "create_subprocess_exec",
        _fake_subprocess_exec,
    )

    await _ytdlp_metadata.fetch_metadata("https://youtube.com/watch?v=abc")

    # --cookies <path> must be in the args
    assert "--cookies" in captured_args
    cookies_idx = captured_args.index("--cookies")
    assert captured_args[cookies_idx + 1] == str(cookies)


@pytest.mark.asyncio
async def test_ytdlp_metadata_does_not_force_player_client(tmp_path, monkeypatch):
    """Phase 12.5 fix #8: dropped the web_embedded,tv_simply extractor
    args. With residential proxy egress, the default client works AND
    has all formats — web_embedded was reporting "Requested format is
    not available" on niche videos. Regression guard against re-adding."""
    from src.pipeline.extractors import _ytdlp_metadata

    from src.config import settings
    monkeypatch.setattr(settings, "youtube_cookies_path", str(tmp_path / "missing.txt"))

    captured_args: list[str] = []

    async def _fake_subprocess_exec(*args, **kwargs):
        captured_args.extend(args)
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(b"", b""))
        proc.returncode = 1
        return proc

    monkeypatch.setattr(
        _ytdlp_metadata.asyncio,
        "create_subprocess_exec",
        _fake_subprocess_exec,
    )

    await _ytdlp_metadata.fetch_metadata("https://youtube.com/watch?v=abc")

    # No player_client override; yt-dlp uses its default (web) client.
    assert "--extractor-args" not in captured_args
    joined = " ".join(captured_args)
    assert "web_embedded" not in joined
    assert "tv_simply" not in joined


@pytest.mark.asyncio
async def test_ytdlp_metadata_omits_cookies_when_file_missing(tmp_path, monkeypatch):
    """When no cookies file exists, --cookies must NOT be passed (yt-dlp
    errors on a missing path)."""
    from src.pipeline.extractors import _ytdlp_metadata

    from src.config import settings
    monkeypatch.setattr(settings, "youtube_cookies_path", str(tmp_path / "nonexistent.txt"))

    captured_args: list[str] = []

    async def _fake_subprocess_exec(*args, **kwargs):
        captured_args.extend(args)
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(b"", b""))
        proc.returncode = 1
        return proc

    monkeypatch.setattr(
        _ytdlp_metadata.asyncio,
        "create_subprocess_exec",
        _fake_subprocess_exec,
    )

    await _ytdlp_metadata.fetch_metadata("https://youtube.com/watch?v=abc")

    assert "--cookies" not in captured_args


@pytest.mark.asyncio
async def test_transcript_api_passes_http_client_when_file_exists(tmp_path, monkeypatch):
    """YouTubeTranscriptApi 1.x takes `http_client=` (a requests.Session),
    not `cookie_path`. We load the Netscape file via MozillaCookieJar
    into a Session and pass that."""
    import requests

    from src.pipeline.extractors import _youtube_transcript

    cookies = tmp_path / "youtube.txt"
    cookies.write_text(
        "# Netscape HTTP Cookie File\n"
        ".youtube.com\tTRUE\t/\tTRUE\t0\tSID\tabc\n",
        encoding="utf-8",
    )

    from src.config import settings
    monkeypatch.setattr(settings, "youtube_cookies_path", str(cookies))

    init_kwargs: dict = {}

    class _FakeApi:
        def __init__(self, **kwargs):
            init_kwargs.update(kwargs)

        def fetch(self, video_id, languages=None):
            class _Snip:
                text = "hello"
                start = 0.0
            return [_Snip()]

    with patch("youtube_transcript_api.YouTubeTranscriptApi", _FakeApi):
        result = await _youtube_transcript.fetch_youtube_transcript(
            "https://youtu.be/abcdefghijk",
        )

    # Output now has timestamp formatting (PR #42); just verify content + format.
    assert "hello" in result
    assert "[**0:00**]" in result
    # The kwarg is `http_client`, and it's a requests.Session whose
    # cookie jar contains the SID cookie loaded from the Netscape file.
    assert "http_client" in init_kwargs
    assert isinstance(init_kwargs["http_client"], requests.Session)
    cookie_names = {c.name for c in init_kwargs["http_client"].cookies}
    assert "SID" in cookie_names


@pytest.mark.asyncio
async def test_transcript_api_omits_http_client_when_file_missing(tmp_path, monkeypatch):
    from src.pipeline.extractors import _youtube_transcript

    from src.config import settings
    monkeypatch.setattr(settings, "youtube_cookies_path", str(tmp_path / "missing.txt"))

    init_kwargs: dict = {}

    class _FakeApi:
        def __init__(self, **kwargs):
            init_kwargs.update(kwargs)

        def fetch(self, video_id, languages=None):
            class _Snip:
                text = "hi"
                start = 0.0
            return [_Snip()]

    with patch("youtube_transcript_api.YouTubeTranscriptApi", _FakeApi):
        await _youtube_transcript.fetch_youtube_transcript("https://youtu.be/abcdefghijk")

    assert "http_client" not in init_kwargs
    assert "cookie_path" not in init_kwargs  # regression guard
