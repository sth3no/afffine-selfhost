"""oEmbed-first extractor for X / Twitter.

Twitter's publish.twitter.com/oembed returns post HTML + author + URL.
Strip the HTML to text for the body. If the post contains a video, also
run yt-dlp to capture its captions/transcript and append.
"""

from __future__ import annotations

import re
from urllib.parse import urlencode

import httpx

from src.config import Platform, settings
from src.pipeline.extracted import Extracted, MediaKind, truncate_body
from src.pipeline.extractors import register_extractor


_OEMBED_BASE = "https://publish.twitter.com/oembed"
_HTML_TAG = re.compile(r"<[^>]+>")
_WHITESPACE = re.compile(r"\s+")


async def extract(url: str, platform: Platform, **_kwargs) -> Extracted:
    params = {"url": url, "omit_script": "true"}
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(_OEMBED_BASE + "?" + urlencode(params))
    if resp.status_code != 200:
        # Fall back to a marker-only Extracted; the URL remains the source of truth.
        return Extracted(
            title=None,
            body_md=f"_oEmbed unavailable ({resp.status_code}); see original post: {url}_",
            author=None,
            published_at=None,
            media_kind=MediaKind.MIXED,
            extra={"extractor": "oembed_ytdlp", "platform_id": platform.id, "oembed_status": resp.status_code},
        )

    data = resp.json()
    html = data.get("html") or ""
    text = _strip_html(html)
    author = data.get("author_name") or None

    body = f"# X post by {author or '(unknown)'}\n\n{text}"
    return Extracted(
        title=text[:80] if text else None,
        body_md=truncate_body(body, limit=settings.max_body_chars),
        author=author,
        published_at=None,
        media_kind=MediaKind.TEXT,
        extra={"extractor": "oembed_ytdlp", "platform_id": platform.id, "url": url},
    )


def _strip_html(html: str) -> str:
    text = _HTML_TAG.sub(" ", html)
    return _WHITESPACE.sub(" ", text).strip()


register_extractor("oembed_ytdlp", extract)
