"""YouTube transcript fetcher via youtube-transcript-api.

Bypasses cobalt + yt-dlp YouTube auth blocks because YouTube's auto-caption
URLs are unauthenticated for any public video that has captions enabled.

Used by cobalt_ext as a transcript fallback when cobalt returns
`error.api.youtube.login`. Combined with oEmbed metadata, this restores
full search-quality content for the majority of YouTube captures even
when the audio download path is blocked.

Returns None on any failure — caller decides next fallback (e.g. body
note that transcript is unavailable).
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Sequence

log = logging.getLogger(__name__)


# Matches the 11-char video id in any of the standard YouTube URL shapes.
_VIDEO_ID_RE = re.compile(
    r"(?:v=|youtu\.be/|/embed/|/shorts/|/live/)([a-zA-Z0-9_-]{11})"
)


def _build_cookie_session(cookie_path: str):
    """Load a Netscape cookies.txt into a requests.Session for transcript-api.

    Returns the session, or None on any failure — caller falls back to
    no-cookies mode (still works for ~80% of public videos with auto-captions).
    """
    try:
        import http.cookiejar
        import requests
    except ImportError:
        return None

    try:
        jar = http.cookiejar.MozillaCookieJar(cookie_path)
        # ignore_discard=True: include session cookies (expires=0 in Netscape)
        # ignore_expires=True: don't drop cookies based on the expires column
        # (yt-dlp cookies dumps often have expires=0 even for non-session ones).
        jar.load(ignore_discard=True, ignore_expires=True)
    except (FileNotFoundError, OSError, http.cookiejar.LoadError) as e:
        log.warning("youtube_transcript: cookie load failed: %s", e)
        return None

    session = requests.Session()
    session.cookies = jar
    return session


def extract_video_id(url: str) -> str | None:
    """Return the 11-char YouTube video id from any URL shape, or None."""
    m = _VIDEO_ID_RE.search(url)
    return m.group(1) if m else None


async def fetch_youtube_transcript(
    url: str,
    *,
    languages: Sequence[str] = ("en", "cs"),
) -> str | None:
    """Return formatted transcript markdown for the given URL, or None.

    Output format: ~30-second paragraphs prefixed with a clickable
    `[**0:42**](url&t=42s)` markdown link that jumps to that moment in
    the YT video. Each paragraph in the doc body is independently
    navigable — no more "one wall of text".

    The youtube-transcript-api lib is synchronous; we run it in a thread
    to keep the worker's event loop responsive (transcript fetches are
    network I/O on YouTube's caption CDN, typically 1-3s).
    """
    video_id = extract_video_id(url)
    if not video_id:
        log.warning("youtube_transcript: no video id in URL %s", url)
        return None

    return await asyncio.to_thread(_fetch_sync, video_id, tuple(languages), url)


def _format_timestamp(seconds: float) -> str:
    """Format seconds as `H:MM:SS` (only includes H when >= 1 hour)."""
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m}:{sec:02d}"


def _format_transcript_with_timestamps(
    snippets,
    *,
    video_url: str,
    chunk_seconds: int = 30,
) -> str:
    """Group consecutive caption snippets into ~chunk_seconds paragraphs.

    Each paragraph starts with a clickable timestamp link in the form
    `[**0:42**](https://youtube.com/watch?v=ABC&t=42s)` followed by the
    consolidated text for that chunk. Markdown blank-line separated so
    the orchestrator's parser emits one block per chunk.
    """
    chunks: list[tuple[float, list[str]]] = []  # (start_seconds, [text, ...])
    current_start: float | None = None

    for snip in snippets:
        text = (getattr(snip, "text", "") or "").strip()
        if not text:
            continue
        start = float(getattr(snip, "start", 0.0) or 0.0)
        if current_start is None:
            current_start = start
            chunks.append((current_start, [text]))
            continue
        # Same chunk if within window AND latest chunk exists.
        if start - current_start < chunk_seconds and chunks:
            chunks[-1][1].append(text)
        else:
            current_start = start
            chunks.append((current_start, [text]))

    if not chunks:
        return ""

    # Build a clean YT URL with no trailing query state for the &t= link.
    base = video_url.split("&t=")[0].split("#")[0]
    sep = "&" if "?" in base else "?"

    parts: list[str] = []
    for start, lines in chunks:
        # Dedupe consecutive duplicates inside the chunk (auto-captions
        # often repeat the previous line on overlap).
        deduped: list[str] = []
        for line in lines:
            if not deduped or deduped[-1] != line:
                deduped.append(line)
        text = " ".join(deduped)
        ts = _format_timestamp(start)
        link = f"[**{ts}**]({base}{sep}t={int(start)}s)"
        parts.append(f"{link} {text}")

    return "\n\n".join(parts)


def _fetch_sync(video_id: str, languages: tuple[str, ...], video_url: str) -> str | None:
    """Synchronous fetch + format. Errors → None.

    Phase 12: cookies loaded into a requests.Session via MozillaCookieJar
    and passed as `http_client=` (youtube-transcript-api 1.x __init__
    only accepts `proxy_config` and `http_client`).

    Output is markdown with `[**mm:ss**](url&t=Ns)` clickable timestamp
    links per ~30-second paragraph — every paragraph jumps to that
    moment in the source video.
    """
    try:
        from youtube_transcript_api import (
            YouTubeTranscriptApi,
            TranscriptsDisabled,
            NoTranscriptFound,
            VideoUnavailable,
            CouldNotRetrieveTranscript,
        )
    except ImportError as e:
        log.warning("youtube_transcript_api not installed: %s", e)
        return None

    # Lazy import (config has its own deps that may not be available
    # in tests that mock the surrounding context).
    from src.config import settings
    from src.youtube_cookies import cookie_file_exists

    api_kwargs: dict = {}
    if cookie_file_exists(settings.youtube_cookies_path):
        session = _build_cookie_session(settings.youtube_cookies_path)
        if session is not None:
            api_kwargs["http_client"] = session

    try:
        ytt_api = YouTubeTranscriptApi(**api_kwargs)
        transcript = ytt_api.fetch(video_id, languages=list(languages))
    except (TranscriptsDisabled, NoTranscriptFound, VideoUnavailable):
        # Common, non-actionable failures — caller falls back to oEmbed-only.
        return None
    except CouldNotRetrieveTranscript as e:
        log.warning("youtube_transcript: could not retrieve %s: %s", video_id, e)
        return None
    except Exception as e:  # noqa: BLE001 — best-effort by design
        log.warning("youtube_transcript: unexpected error for %s: %s", video_id, e)
        return None

    # transcript is iterable of FetchedTranscriptSnippet objects with .text + .start.
    snippets = list(transcript)
    if not snippets:
        return None

    formatted = _format_transcript_with_timestamps(
        snippets,
        video_url=video_url,
        chunk_seconds=30,
    )
    return formatted or None
