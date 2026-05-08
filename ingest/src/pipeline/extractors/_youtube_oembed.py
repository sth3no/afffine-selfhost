"""YouTube oEmbed fallback — works when cobalt + yt-dlp are blocked by bot detection.

YouTube's oEmbed endpoint (`https://www.youtube.com/oembed?url=...&format=json`) is
unauthenticated and returns title + author_name + thumbnail_url for any public
video. No login, no cookies. We use it as a last-resort metadata source when
cobalt returns `error.api.youtube.login` and yt-dlp also fails on bot detection.

Returns None on any error so the caller can decide a final fallback (URL-only).
"""

from __future__ import annotations

import logging
from urllib.parse import quote

import httpx

log = logging.getLogger(__name__)

_OEMBED_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)

# Tests inject a MockTransport here.
_TEST_TRANSPORT: httpx.AsyncBaseTransport | None = None


async def fetch_youtube_oembed(url: str) -> dict | None:
    """Return oEmbed metadata for a YouTube URL, or None on failure.

    Response shape (when successful):
        {
            "title": "Video Title",
            "author_name": "Channel Name",
            "author_url": "https://www.youtube.com/@channel",
            "thumbnail_url": "...",
            "type": "video",
            "html": "<iframe>...</iframe>",
            ...
        }
    """
    encoded = quote(url, safe=":/?=&%")
    endpoint = f"https://www.youtube.com/oembed?url={encoded}&format=json"

    kwargs: dict = {"timeout": _OEMBED_TIMEOUT}
    if _TEST_TRANSPORT is not None:
        kwargs["transport"] = _TEST_TRANSPORT

    try:
        async with httpx.AsyncClient(**kwargs) as client:
            resp = await client.get(endpoint)
        if resp.status_code != 200:
            log.warning("youtube oembed returned %s for %s", resp.status_code, url)
            return None
        return resp.json()
    except (httpx.HTTPError, ValueError) as e:
        log.warning("youtube oembed errored for %s: %s", url, e)
        return None
