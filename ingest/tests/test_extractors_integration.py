"""Live-stack integration tests for extractors. Hits real URLs.

Skipped unless INTEGRATION=1 in the environment. Use stable URLs so the
tests don't break on platform churn — pick arxiv (very stable IDs),
a documented YouTube channel video with captions, a public r/python
post.
"""

from __future__ import annotations

import os

import pytest

from src.config import Platform
from src.pipeline.extracted import MediaKind
from src.pipeline.extractors import get_extractor

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("INTEGRATION") != "1",
        reason="set INTEGRATION=1 to run live extractor tests",
    ),
]


def _plat(id_, group, folder, ext) -> Platform:
    return Platform(id=id_, group=group, folder_name=folder, hosts=["*"], extractor=ext)


@pytest.mark.asyncio
async def test_markitdown_against_arxiv():
    fn = get_extractor("markitdown")
    e = await fn("https://arxiv.org/abs/2401.00001",
                 _plat("arxiv", "Research papers", "arXiv", "markitdown"))
    assert e.body_md
    assert e.media_kind == MediaKind.TEXT


@pytest.mark.asyncio
async def test_reddit_against_public_post():
    fn = get_extractor("reddit_json")
    # Pick a thread that's been pinned/locked for years if possible.
    e = await fn("https://www.reddit.com/r/python/",
                 _plat("reddit", "Socials", "Reddit", "reddit_json"))
    assert e.body_md  # subreddit listing returns the same JSON shape


@pytest.mark.asyncio
async def test_oembed_against_public_x_post():
    fn = get_extractor("oembed_ytdlp")
    # Substitute a stable, public account/post.
    e = await fn("https://x.com/AnthropicAI/status/1",
                 _plat("x", "Socials", "X", "oembed_ytdlp"))
    # 404 is acceptable here — public marker check
    assert e.body_md


@pytest.mark.asyncio
async def test_ytdlp_against_short_youtube_with_captions():
    fn = get_extractor("ytdlp")
    # Short video known to have auto-captions.
    e = await fn("https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                 _plat("youtube", "Socials", "Youtube", "ytdlp"))
    assert e.title
    assert e.media_kind == MediaKind.VIDEO
