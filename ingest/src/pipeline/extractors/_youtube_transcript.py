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


def extract_video_id(url: str) -> str | None:
    """Return the 11-char YouTube video id from any URL shape, or None."""
    m = _VIDEO_ID_RE.search(url)
    return m.group(1) if m else None


async def fetch_youtube_transcript(
    url: str,
    *,
    languages: Sequence[str] = ("en", "cs"),
) -> str | None:
    """Return joined transcript text for the given URL, or None on any failure.

    The youtube-transcript-api lib is synchronous; we run it in a thread to
    keep the worker's event loop responsive (transcript fetches are network
    I/O on YouTube's caption CDN, typically 1-3s).
    """
    video_id = extract_video_id(url)
    if not video_id:
        log.warning("youtube_transcript: no video id in URL %s", url)
        return None

    return await asyncio.to_thread(_fetch_sync, video_id, tuple(languages))


def _fetch_sync(video_id: str, languages: tuple[str, ...]) -> str | None:
    """Synchronous fetch + format. Errors → None.

    Phase 12: when a YouTube cookies file is present, pass it to
    YouTubeTranscriptApi so authenticated requests bypass the
    cloud-IP block. Without cookies, cloud-provider IPs typically
    get RequestBlocked.
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
        # youtube-transcript-api 1.x accepts cookie_path in __init__.
        api_kwargs["cookie_path"] = settings.youtube_cookies_path

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

    # transcript is iterable of FetchedTranscriptSnippet objects with .text.
    parts: list[str] = []
    for snippet in transcript:
        text = (getattr(snippet, "text", "") or "").strip()
        if text:
            parts.append(text)

    if not parts:
        return None

    # Dedupe consecutive duplicates (auto-captions repeat lines on overlap).
    deduped: list[str] = []
    for line in parts:
        if not deduped or deduped[-1] != line:
            deduped.append(line)
    return "\n".join(deduped)
