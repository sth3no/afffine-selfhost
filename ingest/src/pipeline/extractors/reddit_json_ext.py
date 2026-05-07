"""Reddit post → Markdown via the public .json endpoint."""

from __future__ import annotations

from datetime import datetime, timezone

import httpx

from src.config import Platform, settings
from src.pipeline.extracted import Extracted, MediaKind, truncate_body
from src.pipeline.extractors import register_extractor


async def extract(url: str, platform: Platform) -> Extracted:
    json_url = url.split("?")[0].rstrip("/") + ".json"
    headers = {"User-Agent": "affine-ingest/0.1"}
    async with httpx.AsyncClient(timeout=10.0, headers=headers) as client:
        resp = await client.get(json_url)
    resp.raise_for_status()
    data = resp.json()

    post = data[0]["data"]["children"][0]["data"]
    title = post.get("title") or None
    author = post.get("author") or None
    selftext = post.get("selftext") or ""
    subreddit = post.get("subreddit") or ""
    created_utc = post.get("created_utc")
    published = (
        datetime.fromtimestamp(int(created_utc), tz=timezone.utc) if created_utc else None
    )

    parts = [f"# {title or '(untitled post)'}"]
    parts.append(f"_r/{subreddit} · u/{author or '(deleted)'}_")
    if selftext:
        parts.append("\n" + selftext)

    # Top 5 comments
    if len(data) > 1:
        comments = data[1]["data"]["children"]
        if comments:
            parts.append("\n## Top comments\n")
            for c in comments[:5]:
                d = c.get("data", {})
                if d.get("body"):
                    parts.append(f"- **u/{d.get('author', '?')}**: {d['body']}")

    body = "\n\n".join(parts)
    return Extracted(
        title=title,
        body_md=truncate_body(body, limit=settings.max_body_chars),
        author=author,
        published_at=published,
        media_kind=MediaKind.TEXT,
        extra={
            "extractor": "reddit_json",
            "platform_id": platform.id,
            "subreddit": subreddit,
            "url": url,
        },
    )


register_extractor("reddit_json", extract)
