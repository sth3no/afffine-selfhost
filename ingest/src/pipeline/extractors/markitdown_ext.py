"""URL -> Markdown via Microsoft markitdown.

Used for: articles, arxiv, podcast pages, generic catch-all. Markitdown's
own URL fetch layer handles HTML->MD with reasonable cleanup; we wrap it
with truncation and the Extracted contract.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from markitdown import MarkItDown

from src.config import Platform, settings
from src.pipeline.extracted import Extracted, MediaKind, truncate_body
from src.pipeline.extractors import register_extractor


async def extract(url: str, platform: Platform, **_kwargs) -> Extracted:
    # markitdown is sync; run in a thread to avoid blocking the loop.
    md = MarkItDown()
    result = await asyncio.to_thread(md.convert, url)

    title = (getattr(result, "title", None) or "").strip() or None
    body = (result.text_content or "").strip()

    return Extracted(
        title=title,
        body_md=truncate_body(body, limit=settings.max_body_chars),
        author=None,
        published_at=None,
        media_kind=MediaKind.TEXT,
        extra={"extractor": "markitdown", "platform_id": platform.id},
    )


register_extractor("markitdown", extract)
