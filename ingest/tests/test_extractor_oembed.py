from unittest.mock import patch, MagicMock

import httpx
import pytest

from src.config import Platform
from src.pipeline.extracted import MediaKind
from src.pipeline.extractors.oembed_ytdlp_ext import extract


def _platform() -> Platform:
    return Platform(id="x", group="Socials", folder_name="X",
                    hosts=["x.com", "twitter.com"], extractor="oembed_ytdlp")


@pytest.mark.asyncio
async def test_oembed_extracts_post_text_and_author():
    fake_response = MagicMock(status_code=200)
    fake_response.json.return_value = {
        "html": '<blockquote><p>Hello <a href="...">world</a> &mdash; testing.</p></blockquote>',
        "author_name": "@example",
    }

    with patch("src.pipeline.extractors.oembed_ytdlp_ext.httpx.AsyncClient") as Client:
        ctx = Client.return_value.__aenter__.return_value
        ctx.get.return_value = fake_response
        e = await extract("https://x.com/example/status/1", _platform())

    assert e.author == "@example"
    assert "Hello" in e.body_md
    assert "world" in e.body_md
    assert "<" not in e.body_md
    assert e.media_kind == MediaKind.TEXT


@pytest.mark.asyncio
async def test_oembed_unavailable_returns_marker_body():
    fake_response = MagicMock(status_code=404)
    with patch("src.pipeline.extractors.oembed_ytdlp_ext.httpx.AsyncClient") as Client:
        Client.return_value.__aenter__.return_value.get.return_value = fake_response
        e = await extract("https://x.com/locked/status/2", _platform())

    assert "oEmbed unavailable" in e.body_md
    assert e.author is None
    assert e.extra["oembed_status"] == 404
